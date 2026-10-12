/* Compose and send. See app/input.h for the design. */
#include <string.h>

#include "app/input.h"

#include <string>
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

ComposeBox::ComposeBox()
    : m_edit(this), m_box(0), m_sendButton(0), m_stopButton(0), m_net(0),
      m_html(0), m_xfer(0), m_pillbar(0), m_sender(0), m_listener(0)
{
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

void ComposeBox::doSend()
{
    char *buf;
    int n;

    if (m_listener && m_listener->isSendBlocked()) return;

    /* Refuse rather than truncate. GetWindowTextA copies what fits and says
     * nothing, so a pasted log longer than the buffer was silently cut and the
     * SHORTENED text is what went to the model -- the person at the keyboard
     * losing bytes on the one path where they are the author. EM_LIMITTEXT
     * below normally stops it arriving at all; this is the check that makes
     * that a guarantee rather than a hope. */
    if (GetWindowTextLengthA(m_box) >= RC_INPUT_MAX) {
        if (m_html)
            m_html->error(
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
        if (m_html) m_html->error("Out of memory. Nothing was sent.");
        return;
    }

    n = GetWindowTextA(m_box, buf, RC_INPUT_MAX);
    if (n <= 0 && (!m_pillbar || !m_pillbar->active())) {
        HeapFree(GetProcessHeap(), 0, buf);
        return;
    }

    /* status_refresh() used to run between note_tx_done and the
     * SetWindowTextA that clears the box; it now runs just after, bundled
     * into `sent` with ui_set_busy(1) -- both are synchronous UI refreshes
     * with no dependency on one another or on the box's text, so the reorder
     * is not observable. */
    if (sendText(buf)) SetWindowTextA(m_box, "");
    HeapFree(GetProcessHeap(), 0, buf);
    SetFocus(m_box);
}

bool ComposeBox::sendText(const char *text)
{
    unsigned n;

    if ((!text || !*text) && (!m_pillbar || !m_pillbar->active())) return false;
    /* The same refusal the Send button gets, not a second opinion about it:
     * a menu entry that sends a prompt mid-turn would be the one path around
     * the check that stops two turns overlapping. */
    if (m_listener && m_listener->isSendBlocked()) return false;

    n = text ? (unsigned)strlen(text) : 0;
    if (n >= RC_INPUT_MAX) {
        if (m_html) m_html->error("That message is too long to send.");
        return false;
    }

    const char *keys[4];
    const char *vals[4];
    int nkv = 0;
    std::string workspace, extIds, extNames;

    if (m_pillbar && m_pillbar->active()) {
        workspace = m_pillbar->workspaceAttachments();
        if (!workspace.empty()) {
            keys[nkv] = "attach";
            vals[nkv] = workspace.c_str();
            nkv++;
        }
        m_pillbar->externalAttachments(&extIds, &extNames);
        if (!extIds.empty()) {
            keys[nkv] = "attach_id";
            vals[nkv] = extIds.c_str();
            nkv++;
            keys[nkv] = "attach_name";
            vals[nkv] = extNames.c_str();
            nkv++;
        }
    }

    /* NO LOCAL ECHO. The bridge sends the user turn like every other block
     * from stage 3b, and the attachment names reach it in `attach_name`
     * above, so it can build the same line this side used to.
     *
     * It was echoed here, before the send, so typing felt responsive on a
     * dialup link. The copy went into whatever window was on screen, which
     * after this stage can be one the reader has paged back to -- where
     * that record does not belong and where the bridge, having recorded it
     * at the end, will never put it. It also never quite matched the
     * bridge's own copy: user_turn strips before escaping and this side's
     * escaper did not, so a trailing blank line rendered one line taller
     * here than in the copy shown after paging back (audit 01.29).
     *
     * What that cost: two small frames of round trip. What it bought: one
     * source for the transcript, one escaper, and a block that can only
     * land where it belongs. */
    if (m_listener) m_listener->returnToLive();

    if (m_xfer) m_xfer->note_tx_start((unsigned long)n);
    int sent = (rc_net_send(m_net, "INPUT", keys, vals, nkv, text ? text : "", n) == 0);
    if (sent) {
        if (m_pillbar && m_pillbar->active()) {
            m_pillbar->onSend();
        }
        if (m_xfer) m_xfer->note_tx_done((unsigned long)n);
        if (m_listener) m_listener->onSent();
        return true;
    }
    if (m_html) m_html->error("Not connected.");
    return false;
}

bool ComposeBox::compose(const char *text)
{
    if (!m_box || !text) return false;
    if (GetWindowTextLengthA(m_box) > 0) {
        if (m_html)
            m_html->error("There is already a message in the box. Send it or "
                        "clear it first, then pick the command again.");
        SetFocus(m_box);
        return false;
    }
    SetWindowTextA(m_box, text);
    /* Caret to the end, so typing continues the line rather than landing in
     * front of the command -- EM_SETSEL with both ends past the text. */
    SendMessageA(m_box, EM_SETSEL, (WPARAM)-1, (LPARAM)-1);
    SetFocus(m_box);
    return true;
}

void ComposeBox::doStop()
{
    rc_log("stop requested");
    /* A lost INTERRUPT is a Stop button that does nothing, on a turn that
     * keeps running and keeps costing. */
    if (m_sender)
        m_sender->sendFrame("Stop", "INTERRUPT", NULL, NULL, 0, NULL, 0);
}

/* Enter sends; Shift+Enter inserts a newline. */
LRESULT ComposeBox::EditBox::handle(UINT m, WPARAM w, LPARAM l)
{
    if (m == WM_KEYDOWN && w == VK_RETURN
        && !(GetKeyState(VK_SHIFT) & 0x8000)) {
        m_owner->doSend();
        return 0;
    }
    if (m == WM_CHAR && w == VK_RETURN && !(GetKeyState(VK_SHIFT) & 0x8000))
        return 0;   /* swallow the beep */

    if (m == WM_DROPFILES) {
        SendMessageA(GetParent(hwnd()), WM_DROPFILES, w, l);
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
        SendMessageA(hwnd(), EM_SETSEL, 0, (LPARAM)-1);
        return 0;
    }
    if (m == WM_CHAR && w == 1)         /* Ctrl+A arrives as SOH */
        return 0;
    return previous(m, w, l);
}

ComposeBox *ComposeBox::create(HWND parent, HINSTANCE inst, HFONT font,
                               rc_net *m_net, rc_html *m_html, rc_xferstat *m_xfer,
                               AttachmentBar *m_pillbar, FrameSender *m_sender,
                               ComposeListener *listener)
{
    ComposeBox *p = new ComposeBox();
    if (!p) return 0;

    p->m_net = m_net;
    p->m_html = m_html;
    p->m_xfer = m_xfer;
    p->m_pillbar = m_pillbar;
    p->m_sender = m_sender;
    p->m_listener = listener;

    HWND edit = CreateWindowExA(WS_EX_CLIENTEDGE, "EDIT", "",
        WS_CHILD | WS_VISIBLE | WS_TABSTOP | WS_VSCROLL |
        ES_MULTILINE | ES_AUTOVSCROLL | ES_WANTRETURN,
        0, 0, 0, 0, parent, (HMENU)ID_INPUT, inst, NULL);
    check_child("input", edit);
    /* Owned and subclassed from here. */
    if (edit && !p->m_edit.attach(edit))
        DestroyWindow(edit);
    p->m_box = p->m_edit.hwnd();
    /* Stop an over-long message being composed at all, which is the only
     * feedback that arrives at the moment the user does it rather than at
     * send time. The default for a multiline EDIT is 32KB, well past what
     * do_send can carry. */
    SendMessageA(p->m_box, EM_LIMITTEXT, (WPARAM)(RC_INPUT_MAX - 1), 0);

    p->m_sendButton = CreateWindowExA(0, "BUTTON", "&Send",
        WS_CHILD | WS_VISIBLE | WS_TABSTOP | BS_DEFPUSHBUTTON,
        0, 0, 0, 0, parent, (HMENU)ID_SEND, inst, NULL);
    check_child("send", p->m_sendButton);

    p->m_stopButton = CreateWindowExA(0, "BUTTON", "S&top",
        WS_CHILD | WS_VISIBLE | WS_TABSTOP | BS_PUSHBUTTON,
        0, 0, 0, 0, parent, (HMENU)ID_STOP, inst, NULL);
    check_child("stop", p->m_stopButton);
    EnableWindow(p->m_stopButton, FALSE);

    SendMessageA(p->m_box,      WM_SETFONT, (WPARAM)font, TRUE);
    SendMessageA(p->m_sendButton, WM_SETFONT, (WPARAM)font, TRUE);
    SendMessageA(p->m_stopButton, WM_SETFONT, (WPARAM)font, TRUE);

    return p;
}

void ComposeBox::move(int cx, int y, int cw, int winW)
{
    /* The "4" here is main.c's GUTTER, which this module cannot see by name
     * (it is a plain #define in main.cpp, not exported by any header) --
     * same value, spelled out, exactly as bar.cpp's move and pagebar.cpp's
     * already do for the same constant. */
    MoveWindow(m_box, cx, y, cw - 4 - BUTTON_WIDTH, INPUT_HEIGHT, TRUE);
    MoveWindow(m_sendButton, winW - 4 - BUTTON_WIDTH, y, BUTTON_WIDTH, 24, TRUE);
    MoveWindow(m_stopButton, winW - 4 - BUTTON_WIDTH, y + 28, BUTTON_WIDTH, 24, TRUE);
}

bool ComposeBox::command(int id)
{
    switch (id) {
    case ID_SEND: doSend(); return true;
    case ID_STOP: doStop(); return true;
    }
    return false;
}

void ComposeBox::enableSend(bool enable)
{
    EnableWindow(m_sendButton, enable ? TRUE : FALSE);
}

void ComposeBox::enableStop(bool enable)
{
    EnableWindow(m_stopButton, enable ? TRUE : FALSE);
}

void ComposeBox::focus()
{
    SetFocus(m_box);
}

bool ComposeBox::isEmpty() const
{
    /* WM_GETTEXTLENGTH rather than reading the text: the answer is a count,
     * the box holds up to RC_INPUT_MAX bytes, and this is called from the
     * message loop on every PgUp. */
    return SendMessageA(m_box, WM_GETTEXTLENGTH, 0, 0) == 0;
}

bool ComposeBox::hasFocus() const
{
    return GetFocus() == m_box;
}
