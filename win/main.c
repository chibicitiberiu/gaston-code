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

#include "frameproto.h"
#include "ini.h"
#include "log.h"
#include "net.h"
#include <ole2.h>
#include "htmlview.h"

#define ID_TRANSCRIPT 1001
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

#define INPUT_HEIGHT  56
#define BUTTON_WIDTH  64
#define GUTTER        4
#define PAGEBAR_H     24
#define PGBTN_W       28
#define CHATS_W      170
#define CHAT_INDENT   10   /* session rows sit under their date header */
#define CHAT_ROW_H    15

static rc_config     g_cfg;
static rc_net        g_net;
static rc_html *g_html;
static int      g_doc_sent;     /* the bridge has given us a skeleton */
static HWND  g_main, g_input, g_send, g_stop, g_status;
static HWND  g_live, g_first, g_prev, g_next, g_last, g_pagelbl;
static HWND  g_chats, g_refresh;

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
static int  g_chat_widest = 0;      /* px, for LB_SETHORIZONTALEXTENT */

/* Not "Claude Code": that is Anthropic's product name and this is a
 * third-party client for it. Gaston is a French given name of roughly the
 * right vintage for the machine it runs on. */
#define RC_TITLE "Gaston Code"

/* -1 means the "thinking" indicator is not currently shown. */
static long  g_think_mark = -1;
static DWORD g_turn_start_ms = 0;
/* Output has started arriving for this turn. "Thinking" and "receiving" are
 * different things to be waiting on, and saying "thinking" while text is
 * visibly streaming is just wrong. */
static int   g_streaming = 0;

static int  label_width_px(const char *text);
static void think_show(void);
static void turn_done(void);
static void esc_html(const char *in, char *out, unsigned cap);
static void layout(HWND hwnd);
static void fit_label(char *label, unsigned cap, int avail_px);
static int  chats_text_width(void);
static void think_hide(void);

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
static char  g_exe_dir[MAX_PATH];

/* ------------------------------------------------------------------ utils */

static void exe_dir(char *out, unsigned n)
{
    char path[MAX_PATH];
    char *slash;
    GetModuleFileNameA(NULL, path, sizeof(path));
    slash = strrchr(path, '\\');
    if (slash) *slash = '\0';
    _snprintf(out, n - 1, "%s", path);
    out[n - 1] = '\0';
}

/* Bytes, readably. Under 10K the exact figure is the interesting one -- "rx
 * 412/900" says something that "rx 0k/0k" does not. */
static void fmt_bytes(char *out, unsigned cap, unsigned long n)
{
    if (n < 10240) _snprintf(out, cap - 1, "%lu", n);
    else           _snprintf(out, cap - 1, "%luk", n / 1024);
    out[cap - 1] = '\0';
}

static void status_set(int pane, const char *text)
{
    if (g_status) SendMessageA(g_status, SB_SETTEXTA, (WPARAM)pane, (LPARAM)text);
}

static void status_refresh(void)
{
    char buf[128];
    _snprintf(buf, sizeof(buf), " %s  %s:%d",
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
            _snprintf(rx, sizeof(rx) - 1, "rx %s/%s", a, b);
        } else if (g_rx_done) {
            fmt_bytes(a, sizeof(a), g_rx_done);
            _snprintf(rx, sizeof(rx) - 1, "rx %s", a);
        }
        if (g_tx_total) {
            fmt_bytes(a, sizeof(a), g_tx_done);
            fmt_bytes(b, sizeof(b), g_tx_total);
            _snprintf(tx, sizeof(tx) - 1, "%stx %s/%s", rx[0] ? "  " : "", a, b);
        } else if (g_tx_done) {
            fmt_bytes(a, sizeof(a), g_tx_done);
            _snprintf(tx, sizeof(tx) - 1, "%stx %s", rx[0] ? "  " : "", a);
        }
        _snprintf(buf, sizeof(buf), " %s%s", rx, tx);
        status_set(1, buf);
    } else {
        g_rx_done = g_rx_total = g_tx_done = g_tx_total = 0;
        status_set(1, "");
    }

    /* Tick the elapsed seconds while a turn is in flight. A counter that moves
     * is the difference between "working" and "hung", and it costs one
     * SB_SETTEXT a second rather than touching the transcript. */
    if (g_turn_active && g_turn_start_ms) {
        _snprintf(buf, sizeof(buf), " %s %lus",
                  g_streaming ? "receiving..." : "Claude is thinking...",
                  (unsigned long)((GetTickCount() - g_turn_start_ms) / 1000));
        status_set(2, buf);
    }
}

