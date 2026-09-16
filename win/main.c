/* retroclaude.exe -- native Windows 98 client for Claude Code.
 *
 * One process, one TCP connection, two threads in Phase 1 (UI + reader).
 * The RPC worker joins in Phase 3.
 *
 * Build: see Makefile. Target is Win98 SE on a Pentium MMX 200 with 32MB.
 */
#include <windows.h>
#include <commctrl.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "frameproto.h"
#include "ini.h"
#include "log.h"
#include "net.h"
#include "transcript.h"

#define ID_TRANSCRIPT 1001
#define ID_INPUT      1002
#define ID_SEND       1003
#define ID_STOP       1004
#define ID_STATUS     1005

#define TIMER_RECONNECT 1
#define TIMER_STATUS    2

#define INPUT_HEIGHT  56
#define BUTTON_WIDTH  64
#define GUTTER        4

static rc_config     g_cfg;
static rc_net        g_net;
static rc_transcript g_tr;
static HWND  g_main, g_input, g_send, g_stop, g_status;
static HFONT g_ui_font;
static WNDPROC g_input_oldproc;
static int   g_turn_active = 0;
static int   g_backoff_ms = 1000;
static char  g_exe_dir[MAX_PATH];

/* ------------------------------------------------------------------ utils */

static void exe_dir(char *out, unsigned n)
{
    char path[MAX_PATH];
    char *slash;
    GetModuleFileNameA(NULL, path, sizeof(path));
    slash = strrchr(path, '\\');
    if (slash) *slash = '\0';
    _snprintf(out, n - 1, "%s", path);
    out[n - 1] = '\0';
}

static void status_set(int pane, const char *text)
{
    if (g_status) SendMessageA(g_status, SB_SETTEXTA, (WPARAM)pane, (LPARAM)text);
}

static void status_refresh(void)
{
    char buf[128];
    _snprintf(buf, sizeof(buf), " %s  %s:%d",
              rc_state_name(g_net.state), g_cfg.host, g_cfg.port);
    status_set(0, buf);
    _snprintf(buf, sizeof(buf), " rx %luk  tx %luk",
              g_net.rx_bytes / 1024, g_net.tx_bytes / 1024);
    status_set(1, buf);
}

static void ui_set_busy(int busy)
{
    g_turn_active = busy;
    EnableWindow(g_stop, busy ? TRUE : FALSE);
    EnableWindow(g_send, busy ? FALSE : TRUE);
}

/* ------------------------------------------------------------ frame render */

static rc_style style_from_name(const char *s)
{
    if (!s)                          return RC_STYLE_PLAIN;
    if (strcmp(s, "code") == 0)      return RC_STYLE_CODE;
    if (strcmp(s, "bold") == 0)      return RC_STYLE_BOLD;
    if (strcmp(s, "thinking") == 0)  return RC_STYLE_THINKING;
    return RC_STYLE_PLAIN;
}

