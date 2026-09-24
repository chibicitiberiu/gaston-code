/* retroclaude.exe -- native Windows 98 client for Claude Code.
 *
 * One process, one TCP connection, two threads in Phase 1 (UI + reader).
 * The RPC worker joins in Phase 3.
 *
 * Build: see Makefile. Target is Win98 SE on a Pentium MMX 200 with 32MB.
 */
#include <windows.h>
#include <commctrl.h>
#include <commdlg.h>
#include <shellapi.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "net/frameproto.h"
#include "core/ini.h"
#include "core/log.h"
#include "core/path.h"
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
#include "app/hostdlg.h"
#include "app/toolbar.h"
#include "app/export.h"
#include "app/cache.h"
#include "app/tooldet.h"
#include "app/workdir.h"
#include "app/workdlg.h"
#include "app/pillbar.h"
#include "app/screenshot.h"
#include "app/statusbar.h"
#include "app/reconnect.h"
#include "app/turn.h"
#include "net/caps.h"
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
static rc_pillbar  *g_pillbar;
static rc_toolbar  *g_toolbar;
/* The export sink. One instance, created in create_children and destroyed
 * in WM_DESTROY, the same shape as g_html -- see app/export.h. */
static rc_export   *g_export;
/* The on-disk transcript cache -- see app/cache.h. One instance, created in
 * create_children and destroyed in WM_DESTROY, the same shape as g_html. */
static rc_cache    *g_cache;
/* Has the cached transcript already been put on screen for this outage? The
 * reconnect timer fires every few seconds while the bridge is down, and
 * restoring on each one would clear and redraw the transcript under someone
 * who is reading it. */
static int          g_cache_shown;
/* The accelerator table. Loaded once in WinMain; owned by the module, so it
 * is not destroyed (DestroyAcceleratorTable is for CreateAcceleratorTable). */
static HACCEL       g_accel;
static rc_optmenu *g_optmenu;
static rc_pagebar *g_pagebar;
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
static HWND          g_main;
static rc_statusbar *g_status;
static rc_reconnect *g_reconn;
static rc_turn      *g_turn;
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

static void layout(HWND hwnd);
static int  ui_send(const char *what, const char *type,
                    const char *const *keys, const char *const *vals,
                    int nkv, const void *body, unsigned body_len);
static int  ui_send_text(const char *text, int compose);
static void cache_restore(void);

/* app/workdlg.h's `propose` callback. The dialog calls this ITSELF, between
 * the copy and the wait, so one progress window covers both halves of what
 * the user sees as a single operation.
 *
 * A PROPOSAL, not the change. The bridge records it against the conversation
 * and sends it back in the next OPTION batch, which is what makes it real on
 * this side -- see app/workdir.h for why the authority runs that way. */
static void propose_workdir(const char *path, int staged)
{
    const char *keys[2], *vals[2];
    if (!path || !*path) return;
    keys[0] = "path";   vals[0] = path;
    /* WHAT THE USER CHOSE, not merely where. "Let the assistant merge them"
     * is an instruction to the assistant, and the assistant is on the other
     * end of this socket -- without this header the copies land in
     * Gaston-Incoming and nothing ever asks anyone to reconcile them, which
     * makes that answer do half of what its label says. */
    keys[1] = "staged"; vals[1] = staged ? "merge" : "";
    ui_send("The work directory change", "SETWORK", keys, vals, 2, NULL, 0);
}

/* Pagination state moved to app/pagebar.cpp -- see app/pagebar.h. g_pagebar
 * is the one instance, created in create_children and destroyed in
 * WM_DESTROY, the same shape as g_html. */
static HFONT g_ui_font;
static HFONT g_ui_font_bold;
static char  g_exe_dir[MAX_PATH];
/* WinMain's HINSTANCE, kept because the Server Host dialog needs it and
 * DialogBoxIndirectParam is called from a WM_COMMAND handler that has no
 * other way to reach it. */
static HINSTANCE g_inst;


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

#define status_set(pane, text) do { if (g_status) g_status->set_pane(pane, text); } while (0)

static void status_refresh(void)
{
    if (g_status)
        g_status->refresh(g_net.state, g_cfg.host, g_cfg.port, g_xfer,
                          g_turn ? g_turn->active() : 0,
                          g_turn ? g_turn->start_ms() : 0,
                          g_turn ? (int)g_turn->state() : (int)TURN_IDLE,
                          g_bar && g_bar->active(), g_html);
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
    AppendMenuA(file, MF_STRING, IDM_ATTFILE, "&Attach File...");
    AppendMenuA(file, MF_SEPARATOR, 0, NULL);
    AppendMenuA(file, MF_STRING, IDM_EXPORT, "&Export as HTML...");
    AppendMenuA(file, MF_SEPARATOR, 0, NULL);
    AppendMenuA(file, MF_STRING, IDM_CLOSE, "&Close");

    AppendMenuA(cfg, MF_STRING, IDM_HOST, "&Server Host...");
    AppendMenuA(cfg, MF_STRING, IDM_PERMS, "&What can run without asking...");

    AppendMenuA(help, MF_STRING, IDM_ABOUT, "&About...");

    AppendMenuA(bar, MF_POPUP, (UINT_PTR)file, "&File");
    AppendMenuA(bar, MF_POPUP, (UINT_PTR)cfg, "&Configure");
    /* The Model and Effort popups: created and appended to `bar` by
     * app/optmenu.cpp now -- see app/optmenu.h. Between Configure and Help,
     * so the menu order is unchanged. */
    g_optmenu = rc_optmenu::create(hwnd, bar, ui_send, ui_send_text);
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
    return !(g_turn && g_turn->active())
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
    int active = g_turn && g_turn->active();
    if (g_inputmod) g_inputmod->enable_send((active || (g_bar && g_bar->blocks_send()))
                          ? 0 : 1);
    if (g_inputmod) g_inputmod->enable_stop(active);
}

static void ui_set_busy(int busy)
{
    (void)busy;
    ui_refresh_enables();
}

/* app/input.h's `blocked`/`sent` callbacks: do_send's own guard (checked
 * again because Enter reaches do_send directly, bypassing whatever
 * EnableWindow last set) and what runs after a typed INPUT has actually
 * gone out. */
