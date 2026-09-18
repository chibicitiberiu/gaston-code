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
#include "sync/syncrpc.h"
#include "sync/sync.h"

#define ID_INPUT      1002
#define ID_SEND       1003
#define ID_STOP       1004
#define ID_STATUS     1005
#define ID_LIVE       1006
#define ID_FIRST      1007
#define ID_PREV       1008
#define ID_NEXT       1009
#define ID_LAST       1010
#define ID_PAGELBL    1011
#define ID_CHATS      1012
#define ID_REFRESH    1013
/* The permission bar. Modeless, above the input box: a modal here would pump
 * messages and re-enter the transcript while Claude is mid-stream, and the
 * obvious fix -- queueing frames for the duration -- costs heap on a machine
 * measured at 28KB free after a turn. */
#define ID_PERM_TEXT  1014
#define ID_PERM_ALLOW 1015
#define ID_PERM_ALWAYS 1016
#define ID_PERM_DENY  1017

/* Menu. Model and effort entries are numbered from a base because the lists
 * come from the bridge and are not known at compile time. */
#define IDM_NEW       1099
#define IDM_EXPORT    1100
#define IDM_CLOSE     1101
#define IDM_HOST      1110
#define IDM_ABOUT     1120
#define IDM_MODEL0    1200      /* .. IDM_MODEL0 + MAX_OPTS */
#define IDM_EFFORT0   1300      /* .. IDM_EFFORT0 + MAX_OPTS */
#define MAX_OPTS      16

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
#define WM_RC_OPENCHAT (WM_APP + 20)  /* chats_proc -> wnd_proc: open a chat */

#define INPUT_HEIGHT  56
#define BUTTON_WIDTH  64
#define GUTTER        4
#define PAGEBAR_H     24
#define PGBTN_W       28
#define CHATS_W      170
#define CHAT_INDENT   10   /* session rows sit under their date header */
#define CHAT_ROW_H    15
#define PERM_H        26   /* the permission bar, when shown */
#define PERMBTN_W     58

static rc_config     g_cfg;
static rc_net        g_net;
static rc_html *g_html;
static int      g_doc_sent;     /* the bridge has given us a skeleton */
static HWND  g_main, g_input, g_send, g_stop, g_status;
static HWND  g_live, g_first, g_prev, g_next, g_last, g_pagelbl;
static HWND  g_chats, g_refresh;
static HWND  g_perm_text, g_perm_allow, g_perm_always, g_perm_deny;

/* The permission request currently on screen. PERMREQ/PERMEND are a state
 * machine keyed on this id, never a show/hide toggle: a PERMEND can arrive
 * for a request we are not showing (the bridge resolved it while a reply was
 * in flight, or a session rebuild cleared it), and toggling on that would
 * leave the bar up during a live request or hidden during one. */
static char g_perm_id[80];
static int  g_perm_pending;
/* The bar's own text, kept separately from what is on screen so PERMWAIT can
 * append a queue-depth suffix without losing the original prompt, and drop it
 * again if the queue empties. */
static char g_perm_base_text[224];
static int  g_perm_waiting;      /* other requests queued behind this one */

/* The bar has two jobs and one set of controls.
 *
 * BAR_PERM is Allow / Always / Deny for a tool call. BAR_HALT is a single
 * Continue, shown when the bridge has STOPPED the agent -- today that means a
 * conflicting edit was found, the user's copy was kept, and the agent must not
 * carry on until the user says they have finished with the file.
 *
 * One set of controls rather than two because the 16-bit USER/GDI heaps on 9x
 * are a fixed 64KB that does not scale with RAM.
 *
 * The original note here also said the two states were "mutually exclusive by
 * construction: a halted agent is not running a tool and cannot be asking for
 * permission". THAT IS FALSE, and it cost both of them. A halt is raised by a
 * sync pass, which runs on the guest's watcher and owes nothing to the agent's
 * turn -- so a conflict is found precisely WHILE the agent works, because the
 * agent writing files is what moves the tree. Each show() then overwrote the
 * other's state:
 *
 *   PERMREQ over a halt -- the mode became BAR_PERM, so Continue answered the
 *   permission instead, and PERMEND then cleared g_halt_id while the bridge
 *   still held the halt. No further bar could be raised (the bridge's raise is
 *   idempotent while one is outstanding) and RESUME could never be sent, so
 *   every latched path refused the agent's writes forever.
 *
 *   HALT over a permission -- g_perm_pending was cleared, so Allow sent RESUME
 *   and the permission request was never answered at all, leaving the model
 *   waiting on a prompt nobody could reply to until the bridge's deadline.
 *
 * So the halt is REMEMBERED rather than shown-or-lost. A permission request
 * takes the bar, because it is the one with a deadline; the halt waits behind
 * it and comes back when it clears. */
/* The longest message that can be composed and sent in one go. The frame
 * protocol would carry far more, but do_send builds it on the stack and the
 * guard page on 9x is not something to find out about the hard way. Enforced
 * at the control with EM_LIMITTEXT and checked again at send. */
#define RC_INPUT_MAX 4096

#define BAR_NONE 0
#define BAR_PERM 1
#define BAR_HALT 2
static int  g_bar_mode;
static char g_halt_id[80];
/* The halt's text, kept so the bar can be put BACK after a permission request
 * borrows it. Without this there is nothing to redisplay. */
static char g_halt_text[256];

/* Session ids, parallel to the list box. A ListBox rather than a ListView
 * deliberately: fewer GDI objects, and the 16-bit USER/GDI heaps on 9x are a
 * fixed 64KB that does not scale with RAM. */
#define MAX_CHATS 64
static char g_chat_ids[MAX_CHATS][64];
/* 0 = a session row, 1 = a date-group header. Headers are drawn differently
 * and refuse to be opened. */
static char g_chat_is_header[MAX_CHATS];
static char g_chat_group[40];       /* last group emitted, to spot changes */
static int  g_chat_current = -1;    /* row of the conversation that is open */

/* The Model and Effort menus are built from what the bridge sends, so the
 * lists can change without rebuilding this binary. */
static HMENU g_menu_model, g_menu_effort;
static char  g_opt_model[MAX_OPTS][64];
static char  g_opt_effort[MAX_OPTS][64];
static int   g_n_model, g_n_effort;
static int   g_opts_open;   /* mid-batch, so the next model row appends */

/* Transfer progress. The byte counters used to sit in the status bar all the
 * time as a running total, which told you nothing: the interesting question is
 * whether something is moving RIGHT NOW and how much of it is left. These are
 * shown while a transfer is in flight and cleared when it goes quiet.
 *
 * Two shapes, because there are two kinds of traffic. A page load arrives in
 * several chunks and has a known size, so it shows as done/total. Streamed
 * text arrives as many small frames with no total worth speaking of, so it
 * shows as a running count for the burst. */
static unsigned long g_rx_done, g_rx_total;
static unsigned long g_tx_done, g_tx_total;
static DWORD         g_xfer_ms;
static int  g_chat_count = 0;

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
static void fit_label(char *label, unsigned cap, int avail_px);
static int  chats_text_width(void);
static void turn_receiving(void);
static int  ui_send(const char *what, const char *type,
                    const char *const *keys, const char *const *vals,
                    int nkv, const void *body, unsigned body_len);

