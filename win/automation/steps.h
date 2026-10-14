/* steps.h -- a list of UI steps, run in order, stopping at the first failure.
 *
 * What the `input` tool sends. Actions (focus, click, close, keys, typing,
 * the mouse) and waits (for a control, for it to go, for its text, for the
 * desktop to go quiet) in one list, so "focus Notepad, type, Ctrl+S, wait
 * for Save As, type a name, Enter, wait for it to go" is one round trip to
 * a 200MHz machine instead of seven. There is no branching: a failed wait IS
 * the guard, and the agent is the control flow between lists.
 *
 * Three rules that are the point of this file rather than details of it:
 *
 *   - NOTHING STAYS HELD. Whatever a step pressed and the list did not
 *     release -- because it failed, was cancelled, timed out or simply forgot
 *     the `up` -- is released before `run` returns. A modifier left down on
 *     somebody's machine turns every later click into a Ctrl+click and
 *     nothing reports it.
 *
 *   - TYPING GOES WHERE IT WAS MEANT TO. The runner tracks the top-level
 *     window input is meant for (set by focus, click and a wait that matched
 *     a window) and, before every key and every character, checks that the
 *     foreground is that window or one it owns. A popup that steals focus
 *     mid-list stops the list with `focus_lost`; it does not receive the
 *     rest of the text.
 *
 *   - EVERY STEP IS BOUNDED, and so is the list. A step gets the lesser of
 *     its own timeout and what the list has left; a cancel flag, raised from
 *     another thread, stops it between steps and inside any wait.
 *
 * Knows nothing of the wire: the shared adapter (ui/uiengine.cpp) decodes
 * the frame into these and encodes the reports, and gremote's UI lane can do
 * the same with its own framing. See docs/REMOTE.md.
 */
#ifndef RC_AUTOMATION_STEPS_H
#define RC_AUTOMATION_STEPS_H

#include "automation/input.h"
#include "automation/resolve.h"
#include "automation/wait.h"
#include "automation/msaa.h"
#include "automation/calldeadline.h"

enum StepKind
{
    STEP_FOCUS = 0,
    STEP_CLICK,
    STEP_CLOSE,
    STEP_KEYDOWN,
    STEP_KEYUP,
    STEP_BTNDOWN,
    STEP_BTNUP,
    STEP_MOVE,
    STEP_MOVEBY,
    STEP_TAP,           /* one key, or one character as a chord, with mods */
    STEP_TYPE,          /* characters on the target's layout */
    STEP_WAIT,          /* a fixed pause */
    STEP_WAITFOR,
    STEP_WAITGONE,
    STEP_WAITTEXT,
    STEP_IDLE
};

/* Where a pointer step lands, in the order of preference the tool teaches:
 * a control's centre; an MSAA child of it by name (`item`); some text inside
 * an Edit control (`text`); an offset from its top-left (`at`); and only
 * when nothing can name the spot, screen pixels.
 *
 * PLAIN DATA, and every string is borrowed: the strings point into the
 * caller's buffer (the request body, decoded in place) and live as long as
 * it does. A first version held std::strings and cost 22KB of code in
 * unwinding and copying -- on an image whose load size is gated -- to carry
 * text that was already sitting in the frame. */
struct StepTarget
{
    const char *sel;        /* the control, or the window for `screen` */
    const char *item;       /* glob on an MSAA child's name */
    int         itemNth;    /* 1-based; 0 = must be unique */
    const char *text;       /* glob inside an Edit control's text */
    int         textNth;
    bool        edgeEnd;    /* land after the text rather than before it */
    bool        hasAt;
    int         atX, atY;
    bool        hasScreen;
    int         screenX, screenY;
};

/* One step. Plain data like its target; stepInit gives every field its
 * empty value, with "" for strings so nothing downstream tests for NULL. */
struct Step
{
    StepKind    kind;
    StepTarget  target;     /* pointer steps, focus, close, the waits */
    const char *in;         /* typing: the window it must go to */
    const char *key;        /* KEYDOWN/KEYUP/TAP: a KEYS_V1 name, or one
                               character for TAP */
    int         mods;       /* TAP: 1 Shift, 2 Ctrl, 4 Alt */
    const char *text;       /* TYPE: the characters; WAITTEXT: substring */
    unsigned    textLen;    /* TYPE may carry a NUL-free run of any byte */
    MouseButton button;
    int         count;      /* CLICK: 1, or 2 for a double click */
    int         dx, dy;     /* MOVEBY */
    DWORD       ms;         /* WAIT: the pause; IDLE: how long quiet */
    DWORD       timeoutMs;  /* 0: the list's default */
};