static void page_update_ui(void)
{
    char lbl[64];
    _snprintf(lbl, sizeof(lbl), "Page %d/%d%s",
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
    _snprintf(buf, sizeof(buf), "%d", n);
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
    rc_net_send(&g_net, "SWITCH", keys, vals, 1, NULL, 0);
}

static void ui_set_busy(int busy)
{
    g_turn_active = busy;
    EnableWindow(g_stop, busy ? TRUE : FALSE);
    EnableWindow(g_send, busy ? FALSE : TRUE);
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
        if (v) _snprintf(g_net.sess, sizeof(g_net.sess) - 1, "%s", v);
        v = rc_frame_get(f, "next_seq");
        if (v) g_net.next_seq = (unsigned)atoi(v);
        v = rc_frame_get(f, "page");  if (v) g_view = atoi(v);
        v = rc_frame_get(f, "total"); if (v) g_total = atoi(v);
        g_is_live = 1;
        page_update_ui();
        chats_request();
        g_backoff_ms = 1000;
        rc_log("ready: sess=%s model=%s", g_net.sess,
               rc_frame_get(f, "model") ? rc_frame_get(f, "model") : "?");
        rc_html_append(g_html, "<div class=\"notice\">Connected to bridge.</div>");
        status_refresh();

    } else if (strcmp(f->type, "DOC") == 0) {
        /* The stylesheet and skeleton come from the bridge, so the look can be
         * changed by restarting it rather than by rebuilding and re-deploying
         * the guest binary. */
        if (!g_doc_sent) {
            g_doc_sent = 1;
            rc_html_set_document(g_html, f->body);
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
         * "live" replaces the block still being written; anything else is a
         * finished block appended to the end and never touched again. */
        const char *mode = rc_frame_get(f, "mode");
        int at_bottom = rc_html_at_bottom(g_html);
        think_hide();
        if (mode && strcmp(mode, "live") == 0)
            rc_html_live(g_html, f->body);
        else
            rc_html_append(g_html, f->body);
        if (at_bottom) rc_html_scroll_bottom(g_html);

    } else if (strcmp(f->type, "TOOL") == 0) {
        /* The bridge sends tool activity as a finished block now; this branch
         * only survives for older bridges. */
        think_hide();

    } else if (strcmp(f->type, "TURN") == 0) {
        const char *state = rc_frame_get(f, "state");
        if (state && strcmp(state, "start") == 0) {
            ui_set_busy(1);
            g_turn_start_ms = GetTickCount();
            status_set(2, " Claude is thinking...");
            think_show();
        } else {
            char buf[96];
            turn_done();
            ui_set_busy(0);
            /* Now, and only now, re-fetch the list. Sending a message is what
             * makes a conversation the most recent one, so this is where the
             * reordering belongs -- not on merely opening one. */
            chats_request();
            v = rc_frame_get(f, "cost_usd");
            _snprintf(buf, sizeof(buf), " done  $%s", (v && *v) ? v : "?");
            status_set(2, buf);

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
                _snprintf(g_opt_model[g_n_model], 63, "%s", id);
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
                _snprintf(g_opt_effort[g_n_effort], 63, "%s", id);
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
        g_chat_widest = 0;
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
                _snprintf(g_chat_group, sizeof(g_chat_group) - 1, "%s", grp);
                g_chat_group[sizeof(g_chat_group) - 1] = '\0';
                g_chat_ids[g_chat_count][0] = '\0';
                g_chat_is_header[g_chat_count] = 1;
                SendMessageA(g_chats, LB_ADDSTRING, 0, (LPARAM)g_chat_group);
                g_chat_count++;
            }

            /* No "* " marker any more: the selection highlight already says
             * which conversation is open, and a marker meant every switch had
             * to relabel the whole list. */
            _snprintf(label, sizeof(label) - 1, "%s", title ? title : id);
            label[sizeof(label) - 1] = '\0';
            _snprintf(g_chat_ids[g_chat_count], 63, "%s", id);
            g_chat_is_header[g_chat_count] = 0;
            fit_label(label, sizeof(label), chats_text_width() - CHAT_INDENT);
            SendMessageA(g_chats, LB_ADDSTRING, 0, (LPARAM)label);
            if (cur && *cur == '1') {
                g_chat_current = g_chat_count;
                SendMessageA(g_chats, LB_SETCURSEL, g_chat_count, 0);
            }
            {
                int px = label_width_px(label);
                if (px > g_chat_widest) g_chat_widest = px;
            }
            g_chat_count++;
        }

    } else if (strcmp(f->type, "SESSIONSEND") == 0) {
        /* RDW_FRAME as well as the client: the frame is what draws the
         * client edge, and repainting only the interior leaves it stale. */
        SendMessageA(g_chats, WM_SETREDRAW, TRUE, 0);
        RedrawWindow(g_chats, NULL, NULL,
                     RDW_INVALIDATE | RDW_ERASE | RDW_FRAME | RDW_UPDATENOW);
        rc_log("chat list: %d sessions, widest %dpx", g_chat_count,
               g_chat_widest);
        log_mem("chat list loaded");

    } else if (strcmp(f->type, "SWITCHED") == 0) {
        v = rc_frame_get(f, "total");
        if (v) g_total = atoi(v);
        v = rc_frame_get(f, "id");
        if (v) _snprintf(g_net.sess, sizeof(g_net.sess) - 1, "%s", v);
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
        g_think_mark = -1;      /* the control is about to be cleared */
        /* One page in, one page out: clear before rendering, so the control
         * never holds two. */
        rc_html_clear(g_html);
        v = rc_frame_get(f, "continued");
        if (v && *v == '1')
            rc_html_append(g_html, "<div class=\"notice\">"
                           "continued from the previous page</div>");
        page_update_ui();

    } else if (strcmp(f->type, "PAGEEND") == 0) {
        if (g_loading_page) { g_loading_page = 0; rc_html_scroll_bottom(g_html); }
        page_update_ui();
        log_mem("page loaded");

    } else if (strcmp(f->type, "PAGESEALED") == 0) {
        v = rc_frame_get(f, "total"); if (v) g_total = atoi(v);
        /* On the live page, follow the seal forward. On an older page, the
         * counter simply ticks up -- that IS the new-content notification. */
        if (g_is_live) g_view = g_total;
        page_update_ui();

    } else if (strcmp(f->type, "ANCHOR") == 0) {
        rc_log("anchor: page=%s preview=%s",
               rc_frame_get(f, "page") ? rc_frame_get(f, "page") : "?",
               rc_frame_get(f, "preview") ? rc_frame_get(f, "preview") : "");

    } else if (strcmp(f->type, "ERROR") == 0) {
        char line[512];
        _snprintf(line, sizeof(line) - 1, "\r\n[%s] %s\r\n",
                  rc_frame_get(f, "code") ? rc_frame_get(f, "code") : "error",
                  rc_frame_get(f, "msg") ? rc_frame_get(f, "msg") : "");
        rc_log("bridge error: %s", line);
        rc_html_append(g_html, line);
        ui_set_busy(0);

    } else if (strcmp(f->type, "NOTICE") == 0) {
        {
            char esc[1024], div[1200];
            esc_html(f->body, esc, sizeof(esc));
            _snprintf(div, sizeof(div) - 1,
                      "<div class=\"notice\">%s</div>", esc);
            div[sizeof(div) - 1] = '\0';
            rc_html_append(g_html, div);
        }

    } else {
        rc_log("unhandled frame type: %s", f->type);
    }
}

