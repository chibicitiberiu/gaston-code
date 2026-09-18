/* Compose and send. See app/input.h for the design. */
#include "app/input.h"
#include "core/log.h"

/* The longest message that can be composed and sent in one go. The frame
 * protocol would carry far more, but this is exactly one page and do_send's
 * own comment on its HeapAlloc explains why it is not a stack buffer:
 * building it on the stack is the guard-page fault tools/stackframes.py
 * exists to catch, not just something to avoid on general principle.
 * Enforced at the control with EM_LIMITTEXT and checked again at send. */
#define RC_INPUT_MAX 4096

/* Send/Stop's width -- this module's own business, same as app/pagebar.cpp's
 * PGBTN_W and app/bar.cpp's PERMBTN_W. */
#define BUTTON_WIDTH 64

static HWND      g_input, g_send, g_stop;
static WNDPROC   g_input_oldproc;
static rc_net   *g_net;
static rc_html  *g_html;
static rc_xferstat *g_xfer;
static rc_input_send_fn g_send_fn;
static int      (*g_blocked)(void);
static void     (*g_sent)(void);

/* CreateWindowExA can fail, and on this platform that was never a
 * theoretical concern: the 16-bit USER/GDI heaps on 9x are a fixed 64KB that
 * does not scale with RAM -- see main.cpp's own check_child comment, the
 * sixth citation of this across main.cpp/chatlist.cpp/bar.cpp/pagebar.cpp/
 * input.cpp. Each module that creates controls keeps its own copy of this
 * rather than sharing one across files that do not otherwise depend on each
 * other. */
static void check_child(const char *what, HWND h)
{
    if (!h)
        rc_log("input: %s failed to create (GetLastError=%lu)",
               what, (unsigned long)GetLastError());
}

/* Echo the user's own line, meant to match bridge/view/html.py's user_turn()
 * (not bridge/markdown.py -- that module is a .bak and user_block() does not
 * exist): the bridge records that same string into the paginator so that
 * paging back to a sealed page still shows the question above the answer.
 * Echoing locally rather than waiting for the bridge to send it back is what
 * keeps typing responsive on a dialup link.
 *
 * They do NOT in fact match: user_turn() does text.strip().splitlines()
 * before escaping (html.py:102), and this side's esc_into() (htmlview.cpp)
 * escapes the raw text without stripping -- so a message with a trailing
 * blank line renders one line taller here than it does in the paginator-
 * sealed copy shown after paging back. Recorded as audit finding 01.29;
 * left as found rather than fixed here. */
static void echo_user_block(const char *text)
{
    /* Was twenty lines of escaping and heap juggling here, with a buffer that
     * silently cut the echo at 4095 bytes while the full text went to the
     * model. The view owns its markup now, and sizes from the input. */
    if (g_html) g_html->user(text);
}

static void do_send(void)
{
    char *buf;
    int n;

    if (g_blocked && g_blocked()) return;

    /* Refuse rather than truncate. GetWindowTextA copies what fits and says
     * nothing, so a pasted log longer than the buffer was silently cut and the
     * SHORTENED text is what went to the model -- the person at the keyboard
     * losing bytes on the one path where they are the author. EM_LIMITTEXT
     * below normally stops it arriving at all; this is the check that makes
     * that a guarantee rather than a hope. */
    if (GetWindowTextLengthA(g_input) >= RC_INPUT_MAX) {
        if (g_html)
            g_html->error(
                      "That message is too long to send (the limit is about "
                      "4000 characters). Nothing was sent -- shorten it, or "
                      "put the long part in a file under the mirrored folder.");
        return;
    }

    /* Heap, not the stack this used to be on inside main.cpp. RC_INPUT_MAX
     * is exactly one page (4096 bytes); Open Watcom emits no stack probe for
     * a frame that size, which is precisely the fault tools/stackframes.py
     * (make check's "stack frames" gate) exists to catch -- its own
     * docstring is a real crash on the real machine from this exact pattern.
     * Moving this function into its own translation unit (audit finding
     * 01.59) changed the link order enough that the frame stopped landing
     * next to a symbol the gate happened to treat as exempt and the gate
     * caught it -- the frame was always this size, the move only exposed
     * it. do_get/do_scan (sync/syncrpc.cpp) put their own multi-KB buffers
     * on the heap for the identical reason; same fix here. */
    buf = (char *)HeapAlloc(GetProcessHeap(), 0, RC_INPUT_MAX);
    if (!buf) {
        if (g_html) g_html->error("Out of memory. Nothing was sent.");
        return;
    }

    n = GetWindowTextA(g_input, buf, RC_INPUT_MAX);
    if (n <= 0) { HeapFree(GetProcessHeap(), 0, buf); return; }

    echo_user_block(buf);

    if (g_xfer) g_xfer->note_tx_start((unsigned long)n);
    if (rc_net_send(g_net, "INPUT", NULL, NULL, 0, buf, (unsigned)n) == 0) {
        if (g_xfer) g_xfer->note_tx_done((unsigned long)n);
        SetWindowTextA(g_input, "");
        /* status_refresh() used to run between the tx_done above and the
         * SetWindowTextA that clears the box; it now runs just after,
         * bundled into `sent` with ui_set_busy(1) -- both are synchronous
         * UI refreshes with no dependency on one another or on the box's
         * text, so the reorder is not observable. */
        if (g_sent) g_sent();
    } else {
        if (g_html) g_html->error("Not connected.");
    }
    HeapFree(GetProcessHeap(), 0, buf);
    SetFocus(g_input);
}