static void render_frame(rc_frame *f)
{
    const char *v;

    if (strcmp(f->type, "READY") == 0) {
        v = rc_frame_get(f, "sess");
        if (v) _snprintf(g_net.sess, sizeof(g_net.sess) - 1, "%s", v);
        v = rc_frame_get(f, "next_seq");
        if (v) g_net.next_seq = (unsigned)atoi(v);
        g_backoff_ms = 1000;
        rc_log("ready: sess=%s model=%s", g_net.sess,
               rc_frame_get(f, "model") ? rc_frame_get(f, "model") : "?");
        rc_transcript_begin(&g_tr);
        rc_transcript_append(&g_tr, "Connected to bridge.\r\n", RC_STYLE_TOOL);
        rc_transcript_end(&g_tr);
        status_refresh();

    } else if (strcmp(f->type, "TEXT") == 0) {
        rc_style st = style_from_name(rc_frame_get(f, "style"));
        /* Body is not NUL-terminated by the protocol; the reader over-allocates
         * by one and terminates it, so this is safe. */
        rc_transcript_begin(&g_tr);
        rc_transcript_append(&g_tr, f->body, st);
        rc_transcript_end(&g_tr);

    } else if (strcmp(f->type, "TOOL") == 0) {
        char line[512];
        _snprintf(line, sizeof(line) - 1, "\r\n  -> %s  %.*s\r\n",
                  rc_frame_get(f, "name") ? rc_frame_get(f, "name") : "?",
                  (int)f->body_len, f->body);
        rc_transcript_begin(&g_tr);
        rc_transcript_append(&g_tr, line, RC_STYLE_TOOL);
        rc_transcript_end(&g_tr);

    } else if (strcmp(f->type, "TURN") == 0) {
        const char *state = rc_frame_get(f, "state");
        if (state && strcmp(state, "start") == 0) {
            ui_set_busy(1);
            status_set(2, " thinking...");
        } else {
            char buf[96];
            ui_set_busy(0);
            v = rc_frame_get(f, "cost_usd");
            _snprintf(buf, sizeof(buf), " done  $%s", (v && *v) ? v : "?");
            status_set(2, buf);
            rc_transcript_begin(&g_tr);
            rc_transcript_append(&g_tr, "\r\n", RC_STYLE_PLAIN);
            rc_transcript_end(&g_tr);
        }

    } else if (strcmp(f->type, "ERROR") == 0) {
        char line[512];
        _snprintf(line, sizeof(line) - 1, "\r\n[%s] %s\r\n",
                  rc_frame_get(f, "code") ? rc_frame_get(f, "code") : "error",
                  rc_frame_get(f, "msg") ? rc_frame_get(f, "msg") : "");
        rc_log("bridge error: %s", line);
        rc_transcript_begin(&g_tr);
        rc_transcript_append(&g_tr, line, RC_STYLE_ERROR);
        rc_transcript_end(&g_tr);
        ui_set_busy(0);

    } else if (strcmp(f->type, "NOTICE") == 0) {
        rc_transcript_begin(&g_tr);
        rc_transcript_append(&g_tr, "\r\n", RC_STYLE_PLAIN);
        rc_transcript_append(&g_tr, f->body, RC_STYLE_TOOL);
        rc_transcript_append(&g_tr, "\r\n", RC_STYLE_PLAIN);
        rc_transcript_end(&g_tr);

    } else {
        rc_log("unhandled frame type: %s", f->type);
    }
}

/* ------------------------------------------------------------------ input */

static void do_send(void)
{
    char buf[4096];
    int n;

    if (g_turn_active) return;
    n = GetWindowTextA(g_input, buf, sizeof(buf));
    if (n <= 0) return;

    rc_transcript_begin(&g_tr);
    rc_transcript_append(&g_tr, "\r\n> ", RC_STYLE_USER);
    rc_transcript_append(&g_tr, buf, RC_STYLE_USER);
    rc_transcript_append(&g_tr, "\r\n\r\n", RC_STYLE_USER);
    rc_transcript_end(&g_tr);

    if (rc_net_send(&g_net, "INPUT", NULL, NULL, 0, buf, (unsigned)n) == 0) {
        SetWindowTextA(g_input, "");
        ui_set_busy(1);
    } else {
        rc_transcript_begin(&g_tr);
        rc_transcript_append(&g_tr, "[not connected]\r\n", RC_STYLE_ERROR);
        rc_transcript_end(&g_tr);
    }
    SetFocus(g_input);
}

static void do_stop(void)
{
    rc_log("stop requested");
    rc_net_send(&g_net, "INTERRUPT", NULL, NULL, 0, NULL, 0);
}

/* Enter sends; Shift+Enter inserts a newline. */
static LRESULT CALLBACK input_proc(HWND h, UINT m, WPARAM w, LPARAM l)
{
    if (m == WM_KEYDOWN && w == VK_RETURN && !(GetKeyState(VK_SHIFT) & 0x8000)) {
        do_send();
        return 0;
    }
    if (m == WM_CHAR && w == VK_RETURN && !(GetKeyState(VK_SHIFT) & 0x8000))
        return 0;   /* swallow the beep */
    return CallWindowProc(g_input_oldproc, h, m, w, l);
}

/* ----------------------------------------------------------------- layout */

static void layout(HWND hwnd)
{
    RECT rc;
    int sb_h = 0, y, w, h;
    RECT srect;

    GetClientRect(hwnd, &rc);
    if (g_status) {
        SendMessageA(g_status, WM_SIZE, 0, 0);
        GetWindowRect(g_status, &srect);
        sb_h = srect.bottom - srect.top;
    }

    w = rc.right - rc.left;
    h = rc.bottom - rc.top - sb_h;
    y = h - INPUT_HEIGHT - GUTTER;

    MoveWindow(g_tr.hwnd, GUTTER, GUTTER, w - 2 * GUTTER, y - GUTTER, TRUE);
    MoveWindow(g_input, GUTTER, y,
               w - 3 * GUTTER - BUTTON_WIDTH, INPUT_HEIGHT, TRUE);
    MoveWindow(g_send, w - GUTTER - BUTTON_WIDTH, y, BUTTON_WIDTH, 24, TRUE);
    MoveWindow(g_stop, w - GUTTER - BUTTON_WIDTH, y + 28, BUTTON_WIDTH, 24, TRUE);
}

