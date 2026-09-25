/* The chat list. See app/chatlist.h for the design.
 *
 * A ListBox rather than a ListView deliberately: fewer GDI objects, and the
 * 16-bit USER/GDI heaps on 9x are a fixed 64KB that does not scale with RAM.
 * Owner-draw so a date header reads as a header instead of as a conversation
 * called "Today" -- LBS_HASSTRINGS keeps the strings, this only replaces the
 * painting.
 */
#include "app/chatlist.h"
#include "core/str.h"
#include "core/log.h"
#include "core/rc_cpp.h"
#include <string.h>
#include <stdlib.h>

#define CHAT_INDENT 10   /* session rows sit under their date header */
#define CHAT_ROW_H  15

/* Width available for an item's text: the client area less the vertical
 * scrollbar and the couple of pixels the listbox insets text by. */
int ChatList::textWidth()
{
    RECT rc;

    if (!m_list) return 0;
    GetClientRect(m_list, &rc);
    return (rc.right - rc.left) - GetSystemMetrics(SM_CXVSCROLL) - 6;
}

/* Shorten a label with an ellipsis until it fits.
 *
 * Two reasons, and the cosmetic one is the lesser. A label wider than the
 * client area makes Win98's listbox overdraw its own client edge (see the
 * CreateWindowEx comment). And silently cutting a name off at the frame reads
 * as a rendering glitch, where "..." reads as "there is more".
 *
 * GetTextExtentExPointA answers "how many characters fit in N pixels" in one
 * call, so this stays one GDI round trip per label rather than a search. */
void ChatList::fitLabel(char *label, unsigned cap, int availPx, bool bold)
{
    SIZE sz;
    INT fit = 0;
    int n;
    char buf[192];

    /* This did not guard its argument. One caller today, always a local
     * array, so the invariant held by accident of there being a single call
     * site. C89: declarations first. */
    if (!label || !cap) return;
    if (!m_list || availPx <= 0) return;
    n = (int)strlen(label);
    if (n <= 0) return;

    {
        WindowDC dc(m_list);
        if (!dc.valid()) return;
        /* THE FONT THE ROW IS ACTUALLY DRAWN WITH. The open conversation is
         * drawn in m_fontBold (see the draw code below), and bold is wider
         * than normal -- so measuring every row with m_fontNormal let exactly
         * that row overflow. That is the condition this file documents as
         * making Win98's listbox paint over its own WS_EX_CLIENTEDGE border,
         * "measured as white pixels at x=8,9": the documented fix did not
         * cover the one row most likely to trigger it. (audit 01.37) */
        Selected sel(dc.get(), bold ? m_fontBold : m_fontNormal);
        sz.cx = 0;
        if (GetTextExtentExPointA(dc.get(), label, n, availPx, &fit, NULL, &sz)
            && fit < n) {
            if (fit > 3) fit -= 3; else fit = 1;
            rc_strf(buf, sizeof(buf), "%.*s...", fit, label);
            rc_strf(label, cap, "%s", buf);
            label[cap - 1] = '\0';
        }
    }
}

void ChatList::request()
{
    rc_net_send(m_net, "GETSESSIONS", NULL, NULL, 0, NULL, 0);
}

void ChatList::openSelected()
{
    int sel = (int)SendMessageA(m_list, LB_GETCURSEL, 0, 0);
    const char *keys[1]; const char *vals[1];

    if (sel == LB_ERR || sel < 0 || sel >= m_count) return;
    if (m_isHeader[sel]) return;              /* a date header, not a chat */
    if (strcmp(m_ids[sel], m_net->sess) == 0) return;   /* already open */

    /* Switching closes the live client, which kills a running request. Ask
     * rather than doing it silently -- a click in the list is easy to make by
     * accident and the reply that is lost took real time to produce. That
     * policy question is not this class's to answer -- see rc_chats_confirm_fn
     * in app/chatlist.h. */
    if (m_listener && !m_listener->confirmSwitch()) {
        /* Put the highlight back on the conversation that is actually open. */
        if (m_current >= 0)
            SendMessageA(m_list, LB_SETCURSEL, m_current, 0);
        return;
    }

    keys[0] = "id"; vals[0] = m_ids[sel];
    rc_log("switching to session %s", m_ids[sel]);
    /* Loading a conversation means closing the live client, resuming, and
     * replaying its history, which is not instant. Say so rather than
     * looking frozen -- that is main.c's title bar and status pane, not
     * this class's, hence the callback. */
    if (m_listener) m_listener->onSwitching();
    m_sender->sendFrame("Switching conversation", "SWITCH", keys, vals, 1, NULL, 0);
}

