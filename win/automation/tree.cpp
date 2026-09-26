#include "automation/tree.h"
#include "core/str.h"

#include <string.h>

WalkOptions::WalkOptions()
{
    root = NULL;
    pid = 0;
    /* 200ms, and the number is measured rather than picked. The same
     * 45-node walk took 72ms at a 200ms budget and 485ms at 2000ms WITH
     * ZERO TIMEOUTS EITHER WAY: on 9x the cost of SendMessageTimeout tracks
     * the budget handed to it, not how fast the target answers. So pass the
     * smallest budget that works, and escalate only for a thread that has
     * actually failed. */
    callTimeoutMs = 200;
    deadlineMs = 5000;
    /* A guard against a cyclic GW_HWNDNEXT chain, not a budget. An idle
     * desktop measured 44 nodes; the deadline is what bounds a walk. */
    maxNodes = 4096;
    /* The measured tree is 4 to 6 deep. This bounds the RECURSION, which
     * maxNodes does not: descend() puts a 324-byte Node on the stack at
     * every level, and the frame gate caps one frame, not a thousand of
     * them. Deeper than this is counted and reported, never followed. */
    maxDepth = 24;
    excludePid = 0;
    visibleOnly = true;
    wantText = true;
}

/* Names match MSAA's, lowercased and unspaced, so a selector written
 * `[role=pushbutton]` matches whether the role came from here or from
 * oleacc's GetRoleTextA. The two must not drift: msaa.cpp normalises "push
 * button" to exactly this spelling. */
const char *roleFromClassAndStyle(const char *cls, DWORD style)
{
    if (!cls || !cls[0])
        return 0;

    if (!lstrcmpiA(cls, "Button"))
    {
        /* BS_TYPEMASK is the low nibble. A Button's style is the only place
         * the difference between a push button, a checkbox and a radio
         * button lives -- the class name says "Button" for all three. */
        switch (style & 0x0FL)
        {
        case BS_PUSHBUTTON:
        case BS_DEFPUSHBUTTON:      return "pushbutton";
        case BS_CHECKBOX:
        case BS_AUTOCHECKBOX:
        case BS_3STATE:
        case BS_AUTO3STATE:         return "checkbox";
        case BS_RADIOBUTTON:
        case BS_AUTORADIOBUTTON:    return "radiobutton";
        case BS_GROUPBOX:           return "grouping";
        /* BS_OWNERDRAW, BS_USERBUTTON, BS_PUSHBOX and the unused values
         * fall through to nothing on purpose. An owner-draw button is drawn
         * by its application and could be anything at all -- it is exactly
         * the residue the MSAA tier exists for, and calling it a push
         * button here is a confident wrong answer that stops MSAA ever
         * being asked. */
        default:                    return 0;
        }
    }

    if (!lstrcmpiA(cls, "Edit"))                return "text";
    if (!lstrcmpiA(cls, "Static"))              return "statictext";
    if (!lstrcmpiA(cls, "ListBox"))             return "list";
    if (!lstrcmpiA(cls, "ComboBox"))            return "combobox";
    if (!lstrcmpiA(cls, "ScrollBar"))           return "scrollbar";
    if (!lstrcmpiA(cls, "SysListView32"))       return "list";
    if (!lstrcmpiA(cls, "SysTreeView32"))       return "outline";
    if (!lstrcmpiA(cls, "SysTabControl32"))     return "pagetablist";
    if (!lstrcmpiA(cls, "ToolbarWindow32"))     return "toolbar";
    if (!lstrcmpiA(cls, "msctls_statusbar32"))  return "statusbar";
    if (!lstrcmpiA(cls, "msctls_progress32"))   return "progressbar";
    if (!lstrcmpiA(cls, "msctls_trackbar32"))   return "slider";
    if (!lstrcmpiA(cls, "msctls_updown32"))     return "spinbutton";
    if (!lstrcmpiA(cls, "SysHeader32"))         return "list";
    if (!lstrcmpiA(cls, "#32770"))              return "dialog";

    /* Unknown. NOT a guess: an empty role is what tells a caller this is
     * the residue MSAA is for. */
    return 0;
}

TreeWalker::TreeWalker()
{
    m_slowN = 0;
    m_seen = 0;
    m_timeouts = 0;
    m_deferred = 0;
    m_tooDeep = 0;
    m_until = 0;
    m_hasDeadline = false;
    m_focus = NULL;
    memset(m_slowTid, 0, sizeof(m_slowTid));
    memset(m_slowPid, 0, sizeof(m_slowPid));
}