/* ------------------------------------------------------------------ input */

/* Width of a list label in the actual UI font. Cheaper than it looks: called
 * once per session while the list is rebuilt, ~36 times. */
static int label_width_px(const char *text)
{
    HDC dc;
    HFONT old;
    SIZE sz;
    int n;

    if (!g_chats || !text) return 0;
    n = (int)strlen(text);
    if (n <= 0) return 0;
    dc = GetDC(g_chats);
    if (!dc) return 0;
    old = (HFONT)SelectObject(dc, g_ui_font);
    sz.cx = 0;
    GetTextExtentPoint32A(dc, text, n, &sz);
    SelectObject(dc, old);
    ReleaseDC(g_chats, dc);
    return (int)sz.cx;
}

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
        _snprintf(buf, sizeof(buf) - 1, "%.*s...", fit, label);
        buf[sizeof(buf) - 1] = '\0';
        _snprintf(label, cap - 1, "%s", buf);
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
 * The obvious design is to append "Claude is thinking..." and delete it again
 * when output arrives. It works everywhere except here: deleting with
 * EM_REPLACESEL puts Win98's riched20 back into the state where it stops
 * painting altogether, the same failure as an unprimed control, and the whole
 * answer then streams in invisibly. Measured twice, including with the
 * selection positions corrected -- `truncate to 152 from 174 left 153`, right
 * range, dead control.
 *
 * So the indicator lives in the title bar and the status bar, neither of which
 * needs the transcript touched. The title says that something is happening,
 * the status pane ticks the seconds so it is visibly not a hang, and both
 * clear themselves. */
