#include "automation/watch.h"

#include <string.h>

/* The event numbers, spelled out for the same reason as the typedefs. */
#define RC_EV_SYS_MIN            0x0001L
#define RC_EV_SYS_FOREGROUND     0x0003L
#define RC_EV_SYS_MENUSTART      0x0004L
#define RC_EV_SYS_MENUEND        0x0005L
#define RC_EV_SYS_MENUPOPUPSTART 0x0006L
#define RC_EV_SYS_MENUPOPUPEND   0x0007L
#define RC_EV_SYS_CAPTURESTART   0x0008L
#define RC_EV_SYS_CAPTUREEND     0x0009L
#define RC_EV_SYS_MOVESIZESTART  0x000AL
#define RC_EV_SYS_MOVESIZEEND    0x000BL
#define RC_EV_SYS_DIALOGSTART    0x0010L
#define RC_EV_SYS_DIALOGEND      0x0011L
#define RC_EV_SYS_MAX            0x0020L

#define RC_EV_OBJ_CREATE         0x8000L
#define RC_EV_OBJ_DESTROY        0x8001L
#define RC_EV_OBJ_SHOW           0x8002L
#define RC_EV_OBJ_HIDE           0x8003L
#define RC_EV_OBJ_REORDER        0x8004L
#define RC_EV_OBJ_FOCUS          0x8005L
#define RC_EV_OBJ_SELECTION      0x8006L
#define RC_EV_OBJ_SELECTIONADD   0x8007L
#define RC_EV_OBJ_SELECTIONREM   0x8008L
#define RC_EV_OBJ_SELECTIONWITHIN 0x8009L
#define RC_EV_OBJ_STATECHANGE    0x800AL
#define RC_EV_OBJ_LOCATIONCHANGE 0x800BL
#define RC_EV_OBJ_NAMECHANGE     0x800CL
#define RC_EV_OBJ_DESCCHANGE     0x800DL
#define RC_EV_OBJ_VALUECHANGE    0x800EL
#define RC_EV_OBJ_MAX            0x800FL

#define RC_WINEVENT_OUTOFCONTEXT 0x0000
#define RC_WINEVENT_SKIPOWNPROCESS 0x0002

typedef RC_HWINEVENTHOOK (WINAPI *PFN_SETWINEVENT)(DWORD, DWORD, HMODULE,
                                                   RC_WINEVENTPROC, DWORD,
                                                   DWORD, DWORD);
typedef BOOL (WINAPI *PFN_UNHOOKWINEVENT)(RC_HWINEVENTHOOK);

/* SetWinEventHook's callback takes no context pointer.
 *
 * EnumWindows has an LPARAM and tree.cpp threads its state through it;
 * WINEVENTPROC has nothing of the kind, and the hook handle is the only
 * thing the system hands back that could identify the subscriber. So this
 * table, which is the smallest thing that turns one into the other.
 *
 * It is not the shared mutable state REMOTE.md rules out. Every slot
 * belongs to one UiWatch, and it is written and read only from the thread
 * that owns that watch: WINEVENT_OUTOFCONTEXT posts to the INSTALLING
 * thread's message queue, so a callback cannot run anywhere else. Two
 * watches on two threads get two slots and never touch each other's.
 */
enum { RC_WATCH_SLOTS = 8 };

struct WatchSlot
{
    RC_HWINEVENTHOOK hook;
    UiWatch         *owner;
};

static WatchSlot g_slots[RC_WATCH_SLOTS];

/* False when the table is full, and the caller then UNHOOKS rather than
 * carrying on. A hook with no slot still fires, finds no owner, and drops
 * every event it was installed for -- silently, which is the failure this
 * whole file is trying not to have. */
static bool slotAdd(RC_HWINEVENTHOOK h, UiWatch *w)
{
    if (!h)
        return false;
    for (int i = 0; i < RC_WATCH_SLOTS; i++)
        if (!g_slots[i].hook)
        {
            g_slots[i].hook = h;
            g_slots[i].owner = w;
            return true;
        }
    return false;
}

static void slotDropAll(UiWatch *w)
{
    for (int i = 0; i < RC_WATCH_SLOTS; i++)
        if (g_slots[i].owner == w)
        {
            g_slots[i].hook = 0;
            g_slots[i].owner = 0;
        }
}

static UiWatch *slotFind(RC_HWINEVENTHOOK h)
{
    for (int i = 0; i < RC_WATCH_SLOTS; i++)
        if (g_slots[i].hook == h)
            return g_slots[i].owner;
    return 0;
}

