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
#include <string.h>
#include <stdlib.h>

#define CHAT_INDENT 10   /* session rows sit under their date header */
#define CHAT_ROW_H  15

/* Session ids, parallel to the list box. */
#define MAX_CHATS 64
static char g_chat_ids[MAX_CHATS][64];
/* 0 = a session row, 1 = a date-group header. Headers are drawn differently
 * and refuse to be opened. */
static char g_chat_is_header[MAX_CHATS];
static char g_chat_group[40];       /* last group emitted, to spot changes */
static int  g_chat_current = -1;    /* row of the conversation that is open */
static int  g_chat_count = 0;

static HWND g_chats, g_refresh;
static HFONT g_font_normal, g_font_bold;
static WNDPROC g_chats_oldproc;
static rc_net *g_net;
static rc_chats_confirm_fn g_confirm;
static rc_chats_send_fn g_send;
static void (*g_switching)(void);
static void (*g_switched)(rc_frame *f);

/* Width available for an item's text: the client area less the vertical
 * scrollbar and the couple of pixels the listbox insets text by. */
static int chats_text_width(void)
{
    RECT rc;

    if (!g_chats) return 0;
    GetClientRect(g_chats, &rc);
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
static void fit_label(char *label, unsigned cap, int avail_px)
{
    HDC dc;
    HFONT old;
    SIZE sz;
    INT fit = 0;
    int n;
    char buf[192];

    /* This did not guard its argument. One caller today, always a local
     * array, so the invariant held by accident of there being a single call
     * site. C89: declarations first. */
    if (!label || !cap) return;
    if (!g_chats || avail_px <= 0) return;
    n = (int)strlen(label);
    if (n <= 0) return;

    dc = GetDC(g_chats);
    if (!dc) return;
    old = (HFONT)SelectObject(dc, g_font_normal);
    sz.cx = 0;
    if (GetTextExtentExPointA(dc, label, n, avail_px, &fit, NULL, &sz)
        && fit < n) {
        if (fit > 3) fit -= 3; else fit = 1;
        rc_strf(buf, sizeof(buf), "%.*s...", fit, label);
        rc_strf(label, cap, "%s", buf);
        label[cap - 1] = '\0';
    }
    SelectObject(dc, old);
    ReleaseDC(g_chats, dc);
}

void rc_chats_request(void)
{
    rc_net_send(g_net, "GETSESSIONS", NULL, NULL, 0, NULL, 0);
}

void rc_chats_open_selected(void)
{
    int sel = (int)SendMessageA(g_chats, LB_GETCURSEL, 0, 0);
    const char *keys[1]; const char *vals[1];

    if (sel == LB_ERR || sel < 0 || sel >= g_chat_count) return;
    if (g_chat_is_header[sel]) return;          /* a date header, not a chat */
    if (strcmp(g_chat_ids[sel], g_net->sess) == 0) return;   /* already open */

    /* Switching closes the live client, which kills a running request. Ask
     * rather than doing it silently -- a click in the list is easy to make by
     * accident and the reply that is lost took real time to produce. That
     * policy question is not this module's to answer -- see rc_chats_confirm_fn
     * in app/chatlist.h. */
    if (g_confirm && !g_confirm()) {
        /* Put the highlight back on the conversation that is actually open. */
        if (g_chat_current >= 0)
            SendMessageA(g_chats, LB_SETCURSEL, g_chat_current, 0);
        return;
    }

    keys[0] = "id"; vals[0] = g_chat_ids[sel];
    rc_log("switching to session %s", g_chat_ids[sel]);
    /* Loading a conversation means closing the live client, resuming, and
     * replaying its history, which is not instant. Say so rather than
     * looking frozen -- that is main.c's title bar and status pane, not
     * this module's, hence the callback. */
    if (g_switching) g_switching();
    g_send("Switching conversation", "SWITCH", keys, vals, 1, NULL, 0);
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
 * rebuilt. */
static LRESULT CALLBACK chats_proc(HWND h, UINT m, WPARAM w, LPARAM l)
{
    LRESULT r = CallWindowProc(g_chats_oldproc, h, m, w, l);
    if (m == WM_LBUTTONUP)
        PostMessageA(GetParent(h), WM_RC_OPENCHAT, 0, 0);
    return r;
}

static void on_sessions(rc_frame *f)
{
    (void)f;
    SendMessageA(g_chats, WM_SETREDRAW, FALSE, 0);
    SendMessageA(g_chats, LB_RESETCONTENT, 0, 0);
    g_chat_count = 0;
    g_chat_group[0] = '\0';
    g_chat_current = -1;
}

static void on_session(rc_frame *f)
{
    const char *id = rc_frame_get(f, "id");
    const char *title = rc_frame_get(f, "title");
    const char *cur = rc_frame_get(f, "current");
    const char *grp = rc_frame_get(f, "group");
    if (id && g_chat_count < MAX_CHATS) {
        char label[160];

        /* A header row whenever the bucket changes. The list arrives
         * newest first, so these come out in order on their own. */
        if (grp && *grp && strcmp(grp, g_chat_group) != 0
            && g_chat_count < MAX_CHATS - 1) {
            rc_strf(g_chat_group, sizeof(g_chat_group), "%s", grp);
            g_chat_ids[g_chat_count][0] = '\0';
            g_chat_is_header[g_chat_count] = 1;
            SendMessageA(g_chats, LB_ADDSTRING, 0, (LPARAM)g_chat_group);
            g_chat_count++;
        }

        /* No "* " marker any more: the selection highlight already says
         * which conversation is open, and a marker meant every switch had
         * to relabel the whole list. */
        rc_strf(label, sizeof(label), "%s", title ? title : id);
        /* rc_strcpy, and the bound is sizeof rather than a literal 63.
         *
         * `id` is a wire header value and may legitimately be up to 255
         * bytes. _snprintf(dst, 63, ...) wrote indices 0..62 of a 64-byte
         * row and never touched index 63 -- so termination came from the
         * row being zero-filled BSS that nothing else writes. True today,
         * and an unstated invariant rather than a contract: one memset, or
         * one widened copy, and strcmp at the two call sites below walks
         * off the end. */
        rc_strcpy(g_chat_ids[g_chat_count],
                  sizeof(g_chat_ids[g_chat_count]), id);
        g_chat_is_header[g_chat_count] = 0;
        fit_label(label, sizeof(label), chats_text_width() - CHAT_INDENT);
        SendMessageA(g_chats, LB_ADDSTRING, 0, (LPARAM)label);
        if (cur && *cur == '1') {
            g_chat_current = g_chat_count;
            SendMessageA(g_chats, LB_SETCURSEL, g_chat_count, 0);
        }
        g_chat_count++;
    }
}

static void on_sessionsend(rc_frame *f)
{
    (void)f;
    /* RDW_FRAME as well as the client: the frame is what draws the
     * client edge, and repainting only the interior leaves it stale. */
    SendMessageA(g_chats, WM_SETREDRAW, TRUE, 0);
    RedrawWindow(g_chats, NULL, NULL,
                 RDW_INVALIDATE | RDW_ERASE | RDW_FRAME | RDW_UPDATENOW);
    rc_log("chat list: %d sessions", g_chat_count);
}

static void on_switched(rc_frame *f)
{
    const char *v = rc_frame_get(f, "id");
    if (v) rc_strf(g_net->sess, sizeof(g_net->sess), "%s", v);
    /* Deliberately NOT refreshing the list here. Resuming a session
     * rewrites its file, so a refresh re-sorts by modification time and
     * yanks whatever you just opened to the top -- which is startling when
     * all you did was look at it. The list is re-fetched when a turn ends
     * instead, so it reorders only once you have actually said something.
     * The selection already shows which conversation is open. */
    {   /* Mark the newly opened conversation in place. Re-fetching the
         * list would reorder it, which is exactly what we are avoiding. */
        int i;
        g_chat_current = -1;
        for (i = 0; i < g_chat_count; i++) {
            if (!g_chat_is_header[i]
                && strcmp(g_chat_ids[i], g_net->sess) == 0) {
                g_chat_current = i;
                break;
            }
        }
        InvalidateRect(g_chats, NULL, TRUE);
    }
    if (g_switched) g_switched(f);
}

int rc_chats_frame(rc_frame *f)
{
    if (strcmp(f->type, "SESSIONS") == 0)    { on_sessions(f);    return 1; }
    if (strcmp(f->type, "SESSION") == 0)     { on_session(f);     return 1; }
    if (strcmp(f->type, "SESSIONSEND") == 0) { on_sessionsend(f); return 1; }
    if (strcmp(f->type, "SWITCHED") == 0)    { on_switched(f);    return 1; }
    return 0;
}

int rc_chats_measure(MEASUREITEMSTRUCT *mis)
{
    if (!mis || mis->CtlID != ID_CHATS) return 0;
    mis->itemHeight = CHAT_ROW_H;
    return 1;
}

int rc_chats_draw(DRAWITEMSTRUCT *dis)
{
    char text[192];
    RECT rc;
    int header, selected;
    COLORREF oldfg;
    int oldbk;

    if (!dis || dis->CtlID != ID_CHATS) return 0;
    if ((int)dis->itemID < 0) return 1;

    text[0] = '\0';
    SendMessageA(dis->hwndItem, LB_GETTEXT, dis->itemID, (LPARAM)text);
    header = ((int)dis->itemID < g_chat_count
              && g_chat_is_header[dis->itemID]);
    selected = (dis->itemState & ODS_SELECTED) && !header;
    rc = dis->rcItem;

    if (header) {
        /* A quiet band with a rule under it: visible as structure without
         * competing with the conversation titles. */
        HBRUSH br = CreateSolidBrush(GetSysColor(COLOR_BTNFACE));
        FillRect(dis->hDC, &rc, br);
        DeleteObject(br);
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
            HBRUSH line = CreateSolidBrush(GetSysColor(COLOR_BTNSHADOW));
            RECT r2 = rc;
            r2.top = r2.bottom - 1;
            FillRect(dis->hDC, &r2, line);
            DeleteObject(line);
        }
        return 1;
    }

    {
        HBRUSH br = CreateSolidBrush(GetSysColor(
            selected ? COLOR_HIGHLIGHT : COLOR_WINDOW));
        FillRect(dis->hDC, &rc, br);
        DeleteObject(br);
    }
    oldfg = SetTextColor(dis->hDC, GetSysColor(
        selected ? COLOR_HIGHLIGHTTEXT : COLOR_WINDOWTEXT));
    oldbk = SetBkMode(dis->hDC, TRANSPARENT);
    rc.left += CHAT_INDENT;
    {
        HFONT want = ((int)dis->itemID == g_chat_current && g_font_bold)
                     ? g_font_bold : g_font_normal;
        HFONT oldf = (HFONT)SelectObject(dis->hDC, want);
        DrawTextA(dis->hDC, text, -1, &rc,
                  DT_LEFT | DT_SINGLELINE | DT_VCENTER | DT_NOPREFIX);
        SelectObject(dis->hDC, oldf);
    }
    SetBkMode(dis->hDC, oldbk);
    SetTextColor(dis->hDC, oldfg);

    if (dis->itemState & ODS_FOCUS)
        DrawFocusRect(dis->hDC, &dis->rcItem);
    return 1;
}

HWND rc_chats_hwnd(void)
{
    return g_chats;
}

void rc_chats_move(int x, int y, int w, int col_h)
{
    int list_h = col_h - 24;
    MoveWindow(g_chats, x, y, w, list_h, TRUE);
    MoveWindow(g_refresh, x, y + list_h + 2, w, 22, TRUE);
}

static void check_child(const char *what, HWND h)
{
    if (!h)
        rc_log("chatlist: %s failed to create (GetLastError=%lu)",
               what, (unsigned long)GetLastError());
}

void rc_chats_create(HWND parent, HINSTANCE inst, rc_net *net,
                      HFONT normal, HFONT bold, rc_chats_confirm_fn confirm,
                      rc_chats_send_fn send, void (*switching)(void),
                      void (*switched)(rc_frame *f))
{
    g_net = net;
    g_font_normal = normal;
    g_font_bold = bold;
    g_confirm = confirm;
    g_send = send;
    g_switching = switching;
    g_switched = switched;

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
    g_chats = CreateWindowExA(WS_EX_CLIENTEDGE, "LISTBOX", "",
        WS_CHILD | WS_VISIBLE | WS_TABSTOP | WS_VSCROLL |
        WS_CLIPSIBLINGS | LBS_NOTIFY | LBS_HASSTRINGS | LBS_NOINTEGRALHEIGHT |
        LBS_OWNERDRAWFIXED,
        0, 0, 0, 0, parent, (HMENU)ID_CHATS, inst, NULL);
    check_child("chats", g_chats);

    g_refresh = CreateWindowExA(0, "BUTTON", "Refresh",
        WS_CHILD | WS_VISIBLE | BS_PUSHBUTTON, 0, 0, 0, 0,
        parent, (HMENU)ID_REFRESH, inst, NULL);
    check_child("refresh", g_refresh);

    SendMessageA(g_chats,   WM_SETFONT, (WPARAM)g_font_normal, TRUE);
    SendMessageA(g_refresh, WM_SETFONT, (WPARAM)g_font_normal, TRUE);

    g_chats_oldproc = (WNDPROC)SetWindowLongA(g_chats, GWL_WNDPROC,
                                              (LONG)(LONG_PTR)chats_proc);
}
