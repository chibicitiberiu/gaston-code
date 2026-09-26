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
 *   [action=press]         MSAA default action, lowercased and unspaced
 *   [state*="checked"]     MSAA state, a comma-joined list -- see msaa.cpp
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

/* Sized so a Selector fits comfortably in a stack frame. At 8 steps, 4
 * attributes and 64-byte values the object was ~3KB, and two of them in one
 * function is most of the 4096 bytes a frame may use before it steps over
 * Win98's guard page and faults. Four steps is more than the measured tree
 * is deep (4 to 6, and a selector rarely names every level); three
 * attributes per step and 48-byte values have never been close to binding. */
#define RC_SEL_STEPS 4
#define RC_SEL_ATTRS 3
#define RC_SEL_VAL   48

/* An enrichment field is wider than a selector VALUE because `state` is a
 * list rather than a word: "unavailable,checked,focusable" is already 30
 * characters and a selector reaches into it with [state*="checked"], which
 * fits in RC_SEL_VAL several times over. This does not enlarge SelStep, and
 * SelStep is what the guard-page budget is spent on. */
#define RC_ENRICH_VAL 64

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
    ATTR_VALUE,       /* MSAA */
    ATTR_ACTION,      /* MSAA */
    ATTR_STATE        /* MSAA */
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
    char name[RC_ENRICH_VAL];
    char role[RC_ENRICH_VAL];
    char value[RC_ENRICH_VAL];
    /* What activating this control would do -- "press", "check", "open".
     * The only machine-readable answer to "is this thing clickable", and
     * the field REMOTE.md's tree record has always promised. */
    char action[RC_ENRICH_VAL];
    /* Comma-joined, most useful first: "checked,focusable". The only source
     * for a checkbox's tick, which no message reaches across a process
     * boundary on 9x. */
    char state[RC_ENRICH_VAL];
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

    /* The same test against any step, which `Resolver` needs to walk an
     * ancestor chain. `matchesLeaf` is this with i = stepCount() - 1. */
    bool matchesStep(int i, const Node &n, const Enrichment *e) const;

    /* Does this selector name any MSAA attribute at all? What a caller asks
     * before building an `MsaaSource`. */
    bool needsEnrichment() const { return m_needsMsaa != 0; }

    /* Does testing step `i` against THIS node need MSAA? Not the same
     * question, and the difference is the whole point of `Node::role`: a
     * standard control carries a role derived from its class and style, so
     * `[role=checkbox]` on a real checkbox is answered for free and must not
     * drag a cross-process call in behind it. Only a class the walk did not
     * recognise falls through. */
    bool stepNeedsEnrichment(int i, const Node &n) const;

    /* WHICH MSAA fields, not merely whether. Each one is a separate COM
     * call into another process; fetching all three when the selector asks
     * about one is three times the work, and on a 200MHz machine that is
     * the difference between usable and not. */
    enum { WANT_NAME = 1, WANT_ROLE = 2, WANT_VALUE = 4,
           WANT_ACTION = 8, WANT_STATE = 16 };
    int wanted() const { return m_needsMsaa; }

private:
    SelStep m_steps[RC_SEL_STEPS];
    int     m_stepN;
    int     m_needsMsaa;   /* WANT_* bits */

    Selector(const Selector &);
    Selector &operator=(const Selector &);
};

#endif
