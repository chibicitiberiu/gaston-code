/* retroclaude.exe -- native Windows 98 client for Claude Code.
 *
 * One process, one TCP connection, two threads in Phase 1 (UI + reader).
 * The RPC worker joins in Phase 3.
 *
 * Build: see Makefile. Target is Win98 SE on a Pentium MMX 200 with 32MB.
 */
#include <windows.h>
#include <commctrl.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "net/frameproto.h"
#include "core/ini.h"
#include "core/log.h"
#include "core/str.h"
#include "net/net.h"
#include <ole2.h>
#include "app/htmlview.h"
#include "app/bar.h"
#include "app/chatlist.h"
#include "sync/syncrpc.h"
#include "sync/sync.h"
#include "rpc/rpc.h"
#include "app/xferstat.h"
#include "app/pagebar.h"
#include "app/optmenu.h"
#include "app/input.h"
#include "app/resource.h"

#define ID_STATUS     1005
/* ID_CHATS/ID_REFRESH are declared in app/chatlist.h now,
 * ID_PERM_TEXT/ALLOW/ALWAYS/DENY in app/bar.h, ID_LIVE/FIRST/PREV/NEXT/
 * LAST/PAGELBL in app/pagebar.h, and ID_INPUT/ID_SEND/ID_STOP in
 * app/input.h -- all four next to the module that creates and dispatches
 * them. */

/* Menu. IDM_MODEL0/IDM_EFFORT0/MAX_OPTS are declared in app/optmenu.h now,
 * next to the module that owns that numbering.
 *
 * IDM_NEW and friends moved to app/resource.h, because gaston.rc's
 * ACCELERATORS block names IDM_NEW and wrc cannot see a #define that lives
 * in a .cpp. Duplicating the number in two files is how the two drift. */

#define TIMER_RECONNECT 1
#define TIMER_STATUS    2
/* Navigation is asynchronous, so the document is polled for readiness until it
 * arrives, then the timer stops. */
#define TIMER_HTMLPOLL  3

/* WM_APP-offset messages this file posts to itself. One block, so Phase 6's
 * cross-thread posts have somewhere to go instead of a fourth style turning
 * up (audit finding 01.22). net.h owns WM_APP+1 (WM_RC_FRAME) and WM_APP+2
 * (WM_RC_STATE) -- those are net.c's messages to this window, declared next
 * to the code that posts them, not duplicated here. */
#define WM_RC_CONNECT  (WM_APP + 99)  /* self-post: connect after first paint */
/* WM_RC_OPENCHAT (chatlist.cpp's rc_chats::list_proc -> wnd_proc: open a
 * chat) is declared in app/chatlist.h now, next to the module that posts
 * it. */

#define GUTTER        4
#define CHATS_W      170
/* PERM_H (the permission bar's height, when shown) is declared in app/bar.h;
 * layout() needs it to shrink the transcript, bar.cpp needs it to size its
 * own controls. PERMBTN_W is bar.cpp's own business now. PAGEBAR_H is
 * declared in app/pagebar.h the same way; PGBTN_W is pagebar.cpp's own
 * business. CHAT_INDENT and CHAT_ROW_H moved to app/chatlist.cpp with the
 * drawing code they size. INPUT_HEIGHT (the compose row's height, which
 * layout() needs before asking app/input.cpp to position its own controls)
 * is declared in app/input.h the same way; the button width is input.cpp's
 * own business. */

static rc_config     g_cfg;
static rc_net        g_net;
static rc_html *g_html;
static rc_bar  *g_bar;
static rc_chats *g_chats;
static rc_xferstat *g_xfer;
static rc_input    *g_inputmod;
/* The accelerator table. Loaded once in WinMain; owned by the module, so it
 * is not destroyed (DestroyAcceleratorTable is for CreateAcceleratorTable). */
static HACCEL       g_accel;
static rc_optmenu *g_optmenu;
static rc_pagebar *g_pagebar;
static int      g_doc_sent;     /* the bridge has given us a skeleton */
/* "Connected to bridge." is announced AFTER the page that follows READY, not
 * before it.
 *
 * on_ready used to write the notice and then call goto_live(), which
 * re-requests the live page -- and PAGESTART clears the control. So the one
 * line confirming the connection was wiped by the next thing the same
 * handler did, every time, and a fresh connection said nothing in the
 * transcript at all.
 *
 * On a FIRST connect it was worse and in a different way: the document does
 * not exist yet, so the notice goes into the pre-ready queue, and clear()
 * zeroes that queue too. Announcing after PAGEEND is the one point that is
 * past both. */
static int      g_announce_connected;
static HWND  g_main, g_status;
/* The page bar's own HWNDs and pagination state moved to app/pagebar.cpp --
 * see app/pagebar.h. The permission/halt bar's own HWNDs, id, pending state
 * and text all moved
 * to app/bar.cpp -- see app/bar.h. g_bar is the one instance, created in
 * create_children and destroyed in WM_DESTROY, the same shape as g_html. The
 * design rationale that used to sit here (why one set of controls serves two
 * request kinds, and the four-bugs history of arbitrating them pairwise)
 * moved with it. The chat list's HWNDs
 * and its seven id-keyed globals moved to app/chatlist.cpp the same way --
 * see app/chatlist.h. g_chats is the one instance, created in
 * create_children and destroyed in WM_DESTROY, the same shape as g_html.
 * The input box, Send/Stop and RC_INPUT_MAX (the longest
 * message do_send can compose and send in one go) moved to app/input.cpp --
 * see app/input.h. */

/* The Model and Effort menus (their HMENUs, id-keyed arrays and batch state)
 * moved to app/optmenu.cpp -- see app/optmenu.h. g_optmenu is the one
 * instance, created in build_menu (menu order is this file's business) and
 * destroyed in WM_DESTROY, the same shape as g_html. */

/* Transfer progress (the rx/tx counters and their formatting) moved to
 * app/xferstat.cpp -- see app/xferstat.h. g_xfer is the one instance,
 * created in create_children and destroyed in WM_DESTROY, the same shape as
 * g_html. */

/* Not "Claude Code": that is Anthropic's product name and this is a
 * third-party client for it. Gaston is a French given name of roughly the
 * right vintage for the machine it runs on. */
#define RC_TITLE "Gaston Code"

/* Where the current turn is, for the title bar and status pane.
 *
 * Replaces g_think_mark, a `long` that only ever held -1 or 1 -- a leftover
 * from the RichEdit era when it was a real character offset into the
 * control -- and g_streaming, a boolean spelled backwards. Together the two
 * were encoding this same three-way state ("nothing running" / "waiting on
 * the model" / "text is arriving") as two booleans, one of them redundant
 * with the other's negation. "Thinking" and "receiving" are different things
 * to be waiting on, and saying "thinking" while text is visibly streaming is
 * just wrong -- that distinction is the whole reason this needs three states
 * and not two. */
typedef enum { TURN_IDLE, TURN_THINKING, TURN_STREAMING } rc_turn_state;
static rc_turn_state g_turn_state = TURN_IDLE;
static DWORD g_turn_start_ms = 0;

static void think_show(void);
static void turn_done(void);
static void layout(HWND hwnd);
static void turn_receiving(void);
static int  ui_send(const char *what, const char *type,
                    const char *const *keys, const char *const *vals,
                    int nkv, const void *body, unsigned body_len);

/* Pagination state moved to app/pagebar.cpp -- see app/pagebar.h. g_pagebar
 * is the one instance, created in create_children and destroyed in
 * WM_DESTROY, the same shape as g_html. */
static HFONT g_ui_font;
static HFONT g_ui_font_bold;
static int   g_turn_active = 0;
static int   g_backoff_ms = 1000;
/* Whether this outage has already been announced in the transcript. */
static int   g_retry_noted;
static char  g_exe_dir[MAX_PATH];

/* ------------------------------------------------------------------ utils */

static void exe_dir(char *out, unsigned n)
{
    char path[MAX_PATH];
    char *slash;
    GetModuleFileNameA(NULL, path, sizeof(path));
    slash = strrchr(path, '\\');
    if (slash) *slash = '\0';
    rc_strf(out, n, "%s", path);
    out[n - 1] = '\0';
}

static void status_set(int pane, const char *text)
{
    if (g_status) SendMessageA(g_status, SB_SETTEXTA, (WPARAM)pane, (LPARAM)text);
}