/* Open a conversation on a plain click.
 *
 * Not via LBN_SELCHANGE: that also fires for arrow keys, and switching on
 * those would tear down a live session every time the list was scrolled
 * with the keyboard. Testing the mouse button state inside the notification
 * was tried and is unreliable -- by the time it arrives the button may
 * already be up. Handling the button release directly is unambiguous. The
 * switch is POSTED rather than done here so the listbox finishes with the
 * click (and releases capture) before the transcript is torn down and
 * rebuilt.
 *
 */
LRESULT ChatList::ListBox::handle(UINT msg, WPARAM wp, LPARAM lp)
{
    LRESULT r = previous(msg, wp, lp);
    if (msg == WM_LBUTTONUP)
        PostMessageA(GetParent(hwnd()), WM_RC_OPENCHAT, 0, 0);
    return r;
}

void ChatList::onSessions(rc_frame *f)
{
    (void)f;
    beginRows();
}

void ChatList::onSession(rc_frame *f)
{
    addRow(rc_frame_get(f, "id"), rc_frame_get(f, "title"),
            rc_frame_get(f, "group"),
            (rc_frame_get(f, "current")
             && *rc_frame_get(f, "current") == '1'));
}

/* The body of onSession, with the frame taken off the front.
 *
 * Split out so the disk cache can replay a list without building frames the
 * wire never sent -- app/cache.h keeps the rows, not the framing, and a
 * cached row and a live one have to land in the list by the same path or
 * they will differ in some detail nobody thinks to check until it is wrong
 * on one of them. */
void ChatList::addRow(const char *id, const char *title, const char *grp,
                      bool current)
{
    if (id && m_count < MAX_CHATS) {
        char label[RC_CHAT_LABEL];

        /* A header row whenever the bucket changes. The list arrives
         * newest first, so these come out in order on their own. */
        if (grp && *grp && strcmp(grp, m_group) != 0
            && m_count < MAX_CHATS - 1) {
            rc_strf(m_group, sizeof(m_group), "%s", grp);
            m_ids[m_count][0] = '\0';
            m_isHeader[m_count] = 1;
            SendMessageA(m_list, LB_ADDSTRING, 0, (LPARAM)m_group);
            m_count++;
        }

        /* No "* " marker any more: the selection highlight already says
         * which conversation is open, and a marker meant every switch had
         * to relabel the whole list. */
        rc_strf(label, sizeof(label), "%s", title ? title : id);
        /* rc_strcpy, and the bound is sizeof rather than a literal 63.
         *
         * `id` is a wire header value and may legitimately be up to 255
         * bytes. The old `_snprintf(dst, 63, ...)` wrote indices 0..62 of a
         * 64-byte row and never touched index 63, so termination came from
         * the row being zero-filled BSS that nothing else wrote -- an
         * unstated invariant rather than a contract; one memset, or one
         * widened copy, and strcmp at the two call sites below would have
         * walked off the end. rc_strcpy does not have that problem: it
         * always writes a terminator within cap, truncating or not -- which
         * matters more now than it did, since m_ids is a heap-allocated
         * member of this class (see core/rc_cpp.h's operator new) and there
         * is no zero-filled BSS behind it to fall back on. */
        rc_strcpy(m_ids[m_count],
                  sizeof(m_ids[m_count]), id);
        m_isHeader[m_count] = 0;
        /* `current` is the row that gets drawn bold -- measure it that way. */
        fitLabel(label, sizeof(label), textWidth() - CHAT_INDENT, current);
        SendMessageA(m_list, LB_ADDSTRING, 0, (LPARAM)label);
        if (current) {
            m_current = m_count;
            SendMessageA(m_list, LB_SETCURSEL, m_count, 0);
        }
        m_count++;
    }
}

