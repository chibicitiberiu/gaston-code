#include "automation/indicator.h"

#include "core/log.h"
#include "core/rc_cpp.h"
#include "core/window.h"

#include <string.h>

namespace {

const char *const IND_CLASS = "GastonDriveIndicator";

/* Padding around the text, and the gap between sentence and button. */
enum { PAD_X = 8, PAD_Y = 4, GAP = 10, BTN_PAD = 10, EDGE = 6 };

enum { IDT_IND = 1 };

/* Drain this thread's queue so a wait here does not stall WinEvent
 * delivery. Input::pumpOwnQueue exists for the same reason and records the
 * measurement behind it: out-of-context hook delivery stopped past ~220
 * unpumped events. WM_QUIT is put back rather than eaten. */
void pumpHere(void)
{
    MSG msg;
    int n = 0;
    while (n++ < 32 && PeekMessageA(&msg, NULL, 0, 0, PM_REMOVE))
    {
        if (msg.message == WM_QUIT)
        {
            PostQuitMessage((int)msg.wParam);
            return;
        }
        TranslateMessage(&msg);
        DispatchMessageA(&msg);
    }
}

}  /* namespace */

/* ------------------------------------------------------------------ */

class DriveIndicator::Strip : public Window
{
public:
    Strip(DriveIndicator &owner)
        : m_owner(owner), m_font(0), m_shownAt(0), m_textW(0), m_btnW(0),
          m_h(0), m_atTop(false), m_visible(false), m_hotkey(false),
          m_ticking(false)
    {
        SetRectEmpty(&m_btn);
    }

    ~Strip()
    {
        /* From the derived destructor, per core/window.h: ~Window runs
         * WM_DESTROY after the derived part is gone. This is also the only
         * thread that may destroy it -- DestroyWindow across threads fails
         * and nothing checks the return. */
        if (m_hotkey && hwnd())
            UnregisterHotKey(hwnd(), 1);
        destroy();
    }

    bool make(HINSTANCE inst);
    void tick();
    void wake() { clock(true); tick(); }

protected:
    LRESULT handle(UINT msg, WPARAM wp, LPARAM lp);

private:
    void measure();
    void place();
    void paint(HDC dc);
    void show(bool on);
    void clock(bool on);
    const char *line() const;

    DriveIndicator &m_owner;
    HFONT   m_font;         /* a stock object: nothing to delete */
    DWORD   m_shownAt;
    int     m_textW, m_btnW, m_h;
    RECT    m_btn;
    bool    m_atTop;
    bool    m_visible;
    bool    m_hotkey;
    bool    m_ticking;
};

const char *DriveIndicator::Strip::line() const
{
    if (m_owner.m_stopped)
        return "Stopping...";
    return m_owner.m_who;
}

void DriveIndicator::Strip::measure()
{
    WindowDC dc(hwnd());
    Selected pick(dc.get(), m_font);
    SIZE s;
    TEXTMETRICA tm;

    m_textW = 0;
    if (GetTextExtentPoint32A(dc.get(), m_owner.m_who,
                              (int)strlen(m_owner.m_who), &s))
        m_textW = s.cx;
    /* The chord is on the button, so the button is as wide as its words. */
    m_btnW = 0;
    if (GetTextExtentPoint32A(dc.get(), m_owner.m_chordText,
                              (int)strlen(m_owner.m_chordText), &s))
        m_btnW = s.cx + BTN_PAD * 2;
    m_h = 20;
    if (GetTextMetricsA(dc.get(), &tm))
        m_h = tm.tmHeight + PAD_Y * 2 + 4;
}

void DriveIndicator::Strip::place()
{
    RECT work;
    int w = PAD_X + m_textW + GAP + m_btnW + PAD_X;
    int x, y;

    if (!SystemParametersInfoA(SPI_GETWORKAREA, 0, &work, 0))
    {
        work.left = work.top = 0;
        work.right = GetSystemMetrics(SM_CXSCREEN);
        work.bottom = GetSystemMetrics(SM_CYSCREEN);
    }
    if (w > work.right - work.left - EDGE * 2)
        w = work.right - work.left - EDGE * 2;

    x = work.right - w - EDGE;
    y = m_atTop ? work.top + EDGE : work.bottom - m_h - EDGE;
    SetWindowPos(hwnd(), HWND_TOPMOST, x, y, w, m_h,
                 SWP_NOACTIVATE | SWP_NOOWNERZORDER);

    m_btn.right = w - PAD_X;
    m_btn.left = m_btn.right - m_btnW;
    m_btn.top = PAD_Y - 2;
    m_btn.bottom = m_h - PAD_Y + 2;
}