static int input_blocked(void)
{
    return (g_turn && g_turn->active()) || (g_bar && g_bar->blocks_send());
}

static void pillbar_detached(void)
{
    if (g_main) layout(g_main);
}

static void input_sent(void)
{
    if (g_turn) g_turn->note_input();
    status_refresh();
    ui_set_busy(1);
    if (g_main) layout(g_main);
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
    /* THE KEYBOARD GOES BACK TO THE INPUT BOX when the bar comes down.
     *
     * show() hides the buttons, and Win32 does not reassign focus when the
     * focused window is hidden -- it lands on the frame, which has no text
     * handling. So on the most common path in the app (press Allow, PERMEND
     * arrives, pop(), show()) the user kept typing and the characters went
     * nowhere until they clicked the input box.
     *
     * Here rather than in bar.cpp because this is the layer that knows where
     * focus belongs; the bar has no business naming the input box. Only when
     * the bar is actually down, and only if focus is not already somewhere
     * the user put it deliberately. */
    if (g_bar && !g_bar->active() && g_inputmod) {
        HWND f = GetFocus();
        if (!f || f == g_main) g_inputmod->focus();
    }
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
    /* The UI budget, not the bulk one: this runs on the window thread and the
     * message pump does not run while it waits. See RC_SEND_WAIT_UI. */
    int rc = rc_net_send_ui(&g_net, type, keys, vals, nkv, body, body_len);
    if (rc == RC_SEND_OK)
        return 1;
    rc_log("ui: %s (%s) did not reach the bridge (%s)", what, type,
           rc == RC_SEND_FULL ? "queue full" : "link down");
    {
        char msg[224];
        /* TWO DIFFERENT SENTENCES, because they are two different situations
         * and only one of them is going to fix itself. A full queue means a
         * bulk transfer is ahead of this frame -- 64KB takes about thirteen
         * seconds to drain at 40kbit -- and pressing the button again in a
         * moment works. Saying "the connection is down" for that sent the
         * user to look at the network and promised a reconnect that was never
         * coming, while the permission request they were answering was still
         * outstanding on the bridge. (audit 01.17) */
        if (rc == RC_SEND_FULL)
            rc_strf(msg, sizeof(msg),
                    "%s could not be sent right now -- a file transfer is "
                    "using the connection. It has NOT taken effect. Try "
                    "again in a moment.", what);
        else
            rc_strf(msg, sizeof(msg),
                    "%s could not be sent -- the connection is down. It will "
                    "not have taken effect. Try again once it reconnects.",
                    what);
        if (g_html) g_html->error(msg);
    }
    return 0;
}

/* app/optmenu.h's rc_optmenu_input_fn: "send this as if it had been typed".
 *
 * A thin forward to compose-and-send rather than a second sender, because a
 * slash command IS a prompt -- the CLI reads it out of the message text and
 * there is no frame for one. Going through rc_input is what gives it the
 * local echo, the mid-turn refusal and the busy state. Created after the
 * menus (create_children runs at WM_CREATE, build_menu at its top), so this
 * can be called before g_inputmod exists only if a frame arrives first,
 * which cannot happen -- guarded anyway. */
static int ui_send_text(const char *text, int compose)
{
    if (!g_inputmod) return 0;
    return compose ? g_inputmod->compose(text) : g_inputmod->send_text(text);
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
    if (g_reconn) g_reconn->on_connected();
    v = rc_frame_get(f, "model");
    rc_log("ready: sess=%s model=%s", g_net.sess, v ? v : "?");
    /* A reconnect gets a fresh transcript from the bridge, so whatever was
     * restored from the cache during the outage is about to be replaced by
     * the real thing. Re-arm for the NEXT outage. */
    g_cache_shown = 0;
    /* WHICH conversation, not which connection. `sess` above is the
     * per-connection handle and is never a conversation id; `conv` is the
     * one the transcript arriving next belongs to. Without this, a plain
     * reconnect cached nothing at all -- only a SWITCHED told the cache
     * where to put pages, and a reconnect does not send one. */
    v = rc_frame_get(f, "conv");
    if (g_cache) g_cache->open_session(v ? v : "");
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
    if (g_turn) g_turn->on_doc(f);
}

static void on_text(rc_frame *f)
{
    if (g_turn) g_turn->on_text(f);
}

static void on_tool(rc_frame *f)
{
    if (g_turn) g_turn->on_tool(f);
}

static void on_turn(rc_frame *f)
{
    if (g_turn) g_turn->on_turn(f);
}

/* OPTION, OPTIONSEND -- both owned by app/optmenu.cpp now; see
 * rc_optmenu_frame's own comments there for what each one does. */
static void on_optmenu(rc_frame *f)
{
    if (g_optmenu) g_optmenu->frame(f);
    /* The work directory arrives in this batch like the model and the mode,
     * and THIS is what makes it real on this side: the sync layer is
     * re-pointed at it, the watcher follows, and the folder is created if it
     * has never existed. Called on every batch because rc_workdir_apply
     * returns without touching anything when the path has not changed. */
    if (g_optmenu && strcmp(f->type, "OPTIONSEND") == 0) {
        const char *want = g_optmenu->work_path();
        if (!rc_workdir_apply(want) && want && *want && g_html) {
            char msg[256];
            rc_strf(msg, sizeof(msg),
                    "Could not use %s as the work directory. Files will not "
                    "be shared until this is fixed.", want);
            g_html->error(msg);
        }
    }
    /* The toolbar shows the ticked entries as its button text, so it has to
     * be told when a batch lands. Cheap enough to do on every OPTION row
     * rather than only OPTIONSEND: re-labelling is two TB_SETBUTTONINFO
     * calls and the batch is a handful of frames once per session. */
    if (g_toolbar) g_toolbar->refresh();
}

/* app/chatlist.h's `switched` callback: a SWITCHED frame has already been
 * folded into the chat list's own bookkeeping (the newly-open conversation
 * marked and redrawn) -- this is everything about SWITCHED that is not the
 * chat list's business. */
