/* resolve.h -- turn a selector into one control, or refuse.
 *
 * `Selector` parses and `Selector::matchesStep` tests one node against one
 * step. Nothing joined the two to a walk, so `>` versus descendant, `:nth`
 * and the ambiguity rule were all parsed and then ignored. This is that
 * join, and it is what makes the library callable from outside.
 *
 * Three rules it exists to enforce:
 *
 *   Ancestry is real. `Notepad > Edit` must mean a direct child and
 *   `Notepad Edit` any descendant. The walk is depth-first, so a node's
 *   ancestors are the most recent nodes at lower depths -- a stack indexed
 *   by `Node::depth` reconstructs the chain with no extra traversal.
 *
 *   More than one match is an ERROR. It does not take the first. The whole
 *   point of a tree over a screenshot is exactness, and quietly clicking one
 *   of four buttons called OK throws that away. The caller disambiguates
 *   with `:nth(n)` deliberately.
 *
 *   MSAA is paid for on candidates only. `matchesStep` answers *true* for an
 *   MSAA attribute it has no enrichment for -- "unknown, keep it" rather
 *   than "no" -- so a node survives the cheap pass and is enriched only if
 *   it got that far. Enrichment is cached per HWND, hits and misses alike,
 *   for the length of one resolve.
 */
#ifndef RC_AUTOMATION_RESOLVE_H
#define RC_AUTOMATION_RESOLVE_H

#include "automation/select.h"
#include "automation/tree.h"
#include "core/rc_cpp.h"

class MsaaSource;

enum ResolveOutcome
{
    RESOLVE_OK = 0,
    RESOLVE_NONE,        /* nothing matched */
    RESOLVE_AMBIGUOUS,   /* more than one, and no :nth to pick with */
    RESOLVE_TRUNCATED,   /* the walk ended early; the answer may be wrong */
    RESOLVE_NO_MEMORY
};

/* How deep an ancestor chain is tracked. The measured tree is 4 to 6 deep;
 * past this the chain is clamped and a selector needing a deeper ancestor
 * will not match, which is a miss rather than a wrong answer. */
#define RC_CHAIN_MAX 12

/* Matches kept for the ambiguity message. Enough to tell the caller what it
 * collided with without holding the desktop. */
#define RC_SAMPLE_MAX 3

/* Distinct windows whose enrichment is remembered for one resolve. A node is
 * tested once per selector step, so without this a three-step selector pays
 * three times for the same window -- and one MSAA property was measured at
 * about 3 ms on both machines, the boundary cost that a faster CPU does not
 * touch. Past this the resolve still answers correctly, just slower. */
#define RC_ENRICH_CACHE 16

/* Everything of size, in one heap block.
 *
 * A Node is 324 bytes: the chain alone is 3888 and the whole of this is near
 * 7 KB, where a stack FRAME may use 4096 before it steps over Win98's guard
 * page and faults -- the crash that took down AUTOCHK on the P200. Holding
 * it here is what lets a `Resolver` itself be an ordinary local.
 *
 * Every member is POD, so one HeapAlloc and one memset construct it. That is
 * also the Watcom trap avoided: C++98 default-initialises a non-POD to heap
 * garbage, `()` or not. */
struct ResolveState
{
    Node       chain[RC_CHAIN_MAX];
    Node       match;
    Node       samples[RC_SAMPLE_MAX];
    HWND       cacheKey[RC_ENRICH_CACHE];
    Enrichment cacheVal[RC_ENRICH_CACHE];
    bool       cacheHit[RC_ENRICH_CACHE];   /* false: asked, has no object */
    Enrichment scratch;                     /* used once the cache is full */
};

class Resolver : public NodeSink
{
public:
    /* `msaa` may be NULL. A selector that asks about a name or a value then
     * cannot be answered, and the nodes it would have tested are refused
     * rather than guessed at -- pass one whenever `sel.wanted()` is
     * non-zero. `[role=...]` is the exception: the walk derives a role from
     * class and style for free, and only an unrecognised class falls
     * through to MSAA. */
    Resolver(const Selector &sel, MsaaSource *msaa);

    ResolveOutcome resolve(const WalkOptions &opt, TreeWalker &walker);

    /* NULL unless the last resolve returned RESOLVE_OK. A pointer and not a
     * reference because there is genuinely nothing to return when the
     * allocation failed or nothing matched, and a reference would have to
     * invent a node to say so. */
    const Node *match() const;

    /* Exact, not a lower bound: on ambiguity the walk runs to the end so the
     * caller can tell whether the `:nth(n)` it is about to write exists. */
    int matchCount() const      { return m_matches; }

    int sampleCount() const     { return m_sampleN; }
    const Node *sample(int i) const;

    /* Windows an MSAA property was actually fetched for. The number this
     * library exists to keep small; `autocheck` asserts on it. */
    int enriched() const        { return m_enriched; }

    /* Why the walk stopped, for the difference between "not there" and "we
     * ran out of time before we could tell". */
    WalkEnd walkEnd() const     { return m_end; }

private:
    virtual bool onNode(const Node &n);

    bool stepMatches(int si, const Node &n);
    bool matchUp(int si, int ai);
    const Enrichment *enrichmentFor(const Node &n);

    const Selector &m_sel;
    MsaaSource     *m_msaa;

    HeapPtr<ResolveState> m_st;

    int     m_chainN;
    int     m_matches;
    int     m_sampleN;
    int     m_cacheN;
    int     m_enriched;
    int     m_nth;          /* :nth on the last step, 0 for none */
    bool    m_found;        /* m_st->match holds the answer */
    WalkEnd m_end;

    Resolver(const Resolver &);
    Resolver &operator=(const Resolver &);
};

#endif
