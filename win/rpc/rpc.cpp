/* The RPC worker thread.
 *
 * One command at a time, because that is what the permission bar shows and
 * what a 200MHz machine should be doing anyway. A second EXEC arriving while
 * one runs is answered `status=busy` rather than queued: the bridge already
 * serialises on its side, so a second one here means something has gone wrong
 * and silently queueing would hide it.
 *
 * Ownership: a frame handed to rc_rpc_submit belongs to THIS thread, which
 * frees it. The reader does not free it and the UI thread never sees it. See
 * the three-way contract in net.h -- getting it wrong is a double free,
 * which on 9x is a reboot.
 */

#include <windows.h>
#include <process.h>
#include <stdio.h>
#include <string.h>
#include <stdlib.h>
#include "rpc/rpc.h"
#include "rpc/exec.h"
#include "sync/syncrpc.h"
#include "sync/sync.h"
#include "core/log.h"
#include "core/str.h"

static HANDLE           g_thread;
/* The directory watcher, resolved dynamically. NULL is a supported outcome --
 * the poll below is the mechanism and this only shortens the wait. Experiment
 * 0.14 confirmed Win98 SE has it and that bWatchSubtree works on FAT, but a
 * static import would kill the image at load on a guest that lacks it. */
typedef HANDLE (WINAPI *FFCN)(LPCSTR, BOOL, DWORD);
typedef BOOL   (WINAPI *FNCN)(HANDLE);
typedef BOOL   (WINAPI *FCCN)(HANDLE);
static FFCN   p_first_change;
static FNCN   p_next_change;
static FCCN   p_close_change;
static HANDLE g_watch = INVALID_HANDLE_VALUE;
static DWORD  g_last_poll;
static DWORD  g_dirty_at;
static HANDLE           g_wake;
static CRITICAL_SECTION g_lock;
static int              g_ready;
static volatile LONG    g_running;
static rc_frame        *g_queued;       /* at most one, guarded by g_lock */
static rc_net          *g_net;

/* SYNC* frames get their own FIFO, and that is not a refinement -- it is the
 * difference between the layer working and not.
 *
 * A transfer is a STREAM: SYNCPUT off=0, off=8192, ... then SYNCPUTEND. Every
 * chunk after the first arrives while the worker is inside the previous one,
 * so the one-at-a-time job slot answers it `busy`. That rejects chunks 2..N of
 * every transfer over 8KB -- which is most of them.
 *
 * Bounded, and the reader thread waits when it is full rather than the queue
 * growing: this machine has 31MB and the bridge streams chunks without waiting
 * for an ack, so an unbounded queue is a whole file in RAM. Blocking the
 * reader closes the TCP window and the bridge's own drain() does the rest --
 * the same mechanism net.c already uses for UI frames. */
#define RPC_SYNCQ_MAX 24
static rc_frame        *g_syncq[RPC_SYNCQ_MAX];
static int              g_syncq_head;
static int              g_syncq_n;

/* Where the output of the command currently running goes. */
typedef struct {
    char id[64];
    int  lost;      /* an EXECOUT could not be queued */
} exec_ctx;

static int  is_sync(const rc_frame *f);

/* Everything the RPC layer answers to. See rpc.h for why this is here and not
 * in net.c's reader loop.
 *
 * The dispatch itself is a switch on the frame's LANE (frameproto.h), not on
 * its type: which lane a type belongs to is the lane table's job, so this
 * function does not grow a new branch every time Phase 6 adds a tool. A lane
 * with more than one member -- IMMEDIATE, once list_windows exists -- still
 * needs its own dispatch inside that case, the same way this one does for
 * EXECKILL today; the table only spares this function from also having to
 * know which TYPES map to which LANE. */
