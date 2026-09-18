/* The permission/halt bar. See app/bar.h for the design.
 *
 * One set of controls rather than two, because the 16-bit USER/GDI heaps on
 * 9x are a fixed 64KB that does not scale with RAM.
 *
 * BAR_PERM is Allow / Always / Deny for a tool call. BAR_HALT is a single
 * Continue, shown when the bridge has STOPPED the agent -- today that means a
 * conflicting edit was found, the user's copy was kept, and the agent must not
 * carry on until the user says they have finished with the file.
 *
 * main.c's version of this comment, before the bar moved here, said the two
 * states were "mutually exclusive by construction: a halted agent is not
 * running a tool and cannot be asking for permission". THAT WAS FALSE, and it
 * cost both of them. A halt is raised by a sync pass, which runs on the
 * guest's watcher and owes nothing to the agent's turn -- so a conflict is
 * found precisely WHILE the agent works, because the agent writing files is
 * what moves the tree. Each show() then overwrote the other's state:
 *
 *   PERMREQ over a halt -- the mode became BAR_PERM, so Continue answered the
 *   permission instead, and PERMEND then cleared the halt while the bridge
 *   still held it. No further bar could be raised (the bridge's raise is
 *   idempotent while one is outstanding) and RESUME could never be sent, so
 *   every latched path refused the agent's writes forever.
 *
 *   HALT over a permission -- the permission's pending flag was cleared, so
 *   Allow sent RESUME and the permission request was never answered at all,
 *   leaving the model waiting on a prompt nobody could reply to until the
 *   bridge's deadline.
 *
 * That was fixed by remembering the halt instead of losing it, but the fix
 * was four functions in main.c each hand-checking the others' state
 * (g_bar_mode plus satellite g_perm_ and g_halt_ globals) -- audit findings
 * 01.55 and 04.6. This file replaces that with a small bounded STACK of
 * pending requests and a descriptor table keyed by kind. The visible bar is
 * always DERIVED -- the highest-priority entry currently on the stack --
 * instead of stored redundantly, so "a permission takes the bar because it
 * has a deadline; a halt waits behind it and comes back when it clears"
 * falls out of the priority ordering below rather than being three separate
 * `if`s that have to agree with each other.
 */
#include "app/bar.h"
#include "core/str.h"
#include "core/log.h"
#include <string.h>
#include <stdlib.h>

#define PERMBTN_W 58

#define BAR_PERM 1
#define BAR_HALT 2
/* Phase 6 is expected to add a third kind here -- see app/bar.h. */

/* One pending request. `waiting` is PERM-only (PERMWAIT's queue depth);
 * `allows_always` is PERM-only (whether the bridge said a rule can be
 * expressed for this tool -- see the ShowWindow comment in bar_show below).
 * Both are simply unused, and harmless, on a HALT entry. */
typedef struct {
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
} rc_bar_entry;

/* Perm plus halt today, with one spare slot for Phase 6's third kind -- see
 * app/bar.h. Every allocation in this codebase is bounded and small; a stack
 * that could grow without limit would just be the same defect PERMREQ's
 * re-entrancy guard below exists to prevent, one level up. */
#define BAR_STACK_MAX 3
static rc_bar_entry g_stack[BAR_STACK_MAX];
static int          g_depth;

typedef struct {
    int         kind;
    int         priority;   /* lower wins the bar; see bar_visible() */
    const char *primary;    /* the SAME button, relabelled per kind */
    const char *secondary;  /* NULL = this kind never offers it */
    const char *tertiary;
    int         blocks_send;
} rc_bar_desc;

/* Halt has no Deny and offers Always to nobody -- see bar_show's ShowWindow
 * calls, which read secondary/tertiary straight out of this table instead of
 * branching on kind. Perm wins the bar over halt (priority 0 < 1): it has a
 * deadline, and a halt does not. */
static const rc_bar_desc g_desc[] = {
    { BAR_PERM, 0, "Allow",    "Always", "Deny", 1 },
    { BAR_HALT, 1, "Continue", NULL,     NULL,   0 },
};