/* Pagination state.
 *
 * The control holds exactly ONE page, which is what bounds memory by page size
 * rather than by conversation length -- the whole reason a 32MB box can run
 * this. g_view == g_total means LIVE: new text appends as it streams. On any
 * earlier page nothing appends at all, which also removes the
 * append-while-scrolled-up case that RichEdit handles worst. */
static int   g_view = 1;       /* page currently displayed */
static int   g_total = 1;      /* pages that exist */
static int   g_is_live = 1;
static int   g_loading_page = 0;
static HFONT g_ui_font;
static HFONT g_ui_font_bold;
static WNDPROC g_input_oldproc;
static WNDPROC g_chats_oldproc;
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

/* Bytes, readably. Under 10K the exact figure is the interesting one -- "rx
 * 412/900" says something that "rx 0k/0k" does not. */
static void fmt_bytes(char *out, unsigned cap, unsigned long n)
{
    if (n < 10240) rc_strf(out, cap, "%lu", n);
    else           rc_strf(out, cap, "%luk", n / 1024);
    out[cap - 1] = '\0';
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
    /* Only while something is actually moving. A transfer is "over" when
     * nothing has arrived for a moment, which is also what makes a stalled
     * one visible: the numbers stop but the pane does not clear instantly. */
    if (GetTickCount() - g_xfer_ms < 1200) {
        char rx[64], tx[64], a[16], b[16];
        rx[0] = tx[0] = '\0';
        if (g_rx_total) {
            fmt_bytes(a, sizeof(a), g_rx_done);
            fmt_bytes(b, sizeof(b), g_rx_total);
            rc_strf(rx, sizeof(rx), "rx %s/%s", a, b);
        } else if (g_rx_done) {
            fmt_bytes(a, sizeof(a), g_rx_done);
            rc_strf(rx, sizeof(rx), "rx %s", a);
        }
        if (g_tx_total) {
            fmt_bytes(a, sizeof(a), g_tx_done);
            fmt_bytes(b, sizeof(b), g_tx_total);
            rc_strf(tx, sizeof(tx), "%stx %s/%s", rx[0] ? "  " : "", a, b);
        } else if (g_tx_done) {
            fmt_bytes(a, sizeof(a), g_tx_done);
            rc_strf(tx, sizeof(tx), "%stx %s", rx[0] ? "  " : "", a);
        }
        rc_strf(buf, sizeof(buf), " %s%s", rx, tx);
        status_set(1, buf);
    } else {
        g_rx_done = g_rx_total = g_tx_done = g_tx_total = 0;
        status_set(1, "");
    }

    /* Tick the elapsed seconds while a turn is in flight. A counter that moves
     * is the difference between "working" and "hung", and it costs one
     * SB_SETTEXT a second rather than touching the transcript. */
    if (g_turn_active && g_turn_start_ms) {
        rc_strf(buf, sizeof(buf), " %s %lus",
                  g_turn_state == TURN_STREAMING
                      ? "receiving..." : "Claude is thinking...",
                  (unsigned long)((GetTickCount() - g_turn_start_ms) / 1000));
        status_set(2, buf);
    }
}

static void page_update_ui(void)
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

static void page_goto(const char *which)
{
    const char *keys[1]; const char *vals[1];
    keys[0] = "page"; vals[0] = which;
    rc_net_send(&g_net, "GETPAGE", keys, vals, 1, NULL, 0);
}

static void page_goto_n(int n)
{
    char buf[16];
    if (n < 1) n = 1;
    if (n > g_total) n = g_total;
    rc_strf(buf, sizeof(buf), "%d", n);
    page_goto(buf);
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

    g_menu_model = CreatePopupMenu();
    g_menu_effort = CreatePopupMenu();
    AppendMenuA(g_menu_model, MF_STRING | MF_GRAYED, 0, "(not connected)");
    AppendMenuA(g_menu_effort, MF_STRING | MF_GRAYED, 0, "(not connected)");

    AppendMenuA(help, MF_STRING, IDM_ABOUT, "&About...");

    AppendMenuA(bar, MF_POPUP, (UINT_PTR)file, "&File");
    AppendMenuA(bar, MF_POPUP, (UINT_PTR)cfg, "&Configure");
    AppendMenuA(bar, MF_POPUP, (UINT_PTR)g_menu_model, "&Model");
    AppendMenuA(bar, MF_POPUP, (UINT_PTR)g_menu_effort, "&Effort");
    AppendMenuA(bar, MF_POPUP, (UINT_PTR)help, "&Help");
    SetMenu(hwnd, bar);
}

/* Empty a popup without destroying it: the menu bar holds the handle. */
static void clear_popup(HMENU m)
{
    while (DeleteMenu(m, 0, MF_BYPOSITION))
        ;
}

static void chats_request(void)
{
    rc_net_send(&g_net, "GETSESSIONS", NULL, NULL, 0, NULL, 0);
}

static void chats_switch(void)
{
    int sel = (int)SendMessageA(g_chats, LB_GETCURSEL, 0, 0);
    const char *keys[1]; const char *vals[1];

    if (sel == LB_ERR || sel < 0 || sel >= g_chat_count) return;
    if (g_chat_is_header[sel]) return;          /* a date header, not a chat */
    if (strcmp(g_chat_ids[sel], g_net.sess) == 0) return;   /* already open */

    /* Switching closes the live client, which kills a running request. Ask
     * rather than doing it silently -- a click in the list is easy to make by
     * accident and the reply that is lost took real time to produce. */
    if (g_turn_active
        && MessageBoxA(g_main,
                       "A request is still running.\n\n"
                       "Opening another conversation will stop it. Continue?",
                       RC_TITLE, MB_YESNO | MB_ICONQUESTION) != IDYES) {
        /* Put the highlight back on the conversation that is actually open. */
        if (g_chat_current >= 0)
            SendMessageA(g_chats, LB_SETCURSEL, g_chat_current, 0);
        return;
    }

    keys[0] = "id"; vals[0] = g_chat_ids[sel];
    rc_log("switching to session %s", g_chat_ids[sel]);
    /* Loading a conversation means closing the live client, resuming, and
     * replaying its history, which is not instant. Say so rather than looking
     * frozen. */
    SetWindowTextA(g_main, RC_TITLE "  -  loading...");
    status_set(2, " loading conversation...");
    ui_send("Switching conversation", "SWITCH", keys, vals, 1, NULL, 0);
}

static void ui_set_busy(int busy)
{
    g_turn_active = busy;
    EnableWindow(g_stop, busy ? TRUE : FALSE);
    /* A pending permission request blocks Send too. That is the one property
     * a modal dialog would have given us: the request cannot be ignored.
     * Without the OR here a TURN end frame arriving mid-prompt would re-enable
     * Send behind our back.
     *
     * Stop stays enabled deliberately. It is the only escape if the CLI itself
     * has wedged, and it is how the cancellation path gets exercised at all --
     * the bridge answers an interrupted prompt with PERMEND reason=cancelled,
     * which clears the bar. */
    EnableWindow(g_send, (busy || g_perm_pending) ? FALSE : TRUE);
}