static void think_show(void)
{
    if (g_think_mark >= 0) return;
    g_think_mark = 1;
    g_streaming = 0;
    SetWindowTextA(g_main, RC_TITLE "  -  working...");
}

/* First output of the turn. The title stays as it is until the turn actually
 * ends -- the window is still busy -- but the wording in the status bar
 * changes, because there is now something to look at. */
static void think_hide(void)
{
    if (g_think_mark < 0) return;
    g_think_mark = -1;
    g_streaming = 1;
}

static void turn_done(void)
{
    g_think_mark = -1;
    g_streaming = 0;
    SetWindowTextA(g_main, RC_TITLE);
}

/* Echo the user's own line. Must match bridge/markdown.py user_block(): the
 * bridge records the identical string into the paginator so that paging back
 * to a sealed page still shows the question above the answer. Echoing locally
 * rather than waiting for the bridge to send it back is what keeps typing
 * responsive on a dialup link. */
#define RC_USER_RULE "----------------------------------------------------------"

/* Escape for HTML. The guest builds exactly one piece of markup itself -- the
 * echo of the user's own line -- and it still has to escape, because what gets
 * typed into the input box is not trusted to be free of '<'. */
static void esc_html(const char *in, char *out, unsigned cap)
{
    unsigned o = 0;

    for (; *in && o + 8 < cap; in++) {
        const char *rep = NULL;
        if (*in == '&')      rep = "&amp;";
        else if (*in == '<') rep = "&lt;";
        else if (*in == '>') rep = "&gt;";
        else if (*in == '\n') rep = "<br>";
        else if (*in == '\r') continue;
        if (rep) {
            unsigned n = (unsigned)strlen(rep);
            memcpy(out + o, rep, n);
            o += n;
        } else {
            out[o++] = *in;
        }
    }
    out[o] = '\0';
}

static void echo_user_block(const char *text)
{
    char esc[4096], div[4400];

    esc_html(text, esc, sizeof(esc));
    _snprintf(div, sizeof(div) - 1, "<div class=\"you\">%s</div>", esc);
    div[sizeof(div) - 1] = '\0';
    rc_html_append(g_html, div);
    rc_html_scroll_bottom(g_html);
}

static void do_send(void)
{
    char buf[4096];
    int n;

    if (g_turn_active) return;
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
        rc_html_append(g_html, "<div class=\"err\">Not connected.</div>");
    }
    SetFocus(g_input);
}

static void do_stop(void)
{
    rc_log("stop requested");
    rc_net_send(&g_net, "INTERRUPT", NULL, NULL, 0, NULL, 0);
}

