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
#include "core/rc_cpp.h"
#include "app/sender.h"

#ifndef __cplusplus
#error "pagebar.h is C++; the page bar is a class now, see app/htmlview.h"
#endif

#define ID_LIVE    1006
#define ID_FIRST   1007
#define ID_PREV    1008
#define ID_NEXT    1009
#define ID_LAST    1010
#define ID_PAGELBL 1011
/* The autoscroll toggle. It lives on this strip because that is where it
 * belongs on screen -- "follow the end of the document" sits next to "which
 * page am I on" -- not because paging and scrolling are the same subject.
 * app/pagebar.cpp's constructor says so again beside the button. */
/* 1018, not 1012: the page-bar block ends at ID_PAGELBL and 1012 is
 * ID_CHATS. Watcom caught it as a duplicate case; a compiler that did not
 * would have routed the chat list's notifications to the toggle. */
#define ID_FOLLOW  1018

/* The bar's fixed height, and the geometry main.c's layout() needs to know
 * before asking this module to position its own controls -- same
 * relationship as app/bar.h's PERM_H. PGBTN_W (button width) is this
 * module's own business and stays in pagebar.cpp. */
#define PAGEBAR_H 24

class rc_pagebar {
    int view, total;       /* page currently displayed; pages that exist */
    /* How many RECORDS the transcript holds, which is what the wire carries
     * now. `total` is derived from it and this machine's own window size,
     * because stage 3b made the page the client's idea: the bridge counts
     * blocks and has no opinion about where one page ends. */
    int blocks;
    int is_live;
    int loading_page;

    HWND    live, first, prev, next, last, pagelbl, follow;
    rc_net *net;
    rc_html *html;
    FrameSender *sender;
    /* The autoscroll button's face. BM_SETIMAGE does not take ownership, so
     * this class does -- see the destructor. */
    GdiObj follow_bmp;

    rc_pagebar(const rc_pagebar &);
    rc_pagebar &operator=(const rc_pagebar &);
    rc_pagebar(HWND parent, HINSTANCE inst, HFONT font, rc_net *net_,
               rc_html *html_, FrameSender *send_);

    void update_ui();
    /* The window this machine asks for, and the page count it implies. */
    int  per_page() const;
    int  pages() const;
    void goto_offset(const char *which);
    void goto_n(int n);

public:
    /* net: kept for the fallback path only. This module used to send GETBLOCKS
     *      through it directly, with the return discarded, on the reasoning
     *      that "there is no larger 'what the user asked for' wrapper this
     *      needs beyond the raw send." That was wrong on its own terms --
     *      app/bar.h has taken a FrameSender (app/sender.h) for exactly this since it was
     *      written -- and it made the page strip the one user-initiated send
     *      in the tree that failed in silence.
     * send: main.cpp's ui_send. A page request either goes out or the user is
     *      told why not.
     * html: RANGE/RANGEEND clear and scroll the transcript --
     *      that is what those two frames mean, not a policy question to hand
     *      back to main.c the way chatlist.h's confirm/switching/switched
     *      are. May be NULL (the view failed to create); every call is
     *      guarded, exactly as main.c already guards every other g_html
     *      use. */
    static rc_pagebar *create(HWND parent, HINSTANCE inst, HFONT font,
                               rc_net *net, rc_html *html,
                               FrameSender *send);
    ~rc_pagebar() {}

    /* x: the strip's left edge (where Live starts). y: the strip's top.
     * Unlike the bar, the page strip is never hidden, so there is no
     * active() to gate this on. */
    void move(int x, int y);

    /* RANGE, RANGEEND, GREW -- the three frame types this module owns. Returns non-zero if `f->type` was one of those. Resetting
     * main.c's own turn-state enum on RANGE, and log_mem on RANGEEND, are
     * Resetting main.c's own turn-state enum on RANGE, and log_mem on
     * RANGEEND, are not this module's business and stay in its wrapper around
     * this call -- the same split on_chats already makes around
     * ChatList::frame. */
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
     * note_ready: marks the session live and refreshes the buttons. READY
     * stopped carrying a position at stage 3b -- the RANGE that goto_live()
     * asks for right after is where the numbers come from, and a position
     * here would be a second source for one fact, stale by the time it was
     * read. SWITCHED lost its total for the same reason.
     * note_total: the record count off an APPEND TEXT frame, which is where
     * it travels for a reader following live -- a frame was going out
     * anyway, so a GREW of its own would be a second frame per block on a
     * link measured in single digits of KB/s. A frame without the header
     * leaves the count alone.
     * goto_live: on_ready's trailing page_goto("live"), and the same ask
     * after a real conversation switch. */
    /* The autoscroll toggle's state, pushed in at creation and read back
     * when it is clicked. Kept here rather than in main.cpp's config so the
     * button and the setting cannot disagree about which is true. */
    void set_follow(int on);
    int  follow_on() const;

    void note_ready(rc_frame *f);
    void note_total(rc_frame *f);
    void goto_live();

    /* Which page is on screen, for the cache's filename. The cache keys a
     * file on the page NUMBER and always did; stage 3b only changed who
     * works that number out, so asking the bar for it keeps one answer
     * rather than two that can disagree. */
    int  page() const { return view; }
};

#endif
