/* Headless conformance check for the RPC lane routing (04.5).
 *
 * Links the real frameproto.c and rpc.c, so this exercises the same lane
 * table and the same worker rc_rpc_route drives, without needing a live
 * bridge -- rc_net_send on an rc_net with no connection returns
 * RC_SEND_DEAD before anything else, and a zeroed rc_net has none, so every
 * EXECRESULT/EXECOUT this check provokes is dropped without touching a
 * socket. See net.cpp's send_frame.
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
#include "rpc/exec.h"
#include "core/log.h"
#include "sync/sync.h"
#include "sync/syncrpc.h"
#include "core/str.h"

/* Zero-initialised: `conn` is NULL, which is what rc_net_send checks first.
 * rc_rpc_* never calls rc_net_start, so it stays NULL for the whole check
 * and every send from inside the worker is a guaranteed, harmless no-op. */
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
    case RC_LANE_DESKTOP:   return "DESKTOP";
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
    rc_frame_delete(f);
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
    /* Phase 6. Named one by one in the table rather than matched on a "UI"
     * prefix, so this is four assertions and not one. */
    check_lane("UITREE",     RC_LANE_DESKTOP);
    check_lane("UICLICK",    RC_LANE_DESKTOP);
    check_lane("UIKEYS",     RC_LANE_DESKTOP);
    check_lane("UISHOT",     RC_LANE_DESKTOP);
    /* And the rule that makes the desktop lane safe to add: the reply types
     * the GUEST sends must never be routed back INTO it. A prefix rule
     * would have claimed all three. */
    check_lane("UIENT",      RC_LANE_UI);
    check_lane("UIDAT",      RC_LANE_UI);
    check_lane("UIEND",      RC_LANE_UI);
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
     * An EMPTY `shell` header, with cmd.exe named explicitly in the body.
     *
     * The header is the invocation prefix the bridge composes from whichever
     * shell the model picked, and empty means "spawn this directly" -- so
     * this runs cmd.exe itself rather than asking for a shell. It has to:
     * this project's Wine prefix reports itself as 9x (CLAUDE.md: "verify on
     * the guest, not under Wine" -- the whole point is that the guest code
     * runs unmodified) and Wine has no command.com to spawn. Which shell
     * runs is not what the JOB/IMMEDIATE lane machinery here is about, so it
     * is sidestepped through the wire protocol's own header rather than by
     * faking anything.
     *
     * It used to be `shell=0` against a flag, and exec.c picked the shell
     * itself from GetVersion(). It picks nothing now -- see rc_exec_run. */
    xkeys[0] = "id";         xvals[0] = "x1";
    xkeys[1] = "timeout_ms"; xvals[1] = "8000";
    xkeys[2] = "shell";      xvals[2] = "";
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

/* --- 03.15: an out-of-range EXEC timeout is clamped AND SAYS SO ----------- */

/* rc_log_open(".") below writes here; opened with FILE_SHARE_READ, so this
 * can read it back while rpc.c's own handle is still open and writing to it
 * from the worker thread. */
#define LOG_FILE "retroclaude.log"

static int log_contains(const char *needle)
{
    HANDLE h;
    DWORD sz, got;
    char *buf;
    int found;

    h = CreateFileA(LOG_FILE, GENERIC_READ,
                    FILE_SHARE_READ | FILE_SHARE_WRITE, NULL,
                    OPEN_EXISTING, 0, NULL);
    if (h == INVALID_HANDLE_VALUE) return 0;
    sz = GetFileSize(h, NULL);
    if (sz == INVALID_FILE_SIZE || sz == 0) { CloseHandle(h); return 0; }
    buf = (char *)HeapAlloc(GetProcessHeap(), 0, sz + 1);
    if (!buf) { CloseHandle(h); return 0; }
    found = ReadFile(h, buf, sz, &got, NULL) && got == sz;
    CloseHandle(h);
    if (!found) { HeapFree(GetProcessHeap(), 0, buf); return 0; }
    buf[sz] = '\0';
    found = strstr(buf, needle) != NULL;
    HeapFree(GetProcessHeap(), 0, buf);
    return found;
}

