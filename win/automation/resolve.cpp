#include "automation/resolve.h"
#include "automation/msaa.h"

#include <string.h>

Resolver::Resolver(const Selector &sel, MsaaSource *msaa)
    : m_sel(sel),
      m_msaa(msaa),
      m_st((ResolveState *)HeapAlloc(GetProcessHeap(), 0,
                                     sizeof(ResolveState)))
{
    if (m_st.valid())
        memset(m_st.get(), 0, sizeof(ResolveState));
    m_chainN = 0;
    m_matches = 0;
    m_sampleN = 0;
    m_cacheN = 0;
    m_enriched = 0;
    m_nth = 0;
    /* Watcom is C++98 and this class is heap-allocated by some callers, so
     * every member is set by hand -- CLAUDE.md, "new T() does not zero a
     * class". A garbage hit count would report a step as present. */
    memset(m_stepHit, 0, sizeof(m_stepHit));
    m_found = false;
    m_tooDeep = 0;
    m_end = WALK_COMPLETE;
}

const Node *Resolver::match() const
{
    if (!m_st.valid() || !m_found)
        return 0;
    return &m_st.get()->match;
}

const Node *Resolver::sample(int i) const
{
    if (!m_st.valid() || i < 0 || i >= m_sampleN)
        return 0;
    return &m_st.get()->samples[i];
}

const Enrichment *Resolver::enrichmentFor(const Node &n)
{
    if (!m_msaa)
        return 0;

    ResolveState *st = m_st.get();

    /* Misses are remembered as well as hits. A window with no accessible
     * object is ordinary -- it was well over half of them on both machines
     * -- and re-asking it once per selector step is paying the boundary
     * cost repeatedly for the same "no". */
    for (int i = 0; i < m_cacheN; i++)
        if (st->cacheKey[i] == n.hwnd)
            return st->cacheHit[i] ? &st->cacheVal[i] : 0;

    /* Only the fields this selector actually tests. Each one is a separate
     * marshalled call, so asking for three when one was wanted is three
     * times the boundary cost -- the cost a faster processor was measured
     * not to touch. */
    bool full = (m_cacheN >= RC_ENRICH_CACHE);
    Enrichment &e = full ? st->scratch : st->cacheVal[m_cacheN];
    memset(&e, 0, sizeof(e));
    bool got = m_msaa->enrich(n.hwnd, m_sel.wanted(), e);
    m_enriched++;

    /* A full cache is slower, never wrong: the answer is still fetched, it
     * is just fetched again next time. */
    if (!full)
    {
        st->cacheKey[m_cacheN] = n.hwnd;
        st->cacheHit[m_cacheN] = got;
        m_cacheN++;
    }
    return got ? &e : 0;
}

int Resolver::unmatchedStep() const
{
    for (int i = m_sel.stepCount() - 1; i >= 0; i--)
    {
        if (i < RC_SEL_STEPS && m_stepHit[i] == 0)
            return i;
    }
    return -1;
}

bool Resolver::stepMatches(int si, const Node &n)
{
    /* Cheap pass first. An MSAA attribute answers "unknown, keep it" with no
     * enrichment, so a node reaching the second pass is one worth paying
     * for -- and a node rejected on class, id or state never costs a
     * cross-process call at all. */
    if (!m_sel.matchesStep(si, n, 0))
        return false;
    /* Per NODE, not per selector. A standard control's role came from its
     * class and style during the walk, so `[role=checkbox]` on a real
     * checkbox is already settled and must not drag a 3 ms cross-process
     * call in behind it. */
    if (!m_sel.stepNeedsEnrichment(si, n))
        return noteHit(si);

    const Enrichment *e = enrichmentFor(n);
    if (!e)
    {
        /* No MSAA object, or no source. A node cannot be shown to satisfy an
         * attribute nobody can read, and answering yes would hand back the
         * wrong control -- which is the one failure this whole layer exists
         * to prevent. */
        return false;
    }
    if (!m_sel.matchesStep(si, n, e))
        return false;
    return noteHit(si);
}

/* Records that step `si` matched a node on its own, and returns true so it can
 * stand in for the `return true` it replaces. Counted at the SUCCESSFUL exits
 * only: a node that passes the cheap pass and then fails on an MSAA attribute
 * has not matched, and counting it would report the selector as present when
 * the thing it names is not. */
bool Resolver::noteHit(int si)
{
    if (si >= 0 && si < RC_SEL_STEPS)
        m_stepHit[si]++;
    return true;
}