bool TreeWalker::isSlowThread(DWORD tid) const
{
    for (int i = 0; i < m_slowN; i++)
        if (m_slowTid[i] == tid)
            return true;
    return false;
}

bool TreeWalker::isSlowProcess(DWORD pid) const
{
    for (int i = 0; i < m_slowN; i++)
        if (m_slowPid[i] == pid)
            return true;
    return false;
}

void TreeWalker::markSlow(DWORD tid, DWORD pid)
{
    if (isSlowThread(tid) || m_slowN >= MAX_SLOW)
        return;
    m_slowTid[m_slowN] = tid;
    m_slowPid[m_slowN] = pid;
    m_slowN++;
}

/* The focused control, once per walk, and NOT GetFocus() per node.
 *
 * GetFocus reports the focus of the CALLING THREAD's input queue. For every
 * window outside our own thread it returns NULL, so `:focused` -- parsed,
 * documented and in the node record -- could never once be true on a real
 * desktop. The bug was invisible in testing because the only coverage was a
 * synthetic node asserting a negative.
 *
 * On 9x the way to read another thread's focus is to join its input queue.
 * GetGUIThreadInfo would do it without that and is NT 5.0+, so it is out.
 * The attach is done ONCE for the foreground thread rather than per node,
 * and undone immediately: it is documented as unreliable on 9x (the import
 * audit warns about it by name), and briefly sharing an input queue with
 * the foreground application is not something to do eighty times a walk.
 *
 * If the attach is refused, the honest answer is the foreground window
 * itself and nothing inside it -- a top-level answer rather than a wrong
 * one. */
static HWND focusedWindow()
{
    HWND fg = GetForegroundWindow();
    if (!fg)
        return NULL;

    DWORD me = GetCurrentThreadId();
    DWORD other = GetWindowThreadProcessId(fg, NULL);
    if (other == me)
        return GetFocus();

    if (!AttachThreadInput(me, other, TRUE))
        return fg;
    HWND f = GetFocus();
    AttachThreadInput(me, other, FALSE);
    return f ? f : fg;
}

bool TreeWalker::inProcess(HWND h, DWORD pid)
{
    DWORD owner = 0;
    GetWindowThreadProcessId(h, &owner);
    return owner == pid;
}

bool TreeWalker::outOfTime() const
{
    if (!m_hasDeadline)
        return false;
    return (long)(GetTickCount() - m_until) >= 0;
}

void TreeWalker::fill(Node &n, HWND h, const WalkOptions &opt,
                      const char *parentPath, int index, int depth)
{
    memset(&n, 0, sizeof(n));
    n.hwnd = h;
    n.parent = GetParent(h);
    n.depth = depth;
    n.tid = GetWindowThreadProcessId(h, &n.pid);
    n.ctrlId = GetDlgCtrlID(h);
    n.style = (DWORD)GetWindowLongA(h, GWL_STYLE);
    n.visible = IsWindowVisible(h) ? true : false;
    n.enabled = IsWindowEnabled(h) ? true : false;
    n.focused = (m_focus != NULL && m_focus == h);
    GetWindowRect(h, &n.rect);
    GetClassNameA(h, n.cls, sizeof(n.cls));

    /* Free: both inputs are already read above. */
    {
        const char *r = roleFromClassAndStyle(n.cls, n.style);
        if (r)
            rc_strncpy(n.role, sizeof(n.role), r, (unsigned)strlen(r));
    }

    /* "0/3/1" -- survives a repaint, which an HWND does too but a caption
     * does not. rc_strf rather than sprintf: the unbounded family fails the
     * build (win/Makefile check-sources). */
    if (parentPath && parentPath[0])
        rc_strf(n.path, sizeof(n.path), "%s/%d", parentPath, index);
    else
        rc_strf(n.path, sizeof(n.path), "%d", index);

    if (!opt.wantText)
        return;

    /* A thread that has already refused to answer is not asked again at the
     * full budget; it is asked at a token one so the record still carries
     * whatever comes back cheaply. Everything else is asked normally. */
    DWORD budget = opt.callTimeoutMs;
    if (isSlowThread(n.tid))
    {
        budget = 1;
        m_deferred++;
    }
    else if (isSlowProcess(n.pid))
    {
        budget = opt.callTimeoutMs / 4 ? opt.callTimeoutMs / 4 : 1;
        m_deferred++;
    }

    DWORD_PTR res = 0;
    /* SMTO_ABORTIFHUNG so USER can refuse immediately for a thread it has
     * already flagged. WM_GETTEXT and not GetWindowText: across a process
     * boundary the latter returns only a caption, while this is marshalled
     * by USER32 and returns a control's contents. */
    LRESULT ok = SendMessageTimeoutA(h, WM_GETTEXT, sizeof(n.text),
                                     (LPARAM)n.text, SMTO_ABORTIFHUNG,
                                     budget, &res);
    if (!ok)
    {
        n.text[0] = '\0';
        n.textTimedOut = true;
        m_timeouts++;
        markSlow(n.tid, n.pid);
    }
}

