#include "core/app.h"

#include <new>
#include "core/heap.h"

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
      m_shutDown(false),
      m_sessionEnding(false)
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
    /* operator new throws (core/heap.cpp); the main thread's boundary is
     * here. The other threads catch at their own entry points. */
    try
    {
        return runLifecycle(cmdLine);
    }
    catch (std::bad_alloc &)
    {
        rc_out_of_memory("the main thread");
    }
    return RUN_FAILED;
}

int App::runLifecycle(const char *cmdLine)
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
    /* The boundary between the two halves of the teardown, and only on the
     * path where it is evidence. Reading a log back afterwards, a fault in a
     * service's signalStop and a fault in cleanup()'s destructors are
     * otherwise the same absence. */
    if (m_sessionEnding)
        rc_log("app: every service is signalled and abandoned; cleaning up");
    cleanup();
}

/* Which kind of session end, from the lParam the handlers are given. On 9x
 * this is the difference between "log off" and "shut down", and gremote is
 * meant to survive one and not the other (docs/REMOTE.md), so a log that
 * does not say which cannot answer whether it behaved. */
static const char *endSessionKind(LPARAM lp)
{
    return (lp & ENDSESSION_LOGOFF) ? "a logoff" : "a shutdown or restart";
}

bool App::onMessage(UINT msg, WPARAM wp, LPARAM lp, LRESULT *result)
{
    *result = 0;
    switch (msg)
    {
    case WM_APP_QUIT:
        PostQuitMessage((int)wp);
        return true;
    case WM_QUERYENDSESSION:
        /* Agree, and change nothing. A shutdown any other program vetoes is
         * cancelled and we would have no way to put a signalled service
         * back -- so the teardown waits for WM_ENDSESSION, which is Windows
         * saying it has committed. The log line is worth its bytes: it is
         * how a shutdown that stalls LATER is told from one that stalled
         * here -- and, now that the previous run's log survives the restart
         * that recovers the machine, it is also how "WM_ENDSESSION never
         * arrived" is told from "the broadcast never reached us at all".
         * USER sends this one first, to every top-level window. */
        rc_log("app: Windows asks whether the session may end (%s); yes",
               endSessionKind(lp));
        *result = TRUE;
        return true;
    case WM_ENDSESSION:
        if (!wp)
        {
            /* Somebody vetoed it. Nothing was torn down, so there is
             * nothing to undo. */
            rc_log("app: the session end was cancelled; carrying on");
            return true;
        }
        /* RETURN FAST, AND DO NOT JOIN ANYTHING HERE.
         *
         * Windows does not carry on with its shutdown until this window
         * procedure returns. On 9x a handler that takes its time is the
         * "not responding / End Task" dialog, and by WM_ENDSESSION Explorer
         * is already gone, so there is no desktop left to show it on -- the
         * machine just sits there with no desktop and no reboot, which is
         * what a real Pentium 200 did on 2026-10-01 (docs/REMOTE.md, P200
         * checklist). The thread Windows is waiting for can also be one of
         * ours: gremote's REBOOT calls ExitWindowsEx on the Connection
         * reader thread, that call never returns, and the broadcast it
         * drives SendMessages THIS window -- so joining here would wait on
         * the accept thread, which waits on the reader, which waits on this
         * handler.
         *
         * So: set the flag FIRST, then run the same stopServices() and
         * cleanup() as the ordinary path. joinOrLeak() reads the flag and
         * abandons each service unjoined instead of waiting for it, and
         * cleanup() still gets to write and close the log -- a hang with no
         * log is this project's worst case, and the lines below are what say
         * afterwards that a session end was seen and the fast path was
         * taken.
         *
         * Nothing is freed that a live thread can touch: abandoning means
         * releasing the UniqueObj, so cleanup()'s reset() finds it empty,
         * and runApp() leaks the App itself for the same reason.
         *
         * FOUR MARKERS, AND THEY ARE THE WHOLE POINT OF THE PATH. The log is
         * read after a forced restart, so every line it does not contain has
         * to mean something definite:
         *
         *   no "entered" line          -- WM_ENDSESSION never arrived (and
         *                                 the WM_QUERYENDSESSION line above
         *                                 says whether the broadcast reached
         *                                 us at all);
         *   "entered" and no more      -- faulted while signalling, with the
         *                                 abandoned-X lines naming how far;
         *   "cleaning up" and no more  -- faulted in cleanup();
         *   "returning ... after N ms" -- the fast path ran to the end, and
         *                                 N says whether it was fast.
         *
         * The last of those is written with rc_log_tail, not rc_log, because
         * cleanup() has closed the log by then: it reopens the file and
         * appends. Without it the exiting banner is the final line on both a
         * clean return and a fault on the way out. */
        m_sessionEnding = true;
        {
            DWORD t0 = GetTickCount();
            rc_log("app: Windows is ending the session (%s); signalling every "
                   "service and leaving this handler WITHOUT joining any of "
                   "them", endSessionKind(lp));
            shutdown();
            rc_log_tail("app: the session-end handler is returning to Windows "
                        "after %lu ms; nothing below this line is ours",
                        (unsigned long)(GetTickCount() - t0));
        }
        PostQuitMessage(0);
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