static void status_refresh(void)
{
    char buf[128];
    rc_strf(buf, sizeof(buf), " %s  %s:%d",
              rc_state_name(g_net.state), g_cfg.host, g_cfg.port);
    status_set(0, buf);
    /* app/xferstat.cpp owns the rx/tx counters and this pane's formatting,
     * including the "nothing for 1200ms" reset -- see app/xferstat.h. */
    if (g_xfer) g_xfer->text(buf, sizeof(buf));
    else        buf[0] = '\0';
    status_set(1, buf);

    /* Tick the elapsed seconds while a turn is in flight. A counter that moves
     * is the difference between "working" and "hung", and it costs one
     * SB_SETTEXT a second rather than touching the transcript. */
    if (g_turn_active && g_turn_start_ms) {
        const char *what = g_turn_state == TURN_STREAMING
                               ? "receiving..." : "Claude is thinking...";
        unsigned long secs =
            (unsigned long)((GetTickCount() - g_turn_start_ms) / 1000);
        rc_strf(buf, sizeof(buf), " %s %lus", what, secs);
        status_set(2, buf);

        /* And the same thing at the bottom of the transcript, which is where
         * the eye already is. The status bar is 400 pixels away from the
         * text you are reading; a line under the last paragraph is not.
         *
         * The spinner is a real file beside the document because IE5
         * animates GIFs but has no data: URI support. If it is missing the
         * <img> renders as nothing and the line still reads correctly --
         * which is why the wording never depends on it. */
        if (g_html) {
            char row[256];
            rc_strf(row, sizeof(row),
                    "<div class=\"status\">"
                    "<img src=\"spin.gif\" width=\"16\" height=\"16\" alt=\"\">"
                    "%s %lus</div>", what, secs);
            g_html->status(row);
        }
    }
}

/* Free physical memory, as the system sees it.
 *
 * GlobalMemoryStatus rather than GetProcessMemoryInfo: the latter is psapi,
 * which is NT-only and would not even load here. System-wide free memory
 * measured with and without the app running is what actually answers "does
 * this fit on a 32MB machine", and it counts everything -- heap, the GDI and
 * USER objects, the RichEdit's own storage, riched20 itself -- rather than
 * only what we allocated on purpose. */
static void log_mem(const char *when)
{
    MEMORYSTATUS ms;

    memset(&ms, 0, sizeof(ms));
    ms.dwLength = sizeof(ms);
    GlobalMemoryStatus(&ms);
    rc_log("mem [%s]: %luK free of %luK phys, %luK free of %luK page, load %lu%%",
           when,
           (unsigned long)(ms.dwAvailPhys / 1024),
           (unsigned long)(ms.dwTotalPhys / 1024),
           (unsigned long)(ms.dwAvailPageFile / 1024),
           (unsigned long)(ms.dwTotalPageFile / 1024),
           (unsigned long)ms.dwMemoryLoad);
}

/* Build the menu bar in code rather than from a resource: it is a dozen
 * items, and the two interesting menus are filled in at runtime anyway. */
static void build_menu(HWND hwnd)
{
    HMENU bar = CreateMenu();
    HMENU file = CreatePopupMenu();
    HMENU cfg = CreatePopupMenu();
    HMENU help = CreatePopupMenu();

    AppendMenuA(file, MF_STRING, IDM_NEW, "&New Session\tCtrl+N");
    AppendMenuA(file, MF_SEPARATOR, 0, NULL);
    AppendMenuA(file, MF_STRING, IDM_EXPORT, "&Export as HTML...");
    AppendMenuA(file, MF_SEPARATOR, 0, NULL);
    AppendMenuA(file, MF_STRING, IDM_CLOSE, "&Close");

    AppendMenuA(cfg, MF_STRING, IDM_HOST, "&Server Host...");

    AppendMenuA(help, MF_STRING, IDM_ABOUT, "&About...");

    AppendMenuA(bar, MF_POPUP, (UINT_PTR)file, "&File");
    AppendMenuA(bar, MF_POPUP, (UINT_PTR)cfg, "&Configure");
    /* The Model and Effort popups: created and appended to `bar` by
     * app/optmenu.cpp now -- see app/optmenu.h. Between Configure and Help,
     * so the menu order is unchanged. */
    g_optmenu = rc_optmenu::create(hwnd, bar, ui_send);
    AppendMenuA(bar, MF_POPUP, (UINT_PTR)help, "&Help");
    SetMenu(hwnd, bar);
}

/* app/chatlist.h's rc_chats_confirm_fn: "a switch is about to happen, is a
 * turn running, may I?" -- the chat list module does not own this policy
 * question (see the header comment there). Switching closes the live
 * client, which kills a running request; ask rather than doing it silently
 * -- a click in the list is easy to make by accident and the reply that is
 * lost took real time to produce. */
static int chats_confirm(void)
{
    return !g_turn_active
        || MessageBoxA(g_main,
                       "A request is still running.\n\n"
                       "Opening another conversation will stop it. Continue?",
                       RC_TITLE, MB_YESNO | MB_ICONQUESTION) == IDYES;
}

/* app/chatlist.h's `switching` callback: a switch was confirmed and is about
 * to go out on the wire. Loading a conversation means closing the live
 * client, resuming, and replaying its history, which is not instant. Say so
 * rather than looking frozen. */
static void chats_switching(void)
{
    SetWindowTextA(g_main, RC_TITLE "  -  loading...");
    status_set(2, " loading conversation...");
}

/* Split from ui_set_busy (audit finding 01.14): ui_set_busy used to be both a
 * setter and a refresher -- bar_hide() called ui_set_busy(g_turn_active),
 * i.e. g_turn_active = g_turn_active, purely to get Send's enabled state
 * recomputed. This is the recompute half, callable on its own by anything
 * that changed one of the two inputs without changing the turn flag -- which
 * is exactly what the bar module's `changed` callback needs (see
 * app/bar.h): showing, hiding or relabelling the bar never touches
 * g_turn_active, but always wants Send's enabled state re-checked against
 * blocks_send(). */
static void ui_refresh_enables(void)
{
    /* A pending permission request blocks Send too. That is the one property
     * a modal dialog would have given us: the request cannot be ignored.
     * Without the OR here a TURN end frame arriving mid-prompt would re-enable
     * Send behind our back.
     *
     * Stop stays enabled deliberately. It is the only escape if the CLI itself
     * has wedged, and it is how the cancellation path gets exercised at all --
     * the bridge answers an interrupted prompt with PERMEND reason=cancelled,
     * which clears the bar. */
    if (g_inputmod) g_inputmod->enable_send((g_turn_active || (g_bar && g_bar->blocks_send()))
                          ? 0 : 1);
}

static void ui_set_busy(int busy)
{
    g_turn_active = busy;
    if (g_inputmod) g_inputmod->enable_stop(busy);
    ui_refresh_enables();
}

/* app/input.h's `blocked`/`sent` callbacks: do_send's own guard (checked
 * again because Enter reaches do_send directly, bypassing whatever
 * EnableWindow last set) and what runs after a typed INPUT has actually
 * gone out. */
static int input_blocked(void)
{
    return g_turn_active || (g_bar && g_bar->blocks_send());
}

static void input_sent(void)
{
    status_refresh();
    ui_set_busy(1);
}

/* ------------------------------------------------------- permission bar */

/* The bar's own `changed` callback (see app/bar.h): the bar changes the
 * transcript's height, and the HTML view has no HWND to resize -- move() is
 * the only lever it has, so the whole layout must be re-run rather than just
 * showing the controls. This replaces bar_layout() + ui_set_busy(g_turn_active)
 * being called together, by hand, at every one of the bar's own show/hide
 * sites -- there is now exactly one place that pairing can be forgotten. */
static void bar_changed(void)
{
    if (g_main) layout(g_main);
    ui_refresh_enables();
}

/* Send something the USER just asked for, and say so if it did not go.
 *
 * rc_net_send returns -1 on a closed link, a full queue or an oversized
 * frame. Ignoring that turns a button press into silence: the press looks
 * accepted, nothing happens, and there is nothing on screen to explain it.
 * Three of ~20 call sites checked, and the one seven-line comment explaining
 * why it must be checked sat above the Continue button while the bar's own
 * reply helper (now bar.cpp's bar_reply) -- the same bar, the other three
 * buttons -- did not check at all. */
