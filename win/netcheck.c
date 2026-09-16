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
#include "frameproto.h"
#include "net.h"
#include "log.h"

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

        rc_frame_free(f);
        HeapFree(GetProcessHeap(), 0, f);
        InterlockedDecrement(&g_net.inflight);
        return 0;
    }
    if (m == WM_RC_STATE) {
        if ((rc_state)w == RC_FAILED) { g_fail = 1; g_done = 1; }
        return 0;
    }
    return DefWindowProcA(h, m, w, l);
}

int main(int argc, char **argv)
{
    WNDCLASSA wc;
    MSG msg;
    DWORD deadline;
    const char *host = (argc > 1) ? argv[1] : "127.0.0.1";
    int port = (argc > 2) ? atoi(argv[2]) : 7890;
    const char *secret = (argc > 3) ? argv[3] : "dev-secret";
    if (argc > 4) g_prompt = argv[4];

    rc_log_open(".");

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
    if (rc_net_start(&g_net, g_sink, host, port, secret) != 0) {
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

    rc_net_stop(&g_net);
    rc_log_close();

    printf("\n--- reply ---\n%s\n-------------\n", g_text);
    printf("rx=%lu tx=%lu inflight=%ld\n",
           g_net.rx_bytes, g_net.tx_bytes, g_net.inflight);

    if (g_fail)        { printf("FAIL: error frame or link failure\n"); return 1; }
    if (!g_done)       { printf("FAIL: timed out\n"); return 1; }
    if (!g_textlen)    { printf("FAIL: no text received\n"); return 1; }
    if (g_net.inflight != 0) { printf("FAIL: %ld frames leaked\n", g_net.inflight); return 1; }
    printf("PASS: guest framing + networking round trip\n");
    return 0;
}