void ChatList::onSessionsEnd(rc_frame *f)
{
    (void)f;
    endRows();
    rc_log("chat list: %d sessions", m_count);
}

void ChatList::beginRows()
{
    SendMessageA(m_list, WM_SETREDRAW, FALSE, 0);
    SendMessageA(m_list, LB_RESETCONTENT, 0, 0);
    m_count = 0;
    m_group[0] = '\0';
    m_current = -1;
}

/* See the declaration in chatlist.h. */
void ChatList::abortRows()
{
    /* The PIXELS are the thing that has to be put right. LB_RESETCONTENT has
     * already run with redraw off, so the listbox is empty while the screen
     * still shows the old list -- and m_ids[]/m_isHeader[] were being
     * refilled from a batch that stopped halfway. Turning redraw back on and
     * repainting makes the screen agree with the array again, which is the
     * whole invariant: openSelected() indexes m_ids[] by the row the user
     * clicked, so a screen showing rows the array no longer has means a click
     * on "Fix the parser" switches to whatever conversation now occupies that
     * index -- tearing down the live session on a confirmation the user gave
     * for a different switch.
     *
     * Leaving the partial batch in place rather than clearing it: a short list
     * is honest about what arrived, and the bridge resends the whole list on
     * reconnect anyway. */
    endRows();
}

void ChatList::endRows()
{
    /* RDW_FRAME as well as the client: the frame is what draws the
     * client edge, and repainting only the interior leaves it stale. */
    SendMessageA(m_list, WM_SETREDRAW, TRUE, 0);
    RedrawWindow(m_list, NULL, NULL,
                 RDW_INVALIDATE | RDW_ERASE | RDW_FRAME | RDW_UPDATENOW);
}

void ChatList::onSwitchedFrame(rc_frame *f)
{
    /* `m_net->sess` is NOT written here any more. It is rc_net state
     * (net/net.h), read by HELLO's builder on the CONNECT path, and this
     * class writing it made a reconnect racing a SWITCHED able to read a
     * half-written 64-byte buffer and resume the wrong conversation or none.
     * It is also not this class's to own -- chatlist.h says this class keeps
     * "id-keyed bookkeeping", and the session id is not that.
     *
     * main.cpp is the single writer now, through the `switched` callback
     * below, which already exists for exactly this: "everything about
     * SWITCHED that is not the chat list's business." (audit 01.37) */
    /* Deliberately NOT refreshing the list here. Resuming a session
     * rewrites its file, so a refresh re-sorts by modification time and
     * yanks whatever you just opened to the top -- which is startling when
     * all you did was look at it. The list is re-fetched when a turn ends
     * instead, so it reorders only once you have actually said something.
     * The selection already shows which conversation is open. */
    {   /* Mark the newly opened conversation in place. Re-fetching the
         * list would reorder it, which is exactly what we are avoiding. */
        int i;
        m_current = -1;
        for (i = 0; i < m_count; i++) {
            if (!m_isHeader[i]
                && strcmp(m_ids[i], m_net->sess) == 0) {
                m_current = i;
                break;
            }
        }
        InvalidateRect(m_list, NULL, TRUE);
    }
    if (m_listener) m_listener->onSwitched(f);
}

