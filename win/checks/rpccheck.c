/* Headless conformance check for the RPC lane routing (04.5).
 *
 * Links the real frameproto.c and rpc.c, so this exercises the same lane
 * table and the same worker rc_rpc_route drives, without needing a live
 * bridge -- rc_net_send is a safe no-op whenever `net->running` is 0, which a
 * zeroed rc_net never sets, so every EXECRESULT/EXECOUT this check provokes
 * is built, discarded, and never touches a socket. See net.c's rc_net_send:
 * `if (n->sock == INVALID_SOCKET || !n->running) return -1;` is checked
 * before anything that would need a real connection.
 *
 * Two properties, matching the two things 04.5 asked for:
 *
 *   - the LANE TABLE classifies each frame type the way the code that reads
 *     it actually behaves, not the way its name suggests;
 *   - a frame in the IMMEDIATE lane -- EXECKILL, today -- is answered without
 *     ever waiting on the JOB lane's one slot, even while that slot is
 *     demonstrably busy running something that cannot finish on its own.
 */
#include <windows.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "net/frameproto.h"
#include "net/net.h"
#include "rpc/rpc.h"
#include "core/log.h"

/* Zero-initialised: sock is 0 (not a real socket) and running is 0, which is
 * the field rc_net_send actually checks before touching the wire. rc_rpc_*
 * never calls rc_net_start, so `running` stays 0 for the whole check and
 * every send from inside the worker is a guaranteed, harmless no-op. */
static rc_net g_net;
static volatile LONG g_always_running = 1;

static int g_failures;

static const char *lane_name(rc_lane l)
{
    switch (l) {
    case RC_LANE_UI:        return "UI";
    case RC_LANE_JOB:       return "JOB";
    case RC_LANE_STREAM:    return "STREAM";
    case RC_LANE_IMMEDIATE: return "IMMEDIATE";
    default:                return "?";
    }
}

static void check_lane(const char *type, rc_lane want)
{
    rc_lane got = rc_frame_lane(type);
    int ok = (got == want);
    if (!ok) g_failures++;
    printf("%s\tlane(%s) == %s\tgot %s\n",
           ok ? "pass" : "FAIL", type, lane_name(want), lane_name(got));
}

/* Builds a frame exactly the way the reader thread does: encode with the real
 * rc_frame_build, then hand the payload to rc_frame_parse so this exercises
 * the real wire round trip, not a hand-built struct. Ownership matches
 * net.c's: the caller owns the returned frame until it is handed to
 * rc_rpc_route/rc_rpc_submit (which free it) or freed directly. */
static rc_frame *make_frame(const char *type, const char *const *keys,
                            const char *const *vals, int nkv,
                            const char *body)
{
    char build[512];
    int total;
    unsigned len;
    char *payload;
    rc_frame *f;

    total = rc_frame_build(build, sizeof(build), type, keys, vals, nkv,
                           body, body ? (unsigned)strlen(body) : 0);
    if (total < 4) return NULL;
    len = (unsigned)total - 4;

    payload = (char *)HeapAlloc(GetProcessHeap(), 0, len + 1);
    if (!payload) return NULL;
    memcpy(payload, build + 4, len);
    payload[len] = '\0';

    f = (rc_frame *)HeapAlloc(GetProcessHeap(), 0, sizeof(rc_frame));
    if (!f) { HeapFree(GetProcessHeap(), 0, payload); return NULL; }

    if (rc_frame_parse(payload, len, f) != 0) {
        HeapFree(GetProcessHeap(), 0, payload);
        HeapFree(GetProcessHeap(), 0, f);
        return NULL;
    }
    return f;
}

static void free_frame(rc_frame *f)
{
    if (!f) return;
    rc_frame_free(f);
    HeapFree(GetProcessHeap(), 0, f);
}

/* --- the table itself ----------------------------------------------------- */

