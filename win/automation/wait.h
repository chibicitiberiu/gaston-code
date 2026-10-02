/* wait.h -- "until", without a poll and without a hang.
 *
 * Subscribe, THEN check, then wait. The other order races and the race is
 * silent: the thing can appear between the check and the subscription, and
 * the wait then blocks for its whole timeout on a condition that is already
 * true. Gate 0.15e built this ordering and measured it -- 1500 ms of waiting
 * cost two tree scans, where a 50 ms poll would have cost thirty.
 *
 * Every wait says WHY it ended, never a bare boolean. A wait that returns
 * false cannot tell a caller whether the control is absent, whether the
 * walk ran out of time before it could look, or whether the control was
 * there all along under a name that did not match -- and those call for
 * three different next moves.
 *
 * And a negative wait matters as much as a positive one. Something that
 * will never exist must come back at its deadline and say so. A primitive
 * that hangs instead is worse than none, because it hangs rarely, which is
 * the worst failure schedule there is.
 */
#ifndef RC_AUTOMATION_WAIT_H
#define RC_AUTOMATION_WAIT_H

#include "automation/resolve.h"
#include "automation/watch.h"

/* UIWAIT_ and not WAIT_. winbase.h already defines WAIT_TIMEOUT as 258L, so
 * an enumerator by that name silently becomes a long -- Watcom rejects the
 * assignment and the error names a type, not a macro, which takes a while to
 * read. */
enum WaitEnd
{
    UIWAIT_ALREADY = 0,   /* true before we waited -- the common case, free */
    UIWAIT_SETTLED,       /* became true while we watched */
    UIWAIT_TIMEOUT,       /* the budget ran out, and it never did */
    UIWAIT_AMBIGUOUS,     /* matched, but more than once; see matchCount */
    UIWAIT_TRUNCATED      /* a walk kept ending early; the answer is not known */
};

struct WaitReport
{
    WaitEnd end;
    DWORD   ms;          /* wall clock actually spent */
    int     rechecks;    /* tree scans it cost -- the number that matters */
    int     matchCount;  /* from the last scan */
    bool    polled;      /* no event feed; this was a poll after all */
    bool    everMatched; /* the control was seen at least once */
    bool    haveNode;
    Node    node;        /* the match, when haveNode */
    /* On ambiguity, which controls collided -- handles only, this session's,
     * so the reply can name them without a Node apiece on the stack. */
    int     sampleN;
    HWND    samples[RC_SAMPLE_MAX];
};

/* How often a wait rescans when there is no event feed to ride. Only
 * reached on a system without SetWinEventHook; Win98 SE has it. */
#define RC_WAIT_POLL_MS 150

/* And how long a wait will ride the feed before rescanning ANYWAY.
 *
 * Not belt and braces. A feed that drops one event in ten is worse than no
 * feed, because a wait built on it does not fail -- it hangs, for its whole
 * budget, and it does so rarely, which is the worst failure schedule there
 * is. Measured: Wine emits CREATE for a hidden popup and never emits
 * DESTROY, so `forGone` sat out its full four seconds on a window that had
 * been gone since the first tenth of a second.
 *
 * With this, a missed event costs a second of latency instead of the whole
 * wait, and a four-second wait still only pays four scans in the worst
 * case rather than the thirty a poll would. */
#define RC_WAIT_BACKSTOP_MS 1000

class UiWaiter
{
public:
    /* `watch` need not be started. If it is not -- or cannot be -- the waits
     * still work and say `polled` in the report, rather than refusing. */
    UiWaiter(UiWatch &watch, TreeWalker &walker, MsaaSource *msaa);

    /* Until the selector resolves to exactly one control. */
    void forControl(const Selector &sel, const WalkOptions &opt,
                    DWORD budgetMs, WaitReport &out);

    /* Until it resolves AND that control's text contains `substring`.
     * Separate from folding the text into the selector because the report
     * then distinguishes "the control never appeared" from "it was there
     * and never said that", and those are different bugs. */
    void forText(const Selector &sel, const char *substring,
                 const WalkOptions &opt, DWORD budgetMs, WaitReport &out);

    /* Until it resolves to nothing. UIWAIT_AMBIGUOUS cannot occur. */
    void forGone(const Selector &sel, const WalkOptions &opt,
                 DWORD budgetMs, WaitReport &out);

    /* Until the desktop has produced no event for `quietMs` in a row. What
     * you want after a click, before reading the result: the animation, the
     * repaint and the dialog construction are all still in flight. Costs no
     * tree scans at all. */
    void forIdle(DWORD quietMs, DWORD budgetMs, WaitReport &out);

private:
    /* All four waits are one loop with a different question at the bottom.
     * Keeping them one loop is what stops the backstop, the ordering and the
     * truncation rule from being got right in two places out of three. */
    enum WaitMode { MODE_CONTROL, MODE_TEXT, MODE_GONE };

    void loop(WaitMode mode, const Selector &sel, const char *substring,
              const WalkOptions &opt, DWORD budgetMs, WaitReport &out);
    bool scan(const Selector &sel, const WalkOptions &opt, WaitReport &out,
              ResolveOutcome &outcome);

    UiWatch    &m_watch;
    TreeWalker &m_walker;
    MsaaSource *m_msaa;

    UiWaiter(const UiWaiter &);
    UiWaiter &operator=(const UiWaiter &);
};

#endif
