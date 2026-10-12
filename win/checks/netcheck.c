/* Headless conformance check for the guest's networking and framing.
 *
 * Links the real net.c and frameproto.c, so this exercises the same code the
 * GUI uses -- connect, HELLO, send INPUT, parse the reply frames -- without
 * needing GUI automation to drive it. Run it under Wine in CI, and on the
 * guest itself when a real Win98 box is available.
 *
 *   netcheck <host> <port> <secret> "<prompt>"
 */
#include <windows.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "net/frameproto.h"
#include "net/net.h"
#include "core/log.h"

static rc_net  g_net;
static HWND    g_sink;
static int     g_ready = 0, g_done = 0, g_fail = 0;
static char    g_text[8192];
static unsigned g_textlen = 0;
static const char *g_prompt = "Reply with exactly: PONG-42";

static LRESULT CALLBACK sink_proc(HWND h, UINT m, WPARAM w, LPARAM l)
{
    if (m == WM_RC_FRAME) {
        rc_frame *f = (rc_frame *)l;
        printf("  <- %-12s", f->type);
        {   int i;
            for (i = 0; i < f->nheaders && i < 3; i++)
                printf(" %s=%s", f->headers[i].key, f->headers[i].val);
        }
        printf("\n");
        fflush(stdout);

        if (strcmp(f->type, "READY") == 0 && !g_ready) {
            g_ready = 1;
            if (rc_net_send(&g_net, "INPUT", NULL, NULL, 0,
                            g_prompt, (unsigned)strlen(g_prompt)) != 0) {
                printf("  !! INPUT send failed\n");
                g_fail = 1; g_done = 1;
            }
        } else if (strcmp(f->type, "TEXT") == 0) {
            unsigned n = f->body_len;
            if (g_textlen + n < sizeof(g_text) - 1) {
                memcpy(g_text + g_textlen, f->body, n);
                g_textlen += n;
                g_text[g_textlen] = '\0';
            }
        } else if (strcmp(f->type, "TURN") == 0) {
            const char *st = rc_frame_get(f, "state");
            if (st && strcmp(st, "start") != 0) g_done = 1;
        } else if (strcmp(f->type, "ERROR") == 0) {
            g_fail = 1; g_done = 1;
        }

        /* One call, not three hand-ordered steps -- this binary exists to
         * check the ownership rule, so it should not re-implement it. */
        rc_net_frame_done(&g_net, f);
        return 0;
    }
    if (m == WM_RC_STATE) {
        if ((rc_state)w == RC_FAILED) { g_fail = 1; g_done = 1; }
        return 0;
    }
    return DefWindowProcA(h, m, w, l);
}

/* ------------------------------------------------------- slowsend scenario
 *
 * docs/BACKLOG.md #1 names two weaknesses in the outbound queue that only a
 * stalled or throttled link exercises, and LAN-speed Wine runs never have:
 * the byte cap is documented as SOFT (a writer that waits its limit enqueues
 * anyway), and rc_net_send can block up to 5s (50 x 100ms) doing it. Neither
 * had ever run against anything that stops reading.
 *
 * This scenario is that stalled reader's counterpart on the sending side: it
 * skips the normal READY/INPUT/TEXT round trip entirely and instead hammers
 * rc_net_send directly, in a tight loop, against a peer the test harness has
 * told to accept the connection and never read from it again.
 *
 * Measurement, not assumption: on loopback under Wine, the kernel itself
 * absorbs on the order of 1.5-2MB before the guest's own send() call ever
 * blocks -- socket buffer autotuning, not this app's 64KB queue cap, is what
 * looks like slack at first. So this loop does not stop after a fixed frame
 * count; a fixed count either wastes minutes proving nothing (too high) or
 * never reaches real backpressure at all (too low, and the OLD version of
 * this file did exactly that against a naive stalled-reader harness -- see
 * the module docstring in test_guest_net.py for the numbers). It stops once
 * it has SEEN backpressure happen SLOW_STREAK times, which is enough to show
 * a trend rather than one data point, with a large frame-count ceiling as
 * the only thing standing between a misbehaving build and a hung check.
 *
 *   netcheck --slowsend <host> <port> <secret> [maxframes] [framebytes]
 */
#define SLOWSEND_MAX_FRAMES  400   /* safety ceiling, not the expected count */
#define SLOWSEND_SLOW_MS     1000  /* a call this long means backpressure hit */
#define SLOWSEND_SLOW_STREAK 3     /* this many slow calls proves the trend */

