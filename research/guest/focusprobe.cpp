/* focusprobe -- can a BACKGROUND process raise another process's window on 98?
 *
 * Not a check. It measures the one thing BACKLOG 16's fix depends on, and
 * that no amount of Wine can answer: Wine's window manager is the host's, and
 * it grants foreground changes that Windows 98 may refuse.
 *
 * The situation being reproduced is the client's, exactly. The client is not
 * the foreground window -- the user just pressed Allow on its permission bar,
 * or it never had focus at all -- and it wants to click a control in a THIRD
 * process's window that is sitting behind something else. `Input::clickAt`
 * refuses that click today because `WindowFromPoint` says another window is on
 * top, and the advice it gives ("bring the window forward first") names an
 * action nothing implements.
 *
 * So: two Notepads. The second one is foreground and covers the first. We are
 * a third process, in the background, trying to raise the first. Every route
 * is tried in turn and the answer is printed, because on 9x the rules for
 * SetForegroundWindow are not the documented NT ones and the only way to know
 * is to ask the machine.
 *
 * Output goes to the console: this box has no serial port and no floppy, so a
 * screenshot is the transport. Keep the lines short and few.
 */

#include <windows.h>
#include <stdio.h>
#include <string.h>

#include "core/str.h"
#include "automation/input.h"

/* ---- finding the two Notepads ---------------------------------------- */

struct Found
{
    HWND h[8];
    int  n;
};

static BOOL CALLBACK collect(HWND h, LPARAM lp)
{
    Found *f = (Found *)lp;
    char cls[64];

    if (f->n >= 8)
        return FALSE;
    if (!IsWindowVisible(h))
        return TRUE;
    if (!GetClassNameA(h, cls, sizeof(cls)))
        return TRUE;
    if (strcmp(cls, "Notepad") != 0)
        return TRUE;
    f->h[f->n++] = h;
    return TRUE;
}

static int notepads(Found &f)
{
    f.n = 0;
    EnumWindows(collect, (LPARAM)&f);
    return f.n;
}

static DWORD pidOf(HWND h)
{
    DWORD pid = 0;
    GetWindowThreadProcessId(h, &pid);
    return pid;
}

/* The same root walk clickAt uses (input.cpp:21). GetAncestor would be
 * shorter and is exactly the kind of import this project refuses. */
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

/* Is `h` the window the click would actually reach at its own centre? That
 * is the question clickAt asks, so it is the question that decides whether a
 * raise was good enough -- foreground is not the same as on top. */
static int onTopAtCentre(HWND h)
{
    RECT r;
    POINT p;
    HWND under;

    if (!GetWindowRect(h, &r))
        return 0;
    p.x = (r.left + r.right) / 2;
    p.y = (r.top + r.bottom) / 2;
    under = WindowFromPoint(p);
    if (!under)
        return 0;
    if (under == h)
        return 1;
    return rootOf(under) == h;
}

static void say(const char *name, int ok, const char *detail)
{
    printf("%-28s %-4s %s\n", name, ok ? "yes" : "NO", detail ? detail : "");
    fflush(stdout);
}