static void create_children(HWND hwnd, HINSTANCE inst)
{
    int parts[3];
    NONCLIENTMETRICSA ncm;

    memset(&ncm, 0, sizeof(ncm));
    ncm.cbSize = sizeof(ncm);
    if (SystemParametersInfoA(SPI_GETNONCLIENTMETRICS, sizeof(ncm), &ncm, 0))
        g_ui_font = CreateFontIndirectA(&ncm.lfMessageFont);
    else
        g_ui_font = (HFONT)GetStockObject(DEFAULT_GUI_FONT);

    if (rc_transcript_create(&g_tr, hwnd, ID_TRANSCRIPT, inst) != 0)
        MessageBoxA(hwnd, "Could not create the RichEdit control.\n"
                          "riched20.dll may be missing.",
                    "retroclaude", MB_ICONERROR);

    g_input = CreateWindowExA(WS_EX_CLIENTEDGE, "EDIT", "",
        WS_CHILD | WS_VISIBLE | WS_TABSTOP | WS_VSCROLL |
        ES_MULTILINE | ES_AUTOVSCROLL | ES_WANTRETURN,
        0, 0, 0, 0, hwnd, (HMENU)ID_INPUT, inst, NULL);

    g_send = CreateWindowExA(0, "BUTTON", "Send",
        WS_CHILD | WS_VISIBLE | WS_TABSTOP | BS_DEFPUSHBUTTON,
        0, 0, 0, 0, hwnd, (HMENU)ID_SEND, inst, NULL);

    g_stop = CreateWindowExA(0, "BUTTON", "Stop",
        WS_CHILD | WS_VISIBLE | WS_TABSTOP | BS_PUSHBUTTON,
        0, 0, 0, 0, hwnd, (HMENU)ID_STOP, inst, NULL);
    EnableWindow(g_stop, FALSE);

    g_status = CreateWindowExA(0, STATUSCLASSNAMEA, "",
        WS_CHILD | WS_VISIBLE | SBARS_SIZEGRIP,
        0, 0, 0, 0, hwnd, (HMENU)ID_STATUS, inst, NULL);
    parts[0] = 220; parts[1] = 340; parts[2] = -1;
    SendMessageA(g_status, SB_SETPARTS, 3, (LPARAM)parts);

    SendMessageA(g_input, WM_SETFONT, (WPARAM)g_ui_font, TRUE);
    SendMessageA(g_send,  WM_SETFONT, (WPARAM)g_ui_font, TRUE);
    SendMessageA(g_stop,  WM_SETFONT, (WPARAM)g_ui_font, TRUE);

    g_input_oldproc = (WNDPROC)SetWindowLongA(g_input, GWL_WNDPROC,
                                              (LONG)input_proc);
}

/* -------------------------------------------------------------- main proc */

static void try_connect(HWND hwnd)
{
    status_refresh();
    if (rc_net_start(&g_net, hwnd, g_cfg.host, g_cfg.port, g_cfg.secret) != 0) {
        char msg[160];
        _snprintf(msg, sizeof(msg),
                  "Cannot reach %s:%d - retrying in %ds\r\n",
                  g_cfg.host, g_cfg.port, g_backoff_ms / 1000);
        rc_transcript_begin(&g_tr);
        rc_transcript_append(&g_tr, msg, RC_STYLE_ERROR);
        rc_transcript_end(&g_tr);

        /* Backoff on a timer, never a sleeping loop -- the message queue has
         * to keep pumping or the window is a dead rectangle. */
        SetTimer(hwnd, TIMER_RECONNECT, (UINT)g_backoff_ms, NULL);
        g_backoff_ms *= 2;
        if (g_backoff_ms > 15000) g_backoff_ms = 15000;
    }
    status_refresh();
}

