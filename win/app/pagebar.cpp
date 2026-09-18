/* The page navigation bar. See app/pagebar.h for the design. */
#include "app/pagebar.h"
#include "core/str.h"
#include "core/log.h"
#include <stdlib.h>
#include <string.h>

#define PGBTN_W 28

/* Pagination state.
 *
 * The control holds exactly ONE page, which is what bounds memory by page size
 * rather than by conversation length -- the whole reason a 32MB box can run
 * this. g_view == g_total means LIVE: new text appends as it streams. On any
 * earlier page nothing appends at all, which also removes the
 * append-while-scrolled-up case that RichEdit handles worst. */
static int g_view = 1;       /* page currently displayed */
static int g_total = 1;      /* pages that exist */
static int g_is_live = 1;
static int g_loading_page = 0;

static HWND     g_live, g_first, g_prev, g_next, g_last, g_pagelbl;
static rc_net  *g_net;
static rc_html *g_html;

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

static void pagebar_update_ui(void)
{
    char lbl[64];
    rc_strf(lbl, sizeof(lbl), "Page %d/%d%s",
              g_view, g_total, g_is_live ? " (Live)" : "");
    SetWindowTextA(g_pagelbl, lbl);

    EnableWindow(g_first, g_view > 1);
    EnableWindow(g_prev,  g_view > 1);
    EnableWindow(g_next,  g_view < g_total);
    EnableWindow(g_last,  g_view < g_total);
    EnableWindow(g_live,  !g_is_live);
}

static void pagebar_goto(const char *which)
{
    const char *keys[1]; const char *vals[1];
    keys[0] = "page"; vals[0] = which;
    rc_net_send(g_net, "GETPAGE", keys, vals, 1, NULL, 0);
}

static void pagebar_goto_n(int n)
{
    char buf[16];
    if (n < 1) n = 1;
    if (n > g_total) n = g_total;
    rc_strf(buf, sizeof(buf), "%d", n);
    pagebar_goto(buf);
}

void rc_pagebar_create(HWND parent, HINSTANCE inst, HFONT font, rc_net *net,
                        rc_html *html)
{
    g_net = net;
    g_html = html;

    g_live = CreateWindowExA(0, "BUTTON", "Live",
        WS_CHILD | WS_VISIBLE | BS_PUSHBUTTON, 0, 0, 0, 0,
        parent, (HMENU)ID_LIVE, inst, NULL);
    check_child("live", g_live);
    g_first = CreateWindowExA(0, "BUTTON", "|<",
        WS_CHILD | WS_VISIBLE | BS_PUSHBUTTON, 0, 0, 0, 0,
        parent, (HMENU)ID_FIRST, inst, NULL);
    check_child("first", g_first);
    g_prev = CreateWindowExA(0, "BUTTON", "<",
        WS_CHILD | WS_VISIBLE | BS_PUSHBUTTON, 0, 0, 0, 0,
        parent, (HMENU)ID_PREV, inst, NULL);
    check_child("prev", g_prev);
    g_pagelbl = CreateWindowExA(0, "STATIC", "Page 1/1",
        WS_CHILD | WS_VISIBLE | SS_CENTER, 0, 0, 0, 0,
        parent, (HMENU)ID_PAGELBL, inst, NULL);
    check_child("pagelbl", g_pagelbl);
    g_next = CreateWindowExA(0, "BUTTON", ">",
        WS_CHILD | WS_VISIBLE | BS_PUSHBUTTON, 0, 0, 0, 0,
        parent, (HMENU)ID_NEXT, inst, NULL);
    check_child("next", g_next);
    g_last = CreateWindowExA(0, "BUTTON", ">|",
        WS_CHILD | WS_VISIBLE | BS_PUSHBUTTON, 0, 0, 0, 0,
        parent, (HMENU)ID_LAST, inst, NULL);
    check_child("last", g_last);

    SendMessageA(g_live,    WM_SETFONT, (WPARAM)font, TRUE);
    SendMessageA(g_first,   WM_SETFONT, (WPARAM)font, TRUE);
    SendMessageA(g_prev,    WM_SETFONT, (WPARAM)font, TRUE);
    SendMessageA(g_next,    WM_SETFONT, (WPARAM)font, TRUE);
    SendMessageA(g_last,    WM_SETFONT, (WPARAM)font, TRUE);
    SendMessageA(g_pagelbl, WM_SETFONT, (WPARAM)font, TRUE);

    /* The initial enabled state -- create_children used to do this itself
     * with one page_update_ui() call after every child existed. */
    pagebar_update_ui();
}