int rc_rpc_route(rc_frame *f, volatile LONG *running)
{
    if (!f) return RC_ROUTE_PASS;

    switch (rc_frame_lane(f->type)) {

    case RC_LANE_IMMEDIATE:
        /* EXECKILL is handled RIGHT HERE, on the reader thread, and never
         * queued. Queueing it behind the worker would be useless: the worker
         * is busy running the very command this is meant to stop, so the
         * frame would not be looked at until that command finished.
         * rc_exec_cancel is built to be called from another thread. */
        if (strcmp(f->type, "EXECKILL") == 0) {
            const char *kid = rc_frame_get(f, "id");
            /* rc_frame_get returns NULL for an absent header, and nothing
             * requires EXECKILL to carry an id. */
            rc_log("rpc: EXECKILL for %s", kid ? kid : "(no id)");
            rc_exec_cancel();
        }
        rc_frame_free(f);
        HeapFree(GetProcessHeap(), 0, f);
        return RC_ROUTE_TAKEN;

    case RC_LANE_JOB:
        if (rc_rpc_submit(f) != 0) {
            rc_frame_free(f);
            HeapFree(GetProcessHeap(), 0, f);
        }
        return RC_ROUTE_TAKEN;

    case RC_LANE_STREAM:
        /* SYNC* goes to the worker too, and this is load-bearing. Without it
         * every sync frame falls through to the UI thread and lands in
         * render_frame's "unhandled frame type" branch -- so the guest never
         * answers a SYNCSCAN, the bridge waits out its 600s timeout holding
         * the sync lock, and every later trigger logs "coalescing" forever.
         * The symptom is a sync layer that looks wired up on both sides and
         * transfers nothing, with no error on either.
         *
         * Backpressure BEFORE submit, not after: the worker can be inside a
         * two-minute EXEC, and the bridge streams put chunks without waiting
         * for an ack. Waiting here stops reading the socket, which is the one
         * form of pushback that costs no memory. */
        while (rc_rpc_syncq_full() && (!running || *running))
            Sleep(10);
        if (rc_rpc_submit(f) != 0) {
            rc_frame_free(f);
            HeapFree(GetProcessHeap(), 0, f);
        }
        return RC_ROUTE_TAKEN;

    case RC_LANE_UI:
    default:
        return RC_ROUTE_PASS;
    }
}
static void reject_sync_busy(rc_frame *f);

/* Generous, because the watcher shortens it when present -- and a brisk poll is
 * CPU a 200MHz machine does not have to spare. */
#define RPC_POLL_MS      200
#define RPC_SETTLE_MS   2000
#define RPC_SWEEP_MS   30000

/* Tell the bridge the tree moved, or that we are still here.
 *
 * SYNCDIRTY when there is a sync root: the bridge owns the baseline and
 * decides what to do; the guest never reconciles anything itself.
 *
 * PING when there is not, and that is the fix for a real disconnection. The
 * bridge reaps a guest that has been silent for 90 seconds, because a machine
 * that GPFs leaves no FIN behind and would otherwise hold the slot forever.
 * But `last_seen` only moves when a frame arrives, and with no sync root this
 * function used to send NOTHING -- so a user reading a permission prompt was
 * hung up on after 90 seconds, against a documented 600-second deadline for
 * that very prompt. The bridge has handled PING since the beginning and the
 * guest had never sent one. */
static void rpc_maybe_nudge(void)
{
    DWORD now = GetTickCount();
    int due = 0;

    if (g_dirty_at && (now - g_dirty_at) > RPC_SETTLE_MS) {
        due = 1;
        g_dirty_at = 0;
    } else if ((now - g_last_poll) > RPC_SWEEP_MS) {
        due = 1;
    }
    if (!due) return;

    g_last_poll = now;

    /* Throughput, measured rather than assumed.
     *
     * PLAN.md makes adaptive codec selection depend on this -- "the sender
     * picks the codec from the measured link speed" -- and the bridge has
     * handled STAT since the beginning. The guest had never sent one, so
     * guest.link_bps was always None and every threshold in compress.py and
     * coalesce.py was inert: the same code ran on a 10Mbit LAN and a modem,
     * which is the exact thing the mechanism exists to avoid.
     *
     * Bytes over wall time since the last sample, from counters net.c already
     * keeps. Only when something actually moved -- an idle link would
     * otherwise report a speed of nearly zero and pin the bridge to its
     * slowest codec for a link that is merely quiet. */
    {
        static unsigned long last_tx, last_rx;
        static DWORD last_at;
        unsigned long moved = (g_net->tx_bytes - last_tx)
                            + (g_net->rx_bytes - last_rx);
        DWORD elapsed = last_at ? (now - last_at) : 0;

        if (elapsed >= 1000 && moved > 0) {
            char bps[24];
            rc_strf(bps, sizeof(bps), "%lu",
                    (unsigned long)((double)moved * 1000.0 / elapsed));
            {
                const char *k[1]; const char *v[1];
                k[0] = "bps"; v[0] = bps;
                rc_net_send(g_net, "STAT", k, v, 1, NULL, 0);
            }
        }
        last_tx = g_net->tx_bytes;
        last_rx = g_net->rx_bytes;
        last_at = now;
    }

    if (rc_sync_root()[0])
        rc_net_send(g_net, "SYNCDIRTY", NULL, NULL, 0, NULL, 0);
    else
        rc_net_send(g_net, "PING", NULL, NULL, 0, NULL, 0);
}