static const rc_bar_desc *bar_desc(int kind)
{
    unsigned i;
    for (i = 0; i < sizeof(g_desc) / sizeof(g_desc[0]); i++)
        if (g_desc[i].kind == kind) return &g_desc[i];
    return NULL;   /* unreachable for a kind this file pushed itself */
}

static HWND g_text, g_allow, g_always, g_deny;
static rc_net *g_net;
static rc_bar_send_fn g_send;
static void (*g_changed)(void);

void rc_bar_create(HWND parent, HINSTANCE inst, HFONT font, rc_net *net,
                    rc_bar_send_fn send, void (*changed)(void))
{
    g_net = net;
    g_send = send;
    g_changed = changed;

    /* Created hidden and shown with ShowWindow rather than created on
     * demand: the 16-bit USER/GDI heaps on 9x are a fixed 64KB, and churning
     * window handles is exactly what this codebase avoids. */
    g_text = CreateWindowExA(0, "STATIC", "",
        WS_CHILD | SS_LEFTNOWORDWRAP | SS_ENDELLIPSIS,
        0, 0, 0, 0, parent, (HMENU)ID_PERM_TEXT, inst, NULL);
    g_allow = CreateWindowExA(0, "BUTTON", "Allow",
        WS_CHILD | BS_PUSHBUTTON,
        0, 0, 0, 0, parent, (HMENU)ID_PERM_ALLOW, inst, NULL);
    g_always = CreateWindowExA(0, "BUTTON", "Always",
        WS_CHILD | BS_PUSHBUTTON,
        0, 0, 0, 0, parent, (HMENU)ID_PERM_ALWAYS, inst, NULL);
    g_deny = CreateWindowExA(0, "BUTTON", "Deny",
        WS_CHILD | BS_PUSHBUTTON,
        0, 0, 0, 0, parent, (HMENU)ID_PERM_DENY, inst, NULL);
    if (!g_text || !g_allow || !g_always || !g_deny)
        rc_log("bar: a control failed to create (GetLastError=%lu)",
               (unsigned long)GetLastError());

    SendMessageA(g_text,   WM_SETFONT, (WPARAM)font, TRUE);
    SendMessageA(g_allow,  WM_SETFONT, (WPARAM)font, TRUE);
    SendMessageA(g_always, WM_SETFONT, (WPARAM)font, TRUE);
    SendMessageA(g_deny,   WM_SETFONT, (WPARAM)font, TRUE);
}

/* bar_visible: the highest-priority entry on the stack, or NULL. rc_bar_move,
 * rc_bar_blocks_send and bar_show all derive the SAME visible entry from the
 * SAME stack rather than reading a cached "which one is showing" flag, which
 * is what let the old g_bar_mode and g_halt_id disagree with each other. */
static rc_bar_entry *bar_visible(void)
{
    int i, besti = -1, bestp = 0x7fffffff;
    for (i = 0; i < g_depth; i++) {
        int p = bar_desc(g_stack[i].kind)->priority;
        if (p < bestp) { bestp = p; besti = i; }
    }
    return besti >= 0 ? &g_stack[besti] : NULL;
}

/* Right-to-left: the buttons are fixed width and the label takes whatever is
 * left, so a long tool description truncates rather than pushing Deny off
 * the edge.
 *
 * A halt has only Continue, and the hidden buttons must not be given screen
 * space -- MoveWindow on a hidden control costs nothing, but leaving the gap
 * would put Continue in the middle of the bar with a hole beside it. */
