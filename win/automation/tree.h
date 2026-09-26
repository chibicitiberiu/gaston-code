/* tree.h -- the control tree, streamed.
 *
 * One record per window, handed to a sink as it is found. Nothing is
 * accumulated: a desktop is small (44 nodes measured idle, 83 with several
 * applications up) but the caller decides what to keep, and on a machine
 * with 28 KB free after a turn the walker is not the place to build a list.
 *
 * What this does NOT do:
 *
 *   - it does not enrich with MSAA. That costs about as much as everything
 *     else here put together and names well under half the nodes, so it is
 *     applied to candidates a selector is deciding between, not to a walk.
 *   - it does not walk hidden windows by default. Not an optimisation:
 *     asking oleacc about a hidden window realised the Start menu and left
 *     it painted on screen with nothing owning it. A tool that reports what
 *     is on screen must not change what is on screen.
 *   - it does not create a window or own a message loop, per REMOTE.md.
 *
 * See docs/UIAUTOMATION.md for where this sits in the ladder.
 */
#ifndef RC_AUTOMATION_TREE_H
#define RC_AUTOMATION_TREE_H

#include <windows.h>

/* Long enough for a dialog's worth of nesting and a control's caption;
 * anything past these is truncated, never grown. */
#define RC_PATH_MAX  64
#define RC_CLASS_MAX 64
#define RC_TEXT_MAX  128
#define RC_ROLE_MAX  20

struct Node
{
    HWND  hwnd;            /* this session only, meaningless across one */
    HWND  parent;
    int   depth;
    char  path[RC_PATH_MAX];    /* child-index path, "0/3/1" */
    char  cls[RC_CLASS_MAX];
    char  text[RC_TEXT_MAX];    /* WM_GETTEXT, empty if not asked or timed out */
    /* A role derived from the class name and the style bits, free. For a
     * Button the style IS the role -- BS_CHECKBOX, BS_RADIOBUTTON,
     * BS_GROUPBOX -- and GetWindowLong is a read of memory USER already
     * has, which the walk performs anyway for `visible` and `enabled`.
     *
     * Measured against a dialog on the P200: this classified 46 of 82
     * windows at 731 us/node where MSAA charged 3048 for the same answer.
     * Empty means a class we do not recognise, which is where MSAA earns
     * its cost and nowhere else. */
    char  role[RC_ROLE_MAX];
    RECT  rect;
    DWORD style;
    DWORD pid;
    DWORD tid;
    int   ctrlId;               /* GetDlgCtrlID, stable inside a dialog */
    bool  visible;
    bool  enabled;
    bool  focused;
    bool  textTimedOut;         /* asked, did not answer inside the budget */
};

/* The class-and-style role, exposed because a caller resolving a selector
 * against nodes it already holds needs the same mapping. Returns NULL for a
 * class this does not know, never a guess. */
const char *roleFromClassAndStyle(const char *cls, DWORD style);

/* Return false to stop the walk. A sink that has found what it wanted says
 * so rather than letting the rest of the desktop be enumerated. */
class NodeSink
{
public:
    virtual ~NodeSink() {}
    virtual bool onNode(const Node &n) = 0;
};

struct WalkOptions
{
    HWND  root;             /* NULL: every top-level window */
    DWORD pid;              /* 0: any process */
    DWORD callTimeoutMs;    /* per WM_GETTEXT; SMALL -- see tree.cpp */
    DWORD deadlineMs;       /* the whole walk. 0: no deadline */
    int   maxNodes;         /* cyclic-chain guard, not a budget */
    int   maxDepth;         /* bounds the RECURSION; see tree.cpp */
    /* Windows belonging to this process are skipped. The client walking the
     * desktop passes its own pid: TreeWalker sends WM_GETTEXT, a same-thread
     * SendMessage is a direct call, and reaching our own in-place-active
     * HTML control from inside an RPC handler is reentrancy we can simply
     * decline. */
    DWORD excludePid;
    bool  visibleOnly;
    bool  wantText;

    WalkOptions();
};

/* Why a walk ended. A truncated tree says so rather than ending silently. */
enum WalkEnd
{
    WALK_COMPLETE = 0,
    WALK_DEADLINE,
    WALK_MAXNODES,
    WALK_STOPPED        /* the sink asked it to stop */
};

/* Carries a walk's state through EnumWindows' LPARAM. It is in the header
 * only because stepTop below has to name it; nothing outside tree.cpp
 * builds one. REMOTE.md forbids per-module globals in the libs, and two
 * walks at once would share them. */
struct TopContext;

class TreeWalker
{
public:
    TreeWalker();

    WalkEnd walk(const WalkOptions &opt, NodeSink &sink);

    int nodesSeen() const   { return m_seen; }
    int textTimeouts() const { return m_timeouts; }
    int deferredNodes() const { return m_deferred; }
    /* Subtrees refused for depth. Non-zero means the tree is INCOMPLETE. */
    int tooDeep() const { return m_tooDeep; }
    /* Windows left out because they are excludePid's. The caller has to be
     * able to SAY so: a tree that silently omits the one window the agent
     * was asked about reads as "it is not open", and the agent then hunts
     * for it forever. Measured on the VM 2026-09-27 -- a real agent looped
     * tree, shot, tree until a human stopped it. */
    int selfSkipped() const { return m_selfSkipped; }

    /* Public only so EnumWindows' free-function callback can reach it. */
    bool stepTop(HWND h, TopContext &ctx);

private:
    /* A window that will not answer is a property of its THREAD, not of
     * itself, so one wedged application costs one timeout and not one per
     * control. Three tiers: the stalled thread last, other threads of a
     * process that has already stalled once next-to-last, everything else
     * normally. On 9x the middle tier is real: a 16-bit app holding
     * Win16Mutex blocks USER and GDI for every thread in the system. */
    enum { MAX_SLOW = 16 };
    DWORD m_slowTid[MAX_SLOW];
    DWORD m_slowPid[MAX_SLOW];
    int   m_slowN;

    int   m_seen;
    int   m_timeouts;
    int   m_deferred;
    int   m_tooDeep;
    int   m_selfSkipped;
    HWND  m_focus;          /* resolved once per walk, see tree.cpp */
    DWORD m_until;
    bool  m_hasDeadline;

    static bool inProcess(HWND h, DWORD pid);
    bool  isSlowThread(DWORD tid) const;
    bool  isSlowProcess(DWORD pid) const;
    void  markSlow(DWORD tid, DWORD pid);
    bool  outOfTime() const;

    void  fill(Node &n, HWND h, const WalkOptions &opt,
               const char *parentPath, int index, int depth);
    WalkEnd descend(HWND parent, const WalkOptions &opt, NodeSink &sink,
                    const char *parentPath, int depth);

    TreeWalker(const TreeWalker &);
    TreeWalker &operator=(const TreeWalker &);
};

#endif
