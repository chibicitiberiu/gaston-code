/* input.h -- clicking and typing at another program.
 *
 * Everything here can go wrong in a way that is somebody else's problem, so
 * the refusals matter more than the actions.
 *
 * `SendInput` exists on 98 but not on 95 or NT4 before SP3, so it is
 * resolved with GetProcAddress and preferred when found: it delivers a
 * batch atomically, which matters on a machine a person may also be
 * touching. `keybd_event`/`mouse_event` are the fallback and are what
 * REMOTE.md assumed would be the only path.
 */
#ifndef RC_AUTOMATION_INPUT_H
#define RC_AUTOMATION_INPUT_H

#include <windows.h>

enum InputResult
{
    INPUT_OK = 0,
    INPUT_REFUSED_SELF,     /* the target belongs to this process */
    INPUT_NO_WINDOW,        /* not a window, or gone */
    INPUT_NOT_FOREGROUND,   /* focus would not move; we did not type */
    INPUT_OCCLUDED,         /* something else is on top of the point */
    INPUT_FAILED
};

class Input
{
public:
    Input();

    /* Try to make the foreground lock stop flashing a taskbar button
     * instead of raising a window. Measured at 15000ms on Win98 SE, and
     * setting it to 0 is accepted and reads back as 0. Call once at start.
     * Returns the value that was there, so a caller can put it back --
     * leaving it at 0 is a change to somebody's machine that outlives us.
     */
    static DWORD relaxForegroundLock();
    static void  restoreForegroundLock(DWORD previous);

    /* True if this window belongs to us. Compares the PROCESS, not the
     * HWND: a child control is a different HWND on the same thread, and an
     * HWND comparison lets the agent type into our own input box in a
     * self-feeding loop. */
    static bool isOurs(HWND target);

    /* Bring `target` forward and give it focus, attaching to its input
     * queue only if it will not come forward on its own -- and DETACHING
     * afterwards, or this thread permanently inherits the target's
     * input-queue state. Verifies with GetForegroundWindow and reports
     * INPUT_NOT_FOREGROUND rather than typing somewhere unintended. */
    InputResult focus(HWND target);

    /* Click at a screen point. After moving, WindowFromPoint must be the
     * window that was asked for, or something is on top of it and the
     * click is refused rather than delivered to whatever is there. */
    InputResult clickAt(HWND expect, int x, int y);

    /* The centre of `target`'s rect, then clickAt. One execution path, not
     * two: a control reference resolves to a point and then performs the
     * same real click a coordinate would. */
    InputResult clickWindow(HWND target);

    /* Plain characters and the few keys a dialog needs. Ctrl+Alt+Del is
     * refused: on 9x the VMM owns it at ring 0 so this probably cannot
     * synthesise it anyway, and the cost of being wrong is a reboot. */
    InputResult typeText(HWND target, const char *text);
    InputResult pressKey(HWND target, int vk);

    bool usingSendInput() const { return m_sendInput != 0; }

private:
    void *m_sendInput;      /* PFN, NULL when this OS lacks it */
    void  tapVk(int vk, bool extended);

    Input(const Input &);
    Input &operator=(const Input &);
};

#endif
