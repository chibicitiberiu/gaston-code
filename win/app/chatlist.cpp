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

rc_chats *rc_chats::s_instance;

rc_chats::~rc_chats()
{
    /* Restore BEFORE clearing s_instance: the order matters, because
     * anything dispatched between the two would otherwise reach list_proc
     * with a null singleton. After this the listbox is an ordinary listbox
     * and can be destroyed by Windows in its own time. */
    if (list && oldproc)
        SetWindowLongA(list, GWL_WNDPROC, (LONG)oldproc);
    oldproc = NULL;
    if (s_instance == this) s_instance = NULL;
}

/* Width available for an item's text: the client area less the vertical
 * scrollbar and the couple of pixels the listbox insets text by. */
int rc_chats::text_width()
{
    RECT rc;

    if (!list) return 0;
    GetClientRect(list, &rc);
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
void rc_chats::fit_label(char *label, unsigned cap, int avail_px, int bold)
{
    SIZE sz;
    INT fit = 0;
    int n;
    char buf[192];

    /* This did not guard its argument. One caller today, always a local
     * array, so the invariant held by accident of there being a single call
     * site. C89: declarations first. */
    if (!label || !cap) return;
    if (!list || avail_px <= 0) return;
    n = (int)strlen(label);
    if (n <= 0) return;

    {
        WindowDC dc(list);
        if (!dc.valid()) return;
        /* THE FONT THE ROW IS ACTUALLY DRAWN WITH. The open conversation is
         * drawn in font_bold (see the draw code below), and bold is wider
         * than normal -- so measuring every row with font_normal let exactly
         * that row overflow. That is the condition this file documents as
         * making Win98's listbox paint over its own WS_EX_CLIENTEDGE border,
         * "measured as white pixels at x=8,9": the documented fix did not
         * cover the one row most likely to trigger it. (audit 01.37) */
        Selected sel(dc.get(), bold ? font_bold : font_normal);
        sz.cx = 0;
        if (GetTextExtentExPointA(dc.get(), label, n, avail_px, &fit, NULL, &sz)
            && fit < n) {
            if (fit > 3) fit -= 3; else fit = 1;
            rc_strf(buf, sizeof(buf), "%.*s...", fit, label);
            rc_strf(label, cap, "%s", buf);
            label[cap - 1] = '\0';
        }
    }
}

void rc_chats::request()
{
    rc_net_send(net, "GETSESSIONS", NULL, NULL, 0, NULL, 0);
}

void rc_chats::open_selected()
{
    int sel = (int)SendMessageA(list, LB_GETCURSEL, 0, 0);
    const char *keys[1]; const char *vals[1];

    if (sel == LB_ERR || sel < 0 || sel >= chat_count) return;
    if (chat_is_header[sel]) return;              /* a date header, not a chat */
    if (strcmp(chat_ids[sel], net->sess) == 0) return;   /* already open */

    /* Switching closes the live client, which kills a running request. Ask
     * rather than doing it silently -- a click in the list is easy to make by
     * accident and the reply that is lost took real time to produce. That
     * policy question is not this class's to answer -- see rc_chats_confirm_fn
     * in app/chatlist.h. */
    if (confirm && !confirm()) {
        /* Put the highlight back on the conversation that is actually open. */
        if (chat_current >= 0)
            SendMessageA(list, LB_SETCURSEL, chat_current, 0);
        return;
    }

    keys[0] = "id"; vals[0] = chat_ids[sel];
    rc_log("switching to session %s", chat_ids[sel]);
    /* Loading a conversation means closing the live client, resuming, and
     * replaying its history, which is not instant. Say so rather than
     * looking frozen -- that is main.c's title bar and status pane, not
     * this class's, hence the callback. */
    if (switching) switching();
    sender->sendFrame("Switching conversation", "SWITCH", keys, vals, 1, NULL, 0);
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
 * A static trampoline rather than a member function: WNDPROC's signature is
 * fixed by Windows, and there is only ever one instance -- s_instance is the
 * same singleton assumption the pre-class g_chats_oldproc already relied
 * on. */
LRESULT CALLBACK rc_chats::list_proc(HWND h, UINT m, WPARAM w, LPARAM l)
{
    LRESULT r;
    /* The destructor un-subclasses, so this should be unreachable with a
     * null singleton. It is checked anyway: the cost is one compare and the
     * alternative was a page fault with no log line. */
    if (!s_instance || !s_instance->oldproc)
        return DefWindowProcA(h, m, w, l);
    r = CallWindowProc(s_instance->oldproc, h, m, w, l);
    if (m == WM_LBUTTONUP)
        PostMessageA(GetParent(h), WM_RC_OPENCHAT, 0, 0);
    return r;
}

void rc_chats::on_sessions(rc_frame *f)
{
    (void)f;
    begin_rows();
}

void rc_chats::on_session(rc_frame *f)
{
    add_row(rc_frame_get(f, "id"), rc_frame_get(f, "title"),
            rc_frame_get(f, "group"),
            (rc_frame_get(f, "current")
             && *rc_frame_get(f, "current") == '1'));
}

/* The body of on_session, with the frame taken off the front.
 *
 * Split out so the disk cache can replay a list without building frames the
 * wire never sent -- app/cache.h keeps the rows, not the framing, and a
 * cached row and a live one have to land in the list by the same path or
 * they will differ in some detail nobody thinks to check until it is wrong
 * on one of them. */
void rc_chats::add_row(const char *id, const char *title, const char *grp,
                       int current)
{
    if (id && chat_count < MAX_CHATS) {
        char label[RC_CHAT_LABEL];

        /* A header row whenever the bucket changes. The list arrives
         * newest first, so these come out in order on their own. */
        if (grp && *grp && strcmp(grp, chat_group) != 0
            && chat_count < MAX_CHATS - 1) {
            rc_strf(chat_group, sizeof(chat_group), "%s", grp);
            chat_ids[chat_count][0] = '\0';
            chat_is_header[chat_count] = 1;
            SendMessageA(list, LB_ADDSTRING, 0, (LPARAM)chat_group);
            chat_count++;
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
         * matters more now than it did, since chat_ids is a heap-allocated
         * member of this class (see core/rc_cpp.h's operator new) and there
         * is no zero-filled BSS behind it to fall back on. */
        rc_strcpy(chat_ids[chat_count],
                  sizeof(chat_ids[chat_count]), id);
        chat_is_header[chat_count] = 0;
        /* `current` is the row that gets drawn bold -- measure it that way. */
        fit_label(label, sizeof(label), text_width() - CHAT_INDENT, current);
        SendMessageA(list, LB_ADDSTRING, 0, (LPARAM)label);
        if (current) {
            chat_current = chat_count;
            SendMessageA(list, LB_SETCURSEL, chat_count, 0);
        }
        chat_count++;
    }
}

void rc_chats::on_sessionsend(rc_frame *f)
{
    (void)f;
    end_rows();
    rc_log("chat list: %d sessions", chat_count);
}

void rc_chats::begin_rows()
{
    SendMessageA(list, WM_SETREDRAW, FALSE, 0);
    SendMessageA(list, LB_RESETCONTENT, 0, 0);
    chat_count = 0;
    chat_group[0] = '\0';
    chat_current = -1;
}

/* See the declaration in chatlist.h. */
void rc_chats::abort_rows()
{
    /* The PIXELS are the thing that has to be put right. LB_RESETCONTENT has
     * already run with redraw off, so the listbox is empty while the screen
     * still shows the old list -- and chat_ids[]/chat_is_header[] were being
     * refilled from a batch that stopped halfway. Turning redraw back on and
     * repainting makes the screen agree with the array again, which is the
     * whole invariant: open_selected() indexes chat_ids[] by the row the user
     * clicked, so a screen showing rows the array no longer has means a click
     * on "Fix the parser" switches to whatever conversation now occupies that
     * index -- tearing down the live session on a confirmation the user gave
     * for a different switch.
     *
     * Leaving the partial batch in place rather than clearing it: a short list
     * is honest about what arrived, and the bridge resends the whole list on
     * reconnect anyway. */
    end_rows();
}

void rc_chats::end_rows()
{
    /* RDW_FRAME as well as the client: the frame is what draws the
     * client edge, and repainting only the interior leaves it stale. */
    SendMessageA(list, WM_SETREDRAW, TRUE, 0);
    RedrawWindow(list, NULL, NULL,
                 RDW_INVALIDATE | RDW_ERASE | RDW_FRAME | RDW_UPDATENOW);
}

void rc_chats::on_switched(rc_frame *f)
{
    /* `net->sess` is NOT written here any more. It is rc_net state
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
        chat_current = -1;
        for (i = 0; i < chat_count; i++) {
            if (!chat_is_header[i]
                && strcmp(chat_ids[i], net->sess) == 0) {
                chat_current = i;
                break;
            }
        }
        InvalidateRect(list, NULL, TRUE);
    }
    if (switched) switched(f);
}

int rc_chats::frame(rc_frame *f)
{
    if (strcmp(f->type, "SESSIONS") == 0)    { on_sessions(f);    return 1; }
    if (strcmp(f->type, "SESSION") == 0)     { on_session(f);     return 1; }
    if (strcmp(f->type, "SESSIONSEND") == 0) { on_sessionsend(f); return 1; }
    if (strcmp(f->type, "SWITCHED") == 0)    { on_switched(f);    return 1; }
    return 0;
}

int rc_chats::measure(MEASUREITEMSTRUCT *mis)
{
    if (!mis || mis->CtlID != ID_CHATS) return 0;
    mis->itemHeight = CHAT_ROW_H;
    return 1;
}

int rc_chats::draw(DRAWITEMSTRUCT *dis)
{
    /* Sized from the label bound, not a literal -- see LB_GETTEXT below. */
    char text[RC_CHAT_LABEL + 48];
    RECT rc;
    int header, selected;
    COLORREF oldfg;
    int oldbk;

    if (!dis || dis->CtlID != ID_CHATS) return 0;
    if ((int)dis->itemID < 0) return 1;

    /* LB_GETTEXT HAS NO SIZE PARAMETER -- it writes as much as the item
     * holds, so the buffer must be at least as large as the largest string
     * anything adds. The only two are `label[RC_CHAT_LABEL]` and
     * chat_group[40]; sizing `text` FROM the label bound makes that a
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
    header = ((int)dis->itemID < chat_count
              && chat_is_header[dis->itemID]);
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
        return 1;
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
        HFONT want = ((int)dis->itemID == chat_current && font_bold)
                     ? font_bold : font_normal;
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
    return 1;
}

HWND rc_chats::hwnd() const
{
    return list;
}

void rc_chats::move(int x, int y, int w, int col_h)
{
    int list_h = col_h - 24;
    MoveWindow(list, x, y, w, list_h, TRUE);
    MoveWindow(refresh, x, y + list_h + 2, w, 22, TRUE);
}

static void check_child(const char *what, HWND h)
{
    if (!h)
        rc_log("chatlist: %s failed to create (GetLastError=%lu)",
               what, (unsigned long)GetLastError());
}

rc_chats::rc_chats(HWND parent, HINSTANCE inst, rc_net *net_, HFONT normal,
                    HFONT bold, rc_chats_confirm_fn confirm_,
                    FrameSender *send_, void (*switching_)(void),
                    void (*switched_)(rc_frame *f))
    : chat_current(-1), chat_count(0), font_normal(normal), font_bold(bold),
      net(net_), confirm(confirm_), sender(send_), switching(switching_),
      switched(switched_)
{
    chat_group[0] = '\0';
    s_instance = this;

    /* WS_CLIPSIBLINGS: keeps neighbours out of its rectangle.
     *
     * LBS_OWNERDRAWFIXED with LBS_HASSTRINGS: the listbox still stores and
     *   searches the strings, we only take over the painting. That is what
     *   lets a date-group header look like a header rather than like a
     *   conversation called "Today".
     *
     * No WS_HSCROLL: labels are truncated to fit instead, see fit_label().
     * An item wider than the client area makes Win98's listbox paint over its
     * own WS_EX_CLIENTEDGE border -- measured as white pixels at x=8,9 where
     * (128,128,128) and (0,0,0) should be, on exactly the rows whose text
     * overflowed, surviving both a scroll and a minimise/restore. */
    list = CreateWindowExA(WS_EX_CLIENTEDGE, "LISTBOX", "",
        WS_CHILD | WS_VISIBLE | WS_TABSTOP | WS_VSCROLL |
        WS_CLIPSIBLINGS | LBS_NOTIFY | LBS_HASSTRINGS | LBS_NOINTEGRALHEIGHT |
        LBS_OWNERDRAWFIXED,
        0, 0, 0, 0, parent, (HMENU)ID_CHATS, inst, NULL);
    check_child("chats", list);

    /* WS_TABSTOP and a mnemonic: this was mouse-only. The menu bar owns
     * F/C/M/E/H, so R is free. */
    refresh = CreateWindowExA(0, "BUTTON", "&Refresh",
        WS_CHILD | WS_VISIBLE | WS_TABSTOP | BS_PUSHBUTTON, 0, 0, 0, 0,
        parent, (HMENU)ID_REFRESH, inst, NULL);
    check_child("refresh", refresh);

    SendMessageA(list,    WM_SETFONT, (WPARAM)font_normal, TRUE);
    SendMessageA(refresh, WM_SETFONT, (WPARAM)font_normal, TRUE);

    oldproc = (WNDPROC)SetWindowLongA(list, GWL_WNDPROC,
                                      (LONG)(LONG_PTR)list_proc);
}

rc_chats *rc_chats::create(HWND parent, HINSTANCE inst, rc_net *net,
                            HFONT normal, HFONT bold,
                            rc_chats_confirm_fn confirm,
                            FrameSender *send, void (*switching)(void),
                            void (*switched)(rc_frame *f))
{
    return new rc_chats(parent, inst, net, normal, bold, confirm, send,
                         switching, switched);
}