static void rpc_watch_start(void)
{
    HMODULE k32 = GetModuleHandleA("kernel32.dll");
    const char *root = rc_sync_root();

    p_first_change = (FFCN)GetProcAddress(k32, "FindFirstChangeNotificationA");
    p_next_change  = (FNCN)GetProcAddress(k32, "FindNextChangeNotification");
    p_close_change = (FCCN)GetProcAddress(k32, "FindCloseChangeNotification");

    if (!root[0] || !p_first_change || !p_next_change) {
        rc_log("rpc: no directory watcher; polling every %dms", RPC_SWEEP_MS);
        return;
    }
    g_watch = p_first_change(root, TRUE,
                             FILE_NOTIFY_CHANGE_FILE_NAME |
                             FILE_NOTIFY_CHANGE_LAST_WRITE |
                             FILE_NOTIFY_CHANGE_SIZE);
    if (g_watch == INVALID_HANDLE_VALUE || g_watch == NULL) {
        rc_log("rpc: watcher failed on %s (err=%lu); polling instead",
               root, (unsigned long)GetLastError());
        g_watch = INVALID_HANDLE_VALUE;
    } else {
        rc_log("rpc: watching %s", root);
    }
}

static void send_out(void *ctx, const char *data, unsigned len)
{
    exec_ctx *c = (exec_ctx *)ctx;
    const char *keys[1];
    const char *vals[1];

    if (!len) return;
    keys[0] = "id"; vals[0] = c->id;
    /* Straight onto the outbound queue; the sender thread owns the wire, so
     * this does not block on a slow link.
     *
     * The return matters. rc_net_send fails on a dead socket, an oversized
     * frame or a failed HeapAlloc, and silently dropping a chunk would leave
     * a HOLE in the output that nothing marks -- the model would be handed a
     * plausible-looking but incomplete `dir` listing and have no way to know.
     * Flagging it here makes the EXECRESULT say so. */
    if (rc_net_send(g_net, "EXECOUT", keys, vals, 1, data, len) != 0) {
        c->lost = 1;
        rc_log("rpc: EXECOUT %s dropped (%u bytes); output is incomplete",
               c->id, len);
    }
}