static int ui_send(const char *what, const char *type,
                   const char *const *keys, const char *const *vals, int nkv,
                   const void *body, unsigned body_len)
{
    if (rc_net_send(&g_net, type, keys, vals, nkv, body, body_len) == 0)
        return 1;
    rc_log("ui: %s (%s) did not reach the bridge", what, type);
    {
        char msg[192];
        rc_strf(msg, sizeof(msg),
                "%s could not be sent -- the connection is down. It will not "
                "have taken effect. Try again once it reconnects.", what);
        if (g_html) g_html->error(msg);
    }
    return 0;
}

/* ------------------------------------------------------------ frame render */

/* render_frame used to be a 340-line if/else chain over 23 frame types --
 * audit finding 01.12. Consequences, in order of severity: (a) it was the
 * reason main.c could not be split, since every module's frame handling was
 * interleaved in one function body; (b) it cost up to 22 strcmp calls per
 * frame on a 200MHz Pentium; (c) a typo'd type string was a silent
 * fall-through to "unhandled frame type", the exact failure mode net.h's
 * rc_rpc_route comment already documents for a missing route, one layer up.
 * Below is a table of {type, handler} pairs instead -- the same shape
 * frameproto.h's own "four lanes" table already uses one layer down. A
 * linear walk of it is no slower than the chain it replaces, and it is one
 * place to read the full list of frame types this binary understands. */
typedef void (*rc_frame_fn)(rc_frame *f);

static void on_ready(rc_frame *f)
{
    const char *v;
    v = rc_frame_get(f, "sess");
    if (v) rc_strf(g_net.sess, sizeof(g_net.sess), "%s", v);
    /* page/total (if present), marking the session live, and the button
     * refresh that follows are app/pagebar.cpp's business now -- see
     * app/pagebar.h's rc_pagebar::note_ready. */
    if (g_pagebar) g_pagebar->note_ready(f);
    if (g_chats) g_chats->request();
    g_backoff_ms = 1000;
    g_retry_noted = 0;
    v = rc_frame_get(f, "model");
    rc_log("ready: sess=%s model=%s", g_net.sess, v ? v : "?");
    g_announce_connected = 1;
    /* READY carries page/total for the session as it stands. goto_live()
     * re-requests the live page explicitly, which clears the control
     * (PAGESTART) and repopulates it from what the bridge now has -- cheap
     * and correct on a fresh session too, where it is a no-op over an empty
     * page.
     *
     * It is also what used to eat the "Connected to bridge." notice this
     * handler wrote one line earlier; see g_announce_connected. */
    if (g_pagebar) g_pagebar->goto_live();
    status_refresh();
}

static void on_doc(rc_frame *f)
{
    /* The stylesheet and skeleton come from the bridge, so the look can be
     * changed by restarting it rather than by rebuilding and re-deploying
     * the guest binary.
     *
     * Unconditional now. This used to be guarded by a g_doc_sent flag that
     * was per PROCESS, not per connection, so the bridge's skeleton was
     * installed once at first contact and never again -- which meant a
     * stylesheet edit could not reach a running guest at all, and the guest
     * could not have a document before a bridge answered.
     *
     * rc_html::set_document now decides: identical content is a no-op (the
     * bridge re-sends the same skeleton on every attach, and re-navigating
     * would throw the transcript away for nothing), different content
     * re-navigates. The cold-start document beside the exe is what the app
     * boots with; this is what keeps it current. */
    if (g_html) g_html->set_document(f->body);
    /* The control may have been created here rather than with the other
     * children, in which case it has missed every layout so far and is still
     * sized to whatever DoVerb was given. Cheap enough to always do. */
    layout(g_main);
    SetTimer(g_main, TIMER_HTMLPOLL, 100, NULL);
}

static void on_text(rc_frame *f)
{
    /* Body is not NUL-terminated by the protocol; the reader
     * over-allocates by one and terminates it, so this is safe.
     *
     * "live" replaces the block still being written; anything else --
     * including no mode= at all -- is a finished block appended to the
     * end and never touched again. That "anything else" is deliberate
     * for an absent header, but not for one that is PRESENT and wrong: a
     * misspelled "live" would silently fall into the append branch below
     * and grow the transcript by a full copy of the answer per token, on
     * a 32MB guest that measures free memory in KB. So an unrecognized
     * but non-empty mode= is logged loudly rather than swallowed -- see
     * audit finding 03.22. */
    const char *mode = rc_frame_get(f, "mode");
    turn_receiving();
    /* Finished markup from the bridge -- the guest transforms nothing --
     * so this is the one path that does NOT escape. The scroll decision
     * moved into the view, which is the only place that can apply it
     * consistently. */
    if (mode && strcmp(mode, "live") == 0) {
        if (g_html) g_html->live(f->body);
    } else {
        if (mode && strcmp(mode, "append") != 0)
            rc_log("TEXT: unrecognized mode=\"%s\" (want \"append\" or "
                   "\"live\"); treating as a finished block", mode);
        if (g_html) g_html->block(f->body);
    }
}

static void on_tool(rc_frame *f)
{
    /* The bridge sends tool activity as a finished block now; this branch
     * only survives for older bridges. */
    (void)f;
    turn_receiving();
}

static void on_turn(rc_frame *f)
{
    const char *v;
    const char *state = rc_frame_get(f, "state");
    if (state && strcmp(state, "start") == 0) {
        ui_set_busy(1);
        g_turn_start_ms = GetTickCount();
        status_set(2, " Claude is thinking...");
        think_show();
    } else {
        turn_done();
        ui_set_busy(0);
        /* Now, and only now, re-fetch the list. Sending a message is what
         * makes a conversation the most recent one, so this is where the
         * reordering belongs -- not on merely opening one. */
        if (g_chats) g_chats->request();
        /* state=="error" used to fall into the same branch as a normal
         * end and render identically -- turn_error() (protocol.py) sends
         * `msg` and no `cost_usd`, so "done $?" is what a failed turn
         * looked like. Distinguished now: an error goes to the transcript
         * as an error and the status bar says so, instead of reading like
         * a quiet success. See audit finding 03.50. */
        if (state && strcmp(state, "error") == 0) {
            /* The message is in the BODY, not a header. It used to be a
             * `msg=` header, and the more informative the error the more
             * likely the user got a dropped LINK instead of the text: a
             * multi-line message percent-encodes past RC_MAX_VAL, the guest
             * refuses an over-long header value by design (01.44), and
             * net.cpp treats an unparseable frame as a dead stream. See
             * turn_error() in protocol.py. */
            if (g_html)
                g_html->error((f->body && *f->body)
                              ? f->body
                              : "The turn ended with an error.");
            status_set(2, " error");
        } else if (state && strcmp(state, "end") == 0
                   && (v = rc_frame_get(f, "ok")) != NULL
                   && strcmp(v, "0") == 0) {
            /* A TURN THAT CAME BACK NOT-OK IS NOT A QUIET SUCCESS.
             *
             * 03.50 fixed state="error", which is the path an EXCEPTION
             * takes. This is the other one, and it is the one a quota or
             * rate-limit refusal actually takes: the SDK returns normally
             * with ok=0 and nothing streamed, so the turn used to render as
             * `done $?` with an empty transcript -- the user's own report was
             * "i'm just getting interrupted with no message nothing".
             *
             * `subtype` is the SDK's own word for what went wrong and is the
             * only thing here that carries any information, so it is shown
             * rather than swallowed. Empty subtype still gets a sentence:
             * "something failed and we do not know what" is a far better
             * answer than a cost line for a turn that produced nothing. */
            const char *why = rc_frame_get(f, "subtype");
            char line[256];
            rc_strf(line, sizeof(line),
                    "The request did not complete%s%s. Nothing was returned "
                    "-- it may not have run at all.",
                    (why && *why) ? ": " : "", (why && *why) ? why : "");
            if (g_html) g_html->error(line);
            status_set(2, " failed");
        } else {
            char buf[96];
            v = rc_frame_get(f, "cost_usd");
            rc_strf(buf, sizeof(buf), " done  $%s", (v && *v) ? v : "?");
            status_set(2, buf);
        }
    }
}