bool ChatList::frame(rc_frame *f)
{
    if (strcmp(f->type, "SESSIONS") == 0)    { onSessions(f);    return true; }
    if (strcmp(f->type, "SESSION") == 0)     { onSession(f);     return true; }
    if (strcmp(f->type, "SESSIONSEND") == 0) { onSessionsEnd(f); return true; }
    if (strcmp(f->type, "SWITCHED") == 0)    { onSwitchedFrame(f); return true; }
    return false;
}

bool ChatList::measure(MEASUREITEMSTRUCT *mis)
{
    if (!mis || mis->CtlID != ID_CHATS) return false;
    mis->itemHeight = CHAT_ROW_H;
    return true;
}

bool ChatList::draw(DRAWITEMSTRUCT *dis)
{
    /* Sized from the label bound, not a literal -- see LB_GETTEXT below. */
    char text[RC_CHAT_LABEL + 48];
    RECT rc;
    int header, selected;
    COLORREF oldfg;
    int oldbk;

    if (!dis || dis->CtlID != ID_CHATS) return false;
    if ((int)dis->itemID < 0) return true;

    /* LB_GETTEXT HAS NO SIZE PARAMETER -- it writes as much as the item
     * holds, so the buffer must be at least as large as the largest string
     * anything adds. The only two are `label[RC_CHAT_LABEL]` and
     * m_group[40]; sizing `text` FROM the label bound makes that a
     * compile-time fact rather than an invariant nobody states, so widening
     * the label can no longer turn this into a stack overflow with no
     * diagnostic. LB_GETTEXTLEN first would be the belt as well as the
     * braces; the sizing is what removes the hazard. (audit 01.38) */
    text[0] = '\0';
    if (SendMessageA(dis->hwndItem, LB_GETTEXTLEN, dis->itemID, 0)
            < (LRESULT)sizeof(text))
        SendMessageA(dis->hwndItem, LB_GETTEXT, dis->itemID, (LPARAM)text);
    else
        rc_log("chatlist: row %d is longer than %u bytes; not drawing its "
               "text", (int)dis->itemID, (unsigned)sizeof(text));
    header = ((int)dis->itemID < m_count
              && m_isHeader[dis->itemID]);
    selected = (dis->itemState & ODS_SELECTED) && !header;
    rc = dis->rcItem;

    if (header) {
        /* A quiet band with a rule under it: visible as structure without
         * competing with the conversation titles. */
        GdiObj br(CreateSolidBrush(GetSysColor(COLOR_BTNFACE)));
        FillRect(dis->hDC, &rc, (HBRUSH)br.get());
        oldfg = SetTextColor(dis->hDC, GetSysColor(COLOR_BTNSHADOW));
        oldbk = SetBkMode(dis->hDC, TRANSPARENT);
        rc.left += 3;
        rc.top += 1;
        DrawTextA(dis->hDC, text, -1, &rc,
                  DT_LEFT | DT_SINGLELINE | DT_VCENTER | DT_NOPREFIX);
        SetBkMode(dis->hDC, oldbk);
        SetTextColor(dis->hDC, oldfg);
        rc = dis->rcItem;
        {   /* hairline under the band */
            GdiObj line(CreateSolidBrush(GetSysColor(COLOR_BTNSHADOW)));
            RECT r2 = rc;
            r2.top = r2.bottom - 1;
            FillRect(dis->hDC, &r2, (HBRUSH)line.get());
        }
        return true;
    }

    {
        GdiObj br(CreateSolidBrush(GetSysColor(
            selected ? COLOR_HIGHLIGHT : COLOR_WINDOW)));
        FillRect(dis->hDC, &rc, (HBRUSH)br.get());
    }
    oldfg = SetTextColor(dis->hDC, GetSysColor(
        selected ? COLOR_HIGHLIGHTTEXT : COLOR_WINDOWTEXT));
    oldbk = SetBkMode(dis->hDC, TRANSPARENT);
    rc.left += CHAT_INDENT;
    {
        HFONT want = ((int)dis->itemID == m_current && m_fontBold)
                     ? m_fontBold : m_fontNormal;
        /* dis->hDC is CALLER-OWNED -- only the SelectObject save/restore is
         * ours to do here, never a WindowDC around it. */
        Selected sel(dis->hDC, want);
        DrawTextA(dis->hDC, text, -1, &rc,
                  DT_LEFT | DT_SINGLELINE | DT_VCENTER | DT_NOPREFIX);
    }
    SetBkMode(dis->hDC, oldbk);
    SetTextColor(dis->hDC, oldfg);

    if (dis->itemState & ODS_FOCUS)
        DrawFocusRect(dis->hDC, &dis->rcItem);
    return true;
}

