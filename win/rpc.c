/* The RPC worker thread.
 *
 * One command at a time, because that is what the permission bar shows and
 * what a 200MHz machine should be doing anyway. A second EXEC arriving while
 * one runs is answered `status=busy` rather than queued: the bridge already
 * serialises on its side, so a second one here means something has gone wrong
 * and silently queueing would hide it.
 *
 * Ownership: a frame handed to rc_rpc_submit belongs to THIS thread, which
 * frees it and decrements net->rpcq. The reader does not free it and the UI
 * thread never sees it. See the three-way contract in net.h -- getting it
 * wrong is a double free, which on 9x is a reboot.
 */

#include <windows.h>
#include <process.h>
#include <stdio.h>
#include <string.h>
#include <stdlib.h>
#include "rpc.h"
#include "exec.h"
#include "log.h"

static HANDLE           g_thread;
static HANDLE           g_wake;
static CRITICAL_SECTION g_lock;
static int              g_ready;
static volatile LONG    g_running;
static rc_frame        *g_queued;       /* at most one, guarded by g_lock */
static rc_net          *g_net;

/* Where the output of the command currently running goes. */
typedef struct {
    char id[64];
} exec_ctx;

static void send_out(void *ctx, const char *data, unsigned len)
{
    exec_ctx *c = (exec_ctx *)ctx;
    const char *keys[1];
    const char *vals[1];

    if (!len) return;
    keys[0] = "id"; vals[0] = c->id;
    /* Straight onto the outbound queue; the sender thread owns the wire, so
     * this does not block on a slow link. */
    rc_net_send(g_net, "EXECOUT", keys, vals, 1, data, len);
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
    _snprintf(ctx.id, sizeof(ctx.id) - 1, "%s", id);
    ctx.id[sizeof(ctx.id) - 1] = '\0';

    timeout_ms = tmo ? (DWORD)atol(tmo) : 20000;
    if (timeout_ms < 1000)   timeout_ms = 1000;
    if (timeout_ms > 120000) timeout_ms = 120000;

    /* The command is in the BODY, never a header: a header value is truncated
     * at 255 bytes silently, and a truncated command would still be run. */
    rc_log("rpc: exec %s (%lums): %.120s", ctx.id,
           (unsigned long)timeout_ms, f->body ? f->body : "");

    memset(&r, 0, sizeof(r));
    rc_exec_run(f->body ? f->body : "", cwd,
                (sh && *sh == '0') ? 0 : 1,
                timeout_ms, send_out, &ctx, &r);

    switch (r.status) {
    case RC_EXEC_TIMEOUT:      status = "timeout";      break;
    case RC_EXEC_SPAWN_FAILED: status = "spawn_failed"; break;
    case RC_EXEC_CANCELLED:    status = "cancelled";    break;
    case RC_EXEC_TOO_LONG:     status = "spawn_failed"; break;
    default:                   status = "ok";           break;
    }

    _snprintf(code, sizeof(code), "%lu", (unsigned long)r.code);
    _snprintf(bytes, sizeof(bytes), "%lu", (unsigned long)r.bytes);
    _snprintf(trunc, sizeof(trunc), "%d", r.truncated ? 1 : 0);

    keys[nkv] = "id";        vals[nkv++] = ctx.id;
    keys[nkv] = "status";    vals[nkv++] = status;
    keys[nkv] = "code";      vals[nkv++] = code;
    keys[nkv] = "bytes";     vals[nkv++] = bytes;
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
    if (g_net) InterlockedDecrement(&g_net->rpcq);
}

static unsigned __stdcall rpc_thread(void *arg)
{
    (void)arg;
    while (g_running) {
        rc_frame *f = NULL;

        EnterCriticalSection(&g_lock);
        f = g_queued;
        g_queued = NULL;
        LeaveCriticalSection(&g_lock);

        if (!f) {
            /* Timed, so `running` going to 0 is noticed even with an idle
             * queue. */
            WaitForSingleObject(g_wake, 200);
            continue;
        }
        run_one(f);
        drop(f);
    }

    /* Anything still queued at shutdown is answered, not silently dropped --
     * the bridge is holding a future for it. */
    EnterCriticalSection(&g_lock);
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
    rc_log("rpc: worker started");
    return 0;
}

int rc_rpc_submit(rc_frame *f)
{
    int busy = 0;

    if (!g_thread || !g_running) return -1;

    EnterCriticalSection(&g_lock);
    if (g_queued) busy = 1;
    else          g_queued = f;
    LeaveCriticalSection(&g_lock);

    if (busy) {
        /* One worker, one command. Answer rather than queue: the bridge
         * serialises already, so a second one means a bug, and queueing would
         * hide it behind a mysterious delay. */
        reject_busy(f);
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
    if (!g_thread) return 1;
    w = WaitForSingleObject(g_thread, timeout_ms);
    if (w != WAIT_OBJECT_0) return 0;
    CloseHandle(g_thread);
    g_thread = NULL;
    if (g_wake) { CloseHandle(g_wake); g_wake = NULL; }
    return 1;
}