/* OPTION, OPTIONSEND -- both owned by app/optmenu.cpp now; see
 * rc_optmenu_frame's own comments there for what each one does. */
static void on_optmenu(rc_frame *f)
{
    if (g_optmenu) g_optmenu->frame(f);
}

/* app/chatlist.h's `switched` callback: a SWITCHED frame has already been
 * folded into the chat list's own bookkeeping (the newly-open conversation
 * marked and redrawn) -- this is everything about SWITCHED that is not the
 * chat list's business. */
static void chats_switched(rc_frame *f)
{
    /* A prompt belongs to the session that raised it, and switching
     * rebuilds the session -- the bridge denies anything pending as
     * "rebuild". Leaving the bar up would offer Allow/Deny for a request
     * that no longer exists, on a conversation the user has left. */
    if (g_bar) g_bar->hide_all();
    /* The page total is app/pagebar.cpp's state now -- see app/pagebar.h's
     * rc_pagebar::note_total. No button refresh here, matching what this
     * call site always did. */
    if (g_pagebar) g_pagebar->note_total(f);
    SetWindowTextA(g_main, RC_TITLE);
    status_set(2, "");
}

/* SESSIONS, SESSION, SESSIONSEND, SWITCHED -- one row in g_dispatch below
 * covers all four, all owned by app/chatlist.cpp now (audit findings 01.1
 * and 01.11: the cleanest cut, because nothing outside these branches
 * touched a g_chat_* global). log_mem stays here rather than moving into
 * chatlist.cpp: it is main.c's own memory-milestone diagnostic, not
 * something the chat list module has reason to know about. */
static void on_chats(rc_frame *f)
{
    if (g_chats) g_chats->frame(f);
    if (strcmp(f->type, "SESSIONSEND") == 0) log_mem("chat list loaded");
}

/* PAGESTART, PAGEEND, PAGESEALED -- all three owned by app/pagebar.cpp now;
 * see rc_pagebar::frame's own comments there for what each one does. Two
 * things about PAGESTART/PAGEEND are not app/pagebar.cpp's business and stay
 * here, the same split on_chats already makes around rc_chats::frame:
 * resetting main.c's own turn-state enum (the control is about to be
 * cleared) and log_mem, main.c's memory-milestone diagnostic. */
static void on_pagebar(rc_frame *f)
{
    if (strcmp(f->type, "PAGESTART") == 0)
        g_turn_state = TURN_IDLE;
    if (g_pagebar) g_pagebar->frame(f);
    if (strcmp(f->type, "PAGEEND") == 0) {
        log_mem("page loaded");
        /* Past PAGESTART's clear() and past the pre-ready queue, so this is
         * the first moment the line can be written and survive. */
        if (g_announce_connected) {
            g_announce_connected = 0;
            if (g_html) g_html->notice("Connected to bridge.");
        }
    }
}

static void on_anchor(rc_frame *f)
{
    const char *page = rc_frame_get(f, "page");
    const char *preview = rc_frame_get(f, "preview");
    rc_log("anchor: page=%s preview=%s", page ? page : "?",
           preview ? preview : "");
}

/* PERMREQ, PERMSTATE, PERMWAIT, PERMEND, HALT, HALTEND -- all six owned by
 * app/bar.cpp now; see rc_bar::frame's own comments there for what each one
 * does. One handler serves all six rows in the table below. */
static void on_bar(rc_frame *f)
{
    if (g_bar) g_bar->frame(f);
}

static void on_error(rc_frame *f)
{
    char line[512];
    const char *code = rc_frame_get(f, "code");
    const char *msg = rc_frame_get(f, "msg");
    rc_strf(line, sizeof(line), "[%s] %s", code ? code : "error",
              msg ? msg : "");
    rc_log("bridge error: %s", line);
    /* Escaped, which it was not: this pushed the bridge's own `code` and
     * `msg` straight into insertAdjacentHTML, so a '<' anywhere in either
     * ended the document. */
    if (g_html) g_html->error(line);
    ui_set_busy(0);
}

static void on_notice(rc_frame *f)
{
    if (g_html) g_html->notice(f->body);
}

static void on_resync(rc_frame *f)
{
    /* The bridge could not replay what we missed and is telling us the
     * transcript has a hole in it. Say so, because the alternative is the
     * user scrolling back through a gap they were never told about.
     *
     * Had no branch at all, so it fell through to "unhandled frame type"
     * -- a log line nobody reads on a machine with no console. */
    const char *why = rc_frame_get(f, "reason");
    char msg[256];
    rc_strf(msg, sizeof(msg),
            "Some of the conversation could not be restored after the "
            "reconnection%s%s. Use the page buttons to re-read it.",
            (why && *why) ? ": " : "", (why && *why) ? why : "");
    if (g_html) g_html->notice(msg);
}

static void on_pong(rc_frame *f)
{
    (void)f;
    /* The answer to a PING. We do not send PING yet -- see BACKLOG -- so
     * this cannot arrive today, but handling it here means the day the
     * keepalive lands it does not read as a protocol error. Nothing to
     * show: receiving it IS the information, and the reader thread has
     * already stamped last_seen by getting this far. */
    rc_log("pong");
}

static const struct { const char *type; rc_frame_fn fn; } g_dispatch[] = {
    { "READY",       on_ready },
    { "DOC",         on_doc },
    { "TEXT",        on_text },
    { "TOOL",        on_tool },
    { "TURN",        on_turn },
    { "OPTION",      on_optmenu },
    { "OPTIONSEND",  on_optmenu },
    { "SESSIONS",    on_chats },
    { "SESSION",     on_chats },
    { "SESSIONSEND", on_chats },
    { "SWITCHED",    on_chats },
    { "PAGESTART",   on_pagebar },
    { "PAGEEND",     on_pagebar },
    { "PAGESEALED",  on_pagebar },
    { "ANCHOR",      on_anchor },
    { "PERMREQ",     on_bar },
    { "PERMSTATE",   on_bar },
    { "PERMWAIT",    on_bar },
    { "PERMEND",     on_bar },
    { "HALT",        on_bar },
    { "HALTEND",     on_bar },
    { "ERROR",       on_error },
    { "NOTICE",      on_notice },
    { "RESYNC",      on_resync },
    { "PONG",        on_pong },
};

static void render_frame(rc_frame *f)
{
    unsigned i;

    /* Progress accounting, before anything else looks at the frame -- see
     * app/xferstat.h. Moved out whole rather than left calling back in:
     * nothing here needs the frame beyond what rc_xferstat::note_rx already
     * parses from it. */
    if (g_xfer) g_xfer->note_rx(f);
    status_refresh();

    for (i = 0; i < sizeof(g_dispatch) / sizeof(g_dispatch[0]); i++) {
        if (strcmp(f->type, g_dispatch[i].type) == 0) {
            g_dispatch[i].fn(f);
            return;
        }
    }
    rc_log("unhandled frame type: %s", f->type);
}

/* ------------------------------------------------------------------ input */

/* chats_text_width and fit_label moved to app/chatlist.cpp with the drawing
 * code that is their only caller. */

/* A turn is running and nothing has come back yet. Without this the window
 * just sits there: Send greys out, but on a slow link or a long think that is
 * indistinguishable from a hang. */
/* Deliberately NOT in the transcript.
 *
 * This used to be forced: the obvious design is to append "Claude is
 * thinking..." and delete it again when output arrives, and that wedged
 * Win98's riched20 -- EM_REPLACESEL put it back into the state where it
 * stopped painting altogether, the same failure as an unprimed control, and
 * the whole answer then streamed in invisibly. riched20 is gone; the
 * transcript is MSHTML now, and rc_html can replace a block in place (live())
 * or empty it (clear()) without that failure mode. The measurement that
 * proved the wedge is kept in PROGRESS.md, not here, because it is no longer
 * why this code looks the way it does.
 *
 * Keeping the indicator out of the transcript is now a deliberate choice, not
 * a rendering constraint: it is transient status, not part of the
 * conversation, and belongs in the title bar and status pane rather than in
 * something a user might scroll back through or export. The title says that
 * something is happening, the status pane ticks the seconds so it is visibly
 * not a hang, and both clear themselves. */