/* ------------------------------------------------------- permission bar */

/* Named bar_layout, not perm_layout: it re-runs the whole window layout for
 * either bar, PERM or HALT, not just the permission half. See audit finding
 * 01.21 -- bar_hide() already used the shared prefix and this one did not. */
static void bar_layout(void)
{
    /* The bar changes the transcript's height, and the HTML view has no HWND
     * to resize -- move() is the only lever it has, so the whole layout
     * must be re-run rather than just showing the controls. */
    if (g_main) layout(g_main);
}

static void bar_hide(void)
{
    if (!g_bar_mode) return;
    /* Clear only what THIS bar owned. Clearing both is how a permission
     * request ending took the halt down with it, leaving the bridge holding a
     * halt the user could never answer. */
    if (g_bar_mode == BAR_HALT) g_halt_id[0] = '\0';
    g_bar_mode = BAR_NONE;
    g_perm_pending = 0;
    g_perm_id[0] = '\0';
    g_perm_waiting = 0;
    ShowWindow(g_perm_text,   SW_HIDE);
    ShowWindow(g_perm_allow,  SW_HIDE);
    ShowWindow(g_perm_always, SW_HIDE);
    ShowWindow(g_perm_deny,   SW_HIDE);
    bar_layout();
    ui_set_busy(g_turn_active);
}

static void halt_show(const char *id, const char *text);

static void perm_hide(void)
{
    if (g_bar_mode != BAR_PERM) return;
    bar_hide();
    /* A halt that arrived while this bar was up was remembered, not shown.
     * Put it back now, or the bridge is left holding a halt with nothing on
     * screen to answer it and every latched path refuses the agent forever. */
    if (g_halt_id[0]) halt_show(g_halt_id, g_halt_text);
}

/* The agent has been stopped and needs the user to say "carry on". */
static void halt_show(const char *id, const char *text)
{
    if (!id || !*id) return;
    rc_strf(g_halt_id, sizeof(g_halt_id), "%s", id);
    rc_strf(g_halt_text, sizeof(g_halt_text), "%s", text && *text
              ? text : "Stopped: a conflicting edit was found");

    /* A permission request is on screen and has a deadline; this does not.
     * Keep the id and the text and come back when that bar clears -- taking
     * the bar here would clear g_perm_pending, so Allow would send RESUME and
     * the tool call would never be answered at all. */
    if (g_bar_mode == BAR_PERM) return;

    g_bar_mode = BAR_HALT;
    g_perm_pending = 0;

    SetWindowTextA(g_perm_text, text && *text
                   ? text : "Stopped: a conflicting edit was found");
    /* The SAME button, relabelled, which is why perm_show sets it back. */
    SetWindowTextA(g_perm_allow, "Continue");
    ShowWindow(g_perm_text,   SW_SHOW);
    ShowWindow(g_perm_allow,  SW_SHOW);
    ShowWindow(g_perm_always, SW_HIDE);
    ShowWindow(g_perm_deny,   SW_HIDE);
    bar_layout();
    ui_set_busy(g_turn_active);
    SetFocus(g_perm_allow);
}

static void halt_hide(void)
{
    if (g_bar_mode != BAR_HALT) return;
    bar_hide();
}

/* Puts g_perm_base_text on screen, with a queue-depth suffix from PERMWAIT
 * when there is one to show. One place for this, so perm_show (which sets the
 * base text) and the PERMWAIT handler (which only ever changes the count)
 * cannot disagree about what is currently displayed. */
static void perm_update_label(void)
{
    char buf[256];
    if (g_perm_waiting > 0)
        rc_strf(buf, sizeof(buf), "%s  (+%d waiting)",
                  g_perm_base_text, g_perm_waiting);
    else
        rc_strf(buf, sizeof(buf), "%s", g_perm_base_text);
    SetWindowTextA(g_perm_text, buf);
}

static void perm_show(const char *id, const char *text, int allows)
{
    if (!id || !*id) return;
    rc_strf(g_perm_id, sizeof(g_perm_id), "%s", id);
    g_perm_pending = 1;
    g_bar_mode = BAR_PERM;
    g_perm_waiting = 0;

    /* Put the label back: halt_show borrows this button and renames it. */
    SetWindowTextA(g_perm_allow, "Allow");
    rc_strf(g_perm_base_text, sizeof(g_perm_base_text), "%s",
              text && *text ? text : "Permission required");
    perm_update_label();
    ShowWindow(g_perm_text,   SW_SHOW);
    ShowWindow(g_perm_allow,  SW_SHOW);
    /* "Always" is offered only when the bridge says a rule can be expressed.
     * For a machine tool it never can -- MCP rules cannot carry specifiers, so
     * the only rule available is the whole tool, and pressing it once would
     * hand over the machine for the rest of the session. */
    ShowWindow(g_perm_always, allows ? SW_SHOW : SW_HIDE);
    ShowWindow(g_perm_deny,   SW_SHOW);
    bar_layout();
    ui_set_busy(g_turn_active);
    SetFocus(g_perm_allow);
}

/* Send something the USER just asked for, and say so if it did not go.
 *
 * rc_net_send returns -1 on a closed link, a full queue or an oversized
 * frame. Ignoring that turns a button press into silence: the press looks
 * accepted, nothing happens, and there is nothing on screen to explain it.
 * Three of ~20 call sites checked, and the one seven-line comment explaining
 * why it must be checked sat above the Continue button while perm_reply --
 * the same bar, the other three buttons -- did not check at all. */
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

static void perm_reply(const char *action)
{
    const char *keys[2];
    const char *vals[2];

    if (!g_perm_pending) return;
    keys[0] = "id";     vals[0] = g_perm_id;
    keys[1] = "action"; vals[1] = action;
    /* Checked, which it was not. A lost PERMREPLY leaves the model waiting on
     * a prompt nobody can answer until the bridge's deadline expires and
     * auto-denies -- and the user, who pressed Allow, is told nothing. */
    ui_send("Your permission answer", "PERMREPLY", keys, vals, 2, NULL, 0);
    /* The bar comes down on PERMEND, not here: the bridge owns the state
     * machine, and a reply can lose a race with a deadline or a disconnect. */
}

/* ------------------------------------------------------------ frame render */