bool DriveIndicator::Strip::make(HINSTANCE inst)
{
    WNDCLASSA wc;

    memset(&wc, 0, sizeof(wc));
    wc.style = CS_HREDRAW | CS_VREDRAW;
    wc.hCursor = LoadCursorA(NULL, IDC_ARROW);
    wc.hbrBackground = (HBRUSH)(COLOR_BTNFACE + 1);
    if (!registerClass(inst, IND_CLASS, &wc))
        return false;
    /* No WS_VISIBLE: it appears when the drive has lasted long enough to
     * be worth saying so. WS_EX_TOOLWINDOW keeps it out of Alt+Tab. */
    if (!create(WS_EX_TOPMOST | WS_EX_TOOLWINDOW, IND_CLASS, "Gaston",
                WS_POPUP, 0, 0, 10, 10, 0, 0, inst))
        return false;

    m_font = (HFONT)GetStockObject(DEFAULT_GUI_FONT);
    measure();
    place();

    /* MOD_ALT is 1 in winuser.h and KEYMOD_ALT is 4 in input.h; these are
     * the winuser ones. MOD_WIN does not exist on 9x. */
    m_hotkey = RegisterHotKey(hwnd(), 1, MOD_CONTROL | MOD_ALT | MOD_SHIFT,
                              m_owner.m_chordVk) != 0;
    if (!m_hotkey)
        rc_log("indicator: %s is not available (another program holds it?); "
               "the Stop button is the only way to cancel",
               m_owner.m_chordText);
    return true;
}

/* The 100ms clock runs only while there is something for it to decide. A
 * timer ticking all day on an idle P200 is exactly the kind of cost this
 * project does not spend. */
void DriveIndicator::Strip::clock(bool on)
{
    if (on == m_ticking)
        return;
    m_ticking = on;
    if (on)
        SetTimer(hwnd(), IDT_IND, TICK_MS, NULL);
    else
        KillTimer(hwnd(), IDT_IND);
}

void DriveIndicator::Strip::show(bool on)
{
    if (on == m_visible)
        return;
    m_visible = on;
    if (on)
    {
        measure();
        place();
        m_shownAt = GetTickCount();
        ShowWindow(hwnd(), SW_SHOWNOACTIVATE);
    }
    else
        ShowWindow(hwnd(), SW_HIDE);
}

void DriveIndicator::Strip::tick()
{
    DWORD now = GetTickCount();
    bool want;

    if (m_owner.m_suppressed)
        want = false;
    else if (m_owner.m_stopped)
        want = (now - m_owner.m_stopTick) < STOP_SAYS_MS;
    else if (m_owner.m_driving)
        want = (now - m_owner.m_driveStart) >= SHOW_DELAY_MS;
    else if (m_visible)
        want = (now - m_owner.m_lastDrive) < DWELL_MS
            || (now - m_shownAt) < MIN_VISIBLE_MS;
    else
        want = false;

    show(want);
    /* Nothing left to decide once it is down and no list is running. */
    clock(want || m_owner.m_driving != 0);
}

LRESULT DriveIndicator::Strip::handle(UINT msg, WPARAM wp, LPARAM lp)
{
    switch (msg)
    {
    case WM_MOUSEACTIVATE:
        /* Never. keyboardTargetOk refuses every typing step while one of
         * our windows is in front. */
        return MA_NOACTIVATE;

    /* NO HTTRANSPARENT HERE, and that is a measurement rather than an
     * omission. It would have let WindowFromPoint and a real click see
     * through the strip to whatever it covers, which is the cheap version
     * of the step-aside the step runner does instead. 98 SE does not
     * honour it: WindowFromPoint returns this window through a
     * WM_NCHITTEST answering HTTRANSPARENT, even with the strip on another
     * thread of the same process, which is the case the documentation says
     * is covered (docs/measurements/stripprobe-vm-20261014.txt). Code that
     * does nothing on the only platform that runs it is worse than no
     * code: the next person reads it as a mechanism that works. */

    case WM_ERASEBKGND:
    {
        RECT rc;
        GetClientRect(hwnd(), &rc);
        FillRect((HDC)wp, &rc, (HBRUSH)(COLOR_BTNFACE + 1));
        return 1;
    }

    case WM_PAINT:
    {
        PAINTSTRUCT ps;
        HDC dc = BeginPaint(hwnd(), &ps);
        if (dc)
            paint(dc);
        EndPaint(hwnd(), &ps);
        return 0;
    }

    case WM_LBUTTONDOWN:
    {
        POINT p;
        p.x = (short)LOWORD(lp);
        p.y = (short)HIWORD(lp);
        /* On the DOWN: the agent is moving the pointer between a real
         * button's down and up, so BN_CLICKED would miss. */
        if (PtInRect(&m_btn, p))
            m_owner.stopPressed();
        return 0;
    }

    case WM_RBUTTONUP:
        /* The only way to move it. Not a drag: SetCapture would hand this
         * thread the mouse half of the input queue for as long as the
         * button is held, which is the TrackPopupMenu hazard. */
        m_atTop = !m_atTop;
        place();
        InvalidateRect(hwnd(), NULL, TRUE);
        return 0;

    case WM_HOTKEY:
        m_owner.stopPressed();
        return 0;

    case WM_DISPLAYCHANGE:
        measure();
        place();
        return 0;

    case WM_TIMER:
        if (wp == IDT_IND)
            tick();
        return 0;

    case WM_DESTROY:
        /* DestroyWindow does not post WM_QUIT, so without this the owning
         * thread sits in GetMessage after its own window is gone and the
         * join in ~DriveIndicator times out into the leak path. */
        PostQuitMessage(0);
        return 0;

    case RC_WM_IND_WAKE:
        wake();
        return 0;
    }
    return Window::handle(msg, wp, lp);
}