static void think_show(void)
{
    if (g_turn_state != TURN_IDLE) return;
    g_turn_state = TURN_THINKING;
    SetWindowTextA(g_main, RC_TITLE "  -  working...");
    /* The bridge has the message: it started a turn for it. Drop the echo's
     * provisional styling in place -- the block does not move or re-render,
     * it just stops looking unsent. */
    if (g_html) g_html->settle_pending();
}

/* First output of the turn. The title stays as it is until the turn actually
 * ends -- the window is still busy -- but the wording in the status bar
 * changes, because there is now something to look at. Was think_hide(): that
 * name was accurate when this deleted the indicator out of a RichEdit
 * control; now it only advances a state enum, and nothing is hidden. */
static void turn_receiving(void)
{
    if (g_turn_state == TURN_IDLE) return;
    g_turn_state = TURN_STREAMING;
}

static void turn_done(void)
{
    g_turn_state = TURN_IDLE;
    SetWindowTextA(g_main, RC_TITLE);
    /* The status line is a statement about right now, so it goes when "right
     * now" stops being true. Leaving "receiving... 41s" under a finished
     * answer would be the same class of lie as the status bar saying
     * "receiving" while a permission prompt waits for a human. */
    if (g_html) g_html->status("");
}

/* echo_user_block, do_send, do_stop and input_proc (the Enter-sends/
 * Shift+Enter-inserts-a-newline subclass) moved to app/input.cpp -- audit
 * finding 01.59, the last of the §1.2 extraction targets to actually move.
 * See app/input.h for the seam: `input_blocked`/`input_sent` above are this
 * file's answers to the two policy questions that module does not own, the
 * same shape as app/chatlist.h's confirm/switching/switched.
 *
 * chats_proc (the listbox subclass that posts WM_RC_OPENCHAT on a plain
 * click) moved to app/chatlist.cpp with the rest of the chat list. */

/* ----------------------------------------------------------------- layout */

static void layout(HWND hwnd)
{
    RECT rc;
    int sb_h = 0, y, ybar, w, h;
    RECT srect;

    GetClientRect(hwnd, &rc);
    if (g_status) {
        SendMessageA(g_status, WM_SIZE, 0, 0);
        GetWindowRect(g_status, &srect);
        sb_h = srect.bottom - srect.top;
    }

    w = rc.right - rc.left;
    h = rc.bottom - rc.top - sb_h;
    /* The input row NEVER moves. An earlier version shifted it up to make room
     * and drew the bar below it, which covered the Stop button completely --
     * so the one control you need while a request is pending was the one that
     * disappeared. The bar docks to the bottom of the transcript instead, and
     * only the transcript gets shorter. */
    y = h - INPUT_HEIGHT - GUTTER;
    ybar = (g_bar && g_bar->active()) ? (y - PERM_H - GUTTER) : y;

    /* Chat list down the left, with its Refresh button under it -- both
     * positioned by app/chatlist.cpp now. */
    if (g_chats) g_chats->move(GUTTER, GUTTER, CHATS_W, h - 2 * GUTTER);

    /* Page bar across the top of the transcript column: positions its own
     * six controls from app/pagebar.cpp now -- see app/pagebar.h. */
    if (g_pagebar) g_pagebar->move(GUTTER * 2 + CHATS_W, GUTTER);

    {
        int cx = GUTTER * 2 + CHATS_W;
        int cw = w - cx - GUTTER;
        if (g_html)
            g_html->move(cx, GUTTER + PAGEBAR_H, cw, ybar - GUTTER - PAGEBAR_H);
        /* The bar positions its own controls -- see app/bar.h -- from its
         * own descriptor table, which is what used to be the "g_bar_mode ==
         * BAR_PERM" branch here (audit finding 01.15: a new bar state needed
         * an edit at this exact spot too). */
        if (g_bar && g_bar->active())
            g_bar->move(cx, ybar, w - GUTTER);
        /* The input box, Send and Stop position their own three controls
         * now -- app/input.cpp, see app/input.h. */
        if (g_inputmod) g_inputmod->move(cx, y, cw, w);
    }
}

/* CreateWindowExA can fail, and on this platform that was never a theoretical
 * concern: the chat list's own CreateWindowExA comment (app/chatlist.cpp)
 * cites the 16-bit USER heap's fixed 64KB as a reason a control might not be
 * created -- one of six such citations across
 * main.cpp/chatlist.cpp/bar.cpp/pagebar.cpp/input.cpp now that the bar, the
 * chat list, the page bar and compose/send are their own files. A NULL here
 * was previously silent -- nothing logged, and every subsequent
 * SendMessageA/MoveWindow/etc. against the handle becomes a quiet no-op,
 * since the *A functions accept NULL and simply do nothing with it. The log
 * is the only diagnostic on a machine with no console, so this is where the
 * failure has to become visible. See audit finding 01.18. */
static void check_child(const char *what, HWND h)
{
    if (!h)
        rc_log("create_children: %s failed to create (GetLastError=%lu)",
               what, (unsigned long)GetLastError());
}

static void create_children(HWND hwnd, HINSTANCE inst)
{
    int parts[3];
    NONCLIENTMETRICSA ncm;

    memset(&ncm, 0, sizeof(ncm));
    ncm.cbSize = sizeof(ncm);
    if (SystemParametersInfoA(SPI_GETNONCLIENTMETRICS, sizeof(ncm), &ncm, 0)) {
        g_ui_font = CreateFontIndirectA(&ncm.lfMessageFont);
        /* A bold companion, used for the conversation that is currently open.
         * Without it the only cue is the selection highlight, which Win98
         * greys out the moment the list loses focus -- so as soon as you click
         * into the input box you can no longer tell which chat you are in. */
        ncm.lfMessageFont.lfWeight = FW_BOLD;
        g_ui_font_bold = CreateFontIndirectA(&ncm.lfMessageFont);
    } else {
        g_ui_font = (HFONT)GetStockObject(DEFAULT_GUI_FONT);
        g_ui_font_bold = g_ui_font;
    }

    g_html = rc_html::create(hwnd);
    if (!g_html)
        MessageBoxA(hwnd, "Could not create the HTML view.", RC_TITLE,
                    MB_OK | MB_ICONERROR);
    else if (g_html->boot_local()) {
        /* There is a document now, before anything has been connected to.
         * The poll timer is what notices it has finished loading; on_doc
         * arms one too and re-arming is harmless. */
        SetTimer(hwnd, TIMER_HTMLPOLL, 100, NULL);
        /* Say what the window is about to do. The transcript used to be a
         * bare grey rectangle until a bridge answered -- and if none ever
         * did, it stayed that way, with the only sign of trouble a word in
         * the status bar. This goes through the pre-ready queue and appears
         * when the document finishes loading. */
        if (g_cfg.host[0]) {
            char line[160];
            rc_strf(line, sizeof(line), "Connecting to %s:%d...",
                    g_cfg.host, g_cfg.port);
            g_html->notice(line);
        } else {
            g_html->notice("No bridge is configured. "
                           "Use Configure -> Server Host... to set one.");
        }
    }
    else
        /* The INI's autoscroll, applied. Audit 01.2 said this setting was
         * "loaded from the INI, written back, round-trip-tested by
         * inicheck.exe, and read by nothing", gave the view a
         * set_autoscroll() to fix it -- and then nobody called it. The
         * finding was marked closed on the strength of the method existing.
         * One line makes the claim true. */
        g_html->set_autoscroll(g_cfg.autoscroll);

    /* The chat list and its Refresh button: created and owned by
     * app/chatlist.cpp now (rc_chats::create logs its own check_child
     * failures, and the LBS_NOINTEGRALHEIGHT/WS_CLIPSIBLINGS/
     * LBS_OWNERDRAWFIXED/no-WS_HSCROLL comment that used to be here moved
     * with the CreateWindowExA call it explains). g_ui_font/g_ui_font_bold
     * are already set above; chats_confirm/chats_switching/chats_switched
     * are this file's answers to the policy questions the chat list does
     * not own -- see the callback comments on rc_chats::create in
     * app/chatlist.h. */
    g_chats = rc_chats::create(hwnd, inst, &g_net, g_ui_font, g_ui_font_bold,
                                chats_confirm, ui_send, chats_switching,
                                chats_switched);

    /* The permission/halt bar: its own controls, created and owned by
     * app/bar.cpp now (rc_bar::create logs its own check_child-equivalent
     * failures). ui_send is main.c's "send something the user just asked
     * for" and bar_changed is the layout + enables refresh the bar needs
     * after any visible change -- see the `send`/`changed` comment on
     * rc_bar::create in app/bar.h. */
    g_bar = rc_bar::create(hwnd, inst, g_ui_font, &g_net, ui_send, bar_changed);

    /* The page bar: its own six controls, created and owned by
     * app/pagebar.cpp now (rc_pagebar::create logs its own check_child
     * failures). g_html may be NULL -- rc_pagebar::create is passed it as-is
     * and every use inside pagebar.cpp is guarded, same as everywhere else
     * in this file. */
    g_pagebar = rc_pagebar::create(hwnd, inst, g_ui_font, &g_net, g_html);

    /* The transfer-progress counters: one instance, created here (ahead of
     * app/input.cpp below, which needs it) and destroyed in WM_DESTROY --
     * the same shape as g_html. May be NULL if `new` itself failed (see
     * core/rc_cpp.h); every use, here and in input.cpp, is guarded. */
    g_xfer = rc_xferstat::create();

    /* The input box and Send/Stop: created and owned by app/input.cpp now
     * (rc_input::create logs its own check_child failures and subclasses the
     * input box itself). ui_send is main.c's "send something the user just
     * asked for", for INTERRUPT; input_blocked/input_sent are this file's
     * answers to the two policy questions that module does not own -- see
     * the callback comments on rc_input::create in app/input.h. */
    g_inputmod = rc_input::create(hwnd, inst, g_ui_font, &g_net, g_html,
                                  g_xfer, ui_send,
                     input_blocked, input_sent);

    g_status = CreateWindowExA(0, STATUSCLASSNAMEA, "",
        WS_CHILD | WS_VISIBLE | SBARS_SIZEGRIP,
        0, 0, 0, 0, hwnd, (HMENU)ID_STATUS, inst, NULL);
    check_child("status", g_status);
    parts[0] = 220; parts[1] = 340; parts[2] = -1;
    SendMessageA(g_status, SB_SETPARTS, 3, (LPARAM)parts);

    /* The chat list, the bar, the page bar and the input row all got their
     * font (and, for the page bar and Stop, their initial enabled state)
     * inside rc_chats::create/rc_bar::create/rc_pagebar::create/
     * rc_input::create above. */
}