static void do_stop(void)
{
    rc_log("stop requested");
    /* A lost INTERRUPT is a Stop button that does nothing, on a turn that
     * keeps running and keeps costing. */
    if (g_send_fn)
        g_send_fn("Stop", "INTERRUPT", NULL, NULL, 0, NULL, 0);
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

void rc_input_create(HWND parent, HINSTANCE inst, HFONT font, rc_net *net,
                      rc_html *html, rc_xferstat *xfer, rc_input_send_fn send,
                      int (*blocked)(void), void (*sent)(void))
{
    g_net = net;
    g_html = html;
    g_xfer = xfer;
    g_send_fn = send;
    g_blocked = blocked;
    g_sent = sent;

    g_input = CreateWindowExA(WS_EX_CLIENTEDGE, "EDIT", "",
        WS_CHILD | WS_VISIBLE | WS_TABSTOP | WS_VSCROLL |
        ES_MULTILINE | ES_AUTOVSCROLL | ES_WANTRETURN,
        0, 0, 0, 0, parent, (HMENU)ID_INPUT, inst, NULL);
    check_child("input", g_input);
    /* Stop an over-long message being composed at all, which is the only
     * feedback that arrives at the moment the user does it rather than at
     * send time. The default for a multiline EDIT is 32KB, well past what
     * do_send can carry. */
    SendMessageA(g_input, EM_LIMITTEXT, (WPARAM)(RC_INPUT_MAX - 1), 0);

    g_send = CreateWindowExA(0, "BUTTON", "Send",
        WS_CHILD | WS_VISIBLE | WS_TABSTOP | BS_DEFPUSHBUTTON,
        0, 0, 0, 0, parent, (HMENU)ID_SEND, inst, NULL);
    check_child("send", g_send);

    g_stop = CreateWindowExA(0, "BUTTON", "Stop",
        WS_CHILD | WS_VISIBLE | WS_TABSTOP | BS_PUSHBUTTON,
        0, 0, 0, 0, parent, (HMENU)ID_STOP, inst, NULL);
    check_child("stop", g_stop);
    EnableWindow(g_stop, FALSE);

    SendMessageA(g_input, WM_SETFONT, (WPARAM)font, TRUE);
    SendMessageA(g_send,  WM_SETFONT, (WPARAM)font, TRUE);
    SendMessageA(g_stop,  WM_SETFONT, (WPARAM)font, TRUE);

    g_input_oldproc = (WNDPROC)SetWindowLongA(g_input, GWL_WNDPROC,
                                              (LONG)input_proc);
}

void rc_input_move(int cx, int y, int cw, int win_w)
{
    /* The "4" here is main.c's GUTTER, which this module cannot see by name
     * (it is a plain #define in main.cpp, not exported by any header) --
     * same value, spelled out, exactly as bar.cpp's rc_bar_move and
     * pagebar.cpp's rc_pagebar_move already do for the same constant. */
    MoveWindow(g_input, cx, y, cw - 4 - BUTTON_WIDTH, INPUT_HEIGHT, TRUE);
    MoveWindow(g_send, win_w - 4 - BUTTON_WIDTH, y, BUTTON_WIDTH, 24, TRUE);
    MoveWindow(g_stop, win_w - 4 - BUTTON_WIDTH, y + 28, BUTTON_WIDTH, 24, TRUE);
}

int rc_input_command(int id)
{
    switch (id) {
    case ID_SEND: do_send(); return 1;
    case ID_STOP: do_stop(); return 1;
    }
    return 0;
}

void rc_input_enable_send(int enable)
{
    EnableWindow(g_send, enable ? TRUE : FALSE);
}

void rc_input_enable_stop(int enable)
{
    EnableWindow(g_stop, enable ? TRUE : FALSE);
}

void rc_input_focus(void)
{
    SetFocus(g_input);
}