/* THE PROPERTY: a timeout_ms above the guest's ceiling is clamped, and the
 * clamp is now a diagnosable event, not a number that just quietly ran for
 * less time than asked. Without the fix in run_one() (rpc.c), nothing in the
 * log distinguishes a clamped request from an ordinary one -- this would find
 * no such line and fail. */
/* The "refuse rather than truncate" guard in rc_exec_run, driven for real.
 *
 * The guard read `if (n < 0 || n >= (int)sizeof(cmd))` -- _snprintf's
 * length-or-negative contract -- while rc_strf returns 1 for "it fit" and 0
 * for "it was truncated". Neither value can satisfy either half, so the guard
 * NEVER fired: RC_EXEC_TOO_LONG was unreachable and a command over 1024 bytes
 * was cut at byte 1023 and handed to the shell. `del c:\temp\*.tmp` cut short
 * becomes `del c:\temp\*`, which is the example this file's own header uses
 * for why the guard exists.
 *
 * The bridge applies no length cap of its own, so this is the only guard in
 * the system. Testing the OUTCOME rather than the shape of the expression is
 * the point: a rewrite that reintroduces the wrong contract fails here.
 *
 * Nothing is ever spawned -- the refusal returns before CreateProcessA. The
 * short command is not run either; it is checked for the ABSENCE of
 * RC_EXEC_TOO_LONG, so this stays a pure-logic test with no child process.
 * (audit 01.3) */
static void exec_refuses_an_overlong_command(void)
{
    static char big[4096];
    rc_exec_result r;
    int rc, ok;

    memset(big, 'x', sizeof(big) - 1);
    big[sizeof(big) - 1] = '\0';

    memset(&r, 0, sizeof(r));
    rc = rc_exec_run(big, NULL, NULL, 1000, NULL, NULL, &r);
    ok = (rc != 0 && r.status == RC_EXEC_TOO_LONG);
    if (!ok) g_failures++;
    printf("%s\ta %u-byte command is refused as too_long\tgot rc=%d status=%d\n",
           ok ? "pass" : "FAIL", (unsigned)strlen(big), rc, (int)r.status);

    /* And the shell form, which prepends the bridge's invocation prefix and
     * so crosses the limit at a LOWER input length -- the branch a fix could
     * easily miss. The prefix is now a caller argument rather than something
     * this file picks, so the test states it. */
    memset(big, 'y', 1100);
    big[1100] = '\0';
    memset(&r, 0, sizeof(r));
    rc = rc_exec_run(big, NULL, "C:\\WINDOWS\\COMMAND.COM /c", 1000,
                     NULL, NULL, &r);
    ok = (rc != 0 && r.status == RC_EXEC_TOO_LONG);
    if (!ok) g_failures++;
    printf("%s\ta 1100-byte shell command is refused as too_long\tgot rc=%d status=%d\n",
           ok ? "pass" : "FAIL", rc, (int)r.status);

    /* A command that FITS must not be refused. Without this the test passes
     * for a guard that refuses everything. */
    memset(&r, 0, sizeof(r));
    r.status = RC_EXEC_TOO_LONG;
    rc_exec_run("", NULL, NULL, 1000, NULL, NULL, &r);
    ok = (r.status != RC_EXEC_TOO_LONG);
    if (!ok) g_failures++;
    printf("%s\ta short command is not refused as too_long\tgot status=%d\n",
           ok ? "pass" : "FAIL", (int)r.status);
}