/* -------------------------------------------------------------- main proc */

static void try_connect(HWND hwnd)
{
    status_refresh();
    /* Tear the previous attempt down FIRST.
     *
     * rc_net_start is not idempotent: it re-initialises the critical section,
     * zeroes the outbound queue and overwrites the thread handles. Calling it
     * with the sender and worker from a dead connection still alive put two
     * senders on one socket and corrupted the queue. rc_net_stop is a no-op
     * when there is nothing to stop. */
    rc_net_stop(&g_net);
    if (rc_net_start(&g_net, hwnd, g_cfg.host, g_cfg.port, g_cfg.secret) != 0) {
        /* Say it ONCE per outage, not once per retry.
         *
         * Appending on every attempt filled the transcript with hundreds of
         * identical "Cannot reach ... retrying in 15s" lines while the bridge
         * was down -- which is not merely ugly: it is unbounded growth in the
         * one structure this machine cannot afford to grow, on a box measured
         * at 28KB free. The status bar already shows the live state, and that
         * is the right place for something that changes every few seconds. */
        if (!g_retry_noted) {
            char msg[160];
            rc_strf(msg, sizeof(msg), "Cannot reach %s:%d - retrying",
                      g_cfg.host, g_cfg.port);
            /* Escaped now. The host comes from the INI, so this was
             * unescaped user-supplied text going into the document. */
            if (g_html) g_html->notice(msg);
            g_retry_noted = 1;
        }

        /* Backoff on a timer, never a sleeping loop -- the message queue has
         * to keep pumping or the window is a dead rectangle. */
        SetTimer(hwnd, TIMER_RECONNECT, (UINT)g_backoff_ms, NULL);
        g_backoff_ms *= 2;
        if (g_backoff_ms > 15000) g_backoff_ms = 15000;
    }
    status_refresh();
}

