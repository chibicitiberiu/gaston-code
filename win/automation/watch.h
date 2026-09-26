/* watch.h -- change as a subscription, not a poll.
 *
 * `SetWinEventHook(WINEVENT_OUTOFCONTEXT)` delivers events about OTHER
 * processes' windows with no DLL in them at all. Gate 0.15b measured it on
 * both machines: 20 of 20 actions produced an event, across 11 distinct
 * event types, and the callback runs on our own thread out of the target's
 * message dispatch -- so nothing we do here can wedge the target.
 *
 * That is what makes a wait affordable. A poll at 50 ms over four seconds
 * is eighty tree scans on a machine where one scan is most of a second. The
 * feed turns that into a handful.
 *
 * OUTOFCONTEXT means the system POSTS to the installing thread's queue, so
 * the events arrive only while that thread pumps. This class does not own a
 * loop -- REMOTE.md forbids one in a library -- but `settle()` will dispatch
 * and block for a caller that is deliberately waiting.
 *
 * Everything here is bounded. The ring drops its oldest rather than growing,
 * and the sequence counters -- which never drop anything -- are what a wait
 * should be built on.
 */
#ifndef RC_AUTOMATION_WATCH_H
#define RC_AUTOMATION_WATCH_H

#include <windows.h>

/* HWINEVENTHOOK and WINEVENTPROC live behind WINVER >= 0x0500 and the build
 * pins 0x0400, so they are spelled out. The function itself is resolved with
 * GetProcAddress and never imported: a static import of something absent
 * kills the image at load, before WinMain, with nothing written. */
typedef HANDLE RC_HWINEVENTHOOK;
typedef void (CALLBACK *RC_WINEVENTPROC)(RC_HWINEVENTHOOK, DWORD, HWND,
                                         LONG, LONG, DWORD, DWORD);

/* What an event means to a caller, which is a much shorter list than what
 * the system sends. A wait does not care that a menu opened rather than a
 * dialog; it cares that the tree it walked a moment ago is now wrong. */
enum UiChange
{
    UI_OTHER = 0,
    UI_STRUCTURE,   /* something appeared, vanished, or was reordered */
    UI_CONTENT,     /* a node's name, value or state changed */
    UI_FOCUS,       /* focus or the foreground window moved */
    UI_LOCATION     /* something moved or resized -- the noisy one */
};

struct UiEvent
{
    DWORD    raw;       /* the EVENT_* number, for a caller that wants it */
    UiChange kind;
    HWND     hwnd;
    long     idObject;
    long     idChild;
    DWORD    tid;
    DWORD    at;        /* GetTickCount at delivery */
};

/* 32 events is about a second of a busy desktop. Past that the OLDEST is
 * dropped and counted: a ring that grows is a ring that runs a 32MB guest
 * out of memory while nobody is reading it, and the sequence counters
 * survive an overflow so a wait built on them cannot miss a change. */
#define RC_WATCH_RING 32

class UiWatch
{
public:
    UiWatch();
    ~UiWatch();

    /* False when SetWinEventHook is not there. Win98 SE has it; Win95 with
     * no MSAA redistributable does not, and the caller then polls. */
    bool available() const { return m_set != 0; }

    /* `pid` and `tid` are filters, 0 for "anything". Both hooks are
     * installed on the CALLING thread and that thread must be the one that
     * pumps -- OUTOFCONTEXT delivery has no other route. */
    /* `skipOwnProcess` keeps our own repaints out of the feed, which a
     * client watching the desktop wants: left in, it wakes on its own
     * window, rescans, repaints, and wakes itself again. Pass false only to
     * watch our own UI. */
    bool start(DWORD pid, DWORD tid, bool skipOwnProcess = true);
    void stop();
    bool running() const { return m_sys != 0 || m_obj != 0; }

    /* Monotonic, and they never miss: a dropped ring entry still bumps
     * these. Compare before and after to ask "did anything change", which
     * is the whole of what a wait needs. */
    unsigned structureSeq() const { return m_structSeq; }
    unsigned contentSeq() const   { return m_contentSeq; }
    unsigned focusSeq() const     { return m_focusSeq; }
    unsigned anySeq() const       { return m_anySeq; }

    int events() const  { return m_events; }
    int dropped() const { return m_dropped; }
    /* Events discarded as our own, and events about a window that had
     * already gone by the time the callback ran -- those cannot be
     * attributed to anyone and are kept. */
    int ownDropped() const   { return m_ownDropped; }
    int unattributed() const { return m_unattributed; }

    /* Copies out up to `cap` events, oldest first, and empties the ring. */
    int drain(UiEvent *out, int cap);

    /* Dispatch whatever is queued so the callbacks run, then block until
     * another message arrives or `ms` elapses. Returns the milliseconds
     * actually spent. A caller that already has a message loop does not
     * need this; a caller that is blocking does, or the events it is
     * waiting for never leave its own queue. */
    DWORD settle(DWORD ms);

    static UiChange classify(DWORD ev);

    /* Public only so the system's context-free callback can reach it. */
    void onEvent(DWORD ev, HWND h, long obj, long child, DWORD tid);

private:
    bool drainQueue();      /* true if WM_QUIT came out; see watch.cpp */

    void *m_set;            /* SetWinEventHook */
    void *m_unset;          /* UnhookWinEvent */
    RC_HWINEVENTHOOK m_sys; /* the 0x0001..0x0020 range */
    RC_HWINEVENTHOOK m_obj; /* the 0x8000..0x800F range */

    UiEvent  m_ring[RC_WATCH_RING];
    int      m_head;        /* next write */
    int      m_count;
    int      m_events;
    int      m_dropped;

    unsigned m_structSeq;
    unsigned m_contentSeq;
    unsigned m_focusSeq;
    unsigned m_anySeq;

    bool     m_skipOwn;
    DWORD    m_selfPid;
    int      m_ownDropped;
    int      m_unattributed;

    UiWatch(const UiWatch &);
    UiWatch &operator=(const UiWatch &);
};

#endif
