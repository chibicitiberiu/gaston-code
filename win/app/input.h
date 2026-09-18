/* Compose and send: the input box, the Send/Stop buttons, and the local echo
 * of what was typed.
 *
 * The last of the §1.2 extraction targets out of main.c to actually move
 * (audit finding 01.59) -- it was never one of the five main.c named when it
 * split bar.cpp/chatlist.cpp/xferstat.cpp/pagebar.cpp/optmenu.cpp out
 * (finding 01.1), so it survived that pass. Same shape as those five: a
 * module plus its own entry points, seamed at the dispatch table
 * render_frame became (finding 01.12).
 */
#ifndef RC_INPUT_H
#define RC_INPUT_H

#include <windows.h>
#include "net/net.h"
#include "app/htmlview.h"

#ifdef __cplusplus
extern "C" {
#endif

#define ID_INPUT 1002
#define ID_SEND  1003
#define ID_STOP  1004

/* The compose row's fixed height, and the geometry main.c's layout() needs
 * to know before asking this module to position its own controls -- same
 * relationship as app/bar.h's PERM_H and app/pagebar.h's PAGEBAR_H. The
 * button width is this module's own business and stays in input.cpp. */
#define INPUT_HEIGHT 56

/* Signature of main.c's ui_send: "send something the user just asked for,
 * and say so if it did not go." Reused rather than duplicated, exactly as
 * app/bar.h's rc_bar_send_fn and app/chatlist.h's rc_chats_send_fn do. */
typedef int (*rc_input_send_fn)(const char *what, const char *type,
                                 const char *const *keys,
                                 const char *const *vals, int nkv,
                                 const void *body, unsigned body_len);

/* net: the one raw rc_net_send this module makes itself, for INPUT -- same
 *      shape as app/pagebar.h's `net` parameter. do_send has always had its
 *      own "Not connected" refusal rather than going through main.c's
 *      ui_send (which would report a different message), so that is
 *      preserved rather than folded into `send` below.
 * html: user()/error() -- the local echo of a sent message and the
 *      "too long"/"not connected" refusals. May be NULL exactly as
 *      everywhere else this file guards g_html; every call here is guarded
 *      too.
 * send: main.c's ui_send, for INTERRUPT (do_stop).
 * blocked: "is Send currently refused" -- g_turn_active || rc_bar_blocks_send()
 *      today. do_send checks this itself, not just the button's enabled
 *      state, because Enter (this module's own input_proc) reaches do_send
 *      directly and bypasses whatever EnableWindow last set.
 * sent: called once a typed INPUT has actually gone out, after the box has
 *      been cleared -- main.c's status_refresh() plus ui_set_busy(1) today,
 *      UI chrome this module does not own. */
void rc_input_create(HWND parent, HINSTANCE inst, HFONT font, rc_net *net,
                      rc_html *html, rc_input_send_fn send,
                      int (*blocked)(void), void (*sent)(void));

/* cx/y: where the input box starts. cw: the transcript column's width, which
 * bounds the input box (Send/Stop sit to its right). win_w: the full client
 * width, which is what Send/Stop are actually measured from. Same split
 * layout() already made at this call site before the move. */
void rc_input_move(int cx, int y, int cw, int win_w);

/* WM_COMMAND for ID_SEND/ID_STOP. Returns non-zero if `id` was one of those,
 * same contract as rc_bar_command/rc_pagebar_command. */
int rc_input_command(int id);

/* EnableWindow for Send/Stop. main.c still owns WHEN (g_turn_active,
 * rc_bar_blocks_send()) -- ui_refresh_enables/ui_set_busy call these rather
 * than reaching into a raw HWND this module owns now. */
void rc_input_enable_send(int enable);
void rc_input_enable_stop(int enable);

/* SetFocus(the input box) -- used after a send and to forward WM_SETFOCUS. */
void rc_input_focus(void);

#ifdef __cplusplus
}
#endif

#endif
