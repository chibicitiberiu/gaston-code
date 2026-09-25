#include "automation/input.h"

#include <string.h>

/* Behind WINVER >= 0x0500 in the headers, and the build pins 0x0400. Whether
 * Win98 HONOURS them is the question; whether the header admits they exist
 * is not. Measured: it does honour them, and reports 15000 by default. */
#ifndef SPI_GETFOREGROUNDLOCKTIMEOUT
#define SPI_GETFOREGROUNDLOCKTIMEOUT 0x2000
#define SPI_SETFOREGROUNDLOCKTIMEOUT 0x2001
#endif

typedef UINT (WINAPI *PFN_SENDINPUT)(UINT, void *, int);

/* Walk up while the window is still a CHILD. GetAncestor(GA_ROOT) would say
 * this in one call and is not usable here: GA_ROOT sits behind
 * WINVER >= 0x0500 and the build pins 0x0400, and GetAncestor itself is not
 * on 95. GetParent alone is wrong -- for a top-level window it returns the
 * OWNER, so it walks past the root and out the other side. The WS_CHILD
 * test is what stops at exactly the right window. */
static HWND rootOf(HWND h)
{
    while (h && (GetWindowLongA(h, GWL_STYLE) & WS_CHILD))
    {
        HWND p = GetParent(h);
        if (!p || p == h)
            break;
        h = p;
    }
    return h;
}

Input::Input()
{
    /* Resolved, never imported. It is absent on 95 and on NT4 before SP3,
     * and a static import of something absent kills the image at load,
     * before WinMain, with nothing to debug. */
    HMODULE u32 = GetModuleHandleA("user32.dll");
    m_sendInput = u32 ? (void *)GetProcAddress(u32, "SendInput") : 0;
}

DWORD Input::relaxForegroundLock()
{
    DWORD before = 0;
    SystemParametersInfoA(SPI_GETFOREGROUNDLOCKTIMEOUT, 0, &before, 0);
    SystemParametersInfoA(SPI_SETFOREGROUNDLOCKTIMEOUT, 0, (void *)0,
                          SPIF_SENDCHANGE);
    return before;
}

void Input::restoreForegroundLock(DWORD previous)
{
    SystemParametersInfoA(SPI_SETFOREGROUNDLOCKTIMEOUT, 0,
                          (void *)(UINT_PTR)previous, SPIF_SENDCHANGE);
}

bool Input::isOurs(HWND target)
{
    DWORD pid = 0;
    if (!target || !IsWindow(target))
        return false;
    GetWindowThreadProcessId(target, &pid);
    return pid == GetCurrentProcessId();
}

InputResult Input::focus(HWND target)
{
    if (!target || !IsWindow(target))
        return INPUT_NO_WINDOW;
    if (isOurs(target))
        return INPUT_REFUSED_SELF;

    HWND top = rootOf(target);
    if (!top)
        top = target;

    if (IsIconic(top))
        ShowWindow(top, SW_RESTORE);
    SetForegroundWindow(top);

    if (GetForegroundWindow() != top)
    {
        /* It would not come forward on its own. Attach to the foreground
         * thread's input queue, set focus, and DETACH -- without the detach
         * this thread keeps the target's input-queue state for the rest of
         * its life, which poisons every later call. */
        DWORD me = GetCurrentThreadId();
        DWORD theirs = GetWindowThreadProcessId(top, NULL);
        if (theirs && theirs != me && AttachThreadInput(me, theirs, TRUE))
        {
            SetForegroundWindow(top);
            SetFocus(target);
            AttachThreadInput(me, theirs, FALSE);
        }
    }

    /* Verified, not assumed. If focus did not land, saying so is the whole
     * reason TYPE never types into the wrong window. */
    if (GetForegroundWindow() != top)
        return INPUT_NOT_FOREGROUND;
    return INPUT_OK;
}

InputResult Input::clickAt(HWND expect, int x, int y)
{
    if (expect && !IsWindow(expect))
        return INPUT_NO_WINDOW;
    if (expect && isOurs(expect))
        return INPUT_REFUSED_SELF;

    SetCursorPos(x, y);

    if (expect)
    {
        /* Occlusion falls out for free: whatever is under the point is what
         * the click will reach, so if it is not the intended control then
         * something is on top and delivering the click anyway would press
         * the wrong thing. */
        POINT p;
        p.x = x; p.y = y;
        HWND under = WindowFromPoint(p);
        if (under != expect && rootOf(under) != rootOf(expect))
            return INPUT_OCCLUDED;
    }

    mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0);
    mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0);
    return INPUT_OK;
}