WalkEnd TreeWalker::descend(HWND parent, const WalkOptions &opt,
                            NodeSink &sink, const char *parentPath,
                            int depth)
{
    int index = 0;
    for (HWND c = GetWindow(parent, GW_CHILD); c;
         c = GetWindow(c, GW_HWNDNEXT))
    {
        if (outOfTime())
            return WALK_DEADLINE;
        if (m_seen >= opt.maxNodes)
            return WALK_MAXNODES;
        if (opt.visibleOnly && !IsWindowVisible(c))
            continue;
        /* Before fill, not after. fill() sends WM_GETTEXT, and sending it to
         * a window we are about to discard costs a cross-process round trip
         * per foreign window -- and a full timeout for every one of them
         * that happens to be wedged. */
        if (opt.pid && !inProcess(c, opt.pid))
            continue;
        if (opt.excludePid && inProcess(c, opt.excludePid))
            continue;

        Node n;
        fill(n, c, opt, parentPath, index, depth);
        index++;

        m_seen++;
        if (!sink.onNode(n))
            return WALK_STOPPED;

        if (depth + 1 > opt.maxDepth)
        {
            m_tooDeep++;
            continue;
        }
        WalkEnd e = descend(c, opt, sink, n.path, depth + 1);
        if (e != WALK_COMPLETE)
            return e;
    }
    return WALK_COMPLETE;
}

/* EnumWindows needs a plain callback, so the walk's state travels as the
 * LPARAM rather than through a file static -- REMOTE.md forbids per-module
 * globals in the libs, and two walks at once would share them. */
struct TopContext
{
    TreeWalker        *walker;
    const WalkOptions *opt;
    NodeSink          *sink;
    int                index;
    WalkEnd            end;
};

static BOOL CALLBACK onTopLevel(HWND h, LPARAM lp)
{
    TopContext *ctx = (TopContext *)lp;
    return ctx->walker->stepTop(h, *ctx) ? TRUE : FALSE;
}

bool TreeWalker::stepTop(HWND h, TopContext &ctx)
{
    if (outOfTime())
    {
        ctx.end = WALK_DEADLINE;
        return false;
    }
    if (m_seen >= ctx.opt->maxNodes)
    {
        ctx.end = WALK_MAXNODES;
        return false;
    }
    if (ctx.opt->visibleOnly && !IsWindowVisible(h))
        return true;
    if (ctx.opt->pid && !inProcess(h, ctx.opt->pid))
        return true;
    if (ctx.opt->excludePid && inProcess(h, ctx.opt->excludePid))
        return true;

    Node n;
    fill(n, h, *ctx.opt, "", ctx.index, 0);
    ctx.index++;

    m_seen++;
    if (!ctx.sink->onNode(n))
    {
        ctx.end = WALK_STOPPED;
        return false;
    }

    WalkEnd e = descend(h, *ctx.opt, *ctx.sink, n.path, 1);
    if (e != WALK_COMPLETE)
    {
        ctx.end = e;
        return false;
    }
    return true;
}

WalkEnd TreeWalker::walk(const WalkOptions &opt, NodeSink &sink)
{
    m_seen = 0;
    m_timeouts = 0;
    m_deferred = 0;
    m_tooDeep = 0;
    m_slowN = 0;
    m_hasDeadline = opt.deadlineMs != 0;
    m_until = GetTickCount() + opt.deadlineMs;
    m_focus = focusedWindow();

    if (opt.root)
    {
        Node n;
        fill(n, opt.root, opt, "", 0, 0);
        m_seen++;
        if (!sink.onNode(n))
            return WALK_STOPPED;
        return descend(opt.root, opt, sink, n.path, 1);
    }

    TopContext ctx;
    ctx.walker = this;
    ctx.opt = &opt;
    ctx.sink = &sink;
    ctx.index = 0;
    ctx.end = WALK_COMPLETE;
    EnumWindows(onTopLevel, (LPARAM)&ctx);
    return ctx.end;
}
