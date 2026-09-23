/* Compose and send. See app/input.h for the design. */
#include <string.h>

#include "app/input.h"
#include "app/pillbar.h"
#include "core/log.h"
#include "core/str.h"

/* The longest message that can be composed and sent in one go. The frame
 * protocol would carry far more, but this is exactly one page and do_send's
 * own comment on its HeapAlloc explains why it is not a stack buffer:
 * building it on the stack is the guard-page fault tools/check/stackframes.py
 * exists to catch, not just something to avoid on general principle.
 * Enforced at the control with EM_LIMITTEXT and checked again at send. */
#define RC_INPUT_MAX 4096

/* Send/Stop's width -- this module's own business, same as app/pagebar.cpp's
 * PGBTN_W and app/bar.cpp's PERMBTN_W. */
#define BUTTON_WIDTH 64

/* The single instance the static WNDPROC trampoline reaches through. A plain
 * pointer and nothing more, so it has no constructor -- a file-scope object
 * with one would run before WinMain and fail the static-ctor gate. */
rc_input *rc_input::s_instance;

rc_input::~rc_input()
{
    if (box && oldproc)
        SetWindowLongA(box, GWL_WNDPROC, (LONG)oldproc);
    oldproc = NULL;
    if (s_instance == this) s_instance = NULL;
}

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
void rc_input::echo_user_block(const char *text)
{
    /* Was twenty lines of escaping and heap juggling here, with a buffer that
     * silently cut the echo at 4095 bytes while the full text went to the
     * model. The view owns its markup now, and sizes from the input. */
    if (html) html->user(text);
}

void rc_input::do_send(void)
{
    char *buf;
    int n;

    if (blocked_fn && blocked_fn()) return;

    /* Refuse rather than truncate. GetWindowTextA copies what fits and says
     * nothing, so a pasted log longer than the buffer was silently cut and the
     * SHORTENED text is what went to the model -- the person at the keyboard
     * losing bytes on the one path where they are the author. EM_LIMITTEXT
     * below normally stops it arriving at all; this is the check that makes
     * that a guarantee rather than a hope. */
    if (GetWindowTextLengthA(box) >= RC_INPUT_MAX) {
        if (html)
            html->error(
                      "That message is too long to send (the limit is about "
                      "4000 characters). Nothing was sent -- shorten it, or "
                      "put the long part in a file under the mirrored folder.");
        return;
    }

    /* Heap, not the stack this used to be on inside main.cpp. RC_INPUT_MAX
     * is exactly one page (4096 bytes); Open Watcom emits no stack probe for
     * a frame that size, which is precisely the fault tools/check/stackframes.py
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
        if (html) html->error("Out of memory. Nothing was sent.");
        return;
    }

    n = GetWindowTextA(box, buf, RC_INPUT_MAX);
    if (n <= 0 && (!pillbar || !pillbar->active())) {
        HeapFree(GetProcessHeap(), 0, buf);
        return;
    }

    /* status_refresh() used to run between note_tx_done and the
     * SetWindowTextA that clears the box; it now runs just after, bundled
     * into `sent` with ui_set_busy(1) -- both are synchronous UI refreshes
     * with no dependency on one another or on the box's text, so the reorder
     * is not observable. */
    if (send_text(buf)) SetWindowTextA(box, "");
    HeapFree(GetProcessHeap(), 0, buf);
    SetFocus(box);
}