InputResult Input::clickWindow(HWND target)
{
    RECT r;
    if (!target || !IsWindow(target))
        return INPUT_NO_WINDOW;
    if (isOurs(target))
        return INPUT_REFUSED_SELF;
    if (!GetWindowRect(target, &r))
        return INPUT_FAILED;
    return clickAt(target, (r.left + r.right) / 2, (r.top + r.bottom) / 2);
}

void Input::tapVk(int vk, bool extended)
{
    /* The E0 extended flag matters for the arrows, Insert/Delete/Home/End
     * and the page keys: without it the numeric-pad twins are sent instead
     * and an application that distinguishes them gets the wrong one.
     *
     * VK_SNAPSHOT is the documented oddity -- MapVirtualKey returns 0 for
     * it, so the scan code is supplied by hand. */
    UINT scan = (vk == VK_SNAPSHOT) ? 0x37 : MapVirtualKeyA((UINT)vk, 0);
    DWORD flags = extended ? KEYEVENTF_EXTENDEDKEY : 0;
    keybd_event((BYTE)vk, (BYTE)scan, flags, 0);
    keybd_event((BYTE)vk, (BYTE)scan, flags | KEYEVENTF_KEYUP, 0);
}

static bool isExtendedVk(int vk)
{
    switch (vk)
    {
    case VK_LEFT: case VK_RIGHT: case VK_UP: case VK_DOWN:
    case VK_HOME: case VK_END: case VK_PRIOR: case VK_NEXT:
    case VK_INSERT: case VK_DELETE: case VK_DIVIDE: case VK_NUMLOCK:
        return true;
    default:
        return false;
    }
}

InputResult Input::pressKey(HWND target, int vk)
{
    /* Never, on any path. On 9x the VMM owns Ctrl+Alt+Del at ring 0 so this
     * probably cannot synthesise it at all -- but it is one line, and the
     * cost of being wrong is somebody's machine restarting. */
    if (vk == VK_DELETE
        && (GetKeyState(VK_CONTROL) & 0x8000)
        && (GetKeyState(VK_MENU) & 0x8000))
        return INPUT_FAILED;

    InputResult r = focus(target);
    if (r != INPUT_OK)
        return r;
    tapVk(vk, isExtendedVk(vk));
    return INPUT_OK;
}

InputResult Input::typeText(HWND target, const char *text)
{
    if (!text)
        return INPUT_FAILED;

    InputResult r = focus(target);
    if (r != INPUT_OK)
        return r;

    for (const char *p = text; *p; p++)
    {
        /* VkKeyScan gives the virtual key in the low byte and the shift
         * state in the high one, for the CURRENT keyboard layout -- which
         * is why this is not a lookup table: the same character is a
         * different key on a German keyboard. */
        SHORT vs = VkKeyScanA(*p);
        if (vs == -1)
            continue;               /* not typeable on this layout */
        int vk = vs & 0xFF;
        int shift = (vs >> 8) & 0xFF;

        if (shift & 1) keybd_event(VK_SHIFT, 0, 0, 0);
        if (shift & 2) keybd_event(VK_CONTROL, 0, 0, 0);
        if (shift & 4) keybd_event(VK_MENU, 0, 0, 0);
        tapVk(vk, false);
        if (shift & 4) keybd_event(VK_MENU, 0, KEYEVENTF_KEYUP, 0);
        if (shift & 2) keybd_event(VK_CONTROL, 0, KEYEVENTF_KEYUP, 0);
        if (shift & 1) keybd_event(VK_SHIFT, 0, KEYEVENTF_KEYUP, 0);

        /* Re-check every character. A dialog can close under us halfway
         * through a string, and the rest would go to whatever inherited the
         * foreground -- which is exactly how a DELTREE ended up half in a
         * DOS box and half in a chat input on this project's own VM. */
        if (GetForegroundWindow() != rootOf(target))
            return INPUT_NOT_FOREGROUND;
    }
    return INPUT_OK;
}