static void chats_switched(rc_frame *f)
{
    /* THE SINGLE WRITER of net.sess. app/chatlist.cpp used to write it from
     * on_switched, which is the window thread -- while the connect path reads
     * it when building HELLO. One writer, here, on the thread that owns the
     * rest of the connection's state. */
    const char *sid = rc_frame_get(f, "id");
    if (sid) rc_strf(g_net.sess, sizeof(g_net.sess), "%s", sid);
    /* A prompt belongs to the session that raised it, and switching
     * rebuilds the session -- the bridge denies anything pending as
     * "rebuild". Leaving the bar up would offer Allow/Deny for a request
     * that no longer exists, on a conversation the user has left. */
    if (g_bar) g_bar->hide_all();
    /* The page total is app/pagebar.cpp's state now -- see app/pagebar.h's
     * rc_pagebar::note_total. No button refresh here, matching what this
     * call site always did. */
    if (g_pagebar) g_pagebar->note_total(f);
    /* SWITCHED is the only frame that names the conversation now on screen,
     * so it is where the cache learns which folder its pages belong in. The
     * pages themselves arrive immediately afterwards. */
    if (g_cache) g_cache->open_session(rc_frame_get(f, "id"));
    SetWindowTextA(g_main, RC_TITLE);
    status_set(2, "");
}

/* SESSIONS, SESSION, SESSIONSEND, SWITCHED -- one row in g_dispatch below
 * covers all four, all owned by app/chatlist.cpp now (audit findings 01.1
 * and 01.11: the cleanest cut, because nothing outside these branches
 * touched a g_chat_* global). log_mem stays here rather than moving into
 * chatlist.cpp: it is main.c's own memory-milestone diagnostic, not
 * something the chat list module has reason to know about. */
/* The chat list, written to the cache a row at a time as it arrives.
 *
 * A row at a time and never buffered: the first version of this held the
 * whole list in a static array, which cost 12.8KB of BSS on a machine
 * measured at 28 KB free after a turn -- for data whose destination is a
 * file being written one line at a time anyway. Four headers per row, not
 * the framing, because rc_chats::add_row is the path a cached row takes back
 * in and it wants values, not frames. */
static void on_chats(rc_frame *f)
{
    if (g_chats) g_chats->frame(f);

    if (strcmp(f->type, "SESSIONS") == 0) {
        if (g_cache) g_cache->begin_chats();
    } else if (strcmp(f->type, "SESSION") == 0) {
        const char *id = rc_frame_get(f, "id");
        const char *title = rc_frame_get(f, "title");
        const char *grp = rc_frame_get(f, "group");
        const char *cur = rc_frame_get(f, "current");
        char line[220];
        /* Tab-separated, because a title can contain almost anything else
         * and a tab is the one character the bridge's titles never carry --
         * they come from a summary line, not from a file. */
        rc_strf(line, sizeof(line), "%s\t%s\t%s\t%s",
                id ? id : "", title ? title : "", grp ? grp : "",
                (cur && *cur == '1') ? "1" : "0");
        if (g_cache) {
            g_cache->add_chat(line);
            /* The chat list is also where a BRAND NEW conversation's id
             * first reaches the guest. READY carries `conv`, but the SDK
             * does not assign an id until the first query goes out, so on a
             * fresh session that header is empty and stays empty until
             * something says otherwise. The row marked current is that
             * something. */
            if (cur && *cur == '1' && id && *id)
                g_cache->open_session(id);
        }
    } else if (strcmp(f->type, "SESSIONSEND") == 0) {
        if (g_cache) g_cache->end_chats();
        log_mem("chat list loaded");
    }
}

/* Put back what was on screen before the bridge went away.
 *
 * Called once per outage, from the failed-connect path. It does not pretend
 * to be connected: the notice says where this came from, and the page bar is
 * left alone because paging back through a cached conversation needs the
 * bridge and saying otherwise with buttons would be a lie you can click.
 */
static void cache_restore(void)
{
    char sess[64];
    char line[220];
    int pages;

    if (!g_cache || !g_cache->ok() || g_cache_shown) return;
    g_cache_shown = 1;

    /* The chat list first: it is the smaller of the two and it is what makes
     * the window look like an application rather than a failure. One line at
     * a time, so nothing here scales with how many conversations there are. */
    if (g_chats && g_cache->chats_open()) {
        g_chats->begin_rows();
        while (g_cache->chats_next(line, sizeof(line))) {
            char *fields[4];
            int nf = 0;
            char *p = line;
            fields[nf++] = p;
            while (*p && nf < 4) {
                if (*p == '\t') { *p = '\0'; fields[nf++] = p + 1; }
                p++;
            }
            if (nf == 4 && fields[0][0])
                g_chats->add_row(fields[0], fields[1], fields[2],
                                 fields[3][0] == '1');
        }
        g_cache->chats_close();
        g_chats->end_rows();
    }

    /* Then the conversation that was open, newest page first -- which is
     * where the reader was. */
    g_cache->last_session(sess, sizeof(sess));
    if (!sess[0]) return;
    g_cache->open_session(sess);
    pages = g_cache->page_count();
    if (pages <= 0) return;
    if (g_html) {
        g_html->notice("The bridge is not reachable. This is the last page "
                       "of your conversation as it was cached here; it is "
                       "not live, and nothing typed now will be sent.");
        g_cache->load_page(pages, g_html);
    }
    rc_log("cache: showed page %d of %d for %s", pages, pages, sess);
}

/* PAGESTART, PAGEEND, PAGESEALED -- all three owned by app/pagebar.cpp now;
 * see rc_pagebar::frame's own comments there for what each one does. Two
 * things about PAGESTART/PAGEEND are not app/pagebar.cpp's business and stay
 * here, the same split on_chats already makes around rc_chats::frame:
 * resetting main.c's own turn-state enum (the control is about to be
 * cleared) and log_mem, main.c's memory-milestone diagnostic. */