static void render_frame(rc_frame *f)
{
    const char *v;

    {   /* Progress accounting, before anything else looks at the frame. A
         * chunked transfer carries total/off in decompressed bytes; anything
         * else just counts towards "something is arriving". */
        const char *tot = rc_frame_get(f, "total");
        const char *off = rc_frame_get(f, "off");
        unsigned long total = tot ? (unsigned long)atol(tot) : 0;
        g_xfer_ms = GetTickCount();
        if (tot && off && total > f->body_len) {
            /* Genuinely chunked, so there is a meaningful denominator. */
            g_rx_total = total;
            g_rx_done = (unsigned long)atol(off) + f->body_len;
            if (g_rx_done > g_rx_total) g_rx_done = g_rx_total;
        } else {
            /* One frame on its own. Counting these up over the burst is more
             * use than showing each frame's own size as a fraction of itself. */
            g_rx_total = 0;
            g_rx_done += f->body_len;
        }
        status_refresh();
    }

    if (strcmp(f->type, "READY") == 0) {
        v = rc_frame_get(f, "sess");
        if (v) rc_strf(g_net.sess, sizeof(g_net.sess), "%s", v);
        v = rc_frame_get(f, "page");  if (v) g_view = atoi(v);
        v = rc_frame_get(f, "total"); if (v) g_total = atoi(v);
        g_is_live = 1;
        page_update_ui();
        chats_request();
        g_backoff_ms = 1000;
        g_retry_noted = 0;
        v = rc_frame_get(f, "model");
        rc_log("ready: sess=%s model=%s", g_net.sess, v ? v : "?");
        if (g_html) g_html->notice("Connected to bridge.");
        /* READY carries page/total for the session as it stands, but g_doc_sent
         * (below) only installs the document skeleton once per PROCESS, not
         * once per connection -- so on a reconnect this updates the page
         * counters without the control's content agreeing with them, and the
         * window keeps showing whatever the last connection left in it under
         * the new page number. page_goto("live") re-requests the live page
         * explicitly, which clears the control (PAGESTART) and repopulates it
         * from what the bridge now has -- cheap and correct on a fresh session
         * too, where it is a no-op over an empty page. See BACKLOG.md's note
         * on this global. */
        page_goto("live");
        status_refresh();

    } else if (strcmp(f->type, "DOC") == 0) {
        /* The stylesheet and skeleton come from the bridge, so the look can be
         * changed by restarting it rather than by rebuilding and re-deploying
         * the guest binary. */
        if (!g_doc_sent) {
            g_doc_sent = 1;
            if (g_html) g_html->set_document(f->body);
            /* The control is created here rather than with the other children,
             * because it needs the skeleton first -- so it has missed every
             * layout so far and is still sized to whatever DoVerb was given. */
            layout(g_main);
            SetTimer(g_main, TIMER_HTMLPOLL, 100, NULL);
        }

    } else if (strcmp(f->type, "TEXT") == 0) {
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

    } else if (strcmp(f->type, "TOOL") == 0) {
        /* The bridge sends tool activity as a finished block now; this branch
         * only survives for older bridges. */
        turn_receiving();

    } else if (strcmp(f->type, "TURN") == 0) {
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
            chats_request();
            /* state=="error" used to fall into the same branch as a normal
             * end and render identically -- turn_error() (protocol.py) sends
             * `msg` and no `cost_usd`, so "done $?" is what a failed turn
             * looked like. Distinguished now: an error goes to the transcript
             * as an error and the status bar says so, instead of reading like
             * a quiet success. See audit finding 03.50. */
            if (state && strcmp(state, "error") == 0) {
                v = rc_frame_get(f, "msg");
                if (g_html)
                    g_html->error(v && *v ? v : "The turn ended with an error.");
                status_set(2, " error");
            } else {
                char buf[96];
                v = rc_frame_get(f, "cost_usd");
                rc_strf(buf, sizeof(buf), " done  $%s", (v && *v) ? v : "?");
                status_set(2, buf);
            }
        }

    } else if (strcmp(f->type, "OPTION") == 0) {
        const char *kind = rc_frame_get(f, "kind");
        const char *id = rc_frame_get(f, "id");
        const char *label = rc_frame_get(f, "label");
        const char *cur = rc_frame_get(f, "current");
        int on = (cur && *cur == '1');
        /* The first model row of a batch resets both menus. The bridge
         * always sends models before efforts, so this is a reliable start
         * marker and saves a frame type. */
        if (kind && strcmp(kind, "model") == 0 && g_opts_open == 0) {
            g_opts_open = 1;
            clear_popup(g_menu_model);
            clear_popup(g_menu_effort);
            g_n_model = 0;
            g_n_effort = 0;
        }
        if (kind && id && label) {
            if (strcmp(kind, "model") == 0 && g_n_model < MAX_OPTS) {
                rc_strf(g_opt_model[g_n_model], 64, "%s", id);
                AppendMenuA(g_menu_model,
                            MF_STRING | (on ? MF_CHECKED : MF_UNCHECKED),
                            IDM_MODEL0 + g_n_model, label);
                g_n_model++;
            } else if (strcmp(kind, "effort") == 0 && g_n_effort < MAX_OPTS) {
                /* A model with no effort levels gets one greyed entry saying
                 * so, rather than a menu offering five settings it does not
                 * have. */
                const char *dis = rc_frame_get(f, "disabled");
                UINT flags = MF_STRING | (on ? MF_CHECKED : MF_UNCHECKED);
                if (dis && *dis == '1') flags |= MF_GRAYED;
                rc_strf(g_opt_effort[g_n_effort], 64, "%s", id);
                AppendMenuA(g_menu_effort, flags,
                            IDM_EFFORT0 + g_n_effort, label);
                g_n_effort++;
            }
        }

    } else if (strcmp(f->type, "OPTIONSEND") == 0) {
        g_opts_open = 0;
        DrawMenuBar(g_main);

    } else if (strcmp(f->type, "SESSIONS") == 0) {
        SendMessageA(g_chats, WM_SETREDRAW, FALSE, 0);
        SendMessageA(g_chats, LB_RESETCONTENT, 0, 0);
        g_chat_count = 0;
        g_chat_group[0] = '\0';
        g_chat_current = -1;

    } else if (strcmp(f->type, "SESSION") == 0) {
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

    } else if (strcmp(f->type, "SESSIONSEND") == 0) {
        /* RDW_FRAME as well as the client: the frame is what draws the
         * client edge, and repainting only the interior leaves it stale. */
        SendMessageA(g_chats, WM_SETREDRAW, TRUE, 0);
        RedrawWindow(g_chats, NULL, NULL,
                     RDW_INVALIDATE | RDW_ERASE | RDW_FRAME | RDW_UPDATENOW);
        rc_log("chat list: %d sessions", g_chat_count);
        log_mem("chat list loaded");

    } else if (strcmp(f->type, "SWITCHED") == 0) {
        /* A prompt belongs to the session that raised it, and switching
         * rebuilds the session -- the bridge denies anything pending as
         * "rebuild". Leaving the bar up would offer Allow/Deny for a request
         * that no longer exists, on a conversation the user has left. */
        bar_hide();
        v = rc_frame_get(f, "total");
        if (v) g_total = atoi(v);
        v = rc_frame_get(f, "id");
        if (v) rc_strf(g_net.sess, sizeof(g_net.sess), "%s", v);
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
                    && strcmp(g_chat_ids[i], g_net.sess) == 0) {
                    g_chat_current = i;
                    break;
                }
            }
            InvalidateRect(g_chats, NULL, TRUE);
        }
        SetWindowTextA(g_main, RC_TITLE);
        status_set(2, "");

    } else if (strcmp(f->type, "PAGESTART") == 0) {
        v = rc_frame_get(f, "page");  if (v) g_view = atoi(v);
        v = rc_frame_get(f, "total"); if (v) g_total = atoi(v);
        v = rc_frame_get(f, "live");  g_is_live = (v && *v == '1');
        g_loading_page = 1;
        g_turn_state = TURN_IDLE;      /* the control is about to be cleared */
        /* One page in, one page out: clear before rendering, so the control
         * never holds two. */
        if (g_html) g_html->clear();
        v = rc_frame_get(f, "continued");
        if (v && *v == '1' && g_html)
            g_html->notice("continued from the previous page");
        page_update_ui();

    } else if (strcmp(f->type, "PAGEEND") == 0) {
        if (g_loading_page) { g_loading_page = 0; if (g_html) g_html->scroll_bottom(); }
        page_update_ui();
        log_mem("page loaded");

    } else if (strcmp(f->type, "PAGESEALED") == 0) {
        v = rc_frame_get(f, "total"); if (v) g_total = atoi(v);
        /* On the live page, follow the seal forward. On an older page, the
         * counter simply ticks up -- that IS the new-content notification. */
        if (g_is_live) g_view = g_total;
        page_update_ui();

    } else if (strcmp(f->type, "ANCHOR") == 0) {
        const char *page = rc_frame_get(f, "page");
        const char *preview = rc_frame_get(f, "preview");
        rc_log("anchor: page=%s preview=%s", page ? page : "?",
               preview ? preview : "");

    } else if (strcmp(f->type, "PERMREQ") == 0 ||
               strcmp(f->type, "PERMSTATE") == 0) {
        /* PERMSTATE is the same request re-issued after a reconnect. The
         * guest never sends `from=`, so the bridge's replay buffer cannot
         * bring the bar back by itself. */
        const char *id = rc_frame_get(f, "id");
        const char *allows = rc_frame_get(f, "allows");
        perm_show(id ? id : "", f->body ? f->body : "",
                  allows && *allows == '1');

    } else if (strcmp(f->type, "HALT") == 0) {
        /* The bridge has stopped the agent and wants the user to decide when
         * it may go on. Today that is a conflicting edit: their copy was kept,
         * the agent's was preserved, and it must not touch the file again
         * until they have finished with it.
         *
         * The explanation is already in the transcript as a NOTICE -- this is
         * only the button, so the text here is one short line rather than the
         * whole story, which would not fit the bar at 640x480 anyway. */
        const char *id = rc_frame_get(f, "id");
        halt_show(id ? id : "", f->body ? f->body : "");

    } else if (strcmp(f->type, "HALTEND") == 0) {
        /* Keyed on the id for the same reason PERMEND is: the bridge can
         * resolve a halt on its own -- the agent called sync_resolve, or the
         * session was rebuilt -- while the bar is still up. */
        const char *id = rc_frame_get(f, "id");
        if (id && g_halt_id[0] && strcmp(id, g_halt_id) == 0) {
            if (g_bar_mode == BAR_HALT) {
                halt_hide();
            } else {
                /* Remembered behind a permission bar rather than shown. Forget
                 * it, or perm_hide would put up a bar for a halt that has
                 * already been answered. */
                g_halt_id[0] = '\0';
                g_halt_text[0] = '\0';
            }
        }

    } else if (strcmp(f->type, "PERMWAIT") == 0) {
        /* Only the queue depth changed; the request is the same one. Was
         * logged only -- never shown to the person actually waiting on the
         * decision, although PLAN.md's modeless-bar design implies it should
         * be. Now appended to the permission bar itself via
         * perm_update_label(). See audit finding 03.30. */
        const char *id = rc_frame_get(f, "id");
        const char *n = rc_frame_get(f, "waiting");
        if (id && g_perm_pending && strcmp(id, g_perm_id) == 0 && n) {
            rc_log("perm: %s more request(s) queued", n);
            g_perm_waiting = atoi(n);
            if (g_perm_waiting < 0) g_perm_waiting = 0;
            perm_update_label();
        }

    } else if (strcmp(f->type, "PERMEND") == 0) {
        /* Keyed on the id, never a blind hide: a PERMEND can arrive for a
         * request we are not showing -- resolved by a deadline, a disconnect
         * or a session rebuild while a reply was in flight -- and hiding on
         * that would drop the bar during a live request. */
        const char *id = rc_frame_get(f, "id");
        if (id && g_perm_pending && strcmp(id, g_perm_id) == 0)
            perm_hide();
        else if (id)
            rc_log("perm: PERMEND for %s, not the one on screen", id);

    } else if (strcmp(f->type, "ERROR") == 0) {
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

    } else if (strcmp(f->type, "NOTICE") == 0) {
        if (g_html) g_html->notice(f->body);

    } else if (strcmp(f->type, "RESYNC") == 0) {
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

    } else if (strcmp(f->type, "PONG") == 0) {
        /* The answer to a PING. We do not send PING yet -- see BACKLOG -- so
         * this cannot arrive today, but handling it here means the day the
         * keepalive lands it does not read as a protocol error. Nothing to
         * show: receiving it IS the information, and the reader thread has
         * already stamped last_seen by getting this far. */
        rc_log("pong");

    } else {
        rc_log("unhandled frame type: %s", f->type);
    }
}

