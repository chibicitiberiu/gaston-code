/* The Model and Effort menus: built from what the bridge sends, so the lists
 * can change without rebuilding this binary.
 *
 * The last of the three remaining §1.2 extraction targets out of main.c
 * (audit finding 01.58) -- the bar, the chat list and the page bar are
 * already their own files. This is the one the audit called out as most
 * obviously wanting to be one module: the id-range arithmetic in
 * rc_optmenu_command and the array fill in rc_optmenu_frame have to agree
 * about MAX_OPTS and the base constants, and used to be 970 lines apart in
 * main.c.
 */
#ifndef RC_OPTMENU_H
#define RC_OPTMENU_H

#include <windows.h>
#include "net/frameproto.h"

#ifdef __cplusplus
extern "C" {
#endif

/* Model and effort entries are numbered from a base because the lists come
 * from the bridge and are not known at compile time. */
#define IDM_MODEL0  1200      /* .. IDM_MODEL0 + MAX_OPTS */
#define IDM_EFFORT0 1300      /* .. IDM_EFFORT0 + MAX_OPTS */
#define MAX_OPTS    16

/* Signature of main.c's ui_send: "send something the user just asked for,
 * and say so if it did not go." Reused rather than duplicated, exactly as
 * app/bar.h's rc_bar_send_fn and app/chatlist.h's rc_chats_send_fn do. */
typedef int (*rc_optmenu_send_fn)(const char *what, const char *type,
                                   const char *const *keys,
                                   const char *const *vals, int nkv,
                                   const void *body, unsigned body_len);

/* Creates the Model and Effort popups (each seeded with one greyed
 * "(not connected)" placeholder, exactly as build_menu used to) and appends
 * them to `bar` as "&Model"/"&Effort" -- called from build_menu between
 * Configure and Help, so the menu order this module does not own is
 * unchanged.
 * owner: DrawMenuBar's target -- OPTIONSEND redraws the bar after a batch.
 * send: main.c's ui_send, for SETMODEL/SETEFFORT. */
void rc_optmenu_create(HWND owner, HMENU bar, rc_optmenu_send_fn send);

/* OPTION, OPTIONSEND -- the two frame types this module owns. Returns
 * non-zero if `f->type` was one of those. */
int  rc_optmenu_frame(rc_frame *f);

/* WM_COMMAND's default case: the IDM_MODEL0/IDM_EFFORT0 ranges. Returns
 * non-zero if `id` fell in either (whether or not it was actually sent). */
int  rc_optmenu_command(int id);

#ifdef __cplusplus
}
#endif

#endif
