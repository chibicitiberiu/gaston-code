/* The Model and Effort menus: built from what the bridge sends, so the lists
 * can change without rebuilding this binary.
 *
 * The last of the three remaining §1.2 extraction targets out of main.c
 * (audit finding 01.58) -- the bar, the chat list and the page bar are
 * already their own files. This is the one the audit called out as most
 * obviously wanting to be one module: the id-range arithmetic in
 * rc_optmenu::command and the array fill in rc_optmenu::frame have to agree
 * about MAX_OPTS and the base constants, and used to be 970 lines apart in
 * main.c.
 *
 * A CLASS NOW, not a handle plus three free functions (audit finding 01.60):
 * same reasoning as app/htmlview.h, the pattern this module and its five
 * siblings follow. There is exactly one instance, created explicitly by
 * main.cpp's build_menu at menu-build time (never a file-scope object --
 * see core/rc_cpp.h) and destroyed with `delete`.
 */
#ifndef RC_OPTMENU_H
#define RC_OPTMENU_H

#include <windows.h>
#include "net/frameproto.h"

#ifndef __cplusplus
#error "optmenu.h is C++; the menus are a class now, see app/htmlview.h"
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

class rc_optmenu {
    HMENU menu_model, menu_effort;
    char  opt_model[MAX_OPTS][64];
    char  opt_effort[MAX_OPTS][64];
    int   n_model, n_effort;
    int   opts_open;   /* mid-batch, so the next model row appends */
    HWND  owner;
    rc_optmenu_send_fn send;

    rc_optmenu(const rc_optmenu &);
    rc_optmenu &operator=(const rc_optmenu &);
    rc_optmenu(HWND owner_, HMENU bar, rc_optmenu_send_fn send_);

public:
    /* Creates the Model and Effort popups (each seeded with one greyed
     * "(not connected)" placeholder, exactly as build_menu used to) and
     * appends them to `bar` as "&Model"/"&Effort" -- called from build_menu
     * between Configure and Help, so the menu order this module does not own
     * is unchanged.
     * owner: DrawMenuBar's target -- OPTIONSEND redraws the bar after a
     *     batch.
     * send: main.c's ui_send, for SETMODEL/SETEFFORT. */
    static rc_optmenu *create(HWND owner, HMENU bar, rc_optmenu_send_fn send);
    ~rc_optmenu() {}

    /* OPTION, OPTIONSEND -- the two frame types this module owns. Returns
     * non-zero if `f->type` was one of those. */
    int frame(rc_frame *f);

    /* WM_COMMAND's default case: the IDM_MODEL0/IDM_EFFORT0 ranges. Returns
     * non-zero if `id` fell in either (whether or not it was actually
     * sent). */
    int command(int id);
};

#endif
