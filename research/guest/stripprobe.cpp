/* stripprobe -- the four things the driving indicator guesses at.
 *
 * Not a check. Stage 4b built the indicator on readings of documentation
 * that Wine cannot test, because every one of them is about what the 9x
 * window manager does with a topmost window belonging to somebody else:
 *
 *   1. HTTRANSPARENT. The WM_NCHITTEST walk is documented as same-THREAD
 *      ("the message is sent to the underlying windows in the same
 *      thread"), so across a process boundary it should buy nothing --
 *      neither for WindowFromPoint nor for a real mouse_event. The design
 *      does not rely on it; the strip steps aside instead. If 9x is kinder
 *      than the documentation, most of those handshakes can go.
 *
 *   2. How long after a hide the FRAMEBUFFER under it is right. The
 *      screenshot path needs this number and currently sleeps a guessed
 *      120ms. IsWindowVisible goes false when ShowWindow returns, which is
 *      before the windows underneath have repainted -- fine for
 *      hit-testing, which is z-order, and wrong for BitBlt, which reads
 *      pixels.
 *
 *   3. RegisterHotKey, for the chord the strip offers as its primary
 *      cancel route. MOD_WIN is documented as unsupported on 95/98 and F12
 *      is reserved on some configurations, so Ctrl+Alt+Shift+F12 is a
 *      reading, not a measurement -- and a cancel chord that silently did
 *      not register is the worst outcome this feature has.
 *
 *   4. That a SW_SHOWNOACTIVATE topmost popup never takes the foreground,
 *      including when it is clicked. This one is load-bearing rather than
 *      polite: steps.cpp's keyboardTargetOk fails EVERY typing step with
 *      refused_self while one of our own windows is in front, so a strip
 *      that can be activated breaks the feature it belongs to.
 *
 * Output: STRIPPRB.TXT beside the exe, and the console. One line each, 8.3
 * name because this runs on FAT32.
 */

#include <windows.h>
#include <stdio.h>
#include <string.h>

#include "core/str.h"

static FILE *g_out;

static void say(const char *fmt, ...)
{
    char line[200];
    va_list ap;

    va_start(ap, fmt);
    _vsnprintf(line, sizeof(line), fmt, ap);
    va_end(ap);
    line[sizeof(line) - 1] = '\0';
    printf("%s\n", line);
    if (g_out)
    {
        fprintf(g_out, "%s\n", line);
        fflush(g_out);
    }
}

/* ---- two windows: a topmost strip, and a target under it ------------- */

static const char *const STRIP_CLASS = "StripProbeStrip";
static const char *const TARGET_CLASS = "StripProbeTarget";

static volatile LONG g_transparent = 1;   /* what WM_NCHITTEST answers */
static volatile LONG g_targetClicks;
static volatile LONG g_stripClicks;
static volatile LONG g_stripActivated;

static LRESULT CALLBACK stripProc(HWND h, UINT m, WPARAM w, LPARAM l)
{
    switch (m)
    {
    case WM_NCHITTEST:
        if (g_transparent)
            return HTTRANSPARENT;
        return HTCLIENT;
    case WM_MOUSEACTIVATE:
        return MA_NOACTIVATE;
    case WM_ACTIVATE:
        if (LOWORD(w) != WA_INACTIVE)
            InterlockedIncrement(&g_stripActivated);
        break;
    case WM_LBUTTONDOWN:
        InterlockedIncrement(&g_stripClicks);
        return 0;
    case WM_ERASEBKGND:
    {
        RECT rc;
        GetClientRect(h, &rc);
        FillRect((HDC)w, &rc, (HBRUSH)GetStockObject(BLACK_BRUSH));
        return 1;
    }
    }
    return DefWindowProcA(h, m, w, l);
}

static LRESULT CALLBACK targetProc(HWND h, UINT m, WPARAM w, LPARAM l)
{
    switch (m)
    {
    case WM_LBUTTONDOWN:
        InterlockedIncrement(&g_targetClicks);
        return 0;
    case WM_ERASEBKGND:
    {
        RECT rc;
        GetClientRect(h, &rc);
        FillRect((HDC)w, &rc, (HBRUSH)GetStockObject(WHITE_BRUSH));
        return 1;
    }
    }
    return DefWindowProcA(h, m, w, l);
}

static void registerClasses(HINSTANCE inst)
{
    WNDCLASSA wc;

    memset(&wc, 0, sizeof(wc));
    wc.lpfnWndProc = stripProc;
    wc.hInstance = inst;
    wc.lpszClassName = STRIP_CLASS;
    wc.hCursor = LoadCursorA(NULL, IDC_ARROW);
    RegisterClassA(&wc);

    wc.lpfnWndProc = targetProc;
    wc.lpszClassName = TARGET_CLASS;
    RegisterClassA(&wc);
}

