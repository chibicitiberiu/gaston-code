#include "automation/input.h"

#include "core/str.h"

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
    m_blocker[0] = 0;
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

    /* THE KEYBOARD FOCUS INSIDE THE WINDOW, not just the window.
     *
     * `SetFocus` used to run only in the fallback below -- when the window
     * refused to come forward -- so on the ordinary path a named CHILD control
     * never got focus, and the keystrokes went to whichever child already had
     * it. `typeText` then returned INPUT_OK for a control it had not typed
     * into. Measured on real hardware 2026-10-02: typing into an edit box by
     * name left the box empty and reported success, and the same call after a
     * click worked -- which is why the one live session that hit this got away
     * with it (docs/measurements/keys-named-p200-20261002.txt).
     *
     * Only when the target IS a child: for a top-level window
     * SetForegroundWindow already did it, and attaching to another thread's
     * input queue is not free. DETACH always -- without it this thread keeps
     * the target's input-queue state for the rest of its life and poisons
     * every later call. */
    if (target != top)
    {
        DWORD me = GetCurrentThreadId();
        DWORD theirs = GetWindowThreadProcessId(top, NULL);
        if (theirs && theirs != me && AttachThreadInput(me, theirs, TRUE))
        {
            SetFocus(target);
            AttachThreadInput(me, theirs, FALSE);
        }
    }

    if (GetForegroundWindow() != top)
    {
        /* It would not come forward on its own. Same attach, plus another go
         * at the foreground, which is the case this branch was written for. */
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

/* Who is on top, in words the caller can pass on. A refusal that says only
 * "something is on top" leaves a model guessing between its own window, a
 * dialog it has not noticed and the screensaver. */
void Input::noteBlocker(HWND under)
{
    HWND top = rootOf(under);
    char cls[48];
    char title[64];

    m_blocker[0] = 0;
    if (!top || !IsWindow(top))
        return;
    cls[0] = 0;
    title[0] = 0;
    GetClassNameA(top, cls, sizeof(cls));
    GetWindowTextA(top, title, sizeof(title));
    if (title[0])
        rc_strf(m_blocker, sizeof(m_blocker), "%s \"%s\"", cls, title);
    else
        rc_strf(m_blocker, sizeof(m_blocker), "%s", cls);
}

InputResult Input::clickAt(HWND expect, int x, int y)
{
    m_blocker[0] = 0;
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
        {
            /* Covered. Raise it and look again, because a person clicking
             * this control would have raised the window by clicking it --
             * refusing here told the caller to "bring the window forward
             * first" and there was nothing that could (BACKLOG 16).
             *
             * Only when covered, not on every click: an uncovered window is
             * activated by the click itself, and a foreground change nobody
             * needed is a foreground change taken from whoever is at the
             * keyboard.
             *
             * Measured on Win98 SE (docs/measurements/focus-vm-20260927.txt):
             * SetForegroundWindow from a background process DOES work and
             * puts the window on top, while BringWindowToTop and
             * SetWindowPos(HWND_TOP, SWP_NOACTIVATE) are both ignored
             * across a process boundary. Activation is the only route, so
             * this goes through focus() rather than a cheaper z-order call.
             */
            InputResult raised = focus(expect);
            if (raised != INPUT_OK)
                return INPUT_OCCLUDED;

            /* The z-order settles after the call returns, so look again for
             * a bounded while rather than once. */
            int waited;
            for (waited = 0; waited < RAISE_SETTLE_MS; waited += RAISE_STEP_MS)
            {
                under = WindowFromPoint(p);
                if (under == expect || rootOf(under) == rootOf(expect))
                    break;
                Sleep(RAISE_STEP_MS);
            }

            /* Raising can move the window, so the point we measured may no
             * longer be over the control even though nothing covers it. */
            SetCursorPos(x, y);
            under = WindowFromPoint(p);
            if (under != expect && rootOf(under) != rootOf(expect))
            {
                noteBlocker(under);
                return INPUT_OCCLUDED;
            }
        }
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

/* --- named keys in square brackets ---------------------------------------
 *
 * WHY THIS EXISTS. `typeText` used to be characters and nothing else, while
 * the tool that calls it advertised "plain characters plus Tab, Enter, Escape
 * and the arrow keys" -- and an arrow key is not a character, so there was no
 * way to express one. An agent asked to clear a text box reached for the
 * obvious thing, `[Delete][Delete][Delete][Delete]Tiberiu`, and got
 * `John[Delete][Delete][D` in the box and "Typed 39 characters" as the reply
 * (live session, 2026-10-02). It did exactly what it was told and the reply
 * was true; neither helped.
 *
 * `[` AS THE BRACKET, not `{` as SendKeys uses, because `[` is what a model
 * actually reaches for unprompted -- that is where the measurement above came
 * from. `{Name}` is accepted as well for the same reason in reverse: SendKeys
 * is the convention most likely to have been learned. `[[` is a literal `[`.
 */

struct KeyName
{
    const char *name;
    int vk;
};

/* NAMES, not scan codes, and lower case so the compare is one pass.
 *
 * Aliases are deliberate and cheap: a model that writes `Esc`, `Return`,
 * `PgDn` or `Del` meant the same key, and refusing it teaches nothing. What
 * is NOT here is anything whose absence is better than a guess -- no `Break`,
 * no `SysReq`, nothing that needs a scan code this does not have.
 *
 * Scraped by tests/python/guest/test_guest_agreement.py so the bridge's own
 * list cannot drift from this one. Keep the braces and the quoting plain.
 */
static const KeyName KEY_NAMES[] = {
    { "backspace", VK_BACK },     { "bksp",   VK_BACK },
    { "tab",       VK_TAB },      { "enter",  VK_RETURN },
    { "return",    VK_RETURN },   { "escape", VK_ESCAPE },
    { "esc",       VK_ESCAPE },   { "space",  VK_SPACE },
    { "delete",    VK_DELETE },   { "del",    VK_DELETE },
    { "insert",    VK_INSERT },   { "ins",    VK_INSERT },
    { "home",      VK_HOME },     { "end",    VK_END },
    { "pageup",    VK_PRIOR },    { "pgup",   VK_PRIOR },
    { "pagedown",  VK_NEXT },     { "pgdn",   VK_NEXT },
    { "up",        VK_UP },       { "down",   VK_DOWN },
    { "left",      VK_LEFT },     { "right",  VK_RIGHT },
    { "f1",  VK_F1 },  { "f2",  VK_F2 },  { "f3",  VK_F3 },
    { "f4",  VK_F4 },  { "f5",  VK_F5 },  { "f6",  VK_F6 },
    { "f7",  VK_F7 },  { "f8",  VK_F8 },  { "f9",  VK_F9 },
    { "f10", VK_F10 }, { "f11", VK_F11 }, { "f12", VK_F12 }
};
enum { KEY_NAMES_N = sizeof(KEY_NAMES) / sizeof(KEY_NAMES[0]) };

/* Modifier bits, the same order typeText already pushes them in, and the same
 * order VkKeyScan returns them in -- which is why they are these values and
 * not Windows'.
 *
 * KEYMOD_ AND NOT MOD_: `MOD_SHIFT`, `MOD_CONTROL` and `MOD_ALT` are already
 * macros in winuser.h, for RegisterHotKey, AND THEIR BITS ARE DIFFERENT
 * (`MOD_ALT` is 1 there and 4 here). Watcom caught it as a syntax error on the
 * enum; had the names merely shadowed instead, every modifier would have been
 * silently wrong. */
enum { KEYMOD_SHIFT = 1, KEYMOD_CTRL = 2, KEYMOD_ALT = 4 };

static char lowerAscii(char c)
{
    return (c >= 'A' && c <= 'Z') ? (char)(c - 'A' + 'a') : c;
}

/* `a` against a NUL-terminated lower-case name, case-insensitively, for
 * exactly `len` characters of `a`. */
static bool nameIs(const char *a, unsigned len, const char *lower)
{
    unsigned i;
    for (i = 0; i < len; i++)
    {
        if (!lower[i] || lowerAscii(a[i]) != lower[i])
            return false;
    }
    return lower[i] == '\0';
}

/* One `Ctrl+Shift+Name` body, between the brackets. False if the name is not
 * one this knows, in which case nothing is written. */
static bool parseKeyBody(const char *body, unsigned len, int &vk, int &mods)
{
    unsigned at = 0;
    vk = 0;
    mods = 0;

    for (;;)
    {
        /* A `+` SEPARATES A MODIFIER FROM WHAT FOLLOWS, and is also a
         * perfectly good key name on its own -- `[Ctrl++]`. So the split
         * looks for a `+` that is not the last character. */
        unsigned plus = at;
        while (plus < len && body[plus] != '+')
            plus++;
        if (plus >= len || plus + 1 >= len)
            break;                      /* no separator left: the rest is the key */

        unsigned n = plus - at;
        if (nameIs(body + at, n, "ctrl") || nameIs(body + at, n, "control"))
            mods |= KEYMOD_CTRL;
        else if (nameIs(body + at, n, "shift"))
            mods |= KEYMOD_SHIFT;
        else if (nameIs(body + at, n, "alt"))
            mods |= KEYMOD_ALT;
        else
            return false;               /* a word that is not a modifier */
        at = plus + 1;
    }

    unsigned n = len - at;
    if (!n)
        return false;

    for (int i = 0; i < KEY_NAMES_N; i++)
    {
        if (nameIs(body + at, n, KEY_NAMES[i].name))
        {
            vk = KEY_NAMES[i].vk;
            return true;
        }
    }

    /* A SINGLE CHARACTER IS A KEY TOO, which is the whole point of
     * `[Ctrl+A]`. VkKeyScanA so it is the right key on this layout, and its
     * own shift state is folded in -- `[Ctrl+?]` on a US layout is
     * Ctrl+Shift+/ and has to be sent that way. */
    if (n == 1)
    {
        SHORT vs = VkKeyScanA(body[at]);
        if (vs == -1)
            return false;
        vk = vs & 0xFF;
        mods |= (vs >> 8) & 0x07;
        return true;
    }
    return false;
}

/* The token starting at `p` (which points at the opening bracket). Returns the
 * number of characters consumed, or 0 if this is not a token this understands
 * -- including an unterminated bracket, which is refused rather than typed. */
static unsigned parseKeyToken(const char *p, int &vk, int &mods)
{
    char close = (*p == '[') ? ']' : '}';
    unsigned len = 0;
    while (p[1 + len] && p[1 + len] != close)
        len++;
    if (p[1 + len] != close || !len)
        return 0;
    if (!parseKeyBody(p + 1, len, vk, mods))
        return 0;
    return len + 2;
}

InputResult Input::typeText(HWND target, const char *text,
                            char *badKey, unsigned badKeySize)
{
    if (badKey && badKeySize)
        badKey[0] = '\0';
    if (!text)
        return INPUT_FAILED;

    /* PASS ONE: VALIDATE THE WHOLE STRING, before a single keystroke.
     *
     * A string half delivered into somebody's text box is worse than one
     * refused -- and worse than that, the caller cannot tell how far it got.
     * So an unknown `[Name]` is found here, with nothing typed and the token
     * named back. */
    for (const char *p = text; *p; )
    {
        if (*p == '[' && p[1] == '[')      { p += 2; continue; }
        if (*p == '{' && p[1] == '{')      { p += 2; continue; }
        if (*p == '[' || *p == '{')
        {
            int vk = 0, mods = 0;
            unsigned used = parseKeyToken(p, vk, mods);
            if (!used)
            {
                if (badKey && badKeySize)
                {
                    /* Up to the closing bracket, or the end: enough for the
                     * caller to quote back exactly what it did not like. */
                    unsigned n = 0;
                    char close = (*p == '[') ? ']' : '}';
                    while (p[n] && n + 1 < badKeySize)
                    {
                        badKey[n] = p[n];
                        if (p[n] == close) { n++; break; }
                        n++;
                    }
                    badKey[n] = '\0';
                }
                return INPUT_BAD_KEY;
            }
            p += used;
            continue;
        }
        p++;
    }

    InputResult r = focus(target);
    if (r != INPUT_OK)
        return r;

    /* PASS TWO: send it. */
    for (const char *p = text; *p; )
    {
        int vk, mods;
        unsigned used;

        if ((*p == '[' && p[1] == '[') || (*p == '{' && p[1] == '{'))
        {
            vk = VkKeyScanA(*p) & 0xFF;
            mods = (VkKeyScanA(*p) >> 8) & 0x07;
            used = 2;
        }
        else if (*p == '[' || *p == '{')
        {
            used = parseKeyToken(p, vk, mods);
            /* Pass one proved this parses; a zero here would be a different
             * string, which cannot happen. */
            if (!used)
                return INPUT_FAILED;
        }
        else
        {
            /* VkKeyScan gives the virtual key in the low byte and the shift
             * state in the high one, for the CURRENT keyboard layout -- which
             * is why this is not a lookup table: the same character is a
             * different key on a German keyboard. */
            SHORT vs = VkKeyScanA(*p);
            if (vs == -1)
            {
                p++;
                continue;              /* not typeable on this layout */
            }
            vk = vs & 0xFF;
            mods = (vs >> 8) & 0x07;
            used = 1;
        }

        /* NEVER, on any path, and checked on the composed modifiers rather
         * than on the keyboard state: `[Ctrl+Alt+Delete]` reaches here as one
         * token and would otherwise sail past pressKey's guard, which only
         * sees keys already held down. On 9x the VMM owns this at ring 0 so
         * it probably cannot be synthesised at all -- but the cost of being
         * wrong is somebody's machine restarting. */
        if (vk == VK_DELETE && (mods & KEYMOD_CTRL) && (mods & KEYMOD_ALT))
            return INPUT_FAILED;

        if (mods & KEYMOD_SHIFT) keybd_event(VK_SHIFT, 0, 0, 0);
        if (mods & KEYMOD_CTRL)  keybd_event(VK_CONTROL, 0, 0, 0);
        if (mods & KEYMOD_ALT)   keybd_event(VK_MENU, 0, 0, 0);
        tapVk(vk, isExtendedVk(vk));
        if (mods & KEYMOD_ALT)   keybd_event(VK_MENU, 0, KEYEVENTF_KEYUP, 0);
        if (mods & KEYMOD_CTRL)  keybd_event(VK_CONTROL, 0, KEYEVENTF_KEYUP, 0);
        if (mods & KEYMOD_SHIFT) keybd_event(VK_SHIFT, 0, KEYEVENTF_KEYUP, 0);

        /* Re-check every character. A dialog can close under us halfway
         * through a string, and the rest would go to whatever inherited the
         * foreground -- which is exactly how a DELTREE ended up half in a
         * DOS box and half in a chat input on this project's own VM. */
        if (GetForegroundWindow() != rootOf(target))
            return INPUT_NOT_FOREGROUND;
        p += used;
    }
    return INPUT_OK;
}