int main(void)
{
    Found f;
    HWND target, fg;
    DWORD ourPid = GetCurrentProcessId();
    char detail[128];
    OSVERSIONINFOA v;
    int i;

    /* UNBUFFERED, and it is not a nicety. The header comment above says the
     * transport is a screenshot, which was true when this box had no network:
     * against a console stdout is line-buffered and every line appears as it
     * is printed. Driven over gremote's exec lane the same stdout is a PIPE,
     * so it is BLOCK-buffered at 4KB, and the only fflush is after the last
     * test -- which means a probe that hangs part-way returns ZERO BYTES and
     * says nothing about where it stopped.
     *
     * Measured on the Pentium 200, 2026-10-23: it does hang, hitting
     * gremote's 120s max_timeout_ms with exit 1 and no output at all. A probe
     * whose failure mode is silence cannot answer BACKLOG 9.
     *
     * THIS LINE DID NOT FIX THAT, and saying so is the point of writing it
     * down. Unbuffered, over the same lane, the second run produced zero
     * bytes again -- so the silence is not the 4KB buffer, and this program
     * is not reaching its first printf when gremote starts it. PROBE.EXE's
     * stdout comes back over the same lane in the same session, so the lane
     * works; whatever is different here is in this image. The line stays
     * because it is correct and costs nothing, and because the next person
     * to pipe this probe should not have to rule the buffer out twice.
     *
     * Until that is understood, this probe is a KEYBOARD instrument, as its
     * header comment always said. Run it from a DOS box on the machine. */
    setvbuf(stdout, (char *)0, _IONBF, 0);

    v.dwOSVersionInfoSize = sizeof(v);
    GetVersionExA(&v);
    printf("focusprobe: %lu.%lu build %lu\n",
           (unsigned long)v.dwMajorVersion, (unsigned long)v.dwMinorVersion,
           (unsigned long)(v.dwBuildNumber & 0xFFFF));

    /* Two Notepads: the second covers the first and takes the foreground. */
    WinExec("NOTEPAD.EXE", SW_SHOWNORMAL);
    Sleep(2500);
    WinExec("NOTEPAD.EXE", SW_SHOWNORMAL);
    Sleep(2500);

    if (notepads(f) < 2)
    {
        printf("need two Notepad windows, found %d -- cannot measure\n", f.n);
        return 2;
    }

    fg = GetForegroundWindow();
    target = NULL;
    for (i = 0; i < f.n; i++)
    {
        if (f.h[i] != fg)
        {
            target = f.h[i];
            break;
        }
    }
    if (!target)
    {
        printf("both Notepads are the foreground window -- cannot measure\n");
        return 2;
    }

    rc_strf(detail, sizeof(detail), "fg pid %lu, target pid %lu, us %lu",
            (unsigned long)pidOf(fg), (unsigned long)pidOf(target),
            (unsigned long)ourPid);
    say("setup: we are background", pidOf(fg) != ourPid, detail);
    say("setup: target is behind", !onTopAtCentre(target), "");

    /* 1. The plain call. */
    SetForegroundWindow(target);
    Sleep(400);
    say("SetForegroundWindow", GetForegroundWindow() == target, "");
    say("  ... and on top at centre", onTopAtCentre(target), "");

    /* 2. BringWindowToTop on its own -- z-order without activation, which is
     *    all clickAt actually needs. */
    SetForegroundWindow(fg);
    Sleep(400);
    BringWindowToTop(target);
    Sleep(400);
    say("BringWindowToTop", onTopAtCentre(target), "z-order only");

    /* 3. SetWindowPos HWND_TOP, no activate. */
    SetForegroundWindow(fg);
    Sleep(400);
    SetWindowPos(target, HWND_TOP, 0, 0, 0, 0,
                 SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE);
    Sleep(400);
    say("SetWindowPos HWND_TOP", onTopAtCentre(target), "no activate");

    /* 4. The foreground lock, turned off first. SPI_SETFOREGROUNDLOCKTIMEOUT
     *    is 0x2001; the constant is missing from this SDK. */
    SetForegroundWindow(fg);
    Sleep(400);
    {
        int okSpi = SystemParametersInfoA(0x2001, 0, (PVOID)0, 0);
        SetForegroundWindow(target);
        Sleep(400);
        rc_strf(detail, sizeof(detail), "SPI returned %d", okSpi);
        say("after LOCKTIMEOUT=0", GetForegroundWindow() == target, detail);
    }

    /* 5. AttachThreadInput to the foreground thread, the documented 9x
     *    workaround. */
    SetForegroundWindow(fg);
    Sleep(400);
    {
        DWORD ourTid = GetCurrentThreadId();
        DWORD fgTid = GetWindowThreadProcessId(GetForegroundWindow(), NULL);
        BOOL att = AttachThreadInput(ourTid, fgTid, TRUE);
        SetForegroundWindow(target);
        Sleep(400);
        rc_strf(detail, sizeof(detail), "attach %s", att ? "ok" : "failed");
        say("with AttachThreadInput", GetForegroundWindow() == target, detail);
        say("  ... and on top at centre", onTopAtCentre(target), "");
        if (att)
            AttachThreadInput(ourTid, fgTid, FALSE);
    }

    /* 6. Does a click land where we want after the cheapest route that
     *    worked? This is the whole point: not "is it foreground" but "would
     *    clickAt now agree to click it". */
    SetForegroundWindow(fg);
    Sleep(400);
    BringWindowToTop(target);
    SetWindowPos(target, HWND_TOP, 0, 0, 0, 0,
                 SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE);
    Sleep(600);
    say("z-order only, then click-check", onTopAtCentre(target),
        "BringWindowToTop + SetWindowPos");

    /* And the route the fix will actually take. The first run had this line
     * testing the two calls that had just been shown not to work, which made
     * it read as "the fix cannot work" when it measured the opposite. */
    SetForegroundWindow(fg);
    Sleep(400);
    SetForegroundWindow(target);
    Sleep(600);
    say("activate, then click-check", onTopAtCentre(target),
        "SetForegroundWindow -- what click should do");

    /* And now the shipped code, not the raw API. This is the line that says
     * whether BACKLOG 16 is actually closed: clickWindow used to refuse a
     * covered window outright, and should now raise it and go ahead. */
    SetForegroundWindow(fg);
    Sleep(600);
    {
        Input in;
        InputResult r;
        int wasBehind = !onTopAtCentre(target);
        r = in.clickWindow(target);
        rc_strf(detail, sizeof(detail), "behind first %s, result %d%s%s",
                wasBehind ? "yes" : "no", (int)r,
                in.blocker()[0] ? ", on top: " : "",
                in.blocker()[0] ? in.blocker() : "");
        say("Input::clickWindow, covered", wasBehind && r == 0, detail);
        say("  ... target now on top", onTopAtCentre(target), "");
    }

    printf("done\n");
    fflush(stdout);
    return 0;
}