/* ------------------------------------------------------------------ input */

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
    old = (HFONT)SelectObject(dc, g_ui_font);
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
}

/* Echo the user's own line, meant to match bridge/view/html.py's user_turn()
 * (not bridge/markdown.py -- that module is a .bak and user_block() does not
 * exist): the bridge records that same string into the paginator so that
 * paging back to a sealed page still shows the question above the answer.
 * Echoing locally rather than waiting for the bridge to send it back is what
 * keeps typing responsive on a dialup link.
 *
 * They do NOT in fact match: user_turn() does text.strip().splitlines()
 * before escaping (html.py:102), and this side's esc_into() (htmlview.cpp)
 * escapes the raw text without stripping -- so a message with a trailing
 * blank line renders one line taller here than it does in the paginator-
 * sealed copy shown after paging back. Recorded as audit finding 01.29;
 * left as found rather than fixed here. */
static void echo_user_block(const char *text)
{
    /* Was twenty lines of escaping and heap juggling here, with a buffer that
     * silently cut the echo at 4095 bytes while the full text went to the
     * model. The view owns its markup now, and sizes from the input. */
    if (g_html) g_html->user(text);
}

static void do_send(void)
{
    char buf[RC_INPUT_MAX];
    int n;

    if (g_turn_active || g_perm_pending) return;

    /* Refuse rather than truncate. GetWindowTextA copies what fits and says
     * nothing, so a pasted log longer than the buffer was silently cut and the
     * SHORTENED text is what went to the model -- the person at the keyboard
     * losing bytes on the one path where they are the author. EM_LIMITTEXT
     * below normally stops it arriving at all; this is the check that makes
     * that a guarantee rather than a hope. */
    if (GetWindowTextLengthA(g_input) >= RC_INPUT_MAX) {
        if (g_html)
            g_html->error(
                      "That message is too long to send (the limit is about "
                      "4000 characters). Nothing was sent -- shorten it, or "
                      "put the long part in a file under the mirrored folder.");
        return;
    }

    n = GetWindowTextA(g_input, buf, sizeof(buf));
    if (n <= 0) return;

    echo_user_block(buf);

    g_tx_total = (unsigned long)n;
    g_tx_done = 0;
    g_xfer_ms = GetTickCount();
    if (rc_net_send(&g_net, "INPUT", NULL, NULL, 0, buf, (unsigned)n) == 0) {
        g_tx_done = (unsigned long)n;
        status_refresh();
        SetWindowTextA(g_input, "");
        ui_set_busy(1);
    } else {
        if (g_html) g_html->error("Not connected.");
    }
    SetFocus(g_input);
}

