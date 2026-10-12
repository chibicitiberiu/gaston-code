/* The page navigation bar. See app/pagebar.h for the design. */
#include "app/pagebar.h"
#include "app/resource.h"
#include "core/str.h"
#include "core/log.h"
#include "core/rc_cpp.h"
#include <stdlib.h>
#include <string.h>

#define PGBTN_W 28

/* CreateWindowExA can fail, and on this platform that was never a
 * theoretical concern: the 16-bit USER/GDI heaps on 9x are a fixed 64KB that
 * does not scale with RAM -- see main.cpp's own check_child comment, the
 * fifth citation of this across main.cpp/chatlist.cpp/bar.cpp/pagebar.cpp.
 * Each module that creates controls keeps its own copy of this rather than
 * sharing one across files that do not otherwise depend on each other. */
static void check_child(const char *what, HWND h)
{
    if (!h)
        rc_log("pagebar: %s failed to create (GetLastError=%lu)",
               what, (unsigned long)GetLastError());
}

void rc_pagebar::update_ui()
{
    char lbl[64];
    rc_strf(lbl, sizeof(lbl), "Page %d/%d%s",
              view, total, is_live ? " (Live)" : "");
    SetWindowTextA(pagelbl, lbl);

    EnableWindow(first, view > 1);
    EnableWindow(prev,  view > 1);
    EnableWindow(next,  view < total);
    EnableWindow(last,  view < total);
    EnableWindow(live,  !is_live);
}

/* How many records go on one page of this machine. The net layer computed
 * it at the handshake from this machine's memory and the user's page-size
 * setting, and declared it; asking it back is what keeps the number the
 * client asks for and the number the client declared the same number. */
int rc_pagebar::per_page() const
{
    unsigned n = rc_net_page_blocks(net);
    return n ? (int)n : 90;
}

void rc_pagebar::goto_offset(const char *which)
{
    const char *keys[2]; const char *vals[2];
    char lim[16];
    rc_strf(lim, sizeof(lim), "%d", per_page());
    keys[0] = "offset"; vals[0] = which;
    keys[1] = "limit";  vals[1] = lim;
    /* THROUGH ui_send, like the bar and the chat list.
     *
     * This was a bare rc_net_send with its return discarded -- the only
     * user-initiated send in the tree that swallowed failure. main.cpp states
     * the rule where ui_send is defined: "Ignoring that turns a button press
     * into silence: the press looks accepted, nothing happens, and there is
     * nothing on screen to explain it." Pressing Next on a link that had just
     * dropped did exactly nothing, with no message and no log line. */
    if (sender) sender->sendFrame("that page", "GETBLOCKS", keys, vals, 2,
                                  NULL, 0);
    else rc_net_send(net, "GETBLOCKS", keys, vals, 2, NULL, 0);
}

/* A BLOCK COUNT off the wire. Clamped where it is PARSED, once, rather
 * than at each of the places that use it: atoi of an absent or non-numeric
 * header is 0 and atoi("-5") is -5, and these are the only integers in the
 * UI that drive arithmetic straight off the wire. (audit 01.37)
 *
 * Zero is LEGAL here where a page count could not be: an empty conversation
 * has no blocks at all, and before stage 3b that was a transcript of one
 * empty page. The floor is on the PAGE count below, which is still at least
 * one, because the live page exists whether or not anything is on it. */
static int wire_count(const char *v, int fallback)
{
    int n;
    if (!v) return fallback;
    n = atoi(v);
    return n >= 0 ? n : 0;
}

/* The page count this machine's own window size implies. At least one. */
int rc_pagebar::pages() const
{
    int per = per_page();
    int n = (blocks + per - 1) / per;
    return n >= 1 ? n : 1;
}

