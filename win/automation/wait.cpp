#include "automation/wait.h"

#include <string.h>

UiWaiter::UiWaiter(UiWatch &watch, TreeWalker &walker, MsaaSource *msaa)
    : m_watch(watch), m_walker(walker), m_msaa(msaa), m_cancel(0)
{
}

static void beginReport(WaitReport &out, bool polled)
{
    out.end = UIWAIT_TIMEOUT;
    out.ms = 0;
    out.rechecks = 0;
    out.matchCount = 0;
    out.polled = polled;
    out.everMatched = false;
    out.haveNode = false;
    memset(&out.node, 0, sizeof(out.node));
    out.sampleN = 0;
}

/* One scan. True when the selector resolved to exactly one control, with
 * the node copied into the report. */
bool UiWaiter::scan(const Selector &sel, const WalkOptions &opt,
                    WaitReport &out, ResolveOutcome &outcome)
{
    Resolver r(sel, m_msaa);
    outcome = r.resolve(opt, m_walker);
    out.rechecks++;
    out.matchCount = r.matchCount();
    out.sampleN = 0;
    for (int i = 0; i < r.sampleCount() && i < RC_SAMPLE_MAX; i++)
        out.samples[out.sampleN++] = r.sample(i)->hwnd;
    if (outcome == RESOLVE_OK && r.match())
    {
        out.node = *r.match();
        out.haveNode = true;
        out.everMatched = true;
        return true;
    }
    if (r.matchCount() > 0)
        out.everMatched = true;
    out.haveNode = false;
    return false;
}

/* How long to block before looking again.
 *
 * Never the whole remaining budget, even with a live feed. The feed is what
 * makes a wait cheap; the backstop is what stops a missed event turning it
 * into a hang. Without a feed at all it is a plain poll. */
static DWORD sliceFor(bool polled, long left)
{
    DWORD cap = polled ? RC_WAIT_POLL_MS : RC_WAIT_BACKSTOP_MS;
    DWORD slice = (DWORD)left;
    if (slice > cap)
        slice = cap;
    return slice;
}

void UiWaiter::loop(WaitMode mode, const Selector &sel, const char *substring,
                    const WalkOptions &opt, DWORD budgetMs, WaitReport &out)
{
    bool polled = !m_watch.running();
    beginReport(out, polled);
    if (!substring)
        substring = "";

    DWORD t0 = GetTickCount();
    DWORD until = t0 + budgetMs;

    /* The subscription is already live -- the caller started the watch
     * before calling, which is the whole ordering rule -- so this first
     * check cannot miss a change that happens during it. Checking BEFORE
     * subscribing is the race: the thing appears in between, and the wait
     * then blocks for its whole timeout on a condition already true. */
    unsigned seq = m_watch.anySeq();
    ResolveOutcome outcome = RESOLVE_NONE;
    bool truncatedOnce = false;
    bool first = true;
    DWORD lastScan = t0;

    for (;;)
    {
        if (!first)
        {
            long left = (long)(until - GetTickCount());
            if (left <= 0)
                break;

            m_watch.settle(sliceFor(polled, left));
            if (cancelled())
            {
                out.end = UIWAIT_CANCELLED;
                out.ms = GetTickCount() - t0;
                return;
            }

            /* Two reasons to look again, and only two. An event says the
             * desktop changed. The backstop says we have not looked for a
             * while and the feed may have dropped something.
             *
             * Anything else -- and `settle` wakes on ANY message, not just
             * ours -- is not a reason, and skipping those is where the
             * thirty scans of a poll go. */
            unsigned now = m_watch.anySeq();
            bool changed = (now != seq);
            bool overdue = (long)(GetTickCount() - lastScan)
                           >= (long)RC_WAIT_BACKSTOP_MS;
            seq = now;
            if (!changed && !overdue)
                continue;
        }
        lastScan = GetTickCount();

        bool found = scan(sel, opt, out, outcome);
        if (outcome == RESOLVE_TRUNCATED)
            truncatedOnce = true;

        bool satisfied = false;
        switch (mode)
        {
        case MODE_CONTROL:
            satisfied = found;
            break;
        case MODE_TEXT:
            satisfied = found && strstr(out.node.text, substring) != NULL;
            break;
        case MODE_GONE:
            /* Gone means nothing matched, and a walk that ended early does
             * NOT mean that -- it means we did not look everywhere.
             * Reporting a truncated walk as "gone" is how a caller ends up
             * acting on a dialog that is still on screen. */
            satisfied = (out.matchCount == 0
                         && outcome != RESOLVE_TRUNCATED);
            break;
        }

        if (satisfied)
        {
            if (mode == MODE_GONE)
                out.haveNode = false;
            if (first)
                out.end = UIWAIT_ALREADY;
            else
                out.end = UIWAIT_SETTLED;
            out.ms = GetTickCount() - t0;
            return;
        }
        first = false;
    }

    out.ms = GetTickCount() - t0;
    if (mode == MODE_GONE)
    {
        out.haveNode = false;
        if (outcome == RESOLVE_TRUNCATED)
            out.end = UIWAIT_TRUNCATED;
        else
            out.end = UIWAIT_TIMEOUT;
        return;
    }

    /* Ambiguity is an answer and beats the clock: the control IS there, and
     * the caller needs to write a :nth rather than wait longer. */
    if (outcome == RESOLVE_AMBIGUOUS)
        out.end = UIWAIT_AMBIGUOUS;
    else if (truncatedOnce && !out.everMatched)
        out.end = UIWAIT_TRUNCATED;
    else
        out.end = UIWAIT_TIMEOUT;
    /* On a MODE_TEXT miss, `everMatched` is the useful half of the report:
     * it separates "no such control" from "that control never said that". */
}

