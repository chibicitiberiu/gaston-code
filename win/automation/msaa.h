/* msaa.h -- the enrichment tier, kept off the walk.
 *
 * MSAA is the only source for a checkbox's checked state, an edit's
 * contents where no message is marshalled, and a role that distinguishes a
 * push button from a radio button when the class name says "Button" for
 * both. It is also the expensive tier: measured at roughly the cost of
 * everything else in a walk put together, for a name on well under half the
 * nodes. So it is applied to the candidates a selector is deciding between,
 * never to a tree.
 *
 * Two things this gets right that cost a day to find:
 *
 *   OBJID_CLIENT, not OBJID_WINDOW. Asked for the window object, Win98
 *   reports role 9 -- ROLE_SYSTEM_WINDOW -- for every node on the desktop,
 *   with no value and no action anywhere, which reads exactly like "MSAA
 *   knows nothing on this machine". The client object is the control.
 *
 *   oleacc is LOADED, never imported. A static import of a DLL that is not
 *   there kills the image at load, before WinMain, with nothing written and
 *   nothing to debug. It is present on this Win98 SE; it is not promised on
 *   every one.
 */
#ifndef RC_AUTOMATION_MSAA_H
#define RC_AUTOMATION_MSAA_H

#include "automation/select.h"

/* Forward-declared rather than including <oleacc.h> here: that header drags
 * in the whole of OLE and, without NONAMELESSUNION defined first, Watcom's
 * oaidl.h and oleauto.h disagree with each other about VARIANT. msaa.cpp
 * gets the define and the includes in the right order; nothing else has to
 * care. */
struct IAccessible;
class CallDeadline;

/* One accessible object, for the tier that is not made of windows.
 *
 * A web page is a single HWND. The window walk sees one node and stops, and
 * in-process messages reach nothing but the form controls -- there is no
 * window to send LB_GETTEXT to when the "list" is a <ul>. MSAA is the only
 * interface every Win98 browser shares (Gecko implements it deliberately),
 * and the P200 run already came back with `link | First link |
 * http://example.com/one`: the hrefs and the input values are in there. */
struct AccNode
{
    int  depth;
    /* 0 is the object itself. A non-zero child id is a SIMPLE element --
     * one that has no IAccessible of its own and is addressed through its
     * parent, which is how list items and menu entries usually arrive. */
    long childId;
    /* The NUMBER, not the text. ROLE_SYSTEM_DOCUMENT is 15 on every Win98
     * in every language, where GetRoleTextA returns whatever the system
     * speaks -- and finding the document is the whole reason this exists. */
    long roleId;
    char name[RC_ENRICH_VAL];
    char role[RC_ENRICH_VAL];
    char value[RC_ENRICH_VAL];
    char state[RC_ENRICH_VAL];
    RECT rect;
    bool hasRect;
};

/* ROLE_SYSTEM_DOCUMENT. The one role this library names, because "find the
 * page" must not mean "find a window called Internet Explorer_Server" --
 * that is IE by construction, and Win98 had Netscape 4, Mozilla and Opera
 * as well. */
#define RC_ROLE_DOCUMENT 15L

class AccSink
{
public:
    virtual ~AccSink() {}
    /* Return false to stop the walk. */
    virtual bool onAcc(const AccNode &n) = 0;
};

struct AccWalkOptions
{
    int   want;         /* Selector::WANT_* -- each bit is a COM call */
    int   maxNodes;
    int   maxDepth;
    DWORD deadlineMs;

    AccWalkOptions();
};

enum AccEnd
{
    ACC_COMPLETE = 0,
    ACC_NO_OBJECT,      /* the window has no accessible object at all */
    ACC_MAXNODES,
    ACC_MAXDEPTH,
    ACC_DEADLINE,
    ACC_STOPPED         /* the sink asked it to stop */
};

class MsaaSource
{
public:
    MsaaSource();
    ~MsaaSource();

    /* False when oleacc is absent or refused. A caller that gets false
     * falls back to the window tree and reports msaa=0 at the handshake,
     * rather than pretending the fields are empty. */
    bool available() const { return m_aofw != 0; }

    /* Fill `out` for one window, fetching ONLY the fields in `want`
     * (Selector::WANT_*). Each field is a separate COM call into another
     * process, so asking for three when the selector needs one is three
     * times the work -- measured at a few ms each on the emulator and
     * reported at about a second per control on a 200MHz machine, which is
     * where that multiplier stops being academic.
     *
     * Returns false if this window has no accessible object, which is
     * ordinary and not an error. The caller owns the deadline. */
    bool enrich(HWND h, int want, Enrichment &out);

    /* Walk the ACCESSIBLE tree below a window, which is a different tree
     * from the window tree and the only one a document has.
     *
     * Children come in batches. Per-child AccessibleObjectFromWindow was
     * the 8500 us/node measurement; one IEnumVARIANT::Next for sixteen
     * children is one boundary crossing instead of sixteen, and the
     * boundary is the entire cost -- a machine four times faster measured
     * no improvement at all.
     *
     * Which batcher is faster is NOT decided here. The P200 preferred
     * AccessibleChildren (2078 us against 2555) and the XP preferred the
     * enumerator by seven times (463 against 3306), most likely different
     * oleacc builds. So the enumerator is tried and the helper catches
     * what it cannot do, per level, and both counters are reported. */
    AccEnd enumerateFrom(HWND root, const AccWalkOptions &opt, AccSink &sink);

    /* The deadline that can interrupt a call already in flight. Without one,
     * every method here is bounded only by the goodwill of the process being
     * asked: a marshalled property against a wedged apartment takes no
     * timeout argument and blocks. Optional, because `autocheck` and any
     * caller on a thread it does not own can decline it -- a message filter
     * is per apartment, so installing one is the owner's decision. */
    void setDeadline(CallDeadline *d) { m_deadline = d; }

    /* Calls abandoned because the deadline passed. Zero on a healthy
     * desktop; non-zero is a wedged process and worth reporting upward. */
    int  abandoned() const { return m_abandoned; }

    int  objectsMade() const { return m_made; }
    int  namesFound() const  { return m_named; }
    int  enumeratorLevels() const { return m_byEnum; }
    int  helperLevels() const     { return m_byHelper; }

private:
    HMODULE m_oleacc;
    void   *m_aofw;        /* AccessibleObjectFromWindow */
    void   *m_roleText;    /* GetRoleTextA, optional */
    AccEnd walkAcc(IAccessible *acc, long childId, int depth,
                   const AccWalkOptions &opt, AccSink &sink);
    void   fillAcc(IAccessible *acc, long childId, int depth,
                   const AccWalkOptions &opt, AccNode &out);

    CallDeadline *m_deadline;
    int     m_abandoned;

    void   *m_children;    /* AccessibleChildren */
    bool    m_comReady;
    int     m_made;
    int     m_named;
    int     m_byEnum;
    int     m_byHelper;

    /* Deadline and caps for the walk in progress. Members rather than
     * parameters only because the recursion already carries five. */
    DWORD   m_until;
    bool    m_hasDeadline;
    int     m_accSeen;

    MsaaSource(const MsaaSource &);
    MsaaSource &operator=(const MsaaSource &);
};

#endif