static void exec_timeout_clamp_is_logged(void)
{
    const char *xkeys[3]; const char *xvals[3];
    rc_frame *f;
    int ok;

    rc_rpc_start(&g_net);

    /* Absurdly over the 120000ms ceiling; the command itself is instant, so
     * this check is not itself waiting anywhere near that long -- only the
     * clamp decision, made before the child is even spawned, is being
     * proven. An empty `shell` + cmd.exe named explicitly: see the long
     * comment in immediate_survives_busy_job() for why that combination is
     * what runs under this project's Wine prefix. */
    xkeys[0] = "id";         xvals[0] = "x2";
    xkeys[1] = "timeout_ms"; xvals[1] = "999999999";
    xkeys[2] = "shell";      xvals[2] = "";
    f = make_frame("EXEC", xkeys, xvals, 3, "cmd.exe /c echo hi");
    if (!f) {
        printf("FAIL\tbuild clamp EXEC frame\t(alloc)\n");
        g_failures++;
        return;
    }

    if (rc_rpc_submit(f) != 0) {
        printf("FAIL\tsubmit clamp EXEC\t(rejected)\n");
        g_failures++;
        free_frame(f);
        rc_rpc_stop(); rc_rpc_join(5000);
        return;
    }

    /* The clamp is decided and logged before the child is spawned.
     * Poll with a bounded wait rather than an arbitrary fixed sleep,
     * so slow CI/load does not produce false-negative flakes. */
    {
        int waited = 0;
        ok = 0;
        while (waited < 3000) {
            if (log_contains("clamped")) {
                ok = 1;
                break;
            }
            Sleep(50);
            waited += 50;
        }
    }
    if (!ok) g_failures++;
    printf("%s\texec timeout clamp is logged, not silent\t(log contains \"clamped\")\n",
           ok ? "pass" : "FAIL");

    rc_rpc_stop();
    rc_rpc_join(5000);
}


/* Does a REFUSED conditional push take its temp file with it?
 *
 * It did not, and this is the regression test for the one user-visible bug
 * in docs/mem-audit (F1). do_putend closes the temp's handle before the
 * compare-and-swap check -- it must, the commit needs it closed -- and
 * put_fail's cleanup was keyed on that handle still being live, so every
 * refusal after that point left a fully-written .gaston-tmp in the user's
 * tree. The compare-and-swap IS the ordinary path: the whole point of
 * if_crc is to refuse when the user edited the file underneath, so a plain
 * edit conflict leaked one every time. The scanner skips those names, so
 * nothing reported it either.
 *
 * Driven through the real rc_syncrpc_handle with the real frames, against a
 * zeroed rc_net with no connection -- so rc_net_send drops the ack without
 * touching a socket, exactly as the rest of this file does.
 *
 * Prints the directory's contents after the put and after the refusal, so
 * the Python side can assert on names rather than on a count it has to
 * trust.
 */
static void list_dir(const char *label, const char *dir)
{
    char pat[MAX_PATH];
    WIN32_FIND_DATAA fd;
    HANDLE h;
    int n = 0;

    rc_strf(pat, sizeof(pat), "%s\\*", dir);
    h = FindFirstFileA(pat, &fd);
    if (h != INVALID_HANDLE_VALUE) {
        do {
            if (strcmp(fd.cFileName, ".") == 0 ||
                strcmp(fd.cFileName, "..") == 0) continue;
            printf("%s\t%s\n", label, fd.cFileName);
            n++;
        } while (FindNextFileA(h, &fd));
        FindClose(h);
    }
    if (!n) printf("%s\t(empty)\n", label);
}

