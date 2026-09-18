/* The permission/halt bar: a state STACK, not a mode.
 *
 * Two kinds of interruption share one row of controls above the input box:
 * BAR_PERM (Allow / Always / Deny for a tool call) and BAR_HALT (a single
 * Continue, shown when the bridge has STOPPED the agent for a conflicting
 * edit). They used to be arbitrated pairwise -- four uncoordinated functions
 * in main.c, each hand-checking the other's state -- and that arbitration
 * shipped four data-loss bugs (docs/audit/04-planned-features.md finding
 * 04.6). This module replaces the pairwise checks with one bounded stack of
 * pending requests and one descriptor per kind, so the visible bar is always
 * DERIVED (the highest-priority entry on the stack) rather than stored
 * redundantly in satellite booleans that can disagree with each other.
 *
 * Phase 6 is expected to add a third kind (an agent action awaiting
 * confirmation, per docs/PLAN.md) that has no answer button at all -- see
 * audit findings 01.55 and 04.6. Adding it is meant to be one descriptor row
 * plus one BAR_* constant, not a new pairwise interaction with the two that
 * exist today; that is the reason this exists as a stack up front rather than
 * a third special case bolted onto perm/halt.
 */
#ifndef RC_BAR_H
#define RC_BAR_H

#include <windows.h>
#include "net/net.h"
#include "net/frameproto.h"

#ifdef __cplusplus
extern "C" {
#endif

/* The permission bar's controls. Modeless, above the input box: a modal here
 * would pump messages and re-enter the transcript while Claude is mid-stream,
 * and the obvious fix -- queueing frames for the duration -- costs heap on a
 * machine measured at 28KB free after a turn. */
#define ID_PERM_TEXT   1014
#define ID_PERM_ALLOW  1015
#define ID_PERM_ALWAYS 1016
#define ID_PERM_DENY   1017

/* The bar's fixed height when shown, and the geometry main.c's layout() needs
 * to know before asking this module to position its own controls. PERMBTN_W
 * (button width) is this module's own business and stays in bar.cpp. */
#define PERM_H 26

/* Signature of main.c's ui_send: "send something the user just asked for,
 * and say so if it did not go." Reused rather than duplicated, so a
 * PERMREPLY that never reaches the bridge is reported through the same path
 * (and the same seven-line comment) as every other user-initiated send. */
typedef int (*rc_bar_send_fn)(const char *what, const char *type,
                               const char *const *keys,
                               const char *const *vals, int nkv,
                               const void *body, unsigned body_len);

/* net: the one raw rc_net_send this module makes itself (RESUME, on the
 *      Continue button) -- see the comment on that call in bar.cpp for why it
 *      does NOT go through `send` below.
 * send: main.c's ui_send, for PERMREPLY.
 * changed: called after any visible change to the bar (shown, hidden, or
 *      relabelled by a new push/pop) -- the caller re-runs layout() (the HTML
 *      view has no HWND to resize, so the whole window layout must be redone)
 *      and recomputes Send's enabled state. This is what bar_layout() plus
 *      ui_set_busy(g_turn_active) used to do, by hand, at every call site. */
void rc_bar_create(HWND parent, HINSTANCE inst, HFONT font, rc_net *net,
                    rc_bar_send_fn send, void (*changed)(void));

/* Positions this bar's own controls. cx is the left edge (where the label
 * starts), y is the top of the bar's row, right_edge is the rightmost x the
 * buttons may use (i.e. window width minus the outer gutter). Only called
 * when rc_bar_active() is true. */
void rc_bar_move(int cx, int y, int right_edge);

/* Whether the bar currently occupies a row -- layout() uses this exactly
 * where it used to read g_bar_mode directly, to shrink the transcript. */
int rc_bar_active(void);

/* Whether the bar should hold Send disabled right now. Replaces reading
 * g_perm_pending from ui_set_busy/do_send directly: true iff a permission
 * request is the entry currently on screen (a halt alone does not block
 * Send -- Stop is the escape hatch for a wedged CLI, and blocking Send too
 * would leave no way to exercise the cancellation path at all). */
int rc_bar_blocks_send(void);

/* The six frame types this module owns: PERMREQ, PERMSTATE, PERMWAIT,
 * PERMEND, HALT, HALTEND. Returns non-zero if `f->type` was one of those, so
 * a caller building a dispatch table can use this as a single row's handler
 * (see audit finding 01.12) without needing to know the six strings. */
int rc_bar_frame(rc_frame *f);

/* WM_COMMAND for ID_PERM_ALLOW/ALWAYS/DENY. Returns non-zero if `id` was one
 * of those (whether or not there was anything to do, e.g. no bar visible). */
int rc_bar_command(int id);

/* Esc denies whatever permission request is on screen -- but only a
 * permission request; a halt has no "deny", only Continue. Returns non-zero
 * if it consumed the key (there was a permission request to deny). */
int rc_bar_escape(void);

/* Takes every pending request down at once, regardless of kind -- used for
 * disconnect and session-switch, where the bridge has already given up on
 * (or rebuilt past) whatever was outstanding. Distinct from the per-kind
 * removal that PERMEND/HALTEND do through rc_bar_frame: those remove exactly
 * the one entry that was answered, this empties the stack. */
void rc_bar_hide_all(void);

#ifdef __cplusplus
}
#endif

#endif