void rc_bar_move(int cx, int y, int right_edge)
{
    rc_bar_entry *e = bar_visible();
    const rc_bar_desc *d;
    int bx = right_edge - PERMBTN_W;

    if (!e) return;
    d = bar_desc(e->kind);

    if (d->tertiary) {
        MoveWindow(g_deny, bx, y, PERMBTN_W, PERM_H - 4, TRUE);
        bx -= PERMBTN_W + 2;
    }
    /* Reserves the button's width only when Always will actually be shown
     * (this request allows a rule, not just this kind in the abstract) --
     * unlike the pairwise version this replaced, which reserved space for
     * Always in every PERM bar whether or not the button ended up hidden.
     * A minor behaviour change, not one of this pass's named findings: it
     * gives the label a few more pixels when there is nothing to reserve
     * for, which only helps 04.6's "the text is too narrow" complaint. */
    if (d->secondary && e->allows_always) {
        MoveWindow(g_always, bx, y, PERMBTN_W, PERM_H - 4, TRUE);
        bx -= PERMBTN_W + 2;
    }
    MoveWindow(g_allow, bx, y, PERMBTN_W, PERM_H - 4, TRUE);
    /* The "4" here is main.c's GUTTER, which this module cannot see by name
     * (it is a plain #define in main.cpp, not exported by any header) --
     * same value, spelled out. */
    MoveWindow(g_text, cx, y + 4, bx - cx - 4, PERM_H - 6, TRUE);
}

int rc_bar_active(void)
{
    return g_depth > 0;
}

int rc_bar_blocks_send(void)
{
    rc_bar_entry *e = bar_visible();
    return e && bar_desc(e->kind)->blocks_send;
}

static rc_bar_entry *bar_find(int kind)
{
    int i;
    for (i = 0; i < g_depth; i++)
        if (g_stack[i].kind == kind) return &g_stack[i];
    return NULL;
}

/* Puts g_text/g_allow/g_always/g_deny on screen for whichever entry is now
 * visible (or takes them all down if nothing is). Called only from push()
 * and pop() -- a visibility or label change, not PERMWAIT's queue-depth
 * ticks, which only ever touch the text (see bar_relabel). */
static void bar_show(void)
{
    rc_bar_entry *e = bar_visible();

    if (!e) {
        ShowWindow(g_text,   SW_HIDE);
        ShowWindow(g_allow,  SW_HIDE);
        ShowWindow(g_always, SW_HIDE);
        ShowWindow(g_deny,   SW_HIDE);
    } else {
        const rc_bar_desc *d = bar_desc(e->kind);
        char buf[256];

        if (e->waiting > 0)
            rc_strf(buf, sizeof(buf), "%s  (+%d waiting)",
                      e->base_text, e->waiting);
        else
            rc_strf(buf, sizeof(buf), "%s", e->base_text);
        SetWindowTextA(g_text, buf);
        /* The SAME button, relabelled per kind -- see the desc table. */
        SetWindowTextA(g_allow, d->primary);
        ShowWindow(g_text,  SW_SHOW);
        ShowWindow(g_allow, SW_SHOW);
        /* "Always" is offered only when the bridge says a rule can be
         * expressed AND this kind's descriptor allows it at all. For a
         * machine tool it never can -- MCP rules cannot carry specifiers, so
         * the only rule available is the whole tool, and pressing it once
         * would hand over the machine for the rest of the session. */
        ShowWindow(g_always, (d->secondary && e->allows_always)
                              ? SW_SHOW : SW_HIDE);
        ShowWindow(g_deny, d->tertiary ? SW_SHOW : SW_HIDE);
        SetFocus(g_allow);
    }
    if (g_changed) g_changed();
}

/* PERMWAIT alone: relabel in place without touching visibility, layout or
 * Send's enabled state. Keeping this separate from bar_show is what stops a
 * queue-depth tick from stealing focus off the input box every time one
 * arrives. */
static void bar_relabel(rc_bar_entry *e)
{
    char buf[256];
    if (e->waiting > 0)
        rc_strf(buf, sizeof(buf), "%s  (+%d waiting)", e->base_text, e->waiting);
    else
        rc_strf(buf, sizeof(buf), "%s", e->base_text);
    SetWindowTextA(g_text, buf);
}

/* Push a request onto the stack, or refresh it in place if the same kind and
 * id are already there (PERMSTATE re-announcing a live request after a
 * reconnect).
 *
 * A second request of the SAME kind with a DIFFERENT id is audit finding
 * 01.13: unguarded, the old code silently replaced g_perm_id (or g_halt_id)
 * and the first request became unanswerable forever -- the bridge's raise is
 * idempotent while one is outstanding, so no corrected bar could ever be
 * raised for it either. The bridge is supposed to serialise so this should
 * not happen, but the guest should not trust that with nothing checking it.
 * Refusing the new one keeps the invariant that whatever IS on screen stays
 * answerable; at worst the refused request goes unanswered until its own
 * deadline, rather than both of them going unanswered forever. */