static void run_one(rc_frame *f)
{
    const char *id = rc_frame_get(f, "id");
    const char *tmo = rc_frame_get(f, "timeout_ms");
    const char *sh = rc_frame_get(f, "shell");
    const char *cwd = rc_frame_get(f, "cwd");
    const char *keys[5];
    const char *vals[5];
    char code[16], bytes[16], trunc[4];
    const char *status = "ok";
    exec_ctx ctx;
    rc_exec_result r;
    DWORD timeout_ms;
    int nkv = 0;

    if (!id) { rc_log("rpc: EXEC with no id, dropped"); return; }
    /* Reject rather than truncate: a shortened id produces an EXECRESULT the
     * bridge cannot match, and the caller then waits out its whole budget. */
    if (strlen(id) >= sizeof(ctx.id)) {
        rc_log("rpc: EXEC id too long, dropped");
        return;
    }
    ctx.lost = 0;
    rc_strf(ctx.id, sizeof(ctx.id), "%s", id);

    timeout_ms = tmo ? (DWORD)atol(tmo) : 20000;
    if (timeout_ms < 1000)   timeout_ms = 1000;
    if (timeout_ms > 120000) timeout_ms = 120000;

    /* The command is in the BODY, never a header: a header value is truncated
     * at 255 bytes silently, and a truncated command would still be run. */
    rc_log("rpc: exec %s (%lums): %.120s", ctx.id,
           (unsigned long)timeout_ms, f->body ? f->body : "");

    /* Arm first, then re-check: a cancel landing after the arm is caught by
     * the run loop, and one landing before it is caught here. */
    rc_exec_arm();
    if (!g_running) {
        const char *k[2]; const char *v[2];
        k[0] = "id";     v[0] = ctx.id;
        k[1] = "status"; v[1] = "cancelled";
        rc_net_send(g_net, "EXECRESULT", k, v, 2, NULL, 0);
        rc_log("rpc: exec %s abandoned, shutting down", ctx.id);
        return;
    }

    memset(&r, 0, sizeof(r));
    rc_exec_run(f->body ? f->body : "", cwd,
                (sh && *sh == '0') ? 0 : 1,
                timeout_ms, send_out, &ctx, &r);

    switch (r.status) {
    case RC_EXEC_TIMEOUT:      status = "timeout";      break;
    case RC_EXEC_SPAWN_FAILED: status = "spawn_failed"; break;
    case RC_EXEC_CANCELLED:    status = "cancelled";    break;
    case RC_EXEC_TOO_LONG:     status = "too_long";     break;
    default:                   status = "ok";           break;
    }

    rc_strf(code, sizeof(code), "%lu", (unsigned long)r.code);
    rc_strf(bytes, sizeof(bytes), "%lu", (unsigned long)r.bytes);
    /* Output lost on the wire is reported the same way as output past the
     * cap: either way the model must not treat what arrived as complete. */
    rc_strf(trunc, sizeof(trunc), "%d",
              (r.truncated || ctx.lost) ? 1 : 0);

    keys[nkv] = "id";        vals[nkv++] = ctx.id;
    keys[nkv] = "status";    vals[nkv++] = status;
    keys[nkv] = "code";      vals[nkv++] = code;
    /* `bytes` is GONE from the frame, deliberately. It counted bytes PRODUCED
     * -- including everything discarded past the 32KB cap, and including
     * chunks dropped on the wire -- so it could never equal what the bridge
     * received, and an integrity check written against it would have failed on
     * every truncated command. The bridge never read it. `truncated` already
     * carries the only fact the model acts on. It stays in the log below,
     * where "produced" is exactly the number worth having. */
    keys[nkv] = "truncated"; vals[nkv++] = trunc;
    rc_net_send(g_net, "EXECRESULT", keys, vals, nkv, NULL, 0);

    rc_log("rpc: exec %s done status=%s code=%lu bytes=%lu in %lums",
           ctx.id, status, (unsigned long)r.code,
           (unsigned long)r.bytes, (unsigned long)r.ms);
}

static void reject_busy(rc_frame *f)
{
    const char *id = rc_frame_get(f, "id");
    const char *keys[2];
    const char *vals[2];
    if (!id) return;
    keys[0] = "id";     vals[0] = id;
    keys[1] = "status"; vals[1] = "busy";
    rc_net_send(g_net, "EXECRESULT", keys, vals, 2, NULL, 0);
}

static void drop(rc_frame *f)
{
    rc_frame_free(f);
    HeapFree(GetProcessHeap(), 0, f);
}

