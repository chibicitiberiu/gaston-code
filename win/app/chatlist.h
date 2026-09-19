/* The chat list: the left-hand column of past and current conversations.
 *
 * The cleanest cut out of main.c (audit finding 01.1): no g_chat_* global
 * used to be touched outside the lines that moved here. Owns the listbox,
 * its Refresh button, the SESSIONS/SESSION/SESSIONSEND/SWITCHED frame types,
 * and the id-keyed bookkeeping (g_chat_ids, g_chat_is_header, g_chat_group,
 * g_chat_current, g_chat_count -- all folded into this class's own members
 * now) that used to be six main.c globals (audit finding 01.11 counts seven
 * and names a g_chat_widest that does not exist in the code as found; five
 * of the six are these, the sixth is g_chats_oldproc, a member here as well).
 *
 * A CLASS NOW, not a handle plus free functions (audit finding 01.60): same
 * reasoning as app/htmlview.h, the pattern this module and its five
 * siblings follow. There is exactly one instance, created explicitly by
 * main.cpp at window-creation time (never a file-scope object -- see
 * core/rc_cpp.h) and destroyed with `delete`.
 */
#ifndef RC_CHATLIST_H
#define RC_CHATLIST_H

#include <windows.h>
#include "net/net.h"
#include "net/frameproto.h"

#ifndef __cplusplus
#error "chatlist.h is C++; the chat list is a class now, see app/htmlview.h"
#endif

#define ID_CHATS   1012
#define ID_REFRESH 1013

/* WM_APP-offset message this module posts to its parent: the listbox
 * subclass -> wnd_proc, "open the highlighted conversation" -- was declared
 * with the rest of main.c's own WM_APP offsets; it lives here now, since it
 * is this module's event, not main.c's. */
#define WM_RC_OPENCHAT (WM_APP + 20)

/* Signature of main.c's ui_send: "send something the user just asked for,
 * and say so if it did not go." Reused rather than duplicated, exactly as
 * app/bar.h's rc_bar_send_fn does, so a SWITCH that never reaches the
 * bridge is reported through the same path as every other user-initiated
 * send. */
typedef int (*rc_chats_send_fn)(const char *what, const char *type,
                                 const char *const *keys,
                                 const char *const *vals, int nkv,
                                 const void *body, unsigned body_len);

/* "a switch is about to happen, is a turn running, may I?" -- see the
 * `confirm` member comment on rc_chats below. */
typedef int (*rc_chats_confirm_fn)(void);

#define MAX_CHATS 64

class rc_chats {
    /* Session ids, parallel to the list box. */
    char chat_ids[MAX_CHATS][64];
    /* 0 = a session row, 1 = a date-group header. Headers are drawn
     * differently and refuse to be opened. */
    char chat_is_header[MAX_CHATS];
    char chat_group[40];       /* last group emitted, to spot changes */
    int  chat_current;         /* row of the conversation that is open */
    int  chat_count;

    HWND list, refresh;
    HFONT font_normal, font_bold;
    WNDPROC oldproc;
    rc_net *net;
    /* confirm: "a switch is about to happen, is a turn running, may I?" --
     *      this class does not own that policy question (main.c answers it
     *      today with g_turn_active and a MessageBoxA). Return non-zero to
     *      proceed; on a decline this class restores the highlight to
     *      whatever is actually open, since that is its own state to fix
     *      up.
     * switching: called once a switch has been confirmed and is about to go
     *      out on the wire -- main.c uses it to show "loading..." in the
     *      title and status pane, which is UI chrome this class does not
     *      own.
     * switched: called after a SWITCHED frame has been folded into this
     *      class's own bookkeeping (the newly-open conversation is marked
     *      and redrawn) -- `f` is handed back so main.c can pull whatever
     *      else SWITCHED carries (today just `total`) without this class
     *      needing to know what main.c does with it: resetting the page
     *      total, the window title, the status pane, and any bar left over
     *      from the session just left (see app/bar.h's rc_bar::hide_all)
     *      are none of this class's business. */
    rc_chats_confirm_fn confirm;
    rc_chats_send_fn send;
    void (*switching)(void);
    void (*switched)(rc_frame *f);

    /* The single instance, for the listbox subclass trampoline below --
     * there is only ever one, the same invariant the free functions this
     * class replaces already relied on. A plain pointer, zero-initialised
     * in BSS, not an object with a constructor -- see core/rc_cpp.h. */
    static rc_chats *s_instance;

    rc_chats(const rc_chats &);
    rc_chats &operator=(const rc_chats &);
    rc_chats(HWND parent, HINSTANCE inst, rc_net *net_, HFONT normal,
             HFONT bold, rc_chats_confirm_fn confirm_,
             rc_chats_send_fn send_, void (*switching_)(void),
             void (*switched_)(rc_frame *f));

    int text_width();
    void fit_label(char *label, unsigned cap, int avail_px);
    void on_sessions(rc_frame *f);
    void on_session(rc_frame *f);
    void on_sessionsend(rc_frame *f);
    void on_switched(rc_frame *f);

    /* Open a conversation on a plain click -- see chatlist.cpp's own comment
     * on why the switch is posted rather than actioned in place. */
    static LRESULT CALLBACK list_proc(HWND h, UINT m, WPARAM w, LPARAM l);

public:
    static rc_chats *create(HWND parent, HINSTANCE inst, rc_net *net,
                             HFONT normal, HFONT bold,
                             rc_chats_confirm_fn confirm,
                             rc_chats_send_fn send, void (*switching)(void),
                             void (*switched)(rc_frame *f));
    /* NOT empty, and it was.
     *
     * list_proc dereferences s_instance on every message, and the listbox
     * outlives this object: children are destroyed after the frame's
     * WM_DESTROY handler runs, and they get WM_DESTROY/WM_NCDESTROY on the
     * way out. So a deleted rc_chats left a live window pointing at a proc
     * that read freed memory -- an invalid page fault during shutdown, at an
     * address inside this file, with the log ending one line earlier at
     * "reader thread exiting". Un-subclass first, then drop the singleton. */
    ~rc_chats();

    /* x/y/w: the column's position and width. col_h: the column's FULL
     * height, list plus the Refresh button plus the gap between them --
     * this class splits it the same way main.c's layout() used to (list
     * gets col_h minus 24px, Refresh gets the 22px left after a 2px
     * gap). */
    void move(int x, int y, int w, int col_h);

    HWND hwnd() const;
    void request();

    /* SESSIONS, SESSION, SESSIONSEND, SWITCHED -- the four frame types this
     * class owns. Returns non-zero if `f->type` was one of those. */
    int frame(rc_frame *f);

    int draw(DRAWITEMSTRUCT *dis);
    int measure(MEASUREITEMSTRUCT *mis);

    /* The WM_RC_OPENCHAT / dblclick / Enter path. */
    void open_selected();
};

#endif