/* Can steps[0..si] be satisfied by the ancestor chain[0..ai]?
 *
 * Recursive so a descendant combinator can BACKTRACK. Greedy matching --
 * "take the nearest ancestor that fits" -- is wrong: given `A B C`, the
 * nearest ancestor matching B may sit above the only ancestor matching A,
 * and the selector still holds for a B further out. Recursion depth is
 * bounded by RC_SEL_STEPS, which is 4.
 */
bool Resolver::matchUp(int si, int ai)
{
    if (si < 0)
        return true;            /* every step accounted for */
    if (ai < 0)
        return false;           /* steps left, ancestors exhausted */

    /* `directChild` is recorded on the step AFTER the '>', and means that
     * step must be an immediate child of this one -- so there is exactly one
     * ancestor to try and nothing to backtrack over. */
    if (m_sel.step(si + 1).directChild)
    {
        if (!stepMatches(si, m_st.get()->chain[ai]))
            return false;
        return matchUp(si - 1, ai - 1);
    }

    for (int k = ai; k >= 0; k--)
        if (stepMatches(si, m_st.get()->chain[k]) && matchUp(si - 1, k - 1))
            return true;
    return false;
}

bool Resolver::onNode(const Node &n)
{
    ResolveState *st = m_st.get();
    if (!st)
        return false;

    /* The walk is depth-first and never emits a node whose parent it
     * skipped, so a node's ancestors are exactly the most recent nodes at
     * lower depths. Truncating the stack to this node's depth leaves them
     * behind it, with no second traversal and nothing retained between
     * branches. */
    int d = n.depth;
    if (d < 0)
        d = 0;

    /* Past the chain, REFUSE rather than clamp.
     *
     * Clamping wrote every node at depth 11 and deeper into the same slot,
     * so the "ancestor" a descendant selector tested against could be an
     * unrelated node that happened to come past a moment earlier -- a
     * confidently WRONG match, not the miss this was documented as. A node
     * whose ancestry we did not keep is a node we cannot judge. */
    if (d > RC_CHAIN_MAX - 1)
    {
        m_tooDeep++;
        return true;
    }
    st->chain[d] = n;
    m_chainN = d + 1;

    int last = m_sel.stepCount() - 1;
    if (last < 0)
        return true;
    if (!stepMatches(last, n))
        return true;
    if (!matchUp(last - 1, m_chainN - 2))
        return true;

    m_matches++;
    if (m_sampleN < RC_SAMPLE_MAX)
        st->samples[m_sampleN++] = n;

    if (m_nth > 0)
    {
        /* An ordinal was asked for by name. Once it is reached there is
         * nothing left to learn and the rest of the desktop need not be
         * enumerated -- which is what a sink returning false is for. */
        if (m_matches == m_nth)
        {
            st->match = n;
            m_found = true;
            return false;
        }
        return true;
    }

    if (m_matches == 1)
    {
        st->match = n;
        m_found = true;
        return true;
    }

    /* Two matches already means the answer is an error rather than a node.
     * The walk still runs to the end, because the count is what the caller
     * needs to write the `:nth(n)` that disambiguates, and "at least two"
     * does not tell it whether :nth(4) exists. A desktop was 44 nodes idle
     * and 83 busy; finishing it is cheap next to answering wrongly. */
    m_found = false;
    return true;
}

ResolveOutcome Resolver::resolve(const WalkOptions &opt, TreeWalker &walker)
{
    if (!m_st.valid())
        return RESOLVE_NO_MEMORY;

    m_chainN = 0;
    m_matches = 0;
    m_sampleN = 0;
    m_cacheN = 0;
    m_enriched = 0;
    m_found = false;
    m_tooDeep = 0;

    int last = m_sel.stepCount() - 1;
    if (last < 0)
    {
        m_end = WALK_COMPLETE;
        return RESOLVE_NONE;
    }
    m_nth = m_sel.step(last).nth;

    m_end = walker.walk(opt, *this);

    if (m_found)
        return RESOLVE_OK;

    /* Ambiguity first, and even over truncation: more matches cannot unmake
     * the two already found. It is reported only when no ordinal was given
     * -- `:nth(4)` against three matches is not an ambiguity, it is a
     * fourth control that is not there. */
    if (m_nth == 0 && m_matches > 1)
        return RESOLVE_AMBIGUOUS;

    /* Otherwise, if the walk was cut short then "nothing" is not an answer,
     * it is the absence of one. Saying which is the difference between a
     * caller retrying and a caller concluding the control is gone. */
    /* Nodes skipped for depth are the same kind of not-knowing as a walk
     * that ran out of clock: we did not look everywhere, so "not there" is
     * not something we are entitled to say. */
    if (m_end == WALK_DEADLINE || m_end == WALK_MAXNODES || m_tooDeep > 0)
        return RESOLVE_TRUNCATED;
    return RESOLVE_NONE;
}