void rc_pagebar::goto_n(int n)
{
    char buf[16];
    /* CLAMP HIGH FIRST, THEN LOW, which is a rule this file learned the
     * hard way: with a total of 0, goto_n(1) clamped the other way round
     * put `page=0` on the wire. The arithmetic is the client's now and
     * `pages()` cannot return less than 1, so the order is belt and braces
     * -- kept, because the cost is one comparison and the thing it prevents
     * reached the wire once. */
    int top = pages();
    if (n > top) n = top;
    if (n < 1) n = 1;
    rc_strf(buf, sizeof(buf), "%d", (n - 1) * per_page());
    goto_offset(buf);
}

rc_pagebar::rc_pagebar(HWND parent, HINSTANCE inst, HFONT font, rc_net *net_,
                        rc_html *html_, FrameSender *send_)
    : view(1), total(1), blocks(0), is_live(1), loading_page(0), net(net_), html(html_),
      sender(send_), follow_bmp(NULL)
{
    /* Deliberately NO WS_TABSTOP on any of these six.
     *
     * A draft of the keyboard work added one to each. Tab order follows
     * CREATION order, and the page bar is created before the input box -- so
     * five extra stops would sit between the chat list and the place you
     * type, on every Tab, for everyone. The page bar also now has a keyboard
     * route that is better than tabbing: Ctrl+PgUp/PgDn (see gaston.rc), and
     * Ctrl+Home/End for the ends of the current page.
     *
     * What that leaves genuinely mouse-only is Live, |< and >|. If those
     * want a keyboard route they want accelerators of their own, not a
     * position in a cycle nobody wants to walk. */
    live = CreateWindowExA(0, "BUTTON", "Live",
        WS_CHILD | WS_VISIBLE | BS_PUSHBUTTON, 0, 0, 0, 0,
        parent, (HMENU)ID_LIVE, inst, NULL);
    check_child("live", live);
    first = CreateWindowExA(0, "BUTTON", "|<",
        WS_CHILD | WS_VISIBLE | BS_PUSHBUTTON, 0, 0, 0, 0,
        parent, (HMENU)ID_FIRST, inst, NULL);
    check_child("first", first);
    prev = CreateWindowExA(0, "BUTTON", "<",
        WS_CHILD | WS_VISIBLE | BS_PUSHBUTTON, 0, 0, 0, 0,
        parent, (HMENU)ID_PREV, inst, NULL);
    check_child("prev", prev);
    pagelbl = CreateWindowExA(0, "STATIC", "Page 1/1",
        WS_CHILD | WS_VISIBLE | SS_CENTER, 0, 0, 0, 0,
        parent, (HMENU)ID_PAGELBL, inst, NULL);
    check_child("pagelbl", pagelbl);
    next = CreateWindowExA(0, "BUTTON", ">",
        WS_CHILD | WS_VISIBLE | BS_PUSHBUTTON, 0, 0, 0, 0,
        parent, (HMENU)ID_NEXT, inst, NULL);
    check_child("next", next);
    /* The autoscroll toggle, at the end of the strip.
     *
     * HERE because this is where it belongs on screen -- "follow the end of
     * the document" next to "which page am I on" -- and because it was asked
     * for next to the page controls. Paging and scrolling are not the same
     * subject and this is the only thing on the strip that is not paging.
     *
     * BS_AUTOCHECKBOX | BS_PUSHLIKE: a button that stays pressed, which is
     * how Windows 98 spells a toggle without an owner-draw. BS_BITMAP because
     * the face is a drawn arrow -- comctl32 has no arrow that points down,
     * only the back/forward chevrons, which read as navigation. */
    follow = CreateWindowExA(0, "BUTTON", "",
        WS_CHILD | WS_VISIBLE | BS_AUTOCHECKBOX | BS_PUSHLIKE | BS_BITMAP,
        0, 0, 0, 0, parent, (HMENU)ID_FOLLOW, inst, NULL);
    check_child("follow", follow);
    if (follow) {
        /* KEPT, so the destructor can free it. BM_SETIMAGE does NOT take
         * ownership, and without LR_SHARED this is a fresh GDI object every
         * time -- one handle off a fixed 64KB USER/GDI heap, leaked for the
         * life of the process. Small, but this is the heap the rest of the
         * codebase is careful about. (audit 01.37) */
        HBITMAP bmp = (HBITMAP)LoadImageA(inst, MAKEINTRESOURCE(IDB_PGDN),
                                          IMAGE_BITMAP, 0, 0,
                                          LR_DEFAULTCOLOR);
        if (bmp) {
            follow_bmp.reset(bmp);
            SendMessageA(follow, BM_SETIMAGE, IMAGE_BITMAP, (LPARAM)bmp);
        }
        else
            rc_log("pagebar: IDB_PGDN did not load; the autoscroll button "
                   "will be blank");
    }
    last = CreateWindowExA(0, "BUTTON", ">|",
        WS_CHILD | WS_VISIBLE | BS_PUSHBUTTON, 0, 0, 0, 0,
        parent, (HMENU)ID_LAST, inst, NULL);
    check_child("last", last);

    SendMessageA(live,    WM_SETFONT, (WPARAM)font, TRUE);
    SendMessageA(first,   WM_SETFONT, (WPARAM)font, TRUE);
    SendMessageA(prev,    WM_SETFONT, (WPARAM)font, TRUE);
    SendMessageA(next,    WM_SETFONT, (WPARAM)font, TRUE);
    SendMessageA(last,    WM_SETFONT, (WPARAM)font, TRUE);
    SendMessageA(pagelbl, WM_SETFONT, (WPARAM)font, TRUE);

    /* The initial enabled state -- create_children used to do this itself
     * with one page_update_ui() call after every child existed. */
    update_ui();
}