void UiWaiter::forControl(const Selector &sel, const WalkOptions &opt,
                          DWORD budgetMs, WaitReport &out)
{
    loop(MODE_CONTROL, sel, 0, opt, budgetMs, out);
}

void UiWaiter::forText(const Selector &sel, const char *substring,
                       const WalkOptions &opt, DWORD budgetMs,
                       WaitReport &out)
{
    loop(MODE_TEXT, sel, substring, opt, budgetMs, out);
}

void UiWaiter::forGone(const Selector &sel, const WalkOptions &opt,
                       DWORD budgetMs, WaitReport &out)
{
    loop(MODE_GONE, sel, 0, opt, budgetMs, out);
}

void UiWaiter::forIdle(DWORD quietMs, DWORD budgetMs, WaitReport &out)
{
    bool polled = !m_watch.running();
    beginReport(out, polled);

    DWORD t0 = GetTickCount();
    DWORD until = t0 + budgetMs;

    /* With no feed there is nothing to be quiet: report it rather than
     * inventing a delay that looks like a measurement. */
    if (polled)
    {
        out.end = UIWAIT_TIMEOUT;
        out.ms = 0;
        return;
    }

    unsigned seq = m_watch.anySeq();
    DWORD quietSince = GetTickCount();

    for (;;)
    {
        long left = (long)(until - GetTickCount());
        if (left <= 0)
            break;

        long need = (long)quietMs - (long)(GetTickCount() - quietSince);
        if (need <= 0)
        {
            out.end = UIWAIT_SETTLED;
            out.ms = GetTickCount() - t0;
            return;
        }
        if (need > left)
            need = left;

        m_watch.settle((DWORD)need);
        if (cancelled())
        {
            out.end = UIWAIT_CANCELLED;
            out.ms = GetTickCount() - t0;
            return;
        }

        unsigned now = m_watch.anySeq();
        if (now != seq)
        {
            /* The clock restarts, not accumulates. "No event for 300ms" is
             * the question; a busy desktop should never satisfy it. */
            seq = now;
            quietSince = GetTickCount();
        }
    }

    out.ms = GetTickCount() - t0;
    if ((long)(GetTickCount() - quietSince) >= (long)quietMs)
        out.end = UIWAIT_SETTLED;
    else
        out.end = UIWAIT_TIMEOUT;
}
