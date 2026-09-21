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
#include "core/rc_cpp.h"
#include <string.h>
#include <stdlib.h>

#define PERMBTN_W 58

typedef struct {
    int         kind;
    int         priority;   /* lower wins the bar; see rc_bar::visible */
    const char *primary;    /* the SAME button, relabelled per kind */
    const char *secondary;  /* NULL = this kind never offers it */
    const char *tertiary;
    int         blocks_send;
} rc_bar_desc;

/* Halt has no Deny and offers Always to nobody -- see rc_bar::show's
 * ShowWindow calls, which read secondary/tertiary straight out of this table
 * instead of branching on kind. Perm wins the bar over halt (priority 0 < 1):
 * it has a deadline, and a halt does not. */
static const rc_bar_desc g_desc[] = {
    /* These carry the '&' too, because show() writes `primary` straight into
     * the button with SetWindowTextA -- so a table entry without one silently
     * removes the mnemonic the constructor set up. Continue takes N, which
     * Deny also uses: the two are never on screen together. */
    { BAR_PERM, 0, "A&llow",    "Al&ways", "De&ny", 1 },
    { BAR_HALT, 1, "Co&ntinue", NULL,      NULL,    0 },
};

static const rc_bar_desc *bar_desc(int kind)
{
    unsigned i;
    for (i = 0; i < sizeof(g_desc) / sizeof(g_desc[0]); i++)
        if (g_desc[i].kind == kind) return &g_desc[i];
    return NULL;   /* unreachable for a kind this file pushed itself */
}

/* Is a text control focused right now -- i.e. is somebody mid-sentence?
 *
 * By window CLASS rather than by comparing against the input box's HWND, so
 * this needs no coupling to rc_input and covers the host dialog's fields and
 * any edit added later for free. GetFocus() answers for the calling thread
 * only, which is the right scope: show() runs on the window thread, and a
 * control focused on some other thread is not the one about to receive this
 * user's next keystroke. */
static int typing_now(void)
{
    HWND f = GetFocus();
    char cls[16];
    if (!f) return 0;
    if (!GetClassNameA(f, cls, sizeof(cls))) return 0;
    return _stricmp(cls, "EDIT") == 0;
}

rc_bar::rc_bar(HWND parent, HINSTANCE inst, HFONT font, rc_net *net_,
               rc_bar_send_fn send_, void (*changed_)(void))
    : depth(0), net(net_), send(send_), changed(changed_)
{
    /* Created hidden and shown with ShowWindow rather than created on
     * demand: the 16-bit USER/GDI heaps on 9x are a fixed 64KB, and churning
     * window handles is exactly what this codebase avoids. */
    /* SS_NOPREFIX, because this STATIC shows a COMMAND LINE.
     *
     * Without it a STATIC treats '&' as a mnemonic introducer: "&&" renders
     * as a single "&" and a lone "&" vanishes and underlines the next
     * character. So `rm -rf build && make` was displayed as
     * `rm -rf build & make`, and `cmd /c backup & del *.bak` as
     * `cmd /c backup  del *.bak` -- the user approving a visibly different
     * command from the one that would run, and '&', '&&' and '||' are exactly
     * the operators that turn one command into two. The bar's whole job is
     * that what is shown is what is approved. chatlist.cpp already passes
     * DT_NOPREFIX for the same reason. */
    text = CreateWindowExA(0, "STATIC", "",
        WS_CHILD | SS_LEFTNOWORDWRAP | SS_ENDELLIPSIS | SS_NOPREFIX,
        0, 0, 0, 0, parent, (HMENU)ID_PERM_TEXT, inst, NULL);
    /* WS_TABSTOP on all three, and mnemonics. These were mouse-only, which
     * is the worst place in the app for that: the bar is the one control the
     * program STOPS for. They carry the whole keyboard route now -- show()
     * no longer moves focus here when someone is typing, deliberately, so Tab
     * and the mnemonics below are how a bar raised mid-sentence gets an
     * answer without the user reaching for the mouse.
     *
     * L/W/N, and NOT the obvious A/L/D. The comment here used to read "A/L/D
     * avoid the menu bar's F/C/M/E/H", which was true when it was written and
     * stopped being true when app/optmenu.cpp added the Mode and Commands
     * menus: the bar now owns F, C, **D** (Mo&de), M (&Model), E (&Effort),
     * **A** (Comm&ands) and H. A menu-bar mnemonic beats a child button's, so
     * Alt+A opened Commands and Alt+D opened Mode -- two of the bar's three
     * answers were unreachable from the keyboard.
     *
     * Found by pressing Alt+A on the real machine with a permission prompt
     * up and watching the Commands menu drop down. It matters more since the
     * bar stopped taking focus on its own (see show()): that change is right,
     * and it is only safe if the keyboard route it leaves behind actually
     * works.
     *
     * L(al&low), W(al&ways), N(de&ny) are all free of the menu bar. Continue
     * takes N too -- it and Deny are never on screen together, because a halt
     * shows one button and a permission prompt shows the other three.
     *
     * The descriptor table above carries the '&' too, because show() writes
     * `primary` into this button with SetWindowTextA on every push -- a table
     * entry without one would silently undo the caption set here. relabel()
     * is different: it writes into the STATIC, not into these buttons. */
    allow = CreateWindowExA(0, "BUTTON", "A&llow",
        WS_CHILD | WS_TABSTOP | BS_PUSHBUTTON,
        0, 0, 0, 0, parent, (HMENU)ID_PERM_ALLOW, inst, NULL);
    always = CreateWindowExA(0, "BUTTON", "Al&ways",
        WS_CHILD | WS_TABSTOP | BS_PUSHBUTTON,
        0, 0, 0, 0, parent, (HMENU)ID_PERM_ALWAYS, inst, NULL);
    deny = CreateWindowExA(0, "BUTTON", "De&ny",
        WS_CHILD | WS_TABSTOP | BS_PUSHBUTTON,
        0, 0, 0, 0, parent, (HMENU)ID_PERM_DENY, inst, NULL);
    if (!text || !allow || !always || !deny)
        rc_log("bar: a control failed to create (GetLastError=%lu)",
               (unsigned long)GetLastError());

    SendMessageA(text,   WM_SETFONT, (WPARAM)font, TRUE);
    SendMessageA(allow,  WM_SETFONT, (WPARAM)font, TRUE);
    SendMessageA(always, WM_SETFONT, (WPARAM)font, TRUE);
    SendMessageA(deny,   WM_SETFONT, (WPARAM)font, TRUE);
}