static int slowsend_main(int argc, char **argv)
{
    static rc_net n;   /* not on the stack: this scenario can block minutes */
    WNDCLASSA wc;
    HWND sink;
    const char *host   = (argc > 2) ? argv[2] : "127.0.0.1";
    int         port   = (argc > 3) ? atoi(argv[3]) : 7890;
    const char *secret = (argc > 4) ? argv[4] : "dev-secret";
    int maxframes  = (argc > 5) ? atoi(argv[5]) : SLOWSEND_MAX_FRAMES;
    int framebytes = (argc > 6) ? atoi(argv[6]) : 32768;
    char *body;
    int i, ok = 0, failed = 0, slow_seen = 0;
    unsigned long max_ms = 0;
    unsigned max_outq = 0;
    DWORD stop_t0, stop_ms;

    if (rc_log_open(".", "netcheck") != 0) {
        printf("FAIL: cannot open log\n");
        return 2;
    }

    memset(&wc, 0, sizeof(wc));
    /* This scenario never dispatches a frame -- it only cares what
     * rc_net_send itself does -- so the sink exists purely so the reader
     * thread has somewhere harmless to PostMessage to. */
    wc.lpfnWndProc = DefWindowProcA;
    wc.hInstance = GetModuleHandleA(NULL);
    wc.lpszClassName = "NetCheckSlowSink";
    RegisterClassA(&wc);
    sink = CreateWindowExA(0, "NetCheckSlowSink", "", 0, 0, 0, 0, 0,
                           NULL, NULL, wc.hInstance, NULL);
    if (!sink) { printf("FAIL: sink window\n"); return 2; }

    printf("connecting to %s:%d\n", host, port);
    if (rc_net_start(&n, sink, host, port, secret, NULL, 0, 1) != 0) {
        printf("FAIL: cannot connect\n");
        return 2;
    }

    body = (char *)malloc((size_t)framebytes);
    if (!body) { printf("FAIL: out of memory building test body\n"); return 2; }
    memset(body, 'A', (size_t)framebytes);

    printf("slowsend: up to %d frames of %d bytes into a stalled reader, "
           "stopping after %d slow calls (RC_OUTQ_MAX_BYTES=%u)\n",
           maxframes, framebytes, SLOWSEND_SLOW_STREAK,
           (unsigned)RC_OUTQ_MAX_BYTES);

    for (i = 0; i < maxframes && slow_seen < SLOWSEND_SLOW_STREAK; i++) {
        MSG msg;
        int rc;
        unsigned outq;
        DWORD t0, t1, dt;

        while (PeekMessageA(&msg, NULL, 0, 0, PM_REMOVE)) {
            TranslateMessage(&msg);
            DispatchMessageA(&msg);
        }

        t0 = GetTickCount();
        rc = rc_net_send(&n, "INPUT", NULL, NULL, 0, body,
                         (unsigned)framebytes);
        t1 = GetTickCount();
        dt = t1 - t0;
        outq = rc_net_outq_bytes(&n);
        if ((unsigned long)dt > max_ms) max_ms = (unsigned long)dt;
        if (outq > max_outq) max_outq = outq;
        if (dt >= SLOWSEND_SLOW_MS) slow_seen++;

        /* One line per call would be thousands during the fast phase on a
         * generous kernel buffer -- only the calls that matter are logged. */
        if (dt >= SLOWSEND_SLOW_MS || rc != 0) {
            printf("  frame %3d: rc=%d wait_ms=%lu outq_bytes=%u\n",
                   i, rc, (unsigned long)dt, outq);
            fflush(stdout);
        }

        if (rc == 0) ok++; else failed++;
    }

    stop_t0 = GetTickCount();
    rc_net_stop(&n);
    stop_ms = GetTickCount() - stop_t0;

    free(body);
    rc_log_close();

    printf("slowsend: sent=%d ok=%d failed=%d slow_seen=%d max_wait_ms=%lu "
           "max_outq_bytes=%u stop_ms=%lu\n", i, ok, failed, slow_seen,
           max_ms, max_outq, stop_ms);

    /* The three invariants this scenario exists to guarantee -- see
     * docs/BACKLOG.md #1 and tests/python/test_guest_net.py. */
    if (slow_seen == 0) {
        printf("FAIL: nothing ever slowed down -- the stalled reader never "
               "produced backpressure, so this scenario proved nothing\n");
        return 1;
    }
    if (failed == 0) {
        printf("FAIL: backpressure happened %d times but every send still "
               "succeeded -- the queue grew instead of failing\n", slow_seen);
        return 1;
    }
    if (max_outq > (unsigned)RC_OUTQ_MAX_BYTES) {
        printf("FAIL: queue grew past its cap (%u > %u)\n",
               max_outq, (unsigned)RC_OUTQ_MAX_BYTES);
        return 1;
    }
    if (max_ms > 5500) {
        printf("FAIL: a send blocked %lums, past the documented 5s budget\n",
               max_ms);
        return 1;
    }
    printf("PASS: slow-reader scenario -- queue stayed bounded at or under "
           "the cap, a send that could not be queued failed instead of "
           "blocking indefinitely or dropping silently, and rc_net_stop "
           "returned in %lums\n", stop_ms);
    return 0;
}

