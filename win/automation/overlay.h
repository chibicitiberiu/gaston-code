/* The "agent is driving" indicator, as the driving code sees it.
 *
 * Abstract on purpose, and header-only on purpose. `libautomation` is linked
 * by uicheck, autocheck, keyprobe and focusprobe as well as by the two
 * programs, and wlink pulls a library member in on any symbol reference -- so
 * a direct call from input.cpp or steps.cpp would drag the window, the thread
 * and the paint code into four images that can never raise it. Everything
 * here is a vtable slot; only the two Apps name DriveIndicator.
 *
 * The same rule in the other direction: the indicator never names the wire
 * (CLAUDE.md -- a library knows nothing of the ops or of the wire), so what
 * Stop DOES is a StopSink the owning program implements.
 */
#ifndef RC_OVERLAY_H
#define RC_OVERLAY_H

#ifndef __cplusplus
#error "automation/overlay.h is C++"
#endif

#include <windows.h>

class DriveOverlay
{
public:
    /* Inline and empty: a key function here would be defined in
     * indicator.cpp, and referencing it is the thing this class exists to
     * avoid. */
    virtual ~DriveOverlay() {}

    /* A step list is about to run, with the flag Stop raises to cancel it.
     * The strip does not appear immediately -- see SHOW_DELAY_MS. */
    virtual void driveStarting(volatile LONG *cancel) = 0;

    /* The list ended, however it ended. The strip lingers for the dwell. */
    virtual void driveEnded() = 0;

    /* Is the strip on screen and over this point / rect? Cheap, and safe
     * from any thread: a published HWND, GetWindowRect and IsWindowVisible.
     */
    virtual bool covers(int x, int y) = 0;
    virtual bool coversRect(const RECT &r) = 0;

    /* Get out of the way, and do not return until it is true -- posting a
     * hide and clicking is a race the strip wins often enough to matter.
     * False means it did not go within the budget, and the caller must
     * refuse rather than act blind. `settle` additionally waits for what
     * was underneath to repaint, which is what a screenshot needs and a
     * click does not (hit-testing is z-order, not pixels).
     */
    virtual bool stepAside(DWORD budgetMs, bool settle) = 0;

    /* Put it back, if the drive still wants it. Idempotent. */
    virtual void resume() = 0;
};

/* The cancel chords: Ctrl+Alt+Shift+F12 in the client, F11 in gremote.
 *
 * Two, because both programs can run at once (BACKLOG 19, "two programs
 * can drive one desktop") and RegisterHotKey is first-come -- one chord
 * would mean the second program silently has no keyboard cancel at all.
 *
 * Here rather than in the indicator, because the OTHER half of a global
 * hotkey is that a synthesised keystroke fires it just as a real one does:
 * keybd_event is indistinguishable at the far end. Both are therefore
 * refused to every step list, in BOTH programs, and that refusal reads
 * these names. F-keys and not letters so the denylist costs the agent
 * nothing it is likely to want. */
enum { RC_CHORD_VK_CLIENT = VK_F12, RC_CHORD_VK_REMOTE = VK_F11 };

/* The strip out of the way for as long as this object lives, and back
 * afterwards however the scope leaves. Header-only and inline so every
 * caller shares one definition of "ask, wait, put it back".
 *
 * ONE of these per operation, never nested: clickButtonAt calls clickAt
 * internally, so a guard in each would put the strip back at the inner
 * destructor -- before the outer one's mouse_event. */
class OverlayAside
{
public:
    OverlayAside() : m_ov(0) {}

    /* For a click: the point. False means it would not go, and the caller
     * must refuse rather than act blind. */
    bool forPoint(DriveOverlay *ov, int x, int y, DWORD budgetMs)
    {
        if (!ov || !ov->covers(x, y))
            return true;
        if (!ov->stepAside(budgetMs, false))
            return false;
        m_ov = ov;
        return true;
    }

    /* For a capture: the rect, and the pixels under it have to have been
     * repainted, not merely un-owned. A capture reads the framebuffer. */
    bool forCapture(DriveOverlay *ov, const RECT &r, DWORD budgetMs)
    {
        if (!ov || !ov->coversRect(r))
            return true;
        if (!ov->stepAside(budgetMs, true))
            return false;
        m_ov = ov;
        return true;
    }

    ~OverlayAside() { if (m_ov) m_ov->resume(); }

private:
    OverlayAside(const OverlayAside &);
    OverlayAside &operator=(const OverlayAside &);
    DriveOverlay *m_ov;
};

class StopSink
{
public:
    virtual ~StopSink() {}

    /* The person pressed Stop or used the chord. Called on the indicator's
     * own thread, so it must not block: both implementations post. */
    virtual void onStop() = 0;
};

#endif