static unsigned __stdcall rpc_thread(void *arg)
{
    (void)arg;
    while (g_running) {
        rc_frame *f = NULL;

        /* Sync first, always. An EXEC runs for up to two minutes and a sync
         * chunk queued behind it stalls a transfer the bridge is holding a
         * future for; a sync frame, by contrast, is a few milliseconds of file
         * I/O, so draining it first cannot starve anything. */
        EnterCriticalSection(&g_lock);
        if (g_syncq_n) {
            f = g_syncq[g_syncq_head];
            g_syncq[g_syncq_head] = NULL;
            g_syncq_head = (g_syncq_head + 1) % RPC_SYNCQ_MAX;
            g_syncq_n--;
            LeaveCriticalSection(&g_lock);
            rc_sync_handle(g_net, f);
            drop(f);
            continue;
        }
        f = g_queued;
        g_queued = NULL;
        LeaveCriticalSection(&g_lock);

        if (!f) {
            /* The watcher is an ACCELERATOR. Polling is the mechanism, so the
             * design is correct with the notification never firing -- which is
             * what makes a guest without the export merely slower rather than
             * broken.
             *
             * The branch matters: FindFirstChangeNotificationA returns
             * INVALID_HANDLE_VALUE (-1), not NULL, on a missing root, and
             * WaitForMultipleObjects on an invalid handle returns WAIT_FAILED
             * IMMEDIATELY -- turning this loop into a 100% busy spin on a
             * 200MHz machine. */
            if (g_watch != INVALID_HANDLE_VALUE && g_watch != NULL) {
                HANDLE hs[2];
                DWORD r;
                hs[0] = g_wake;
                hs[1] = g_watch;
                r = WaitForMultipleObjects(2, hs, FALSE, RPC_POLL_MS);
                if (r == WAIT_OBJECT_0 + 1) {
                    /* "Something under here changed" -- 9x gives no detail, so
                     * note the time and rescan after the tree settles. A
                     * compile would otherwise trigger a hundred passes. */
                    g_dirty_at = GetTickCount();
                    if (p_next_change) p_next_change(g_watch);
                }
            } else {
                WaitForSingleObject(g_wake, RPC_POLL_MS);
            }
            rpc_maybe_nudge();
            continue;
        }
        run_one(f);
        drop(f);
    }

    /* Anything still queued at shutdown is answered, not silently dropped --
     * the bridge is holding a future for it. */
    EnterCriticalSection(&g_lock);
    while (g_syncq_n) {
        rc_frame *q = g_syncq[g_syncq_head];
        g_syncq[g_syncq_head] = NULL;
        g_syncq_head = (g_syncq_head + 1) % RPC_SYNCQ_MAX;
        g_syncq_n--;
        LeaveCriticalSection(&g_lock);
        reject_sync_busy(q);
        drop(q);
        EnterCriticalSection(&g_lock);
    }
    if (g_queued) {
        rc_frame *f = g_queued;
        g_queued = NULL;
        LeaveCriticalSection(&g_lock);
        reject_busy(f);
        drop(f);
    } else {
        LeaveCriticalSection(&g_lock);
    }
    return 0;
}

int rc_rpc_start(rc_net *net)
{
    unsigned tid = 0;
    if (g_thread) return 0;
    g_net = net;
    if (!g_ready) {
        InitializeCriticalSection(&g_lock);
        g_ready = 1;
    }
    rc_exec_init();
    if (!g_wake) g_wake = CreateEventA(NULL, FALSE, FALSE, NULL);
    g_running = 1;
    /* _beginthreadex, not CreateThread: Open Watcom's multithreaded CRT keeps
     * per-thread data in a list, and a raw CreateThread thread gets it
     * allocated lazily and never released. Same reasoning as net.c's reader. */
    g_thread = (HANDLE)_beginthreadex(NULL, 0, rpc_thread, NULL, 0, &tid);
    if (!g_thread) {
        rc_log("rpc: could not start worker thread");
        g_running = 0;
        return -1;
    }
    rpc_watch_start();
    rc_log("rpc: worker started");
    return 0;
}

/* Reject a sync frame with the SYNC vocabulary, not EXECRESULT.
 *
 * Answering a SYNCPUT chunk with EXECRESULT was the defect that made the first
 * design structurally unable to work: the bridge looks the id up in
 * exec_pending, finds nothing, logs at debug and returns -- a branch added for
 * EXECKILL -- so the transfer hangs forever holding a one-chunk temp file. */
/* Answer a full queue with the ack the REQUEST expects.
 *
 * This always sent SYNCPUTACK, whatever had arrived. The bridge routes an
 * ack by id, so a SYNCPUTACK against a SCAN or a GET reaches the right
 * future carrying the wrong SHAPE -- two values where the awaiting code
 * unpacks six or four. That surfaces as "sync pass aborted: not enough
 * values to unpack", which names neither the frame nor the queue being full.
 * SYNCMKDIR happened to survive, because the bridge handles both acks in one
 * id-keyed branch.
 *
 * The busy path is rare -- it needs the worker's sync FIFO full -- which is
 * exactly why it is worth getting right: a rare path that reports the wrong
 * thing costs an investigation, not a retry. */
static void reject_sync_busy(rc_frame *f)
{
    const char *id = rc_frame_get(f, "id");
    const char *keys[2];
    const char *vals[2];
    const char *ack = "SYNCPUTACK";

    if (!id) return;
    if (strcmp(f->type, "SYNCSCAN") == 0)        ack = "SYNCEND";
    else if (strcmp(f->type, "SYNCGET") == 0)    ack = "SYNCDATEND";
    else if (strcmp(f->type, "SYNCMKDIR") == 0)  ack = "SYNCMKDIRACK";

    keys[0] = "id";     vals[0] = id;
    keys[1] = "status"; vals[1] = "busy";
    rc_net_send(g_net, ack, keys, vals, 2, NULL, 0);
}

