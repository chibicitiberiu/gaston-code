/* "Gaston is controlling the desktop": a topmost strip with a Stop button
 * and a global chord, raised while a step list drives the desktop.
 *
 * It owns a thread, because the only thread that could otherwise host it is
 * the one blocked inside StepRunner::run for the whole drive -- a button or
 * a hotkey there would be dead for up to stepMs. Everything crossing into
 * that thread is a PostMessage or a read of the published HWND; nothing
 * SendMessages it, and nothing outside it touches the Window object.
 *
 * Two invariants, both load-bearing:
 *
 *   IT NEVER TAKES THE FOREGROUND. SW_SHOWNOACTIVATE, MA_NOACTIVATE, and no
 *   child control to hold focus. steps.cpp's keyboardTargetOk fails EVERY
 *   typing step with refused_self when the foreground is ours, so a strip
 *   that can be activated breaks the feature it belongs to. This is also
 *   the whole of the answer to "is it transparent to the keyboard".
 *
 *   STOP ACTS ON THE BUTTON DOWN, not on a click. clickAt SetCursorPos's
 *   before every click, and there is one input queue on 9x, so while the
 *   agent is driving the pointer is being teleported away between a real
 *   button's down and its up. A BN_CLICKED Stop would fail exactly when it
 *   is on screen. The chord is the primary route; the button is the backup.
 */
#ifndef RC_INDICATOR_H
#define RC_INDICATOR_H

#ifndef __cplusplus
#error "automation/indicator.h is C++"
#endif

#include <windows.h>
#include "automation/overlay.h"
#include "core/thread.h"

/* Posted to the strip; its own block, audited by tools/check/msgids.py. */
#define RC_WM_IND_WAKE  (WM_APP + 0x500)

class DriveIndicator : public DriveOverlay
{
public:
    /* `who` is the sentence the person reads; `chordVk` with Ctrl+Alt+Shift
     * is the cancel chord, and `chordText` is how the strip spells it. The
     * two programs pass different values of both, because whichever
     * registers first wins the chord and the second is told it did not.
     *
     * Nothing happens here but setting members: the thread starts on the
     * first drive, so a program that never drives never makes a window.
     */
    DriveIndicator(StopSink *sink, const char *who, UINT chordVk,
                   const char *chordText);
    ~DriveIndicator();

    /* DriveOverlay. */
    void driveStarting(volatile LONG *cancel);
    void driveEnded();
    bool covers(int x, int y);
    bool coversRect(const RECT &r);
    bool stepAside(DWORD budgetMs, bool settle);
    void resume();

    /* The person pressed Stop. Public because the strip calls it. */
    void stopPressed();

    /* Timings, in one place because E12 measures them and they are guesses
     * until it does. */
    enum {
        SHOW_DELAY_MS   = 250,    /* a 50ms focus must not flash a window  */
        MIN_VISIBLE_MS  = 1500,   /* ...and nothing must flash OFF either  */
        DWELL_MS        = 3000,   /* back-to-back lists are one drive      */
        TICK_MS         = 100,    /* the strip's own clock                 */
        HIDE_BUDGET_MS  = 400,    /* what a click waits for it to go       */
        /* What a capture waits on top, for the desktop under the strip
         * to repaint. MEASURED at 12ms on the guest, for a trivial
         * window, and kept at ten times that deliberately: the window
         * under it in real use is a file listing or a browser repainting
         * on another process's pump, and this is paid once per
         * screenshot against hundreds of milliseconds of BitBlt.
         * research/measurements/stripprobe-vm-20261014.txt. */
        SETTLE_MS       = 120,
        STOP_SAYS_MS    = 700     /* "Stopping..." is readable before it  */
    };

private:
    class Strip;
    class Body : public Runnable
    {
    public:
        Body(DriveIndicator &owner) : m_owner(owner) {}
        void run();
    private:
        Body(const Body &);
        Body &operator=(const Body &);
        DriveIndicator &m_owner;
    };

    friend class Body;
    friend class Strip;

    void threadBody();
    bool ensureThread();
    bool pumpWait(bool wantVisible, DWORD budgetMs);

    DriveIndicator(const DriveIndicator &);
    DriveIndicator &operator=(const DriveIndicator &);

    Thread          m_thread;
    Body            m_body;
    StopSink       *m_sink;
    const char     *m_who;
    const char     *m_chordText;
    UINT            m_chordVk;

    /* Published by the thread, read by every other one. */
    volatile HWND   m_hwnd;
    volatile LONG  *m_cancel;
    volatile LONG   m_driving;      /* a list is running right now        */
    volatile LONG   m_suppressed;   /* a click or a capture asked it away */
    volatile LONG   m_stopped;      /* Stop was pressed; say so and go    */
    volatile LONG   m_failed;       /* the window could not be made: once */
    volatile LONG   m_quit;         /* the owner is going; see ~DriveIndicator */
    volatile DWORD  m_driveStart;   /* GetTickCount at the first driveStarting */
    volatile DWORD  m_lastDrive;    /* GetTickCount at the last driveEnded*/
    volatile DWORD  m_stopTick;     /* when Stop was pressed, so it can say so */
};

#endif