int rc_input::send_text(const char *text)
{
    unsigned n;

    if ((!text || !*text) && (!pillbar || !pillbar->active())) return 0;
    /* The same refusal the Send button gets, not a second opinion about it:
     * a menu entry that sends a prompt mid-turn would be the one path around
     * the check that stops two turns overlapping. */
    if (blocked_fn && blocked_fn()) return 0;

    n = text ? (unsigned)strlen(text) : 0;
    if (n >= RC_INPUT_MAX) {
        if (html) html->error("That message is too long to send.");
        return 0;
    }

    const char *keys[4];
    const char *vals[4];
    int nkv = 0;
    char *att_work = NULL;
    char *ws_buf = NULL;
    char *ext_ids = NULL;
    char *ext_names = NULL;
    char *echo_pills = NULL;

    if (pillbar && pillbar->active()) {
        att_work = (char *)HeapAlloc(GetProcessHeap(), 0, 1024 * 3 + 256);
        if (att_work) {
            ws_buf = att_work;
            ext_ids = ws_buf + 1024;
            ext_names = ext_ids + 256;
            echo_pills = ext_names + 1024;

            if (pillbar->get_workspace_attachments(ws_buf, 1024)) {
                keys[nkv] = "attach";
                vals[nkv] = ws_buf;
                nkv++;
            }
            if (pillbar->get_external_attachments(ext_ids, 256, ext_names, 1024)) {
                keys[nkv] = "attach_id";
                vals[nkv] = ext_ids;
                nkv++;
                keys[nkv] = "attach_name";
                vals[nkv] = ext_names;
                nkv++;
            }
            pillbar->get_echo_string(echo_pills, 1024);
        }
    }

    /* Echoed BEFORE the send attempt, matching bridge transcript. */
    if (echo_pills && echo_pills[0]) {
        size_t echo_len = strlen(echo_pills);
        char *echo_buf = (char *)HeapAlloc(GetProcessHeap(), 0, n + echo_len + 64);
        if (echo_buf) {
            if (n > 0) {
                rc_strf(echo_buf, n + echo_len + 64, "%s\r\n%s", echo_pills, text);
            } else {
                rc_strf(echo_buf, n + echo_len + 64, "%s", echo_pills);
            }
            echo_user_block(echo_buf);
            HeapFree(GetProcessHeap(), 0, echo_buf);
        } else {
            echo_user_block(text ? text : "");
        }
    } else {
        echo_user_block(text ? text : "");
    }

    if (xfer) xfer->note_tx_start((unsigned long)n);
    int sent = (rc_net_send(net, "INPUT", keys, vals, nkv, text ? text : "", n) == 0);
    if (att_work) {
        HeapFree(GetProcessHeap(), 0, att_work);
    }
    if (sent) {
        if (pillbar && pillbar->active()) {
            pillbar->on_send();
        }
        if (xfer) xfer->note_tx_done((unsigned long)n);
        if (sent_fn) sent_fn();
        return 1;
    }
    if (html) html->error("Not connected.");
    return 0;
}

int rc_input::compose(const char *text)
{
    if (!box || !text) return 0;
    if (GetWindowTextLengthA(box) > 0) {
        if (html)
            html->error("There is already a message in the box. Send it or "
                        "clear it first, then pick the command again.");
        SetFocus(box);
        return 0;
    }
    SetWindowTextA(box, text);
    /* Caret to the end, so typing continues the line rather than landing in
     * front of the command -- EM_SETSEL with both ends past the text. */
    SendMessageA(box, EM_SETSEL, (WPARAM)-1, (LPARAM)-1);
    SetFocus(box);
    return 1;
}

void rc_input::do_stop(void)
{
    rc_log("stop requested");
    /* A lost INTERRUPT is a Stop button that does nothing, on a turn that
     * keeps running and keeps costing. */
    if (send_fn)
        send_fn("Stop", "INTERRUPT", NULL, NULL, 0, NULL, 0);
}

/* Enter sends; Shift+Enter inserts a newline. */
LRESULT CALLBACK rc_input::box_proc(HWND h, UINT m, WPARAM w, LPARAM l)
{
    if (s_instance && m == WM_KEYDOWN && w == VK_RETURN
        && !(GetKeyState(VK_SHIFT) & 0x8000)) {
        s_instance->do_send();
        return 0;
    }
    if (m == WM_CHAR && w == VK_RETURN && !(GetKeyState(VK_SHIFT) & 0x8000))
        return 0;   /* swallow the beep */

    if (m == WM_DROPFILES) {
        SendMessageA(GetParent(h), WM_DROPFILES, w, l);
        return 0;
    }

    /* Ctrl+A selects all.
     *
     * Windows 98's EDIT control has no such accelerator -- that arrived with
     * the rich edit control and with NT's common controls, and on 9x
     * Ctrl+A simply beeps. Every other application the user has touched in
     * the last twenty years does this, so it is muscle memory arriving at a
     * control that predates it.
     *
     * EM_SETSEL(0, -1) rather than -1/-1, which is the caret-to-end form used
     * in compose(). Both WM_KEYDOWN and WM_CHAR are handled: the first does
     * the work, the second swallows the beep the control would otherwise make
     * for a character it does not want. */
    if (m == WM_KEYDOWN && w == 'A' && (GetKeyState(VK_CONTROL) & 0x8000)) {
        SendMessageA(h, EM_SETSEL, 0, (LPARAM)-1);
        return 0;
    }
    if (m == WM_CHAR && w == 1)         /* Ctrl+A arrives as SOH */
        return 0;
    if (!s_instance || !s_instance->oldproc)
        return DefWindowProcA(h, m, w, l);
    return CallWindowProc(s_instance->oldproc, h, m, w, l);
}