static void pump(DWORD ms)
{
    DWORD t0 = GetTickCount();
    MSG msg;
    for (;;)
    {
        while (PeekMessageA(&msg, NULL, 0, 0, PM_REMOVE))
        {
            TranslateMessage(&msg);
            DispatchMessageA(&msg);
        }
        if (GetTickCount() - t0 >= ms)
            return;
        Sleep(5);
    }
}

/* The strip on its own thread, the way the real one is. */
struct StripArgs
{
    HINSTANCE inst;
    int x, y, w, h;
    volatile HWND hwnd;
    volatile LONG quit;
};

static DWORD WINAPI stripThread(LPVOID p)
{
    StripArgs *a = (StripArgs *)p;
    MSG msg;
    HWND h;

    PeekMessageA(&msg, NULL, WM_USER, WM_USER, PM_NOREMOVE);
    h = CreateWindowExA(WS_EX_TOPMOST | WS_EX_TOOLWINDOW, STRIP_CLASS,
                        "strip", WS_POPUP, a->x, a->y, a->w, a->h,
                        NULL, NULL, a->inst, NULL);
    if (!h)
        return 0;
    ShowWindow(h, SW_SHOWNOACTIVATE);
    a->hwnd = h;
    while (!a->quit && GetMessageA(&msg, NULL, 0, 0) > 0)
    {
        TranslateMessage(&msg);
        DispatchMessageA(&msg);
    }
    DestroyWindow(h);
    a->hwnd = 0;
    return 0;
}

/* ---- 1 and 4: hit-testing and activation ----------------------------- */

static void hitTesting(HINSTANCE inst)
{
    StripArgs a;
    HWND target, under;
    POINT p;
    DWORD tid = 0;
    HANDLE th;
    int mid_x, mid_y;

    target = CreateWindowExA(0, TARGET_CLASS, "target",
                             WS_OVERLAPPEDWINDOW | WS_VISIBLE,
                             40, 40, 320, 200, NULL, NULL, inst, NULL);
    if (!target)
    {
        say("FAIL no target window");
        return;
    }
    SetForegroundWindow(target);
    pump(200);

    /* The strip over the middle of the target, on another thread. */
    mid_x = 140;
    mid_y = 120;
    a.inst = inst;
    a.x = mid_x - 60;
    a.y = mid_y - 10;
    a.w = 120;
    a.h = 20;
    a.hwnd = 0;
    a.quit = 0;
    th = CreateThread(NULL, 0, stripThread, &a, 0, &tid);
    if (!th)
    {
        say("FAIL no strip thread");
        DestroyWindow(target);
        return;
    }
    while (!a.hwnd)
        pump(20);
    pump(200);

    p.x = mid_x;
    p.y = mid_y;
    ClientToScreen(target, &p);
    p.x = a.x + 60;
    p.y = a.y + 10;

    /* 1a. WindowFromPoint with HTTRANSPARENT, strip on ANOTHER THREAD of
     * this same process -- the case the documentation covers. */
    g_transparent = 1;
    under = WindowFromPoint(p);
    say("httransparent-otherthread-windowfrompoint %s",
        under == a.hwnd ? "RETURNS THE STRIP (not honoured)"
                        : (under == target ? "sees through (honoured)"
                                           : "third window"));

    /* 1b. And with it off, which is the control: the strip must be what is
     * under the point, or the geometry is wrong and 1a proves nothing. */
    g_transparent = 0;
    under = WindowFromPoint(p);
    say("httransparent-off-control %s",
        under == a.hwnd ? "strip (geometry ok)" : "WRONG GEOMETRY");

    /* 2. A real click through it. */
    g_transparent = 1;
    g_targetClicks = 0;
    g_stripClicks = 0;
    SetCursorPos(p.x, p.y);
    pump(50);
    mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0);
    mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0);
    pump(300);
    say("httransparent-real-click target=%ld strip=%ld",
        (long)g_targetClicks, (long)g_stripClicks);

    /* 4. Did the strip ever become foreground? It is topmost, it was
     * clicked, and it answered MA_NOACTIVATE. */
    say("noactivate-strip-foreground %s, activations=%ld",
        GetForegroundWindow() == a.hwnd ? "IT IS FOREGROUND (bad)"
                                        : "not foreground (good)",
        (long)g_stripActivated);

    /* 3. The hide-to-repaint gap. The strip is black and the target is
     * white, so one pixel says whether the hide has reached the
     * framebuffer. Posted, not sent: a cross-thread ShowWindow is what the
     * real one must not do. */
    {
        HDC screen = GetDC(NULL);
        DWORD t0;
        int vis_ms = -1;
        int pix_ms = -1;
        int i;

        g_transparent = 0;
        if (screen)
        {
            COLORREF before = GetPixel(screen, p.x, p.y);
            t0 = GetTickCount();
            ShowWindowAsync(a.hwnd, SW_HIDE);
            for (i = 0; i < 400; i++)
            {
                if (vis_ms < 0 && !IsWindowVisible(a.hwnd))
                    vis_ms = (int)(GetTickCount() - t0);
                if (pix_ms < 0 && GetPixel(screen, p.x, p.y) != before)
                    pix_ms = (int)(GetTickCount() - t0);
                if (vis_ms >= 0 && pix_ms >= 0)
                    break;
                pump(5);
            }
            say("hide-invisible-after %dms, pixel-correct-after %dms "
                "(before=%06lx)", vis_ms, pix_ms, (unsigned long)before);
            ReleaseDC(NULL, screen);
        }
        else
            say("hide-gap no screen DC");
    }

    a.quit = 1;
    PostMessageA(a.hwnd, WM_CLOSE, 0, 0);
    WaitForSingleObject(th, 3000);
    CloseHandle(th);
    DestroyWindow(target);
    pump(100);
}