rc_pagebar *rc_pagebar::create(HWND parent, HINSTANCE inst, HFONT font,
                                rc_net *net, rc_html *html,
                                FrameSender *send)
{
    return new rc_pagebar(parent, inst, font, net, html, send);
}

/* Right-to-left is not needed here, unlike the permission bar: none of these
 * six controls are ever hidden, so the strip is always the same width and
 * can be laid out left-to-right once. */
void rc_pagebar::move(int x, int y)
{
    /* The "4" is main.c's GUTTER, which this module cannot see by name (it
     * is a plain #define in main.cpp, not exported by any header) -- same
     * value, spelled out, exactly as bar.cpp's rc_bar_move already does for
     * the same constant. */
    MoveWindow(live,  x, y, 40, PAGEBAR_H - 2, TRUE); x += 40 + 4;
    MoveWindow(first, x, y, PGBTN_W, PAGEBAR_H - 2, TRUE); x += PGBTN_W;
    MoveWindow(prev,  x, y, PGBTN_W, PAGEBAR_H - 2, TRUE); x += PGBTN_W;
    MoveWindow(pagelbl, x, y + 4, 130, PAGEBAR_H - 4, TRUE); x += 130;
    MoveWindow(next,  x, y, PGBTN_W, PAGEBAR_H - 2, TRUE); x += PGBTN_W;
    MoveWindow(last,  x, y, PGBTN_W, PAGEBAR_H - 2, TRUE); x += PGBTN_W + 8;
    /* A gap before it: it is not a page button and should not read as the
     * next one along. */
    MoveWindow(follow, x, y, PGBTN_W, PAGEBAR_H - 2, TRUE);
}