static void do_stop(void)
{
    rc_log("stop requested");
    /* A lost INTERRUPT is a Stop button that does nothing, on a turn that
     * keeps running and keeps costing. */
    ui_send("Stop", "INTERRUPT", NULL, NULL, 0, NULL, 0);
}

/* Open a conversation on a plain click.
 *
 * Not via LBN_SELCHANGE: that also fires for arrow keys, and switching on those
 * would tear down a live session every time the list was scrolled with the
 * keyboard. Testing the mouse button state inside the notification was tried
 * and is unreliable -- by the time it arrives the button may already be up.
 * Handling the button release directly is unambiguous. The switch is POSTED
 * rather than done here so the listbox finishes with the click (and releases
 * capture) before the transcript is torn down and rebuilt. WM_RC_OPENCHAT
 * itself is defined with the rest of this file's WM_APP offsets, up top. */

static LRESULT CALLBACK chats_proc(HWND h, UINT m, WPARAM w, LPARAM l)
{
    LRESULT r = CallWindowProc(g_chats_oldproc, h, m, w, l);
    if (m == WM_LBUTTONUP)
        PostMessageA(GetParent(h), WM_RC_OPENCHAT, 0, 0);
    return r;
}

/* Enter sends; Shift+Enter inserts a newline. */
static LRESULT CALLBACK input_proc(HWND h, UINT m, WPARAM w, LPARAM l)
{
    if (m == WM_KEYDOWN && w == VK_RETURN && !(GetKeyState(VK_SHIFT) & 0x8000)) {
        do_send();
        return 0;
    }
    if (m == WM_CHAR && w == VK_RETURN && !(GetKeyState(VK_SHIFT) & 0x8000))
        return 0;   /* swallow the beep */
    return CallWindowProc(g_input_oldproc, h, m, w, l);
}

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
    ybar = g_bar_mode ? (y - PERM_H - GUTTER) : y;

    {   /* Chat list down the left, with its Refresh button under it. */
        int lh = h - 2 * GUTTER - 24;
        MoveWindow(g_chats, GUTTER, GUTTER, CHATS_W, lh, TRUE);
        MoveWindow(g_refresh, GUTTER, GUTTER + lh + 2, CHATS_W, 22, TRUE);
    }

    {   /* Page bar across the top of the transcript column. */
        int x = GUTTER * 2 + CHATS_W, by = GUTTER;
        MoveWindow(g_live,  x, by, 40, PAGEBAR_H - 2, TRUE); x += 40 + GUTTER;
        MoveWindow(g_first, x, by, PGBTN_W, PAGEBAR_H - 2, TRUE); x += PGBTN_W;
        MoveWindow(g_prev,  x, by, PGBTN_W, PAGEBAR_H - 2, TRUE); x += PGBTN_W;
        MoveWindow(g_pagelbl, x, by + 4, 130, PAGEBAR_H - 4, TRUE); x += 130;
        MoveWindow(g_next,  x, by, PGBTN_W, PAGEBAR_H - 2, TRUE); x += PGBTN_W;
        MoveWindow(g_last,  x, by, PGBTN_W, PAGEBAR_H - 2, TRUE);
    }

    {
        int cx = GUTTER * 2 + CHATS_W;
        int cw = w - cx - GUTTER;
        if (g_html)
            g_html->move(cx, GUTTER + PAGEBAR_H, cw, ybar - GUTTER - PAGEBAR_H);
        if (g_bar_mode) {
            /* Right-to-left: the buttons are fixed width and the label takes
             * whatever is left, so a long tool description truncates rather
             * than pushing Deny off the edge.
             *
             * A halt has only Continue, and the hidden buttons must not be
             * given screen space -- MoveWindow on a hidden control costs
             * nothing, but leaving the gap would put Continue in the middle of
             * the bar with a hole beside it. */
            int bx = w - GUTTER - PERMBTN_W;
            if (g_bar_mode == BAR_PERM) {
                MoveWindow(g_perm_deny, bx, ybar, PERMBTN_W, PERM_H - 4, TRUE);
                bx -= PERMBTN_W + 2;
                MoveWindow(g_perm_always, bx, ybar, PERMBTN_W, PERM_H - 4, TRUE);
                bx -= PERMBTN_W + 2;
            }
            MoveWindow(g_perm_allow, bx, ybar, PERMBTN_W, PERM_H - 4, TRUE);
            MoveWindow(g_perm_text, cx, ybar + 4, bx - cx - GUTTER,
                       PERM_H - 6, TRUE);
        }
        MoveWindow(g_input, cx, y,
                   cw - GUTTER - BUTTON_WIDTH, INPUT_HEIGHT, TRUE);
    }
    MoveWindow(g_send, w - GUTTER - BUTTON_WIDTH, y, BUTTON_WIDTH, 24, TRUE);
    MoveWindow(g_stop, w - GUTTER - BUTTON_WIDTH, y + 28, BUTTON_WIDTH, 24, TRUE);
}