/* Open a conversation on a plain click.
 *
 * Not via LBN_SELCHANGE: that also fires for arrow keys, and switching on those
 * would tear down a live session every time the list was scrolled with the
 * keyboard. Testing the mouse button state inside the notification was tried
 * and is unreliable -- by the time it arrives the button may already be up.
 * Handling the button release directly is unambiguous. The switch is POSTED
 * rather than done here so the listbox finishes with the click (and releases
 * capture) before the transcript is torn down and rebuilt. */
#define WM_RC_OPENCHAT (WM_APP + 20)

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
    int sb_h = 0, y, w, h;
    RECT srect;

    GetClientRect(hwnd, &rc);
    if (g_status) {
        SendMessageA(g_status, WM_SIZE, 0, 0);
        GetWindowRect(g_status, &srect);
        sb_h = srect.bottom - srect.top;
    }

    w = rc.right - rc.left;
    h = rc.bottom - rc.top - sb_h;
    y = h - INPUT_HEIGHT - GUTTER;

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
        rc_html_move(g_html, cx, GUTTER + PAGEBAR_H,
                     cw, y - GUTTER - PAGEBAR_H);
        MoveWindow(g_input, cx, y,
                   cw - GUTTER - BUTTON_WIDTH, INPUT_HEIGHT, TRUE);
    }
    MoveWindow(g_send, w - GUTTER - BUTTON_WIDTH, y, BUTTON_WIDTH, 24, TRUE);
    MoveWindow(g_stop, w - GUTTER - BUTTON_WIDTH, y + 28, BUTTON_WIDTH, 24, TRUE);
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

    g_html = rc_html_create(hwnd);
    if (!g_html)
        MessageBoxA(hwnd, "Could not create the HTML view.", "RetroClaude",
                    MB_OK | MB_ICONERROR);

    g_input = CreateWindowExA(WS_EX_CLIENTEDGE, "EDIT", "",
        WS_CHILD | WS_VISIBLE | WS_TABSTOP | WS_VSCROLL |
        ES_MULTILINE | ES_AUTOVSCROLL | ES_WANTRETURN,
        0, 0, 0, 0, hwnd, (HMENU)ID_INPUT, inst, NULL);

    g_send = CreateWindowExA(0, "BUTTON", "Send",
        WS_CHILD | WS_VISIBLE | WS_TABSTOP | BS_DEFPUSHBUTTON,
        0, 0, 0, 0, hwnd, (HMENU)ID_SEND, inst, NULL);

    g_stop = CreateWindowExA(0, "BUTTON", "Stop",
        WS_CHILD | WS_VISIBLE | WS_TABSTOP | BS_PUSHBUTTON,
        0, 0, 0, 0, hwnd, (HMENU)ID_STOP, inst, NULL);
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

    g_refresh = CreateWindowExA(0, "BUTTON", "Refresh",
        WS_CHILD | WS_VISIBLE | BS_PUSHBUTTON, 0, 0, 0, 0,
        hwnd, (HMENU)ID_REFRESH, inst, NULL);

    g_live = CreateWindowExA(0, "BUTTON", "Live",
        WS_CHILD | WS_VISIBLE | BS_PUSHBUTTON, 0, 0, 0, 0,
        hwnd, (HMENU)ID_LIVE, inst, NULL);
    g_first = CreateWindowExA(0, "BUTTON", "|<",
        WS_CHILD | WS_VISIBLE | BS_PUSHBUTTON, 0, 0, 0, 0,
        hwnd, (HMENU)ID_FIRST, inst, NULL);
    g_prev = CreateWindowExA(0, "BUTTON", "<",
        WS_CHILD | WS_VISIBLE | BS_PUSHBUTTON, 0, 0, 0, 0,
        hwnd, (HMENU)ID_PREV, inst, NULL);
    g_pagelbl = CreateWindowExA(0, "STATIC", "Page 1/1",
        WS_CHILD | WS_VISIBLE | SS_CENTER, 0, 0, 0, 0,
        hwnd, (HMENU)ID_PAGELBL, inst, NULL);
    g_next = CreateWindowExA(0, "BUTTON", ">",
        WS_CHILD | WS_VISIBLE | BS_PUSHBUTTON, 0, 0, 0, 0,
        hwnd, (HMENU)ID_NEXT, inst, NULL);
    g_last = CreateWindowExA(0, "BUTTON", ">|",
        WS_CHILD | WS_VISIBLE | BS_PUSHBUTTON, 0, 0, 0, 0,
        hwnd, (HMENU)ID_LAST, inst, NULL);

    g_status = CreateWindowExA(0, STATUSCLASSNAMEA, "",
        WS_CHILD | WS_VISIBLE | SBARS_SIZEGRIP,
        0, 0, 0, 0, hwnd, (HMENU)ID_STATUS, inst, NULL);
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
    if (rc_net_start(&g_net, hwnd, g_cfg.host, g_cfg.port, g_cfg.secret) != 0) {
        char msg[160];
        _snprintf(msg, sizeof(msg),
                  "Cannot reach %s:%d - retrying in %ds\r\n",
                  g_cfg.host, g_cfg.port, g_backoff_ms / 1000);
        rc_html_append(g_html, msg);

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
        PostMessage(hwnd, WM_APP + 99, 0, 0);   /* connect after first paint */
        return 0;

    case WM_APP + 99:
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
            if (rc_html_poll(g_html)) KillTimer(hwnd, TIMER_HTMLPOLL);
        }
        return 0;

    case WM_RC_FRAME: {
        rc_frame *f = (rc_frame *)lp;
        render_frame(f);
        /* The UI thread owns every posted frame and is the only thing that
         * frees one -- except when PostMessage itself failed, which the reader
         * handles. */
        rc_frame_free(f);
        HeapFree(GetProcessHeap(), 0, f);
        InterlockedDecrement(&g_net.inflight);
        return 0;
    }

    case WM_RC_STATE:
        status_refresh();
        if ((rc_state)wp == RC_FAILED || (rc_state)wp == RC_DISCONNECTED) {
            ui_set_busy(0);
            SetTimer(hwnd, TIMER_RECONNECT, (UINT)g_backoff_ms, NULL);
        }
        return 0;

    case WM_COMMAND: {
        int id = (int)LOWORD(wp);
        switch (id) {
        case ID_SEND:  do_send(); return 0;

        case ID_STOP:  do_stop(); return 0;
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
            rc_net_send(&g_net, "NEWSESSION", NULL, NULL, 0, NULL, 0);
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
            _snprintf(msg, sizeof(msg) - 1,
                      "Currently connecting to %s:%d.\n\n"
                      "Set this in retroclaude.ini next to the program:\n\n"
                      "  [bridge]\n  host=%s\n  port=%d\n\n"
                      "A dialog for this is still to come.",
                      g_cfg.host, g_cfg.port, g_cfg.host, g_cfg.port);
            msg[sizeof(msg) - 1] = '\0';
            MessageBoxA(hwnd, msg, RC_TITLE, MB_OK | MB_ICONINFORMATION);
            return 0;
        }

        case IDM_ABOUT: {
            char msg[420];
            _snprintf(msg, sizeof(msg) - 1,
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
                rc_net_send(&g_net, "SETMODEL", keys, vals, 1, NULL, 0);
                return 0;
            }
            if (id >= IDM_EFFORT0 && id < IDM_EFFORT0 + g_n_effort) {
                const char *keys[1], *vals[1];
                keys[0] = "effort"; vals[0] = g_opt_effort[id - IDM_EFFORT0];
                rc_net_send(&g_net, "SETEFFORT", keys, vals, 1, NULL, 0);
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
        rc_html_destroy(g_html);
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

    memset(&icc, 0, sizeof(icc));
    icc.dwSize = sizeof(icc);
    icc.dwICC = ICC_BAR_CLASSES;
    InitCommonControlsEx(&icc);

    rc_config_load(&g_cfg, g_exe_dir);

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
        MessageBoxA(NULL, "OLE could not start.", "RetroClaude",
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