rc_input *rc_input::create(HWND parent, HINSTANCE inst, HFONT font,
                           rc_net *net_, rc_html *html_, rc_xferstat *xfer_,
                           rc_pillbar *pillbar_,
                           rc_input_send_fn send, int (*blocked)(void),
                           void (*sent)(void))
{
    rc_input *p = new rc_input();
    if (!p) return 0;

    p->net = net_;
    p->html = html_;
    p->xfer = xfer_;
    p->pillbar = pillbar_;
    p->send_fn = send;
    p->blocked_fn = blocked;
    p->sent_fn = sent;
    /* Published BEFORE the subclass below, because SetWindowLongA can deliver
     * a message to box_proc the moment it returns and the trampoline has
     * nothing else to reach through. */
    s_instance = p;

    p->box = CreateWindowExA(WS_EX_CLIENTEDGE, "EDIT", "",
        WS_CHILD | WS_VISIBLE | WS_TABSTOP | WS_VSCROLL |
        ES_MULTILINE | ES_AUTOVSCROLL | ES_WANTRETURN,
        0, 0, 0, 0, parent, (HMENU)ID_INPUT, inst, NULL);
    check_child("input", p->box);
    /* Stop an over-long message being composed at all, which is the only
     * feedback that arrives at the moment the user does it rather than at
     * send time. The default for a multiline EDIT is 32KB, well past what
     * do_send can carry. */
    SendMessageA(p->box, EM_LIMITTEXT, (WPARAM)(RC_INPUT_MAX - 1), 0);

    p->send_btn = CreateWindowExA(0, "BUTTON", "&Send",
        WS_CHILD | WS_VISIBLE | WS_TABSTOP | BS_DEFPUSHBUTTON,
        0, 0, 0, 0, parent, (HMENU)ID_SEND, inst, NULL);
    check_child("send", p->send_btn);

    p->stop_btn = CreateWindowExA(0, "BUTTON", "S&top",
        WS_CHILD | WS_VISIBLE | WS_TABSTOP | BS_PUSHBUTTON,
        0, 0, 0, 0, parent, (HMENU)ID_STOP, inst, NULL);
    check_child("stop", p->stop_btn);
    EnableWindow(p->stop_btn, FALSE);

    SendMessageA(p->box,      WM_SETFONT, (WPARAM)font, TRUE);
    SendMessageA(p->send_btn, WM_SETFONT, (WPARAM)font, TRUE);
    SendMessageA(p->stop_btn, WM_SETFONT, (WPARAM)font, TRUE);

    p->oldproc = (WNDPROC)SetWindowLongA(p->box, GWL_WNDPROC,
                                         (LONG)box_proc);
    return p;
}

void rc_input::move(int cx, int y, int cw, int win_w)
{
    /* The "4" here is main.c's GUTTER, which this module cannot see by name
     * (it is a plain #define in main.cpp, not exported by any header) --
     * same value, spelled out, exactly as bar.cpp's move and pagebar.cpp's
     * already do for the same constant. */
    MoveWindow(box, cx, y, cw - 4 - BUTTON_WIDTH, INPUT_HEIGHT, TRUE);
    MoveWindow(send_btn, win_w - 4 - BUTTON_WIDTH, y, BUTTON_WIDTH, 24, TRUE);
    MoveWindow(stop_btn, win_w - 4 - BUTTON_WIDTH, y + 28, BUTTON_WIDTH, 24, TRUE);
}

int rc_input::command(int id)
{
    switch (id) {
    case ID_SEND: do_send(); return 1;
    case ID_STOP: do_stop(); return 1;
    }
    return 0;
}

void rc_input::enable_send(int enable)
{
    EnableWindow(send_btn, enable ? TRUE : FALSE);
}

void rc_input::enable_stop(int enable)
{
    EnableWindow(stop_btn, enable ? TRUE : FALSE);
}

void rc_input::focus(void)
{
    SetFocus(box);
}

int rc_input::is_empty(void) const
{
    /* WM_GETTEXTLENGTH rather than reading the text: the answer is a count,
     * the box holds up to RC_INPUT_MAX bytes, and this is called from the
     * message loop on every PgUp. */
    return SendMessageA(box, WM_GETTEXTLENGTH, 0, 0) == 0;
}

int rc_input::has_focus(void) const
{
    return GetFocus() == box;
}