void DriveIndicator::Strip::paint(HDC dc)
{
    RECT rc, text;
    Selected pick(dc, m_font);

    GetClientRect(hwnd(), &rc);
    DrawEdge(dc, &rc, EDGE_RAISED, BF_RECT);

    text = rc;
    text.left += PAD_X;
    text.right = m_btn.left - GAP;
    SetBkMode(dc, TRANSPARENT);
    SetTextColor(dc, GetSysColor(COLOR_BTNTEXT));
    DrawTextA(dc, line(), -1, &text,
              DT_VCENTER | DT_SINGLELINE | DT_NOPREFIX | DT_END_ELLIPSIS);

    text = m_btn;
    DrawEdge(dc, &text, EDGE_RAISED, BF_RECT);
    DrawTextA(dc, m_owner.m_chordText, -1, &text,
              DT_CENTER | DT_VCENTER | DT_SINGLELINE | DT_NOPREFIX);
}

/* ------------------------------------------------------------------ */

void DriveIndicator::Body::run()
{
    m_owner.threadBody();
}

DriveIndicator::DriveIndicator(StopSink *sink, const char *who, UINT chordVk,
                               const char *chordText)
    : m_body(*this), m_sink(sink), m_who(who), m_chordText(chordText),
      m_chordVk(chordVk), m_hwnd(0), m_cancel(0), m_driving(0),
      m_suppressed(0), m_stopped(0), m_failed(0), m_quit(0),
      m_driveStart(0), m_lastDrive(0), m_stopTick(0)
{
}

DriveIndicator::~DriveIndicator()
{
    HWND h;

    /* WM_CLOSE to the window, never PostThreadMessage: a thread message to
     * a thread that has not pumped yet is LOST, and the window between
     * Thread::start and CreateWindowEx is exactly that thread. The flag
     * closes the other end of that race -- a thread that publishes its
     * window after this read sees the flag and closes itself. */
    InterlockedExchange((LONG *)&m_quit, 1);
    h = m_hwnd;
    if (h)
        PostMessageA(h, WM_CLOSE, 0, 0);
    if (m_thread.started() && !m_thread.join(2000))
    {
        rc_log("indicator: its thread did not stop; leaking it");
        m_thread.release();
    }
}

void DriveIndicator::threadBody()
{
    MSG msg;

    /* Force the queue into existence before anything is told this thread
     * exists -- uiservice.cpp:46-51 records what happens otherwise. */
    PeekMessageA(&msg, NULL, WM_USER, WM_USER, PM_NOREMOVE);

    {
        Strip strip(*this);
        if (!strip.make(GetModuleHandleA(NULL)))
        {
            /* Latched: a box with no window station answers this way on
             * every drive otherwise. The drive carries on without it,
             * which is the project's degrade (uiservice.cpp:62-68) -- said
             * once, in the log, because a safety affordance that is not
             * there should be visible somewhere. */
            InterlockedExchange((LONG *)&m_failed, 1);
            rc_log("indicator: no window, so nothing will say the desktop "
                   "is being driven");
            return;
        }
        m_hwnd = strip.hwnd();
        if (m_quit)
            PostMessageA(strip.hwnd(), WM_CLOSE, 0, 0);
        rc_log("indicator: up, cancel with %s", m_chordText);
        /* THE FIRST DRIVE IS THE ONE THAT STARTS THIS THREAD, and it has
         * already called driveStarting -- which posted its wake to an
         * m_hwnd that was still null, because this window did not exist
         * yet. Without this the clock never starts on that first run and
         * the strip stays hidden through the whole of it: seen on the
         * guest, a step list that drove for four seconds with the window
         * created, the hotkey registered and nothing on screen. */
        strip.wake();

        while (GetMessageA(&msg, NULL, 0, 0) > 0)
        {
            TranslateMessage(&msg);
            DispatchMessageA(&msg);
        }
        m_hwnd = 0;
    }
}