static void lane_table_cases(void)
{
    check_lane("EXEC",       RC_LANE_JOB);
    check_lane("EXECKILL",   RC_LANE_IMMEDIATE);
    check_lane("SYNCSCAN",   RC_LANE_STREAM);
    check_lane("SYNCPUT",    RC_LANE_STREAM);
    check_lane("SYNCPUTEND", RC_LANE_STREAM);
    check_lane("SYNCMKDIR",  RC_LANE_STREAM);
    check_lane("SYNCGET",    RC_LANE_STREAM);
    /* Everything else is the UI thread's, including a type nobody has heard
     * of -- an unrecognised frame must never fall into a worker lane and be
     * silently eaten before render_frame's own "unhandled" branch sees it. */
    check_lane("TEXT",   RC_LANE_UI);
    check_lane("READY",  RC_LANE_UI);
    check_lane("TURN",   RC_LANE_UI);
    check_lane("BOGUS",  RC_LANE_UI);
}

/* The slow JOB's marker: a file it appends one line to on every pass of its
 * loop. A byte count read from disk is an observable that does not go
 * through rc_net_send, so it is not confused by rc_rpc_stop's own call to
 * rc_exec_cancel() -- see the note below on why that confusion matters. The
 * child inherits this process's current directory (exec.c: a NULL cwd passes
 * NULL to CreateProcessA, which means "inherit"), so the path is relative. */
#define MARKER_FILE "rpccheck_marker.txt"

static DWORD marker_size(void)
{
    HANDLE h = CreateFileA(MARKER_FILE, GENERIC_READ,
                           FILE_SHARE_READ | FILE_SHARE_WRITE, NULL,
                           OPEN_EXISTING, 0, NULL);
    DWORD sz;
    if (h == INVALID_HANDLE_VALUE) return 0;
    sz = GetFileSize(h, NULL);
    CloseHandle(h);
    return (sz == INVALID_FILE_SIZE) ? 0 : sz;
}

/* --- the property: IMMEDIATE is never behind JOB's one slot --------------- */

