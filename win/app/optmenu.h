/* The Mode, Model, Effort and Commands menus: built from what the bridge
 * sends, so the lists can change without rebuilding this binary.
 *
 * Four now, not two. Three are settings with a ticked current value; the
 * fourth is a list of slash commands, which is not a setting at all and has
 * no tick. It lives here anyway because it is the same OPTION frame, the
 * same batch reset, the same id arithmetic and the same WM_COMMAND routing
 * -- a fourth copy of that machinery elsewhere would be the duplication this
 * module exists to have prevented.
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
#define IDM_MODE0   1400      /* .. IDM_MODE0 + MAX_OPTS */
#define IDM_CMD0    1500      /* .. IDM_CMD0 + MAX_OPTS */
#define IDM_WORKSET 1599      /* "Change work directory..." */
#define IDM_ATTFILE 1600      /* Attach > File... */
#define IDM_ATTSHOT 1601      /* Attach > Screenshot */
#define MAX_OPTS    16

/* Signature of main.c's ui_send: "send something the user just asked for,
 * and say so if it did not go." Reused rather than duplicated, exactly as
 * app/bar.h's rc_bar_send_fn and app/chatlist.h's rc_chats_send_fn do. */
typedef int (*rc_optmenu_send_fn)(const char *what, const char *type,
                                   const char *const *keys,
                                   const char *const *vals, int nkv,
                                   const void *body, unsigned body_len);

/* "Send this as if it had been typed" -- app/input.h's rc_input::send_text,
 * behind a function pointer so this module does not depend on that class.
 * A slash command IS a prompt: the CLI reads it out of the message text,
 * there is no command frame, and routing it through compose-and-send is what
 * gives it the local echo, the mid-turn refusal and the busy state that a
 * typed message gets. */
typedef int (*rc_optmenu_input_fn)(const char *text, int compose);

class rc_optmenu {
    HMENU menu_model, menu_effort, menu_mode, menu_cmd, menu_work;
    HMENU menu_attach;
    char  opt_model[MAX_OPTS][64];
    char  opt_effort[MAX_OPTS][64];
    /* Permission mode: Manual / Accept edits / Plan. The list is the
     * BRIDGE'S, not a table here, for the same reason the model list is --
     * `auto` and `bypassPermissions` are withheld for measured reasons that
     * belong beside the measurement (bridge/server.py, MODE_ORDER), not in
     * a menu builder on a 32MB machine. */
    char  opt_mode[MAX_OPTS][64];
    /* Slash commands. Not an option -- nothing is ticked and there is no
     * current value -- but the same bridge-driven menu with the same id
     * arithmetic and the same WM_COMMAND routing, so it lives here rather
     * than as a fourth copy of that machinery somewhere else. WHICH commands
     * appear is the bridge's decision (bridge/server.py, COMMAND_MENU): five
     * of the fifty-three the CLI offers. */
    char  opt_cmd[MAX_OPTS][64];
    /* Does this one take an argument? The bridge says so with a `hint`
     * header, which is the same fact the label's "<optional custom
     * summarization instructions>" shows -- one source, so the label and the
     * behaviour cannot disagree. */
    char  cmd_arg[MAX_OPTS];
    int   n_model, n_effort, n_mode, n_cmd;
    /* The LABELS of the checked entries, which is what the toolbar shows.
     * The label is the bridge's displayName ("Haiku"), not the resolved id
     * ("claude-haiku-4-5-20251001") -- _send_options resolves aliases on
     * purpose, and the resolved form is 20+ characters, which does not fit
     * a toolbar button on a 640px screen. */
    char  cur_model[64], cur_effort[64], cur_mode[64];
    /* Where this conversation's files land, as the guest spells it: the full
     * path for the menu, the short form the button wears. The bridge sends
     * both because a 640px screen has no room for the long one on a button. */
    char  cur_work[128], cur_work_short[128];
    /* Non-zero once the batch has offered at least one effort level that is
     * not greyed out. Haiku has none, and the bridge says so with a single
     * disabled row -- a toolbar button reading "(effort)" forever is worse
     * than no button, so the toolbar hides it. */
    int   effort_ok;
    /* Which mode row is ticked, so one keystroke can advance to the next.
     * An index rather than a search on `cur_mode`, which holds the LABEL --
     * matching a display string back to its id is the sort of thing that
     * works until a label is reworded. -1 when nothing is ticked. */
    int   cur_mode_ix;
    int   opts_open;   /* mid-batch, so the next model row appends */
    HWND  owner;
    rc_optmenu_send_fn send;
    rc_optmenu_input_fn input;

    rc_optmenu(const rc_optmenu &);
    rc_optmenu &operator=(const rc_optmenu &);
    rc_optmenu(HWND owner_, HMENU bar, rc_optmenu_send_fn send_,
               rc_optmenu_input_fn input_);

public:
    /* Creates the Model and Effort popups (each seeded with one greyed
     * "(not connected)" placeholder, exactly as build_menu used to) and
     * appends them to `bar` as "&Model"/"&Effort" -- called from build_menu
     * between Configure and Help, so the menu order this module does not own
     * is unchanged.
     * owner: DrawMenuBar's target -- OPTIONSEND redraws the bar after a
     *     batch.
     * send: main.c's ui_send, for SETMODEL/SETEFFORT/SETMODE.
     * input: compose-and-send, for the Commands menu -- see
     *     rc_optmenu_input_fn above. */
    static rc_optmenu *create(HWND owner, HMENU bar, rc_optmenu_send_fn send,
                              rc_optmenu_input_fn input);
    ~rc_optmenu() {}

    /* OPTION, OPTIONSEND -- the two frame types this module owns. Returns
     * non-zero if `f->type` was one of those. */
    int frame(rc_frame *f);

    /* WM_COMMAND's default case: the IDM_MODEL0/IDM_EFFORT0 ranges. Returns
     * non-zero if `id` fell in either (whether or not it was actually
     * sent). */
    int command(int id);

    /* For the toolbar, which pops these same menus rather than building its
     * own. One source of truth for what the lists contain and which entry is
     * ticked; the toolbar is another way to reach them, not another copy. */
    HMENU model_menu() const  { return menu_model; }
    HMENU effort_menu() const { return menu_effort; }
    HMENU mode_menu() const   { return menu_mode; }
    HMENU cmd_menu() const    { return menu_cmd; }
    HMENU work_menu() const   { return menu_work; }
    HMENU attach_menu() const { return menu_attach; }
    const char *work_label() const { return cur_work_short; }
    const char *work_path() const  { return cur_work; }
    const char *model_label() const  { return cur_model; }
    const char *effort_label() const { return cur_effort; }
    const char *mode_label() const   { return cur_mode; }
    int has_effort() const { return effort_ok; }

    /* Advance to the next permission mode and tell the bridge, for the
     * keyboard shortcut. Wraps. Does nothing when the list is empty, which
     * is the disconnected case. The menus and the toolbar update when the
     * bridge answers with a fresh batch -- not optimistically here, because
     * a mode this client did not manage to set must not be shown as set. */
    void cycle_mode();
};

#endif