static void on_pagebar(rc_frame *f)
{
    if (strcmp(f->type, "PAGESTART") == 0) {
        if (g_turn) g_turn->turn_done();
        /* Cache the page the bridge is about to send. Only a page it says is
         * complete is kept -- see app/cache.h -- so this opens a temporary
         * file that PAGEEND renames and anything else throws away. */
        if (g_cache) {
            const char *n = rc_frame_get(f, "page");
            g_cache->begin_page(n ? atoi(n) : 0);
        }
    }
    if (g_pagebar) g_pagebar->frame(f);
    if (strcmp(f->type, "PAGEEND") == 0) {
        if (g_cache) g_cache->end_page();
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
    /* The transcript copy of the request collapses when the bar does. Here
     * rather than in app/bar.cpp for the same reason log_mem is not in
     * app/chatlist.cpp: the bar owns the bar, and the transcript is this
     * file's business. Unconditional -- settle_perm finds nothing and
     * returns when there is no block on screen, which is the case for a
     * PERMEND that arrives for a request the guest never showed. */
    if (strcmp(f->type, "PERMEND") == 0 && g_html)
        g_html->settle_perm();
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
    if (g_turn) g_turn->abort();
    ui_set_busy(0);
}

static void on_notice(rc_frame *f)
{
    if (g_html) g_html->notice(f->body);
}

static void on_status(rc_frame *f)
{
    const char *text = rc_frame_get(f, "text");
    if (!text || !*text) text = f->body;
    if (g_status) g_status->set_override(text);
    status_refresh();
}

/* EXPORTSTART / EXPORTDAT / EXPORTEND -- all three owned by app/export.cpp;
 * see rc_export::frame there for what each one does. */
static void on_export(rc_frame *f)
{
    if (g_export) g_export->frame(f);
}

static void on_pong(rc_frame *f)
{
    (void)f;
    /* The answer to a PING, which rpc.cpp's sweep now sends every 30s when
     * there is no sync root (with one it sends SYNCDIRTY instead). This
     * comment used to say we did not send PING yet and pointed at BACKLOG;
     * that stopped being true when rpc_maybe_nudge grew its keepalive, and a
     * comment describing a frame as unreachable when it arrives twice a
     * minute is worse than no comment. Nothing to show: receiving it IS the
     * information, and the reader thread has already stamped last_seen by
     * getting this far. */
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
    { "STATUS",      on_status },
    { "EXPORTSTART", on_export },
    { "EXPORTDAT",   on_export },
    { "EXPORTEND",   on_export },
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
    /* NO status_refresh() HERE.
     *
     * note_rx above is the cheap half -- it just accumulates counters -- and
     * it stays. The refresh was the expensive half: eight SendMessageA calls
     * to the status bar plus a 256-byte <div class="status"> through
     * rc_html::status, which is a MultiByteToWideChar, a SysAllocString and a
     * put_innerHTML, i.e. a real MSHTML reflow. Running that per inbound
     * frame during a streamed answer doubled the DOM work per frame on a
     * 200MHz Pentium, and the content was identical between frames because
     * the only variable in it is a whole-second counter.
     *
     * TIMER_STATUS already fires every second and calls status_refresh(),
     * which is the rate rc_html::status's own comment always claimed. See
     * audit 01.33. */

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

    GetClientRect(hwnd, &rc);
    if (g_status) {
        g_status->resize();
        sb_h = g_status->height();
    }

    w = rc.right - rc.left;
    h = rc.bottom - rc.top - sb_h;
    /* The input row NEVER moves. An earlier version shifted it up to make room
     * and drew the bar below it, which covered the Stop button completely --
     * so the one control you need while a request is pending was the one that
     * disappeared. The bar docks to the bottom of the transcript instead, and
     * only the transcript gets shorter. */
    /* The toolbar sits between the input row and the status bar, so the
     * input row DOES move now -- see app/toolbar.h for why that is a
     * deliberate amendment to the rule above and not a re-run of the bug it
     * was written for. Stop is positioned from this same `y`, so it moves
     * with the row rather than being left behind by it. */
    y = h - INPUT_HEIGHT - GUTTER - (g_toolbar ? TOOLBAR_H : 0);
    int pill_h = (g_pillbar && g_pillbar->active()) ? PILL_H : 0;
    int y_pill = y - pill_h;
    ybar = (g_bar && g_bar->active()) ? (y_pill - PERM_H - GUTTER) : y_pill;

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
        if (g_pillbar && g_pillbar->active())
            g_pillbar->move(cx, y_pill, cw);
        /* The input box, Send and Stop position their own three controls
         * now -- app/input.cpp, see app/input.h. */
        if (g_inputmod) g_inputmod->move(cx, y, cw, w);
        /* In the TRANSCRIPT column, not across the full width: the chat
         * list's Refresh button sits at the bottom of the left column and a
         * full-width bar draws straight over it. */
        if (g_toolbar) {
            g_toolbar->move(cx, h - TOOLBAR_H, cw);
            /* New chat at the FAR RIGHT of the page-bar row, the opposite
             * end of the window from the settings. Same y as the page bar,
             * which positions its own six controls from the left and leaves
             * this half of the row empty. */
            g_toolbar->move_top(cx, GUTTER, cw);
        }
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
    NONCLIENTMETRICSA ncm;

    /* The menu bar FIRST, because the toolbar below borrows the Model and
     * Effort popups that build_menu creates. This used to run from WinMain
     * after CreateWindowEx returned, which left g_optmenu NULL for the whole
     * of create_children -- the toolbar took the NULL, kept it, and its two
     * dropdowns read "(model)"/"(effort)" for the rest of the session while
     * the menu bar two inches above showed Haiku ticked. SetMenu before the
     * window is shown needs no DrawMenuBar. */
    build_menu(hwnd);

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

    /* The INI's autoscroll, applied -- UNCONDITIONALLY, and after the page bar
     * exists.
     *
     * Audit 01.2 said this setting was "loaded from the INI, written back,
     * round-trip-tested by inicheck.exe, and read by nothing", gave the view
     * a set_autoscroll() to fix it, and nobody called it; it was then closed
     * a second time on the strength of this call existing. It still was not
     * true, from one missing pair of braces:
     *
     *   - set_autoscroll() sat inside the `else` above, so it ran only when
     *     boot_local() FAILED -- never on a normal launch.
     *   - set_follow() was not in the `else` at all, but ran while g_pagebar
     *     was still NULL, because rc_pagebar::create is ~30 lines below here.
     *     A no-op on either branch.
     *
     * So `autoscroll=0` in retroclaude.ini round-tripped perfectly and the
     * transcript still yanked to the bottom on every append. Toggling Follow
     * at runtime was always a separate and correct path; only the PERSISTED
     * preference was dead -- the one that has to survive a restart.
     *
     * Moved below the page bar's creation so both halves have something to
     * talk to. tests/python/guest/test_guest_config.py now reads the setting
     * back through the view rather than trusting a call site to exist. */

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
    g_pagebar = rc_pagebar::create(hwnd, inst, g_ui_font, &g_net, g_html,
                                   ui_send);

    /* NOW the persisted autoscroll can be applied -- see the long note above
     * the HTML view's creation for why it could not be done there. Both
     * halves, unconditionally, with the objects they configure in existence. */
    if (g_html)    g_html->set_autoscroll(g_cfg.autoscroll);
    if (g_pagebar) g_pagebar->set_follow(g_cfg.autoscroll);

    /* The transfer-progress counters: one instance, created here (ahead of
     * app/input.cpp below, which needs it) and destroyed in WM_DESTROY --
     * the same shape as g_html. May be NULL if `new` itself failed (see
     * core/rc_cpp.h); every use, here and in input.cpp, is guarded. */
    g_xfer = rc_xferstat::create();

    /* Attachment pill bar: sits between transcript and input box */
    rc_syncrpc_set_attachment_lookup(rc_pillbar_lookup_attachment);
    rc_syncrpc_set_attachment_done(rc_pillbar_transfer_done);
    g_pillbar = rc_pillbar::create(hwnd, inst, g_ui_font, pillbar_detached);

    /* The input box and Send/Stop: created and owned by app/input.cpp now
     * (rc_input::create logs its own check_child failures and subclasses the
     * input box itself). ui_send is main.c's "send something the user just
     * asked for", for INTERRUPT; input_blocked/input_sent are this file's
     * answers to the two policy questions that module does not own -- see
     * the callback comments on rc_input::create in app/input.h. */
    g_inputmod = rc_input::create(hwnd, inst, g_ui_font, &g_net, g_html,
                                  g_xfer, g_pillbar, ui_send,
                     input_blocked, input_sent);

    /* After the option menus, because it borrows their HMENUs and their
     * current labels rather than keeping its own copy. */
    g_toolbar = rc_toolbar::create(hwnd, inst, g_ui_font, g_optmenu);
    if (!g_toolbar) rc_log("toolbar: not created; settings stay on the menus");

    /* The export sink. Created here and not on demand so that an EXPORTDAT
     * arriving without a request -- a replayed one after a reconnect --
     * reaches something that knows to drop it rather than an unhandled frame
     * type nobody reads the log for. */
    g_export = rc_export::create(g_html);
    if (!g_export) rc_log("export: sink not created; File > Export will not "
                          "work");

    g_cache = rc_cache::create(g_exe_dir);
    if (!g_cache) rc_log("cache: not created; history will not survive a "
                         "disconnection");

    g_status = rc_statusbar::create(hwnd, inst);
    check_child("status", g_status ? g_status->hwnd() : NULL);

    /* The chat list, the bar, the page bar and the input row all got their
     * font (and, for the page bar and Stop, their initial enabled state)
     * inside rc_chats::create/rc_bar::create/rc_pagebar::create/
     * rc_input::create above. */

    /* Accept file drops from Explorer onto the window and the input box */
    {
        typedef void (WINAPI *PFN_DRAGACCEPTFILES)(HWND, BOOL);
        Dll sh(LoadLibraryA("shell32.dll"));
        PFN_DRAGACCEPTFILES accept = sh.valid()
            ? (PFN_DRAGACCEPTFILES)GetProcAddress(sh.get(), "DragAcceptFiles")
            : NULL;
        if (accept) {
            accept(hwnd, TRUE);
            if (g_inputmod && g_inputmod->get_box())
                accept(g_inputmod->get_box(), TRUE);
        }
    }

    g_reconn = rc_reconnect::create(hwnd, &g_net, &g_cfg, g_exe_dir, g_html,
                                    status_refresh, cache_restore);
    g_turn = rc_turn::create(hwnd, g_html, g_status, g_cache, g_chats,
                             ui_set_busy, status_refresh);
}

/* -------------------------------------------------------------- main proc */

static void handle_attach_path(HWND hwnd, const char *path)
{
    const char *work;
    size_t wlen = 0;
    DWORD file_sz = 0;
    HANDLE hf;

    if (!path || !*path || !g_pillbar) return;

    hf = CreateFileA(path, GENERIC_READ, FILE_SHARE_READ | FILE_SHARE_WRITE, NULL,
                     OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, NULL);
    if (hf != INVALID_HANDLE_VALUE) {
        file_sz = GetFileSize(hf, NULL);
        CloseHandle(hf);
    }

    work = rc_workdir_current();
    if (work && *work) {
        wlen = strlen(work);
    }

    /* Check if path is inside mirrored work directory */
    if (wlen > 0 && _strnicmp(path, work, wlen) == 0 &&
        (path[wlen] == '\\' || path[wlen] == '/' || path[wlen] == '\0')) {
        const char *rel_start = path + wlen;
        char rel[MAX_PATH];
        char *p;
        if (*rel_start == '\\' || *rel_start == '/') rel_start++;
        if (!*rel_start) {
            MessageBoxA(hwnd, "Cannot attach the work directory itself. Select a file inside it.",
                        "Attach File", MB_OK | MB_ICONWARNING);
            return;
        }
        rc_strf(rel, sizeof(rel), "%s", rel_start);
        for (p = rel; *p; p++) {
            if (*p == '\\') *p = '/';
        }
        g_pillbar->stage_workspace(rel, file_sz);
        layout(hwnd);
        return;
    }

    /* Outside work directory: attach as external file */
    {
        static unsigned s_ext_seq = 0;
        char att_id[16];
        const char *fname = strrchr(path, '\\');
        if (!fname) fname = strrchr(path, '/');
        fname = fname ? (fname + 1) : path;

        rc_strf(att_id, sizeof(att_id), "att_%u", ++s_ext_seq);
        g_pillbar->stage_external(att_id, path, fname, file_sz, 0 /* not temp */);
        layout(hwnd);
    }
}

static LRESULT CALLBACK wnd_proc(HWND hwnd, UINT msg, WPARAM wp, LPARAM lp)
{
    switch (msg) {
    case WM_CREATE:
        create_children(hwnd, ((LPCREATESTRUCT)lp)->hInstance);
        PostMessage(hwnd, WM_RC_CONNECT, 0, 0);
        return 0;

    case WM_DROPFILES: {
        HDROP hDrop = (HDROP)wp;
        typedef UINT (WINAPI *PFN_DRAGQUERYFILEA)(HDROP, UINT, LPSTR, UINT);
        typedef void (WINAPI *PFN_DRAGFINISH)(HDROP);
        Dll sh(LoadLibraryA("shell32.dll"));
        PFN_DRAGQUERYFILEA query = sh.valid()
            ? (PFN_DRAGQUERYFILEA)GetProcAddress(sh.get(), "DragQueryFileA")
            : NULL;
        PFN_DRAGFINISH finish = sh.valid()
            ? (PFN_DRAGFINISH)GetProcAddress(sh.get(), "DragFinish")
            : NULL;
        if (query && finish) {
            char path[MAX_PATH];
            if (query(hDrop, 0, path, sizeof(path)) > 0)
                handle_attach_path(hwnd, path);
            finish(hDrop);
        }
        return 0;
    }

    case WM_RC_CONNECT:
        if (g_reconn) g_reconn->try_connect();
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

    /* Double-clicking the connection pane opens the Server Host dialog.
     * Double-clicking the thing that tells you where you are connected to,
     * to change where you are connected to, is the shortest path anyone will
     * look for, and `NMMOUSE::dwItemSpec` names the part -- 0 is the
     * "10.0.2.2:7890" pane.
     *
     * MEASURED, because this was written down as broken and was not. On a
     * real double-click a Windows 98 status bar sends NM_CLICK and then
     * NM_DBLCLK, 110ms apart, exactly as documented. What was broken was the
     * testing: `tools/vm/qmp.py click X Y double` silently ignored the third
     * word -- the CLI verb is `dclick` -- so every attempt to exercise this
     * was a single click, and a working feature was recorded as a defect and
     * then "fixed" twice. The tool accepts both spellings now.
     *
     * A subclass catching WM_LBUTTONDBLCLK also works and was tried; it is
     * not here because it is a subclass, and the two shutdown crashes this
     * app has had were both subclass procs outliving what they reached
     * through. */
    case WM_NOTIFY: {
        NMHDR *nh = (NMHDR *)lp;
        if (g_toolbar && g_toolbar->notify(nh)) return 0;
        if (g_status && g_status->notify(nh, hwnd)) return 0;
        break;
    }

    case WM_RC_OPENCHAT:
        if (g_chats) g_chats->open_selected();
        return 0;

    case WM_TIMER:
        if (g_reconn && g_reconn->handle_timer(wp)) return 0;
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
            /* And the export sink, which has exactly the same latch.
             *
             * `fp` is cleared only by EXPORTEND, by give_up() on an immediate
             * send failure, or by ~rc_export at WM_DESTROY -- nothing reset it
             * on a disconnect. A link that dropped after EXPORTSTART and
             * before EXPORTEND, which on a slow wire is the expected way a
             * few-hundred-KB transfer ends, left `fp` open for the life of the
             * process: every later File > Export answered "An export is
             * already in progress." and did nothing, while a FILE* stayed open
             * on the .$$$ and that file stayed on disk.
             *
             * The sync PUT three lines above was given this treatment and the
             * export sink was not, which is the whole of the bug. */
            if (g_export) g_export->give_up();
            /* And the two other things a CUT BATCH leaves latched.
             *
             * An OPTION batch is six frames wide (models, efforts, work,
             * modes, commands, OPTIONSEND) and a SESSIONS batch is as wide as
             * the chat list -- both are ordinary places for a slow link to
             * drop. The menus then appended the resent batch instead of
             * replacing it, and the chat list kept WM_SETREDRAW(FALSE) down
             * with its pixels and its backing array disagreeing. Same family
             * as the bar and the sync PUT above; same place to fix it. */
            if (g_optmenu) g_optmenu->reset();
            if (g_chats) g_chats->abort_rows();
            if (g_turn) g_turn->abort();
            ui_set_busy(0);
            if (g_reconn) g_reconn->schedule();
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
        case 1030: case 1031: case 1032: case 1033:
        case 1034: case 1035: case 1036: case 1037:
            if (g_pillbar) g_pillbar->command(id);
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
        /* The toolbar's own buttons, through one call -- same shape as the
         * bar's and the page bar's above.
         *
         * EVERY dropdown is here as well as in WM_NOTIFY, because a click on
         * a button's TEXT arrives as a WM_COMMAND and only the arrow sends
         * TBN_DROPDOWN (see rc_toolbar::command). Leaving a new one out of
         * this list is SILENT: the arrow works, the label does nothing, and
         * it looks like the menu failed to build. That has now happened
         * twice -- Commands, then Folder and Attach. */
        case ID_TB_MODE:
        case ID_TB_MODEL:
        case ID_TB_EFFORT:
        case ID_TB_ATTACH:
        case ID_TB_CMD:
        case ID_TB_WORK:
        case ID_TB_NEW:
            if (g_toolbar) g_toolbar->command(id);
            return 0;
        case ID_FOLLOW:
            /* The autoscroll toggle, beside the page controls. The BUTTON
             * owns the state -- it is BS_AUTOCHECKBOX, so Windows has
             * already flipped it by the time this runs -- and everything
             * else follows from reading it back, so the button and the
             * setting cannot disagree about which is true. */
            if (g_pagebar) {
                g_cfg.autoscroll = g_pagebar->follow_on();
                if (g_html) g_html->set_autoscroll(g_cfg.autoscroll);
                if (!rc_config_save(&g_cfg, g_exe_dir) && g_html)
                    g_html->error("That setting could not be saved to "
                                  "retroclaude.ini and will not survive a "
                                  "restart. The drive may be full or "
                                  "write-protected.");
                rc_log("autoscroll: %s", g_cfg.autoscroll ? "on" : "off");
                /* Following again means catching up, not waiting for the
                 * next message to arrive before it means anything. */
                if (g_cfg.autoscroll && g_html) g_html->scroll_bottom();
            }
            return 0;

        case ID_REFRESH: if (g_chats) g_chats->request(); return 0;

        case IDM_NEW:
            if ((g_turn && g_turn->active())
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
            /* NO FOLDER IS ASKED FOR. This used to put up a dialog, and
             * it was the last piece of a design that has been replaced twice
             * over: a folder is named after the conversation, and the CLI
             * titles a conversation from its first message, so the prompt
             * asked the user to invent a name before there was anything to
             * base it on -- and then to live with it, because at the time it
             * could not be changed afterwards.
             *
             * Both halves of that are now false. The folder is committed at
             * the FIRST TOOL CALL and named from the first message, and
             * "Change work directory..." moves an existing one, files and
             * all. The bridge stopped reading the `folder` header when that
             * landed, so by the end this dialog was asking a question and
             * discarding the answer, which is worse than not asking. */
            SetWindowTextA(g_main, RC_TITLE "  -  new session...");
            ui_send("Starting a new conversation", "NEWSESSION",
                    NULL, NULL, 0, NULL, 0);
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

        case IDA_MODENEXT:
            /* Not guarded on the input box having focus, unlike PgUp/PgDn:
             * this changes a setting rather than taking a key away from
             * something you are in the middle of, and it loses nothing. */
            if (g_optmenu) g_optmenu->cycle_mode();
            return 0;

        case IDM_EXPORT:
            /* The destination is chosen BEFORE the request goes out -- see
             * app/export.h for why the obvious order does not work. If the
             * request then fails to reach the bridge, the file that was just
             * opened has to go with it, or the next export would refuse to
             * start against a sink that is holding a path nobody asked for. */
            if (g_export && g_export->begin(hwnd, "CHAT.HTM")) {
                if (!ui_send("The export request", "EXPORT",
                             NULL, NULL, 0, NULL, 0))
                    g_export->give_up();
            }
            return 0;

        case IDM_PERMS:
            /* Answered by the BRIDGE, because the answer is about the
             * session it is running -- which mode, which tools, which folder
             * -- and half of it (what Claude Code resolves locally and never
             * reports) is knowledge this side does not have and must not
             * invent. */
            ui_send("The permissions view", "RULES", NULL, NULL, 0, NULL, 0);
            return 0;

        case IDM_WORKOPEN: {
            const char *cur = rc_workdir_current();
            if (cur && *cur) {
                typedef HINSTANCE (WINAPI *PFN_SHEXEC)(HWND, LPCSTR, LPCSTR, LPCSTR, LPCSTR, INT);
                Dll sh(LoadLibraryA("shell32.dll"));
                PFN_SHEXEC go = sh.valid()
                    ? (PFN_SHEXEC)GetProcAddress(sh.get(), "ShellExecuteA")
                    : NULL;
                if (go)
                    go(hwnd, "open", cur, NULL, NULL, SW_SHOWNORMAL);
                else
                    MessageBoxA(hwnd, cur, "Work Directory", MB_OK | MB_ICONINFORMATION);
            }
            return 0;
        }

        case IDM_ATTFILE: {
            typedef BOOL (WINAPI *PFN_GETOPENFILENAMEA)(LPOPENFILENAMEA);
            OPENFILENAMEA ofn;
            char file[MAX_PATH];
            const char *work = rc_workdir_current();

            file[0] = '\0';
            memset(&ofn, 0, sizeof(ofn));
            ofn.lStructSize = sizeof(ofn);
            ofn.hwndOwner = hwnd;
            ofn.lpstrFilter = "All Files (*.*)\0*.*\0Images (*.bmp;*.pcx;*.tga;*.tif;*.png;*.jpg;*.gif)\0*.bmp;*.dib;*.pcx;*.tga;*.tif;*.tiff;*.png;*.jpg;*.jpeg;*.gif;*.ico\0Text / Code (*.c;*.h;*.txt;*.md;*.ini)\0*.c;*.h;*.txt;*.md;*.py;*.json;*.ini;*.bat\0\0";
            ofn.lpstrFile = file;
            ofn.nMaxFile = sizeof(file);
            ofn.lpstrInitialDir = (work && *work) ? work : "C:\\";
            ofn.lpstrTitle = "Attach file";
            ofn.Flags = OFN_FILEMUSTEXIST | OFN_PATHMUSTEXIST | OFN_HIDEREADONLY;

            {
                Dll dll(LoadLibraryA("comdlg32.dll"));
                PFN_GETOPENFILENAMEA fn = dll.valid()
                    ? (PFN_GETOPENFILENAMEA)GetProcAddress(dll.get(), "GetOpenFileNameA")
                    : NULL;
                if (fn && fn(&ofn)) {
                    handle_attach_path(hwnd, file);
                }
            }
            return 0;
        }

        case IDM_ATTSHOT: {
            char temp_dir[MAX_PATH];
            if (!GetTempPathA(sizeof(temp_dir), temp_dir))
                rc_strf(temp_dir, sizeof(temp_dir), "%s\\", g_exe_dir);
            static unsigned s_shot_seq = 0;
            char shot_path[MAX_PATH];
            rc_strf(shot_path, sizeof(shot_path), "%sretro_shot_%u.bmp", temp_dir, ++s_shot_seq);
            DWORD shot_sz = 0;
            if (!rc_screenshot_capture(shot_path, &shot_sz)) {
                MessageBoxA(hwnd, "Failed to capture desktop screenshot.", RC_TITLE, MB_OK | MB_ICONWARNING);
                return 0;
            }
            char att_id[16];
            rc_strf(att_id, sizeof(att_id), "shot_%u", s_shot_seq);
            if (g_pillbar) {
                char shot_title[64];
                rc_strf(shot_title, sizeof(shot_title), "Screenshot #%u", s_shot_seq);
                g_pillbar->stage_external(att_id, shot_path, shot_title, shot_sz, 1 /* is_temp */);
                layout(hwnd);
            }
            return 0;
        }

        case IDM_WORKSET: {
            /* CHANGE WORK DIRECTORY -- this conversation's, not a parent it
             * will sit under.
             *
             * An earlier version edited the mirrored ROOT in the INI and
             * asked for a restart, which is a different setting wearing this
             * one's name: "setting work directory sets the actual work
             * directory". So this picks a folder and PROPOSES it; the bridge
             * records it against the conversation and sends it back in the
             * next OPTION batch, which is what makes it real on this side
             * (see app/workdir.h for why the authority runs that way).
             *
             * The native browser, not a text field: it is what a Windows user
             * reaches for, and it makes a typo impossible where a typed path
             * would have quietly created one. It is reached through
             * rc_workdlg_run's Browse button when the folder being left has
             * files in it, and directly when it does not -- because a
             * question about what to do with zero files is noise.
             *
             * The COPY, if one was asked for, has already happened by the
             * time this returns. It is local to this machine and the bridge
             * has no part in it, and doing it first means the tree the
             * bridge is about to mirror is the tree the user asked for
             * rather than one that grows under it during the first pass. */
            char picked[MAX_PATH];
            if (g_optmenu)
                rc_workdlg_run(hwnd, g_inst, rc_workdir_current(),
                               RC_CAP_MAXPATH, propose_workdir,
                               picked, sizeof(picked));
            return 0;
        }

        case IDM_HOST:
            if (rc_hostdlg_run(hwnd, g_inst, &g_cfg)) {
                char line[192];
                if (!rc_config_save(&g_cfg, g_exe_dir) && g_html)
                    g_html->error("That setting could not be saved to "
                                  "retroclaude.ini and will not survive a "
                                  "restart. The drive may be full or "
                                  "write-protected.");

                /* Resetting the backoff globals is NOT enough, and a draft of
                 * this said it was.
                 *
                 * SetTimer captured its interval when the retry was armed, so
                 * after a bad host has backed off to 15 seconds, setting
                 * g_backoff_ms = 1000 changes nothing -- the armed timer
                 * still has to expire first. Fixing a typo'd host would still
                 * mean waiting out the old wait, which is the entire point of
                 * the dialog. Kill it, reset, and reconnect now. */
                if (g_reconn) g_reconn->reset();

                /* The host is user-supplied text going into an HTML
                 * document. main.cpp:930's notice already escapes it for
                 * that reason; this is the second, more direct way in. */
                if (g_html) {
                    rc_strf(line, sizeof(line), "Now connecting to %s:%d.",
                            g_cfg.host, g_cfg.port);
                    g_html->notice(line);   /* notice() escapes */
                }
                if (g_reconn) g_reconn->try_connect();
            }
            return 0;

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
        delete g_reconn;
        g_reconn = NULL;
        delete g_turn;
        g_turn = NULL;
        rc_net_stop(&g_net);
        delete g_html;
        g_html = NULL;
        delete g_bar;
        g_bar = NULL;
        delete g_chats;
        g_chats = NULL;
        delete g_inputmod;
        g_inputmod = NULL;
        delete g_pillbar;
        g_pillbar = NULL;
        delete g_xfer;
        g_xfer = NULL;
        delete g_cache;
        g_cache = NULL;
        delete g_export;
        g_export = NULL;
        delete g_toolbar;
        g_toolbar = NULL;
        delete g_optmenu;
        g_optmenu = NULL;
        delete g_pagebar;
        g_pagebar = NULL;
        delete g_status;
        g_status = NULL;
        /* Both fonts, and only once: the fallback path points both at the
         * same stock object, which must not be deleted at all. */
        if (g_ui_font_bold && g_ui_font_bold != g_ui_font)
            DeleteObject(g_ui_font_bold);
        g_ui_font_bold = NULL;
        if (g_ui_font) DeleteObject(g_ui_font);
        /* Nulled like every other handle above it. It was the one that was
         * not, and a stale HFONT outliving WM_DESTROY is a deleted GDI
         * object that still looks valid to anything that reads it. */
        g_ui_font = NULL;
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
    g_inst = inst;
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
    /* The configured root, which is NOT the same thing as the tree being
     * mirrored. Nothing is mirrored at startup and nothing is mirrored while
     * a chat is only being talked to: sharing begins when a conversation
     * commits to a folder, which is the moment its first tool runs. So the
     * live root (rc_syncrpc_set_root / rc_rpc_set_sync_root) stays empty
     * here, and app/workdir.cpp sets it when the bridge names a tree.
     *
     * This one is still recorded, because it is what HELLO reports and what
     * a conversation's folder gets created under. */
    rc_syncrpc_set_home(g_cfg.sync_root);
    if (g_cfg.sync_root[0]) {
        /* CREATE IT if it is not there, parents and all. A sync root is a
         * place the user named in the INI, not a place they are asserting
         * already exists -- the first run after setting one has no folder
         * yet, and leaving it missing left synchronisation dead with "guest
         * scan returned no_root", which is a sentence nobody should have to
         * decode to find out that a directory needed making. A typo'd drive
         * still fails, and still says so. */
        if (GetFileAttributesA(g_cfg.sync_root) == 0xFFFFFFFF) {
            if (rc_mkpath(g_cfg.sync_root))
                rc_log("sync: created %s", g_cfg.sync_root);
            else
                rc_log("sync: cannot create %s (%lu); synchronisation will "
                       "not start", g_cfg.sync_root,
                       (unsigned long)GetLastError());
        }
        /* No recovery pass here any more. An interrupted commit is staged
         * inside the tree it belongs to, and the trees are now the
         * conversation folders UNDER this root -- rc_workdir_apply recovers
         * each one as it is applied, which is both the right place and the
         * right moment (before the first scan of that tree, so it cannot
         * see the file missing and report a deletion nobody made). */
        rc_log("sync: conversations get their folders under %s",
               g_cfg.sync_root);
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

    /* build_menu moved into create_children (WM_CREATE) -- see the comment
     * there; the toolbar has to be able to see the option menus. */
    ShowWindow(g_main, show);
    UpdateWindow(g_main);

    while (GetMessageA(&msg, NULL, 0, 0) > 0) {
        /* Accelerators FIRST, and only the keys whose meaning does not depend
         * on focus -- Ctrl+N, Ctrl+M, F5, Ctrl+PgUp/PgDn. See gaston.rc.
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
