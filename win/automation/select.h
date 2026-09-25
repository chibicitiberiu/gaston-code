/* select.h -- addressing a control without an HWND.
 *
 * HWNDs are stable within a session and meaningless across one, so a caller
 * names what it wants and the name is resolved fresh each time.
 *
 * CSS-like rather than XPath. The measured tree is four to six deep, so
 * XPath's axes buy nothing, and the thing writing these is a language model
 * for which CSS is far better represented in anything to do with UI.
 *
 *   Notepad                class name
 *   #1001                  control id (GetDlgCtrlID)
 *   Notepad > Edit         direct child
 *   Notepad Edit           any descendant
 *   [text^="Untitled"]     ^= prefix, $= suffix, *= contains, = exact
 *   [name="Save"]          MSAA name      ) resolved by the caller, which
 *   [role=pushbutton]      MSAA role      ) enriches only candidates
 *   :visible :enabled :focused
 *   :nth(2)                among the matches so far, 1-based
 *
 * A selector matching more than one node is an ERROR, reported with the
 * count. It does not pick the first. The whole point of a tree over a
 * screenshot is that it is exact, and silently clicking one of four buttons
 * called OK throws that away; ambiguity is resolved by the caller adding
 * :nth(n) on purpose.
 */
#ifndef RC_AUTOMATION_SELECT_H
#define RC_AUTOMATION_SELECT_H

#include "automation/tree.h"

#define RC_SEL_STEPS 8
#define RC_SEL_ATTRS 4
#define RC_SEL_VAL   64

enum AttrOp
{
    ATTR_EXACT = 0,   /* =  */
    ATTR_PREFIX,      /* ^= */
    ATTR_SUFFIX,      /* $= */
    ATTR_CONTAINS     /* *= */
};

enum AttrWhich
{
    ATTR_TEXT = 0,
    ATTR_NAME,        /* MSAA */
    ATTR_ROLE,        /* MSAA */
    ATTR_VALUE        /* MSAA */
};

struct AttrTest
{
    AttrWhich which;
    AttrOp    op;
    char      value[RC_SEL_VAL];
};

struct SelStep
{
    char     cls[RC_CLASS_MAX];   /* empty: any class */
    int      ctrlId;              /* -1: any */
    bool     directChild;         /* '>' preceded this step */
    bool     needVisible;
    bool     needEnabled;
    bool     needFocused;
    int      nth;                 /* 0: any, else 1-based */
    AttrTest attrs[RC_SEL_ATTRS];
    int      attrN;
};

/* Fields a selector needs that a plain walk does not produce. The caller
 * fills these in for candidate nodes only -- that is what keeps MSAA off
 * the whole tree. */
struct Enrichment
{
    char name[RC_SEL_VAL];
    char role[RC_SEL_VAL];
    char value[RC_SEL_VAL];
};

class Selector
{
public:
    Selector();

    /* False with a reason if the text will not parse. Never guesses. */
    bool parse(const char *text, char *err, unsigned errCap);

    int  stepCount() const { return m_stepN; }
    const SelStep &step(int i) const { return m_steps[i]; }

    /* Does this node satisfy the LAST step, ignoring ancestry? Cheap, and
     * what a walk uses to decide whether a node is worth enriching. */
    bool matchesLeaf(const Node &n, const Enrichment *e) const;

    /* Does this node's path satisfy the whole chain? `path` is the node's
     * child-index path and `ancestry` the classes along it, supplied by the
     * caller because the walker streams and does not keep parents. */
    bool needsEnrichment() const { return m_needsMsaa; }

private:
    SelStep m_steps[RC_SEL_STEPS];
    int     m_stepN;
    bool    m_needsMsaa;

    Selector(const Selector &);
    Selector &operator=(const Selector &);
};

#endif