static void bar_push(int kind, const char *id, const char *text,
                      int allows_always)
{
    rc_bar_entry *e = bar_find(kind);

    if (e) {
        if (strcmp(e->id, id) == 0) {
            rc_strf(e->base_text, sizeof(e->base_text), "%s", text);
            e->waiting = 0;
            e->allows_always = allows_always;
            bar_show();
            return;
        }
        rc_log("bar: kind %d already has id \"%s\" outstanding, refusing "
               "new id \"%s\" (see audit finding 01.13)", kind, e->id, id);
        return;
    }
    if (g_depth >= BAR_STACK_MAX) {
        rc_log("bar: stack full (%d), dropping kind %d id \"%s\"",
               BAR_STACK_MAX, kind, id);
        return;
    }
    e = &g_stack[g_depth++];
    e->kind = kind;
    rc_strcpy(e->id, sizeof(e->id), id);
    rc_strf(e->base_text, sizeof(e->base_text), "%s", text);
    e->waiting = 0;
    e->allows_always = allows_always;
    bar_show();
}

/* Remove exactly the one entry that was answered -- keyed on kind AND id,
 * never a blind pop: a PERMEND/HALTEND can arrive for a request that is not
 * (or no longer) on the stack at all -- resolved by a deadline, a disconnect
 * or a session rebuild while a reply was in flight -- and popping blindly on
 * that would drop whatever unrelated entry happened to be on top.
 *
 * Popping a kind that is not the visible one (a halt remembered behind a
 * permission bar, say) used to need its own explicit branch in main.c,
 * because g_halt_id[0] meant two different things depending on g_bar_mode
 * and HALTEND had to know which. Here it does not: removing the entry and
 * re-deriving bar_visible() handles both "it was on screen" and "it was
 * waiting behind something else" the same way, because there was never a
 * second place recording which one that was. */
static void bar_pop(int kind, const char *id)
{
    int i;
    for (i = 0; i < g_depth; i++) {
        if (g_stack[i].kind == kind && strcmp(g_stack[i].id, id) == 0) {
            for (; i < g_depth - 1; i++) g_stack[i] = g_stack[i + 1];
            g_depth--;
            bar_show();
            return;
        }
    }
}

int rc_bar_frame(rc_frame *f)
{
    if (strcmp(f->type, "PERMREQ") == 0 || strcmp(f->type, "PERMSTATE") == 0) {
        /* PERMSTATE is the same request re-issued after a reconnect. The
         * guest never sends `from=`, so the bridge's replay buffer cannot
         * bring the bar back by itself. */
        const char *id = rc_frame_get(f, "id");
        const char *allows = rc_frame_get(f, "allows");
        bar_push(BAR_PERM, id ? id : "", f->body ? f->body : "",
                 allows && *allows == '1');
        return 1;
    }
    if (strcmp(f->type, "HALT") == 0) {
        /* The bridge has stopped the agent and wants the user to decide when
         * it may go on. Today that is a conflicting edit: their copy was
         * kept, the agent's was preserved, and it must not touch the file
         * again until they have finished with it.
         *
         * The explanation is already in the transcript as a NOTICE -- this is
         * only the button, so the text here is one short line rather than the
         * whole story, which would not fit the bar at 640x480 anyway. */
        const char *id = rc_frame_get(f, "id");
        const char *text = f->body && *f->body
                          ? f->body : "Stopped: a conflicting edit was found";
        bar_push(BAR_HALT, id ? id : "", text, 0);
        return 1;
    }
    if (strcmp(f->type, "PERMWAIT") == 0) {
        /* Only the queue depth changed; the request is the same one. */
        const char *id = rc_frame_get(f, "id");
        const char *n = rc_frame_get(f, "waiting");
        rc_bar_entry *e = bar_find(BAR_PERM);
        if (e && id && strcmp(id, e->id) == 0 && n) {
            rc_log("perm: %s more request(s) queued", n);
            e->waiting = atoi(n);
            if (e->waiting < 0) e->waiting = 0;
            bar_relabel(e);
        }
        return 1;
    }
    if (strcmp(f->type, "PERMEND") == 0) {
        const char *id = rc_frame_get(f, "id");
        rc_bar_entry *e = id ? bar_find(BAR_PERM) : NULL;
        if (e && strcmp(id, e->id) == 0)
            bar_pop(BAR_PERM, id);
        else if (id)
            rc_log("perm: PERMEND for %s, not the one on screen", id);
        return 1;
    }
    if (strcmp(f->type, "HALTEND") == 0) {
        /* Keyed on the id for the same reason PERMEND is: the bridge can
         * resolve a halt on its own -- the agent called sync_resolve, or the
         * session was rebuilt -- while the bar is still up. */
        const char *id = rc_frame_get(f, "id");
        rc_bar_entry *e = id ? bar_find(BAR_HALT) : NULL;
        if (e && strcmp(id, e->id) == 0)
            bar_pop(BAR_HALT, id);
        return 1;
    }
    return 0;
}

