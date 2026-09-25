#include "core/app.h"
#include "core/log.h"

/* quit()'s message. WM_APP, not WM_USER: this is our own window class.
 * core/ takes WM_APP + 0x100 upward, clear of net.h's WM_APP + 1..2 and the
 * client's low offsets; it was WM_APP + 1, the same number as WM_RC_FRAME,
 * until tools/check/msgids.py was written to catch exactly that. */
static const UINT WM_APP_QUIT = WM_APP + 0x100;

static const char WINDOW_CLASS[] = "GastonAppWindow";
static const DWORD DEFAULT_JOIN_MS = 5000;

App *App::create(HINSTANCE instance)
{
    App *app = new App(instance);
    if (!app)
        return 0;
    if (!app->createWindow())
    {
        delete app;
        return 0;
    }
    return app;
}

App::App(HINSTANCE instance)
    : m_instance(instance),
      m_filterCount(0),
      m_count(0),
      m_initialised(0),
      m_joinTimeout(DEFAULT_JOIN_MS),
      m_ran(false),
      m_shutDown(false)
{
    for (unsigned i = 0; i < MAX_SERVICES; i++)
        m_leaked[i] = false;
    for (unsigned i = 0; i < MAX_FILTERS; i++)
        m_filters[i] = 0;
}

App::~App()
{
    shutdown();
    /* Explicitly and in reverse, rather than leaving it to the array's
     * member destructors, so a leaked service is released from its guard
     * before anything can delete it. */
    for (unsigned i = m_count; i-- > 0;)
    {
        if (m_leaked[i])
        {
            rc_log("app: leaking %s, its threads did not stop",
                   m_services[i]->name());
            m_services[i].release();
            continue;
        }
        m_services[i].reset();
    }
    /* Clear the back-pointer first: DestroyWindow sends messages. The guard
     * also forgets the handle before destroying it, so windowProc sees no
     * window and falls through to DefWindowProc. */
    if (m_window.valid())
        SetWindowLongA(m_window.get(), GWL_USERDATA, 0);
    m_window.reset();
}

bool App::createWindow()
{
    WNDCLASSA wc;
    ZeroMemory(&wc, sizeof wc);
    wc.lpfnWndProc = windowProc;
    wc.hInstance = m_instance;
    wc.lpszClassName = WINDOW_CLASS;
    /* A second App in one process (the check exe makes several) finds the
     * class already there, which is fine. */
    if (!RegisterClassA(&wc) &&
        GetLastError() != ERROR_CLASS_ALREADY_EXISTS)
    {
        rc_log("app: RegisterClass failed, error %lu", GetLastError());
        return false;
    }
    m_window.reset(CreateWindowExA(0, WINDOW_CLASS, "", WS_POPUP, 0, 0, 0, 0,
                                   0, 0, m_instance, this));
    if (!m_window.valid())
    {
        rc_log("app: CreateWindow failed, error %lu", GetLastError());
        return false;
    }
    return true;
}

bool App::addService(Service *service)
{
    /* Owned from the first line, so every refusal below deletes it. */
    UniqueObj<Service> owned(service);
    if (!owned.valid())
    {
        rc_log("app: addService(null), out of memory building a service");
        return false;
    }
    if (m_count >= MAX_SERVICES || m_ran)
    {
        rc_log("app: cannot add %s (%s)", owned->name(),
               m_ran ? "already running" : "full");
        return false;
    }
    m_services[m_count++].reset(owned.release());
    return true;
}

bool App::addMessageFilter(MessageFilter *filter)
{
    if (!filter || m_filterCount >= MAX_FILTERS)
    {
        rc_log("app: cannot add message filter (%s)", filter ? "full" : "null");
        return false;
    }
    m_filters[m_filterCount++] = filter;
    return true;
}

int App::run()
{
    if (m_ran)
        return RUN_FAILED;
    m_ran = true;

    for (unsigned i = 0; i < m_count; i++)
    {
        if (!m_services[i]->init())
        {
            rc_log("app: %s failed to initialise", m_services[i]->name());
            shutdown();
            return RUN_FAILED;
        }
        m_initialised = i + 1;
    }
    for (unsigned i = 0; i < m_count; i++)
    {
        if (!m_services[i]->start())
        {
            rc_log("app: %s failed to start", m_services[i]->name());
            shutdown();
            return RUN_FAILED;
        }
    }

    MSG msg;
    int code = 0;
    for (;;)
    {
        BOOL got = GetMessageA(&msg, 0, 0, 0);
        if (got == 0)
        {
            code = (int)msg.wParam;
            break;
        }
        if (got == -1)
        {
            rc_log("app: GetMessage failed, error %lu", GetLastError());
            code = RUN_FAILED;
            break;
        }
        bool consumed = false;
        for (unsigned i = 0; i < m_filterCount && !consumed; i++)
            consumed = m_filters[i]->filter(msg);
        if (consumed)
            continue;
        TranslateMessage(&msg);
        DispatchMessageA(&msg);
    }
    shutdown();
    return code;
}

void App::quit(int exitCode)
{
    PostMessageA(m_window.get(), WM_APP_QUIT, (WPARAM)exitCode, 0);
}

HWND App::window() const
{
    return m_window.get();
}

void App::setJoinTimeout(DWORD timeoutMs)
{
    m_joinTimeout = timeoutMs;
}

void App::shutdown()
{
    if (m_shutDown)
        return;
    m_shutDown = true;

    /* Signal everything before waiting on anything, so every service's
     * threads wind down in parallel and a slow one does not hold the rest. */
    for (unsigned i = m_initialised; i-- > 0;)
        m_services[i]->signalStop();
    for (unsigned i = m_initialised; i-- > 0;)
    {
        if (!m_services[i]->join(m_joinTimeout))
        {
            rc_log("app: %s did not stop within %lu ms",
                   m_services[i]->name(), m_joinTimeout);
            m_leaked[i] = true;
        }
    }
}

LRESULT App::handle(UINT msg, WPARAM wp, LPARAM lp)
{
    switch (msg)
    {
    case WM_APP_QUIT:
        PostQuitMessage((int)wp);
        return 0;
    case WM_QUERYENDSESSION:
        return TRUE;
    case WM_ENDSESSION:
        /* Windows may end the process as soon as this returns, so the
         * services stop here and not after the loop. */
        if (wp)
        {
            rc_log("app: Windows is ending the session");
            shutdown();
            PostQuitMessage(0);
        }
        return 0;
    }
    return DefWindowProcA(m_window.get(), msg, wp, lp);
}

LRESULT CALLBACK App::windowProc(HWND hwnd, UINT msg, WPARAM wp, LPARAM lp)
{
    if (msg == WM_NCCREATE)
    {
        CREATESTRUCTA *cs = (CREATESTRUCTA *)lp;
        SetWindowLongA(hwnd, GWL_USERDATA, (LONG)cs->lpCreateParams);
    }
    App *app = (App *)GetWindowLongA(hwnd, GWL_USERDATA);
    if (!app || !app->m_window.valid())
        return DefWindowProcA(hwnd, msg, wp, lp);
    return app->handle(msg, wp, lp);
}
