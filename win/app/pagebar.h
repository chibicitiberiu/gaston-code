/* The page navigation bar: Live / First / Prev / Next / Last and the page
 * label, across the top of the transcript column.
 *
 * Pagination state (which page is shown, how many exist, whether the shown
 * one is the live one, and whether a page is mid-load) moves here with the
 * controls that display it and the two frame-sending functions that change
 * it. The control holds exactly ONE page, which is what bounds memory by
 * page size rather than by conversation length -- the whole reason a 32MB
 * box can run this. view == total means LIVE: new text appends as it
 * streams. On any earlier page nothing appends at all, which also removes
 * the append-while-scrolled-up case that RichEdit handles worst.
 *
 * One of the three remaining §1.2 extraction targets out of main.c (audit
 * finding 01.58) -- the bar and the chat list are already their own files,
 * the same shape as this one: a module plus one dispatch-table row.
 *
 * A CLASS NOW, not a handle plus free functions (audit finding 01.60): same
 * reasoning as app/htmlview.h, the pattern this module and its five
 * siblings follow. There is exactly one instance, created explicitly by
 * main.cpp at window-creation time (never a file-scope object -- see
 * core/rc_cpp.h) and destroyed with `delete`.
 */
#ifndef RC_PAGEBAR_H
#define RC_PAGEBAR_H

#include <windows.h>
#include "net/net.h"
#include "net/frameproto.h"
#include "app/htmlview.h"

#ifndef __cplusplus
#error "pagebar.h is C++; the page bar is a class now, see app/htmlview.h"
#endif

#define ID_LIVE    1006
#define ID_FIRST   1007
#define ID_PREV    1008
#define ID_NEXT    1009
#define ID_LAST    1010
#define ID_PAGELBL 1011

/* The bar's fixed height, and the geometry main.c's layout() needs to know
 * before asking this module to position its own controls -- same
 * relationship as app/bar.h's PERM_H. PGBTN_W (button width) is this
 * module's own business and stays in pagebar.cpp. */
#define PAGEBAR_H 24

class rc_pagebar {
    int view, total;       /* page currently displayed; pages that exist */
    int is_live;
    int loading_page;

    HWND    live, first, prev, next, last, pagelbl;
    rc_net *net;
    rc_html *html;

    rc_pagebar(const rc_pagebar &);
    rc_pagebar &operator=(const rc_pagebar &);
    rc_pagebar(HWND parent, HINSTANCE inst, HFONT font, rc_net *net_,
               rc_html *html_);

    void update_ui();
    void goto_page(const char *which);
    void goto_n(int n);

public:
    /* net: goto_page's rc_net_send, made directly by this module -- same as
     *      app/bar.h's `net` parameter, for the same reason (see the comment
     *      on that call in bar.cpp): a page request the user just asked for
     *      either goes out or is worth reporting, and there is no larger
     *      "what the user asked for" wrapper this needs beyond the raw send.
     * html: PAGESTART/PAGEEND clear, notice and scroll the transcript --
     *      that is what those two frames mean, not a policy question to hand
     *      back to main.c the way chatlist.h's confirm/switching/switched
     *      are. May be NULL (the view failed to create); every call is
     *      guarded, exactly as main.c already guards every other g_html
     *      use. */
    static rc_pagebar *create(HWND parent, HINSTANCE inst, HFONT font,
                               rc_net *net, rc_html *html);
    ~rc_pagebar() {}

    /* x: the strip's left edge (where Live starts). y: the strip's top.
     * Unlike the bar, the page strip is never hidden, so there is no
     * active() to gate this on. */
    void move(int x, int y);

    /* PAGESTART, PAGEEND, PAGESEALED -- the three frame types this module
     * owns. Returns non-zero if `f->type` was one of those. Resetting
     * main.c's own turn-state enum on PAGESTART, and log_mem on PAGEEND, are
     * not this module's business and stay in main.c's own wrapper around
     * this call -- the same split on_chats already makes around
     * rc_chats_frame. */
    int frame(rc_frame *f);

    /* WM_COMMAND for ID_LIVE/FIRST/PREV/NEXT/LAST. Returns non-zero if `id`
     * was one of those. */
    int command(int id);

    /* READY and SWITCHED both change what this module shows, but neither is
     * one of its own three frame types -- same shape as chatlist.h's
     * `switched` callback handing main.c the page total back (audit finding
     * 01.11): main.c owns the frame, this module owns the state the frame
     * updates.
     *
     * note_ready: READY's page/total (each updated only if the header is
     * present) and marks the session live, then refreshes the buttons --
     * everything on_ready's four lines used to do to these globals directly.
     * note_total: SWITCHED's total only, exactly as chats_switched did -- no
     * button refresh, matching that call site's existing behaviour
     * precisely.
     * goto_live: on_ready's trailing page_goto("live"). */
    void note_ready(rc_frame *f);
    void note_total(rc_frame *f);
    void goto_live();
};

#endif