int main(int argc, char **argv)
{
    WNDCLASSA wc;
    MSG msg;
    DWORD deadline;
    const char *host;
    int port;
    const char *secret;

    if (argc > 1 && strcmp(argv[1], "--slowsend") == 0)
        return slowsend_main(argc, argv);

    host = (argc > 1) ? argv[1] : "127.0.0.1";
    port = (argc > 2) ? atoi(argv[2]) : 7890;
    secret = (argc > 3) ? argv[3] : "dev-secret";
    if (argc > 4) g_prompt = argv[4];

    if (rc_log_open(".", "netcheck") != 0) {
        printf("FAIL: cannot open log\n");
        return 2;
    }

    memset(&wc, 0, sizeof(wc));
    wc.lpfnWndProc = sink_proc;
    wc.hInstance = GetModuleHandleA(NULL);
    wc.lpszClassName = "NetCheckSink";
    RegisterClassA(&wc);
    /* A message-only sink still needs a real message loop -- the reader thread
     * posts here, exactly as it does to the GUI. */
    g_sink = CreateWindowExA(0, "NetCheckSink", "", 0, 0, 0, 0, 0,
                             NULL, NULL, wc.hInstance, NULL);
    if (!g_sink) { printf("FAIL: sink window\n"); return 2; }

    printf("connecting to %s:%d\n", host, port);
    if (rc_net_start(&g_net, g_sink, host, port, secret, NULL, 0, 1) != 0) {
        printf("FAIL: cannot connect\n");
        return 2;
    }

    deadline = GetTickCount() + 120000;
    while (!g_done && GetTickCount() < deadline) {
        while (PeekMessageA(&msg, NULL, 0, 0, PM_REMOVE)) {
            TranslateMessage(&msg);
            DispatchMessageA(&msg);
        }
        Sleep(10);
    }

    /* STOP FIRST, THEN DRAIN, AND THE ORDER IS THE WHOLE POINT.
     *
     * g_done is set from inside the dispatch loop above, so the instant it
     * goes true this thread stops pumping -- while the reader thread may
     * still post frames that arrived in the same breath as the TURN end.
     * Those are counted in `inflight` (correctly: they ARE in flight) and
     * were never dispatched, so the check below read one every single run
     * and called it a leak. It was not a leak; it was this harness asserting
     * a quiescence it never waited for.
     *
     * rc_net_stop joins the reader, so once it returns nothing further can
     * be posted. Draining after that is what makes `inflight == 0` mean what
     * the assertion says it means: every frame the UI thread was given, it
     * freed. */
    rc_net_stop(&g_net);
    while (PeekMessageA(&msg, NULL, 0, 0, PM_REMOVE)) {
        TranslateMessage(&msg);
        DispatchMessageA(&msg);
    }
    rc_log_close();

    printf("\n--- reply ---\n%s\n-------------\n", g_text);
    printf("rx=%lu tx=%lu inflight=%ld\n",
           rc_net_rx_bytes(&g_net), rc_net_tx_bytes(&g_net), rc_net_inflight(&g_net));

    if (g_fail)        { printf("FAIL: error frame or link failure\n"); return 1; }
    if (!g_done)       { printf("FAIL: timed out\n"); return 1; }
    if (!g_textlen)    { printf("FAIL: no text received\n"); return 1; }
    if (rc_net_inflight(&g_net) != 0) {
        printf("FAIL: %ld frames leaked\n", rc_net_inflight(&g_net));
        return 1;
    }
    printf("PASS: guest framing + networking round trip\n");
    return 0;
}