void stepInit(Step &s);
bool stepHasTarget(const Step &s);

/* How one step went. `status` is a short word the bridge has a sentence for
 * ("ok", "not_found", "focus_lost", ...); it points at a literal. */
struct StepReport
{
    const char *status;
    char        detail[160];
    char        hit[96];        /* the window a click actually reached */
    DWORD       ms;
    int         selStep;        /* not_found: which part of the selector */
    int         candN;          /* ambiguous: who collided */
    HWND        cand[RC_SAMPLE_MAX];
};

class StepSink
{
public:
    virtual ~StepSink() {}
    /* Called once per step that RAN, failing or not, in order. */
    virtual void onStep(int index, const Step &step, const StepReport &r) = 0;
};

struct StepLimits
{
    DWORD scriptMs;         /* the whole list */
    DWORD stepMs;           /* a step that names no timeout of its own */
    DWORD settleMs;         /* quiet waited for after each action; 0: none */
    StepLimits();
};

class DriveOverlay;

class StepRunner
{
public:
    /* `msaa` may be NULL or unavailable: an `item` target then fails with
     * `no_msaa` and nothing else changes. `deadline` likewise. */
    StepRunner(TreeWalker &walker, MsaaSource *msaa, UiWatch &watch,
               Input &input, CallDeadline *deadline);

    void setCancel(volatile LONG *flag) { m_cancel = flag; }

    /* The "agent is driving" strip, when there is one. It is a topmost
     * window of our own, so a click under it would stall on clickAt's
     * occlusion check -- SetForegroundWindow cannot raise a normal window
     * above a topmost one, so the settle loop just spins and the step
     * fails `occluded` naming our own class. It steps aside for the step
     * and comes back after. Null everywhere there is nobody to tell. */
    void setOverlay(DriveOverlay *ov) { m_overlay = ov; }

    /* Runs `steps` against `base` (scope, excludePid, timeouts for walks).
     * Returns the index of the step that failed, or -1 when every one did
     * what it was asked. `fail` holds that step's report. Held keys and
     * buttons are released before this returns, whatever happened. */
    int run(const Step *steps, int count, const WalkOptions &base,
            const StepLimits &limits, StepSink &sink, StepReport &fail);

    /* Whether `run` had anything to release at the end -- a list that
     * forgot an `up`. Said in the reply so the agent learns. */
    bool releasedAtEnd() const { return m_releasedAtEnd; }

private:
    /* What a click waits for the strip to go. Win16Mutex is the only thing
     * that can hold the indicator's thread, and it is also the state in
     * which a blind click is least predictable, so a timeout refuses. */
    enum { ASIDE_BUDGET_MS = 400 };

    bool runOne(const Step &s, DWORD budget, StepReport &r);
    bool resolveOne(const char *sel, DWORD budget, HWND &out,
                    StepReport &r);
    bool pointFor(const StepTarget &t, DWORD budget, HWND &expect,
                  int &x, int &y, StepReport &r);
    bool itemPoint(HWND h, const StepTarget &t, DWORD budget, int &x, int &y,
                   StepReport &r);
    bool textPoint(HWND h, const StepTarget &t, DWORD budget, int &x, int &y,
                   StepReport &r);
    bool doWait(const Step &s, DWORD budget, StepReport &r);
    bool doClose(const Step &s, DWORD budget, StepReport &r);
    bool keyboardTargetOk(const Step &s, DWORD budget, StepReport &r);
    void settle(DWORD left);
    bool cancelled() const { return m_cancel && *m_cancel; }

    TreeWalker    &m_walker;
    MsaaSource    *m_msaa;
    UiWatch       &m_watch;
    Input         &m_input;
    CallDeadline  *m_deadline;
    volatile LONG *m_cancel;
    DriveOverlay  *m_overlay;

    WalkOptions    m_base;
    StepLimits     m_limits;
    HWND           m_expectTop;
    bool           m_releasedAtEnd;

    StepRunner(const StepRunner &);
    StepRunner &operator=(const StepRunner &);
};

/* Status words, shared with the adapter so a typo is a compile error. */
extern const char *const STEP_OK;

#endif
