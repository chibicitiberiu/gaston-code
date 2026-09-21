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

void rc_pagebar::goto_page(const char *which)
{
    const char *keys[1]; const char *vals[1];
    keys[0] = "page"; vals[0] = which;
    /* THROUGH ui_send, like the bar and the chat list.
     *
     * This was a bare rc_net_send with its return discarded -- the only
     * user-initiated send in the tree that swallowed failure. main.cpp states
     * the rule where ui_send is defined: "Ignoring that turns a button press
     * into silence: the press looks accepted, nothing happens, and there is
     * nothing on screen to explain it." Pressing Next on a link that had just
     * dropped did exactly nothing, with no message and no log line. */
    if (send) send("that page", "GETPAGE", keys, vals, 1, NULL, 0);
    else rc_net_send(net, "GETPAGE", keys, vals, 1, NULL, 0);
}

/* A page count off the wire. Clamped where it is PARSED, once, rather than
 * at each of the six places that used it: atoi of an absent or non-numeric
 * header is 0 and atoi("-5") is -5, and this is the only integer in the UI
 * that drives arithmetic straight off the wire. A tree always has at least
 * one page -- the live one. (audit 01.37) */
static int wire_total(const char *v, int fallback)
{
    int n;
    if (!v) return fallback;
    n = atoi(v);
    return n >= 1 ? n : 1;
}

/* The same, for a page NUMBER: at least 1, and never past the total. */
static int wire_page(const char *v, int total, int fallback)
{
    int n;
    if (!v) return fallback;
    n = atoi(v);
    if (n > total) n = total;
    return n >= 1 ? n : 1;
}

void rc_pagebar::goto_n(int n)
{
    char buf[16];
    /* CLAMP HIGH FIRST, THEN LOW. The other order lets a bad `total` win:
     * `total` is atoi of a wire header, and atoi of an absent or
     * non-numeric value is 0 while atoi("-5") is -5 -- so with total == 0,
     * goto_n(1) clamped to n = 0 and put `GETPAGE page=0` on the wire.
     * set_total() below refuses a total under 1 at the point it is parsed,
     * which is the real fix; this order means a future parser bug cannot
     * reach the wire either. */
    if (n > total) n = total;
    if (n < 1) n = 1;
    rc_strf(buf, sizeof(buf), "%d", n);
    goto_page(buf);
}

rc_pagebar::rc_pagebar(HWND parent, HINSTANCE inst, HFONT font, rc_net *net_,
                        rc_html *html_, rc_pagebar_send_fn send_)
    : view(1), total(1), is_live(1), loading_page(0), net(net_), html(html_),
      send(send_), follow_bmp(NULL)
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
            follow_bmp = bmp;
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
                                rc_pagebar_send_fn send)
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
    if (strcmp(f->type, "PAGESTART") == 0) {
        const char *v;
        v = rc_frame_get(f, "total"); total = wire_total(v, total);
        v = rc_frame_get(f, "page");  view  = wire_page(v, total, view);
        v = rc_frame_get(f, "live");  is_live = (v && *v == '1');
        loading_page = 1;
        /* One page in, one page out: clear before rendering, so the control
         * never holds two. */
        if (html) html->clear();
        v = rc_frame_get(f, "continued");
        if (v && *v == '1' && html)
            html->notice("continued from the previous page");
        update_ui();
        return 1;
    }
    if (strcmp(f->type, "PAGEEND") == 0) {
        if (loading_page) {
            loading_page = 0;
            if (html) html->scroll_bottom();
        }
        update_ui();
        return 1;
    }
    if (strcmp(f->type, "PAGESEALED") == 0) {
        const char *v = rc_frame_get(f, "total"); total = wire_total(v, total);
        /* On the live page, follow the seal forward. On an older page, the
         * counter simply ticks up -- that IS the new-content notification. */
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
     * Holding Next on a slow link queued N GETPAGEs, and every PAGESTART that
     * came back called html->clear() -- N page loads of bandwidth on a wire
     * that may be 5 KB/s, and a transcript that flickered through each one to
     * land on the last. The bar already tracks loading_page for its own UI;
     * it simply was not consulted here. Live is exempt: it is the way back if
     * a page request is lost, so it must never be gated behind one. */
    if (loading_page && id != ID_LIVE) return 1;
    switch (id) {
    case ID_LIVE:  goto_page("live");   return 1;
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
    const char *v;
    v = rc_frame_get(f, "total"); total = wire_total(v, total);
    v = rc_frame_get(f, "page");  view  = wire_page(v, total, view);
    is_live = 1;
    update_ui();
}

void rc_pagebar::note_total(rc_frame *f)
{
    const char *v = rc_frame_get(f, "total");
    total = wire_total(v, total);
}

void rc_pagebar::goto_live()
{
    goto_page("live");
}