/* The reply keys/vals shape perm_reply used to build inline; both call sites
 * below (Allow/Always/Deny) need exactly this. */
static void bar_reply(rc_bar_entry *e, const char *action)
{
    const char *keys[2];
    const char *vals[2];
    keys[0] = "id";     vals[0] = e->id;
    keys[1] = "action"; vals[1] = action;
    /* Checked, which it once was not. A lost PERMREPLY leaves the model
     * waiting on a prompt nobody can answer until the bridge's deadline
     * expires and auto-denies -- and the user, who pressed Allow, is told
     * nothing. */
    g_send("Your permission answer", "PERMREPLY", keys, vals, 2, NULL, 0);
    /* The bar comes down on PERMEND, not here: the bridge owns the state
     * machine, and a reply can lose a race with a deadline or a disconnect. */
}

int rc_bar_command(int id)
{
    rc_bar_entry *e;

    if (id != ID_PERM_ALLOW && id != ID_PERM_ALWAYS && id != ID_PERM_DENY)
        return 0;

    e = bar_visible();
    if (!e) return 1;   /* buttons are hidden when nothing is visible */

    if (e->kind == BAR_HALT) {
        /* One button, two meanings -- Allow and Continue share the same
         * physical control, relabelled per the desc table in bar_show. */
        if (id == ID_PERM_ALLOW) {
            const char *k[1]; const char *v[1];
            k[0] = "id"; v[0] = e->id;
            /* The bar comes down on HALTEND, not here -- the same rule
             * bar_reply follows, and for the same reason. rc_net_send
             * returns -1 on a closed link, a full queue or an oversized
             * frame, and hiding regardless meant a press that never left the
             * machine looked answered: the bridge still held the halt, its
             * raise is idempotent so no new bar could appear, and every
             * latched path refused the agent's writes for good.
             *
             * This goes straight to rc_net_send rather than through the
             * injected `send` (main.c's ui_send): ui_send's failure also
             * pushes a transcript error via the HTML view, and RESUME's
             * failure is reported on the bar's own text instead -- an
             * asymmetry carried over unchanged from before this file
             * existed, not something this pass introduced. */
            if (rc_net_send(g_net, "RESUME", k, v, 1, NULL, 0) < 0)
                SetWindowTextA(g_text,
                               "Not sent -- no link. Press Continue again.");
        }
        return 1;
    }

    if (id == ID_PERM_ALLOW)       bar_reply(e, "allow");
    else if (id == ID_PERM_ALWAYS) bar_reply(e, "always");
    else                            bar_reply(e, "deny");
    return 1;
}

int rc_bar_escape(void)
{
    rc_bar_entry *e = bar_visible();
    if (!e || e->kind != BAR_PERM) return 0;
    bar_reply(e, "deny");
    return 1;
}

void rc_bar_hide_all(void)
{
    if (!g_depth) return;
    g_depth = 0;
    bar_show();
}