/* CreateWindowExA can fail, and on this platform that was never a theoretical
 * concern: the comment above g_chats cites the 16-bit USER heap's fixed 64KB
 * three times in this file as a reason a control might not be created. A NULL
 * here was previously silent -- nothing logged, and every subsequent
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
    else
        /* The INI's autoscroll, applied. Audit 01.2 said this setting was
         * "loaded from the INI, written back, round-trip-tested by
         * inicheck.exe, and read by nothing", gave the view a
         * set_autoscroll() to fix it -- and then nobody called it. The
         * finding was marked closed on the strength of the method existing.
         * One line makes the claim true. */
        g_html->set_autoscroll(g_cfg.autoscroll);

    g_input = CreateWindowExA(WS_EX_CLIENTEDGE, "EDIT", "",
        WS_CHILD | WS_VISIBLE | WS_TABSTOP | WS_VSCROLL |
        ES_MULTILINE | ES_AUTOVSCROLL | ES_WANTRETURN,
        0, 0, 0, 0, hwnd, (HMENU)ID_INPUT, inst, NULL);
    check_child("input", g_input);
    /* Stop an over-long message being composed at all, which is the only
     * feedback that arrives at the moment the user does it rather than at
     * send time. The default for a multiline EDIT is 32KB, well past what
     * do_send can carry. */
    SendMessageA(g_input, EM_LIMITTEXT, (WPARAM)(RC_INPUT_MAX - 1), 0);

    g_send = CreateWindowExA(0, "BUTTON", "Send",
        WS_CHILD | WS_VISIBLE | WS_TABSTOP | BS_DEFPUSHBUTTON,
        0, 0, 0, 0, hwnd, (HMENU)ID_SEND, inst, NULL);
    check_child("send", g_send);

    g_stop = CreateWindowExA(0, "BUTTON", "Stop",
        WS_CHILD | WS_VISIBLE | WS_TABSTOP | BS_PUSHBUTTON,
        0, 0, 0, 0, hwnd, (HMENU)ID_STOP, inst, NULL);
    check_child("stop", g_stop);
    EnableWindow(g_stop, FALSE);

    /* LBS_NOINTEGRALHEIGHT: otherwise the control silently resizes itself to
     *   a whole number of rows after every MoveWindow, so its idea of its own
     *   size and ours disagree.
     * WS_CLIPSIBLINGS: keeps neighbours out of its rectangle.
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
        0, 0, 0, 0, hwnd, (HMENU)ID_CHATS, inst, NULL);
    check_child("chats", g_chats);

    /* The permission bar. Created hidden and shown with ShowWindow rather
     * than created on demand: the 16-bit USER/GDI heaps on 9x are a fixed
     * 64KB, and churning window handles is exactly what this codebase avoids. */
    g_perm_text = CreateWindowExA(0, "STATIC", "",
        WS_CHILD | SS_LEFTNOWORDWRAP | SS_ENDELLIPSIS,
        0, 0, 0, 0, hwnd, (HMENU)ID_PERM_TEXT, inst, NULL);
    check_child("perm_text", g_perm_text);
    g_perm_allow = CreateWindowExA(0, "BUTTON", "Allow",
        WS_CHILD | BS_PUSHBUTTON,
        0, 0, 0, 0, hwnd, (HMENU)ID_PERM_ALLOW, inst, NULL);
    check_child("perm_allow", g_perm_allow);
    g_perm_always = CreateWindowExA(0, "BUTTON", "Always",
        WS_CHILD | BS_PUSHBUTTON,
        0, 0, 0, 0, hwnd, (HMENU)ID_PERM_ALWAYS, inst, NULL);
    check_child("perm_always", g_perm_always);
    g_perm_deny = CreateWindowExA(0, "BUTTON", "Deny",
        WS_CHILD | BS_PUSHBUTTON,
        0, 0, 0, 0, hwnd, (HMENU)ID_PERM_DENY, inst, NULL);
    check_child("perm_deny", g_perm_deny);

    g_refresh = CreateWindowExA(0, "BUTTON", "Refresh",
        WS_CHILD | WS_VISIBLE | BS_PUSHBUTTON, 0, 0, 0, 0,
        hwnd, (HMENU)ID_REFRESH, inst, NULL);
    check_child("refresh", g_refresh);

    g_live = CreateWindowExA(0, "BUTTON", "Live",
        WS_CHILD | WS_VISIBLE | BS_PUSHBUTTON, 0, 0, 0, 0,
        hwnd, (HMENU)ID_LIVE, inst, NULL);
    check_child("live", g_live);
    g_first = CreateWindowExA(0, "BUTTON", "|<",
        WS_CHILD | WS_VISIBLE | BS_PUSHBUTTON, 0, 0, 0, 0,
        hwnd, (HMENU)ID_FIRST, inst, NULL);
    check_child("first", g_first);
    g_prev = CreateWindowExA(0, "BUTTON", "<",
        WS_CHILD | WS_VISIBLE | BS_PUSHBUTTON, 0, 0, 0, 0,
        hwnd, (HMENU)ID_PREV, inst, NULL);
    check_child("prev", g_prev);
    g_pagelbl = CreateWindowExA(0, "STATIC", "Page 1/1",
        WS_CHILD | WS_VISIBLE | SS_CENTER, 0, 0, 0, 0,
        hwnd, (HMENU)ID_PAGELBL, inst, NULL);
    check_child("pagelbl", g_pagelbl);
    g_next = CreateWindowExA(0, "BUTTON", ">",
        WS_CHILD | WS_VISIBLE | BS_PUSHBUTTON, 0, 0, 0, 0,
        hwnd, (HMENU)ID_NEXT, inst, NULL);
    check_child("next", g_next);
    g_last = CreateWindowExA(0, "BUTTON", ">|",
        WS_CHILD | WS_VISIBLE | BS_PUSHBUTTON, 0, 0, 0, 0,
        hwnd, (HMENU)ID_LAST, inst, NULL);
    check_child("last", g_last);

    g_status = CreateWindowExA(0, STATUSCLASSNAMEA, "",
        WS_CHILD | WS_VISIBLE | SBARS_SIZEGRIP,
        0, 0, 0, 0, hwnd, (HMENU)ID_STATUS, inst, NULL);
    check_child("status", g_status);
    parts[0] = 220; parts[1] = 340; parts[2] = -1;
    SendMessageA(g_status, SB_SETPARTS, 3, (LPARAM)parts);

    SendMessageA(g_input,   WM_SETFONT, (WPARAM)g_ui_font, TRUE);
    SendMessageA(g_send,    WM_SETFONT, (WPARAM)g_ui_font, TRUE);
    SendMessageA(g_stop,    WM_SETFONT, (WPARAM)g_ui_font, TRUE);
    SendMessageA(g_live,    WM_SETFONT, (WPARAM)g_ui_font, TRUE);
    SendMessageA(g_first,   WM_SETFONT, (WPARAM)g_ui_font, TRUE);
    SendMessageA(g_prev,    WM_SETFONT, (WPARAM)g_ui_font, TRUE);
    SendMessageA(g_next,    WM_SETFONT, (WPARAM)g_ui_font, TRUE);
    SendMessageA(g_last,    WM_SETFONT, (WPARAM)g_ui_font, TRUE);
    SendMessageA(g_pagelbl, WM_SETFONT, (WPARAM)g_ui_font, TRUE);
    SendMessageA(g_chats,   WM_SETFONT, (WPARAM)g_ui_font, TRUE);
    SendMessageA(g_refresh, WM_SETFONT, (WPARAM)g_ui_font, TRUE);
    SendMessageA(g_perm_text,   WM_SETFONT, (WPARAM)g_ui_font, TRUE);
    SendMessageA(g_perm_allow,  WM_SETFONT, (WPARAM)g_ui_font, TRUE);
    SendMessageA(g_perm_always, WM_SETFONT, (WPARAM)g_ui_font, TRUE);
    SendMessageA(g_perm_deny,   WM_SETFONT, (WPARAM)g_ui_font, TRUE);
    page_update_ui();

    g_chats_oldproc = (WNDPROC)SetWindowLongA(g_chats, GWL_WNDPROC,
                                              (LONG)(LONG_PTR)chats_proc);
    g_input_oldproc = (WNDPROC)SetWindowLongA(g_input, GWL_WNDPROC,
                                              (LONG)input_proc);
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
        SetFocus(g_input);
        return 0;

    case WM_MEASUREITEM: {
        MEASUREITEMSTRUCT *mis = (MEASUREITEMSTRUCT *)lp;
        if (mis && mis->CtlID == ID_CHATS) mis->itemHeight = CHAT_ROW_H;
        return TRUE;
    }

    case WM_DRAWITEM: {
        /* Owner-draw so a date header reads as a header instead of as a
         * conversation named "Today". Everything else about the control is
         * still the stock listbox: LBS_HASSTRINGS keeps the strings, so this
         * only replaces the painting. */
        DRAWITEMSTRUCT *dis = (DRAWITEMSTRUCT *)lp;
        char text[192];
        RECT rc;
        int header, selected;
        COLORREF oldfg;
        int oldbk;

        if (!dis || dis->CtlID != ID_CHATS || (int)dis->itemID < 0)
            return TRUE;

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
            return TRUE;
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
            HFONT want = ((int)dis->itemID == g_chat_current && g_ui_font_bold)
                         ? g_ui_font_bold : g_ui_font;
            HFONT oldf = (HFONT)SelectObject(dis->hDC, want);
            DrawTextA(dis->hDC, text, -1, &rc,
                      DT_LEFT | DT_SINGLELINE | DT_VCENTER | DT_NOPREFIX);
            SelectObject(dis->hDC, oldf);
        }
        SetBkMode(dis->hDC, oldbk);
        SetTextColor(dis->hDC, oldfg);

        if (dis->itemState & ODS_FOCUS)
            DrawFocusRect(dis->hDC, &dis->rcItem);
        return TRUE;
    }

    case WM_RC_OPENCHAT:
        chats_switch();
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
             * Nothing did, and ui_set_busy ORs g_perm_pending into the Send
             * button's enabled state -- so a disconnection while a permission
             * prompt or a halt was on screen left Send disabled across the
             * whole outage AND after the reconnection, with a bar on screen
             * whose buttons answer a request the bridge has already given up
             * on. The app looked wedged.
             *
             * Safe to drop locally because the bridge restores both on
             * reattach: _reissue_prompt re-sends PERMSTATE and _reissue_halt
             * re-sends HALT, so whatever is genuinely still outstanding comes
             * back, and whatever is not, does not. */
            bar_hide();
            ui_set_busy(0);
            SetTimer(hwnd, TIMER_RECONNECT, (UINT)g_backoff_ms, NULL);
        }
        return 0;

    case WM_COMMAND: {
        int id = (int)LOWORD(wp);
        switch (id) {
        case ID_SEND:  do_send(); return 0;

        case ID_STOP:  do_stop(); return 0;
        case ID_PERM_ALLOW:
            /* One button, two meanings -- see the BAR_* note. */
            if (g_bar_mode == BAR_HALT) {
                const char *k[1]; const char *v[1];
                k[0] = "id"; v[0] = g_halt_id;
                /* The bar comes down on HALTEND, not here -- the same rule
                 * perm_reply follows, and for the same reason. rc_net_send
                 * returns -1 on a closed link, a full queue or an oversized
                 * frame, and hiding regardless meant a press that never left
                 * the machine looked answered: the bridge still held the halt,
                 * its raise is idempotent so no new bar could appear, and
                 * every latched path refused the agent's writes for good. */
                if (rc_net_send(&g_net, "RESUME", k, v, 1, NULL, 0) < 0)
                    SetWindowTextA(g_perm_text,
                                   "Not sent -- no link. Press Continue again.");
                return 0;
            }
            perm_reply("allow");
            return 0;
        case ID_PERM_ALWAYS: perm_reply("always"); return 0;
        case ID_PERM_DENY:   perm_reply("deny");   return 0;
        case ID_LIVE:  page_goto("live");      return 0;
        case ID_FIRST: page_goto_n(1);         return 0;
        case ID_PREV:  page_goto_n(g_view - 1); return 0;
        case ID_NEXT:  page_goto_n(g_view + 1); return 0;
        case ID_LAST:  page_goto_n(g_total);   return 0;
        case ID_REFRESH: chats_request();      return 0;

        case IDM_NEW:
            if (g_turn_active
                && MessageBoxA(hwnd,
                               "A request is still running.\n\n"
                               "Starting a new session will stop it. Continue?",
                               RC_TITLE, MB_YESNO | MB_ICONQUESTION) != IDYES)
                return 0;
            SetWindowTextA(g_main, RC_TITLE "  -  new session...");
            ui_send("Starting a new conversation", "NEWSESSION", NULL, NULL, 0, NULL, 0);
            return 0;

        case IDM_CLOSE:
            PostMessageA(hwnd, WM_CLOSE, 0, 0);
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
            if (HIWORD(wp) == LBN_DBLCLK) chats_switch();
            return 0;

        default:
            /* Model and effort are numbered ranges rather than fixed ids,
             * because the two lists are sent by the bridge and are not known
             * when this is compiled. */
            if (id >= IDM_MODEL0 && id < IDM_MODEL0 + g_n_model) {
                const char *keys[1], *vals[1];
                keys[0] = "model"; vals[0] = g_opt_model[id - IDM_MODEL0];
                ui_send("The model change", "SETMODEL", keys, vals, 1, NULL, 0);
                return 0;
            }
            if (id >= IDM_EFFORT0 && id < IDM_EFFORT0 + g_n_effort) {
                const char *keys[1], *vals[1];
                keys[0] = "effort"; vals[0] = g_opt_effort[id - IDM_EFFORT0];
                ui_send("The effort change", "SETEFFORT", keys, vals, 1, NULL, 0);
                return 0;
            }
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
            if (other) {
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
    if (g_cfg.sync_root[0]) {
        /* Put back any file an interrupted commit left aside, BEFORE the first
         * scan sees the path as missing and reports a deletion nobody made. */
        rc_sync_recover(g_cfg.sync_root);
        rc_log("sync: root is %s", g_cfg.sync_root);
    }

    memset(&wc, 0, sizeof(wc));
    wc.lpfnWndProc   = wnd_proc;
    wc.hInstance     = inst;
    wc.hIcon         = LoadIconA(NULL, IDI_APPLICATION);
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
    rc_log("main window hwnd=%p", (void *)g_main);
    log_mem("window created");

    build_menu(g_main);
    ShowWindow(g_main, show);
    UpdateWindow(g_main);

    while (GetMessageA(&msg, NULL, 0, 0) > 0) {
        /* Enter opens the highlighted conversation. Caught here rather than by
         * subclassing the listbox, because IsDialogMessage would otherwise
         * route it to the default pushbutton and the keyboard would have no
         * way to open a chat at all. */
        if (msg.message == WM_KEYDOWN && msg.wParam == 'N'
            && (GetKeyState(VK_CONTROL) & 0x8000)) {
            SendMessageA(g_main, WM_COMMAND, IDM_NEW, 0);
            continue;
        }
        if (msg.message == WM_KEYDOWN && msg.wParam == VK_RETURN
            && msg.hwnd == g_chats) {
            chats_switch();
            continue;
        }
        /* Esc denies a pending request. It has to be intercepted before
         * IsDialogMessageA, which eats VK_ESCAPE and would never let it
         * reach the bar. */
        if (msg.message == WM_KEYDOWN && msg.wParam == VK_ESCAPE
            && g_perm_pending) {
            perm_reply("deny");
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