/* Is this frame a JOB (one at a time, runs for a long time) or STATE MACHINE
 * INPUT (a chunk in a stream that must never queue behind anything)?
 *
 * Streaming chunks are not jobs. Putting them through the one-at-a-time gate
 * means chunk 1 is accepted and chunks 2..N are rejected while the worker is
 * busy writing chunk 1 -- which is every transfer of more than 8KB.
 *
 * Answered from RC_LANE_TABLE's wildcard rule (frameproto.c) rather than its
 * own copy of the SYNC* prefix test -- two prefix checks drift the moment one
 * of them is edited and the other is not. */
static int is_sync(const rc_frame *f)
{
    return rc_frame_lane(f->type) == RC_LANE_STREAM;
}

/* Correlation-id namespaces are ENFORCED, not conventional. Exec ids are x<n>
 * and sync ids are s<n>; today they differ only by a prefix nobody checks, and
 * a stray frame answering the wrong subsystem's future is the kind of bug that
 * takes a day to find. */
static int id_matches_family(const rc_frame *f)
{
    const char *id = rc_frame_get(f, "id");
    if (!id || !*id) return 0;
    return is_sync(f) ? (id[0] == 's') : (id[0] == 'x');
}

/* Reader-thread backpressure. The reader waits on this rather than the queue
 * growing, so a stalled worker costs TCP window instead of heap. */
int rc_rpc_syncq_full(void)
{
    int n;
    if (!g_ready) return 0;
    EnterCriticalSection(&g_lock);
    n = g_syncq_n;
    LeaveCriticalSection(&g_lock);
    return n >= RPC_SYNCQ_MAX;
}

int rc_rpc_submit(rc_frame *f)
{
    int busy = 0;

    if (!g_thread || !g_running) return -1;

    if (!id_matches_family(f)) {
        rc_log("rpc: %s carries id %s, which belongs to another subsystem",
               f->type, rc_frame_get(f, "id") ? rc_frame_get(f, "id") : "(none)");
        drop(f);
        return 0;
    }

    if (is_sync(f)) {
        int full = 0;
        EnterCriticalSection(&g_lock);
        if (g_syncq_n >= RPC_SYNCQ_MAX) {
            full = 1;
        } else {
            g_syncq[(g_syncq_head + g_syncq_n) % RPC_SYNCQ_MAX] = f;
            g_syncq_n++;
        }
        LeaveCriticalSection(&g_lock);
        if (full) {
            /* The caller is rc_rpc_syncq_full()-aware and should not have got
             * here; answering `busy` is better than dropping the frame, which
             * would leave the bridge waiting out its whole timeout. */
            reject_sync_busy(f);
            drop(f);
            return 0;
        }
        SetEvent(g_wake);
        return 0;
    }

    EnterCriticalSection(&g_lock);
    if (g_queued) busy = 1;
    else          g_queued = f;
    LeaveCriticalSection(&g_lock);

    if (busy) {
        /* Answer in the frame family's OWN vocabulary. The bridge is waiting on
         * a sync future or an exec future, and only one of them understands
         * EXECRESULT. */
        if (is_sync(f)) reject_sync_busy(f);
        else            reject_busy(f);
        drop(f);
        return 0;
    }
    SetEvent(g_wake);
    return 0;
}

void rc_rpc_stop(void)
{
    g_running = 0;
    rc_exec_cancel();           /* kill a child that could outlive us */
    if (g_wake) SetEvent(g_wake);
}

int rc_rpc_join(DWORD timeout_ms)
{
    DWORD w;
    if (g_watch != INVALID_HANDLE_VALUE && p_close_change) {
        p_close_change(g_watch);
        g_watch = INVALID_HANDLE_VALUE;
    }
    if (!g_thread) return 1;
    w = WaitForSingleObject(g_thread, timeout_ms);
    if (w != WAIT_OBJECT_0) return 0;
    CloseHandle(g_thread);
    g_thread = NULL;
    if (g_wake) { CloseHandle(g_wake); g_wake = NULL; }
    return 1;
}