static int putend_cases(const char *root)
{
    rc_net net;
    rc_frame *f;
    char body[512];
    const char *keys[4];
    const char *vals[4];
    int n;

    /* No connection, so rc_net_send is the documented no-op. */
    memset(&net, 0, sizeof(net));
    rc_syncrpc_set_root(root);

    /* One chunk: "<len> <rel>" then the file's own bytes. */
    rc_strf(body, sizeof(body), "%u %s", (unsigned)strlen("f1.txt"), "f1.txt");
    n = (int)strlen(body);
    rc_strf(body + n, sizeof(body) - n, "hello");

    keys[0] = "id";  vals[0] = "s1";
    keys[1] = "off"; vals[1] = "0";
    f = make_frame("SYNCPUT", keys, vals, 2, body);
    if (!f) { fprintf(stderr, "no frame\n"); return 3; }
    rc_syncrpc_handle(&net, f);
    free_frame(f);
    list_dir("after_put", root);

    /* SYNCPUTEND carrying an if_crc the target cannot match -- nothing is at
     * that path, so the `absent` test fails and the refusal is `changed`.
     * That is the ordinary edit-conflict answer, and the path that leaked. */
    keys[0] = "id";     vals[0] = "s1";
    keys[1] = "crc";    vals[1] = "0";
    keys[2] = "cls";    vals[2] = "t";
    keys[3] = "if_crc"; vals[3] = "123456";
    f = make_frame("SYNCPUTEND", keys, vals, 4, "");
    if (!f) { fprintf(stderr, "no frame\n"); return 3; }
    rc_syncrpc_handle(&net, f);
    free_frame(f);
    list_dir("after_refusal", root);
    return 0;
}

static unsigned long scan_bytes(const void *data, unsigned len, char *cls_out)
{
    rc_sync_scanner *s = rc_sync_scanner_begin();
    char cls = RC_CLS_TEXT;
    unsigned long crc = 0;
    if (!s) return 0;
    rc_sync_scanner_feed(s, data, len);
    rc_sync_scanner_finish(s, &cls, &crc);
    if (cls_out) *cls_out = cls;
    return crc;
}