rc_bar *rc_bar::create(HWND parent, HINSTANCE inst, HFONT font, rc_net *net,
                        rc_bar_send_fn send, void (*changed)(void))
{
    return new rc_bar(parent, inst, font, net, send, changed);
}

rc_bar_entry *rc_bar::visible()
{
    int i, besti = -1, bestp = 0x7fffffff;
    for (i = 0; i < depth; i++) {
        int p = bar_desc(stack[i].kind)->priority;
        if (p < bestp) { bestp = p; besti = i; }
    }
    return besti >= 0 ? &stack[besti] : NULL;
}

/* Right-to-left: the buttons are fixed width and the label takes whatever is
 * left, so a long tool description truncates rather than pushing Deny off
 * the edge.
 *
 * A halt has only Continue, and the hidden buttons must not be given screen
 * space -- MoveWindow on a hidden control costs nothing, but leaving the gap
 * would put Continue in the middle of the bar with a hole beside it. */
void rc_bar::move(int cx, int y, int right_edge)
{
    rc_bar_entry *e = visible();
    const rc_bar_desc *d;
    int bx = right_edge - PERMBTN_W;

    if (!e) return;
    d = bar_desc(e->kind);

    if (d->tertiary) {
        MoveWindow(deny, bx, y, PERMBTN_W, PERM_H - 4, TRUE);
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
        MoveWindow(always, bx, y, PERMBTN_W, PERM_H - 4, TRUE);
        bx -= PERMBTN_W + 2;
    }
    MoveWindow(allow, bx, y, PERMBTN_W, PERM_H - 4, TRUE);
    /* The "4" here is main.c's GUTTER, which this module cannot see by name
     * (it is a plain #define in main.cpp, not exported by any header) --
     * same value, spelled out. */
    MoveWindow(text, cx, y + 4, bx - cx - 4, PERM_H - 6, TRUE);
}

int rc_bar::active() const
{
    return depth > 0;
}

int rc_bar::blocks_send()
{
    rc_bar_entry *e = visible();
    return e && bar_desc(e->kind)->blocks_send;
}

rc_bar_entry *rc_bar::find_entry(int kind)
{
    int i;
    for (i = 0; i < depth; i++)
        if (stack[i].kind == kind) return &stack[i];
    return NULL;
}

/* Puts text/allow/always/deny on screen for whichever entry is now visible
 * (or takes them all down if nothing is). Called only from push() and pop()
 * -- a visibility or label change, not PERMWAIT's queue-depth ticks, which
 * only ever touch the text (see relabel). */