static LRESULT CALLBACK wnd_proc(HWND hwnd, UINT msg, WPARAM wp, LPARAM lp)
{
    switch (msg) {
    case WM_CREATE:
        create_children(hwnd, ((LPCREATESTRUCT)lp)->hInstance);
        SetTimer(hwnd, TIMER_STATUS, 1000, NULL);
        PostMessage(hwnd, WM_APP + 99, 0, 0);   /* connect after first paint */
        return 0;

    case WM_APP + 99:
        try_connect(hwnd);
        return 0;

    case WM_SIZE:
        layout(hwnd);
        return 0;

    case WM_SETFOCUS:
        SetFocus(g_input);
        return 0;

    case WM_TIMER:
        if (wp == TIMER_RECONNECT) {
            KillTimer(hwnd, TIMER_RECONNECT);
            try_connect(hwnd);
        } else if (wp == TIMER_STATUS) {
            status_refresh();
        }
        return 0;

    case WM_RC_FRAME: {
        rc_frame *f = (rc_frame *)lp;
        render_frame(f);
        /* The UI thread owns every posted frame and is the only thing that
         * frees one -- except when PostMessage itself failed, which the reader
         * handles. */
        rc_frame_free(f);
        HeapFree(GetProcessHeap(), 0, f);
        InterlockedDecrement(&g_net.inflight);
        return 0;
    }

    case WM_RC_STATE:
        status_refresh();
        if ((rc_state)wp == RC_FAILED || (rc_state)wp == RC_DISCONNECTED) {
            ui_set_busy(0);
            SetTimer(hwnd, TIMER_RECONNECT, (UINT)g_backoff_ms, NULL);
        }
        return 0;

    case WM_COMMAND:
        switch (LOWORD(wp)) {
        case ID_SEND: do_send(); return 0;
        case ID_STOP: do_stop(); return 0;
        }
        return 0;

    case WM_CLOSE:
        DestroyWindow(hwnd);
        return 0;

    case WM_DESTROY:
        KillTimer(hwnd, TIMER_STATUS);
        KillTimer(hwnd, TIMER_RECONNECT);
        rc_net_stop(&g_net);
        rc_transcript_destroy(&g_tr);
        if (g_ui_font) DeleteObject(g_ui_font);
        PostQuitMessage(0);
        return 0;
    }
    return DefWindowProcA(hwnd, msg, wp, lp);
}

int WINAPI WinMain(HINSTANCE inst, HINSTANCE prev, LPSTR cmd, int show)
{
    WNDCLASSA wc;
    MSG msg;
    INITCOMMONCONTROLSEX icc;

    (void)prev; (void)cmd;

    /* Log from the very first statement. On this platform "no log file was
     * created" is the primary diagnostic: an NT-only static import kills the
     * process at load, before WinMain, leaving nothing else behind. */
    exe_dir(g_exe_dir, sizeof(g_exe_dir));
    rc_log_open(g_exe_dir);
    rc_log("os version: %08lx", GetVersion());

    memset(&icc, 0, sizeof(icc));
    icc.dwSize = sizeof(icc);
    icc.dwICC = ICC_BAR_CLASSES;
    InitCommonControlsEx(&icc);

    rc_config_load(&g_cfg, g_exe_dir);

    memset(&wc, 0, sizeof(wc));
    wc.lpfnWndProc   = wnd_proc;
    wc.hInstance     = inst;
    wc.hIcon         = LoadIconA(NULL, IDI_APPLICATION);
    wc.hCursor       = LoadCursorA(NULL, IDC_ARROW);
    wc.hbrBackground = (HBRUSH)(COLOR_BTNFACE + 1);
    wc.lpszClassName = "RetroClaudeWnd";
    if (!RegisterClassA(&wc)) { rc_log("RegisterClass failed"); return 1; }

    g_main = CreateWindowExA(0, "RetroClaudeWnd", "Claude Code",
        WS_OVERLAPPEDWINDOW, CW_USEDEFAULT, CW_USEDEFAULT, 640, 480,
        NULL, NULL, inst, NULL);
    if (!g_main) { rc_log("CreateWindowEx failed"); return 1; }

    ShowWindow(g_main, show);
    UpdateWindow(g_main);

    while (GetMessageA(&msg, NULL, 0, 0) > 0) {
        if (!IsDialogMessageA(g_main, &msg)) {
            TranslateMessage(&msg);
            DispatchMessageA(&msg);
        }
    }

    rc_log_close();
    return (int)msg.wParam;
}
