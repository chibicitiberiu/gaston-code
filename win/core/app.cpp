#include "core/app.h"

/* quit()'s message. WM_APP, not WM_USER: this is our own window class.
 * core/ takes WM_APP + 0x100 upward, clear of net.h's WM_APP + 1..2 and the
 * client's low offsets; it was WM_APP + 1, the same number as WM_RC_FRAME,
 * until tools/check/msgids.py was written to catch exactly that. */
static const UINT WM_APP_QUIT = WM_APP + 0x100;

static const char WINDOW_CLASS[] = "GastonAppWindow";

App::App(HINSTANCE instance)
    : m_instance(instance),
      m_window(this),
      m_exitCode(RUN_FAILED),
      m_quitEarly(false),
      m_ran(false),
      m_shutDown(false)
{
}

App::~App()
{
    /* Here, while App is whole: the window's messages still reach
     * onMessage. Its own destructor would run after App's members. */
    m_window.destroy();
}

int App::run(const char *cmdLine)
{
    if (m_ran)
        return RUN_FAILED;
    m_ran = true;

    if (!m_window.make(m_instance))
        return RUN_FAILED;

    bool up = initialize() && parseArgs(cmdLine) && initializeServices() &&
              startServices();
    int code = m_exitCode;
    if (up)
        code = m_quitEarly ? m_exitCode : main();
    shutdown();
    return code;
}

int App::main()
{
    MSG msg;
    for (;;)
    {
        BOOL got = GetMessageA(&msg, 0, 0, 0);
        if (got == 0)
            return (int)msg.wParam;
        if (got == -1)
        {
            rc_log("app: GetMessage failed, error %lu", GetLastError());
            return RUN_FAILED;
        }
        if (preTranslate(msg))
            continue;
        TranslateMessage(&msg);
        DispatchMessageA(&msg);
    }
}

void App::quit(int exitCode)
{
    if (m_window.valid())
    {
        PostMessageA(m_window.hwnd(), WM_APP_QUIT, (WPARAM)exitCode, 0);
        return;
    }
    m_exitCode = exitCode;
    m_quitEarly = true;
}

HWND App::window() const
{
    return m_window.hwnd();
}

bool App::exitWith(int exitCode)
{
    m_exitCode = exitCode;
    return false;
}

void App::shutdown()
{
    if (m_shutDown)
        return;
    m_shutDown = true;
    stopServices();
    cleanup();
}

bool App::onMessage(UINT msg, WPARAM wp, LPARAM lp, LRESULT *result)
{
    (void)lp;
    *result = 0;
    switch (msg)
    {
    case WM_APP_QUIT:
        PostQuitMessage((int)wp);
        return true;
    case WM_QUERYENDSESSION:
        *result = TRUE;
        return true;
    case WM_ENDSESSION:
        /* Windows may end the process as soon as this returns, so the
         * services stop here and not after the loop. */
        if (wp)
        {
            rc_log("app: Windows is ending the session");
            shutdown();
            PostQuitMessage(0);
        }
        return true;
    }
    return false;
}

bool App::HiddenWindow::make(HINSTANCE instance)
{
    /* A second App in one process (the check exe makes several) finds the
     * class already there, which registerClass accepts. */
    return Window::registerClass(instance, WINDOW_CLASS, 0) &&
           create(0, WINDOW_CLASS, "", WS_POPUP, 0, 0, 0, 0, 0, 0, instance);
}

LRESULT App::HiddenWindow::handle(UINT msg, WPARAM wp, LPARAM lp)
{
    LRESULT result;
    if (m_app->onMessage(msg, wp, lp, &result))
        return result;
    return Window::handle(msg, wp, lp);
}