/* ---- 3: the chord ---------------------------------------------------- */

static void chord(HINSTANCE inst)
{
    HWND h = CreateWindowExA(0, TARGET_CLASS, "hotkey", WS_POPUP,
                             0, 0, 10, 10, NULL, NULL, inst, NULL);
    if (!h)
    {
        say("FAIL no hotkey window");
        return;
    }
    {
        UINT mods = MOD_CONTROL | MOD_ALT | MOD_SHIFT;
        BOOL f12 = RegisterHotKey(h, 1, mods, VK_F12);
        DWORD e12 = GetLastError();
        BOOL f11 = RegisterHotKey(h, 2, mods, VK_F11);
        DWORD e11 = GetLastError();
        /* The same chord twice: what a SECOND program gets, which is the
         * case where one of the two programs must be told it has no
         * keyboard cancel. */
        BOOL again = RegisterHotKey(h, 3, mods, VK_F12);
        DWORD eAgain = GetLastError();

        say("hotkey-ctrl-alt-shift-f12 %s err=%lu",
            f12 ? "registered" : "REFUSED", (unsigned long)e12);
        say("hotkey-ctrl-alt-shift-f11 %s err=%lu",
            f11 ? "registered" : "REFUSED", (unsigned long)e11);
        say("hotkey-same-chord-twice %s err=%lu",
            again ? "ACCEPTED (no collision reported)" : "refused",
            (unsigned long)eAgain);
        if (f12) UnregisterHotKey(h, 1);
        if (f11) UnregisterHotKey(h, 2);
        if (again) UnregisterHotKey(h, 3);
    }
    DestroyWindow(h);
}

/* ---- timer granularity ----------------------------------------------- */

static void timerGranularity(HINSTANCE inst)
{
    HWND h = CreateWindowExA(0, TARGET_CLASS, "timer", WS_POPUP,
                             0, 0, 10, 10, NULL, NULL, inst, NULL);
    DWORD t0, ticks[6];
    int n = 0;
    MSG msg;

    if (!h)
    {
        say("FAIL no timer window");
        return;
    }
    SetTimer(h, 1, 10, NULL);          /* ask for 10ms and see */
    t0 = GetTickCount();
    while (n < 6 && GetTickCount() - t0 < 2000)
    {
        if (PeekMessageA(&msg, h, WM_TIMER, WM_TIMER, PM_REMOVE))
            ticks[n++] = GetTickCount() - t0;
        else
            Sleep(1);
    }
    KillTimer(h, 1);
    DestroyWindow(h);
    if (n >= 2)
        say("timer-asked-10ms got %lums apart (n=%d)",
            (unsigned long)((ticks[n - 1] - ticks[0]) / (n - 1)), n);
    else
        say("timer-asked-10ms only %d ticks in 2s", n);
}

int main(void)
{
    char path[MAX_PATH];
    char *slash;
    HINSTANCE inst = GetModuleHandleA(NULL);

    GetModuleFileNameA(NULL, path, sizeof(path));
    slash = strrchr(path, '\\');
    if (slash)
        slash[1] = '\0';
    else
        path[0] = '\0';
    rc_strf(path + strlen(path), sizeof(path) - strlen(path), "STRIPPRB.TXT");
    g_out = fopen(path, "w");

    say("stripprobe: what the driving indicator guesses at");
    registerClasses(inst);
    hitTesting(inst);
    chord(inst);
    timerGranularity(inst);
    say("done");
    if (g_out)
        fclose(g_out);
    return 0;
}
