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
 *
 * A CLASS NOW, not a handle plus free functions (audit finding 01.60): same
 * reasoning as app/htmlview.h, the pattern this module and its five
 * siblings follow. There is exactly one instance, created explicitly by
 * main.cpp at window-creation time (never a file-scope object -- see
 * core/rc_cpp.h) and destroyed with `delete`.
 */
#ifndef RC_BAR_H
#define RC_BAR_H

#include <windows.h>
#include "net/net.h"
#include "net/frameproto.h"

#ifndef __cplusplus
#error "bar.h is C++; the bar is a class now, see app/htmlview.h"
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

#define BAR_PERM 1
#define BAR_HALT 2
/* Phase 6 is expected to add a third kind here. */

/* One pending request. `waiting` is PERM-only (PERMWAIT's queue depth);
 * `allows_always` is PERM-only (whether the bridge said a rule can be
 * expressed for this tool -- see the ShowWindow comment in rc_bar::show
 * in bar.cpp). Both are simply unused, and harmless, on a HALT entry. */
struct rc_bar_entry {
    int  kind;
    char id[80];
    /* The request's own text, kept separately from what is on screen so a
     * PERMWAIT queue-depth suffix can be appended without losing the
     * original prompt, and so a request that is pushed back down the stack
     * (a halt remembered behind a permission bar, say) still has something to
     * redisplay when it resurfaces -- replaces main.c's single g_halt_text,
     * which existed only because there was exactly one thing that could be
     * remembered at a time. */
    char base_text[224];
    int  waiting;
    int  allows_always;
};

/* Perm plus halt today, with one spare slot for Phase 6's third kind. Every
 * allocation in this codebase is bounded and small; a stack that could grow
 * without limit would just be the same defect PERMREQ's re-entrancy guard
 * exists to prevent, one level up. */
#define BAR_STACK_MAX 3

class rc_bar {
    rc_bar_entry stack[BAR_STACK_MAX];
    int          depth;

    HWND    text, allow, always, deny;
    rc_net *net;
    rc_bar_send_fn send;
    void  (*changed)(void);

    rc_bar(const rc_bar &);
    rc_bar &operator=(const rc_bar &);
    rc_bar(HWND parent, HINSTANCE inst, HFONT font, rc_net *net_,
           rc_bar_send_fn send_, void (*changed_)(void));

    /* bar_visible in the pre-class code: the highest-priority entry on the
     * stack, or NULL. move(), blocks_send() and show() all derive the SAME
     * visible entry from the SAME stack rather than reading a cached "which
     * one is showing" flag, which is what let the old g_bar_mode and
     * g_halt_id disagree with each other. */
    rc_bar_entry *visible();
    rc_bar_entry *find_entry(int kind);
    void show();
    void relabel(rc_bar_entry *e);
    void push(int kind, const char *id, const char *text, int allows_always);
    void pop(int kind, const char *id);
    void reply(rc_bar_entry *e, const char *action);

public:
    /* net: the one raw rc_net_send this module makes itself (RESUME, on the
     *      Continue button) -- see the comment on that call in bar.cpp for
     *      why it does NOT go through `send` below.
     * send: main.c's ui_send, for PERMREPLY.
     * changed: called after any visible change to the bar (shown, hidden, or
     *      relabelled by a new push/pop) -- the caller re-runs layout() (the
     *      HTML view has no HWND to resize, so the whole window layout must
     *      be redone) and recomputes Send's enabled state. This is what
     *      bar_layout() plus ui_set_busy(g_turn_active) used to do, by hand,
     *      at every call site. */
    static rc_bar *create(HWND parent, HINSTANCE inst, HFONT font,
                           rc_net *net, rc_bar_send_fn send,
                           void (*changed)(void));
    ~rc_bar() {}

    /* Positions this bar's own controls. cx is the left edge (where the
     * label starts), y is the top of the bar's row, right_edge is the
     * rightmost x the buttons may use (i.e. window width minus the outer
     * gutter). Only called when active() is true. */
    void move(int cx, int y, int right_edge);

    /* Whether the bar currently occupies a row -- layout() uses this exactly
     * where it used to read g_bar_mode directly, to shrink the transcript. */
    int active() const;

    /* Whether the bar should hold Send disabled right now. Replaces reading
     * g_perm_pending from ui_set_busy/do_send directly: true iff a
     * permission request is the entry currently on screen (a halt alone does
     * not block Send -- Stop is the escape hatch for a wedged CLI, and
     * blocking Send too would leave no way to exercise the cancellation path
     * at all). */
    int blocks_send();

    /* The six frame types this module owns: PERMREQ, PERMSTATE, PERMWAIT,
     * PERMEND, HALT, HALTEND. Returns non-zero if `f->type` was one of
     * those, so a caller building a dispatch table can use this as a single
     * row's handler (see audit finding 01.12) without needing to know the
     * six strings. */
    int frame(rc_frame *f);

    /* WM_COMMAND for ID_PERM_ALLOW/ALWAYS/DENY. Returns non-zero if `id` was
     * one of those (whether or not there was anything to do, e.g. no bar
     * visible). */
    int command(int id);

    /* Esc denies whatever permission request is on screen -- but only a
     * permission request; a halt has no "deny", only Continue. Returns
     * non-zero if it consumed the key (there was a permission request to
     * deny). */
    int escape();

    /* Takes every pending request down at once, regardless of kind -- used
     * for disconnect and session-switch, where the bridge has already given
     * up on (or rebuilt past) whatever was outstanding. Distinct from the
     * per-kind removal that PERMEND/HALTEND do through frame(): those remove
     * exactly the one entry that was answered, this empties the stack. */
    void hide_all();
};

#endif