void rc_bar::show()
{
    rc_bar_entry *e = visible();

    if (!e) {
        /* The keyboard is handed back by bar_changed() in main.cpp, which
         * runs at the end of this function and is the layer that knows where
         * focus belongs. Hiding the focused window leaves focus on the frame,
         * which has no text handling -- so on the most common path in the app
         * (press Allow, PERMEND arrives, pop(), show()) the user carried on
         * typing and the characters went nowhere until they clicked the input
         * box. Win32 does not reassign focus when the focused window is
         * hidden. */
        ShowWindow(text,   SW_HIDE);
        ShowWindow(allow,  SW_HIDE);
        ShowWindow(always, SW_HIDE);
        ShowWindow(deny,   SW_HIDE);
    } else {
        const rc_bar_desc *d = bar_desc(e->kind);
        char buf[256];

        if (e->waiting > 0)
            rc_strf(buf, sizeof(buf), "%s  (+%d waiting)",
                      e->base_text, e->waiting);
        else
            rc_strf(buf, sizeof(buf), "%s", e->base_text);
        SetWindowTextA(text, buf);
        /* The SAME button, relabelled per kind -- see the desc table. */
        SetWindowTextA(allow, d->primary);
        ShowWindow(text,  SW_SHOW);
        ShowWindow(allow, SW_SHOW);
        /* "Always" is offered only when the bridge says a rule can be
         * expressed AND this kind's descriptor allows it at all. For a
         * machine tool it never can -- MCP rules cannot carry specifiers, so
         * the only rule available is the whole tool, and pressing it once
         * would hand over the machine for the rest of the session. */
        ShowWindow(always, (d->secondary && e->allows_always)
                            ? SW_SHOW : SW_HIDE);
        ShowWindow(deny, d->tertiary ? SW_SHOW : SW_HIDE);
        /* NOT WHILE THE USER IS TYPING.
         *
         * This was an unconditional SetFocus(allow), and push() runs straight
         * off WM_RC_FRAME at whatever moment the bridge chooses -- a HALT
         * "owes nothing to the agent's turn" (see the top of this file), so a
         * bar appearing mid-sentence is the normal case, not a race. Focus
         * jumped from the input box to Continue, and a BS_PUSHBUTTON with
         * focus is pressed by VK_SPACE: the next space in the sentence being
         * typed sent RESUME, lifting the write refusal so the agent merged
         * over the file the user was editing. The same sequence on a PERMREQ
         * approved a tool call nobody had read.
         *
         * CLAUDE.md's rule is that pressing Continue IS the confirmation. The
         * whole conflict-safety design rests on that keypress being
         * deliberate, and a keystroke in a sentence is not.
         *
         * Taking focus is still right for a bar raised while the user is idle
         * -- that is the argument at the button creation above, and it stands
         * -- so the test is whether a text control has focus, not whether the
         * bar deserves it. When it does not take focus the bar is still fully
         * reachable: the buttons carry A/L/D mnemonics and WS_TABSTOP, and
         * main.cpp's loop runs IsDialogMessageA, so Alt+A/Alt+L/Alt+D work. */
        if (!typing_now()) SetFocus(allow);
    }
    if (changed) changed();
}

/* PERMWAIT alone: relabel in place without touching visibility, layout or
 * Send's enabled state. Keeping this separate from show() is what stops a
 * queue-depth tick from stealing focus off the input box every time one
 * arrives. */
