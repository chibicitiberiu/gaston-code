/* The chat list: the left-hand column of past and current conversations.
 *
 * The cleanest cut out of main.c (audit finding 01.1): no g_chat_* global
 * used to be touched outside the lines that moved here. Owns the listbox,
 * its Refresh button, the SESSIONS/SESSION/SESSIONSEND/SWITCHED frame types,
 * and the id-keyed bookkeeping (g_chat_ids, g_chat_is_header, g_chat_group,
 * g_chat_current, g_chat_count -- all folded into this file's own statics
 * now) that used to be six main.c globals (audit finding 01.11 counts seven
 * and names a g_chat_widest that does not exist in the code as found; five
 * of the six are these, the sixth is g_chats_oldproc, moved here as well).
 */
#ifndef RC_CHATLIST_H
#define RC_CHATLIST_H

#include <windows.h>
#include "net/net.h"
#include "net/frameproto.h"

#ifdef __cplusplus
extern "C" {
#endif

#define ID_CHATS   1012
#define ID_REFRESH 1013

/* WM_APP-offset message this module posts to its parent: chats_proc ->
 * wnd_proc, "open the highlighted conversation" -- was declared with the
 * rest of main.c's own WM_APP offsets; it lives here now, since it is this
 * module's event, not main.c's. */
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

/* confirm: "a switch is about to happen, is a turn running, may I?" -- the
 * module does not own that policy question (main.c answers it today with
 * g_turn_active and a MessageBoxA). Return non-zero to proceed; on a
 * decline this module restores the highlight to whatever is actually open,
 * since that is its own state to fix up.
 *
 * switching: called once a switch has been confirmed and is about to go out
 * on the wire -- main.c uses it to show "loading..." in the title and
 * status pane, which is UI chrome this module does not own either.
 *
 * switched: called after a SWITCHED frame has been folded into this
 * module's own bookkeeping (the newly-open conversation is marked and
 * redrawn) -- `f` is handed back so main.c can pull whatever else SWITCHED
 * carries (today just `total`) without this module needing to know what
 * main.c does with it: resetting the page total, the window title, the
 * status pane, and any bar left over from the session just left (see
 * app/bar.h's rc_bar_hide_all) are none of this module's business. */
typedef int (*rc_chats_confirm_fn)(void);
void rc_chats_create(HWND parent, HINSTANCE inst, rc_net *net,
                      HFONT normal, HFONT bold, rc_chats_confirm_fn confirm,
                      rc_chats_send_fn send, void (*switching)(void),
                      void (*switched)(rc_frame *f));

/* x/y/w: the column's position and width. col_h: the column's FULL height,
 * list plus the Refresh button plus the gap between them -- this module
 * splits it the same way main.c's layout() used to (list gets col_h minus
 * 24px, Refresh gets the 22px left after a 2px gap). */
void rc_chats_move(int x, int y, int w, int col_h);

HWND rc_chats_hwnd(void);
void rc_chats_request(void);

/* SESSIONS, SESSION, SESSIONSEND, SWITCHED -- the four frame types this
 * module owns. Returns non-zero if `f->type` was one of those. */
int  rc_chats_frame(rc_frame *f);

int  rc_chats_draw(DRAWITEMSTRUCT *dis);
int  rc_chats_measure(MEASUREITEMSTRUCT *mis);

/* The WM_RC_OPENCHAT / dblclick / Enter path -- see chatlist.cpp's own
 * comment on why the switch is posted rather than actioned in place. */
void rc_chats_open_selected(void);

#ifdef __cplusplus
}
#endif

#endif