int rc_pagebar::frame(rc_frame *f)
{
    if (strcmp(f->type, "RANGE") == 0) {
        const char *v;
        int per = per_page();
        v = rc_frame_get(f, "total");  blocks = wire_count(v, blocks);
        v = rc_frame_get(f, "live");   is_live = (v && *v == '1');
        /* WHERE IT LANDED, not where it was asked for. `offset=live` is
         * resolved by the bridge against the grid this client's own limit
         * implies, and the transcript can have grown between the ask and
         * the answer -- so the page number is derived from what came back.
         * The division is exact for every window this client asks for,
         * because every one of them starts at a multiple of `per`. */
        v = rc_frame_get(f, "offset");
        view = (v ? wire_count(v, 0) : 0) / (per > 0 ? per : 1) + 1;
        total = pages();
        if (view > total) view = total;
        /* `count` IS READ, and this is what it is for. A window is short
         * for two reasons that look identical on screen: it is the last one
         * and the transcript ends there, or the bridge refused a record out
         * of the middle of it (wire/link.py's oversize branch, which logs
         * and drops). A short window that is NOT the last page is the
         * second case, and saying so in the log is the difference between a
         * missing block somebody can chase and one they cannot.
         *
         * Without a reader this header would be exactly the decoration this
         * stage deleted from TEXT. */
        v = rc_frame_get(f, "count");
        if (v) {
            int got = wire_count(v, per);
            if (got < per && view < total)
                rc_log("page: window %d of %d came back short -- %d blocks "
                       "of %d; the bridge refused one", view, total, got,
                       per);
        }
        loading_page = 1;
        /* One window in, one window out: clear before rendering, so the
         * control never holds two. */
        if (html) html->clear();
        update_ui();
        return 1;
    }
    if (strcmp(f->type, "RANGEEND") == 0) {
        if (loading_page) {
            loading_page = 0;
            if (html) html->scroll_bottom();
        }
        update_ui();
        return 1;
    }
    if (strcmp(f->type, "GREW") == 0) {
        const char *v = rc_frame_get(f, "total");
        blocks = wire_count(v, blocks);
        total = pages();
        /* Following the end, stay at the end: a block that starts a new
         * page moves the reader onto it, which is what a transcript that
         * is being written under you should do. Paged back, the count
         * simply ticks up -- that IS the new-content notification, and it
         * is the whole reason this frame exists. */
        if (is_live) view = total;
        update_ui();
        return 1;
    }
    return 0;
}

int rc_pagebar::command(int id)
{
    /* ONE PAGE IN FLIGHT AT A TIME.
     *
     * Holding Next on a slow link queued N requests, and every RANGE that
     * came back called html->clear() -- N page loads of bandwidth on a wire
     * that may be 5 KB/s, and a transcript that flickered through each one to
     * land on the last. The bar already tracks loading_page for its own UI;
     * it simply was not consulted here. Live is exempt: it is the way back if
     * a page request is lost, so it must never be gated behind one. */
    if (loading_page && id != ID_LIVE) return 1;
    switch (id) {
    case ID_LIVE:  goto_offset("live"); return 1;
    case ID_FIRST: goto_n(1);           return 1;
    case ID_PREV:  goto_n(view - 1);    return 1;
    case ID_NEXT:  goto_n(view + 1);    return 1;
    case ID_LAST:  goto_n(total);       return 1;
    default:       return 0;
    }
}

void rc_pagebar::set_follow(int on)
{
    if (follow) SendMessageA(follow, BM_SETCHECK,
                             on ? BST_CHECKED : BST_UNCHECKED, 0);
}

int rc_pagebar::follow_on() const
{
    if (!follow) return 1;
    return SendMessageA(follow, BM_GETCHECK, 0, 0) == BST_CHECKED;
}

void rc_pagebar::note_ready(rc_frame *f)
{
    /* READY stopped carrying a position at stage 3b. It is still where the
     * bar learns it is back on the live end, and the RANGE that goto_live()
     * asks for immediately after supplies the numbers. */
    (void)f;
    is_live = 1;
    update_ui();
}

void rc_pagebar::note_total(rc_frame *f)
{
    /* ONLY IF THE HEADER IS THERE. Every APPEND that grew the transcript
     * carries it; a LIVE replace does not, because it rewrites the open
     * block and grows nothing, and neither does an APPEND replayed into a
     * window the reader asked for. `wire_count`'s fallback keeps the last
     * known figure in both of those cases. */
    const char *v = rc_frame_get(f, "total");
    if (!v) return;
    blocks = wire_count(v, blocks);
    total = pages();
    /* Following the end, stay at the end -- the same rule GREW applies, and
     * for the same reason: a block that starts a new page moves the reader
     * onto it, because a transcript being written under you should. */
    if (is_live) view = total;
    update_ui();
}

void rc_pagebar::goto_live()
{
    goto_offset("live");
}