HWND ChatList::hwnd() const
{
    return m_list;
}

void ChatList::move(int x, int y, int w, int col_h)
{
    int list_h = col_h - 24;
    MoveWindow(m_list, x, y, w, list_h, TRUE);
    MoveWindow(m_refresh, x, y + list_h + 2, w, 22, TRUE);
}

static void check_child(const char *what, HWND h)
{
    if (!h)
        rc_log("chatlist: %s failed to create (GetLastError=%lu)",
               what, (unsigned long)GetLastError());
}

ChatList::ChatList(HWND parent, HINSTANCE inst, rc_net *net, HFONT normal,
                   HFONT bold, FrameSender *sender,
                   ChatListListener *listener)
    : m_current(-1), m_count(0), m_list(0), m_refresh(0),
      m_fontNormal(normal), m_fontBold(bold), m_net(net), m_sender(sender),
      m_listener(listener)
{
    m_group[0] = '\0';

    /* WS_CLIPSIBLINGS: keeps neighbours out of its rectangle.
     *
     * LBS_OWNERDRAWFIXED with LBS_HASSTRINGS: the listbox still stores and
     *   searches the strings, we only take over the painting. That is what
     *   lets a date-group header look like a header rather than like a
     *   conversation called "Today".
     *
     * No WS_HSCROLL: labels are truncated to fit instead, see fitLabel().
     * An item wider than the client area makes Win98's listbox paint over its
     * own WS_EX_CLIENTEDGE border -- measured as white pixels at x=8,9 where
     * (128,128,128) and (0,0,0) should be, on exactly the rows whose text
     * overflowed, surviving both a scroll and a minimise/restore. */
    HWND box = CreateWindowExA(WS_EX_CLIENTEDGE, "LISTBOX", "",
        WS_CHILD | WS_VISIBLE | WS_TABSTOP | WS_VSCROLL |
        WS_CLIPSIBLINGS | LBS_NOTIFY | LBS_HASSTRINGS | LBS_NOINTEGRALHEIGHT |
        LBS_OWNERDRAWFIXED,
        0, 0, 0, 0, parent, (HMENU)ID_CHATS, inst, NULL);
    check_child("chats", box);
    /* Owned and subclassed from here; the listbox is destroyed with this
     * object rather than left to its parent. */
    if (box && !m_listBox.attach(box))
        DestroyWindow(box);
    m_list = m_listBox.hwnd();

    /* WS_TABSTOP and a mnemonic: this was mouse-only. The menu bar owns
     * F/C/M/E/H, so R is free. */
    m_refresh = CreateWindowExA(0, "BUTTON", "&Refresh",
        WS_CHILD | WS_VISIBLE | WS_TABSTOP | BS_PUSHBUTTON, 0, 0, 0, 0,
        parent, (HMENU)ID_REFRESH, inst, NULL);
    check_child("refresh", m_refresh);

    SendMessageA(m_list,    WM_SETFONT, (WPARAM)m_fontNormal, TRUE);
    SendMessageA(m_refresh, WM_SETFONT, (WPARAM)m_fontNormal, TRUE);

}

ChatList *ChatList::create(HWND parent, HINSTANCE inst, rc_net *net,
                           HFONT normal, HFONT bold, FrameSender *sender,
                           ChatListListener *listener)
{
    return new ChatList(parent, inst, net, normal, bold, sender, listener);
}