static void immediate_survives_busy_job(void)
{
    const char *xkeys[3]; const char *xvals[3];
    const char *kkeys[1]; const char *kvals[1];
    rc_frame *slow, *kill;
    DWORD route_t0, route_t1;
    DWORD size_a, size_b, size_c;
    int taken;

    DeleteFileA(MARKER_FILE);      /* a stale marker from a crashed run */
    rc_rpc_start(&g_net);

    /* A JOB that cannot finish on its own: FOR /L inside cmd.exe's own
     * interpreter, no external process, counting far past anything this check
     * waits for -- deterministic regardless of which utilities the guest, or
     * Wine, happens to ship. (A `:label` / `goto` pair does NOT loop when
     * passed as a single `/c` line -- cmd.exe only resolves labels within a
     * batch FILE -- so this check used that first and measured zero marker
     * growth; FOR /L is the construct that actually works from `/c`.) Only
     * the EXEC timeout below, or a cancel, ends it. It also writes the
     * marker once per pass, which is what makes "still running" observable
     * from out here without a live bridge to carry an EXECRESULT back.
     *
     * shell=0, and cmd.exe named explicitly in the body: exec.c's shell_name()
     * picks command.com whenever GetVersion()'s high bit is set, which this
     * project's Wine prefix deliberately does (CLAUDE.md: "verify on the
     * guest, not under Wine" -- the whole point is that the guest code runs
     * unmodified), and Wine has no command.com to spawn. That is a property of
     * WHICH SHELL RUNS, not of the JOB/IMMEDIATE lane machinery this check
     * exists to test, so it is worked around with the wire protocol's own
     * `shell` header rather than by faking anything. */
    xkeys[0] = "id";         xvals[0] = "x1";
    xkeys[1] = "timeout_ms"; xvals[1] = "8000";
    xkeys[2] = "shell";      xvals[2] = "0";
    slow = make_frame("EXEC", xkeys, xvals, 3,
                      "cmd.exe /c for /l %i in (1,1,50000000) do echo t>>"
                      MARKER_FILE);
    if (!slow) { printf("FAIL\tbuild slow EXEC frame\t(alloc)\n"); g_failures++; return; }

    /* rc_rpc_submit takes ownership on success (0); this check's frame
     * ownership rules mirror net.h's three-way contract exactly. */
    if (rc_rpc_submit(slow) != 0) {
        printf("FAIL\tsubmit slow EXEC\t(rejected)\n");
        g_failures++;
        free_frame(slow);
        rc_rpc_stop(); rc_rpc_join(5000);
        return;
    }

    /* Give the worker time to actually be INSIDE the infinite loop -- not
     * just queued -- so the JOB lane's one slot is unambiguously busy for the
     * rest of this function, not merely racing rpc_thread for who reads
     * g_queued first. CreateProcess under Wine is the slow part; 500ms is
     * generous against it and tiny against the 8000ms timeout below. */
    Sleep(800);
    size_a = marker_size();

    /* THE PROPERTY: EXECKILL must be answered in a handful of milliseconds,
     * not wait out any part of x1's run. A wrongly-routed IMMEDIATE frame --
     * one sent through rc_rpc_submit as if it were a JOB -- would not block
     * this call either, since rc_rpc_submit only enqueues; that is exactly
     * why route latency alone cannot prove the routing is right, and why the
     * marker checks below exist. */
    kkeys[0] = "id"; kvals[0] = "x1";
    kill = make_frame("EXECKILL", kkeys, kvals, 1, "");
    if (!kill) { printf("FAIL\tbuild EXECKILL frame\t(alloc)\n"); g_failures++; }
    else {
        route_t0 = GetTickCount();
        taken = rc_rpc_route(kill, &g_always_running);
        route_t1 = GetTickCount();

        {
            int ok_taken = (taken == RC_ROUTE_TAKEN);
            int ok_fast  = (route_t1 - route_t0) < 1000; /* vs. an 8000ms JOB */
            if (!ok_taken) g_failures++;
            if (!ok_fast)  g_failures++;
            printf("%s\tEXECKILL routed while JOB lane busy\tRC_ROUTE_TAKEN=%d\n",
                   ok_taken ? "pass" : "FAIL", taken);
            printf("%s\tEXECKILL call itself did not block\t%lums\n",
                   ok_fast ? "pass" : "FAIL",
                   (unsigned long)(route_t1 - route_t0));
        }
    }

    /* THE PROOF that EXECKILL, not the eventual rc_rpc_stop() below, is what
     * ended x1: sample the marker twice, 600ms apart, well after the ~100ms
     * poll granularity exec.c's run loop checks g_cancel at, and well before
     * x1's own 8000ms timeout could explain a stop on its own. If EXECKILL
     * actually reached the child, both samples are taken after it is already
     * dead, so the file has stopped growing and size_b == size_c. If EXECKILL
     * was answered but never reached the child -- the exact bug a misrouted
     * IMMEDIATE frame would produce -- x1 is still alive and still appending,
     * and this is what would catch it: rc_rpc_stop() has not run yet. */
    Sleep(600);
    size_b = marker_size();
    Sleep(600);
    size_c = marker_size();
    {
        int grew_at_all = size_a > 0 || size_b > 0;
        int stopped = (size_b == size_c);
        if (!grew_at_all) g_failures++;
        if (!stopped)     g_failures++;
        printf("%s\tslow JOB was genuinely running\tmarker grew %lu -> %lu\n",
               grew_at_all ? "pass" : "FAIL",
               (unsigned long)size_a, (unsigned long)size_b);
        printf("%s\tslow JOB stopped promptly after EXECKILL, before rc_rpc_stop"
               "\tmarker steady %lu == %lu\n",
               stopped ? "pass" : "FAIL",
               (unsigned long)size_b, (unsigned long)size_c);
    }

    /* Cleanup, not part of the property: shuts the worker down so the process
     * can exit. rc_rpc_stop() also calls rc_exec_cancel(), which is why the
     * marker samples above are taken before this line, not after it. */
    rc_rpc_stop();
    rc_rpc_join(5000);
    DeleteFileA(MARKER_FILE);
}

int main(int argc, char **argv)
{
    (void)argc; (void)argv;
    rc_log_open(".");

    lane_table_cases();
    immediate_survives_busy_job();

    rc_log_close();

    if (g_failures) {
        printf("FAIL: %d case(s) failed\n", g_failures);
        return 1;
    }
    printf("PASS: lane table and IMMEDIATE-survives-busy-JOB both hold\n");
    return 0;
}