static void CALLBACK watchProc(RC_HWINEVENTHOOK hook, DWORD ev, HWND h,
                               LONG obj, LONG child, DWORD tid, DWORD ms)
{
    (void)ms;
    UiWatch *w = slotFind(hook);
    if (w)
        w->onEvent(ev, h, obj, child, tid);
}

UiWatch::UiWatch()
{
    /* USER32 is already loaded and always will be, so GetModuleHandle and
     * not LoadLibrary -- there is no reference to give back. */
    HMODULE u32 = GetModuleHandleA("user32.dll");
    m_set = u32 ? (void *)GetProcAddress(u32, "SetWinEventHook") : 0;
    m_unset = u32 ? (void *)GetProcAddress(u32, "UnhookWinEvent") : 0;
    if (!m_unset)
        m_set = 0;      /* a hook that cannot be removed is not usable */
    m_sys = 0;
    m_obj = 0;
    memset(m_ring, 0, sizeof(m_ring));
    m_head = 0;
    m_count = 0;
    m_events = 0;
    m_dropped = 0;
    m_structSeq = 0;
    m_contentSeq = 0;
    m_focusSeq = 0;
    m_anySeq = 0;
    m_skipOwn = false;
    m_selfPid = GetCurrentProcessId();
    m_ownDropped = 0;
    m_unattributed = 0;
}

UiWatch::~UiWatch()
{
    stop();
}

UiChange UiWatch::classify(DWORD ev)
{
    switch ((long)ev)
    {
    case RC_EV_OBJ_CREATE:
    case RC_EV_OBJ_DESTROY:
    case RC_EV_OBJ_SHOW:
    case RC_EV_OBJ_HIDE:
    case RC_EV_OBJ_REORDER:
    case RC_EV_SYS_MENUSTART:
    case RC_EV_SYS_MENUEND:
    case RC_EV_SYS_MENUPOPUPSTART:
    case RC_EV_SYS_MENUPOPUPEND:
    case RC_EV_SYS_DIALOGSTART:
    case RC_EV_SYS_DIALOGEND:
        return UI_STRUCTURE;

    case RC_EV_OBJ_NAMECHANGE:
    case RC_EV_OBJ_DESCCHANGE:
    case RC_EV_OBJ_VALUECHANGE:
    case RC_EV_OBJ_STATECHANGE:
    case RC_EV_OBJ_SELECTION:
    case RC_EV_OBJ_SELECTIONADD:
    case RC_EV_OBJ_SELECTIONREM:
    case RC_EV_OBJ_SELECTIONWITHIN:
        return UI_CONTENT;

    case RC_EV_OBJ_FOCUS:
    case RC_EV_SYS_FOREGROUND:
        return UI_FOCUS;

    /* Its own bucket, and deliberately not "structure". A window being
     * dragged emits this continuously, and a wait that rescanned the tree
     * on each one would poll harder than a poll. */
    case RC_EV_OBJ_LOCATIONCHANGE:
    case RC_EV_SYS_MOVESIZESTART:
    case RC_EV_SYS_MOVESIZEEND:
        return UI_LOCATION;

    default:
        return UI_OTHER;
    }
}

bool UiWatch::start(DWORD pid, DWORD tid, bool skipOwnProcess)
{
    if (!m_set)
        return false;
    if (running())
        return true;

    PFN_SETWINEVENT set = (PFN_SETWINEVENT)m_set;
    /* Our own repaints are events too. Left in, the client watching the
     * desktop wakes on its own window, rescans, repaints, and wakes itself
     * again -- forIdle never settles and forControl's "nothing was
     * delivered, so nothing changed" fast path is defeated by the very scan
     * it was meant to avoid. */
    DWORD flags = RC_WINEVENT_OUTOFCONTEXT;
    if (skipOwnProcess)
        flags |= RC_WINEVENT_SKIPOWNPROCESS;
    /* The flag AND a check in the callback. Wine accepts the flag and
     * delivers our own events anyway, and a filter that works on three
     * machines out of four is not a filter -- the failure it lets through
     * is an event loop feeding itself. The check costs one
     * GetWindowThreadProcessId per event, which is local. */
    m_skipOwn = skipOwnProcess;
    m_selfPid = GetCurrentProcessId();

    /* Two hooks and not one over 0x0001..0x800F. The gap between the system
     * range and the object range is a thousand event numbers we would be
     * asked about for nothing, and the system filters more cheaply than we
     * can. */
    m_sys = set(RC_EV_SYS_MIN, RC_EV_SYS_MAX, NULL, watchProc, pid, tid,
                flags);
    if (!slotAdd(m_sys, this))
    {
        PFN_UNHOOKWINEVENT unset = (PFN_UNHOOKWINEVENT)m_unset;
        if (m_sys) unset(m_sys);
        m_sys = 0;
    }
    m_obj = set(RC_EV_OBJ_CREATE, RC_EV_OBJ_MAX, NULL, watchProc, pid, tid,
                flags);
    if (!slotAdd(m_obj, this))
    {
        PFN_UNHOOKWINEVENT unset = (PFN_UNHOOKWINEVENT)m_unset;
        if (m_obj) unset(m_obj);
        m_obj = 0;
    }

    /* BOTH or neither. One hook installed is worse than none: running()
     * would be true and a whole class of events -- every name and value
     * change, say -- would be silently absent, so a wait would hang rather
     * than fail. */
    if (!m_sys || !m_obj)
    {
        stop();
        return false;
    }
    return true;
}