void rc_bar::relabel(rc_bar_entry *e)
{
    char buf[256];
    if (e->waiting > 0)
        rc_strf(buf, sizeof(buf), "%s  (+%d waiting)", e->base_text, e->waiting);
    else
        rc_strf(buf, sizeof(buf), "%s", e->base_text);
    SetWindowTextA(text, buf);
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
void rc_bar::push(int kind, const char *id, const char *text_, int allows_always)
{
    rc_bar_entry *e = find_entry(kind);

    if (e) {
        if (strcmp(e->id, id) == 0) {
            rc_strf(e->base_text, sizeof(e->base_text), "%s", text_);
            e->waiting = 0;
            e->allows_always = allows_always;
            show();
            return;
        }
        rc_log("bar: kind %d already has id \"%s\" outstanding, refusing "
               "new id \"%s\" (see audit finding 01.13)", kind, e->id, id);
        return;
    }
    if (depth >= BAR_STACK_MAX) {
        rc_log("bar: stack full (%d), dropping kind %d id \"%s\"",
               BAR_STACK_MAX, kind, id);
        return;
    }
    e = &stack[depth++];
    e->kind = kind;
    rc_strcpy(e->id, sizeof(e->id), id);
    rc_strf(e->base_text, sizeof(e->base_text), "%s", text_);
    e->waiting = 0;
    e->allows_always = allows_always;
    show();
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
 * re-deriving visible() handles both "it was on screen" and "it was waiting
 * behind something else" the same way, because there was never a second
 * place recording which one that was. */
void rc_bar::pop(int kind, const char *id)
{
    int i;
    for (i = 0; i < depth; i++) {
        if (stack[i].kind == kind && strcmp(stack[i].id, id) == 0) {
            for (; i < depth - 1; i++) stack[i] = stack[i + 1];
            depth--;
            show();
            return;
        }
    }
}

int rc_bar::frame(rc_frame *f)
{
    if (strcmp(f->type, "PERMREQ") == 0 || strcmp(f->type, "PERMSTATE") == 0) {
        /* PERMSTATE is the same request re-issued after a reconnect. The
         * guest never sends `from=`, so the bridge's replay buffer cannot
         * bring the bar back by itself. */
        const char *id = rc_frame_get(f, "id");
        const char *allows = rc_frame_get(f, "allows");
        push(BAR_PERM, id ? id : "", f->body ? f->body : "",
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
        const char *txt = f->body && *f->body
                          ? f->body : "Stopped: a conflicting edit was found";
        push(BAR_HALT, id ? id : "", txt, 0);
        return 1;
    }
    if (strcmp(f->type, "PERMWAIT") == 0) {
        /* Only the queue depth changed; the request is the same one. */
        const char *id = rc_frame_get(f, "id");
        const char *n = rc_frame_get(f, "waiting");
        rc_bar_entry *e = find_entry(BAR_PERM);
        if (e && id && strcmp(id, e->id) == 0 && n) {
            rc_log("perm: %s more request(s) queued", n);
            e->waiting = atoi(n);
            if (e->waiting < 0) e->waiting = 0;
            relabel(e);
        }
        return 1;
    }
    if (strcmp(f->type, "PERMEND") == 0) {
        const char *id = rc_frame_get(f, "id");
        rc_bar_entry *e = id ? find_entry(BAR_PERM) : NULL;
        if (e && strcmp(id, e->id) == 0)
            pop(BAR_PERM, id);
        else if (id)
            rc_log("perm: PERMEND for %s, not the one on screen", id);
        return 1;
    }
    if (strcmp(f->type, "HALTEND") == 0) {
        /* Keyed on the id for the same reason PERMEND is: the bridge can
         * resolve a halt on its own -- the agent called sync_resolve, or the
         * session was rebuilt -- while the bar is still up. */
        const char *id = rc_frame_get(f, "id");
        rc_bar_entry *e = id ? find_entry(BAR_HALT) : NULL;
        if (e && strcmp(id, e->id) == 0)
            pop(BAR_HALT, id);
        return 1;
    }
    return 0;
}

/* The reply keys/vals shape this used to build inline; both call sites below
 * (Allow/Always/Deny) need exactly this. */
void rc_bar::reply(rc_bar_entry *e, const char *action)
{
    const char *keys[2];
    const char *vals[2];
    keys[0] = "id";     vals[0] = e->id;
    keys[1] = "action"; vals[1] = action;
    /* Checked, which it once was not. A lost PERMREPLY leaves the model
     * waiting on a prompt nobody can answer until the bridge's deadline
     * expires and auto-denies -- and the user, who pressed Allow, is told
     * nothing. */
    send("Your permission answer", "PERMREPLY", keys, vals, 2, NULL, 0);
    /* The bar comes down on PERMEND, not here: the bridge owns the state
     * machine, and a reply can lose a race with a deadline or a disconnect. */
}

int rc_bar::command(int id)
{
    rc_bar_entry *e;

    if (id != ID_PERM_ALLOW && id != ID_PERM_ALWAYS && id != ID_PERM_DENY)
        return 0;

    e = visible();
    if (!e) return 1;   /* buttons are hidden when nothing is visible */

    if (e->kind == BAR_HALT) {
        /* One button, two meanings -- Allow and Continue share the same
         * physical control, relabelled per the desc table in show(). */
        if (id == ID_PERM_ALLOW) {
            const char *k[1]; const char *v[1];
            k[0] = "id"; v[0] = e->id;
            /* The bar comes down on HALTEND, not here -- the same rule
             * reply() follows, and for the same reason. rc_net_send
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
            if (rc_net_send(net, "RESUME", k, v, 1, NULL, 0) < 0)
                SetWindowTextA(text,
                               "Not sent -- no link. Press Continue again.");
        }
        return 1;
    }

    if (id == ID_PERM_ALLOW)       reply(e, "allow");
    else if (id == ID_PERM_ALWAYS) reply(e, "always");
    else                            reply(e, "deny");
    return 1;
}

int rc_bar::escape()
{
    rc_bar_entry *e = visible();
    if (!e || e->kind != BAR_PERM) return 0;
    reply(e, "deny");
    return 1;
}

void rc_bar::hide_all()
{
    if (!depth) return;
    depth = 0;
    show();
}