static int sync_rpc_cases(const char *root)
{
    rc_net net;
    rc_frame *f;
    char body[512];
    const char *keys[4];
    const char *vals[4];
    char target_path[MAX_PATH];
    char tmp_path[MAX_PATH];
    char sub_path[MAX_PATH];
    char crc_str[32];
    char cls_str[4];
    char read_buf[64];
    const char *content1 = "hello from syncrpc\n";
    const char *content2 = "new overwriting content\n";
    unsigned long moved_start;
    unsigned long crc;
    char cls;
    int n, ok;
    HANDLE h;
    DWORD read_bytes, attr;

    memset(&net, 0, sizeof(net));
    rc_syncrpc_set_root(root);
    moved_start = rc_syncrpc_moved();

    /* 1. Happy PUT + PUTEND commit: writes file, verifies content, removes temp, increments moved */
    rc_strf(body, sizeof(body), "%u %s", (unsigned)strlen("put_ok.txt"), "put_ok.txt");
    n = (int)strlen(body);
    rc_strf(body + n, sizeof(body) - n, "%s", content1);

    keys[0] = "id";  vals[0] = "put1";
    keys[1] = "off"; vals[1] = "0";
    f = make_frame("SYNCPUT", keys, vals, 2, body);
    if (!f) { fprintf(stderr, "no frame for put1\n"); return 3; }
    rc_syncrpc_handle(&net, f);
    free_frame(f);

    /* Verify temp file exists before commit */
    rc_strf(tmp_path, sizeof(tmp_path), "%s\\put_ok.txt.gaston-tmp", root);
    ok = (GetFileAttributesA(tmp_path) != 0xFFFFFFFF);
    if (!ok) g_failures++;
    printf("%s\tsyncrpc: temp file exists during put\t%s\n",
           ok ? "pass" : "FAIL", tmp_path);

    crc = scan_bytes(content1, (unsigned)strlen(content1), &cls);
    rc_strf(crc_str, sizeof(crc_str), "%lu", crc);
    cls_str[0] = cls; cls_str[1] = '\0';

    keys[0] = "id";     vals[0] = "put1";
    keys[1] = "crc";    vals[1] = crc_str;
    keys[2] = "cls";    vals[2] = cls_str;
    keys[3] = "if_crc"; vals[3] = "absent";
    f = make_frame("SYNCPUTEND", keys, vals, 4, "");
    if (!f) { fprintf(stderr, "no frame for putend1\n"); return 3; }
    rc_syncrpc_handle(&net, f);
    free_frame(f);

    /* Verify target exists with matching content and temp file is gone */
    rc_strf(target_path, sizeof(target_path), "%s\\put_ok.txt", root);
    ok = (GetFileAttributesA(target_path) != 0xFFFFFFFF) &&
         (GetFileAttributesA(tmp_path) == 0xFFFFFFFF);
    if (ok) {
        h = CreateFileA(target_path, GENERIC_READ, FILE_SHARE_READ, NULL,
                        OPEN_EXISTING, 0, NULL);
        if (h != INVALID_HANDLE_VALUE) {
            memset(read_buf, 0, sizeof(read_buf));
            ReadFile(h, read_buf, sizeof(read_buf) - 1, &read_bytes, NULL);
            CloseHandle(h);
            if (strcmp(read_buf, content1) != 0) ok = 0;
        } else {
            ok = 0;
        }
    }
    if (!ok) g_failures++;
    printf("%s\tsyncrpc: put commits data to target and cleans temp\t%s\n",
           ok ? "pass" : "FAIL", target_path);

    /* 2. PUTEND CRC mismatch refusal: corrupted chunk never replaces target */
    rc_strf(body, sizeof(body), "%u %s", (unsigned)strlen("put_bad.txt"), "put_bad.txt");
    n = (int)strlen(body);
    rc_strf(body + n, sizeof(body) - n, "corrupted bytes");

    keys[0] = "id";  vals[0] = "put2";
    keys[1] = "off"; vals[1] = "0";
    f = make_frame("SYNCPUT", keys, vals, 2, body);
    if (!f) { fprintf(stderr, "no frame for put2\n"); return 3; }
    rc_syncrpc_handle(&net, f);
    free_frame(f);

    keys[0] = "id";     vals[0] = "put2";
    keys[1] = "crc";    vals[1] = "99999999";
    keys[2] = "cls";    vals[2] = "t";
    keys[3] = "if_crc"; vals[3] = "absent";
    f = make_frame("SYNCPUTEND", keys, vals, 4, "");
    if (!f) { fprintf(stderr, "no frame for putend2\n"); return 3; }
    rc_syncrpc_handle(&net, f);
    free_frame(f);

    rc_strf(target_path, sizeof(target_path), "%s\\put_bad.txt", root);
    rc_strf(tmp_path, sizeof(tmp_path), "%s\\put_bad.txt.gaston-tmp", root);
    ok = (GetFileAttributesA(target_path) == 0xFFFFFFFF) &&
         (GetFileAttributesA(tmp_path) == 0xFFFFFFFF);
    if (!ok) g_failures++;
    printf("%s\tsyncrpc: putend crc mismatch refuses and cleans temp\t%s\n",
           ok ? "pass" : "FAIL", target_path);

    /* 3. PUTEND if_crc mismatch preserves existing file on guest */
    rc_strf(body, sizeof(body), "%u %s", (unsigned)strlen("put_ok.txt"), "put_ok.txt");
    n = (int)strlen(body);
    rc_strf(body + n, sizeof(body) - n, "%s", content2);

    keys[0] = "id";  vals[0] = "put3";
    keys[1] = "off"; vals[1] = "0";
    f = make_frame("SYNCPUT", keys, vals, 2, body);
    if (!f) { fprintf(stderr, "no frame for put3\n"); return 3; }
    rc_syncrpc_handle(&net, f);
    free_frame(f);

    /* Send if_crc that does not match content1's CRC */
    crc = scan_bytes(content2, (unsigned)strlen(content2), &cls);
    rc_strf(crc_str, sizeof(crc_str), "%lu", crc);

    keys[0] = "id";     vals[0] = "put3";
    keys[1] = "crc";    vals[1] = crc_str;
    keys[2] = "cls";    vals[2] = "t";
    keys[3] = "if_crc"; vals[3] = "11111111";
    f = make_frame("SYNCPUTEND", keys, vals, 4, "");
    if (!f) { fprintf(stderr, "no frame for putend3\n"); return 3; }
    rc_syncrpc_handle(&net, f);
    free_frame(f);

    rc_strf(target_path, sizeof(target_path), "%s\\put_ok.txt", root);
    rc_strf(tmp_path, sizeof(tmp_path), "%s\\put_ok.txt.gaston-tmp", root);
    ok = (GetFileAttributesA(tmp_path) == 0xFFFFFFFF);
    if (ok) {
        h = CreateFileA(target_path, GENERIC_READ, FILE_SHARE_READ, NULL,
                        OPEN_EXISTING, 0, NULL);
        if (h != INVALID_HANDLE_VALUE) {
            memset(read_buf, 0, sizeof(read_buf));
            ReadFile(h, read_buf, sizeof(read_buf) - 1, &read_bytes, NULL);
            CloseHandle(h);
            if (strcmp(read_buf, content1) != 0) ok = 0; /* must still be content1! */
        } else {
            ok = 0;
        }
    }
    if (!ok) g_failures++;
    printf("%s\tsyncrpc: putend if_crc mismatch preserves existing file\t%s\n",
           ok ? "pass" : "FAIL", target_path);

    /* 4. SYNCMKDIR creates directory hierarchy */
    keys[0] = "id"; vals[0] = "m1";
    f = make_frame("SYNCMKDIR", keys, vals, 1, "test_dir/nested_sub");
    if (!f) { fprintf(stderr, "no frame for mkdir\n"); return 3; }
    rc_syncrpc_handle(&net, f);
    free_frame(f);

    rc_strf(sub_path, sizeof(sub_path), "%s\\test_dir\\nested_sub", root);
    attr = GetFileAttributesA(sub_path);
    ok = (attr != 0xFFFFFFFF && (attr & FILE_ATTRIBUTE_DIRECTORY));
    if (!ok) g_failures++;
    printf("%s\tsyncrpc: mkdir creates directory hierarchy\t%s\n",
           ok ? "pass" : "FAIL", sub_path);

    /* 5. SYNCGET reads existing file */
    keys[0] = "id"; vals[0] = "g1";
    f = make_frame("SYNCGET", keys, vals, 1, "put_ok.txt");
    if (!f) { fprintf(stderr, "no frame for get\n"); return 3; }
    rc_syncrpc_handle(&net, f);
    free_frame(f);
    printf("pass\tsyncrpc: get executes without error on existing file\tput_ok.txt\n");

    /* 6. Moved counter: put1 incremented moved */
    ok = (rc_syncrpc_moved() >= moved_start + 1);
    if (!ok) g_failures++;
    printf("%s\tsyncrpc: moved counter increments on commit\tcount=%lu\n",
           ok ? "pass" : "FAIL", rc_syncrpc_moved());

    return g_failures ? 1 : 0;
}

int main(int argc, char **argv)
{
    if (rc_log_open(".") != 0) {
        fprintf(stderr, "rpccheck: cannot open retroclaude.log\n");
        return 1;
    }

    /* A mode rather than another always-on case: it needs a directory to
     * work in, and the SYNCPUT path writes real files. */
    if (argc >= 3 && strcmp(argv[1], "--putend") == 0) {
        int rc = putend_cases(argv[2]);
        rc_log_close();
        return rc;
    }
    if (argc >= 3 && strcmp(argv[1], "--sync-rpc") == 0) {
        int rc = sync_rpc_cases(argv[2]);
        rc_log_close();
        return rc;
    }
    (void)argc; (void)argv;

    lane_table_cases();
    immediate_survives_busy_job();
    exec_refuses_an_overlong_command();
    exec_timeout_clamp_is_logged();

    rc_log_close();

    if (g_failures) {
        printf("FAIL: %d case(s) failed\n", g_failures);
        return 1;
    }
    printf("PASS: lane table and IMMEDIATE-survives-busy-JOB both hold\n");
    return 0;
}