void UiWatch::stop()
{
    if (m_unset)
    {
        PFN_UNHOOKWINEVENT unset = (PFN_UNHOOKWINEVENT)m_unset;
        if (m_sys) unset(m_sys);
        if (m_obj) unset(m_obj);
    }
    m_sys = 0;
    m_obj = 0;
    slotDropAll(this);
}

void UiWatch::onEvent(DWORD ev, HWND h, long obj, long child, DWORD tid)
{
    if (m_skipOwn && h)
    {
        DWORD owner = 0;
        GetWindowThreadProcessId(h, &owner);
        /* owner == 0 means the window is already gone -- a DESTROY that
         * arrived after the fact. It cannot be attributed to anyone, so it
         * is let through: an unattributable event costs one extra rescan,
         * where dropping a real destruction costs a wait that hangs. */
        if (owner == m_selfPid)
        {
            m_ownDropped++;
            return;     /* not counted, not sequenced, not rung */
        }
        if (owner == 0)
            m_unattributed++;
    }

    UiChange kind = classify(ev);

    m_events++;
    m_anySeq++;
    switch (kind)
    {
    case UI_STRUCTURE: m_structSeq++;  break;
    case UI_CONTENT:   m_contentSeq++; break;
    case UI_FOCUS:     m_focusSeq++;   break;
    default: break;
    }

    UiEvent &e = m_ring[m_head];
    e.raw = ev;
    e.kind = kind;
    e.hwnd = h;
    e.idObject = obj;
    e.idChild = child;
    e.tid = tid;
    e.at = GetTickCount();

    m_head = (m_head + 1) % RC_WATCH_RING;
    if (m_count < RC_WATCH_RING)
        m_count++;
    else
        m_dropped++;    /* the oldest just went; the counters did not */
}

int UiWatch::drain(UiEvent *out, int cap)
{
    int n = m_count < cap ? m_count : cap;
    int first = (m_head - m_count + RC_WATCH_RING * 2) % RC_WATCH_RING;
    /* Oldest first: a caller reading "dialog opened, then the OK button
     * appeared" backwards would draw the opposite conclusion. */
    for (int i = 0; i < n; i++)
        out[i] = m_ring[(first + i) % RC_WATCH_RING];
    m_count = 0;
    return n;
}

/* True when WM_QUIT came out of the queue.
 *
 * PM_REMOVE takes WM_QUIT like anything else and DispatchMessage drops it on
 * the floor, so a settle() that simply pumped would EAT the application's
 * shutdown: close the client while a wait is running and it does not exit.
 * It goes back where it came from and the wait gives up immediately -- a
 * wait is never more important than quitting. */
bool UiWatch::drainQueue()
{
    MSG msg;
    while (PeekMessageA(&msg, NULL, 0, 0, PM_REMOVE))
    {
        if (msg.message == WM_QUIT)
        {
            PostQuitMessage((int)msg.wParam);
            return true;
        }
        TranslateMessage(&msg);
        DispatchMessageA(&msg);
    }
    return false;
}

DWORD UiWatch::settle(DWORD ms)
{
    DWORD t0 = GetTickCount();
    DWORD until = t0 + ms;

    /* Drain first. The event we are about to block for may already be
     * sitting in the queue, and blocking on an empty wait for something
     * that has already happened is how a "subscribe then check" ordering
     * gets undone at the last step. */
    if (drainQueue())
        return GetTickCount() - t0;

    long left = (long)(until - GetTickCount());
    if (left > 0)
    {
        /* Not Sleep in a loop. MsgWaitForMultipleObjects wakes the instant
         * anything is posted, so the wait costs nothing while nothing is
         * happening and adds no latency of its own when it does. */
        MsgWaitForMultipleObjects(0, NULL, FALSE, (DWORD)left, QS_ALLINPUT);
        drainQueue();
    }
    return GetTickCount() - t0;
}