bool DriveIndicator::ensureThread()
{
    if (m_failed)
        return false;
    if (m_thread.started())
        return true;
    return m_thread.start(m_body, "indicator", 16384);
}

void DriveIndicator::driveStarting(volatile LONG *cancel)
{
    HWND h;

    m_cancel = cancel;
    InterlockedExchange((LONG *)&m_stopped, 0);
    m_driveStart = GetTickCount();
    InterlockedExchange((LONG *)&m_driving, 1);
    if (!ensureThread())
        return;
    h = m_hwnd;
    if (h)
        PostMessageA(h, RC_WM_IND_WAKE, 0, 0);
}

void DriveIndicator::driveEnded()
{
    HWND h = m_hwnd;

    m_lastDrive = GetTickCount();
    InterlockedExchange((LONG *)&m_driving, 0);
    m_cancel = 0;
    if (h)
        PostMessageA(h, RC_WM_IND_WAKE, 0, 0);
}

void DriveIndicator::stopPressed()
{
    volatile LONG *cancel = m_cancel;

    rc_log("indicator: Stop pressed at the keyboard");
    if (cancel)
        InterlockedExchange((LONG *)cancel, 1);
    m_stopTick = GetTickCount();
    InterlockedExchange((LONG *)&m_stopped, 1);
    if (m_hwnd)
        InvalidateRect(m_hwnd, NULL, TRUE);
    if (m_sink)
        m_sink->onStop();
}

bool DriveIndicator::covers(int x, int y)
{
    RECT r;
    HWND h = m_hwnd;

    if (!h || !IsWindowVisible(h) || !GetWindowRect(h, &r))
        return false;
    return x >= r.left && x < r.right && y >= r.top && y < r.bottom;
}

bool DriveIndicator::coversRect(const RECT &r)
{
    RECT mine, hit;
    HWND h = m_hwnd;

    if (!h || !IsWindowVisible(h) || !GetWindowRect(h, &mine))
        return false;
    return IntersectRect(&hit, &mine, &r) != 0;
}

bool DriveIndicator::pumpWait(bool wantVisible, DWORD budgetMs)
{
    DWORD t0 = GetTickCount();
    HWND h = m_hwnd;

    if (!h)
        return !wantVisible;
    for (;;)
    {
        if ((IsWindowVisible(h) != 0) == wantVisible)
            return true;
        if (GetTickCount() - t0 >= budgetMs)
            return false;
        pumpHere();
        Sleep(5);
    }
}

bool DriveIndicator::stepAside(DWORD budgetMs, bool settle)
{
    HWND h = m_hwnd;
    RECT was;

    if (!h || !IsWindowVisible(h))
        return true;
    GetWindowRect(h, &was);
    InterlockedExchange((LONG *)&m_suppressed, 1);
    PostMessageA(h, RC_WM_IND_WAKE, 0, 0);
    if (!pumpWait(false, budgetMs))
    {
        /* Win16Mutex is the one thing that can hold this thread, and it is
         * also the state in which clicking blind is least predictable. */
        InterlockedExchange((LONG *)&m_suppressed, 0);
        rc_log("indicator: it did not get out of the way in %lums",
               (unsigned long)budgetMs);
        return false;
    }
    if (settle)
    {
        /* IsWindowVisible goes false when ShowWindow returns, which is
         * before the windows underneath have repainted -- fine for
         * hit-testing, which is z-order, and wrong for BitBlt, which reads
         * the framebuffer. Not RDW_UPDATENOW: that SendMessages WM_PAINT
         * across processes and wedges behind a hung app. */
        RedrawWindow(NULL, &was, NULL,
                     RDW_INVALIDATE | RDW_ERASE | RDW_ALLCHILDREN);
        Sleep(SETTLE_MS);
    }
    return true;
}

void DriveIndicator::resume()
{
    HWND h = m_hwnd;

    InterlockedExchange((LONG *)&m_suppressed, 0);
    if (h)
        PostMessageA(h, RC_WM_IND_WAKE, 0, 0);
}