static LRESULT CALLBACK wnd_proc(HWND hwnd, UINT msg, WPARAM wp, LPARAM lp)
{
    switch (msg) {
    case WM_CREATE:
        create_children(hwnd, ((LPCREATESTRUCT)lp)->hInstance);
        SetTimer(hwnd, TIMER_STATUS, 1000, NULL);
        PostMessage(hwnd, WM_RC_CONNECT, 0, 0);
        return 0;

    case WM_RC_CONNECT:
        try_connect(hwnd);
        return 0;

    case WM_SIZE:
        layout(hwnd);
        return 0;

    case WM_SETFOCUS:
        if (g_inputmod) g_inputmod->focus();
        return 0;

    /* Owner-draw so a date header reads as a header instead of as a
     * conversation named "Today" -- app/chatlist.cpp's own business now,
     * since ID_CHATS is the only owner-draw control this window has. */
    case WM_MEASUREITEM:
        if (g_chats) g_chats->measure((MEASUREITEMSTRUCT *)lp);
        return TRUE;

    case WM_DRAWITEM:
        if (g_chats) g_chats->draw((DRAWITEMSTRUCT *)lp);
        return TRUE;

    case WM_RC_OPENCHAT:
        if (g_chats) g_chats->open_selected();
        return 0;

    case WM_TIMER:
        if (wp == TIMER_RECONNECT) {
            KillTimer(hwnd, TIMER_RECONNECT);
            try_connect(hwnd);
        } else if (wp == TIMER_STATUS) {
            status_refresh();
        } else if (wp == TIMER_HTMLPOLL) {
            if (g_html && g_html->poll()) KillTimer(hwnd, TIMER_HTMLPOLL);
        }
        return 0;

    case WM_RC_FRAME: {
        rc_frame *f = (rc_frame *)lp;
        render_frame(f);
        /* The UI thread owns every posted frame and is the only thing that
         * frees one -- except when PostMessage itself failed, which the reader
         * handles. rc_net_frame_done (net.h) is the two-free-plus-decrement
         * contract in one call; see audit finding 01.24. */
        rc_net_frame_done(&g_net, f);
        return 0;
    }

    case WM_RC_STATE:
        status_refresh();
        if ((rc_state)wp == RC_FAILED || (rc_state)wp == RC_DISCONNECTED) {
            /* Take the bar down FIRST.
             *
             * Nothing did, once, and ui_refresh_enables ORs
             * blocks_send() into the Send button's enabled state --
             * so a disconnection while a permission prompt or a halt was on
             * screen left Send disabled across the whole outage AND after
             * the reconnection, with a bar on screen whose buttons answer a
             * request the bridge has already given up on. The app looked
             * wedged.
             *
             * Safe to drop locally because the bridge restores both on
             * reattach: _reissue_prompt re-sends PERMSTATE and _reissue_halt
             * re-sends HALT, so whatever is genuinely still outstanding comes
             * back, and whatever is not, does not. */
            if (g_bar) g_bar->hide_all();
            /* And abandon a SYNCPUT the drop caught mid-stream. The RPC
             * worker thread survives a reconnect (rc_rpc_start's
             * if (g_thread) return 0), so its state survives with it; a PUT
             * with no closing SYNCPUTEND left g_put.active latched forever,
             * refusing every later PUT as "busy" -- found while checking
             * 01.26's premise that these singletons need per-connection
             * context, and fixed instead where the actual defect was: this
             * is the disconnect side, not a second-connection one. A fresh
             * sync pass after reconnect resends from the top, so there is
             * nothing here worth resuming. */
            rc_syncrpc_abort();
            ui_set_busy(0);
            SetTimer(hwnd, TIMER_RECONNECT, (UINT)g_backoff_ms, NULL);
        }
        return 0;

    case WM_COMMAND: {
        int id = (int)LOWORD(wp);
        switch (id) {
        /* Send and Stop dispatch through one call now too -- see
         * app/input.h. */
        case ID_SEND:
        case ID_STOP:
            if (g_inputmod) g_inputmod->command(id);
            return 0;
        /* All three of the bar's own buttons dispatch through one call now
         * -- see app/bar.h. command() knows which kind is on screen and what
         * each button means for it (audit finding 01.15: this used to be a
         * five-non-adjacent-places problem, one of which was this exact
         * switch). */
        case ID_PERM_ALLOW:
        case ID_PERM_ALWAYS:
        case ID_PERM_DENY:
            if (g_bar) g_bar->command(id);
            return 0;
        /* All five of the page bar's own buttons dispatch through one call
         * now too -- see app/pagebar.h. */
        case ID_LIVE:
        case ID_FIRST:
        case ID_PREV:
        case ID_NEXT:
        case ID_LAST:
            if (g_pagebar) g_pagebar->command(id);
            return 0;
        case ID_REFRESH: if (g_chats) g_chats->request(); return 0;

        case IDM_NEW:
            if (g_turn_active
                && MessageBoxA(hwnd,
                               "A request is still running.\n\n"
                               "Starting a new session will stop it. Continue?",
                               RC_TITLE, MB_YESNO | MB_ICONQUESTION) != IDYES)
                return 0;
            /* Ctrl+N is global, the way it is in every Windows application,
             * and the input box holds focus almost all the time -- so without
             * this, one keystroke while composing throws the message away
             * with nothing on screen to say it happened.
             *
             * Gating the KEY on focus was the other option and it is worse:
             * the shortcut would be unreachable exactly when you are using
             * the app. Guard the destructive act, not the key. "The person at
             * the keyboard never loses a byte." */
            if (g_inputmod && !g_inputmod->is_empty()
                && MessageBoxA(hwnd,
                               "You have a message you have not sent.\n\n"
                               "Starting a new session will discard it. "
                               "Continue?",
                               RC_TITLE, MB_YESNO | MB_ICONQUESTION) != IDYES)
                return 0;
            SetWindowTextA(g_main, RC_TITLE "  -  new session...");
            ui_send("Starting a new conversation", "NEWSESSION", NULL, NULL, 0, NULL, 0);
            return 0;

        case IDM_CLOSE:
            PostMessageA(hwnd, WM_CLOSE, 0, 0);
            return 0;

        /* Accelerator-only commands. They route to the same entry points the
         * buttons use -- rc_pagebar::command clamps to [1,total] and keeps
         * the buttons' enable state honest, which a direct goto_n would
         * bypass. See app/resource.h for why these are not ID_PREV/ID_NEXT. */
        case IDA_PAGEPREV:
            if (g_pagebar) g_pagebar->command(ID_PREV);
            return 0;

        case IDA_PAGENEXT:
            if (g_pagebar) g_pagebar->command(ID_NEXT);
            return 0;

        case IDA_REFRESH:
            if (g_chats) g_chats->request();
            return 0;

        case IDM_EXPORT:
            /* Wired, not implemented: exporting the whole conversation means
             * asking the bridge for every page, which is a chunked transfer
             * and belongs with the rest of that work. */
            MessageBoxA(hwnd,
                        "Not implemented yet.\n\n"
                        "This will ask the bridge for the whole conversation "
                        "and save it as a single HTML file.",
                        RC_TITLE, MB_OK | MB_ICONINFORMATION);
            return 0;

        case IDM_HOST: {
            char msg[320];
            rc_strf(msg, sizeof(msg),
                      "Currently connecting to %s:%d.\n\n"
                      "Set this in retroclaude.ini next to the program:\n\n"
                      "  [bridge]\n  host=%s\n  port=%d\n\n"
                      "A dialog for this is still to come.",
                      g_cfg.host, g_cfg.port, g_cfg.host, g_cfg.port);
            MessageBoxA(hwnd, msg, RC_TITLE, MB_OK | MB_ICONINFORMATION);
            return 0;
        }

        case IDM_ABOUT: {
            char msg[420];
            rc_strf(msg, sizeof(msg),
                      RC_TITLE "\n\n"
                      "A native Windows 98 client for Claude Code.\n"
                      "Built with Open Watcom; the transcript is MSHTML.\n\n"
                      "Not affiliated with Anthropic.\n\n"
                      "Bridge: %s:%d (%s)",
                      g_cfg.host, g_cfg.port, rc_state_name(g_net.state));
            msg[sizeof(msg) - 1] = '\0';
            MessageBoxA(hwnd, msg, "About " RC_TITLE,
                        MB_OK | MB_ICONINFORMATION);
            return 0;
        }
        case ID_CHATS:
            /* A plain click is handled by the listbox subclass, which opens on
             * button release; this is only the double-click, kept for muscle
             * memory. LBN_SELCHANGE is deliberately not used: it also fires
             * for arrow keys, and switching on those would tear down a live
             * session every time the list was scrolled from the keyboard. */
            if (HIWORD(wp) == LBN_DBLCLK && g_chats) g_chats->open_selected();
            return 0;

        default:
            /* The IDM_MODEL0/IDM_EFFORT0 ranges: app/optmenu.cpp's business
             * now -- see app/optmenu.h. */
            if (g_optmenu && g_optmenu->command(id)) return 0;
            break;
        }
        return 0;
    }

    case WM_CLOSE:
        DestroyWindow(hwnd);
        return 0;

    case WM_DESTROY:
        KillTimer(hwnd, TIMER_STATUS);
        KillTimer(hwnd, TIMER_RECONNECT);
        rc_net_stop(&g_net);
        delete g_html;
        g_html = NULL;
        delete g_bar;
        g_bar = NULL;
        delete g_chats;
        g_chats = NULL;
        delete g_inputmod;
        g_inputmod = NULL;
        delete g_xfer;
        g_xfer = NULL;
        delete g_optmenu;
        g_optmenu = NULL;
        delete g_pagebar;
        g_pagebar = NULL;
        /* Both fonts, and only once: the fallback path points both at the
         * same stock object, which must not be deleted at all. */
        if (g_ui_font_bold && g_ui_font_bold != g_ui_font)
            DeleteObject(g_ui_font_bold);
        g_ui_font_bold = NULL;
        if (g_ui_font) DeleteObject(g_ui_font);
        PostQuitMessage(0);
        return 0;
    }
    return DefWindowProcA(hwnd, msg, wp, lp);
}