/* Right-to-left is not needed here, unlike the permission bar: none of these
 * six controls are ever hidden, so the strip is always the same width and
 * can be laid out left-to-right once. */
void rc_pagebar_move(int x, int y)
{
    /* The "4" is main.c's GUTTER, which this module cannot see by name (it
     * is a plain #define in main.cpp, not exported by any header) -- same
     * value, spelled out, exactly as bar.cpp's rc_bar_move already does for
     * the same constant. */
    MoveWindow(g_live,  x, y, 40, PAGEBAR_H - 2, TRUE); x += 40 + 4;
    MoveWindow(g_first, x, y, PGBTN_W, PAGEBAR_H - 2, TRUE); x += PGBTN_W;
    MoveWindow(g_prev,  x, y, PGBTN_W, PAGEBAR_H - 2, TRUE); x += PGBTN_W;
    MoveWindow(g_pagelbl, x, y + 4, 130, PAGEBAR_H - 4, TRUE); x += 130;
    MoveWindow(g_next,  x, y, PGBTN_W, PAGEBAR_H - 2, TRUE); x += PGBTN_W;
    MoveWindow(g_last,  x, y, PGBTN_W, PAGEBAR_H - 2, TRUE);
}

int rc_pagebar_frame(rc_frame *f)
{
    if (strcmp(f->type, "PAGESTART") == 0) {
        const char *v;
        v = rc_frame_get(f, "page");  if (v) g_view = atoi(v);
        v = rc_frame_get(f, "total"); if (v) g_total = atoi(v);
        v = rc_frame_get(f, "live");  g_is_live = (v && *v == '1');
        g_loading_page = 1;
        /* One page in, one page out: clear before rendering, so the control
         * never holds two. */
        if (g_html) g_html->clear();
        v = rc_frame_get(f, "continued");
        if (v && *v == '1' && g_html)
            g_html->notice("continued from the previous page");
        pagebar_update_ui();
        return 1;
    }
    if (strcmp(f->type, "PAGEEND") == 0) {
        if (g_loading_page) {
            g_loading_page = 0;
            if (g_html) g_html->scroll_bottom();
        }
        pagebar_update_ui();
        return 1;
    }
    if (strcmp(f->type, "PAGESEALED") == 0) {
        const char *v = rc_frame_get(f, "total"); if (v) g_total = atoi(v);
        /* On the live page, follow the seal forward. On an older page, the
         * counter simply ticks up -- that IS the new-content notification. */
        if (g_is_live) g_view = g_total;
        pagebar_update_ui();
        return 1;
    }
    return 0;
}

int rc_pagebar_command(int id)
{
    switch (id) {
    case ID_LIVE:  pagebar_goto("live");        return 1;
    case ID_FIRST: pagebar_goto_n(1);           return 1;
    case ID_PREV:  pagebar_goto_n(g_view - 1);  return 1;
    case ID_NEXT:  pagebar_goto_n(g_view + 1);  return 1;
    case ID_LAST:  pagebar_goto_n(g_total);     return 1;
    default:       return 0;
    }
}

void rc_pagebar_note_ready(rc_frame *f)
{
    const char *v;
    v = rc_frame_get(f, "page");  if (v) g_view = atoi(v);
    v = rc_frame_get(f, "total"); if (v) g_total = atoi(v);
    g_is_live = 1;
    pagebar_update_ui();
}

void rc_pagebar_note_total(rc_frame *f)
{
    const char *v = rc_frame_get(f, "total");
    if (v) g_total = atoi(v);
}

void rc_pagebar_goto_live(void)
{
    pagebar_goto("live");
}