int WINAPI WinMain(HINSTANCE inst, HINSTANCE prev, LPSTR cmd, int show)
{
    WNDCLASSA wc;
    MSG msg;
    INITCOMMONCONTROLSEX icc;

    (void)prev; (void)cmd;

    /* Log from the very first statement. On this platform "no log file was
     * created" is the primary diagnostic: an NT-only static import kills the
     * process at load, before WinMain, leaving nothing else behind. */
    exe_dir(g_exe_dir, sizeof(g_exe_dir));
    rc_log_open(g_exe_dir);
    rc_log("os version: %08lx", GetVersion());

    /* One instance. A second one is not a harmless duplicate: the bridge holds
     * ONE session and refuses a second client with `busy` -- so the new window
     * sits there saying "Cannot reach ... retrying" while the old one, often
     * behind it, has the conversation. That looked like a network fault for a
     * while, and it also explains a run of duplicated "Connected to bridge"
     * lines that had been put down to reconnect churn.
     *
     * A named mutex rather than FindWindow alone, because the window does not
     * exist yet during the seconds this actually matters -- two launches in
     * quick succession. FindWindow then raises the one that is already there,
     * so a second launch behaves like clicking the taskbar button. */
    {
        HANDLE once = CreateMutexA(NULL, FALSE, "RetroClaudeSingleInstance");
        if (once && GetLastError() == ERROR_ALREADY_EXISTS) {
            HWND other = FindWindowA("RetroClaudeWnd", NULL);
            rc_log("another instance is running; raising it and exiting");
            /* IsWindow, because FindWindow can hand back a window that is
             * already being destroyed.
             *
             * The window outlives the mutex during teardown -- the handle is
             * released when the process exits, and the class is unregistered
             * after WM_DESTROY -- so relaunching immediately after closing
             * (which is exactly what a ship-and-test loop does) lands here
             * with a HWND that is on its way out. It does not close the race,
             * nothing here can; it closes the window where we would act on a
             * handle already known to be dead. */
            if (other && IsWindow(other)) {
                if (IsIconic(other)) ShowWindow(other, SW_RESTORE);
                SetForegroundWindow(other);
            }
            CloseHandle(once);
            return 0;
        }
        /* Deliberately never closed: the handle IS the lock, and it is
         * released when the process exits, however it exits. */
    }

    memset(&icc, 0, sizeof(icc));
    icc.dwSize = sizeof(icc);
    /* ICC_BAR_CLASSES covers the toolbar, status bar, trackbar and tooltip.
     * It does NOT cover the progress bar -- that is ICC_PROGRESS_CLASS, and
     * without it CreateWindow on PROGRESS_CLASS fails at the moment a
     * transfer first wants to show progress, which is Phase 5's attachment
     * upload. One word now against a failure that would present as "the
     * progress bar does not appear" on a machine with no debugger. */
    icc.dwICC = ICC_BAR_CLASSES | ICC_PROGRESS_CLASS;
    InitCommonControlsEx(&icc);

    rc_config_load(&g_cfg, g_exe_dir);
    rc_syncrpc_set_root(g_cfg.sync_root);
    /* rpc.c needs the same string for its directory watcher, and used to get
     * it by reading rc_syncrpc_root() back -- audit finding 01.25. Pushed
     * here instead, the same way and at the same moment as the line above,
     * so rpc.c no longer reaches into syncrpc.c's state for its own
     * configuration. */
    rc_rpc_set_sync_root(g_cfg.sync_root);
    if (g_cfg.sync_root[0]) {
        /* Put back any file an interrupted commit left aside, BEFORE the first
         * scan sees the path as missing and reports a deletion nobody made. */
        rc_sync_recover(g_cfg.sync_root);
        rc_log("sync: root is %s", g_cfg.sync_root);
    }

    memset(&wc, 0, sizeof(wc));
    wc.lpfnWndProc   = wnd_proc;
    wc.hInstance     = inst;
    /* Two sizes, loaded separately and deliberately.
     *
     * WNDCLASSA has no hIconSm field -- that is WNDCLASSEX, and this stays
     * on RegisterClassA. So Windows derives the 16x16 title-bar icon by
     * shrinking hIcon, and StretchBlt-ing a 32x32 down to 16x16 on a 4bpp
     * image turns the three dots into grey mush. LoadImageA with an explicit
     * size picks the 16x16 entry out of the .ico instead, and WM_SETICON
     * installs it once the window exists.
     *
     * SM_CXSMICON rather than a literal 16: it is 16 on this guest, but it
     * is the metric the shell actually asks for, and asking for the wrong
     * one is how an icon ends up scaled after all. */
    wc.hIcon         = (HICON)LoadImageA(inst, MAKEINTRESOURCE(IDI_GASTON),
                                         IMAGE_ICON,
                                         GetSystemMetrics(SM_CXICON),
                                         GetSystemMetrics(SM_CYICON),
                                         LR_DEFAULTCOLOR);
    if (!wc.hIcon) wc.hIcon = LoadIconA(NULL, IDI_APPLICATION);
    wc.hCursor       = LoadCursorA(NULL, IDC_ARROW);
    wc.hbrBackground = (HBRUSH)(COLOR_BTNFACE + 1);
    wc.lpszClassName = "RetroClaudeWnd";
    if (!RegisterClassA(&wc)) { rc_log("RegisterClass failed"); return 1; }

    /* OleInitialize, not CoInitialize: in-place activation of an embedded
     * control needs the full OLE apartment. */
    if (FAILED(OleInitialize(NULL))) {
        rc_log("OleInitialize failed");
        MessageBoxA(NULL, "OLE could not start.", RC_TITLE,
                    MB_OK | MB_ICONERROR);
        return 1;
    }

    g_main = CreateWindowExA(0, "RetroClaudeWnd", RC_TITLE,
        /* WS_CLIPCHILDREN: without it the parent's background erase paints
         * over its own children, which on a window that is almost entirely
         * children means flicker at best and erased controls at worst. */
        WS_OVERLAPPEDWINDOW | WS_CLIPCHILDREN,
        CW_USEDEFAULT, CW_USEDEFAULT, 640, 480,
        NULL, NULL, inst, NULL);
    if (!g_main) { rc_log("CreateWindowEx failed"); return 1; }
    {
        /* Not freed: an icon loaded from a resource is owned by the module
         * and is released when it unloads. DestroyIcon on one is documented
         * as unnecessary, and on 9x it is actively wrong. */
        HICON small_icon = (HICON)LoadImageA(inst,
                                             MAKEINTRESOURCE(IDI_GASTON),
                                             IMAGE_ICON,
                                             GetSystemMetrics(SM_CXSMICON),
                                             GetSystemMetrics(SM_CYSMICON),
                                             LR_DEFAULTCOLOR);
        if (small_icon)
            SendMessageA(g_main, WM_SETICON, ICON_SMALL,
                         (LPARAM)small_icon);
    }
    rc_log("main window hwnd=%p", (void *)g_main);
    log_mem("window created");

    g_accel = LoadAcceleratorsA(inst, MAKEINTRESOURCE(IDA_MAIN));
    if (!g_accel) rc_log("accelerators: LoadAccelerators failed");

    build_menu(g_main);
    ShowWindow(g_main, show);
    UpdateWindow(g_main);

    while (GetMessageA(&msg, NULL, 0, 0) > 0) {
        /* Accelerators FIRST, and only the keys whose meaning does not depend
         * on focus -- Ctrl+N, F5, Ctrl+PgUp/PgDn. See gaston.rc.
         *
         * Ctrl+N used to be hand-rolled right here with no msg.hwnd filter,
         * which is the same thing an accelerator does; moving it did not
         * change what it matches, and a draft of this plan claimed otherwise.
         * What makes it safe is the confirmation in IDM_NEW, not where the
         * key is translated. */
        if (g_accel && TranslateAcceleratorA(g_main, g_accel, &msg))
            continue;

        /* Enter opens the highlighted conversation. Caught here rather than by
         * subclassing the listbox, because IsDialogMessage would otherwise
         * route it to the default pushbutton and the keyboard would have no
         * way to open a chat at all. */
        if (msg.message == WM_KEYDOWN && msg.wParam == VK_RETURN
            && g_chats && msg.hwnd == g_chats->hwnd()) {
            g_chats->open_selected();
            continue;
        }
        /* Scrolling the transcript from the keyboard, and why these CANNOT be
         * accelerators.
         *
         * TranslateAccelerator does not look at msg.hwnd: it translates for
         * the whole message whatever has focus. PgUp/PgDn/Ctrl+Home/Ctrl+End
         * are the caret keys of a multiline EDIT, so an accelerator would
         * steal them from the input box permanently -- you could never move
         * the caret in a long draft again.
         *
         * So they are conditional, and the condition is "taking them costs
         * the edit control nothing": an empty box has no caret to move. A
         * non-empty one keeps them, and the page bar's buttons and the
         * scrollbar are still there for the mouse.
         *
         * Ctrl is deliberately not checked for PgUp/PgDn: Ctrl+PgUp/PgDn is
         * the accelerator above, which moves between SEALED PAGES, and that
         * is a different axis from scrolling within one. */
        if (msg.message == WM_KEYDOWN && g_html && g_inputmod
            && g_inputmod->is_empty()
            && !(GetKeyState(VK_CONTROL) & 0x8000)) {
            if (msg.wParam == VK_PRIOR) { g_html->scroll_page(-1); continue; }
            if (msg.wParam == VK_NEXT)  { g_html->scroll_page(1);  continue; }
        }
        if (msg.message == WM_KEYDOWN && g_html && g_inputmod
            && g_inputmod->is_empty()
            && (GetKeyState(VK_CONTROL) & 0x8000)) {
            if (msg.wParam == VK_HOME) { g_html->scroll_top();    continue; }
            if (msg.wParam == VK_END)  { g_html->scroll_bottom(); continue; }
        }
        /* Esc denies a pending permission request (a halt has no "deny" --
         * see rc_bar::escape). It has to be intercepted before
         * IsDialogMessageA, which eats VK_ESCAPE and would never let it
         * reach the bar. */
        if (msg.message == WM_KEYDOWN && msg.wParam == VK_ESCAPE
            && g_bar && g_bar->escape()) {
            continue;
        }
        if (!IsDialogMessageA(g_main, &msg)) {
            TranslateMessage(&msg);
            DispatchMessageA(&msg);
        }
    }

    log_mem("exiting");
    OleUninitialize();
    rc_log_close();
    return (int)msg.wParam;
}
