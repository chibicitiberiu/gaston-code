/* The application lifecycle: a fixed sequence of hooks a derived class fills.
 *
 *   run()  initialize -> parseArgs -> initializeServices -> startServices
 *          -> main (the message loop) -> stopServices -> cleanup
 *
 * The derived class holds its services as UniqueObj<> members and creates
 * them itself in initializeServices(), in dependency order, handing each its
 * dependencies through its constructor -- by then the config is loaded and
 * the paths are known, so they are real values, not references to be filled
 * in later. stopServices() and cleanup() take them down by hand, in reverse.
 * For a dozen services, one readable function per phase beats a registry.
 * See docs/REMOTE.md, "Services and lifecycle".
 *
 * FAILURE. If any step up to startServices() returns false, main() is
 * skipped and stopServices() and cleanup() still run, so both must cope with
 * a start that got partway: a UniqueObj that is still empty, a service whose
 * init() failed. exitWith() lets initialize() stop early with a chosen code
 * (the single-instance check exits 0, not as a failure).
 *
 * WHY A HIDDEN WINDOW. quit() must work from any thread. PostThreadMessage to
 * a thread that has no queue yet fails silently on 9x, and a window also
 * receives WM_ENDSESSION, on which App signals the services and gets out of
 * the way before Windows ends the process. It is a plain hidden top-level
 * window: HWND_MESSAGE is 2000+, and EnumWindows finding it (appcheck) is
 * what says it really is in the set USER broadcasts the session messages to.
 *
 * TWO TEARDOWN PATHS, AND THEY ARE NOT THE SAME ONE. Both run stopServices()
 * and cleanup(); what differs is whether a join is allowed to WAIT.
 *
 *   the ordinary path (quit(), the loop ends, run() returns) joins properly.
 *   The process goes on living afterwards -- the updater starts a replacement
 *   and the shell may still be holding a window -- so an orphan thread is a
 *   real bug and waiting for it is right.
 *
 *   the session-ending path (WM_ENDSESSION) MUST NOT WAIT AT ALL. Windows
 *   does not continue its shutdown until this window procedure returns, and
 *   on 9x a handler that does not come back promptly is the "not responding /
 *   End Task" dialog -- with Explorer already gone there is no desktop to put
 *   that dialog on, so the machine simply stops. Worse, the thread Windows is
 *   waiting for can be one of OURS: gremote's REBOOT runs ExitWindowsEx on
 *   the Connection reader thread, ExitWindowsEx does not return, and the
 *   broadcast it drives SendMessages this very window -- so a join here waits
 *   for the accept thread, which waits for the reader, which waits for this
 *   handler. A three-way deadlock broken only by a timeout firing, which is
 *   seconds of a frozen machine every shutdown and a wedge if any one wait in
 *   the chain is unbounded. Observed on real hardware 2026-10-01
 *   (docs/REMOTE.md, P200 checklist).
 *
 * sessionEnding() is the one bit that separates them, and joinOrLeak() is the
 * one place that reads it: on a session end it ABANDONS each service
 * unjoined rather than waiting, which is net.cpp's leak rule with the wait
 * taken out -- a service whose threads may still be live is leaked, never
 * freed. runApp() then leaks the App itself for the same reason, since those
 * threads hold pointers into its members.
 *
 * WM_QUERYENDSESSION DELIBERATELY DOES NOTHING BUT AGREE. A shutdown another
 * program vetoes is cancelled, and there is no un-signalling a service, so
 * nothing is torn down until Windows has committed with WM_ENDSESSION.
 */
#ifndef RC_APP_H
#define RC_APP_H

#ifndef __cplusplus
#error "core/app.h is C++"
#endif

#include <windows.h>
#include "core/log.h"
#include "core/rc_cpp.h"
#include "core/window.h"

/* The shape every service shares. Nothing collects them; the derived App
 * calls these from its lifecycle hooks.
 *
 * A constructor only stores dependencies and cannot fail. Everything that
 * can -- heap, sockets, threads, DLLs -- happens in init() or start().
 * Shutdown is two passes, because one stop() cannot express it:
 * signalStop() on every service, then join() on each, so every service's
 * threads wind down in parallel. signalStop() and join() must be safe on a
 * service whose init() or start() failed or never ran. */
class Service
{
public:
    virtual ~Service() {}

    /* For the log. A literal; never freed. */
    virtual const char *name() const = 0;

    /* Acquire resources. False aborts startup; the service must have
     * released whatever it took before returning false. */
    virtual bool init() = 0;

    /* Start threads, begin listening. False aborts startup. */
    virtual bool start() { return true; }

    /* Ask threads to finish. Never blocks. Idempotent. */
    virtual void signalStop() {}

    /* Wait up to timeoutMs for threads to finish. False if they did not:
     * see joinOrLeak(). */
    virtual bool join(DWORD timeoutMs) { (void)timeoutMs; return true; }
};

/* Create-and-init in one step, for initializeServices(): takes ownership of
 * `made` (null when the heap is out) and runs its init(), logging a failure
 * by name. */
template <class T>
bool initService(UniqueObj<T> &slot, T *made)
{
    slot.reset(made);
    if (!slot.valid())
    {
        rc_log("app: out of memory creating a service");
        return false;
    }
    if (!slot->init())
    {
        rc_log("app: %s failed to initialise", slot->name());
        return false;
    }
    return true;
}

class App
{
public:
    enum { RUN_FAILED = -1 };

    virtual ~App();

    /* The whole lifecycle. Returns main()'s exit code, the code given to
     * exitWith(), or RUN_FAILED. Only once per object. */
    int run(const char *cmdLine);

    /* Safe from any thread once run() has begun; before that, the request
     * is remembered and main() is skipped. */
    void quit(int exitCode);

    /* For services that post to the main thread. Null before run(). */
    HWND window() const;

    /* True from the moment WM_ENDSESSION said the session really is ending.
     * joinOrLeak() reads it and abandons instead of waiting; runApp() reads
     * it and leaks the App. Set on the main thread, inside the handler,
     * before anything is torn down. */
    bool sessionEnding() const { return m_sessionEnding; }

protected:
    explicit App(HINSTANCE instance);

    HINSTANCE instance() const { return m_instance; }

    /* The hooks, in the order run() calls them. */
    virtual bool initialize() { return true; }
    virtual bool parseArgs(const char *cmdLine) { (void)cmdLine; return true; }
    virtual bool initializeServices() = 0;
    virtual bool startServices() { return true; }
    /* Every message before it is translated and dispatched; true consumes
     * it. Accelerators, IsDialogMessage, keys a window handles for its
     * children. */
    virtual bool preTranslate(MSG &msg) { (void)msg; return false; }
    /* The message loop, until quit(). Returns the exit code. */
    virtual int main();
    virtual void stopServices() {}
    virtual void cleanup() {}

    /* From initialize() or parseArgs(): stop without it being a failure.
     * Returns false, for `return exitWith(0);`. */
    bool exitWith(int exitCode);

private:
    App(const App &);
    App &operator=(const App &);

    class HiddenWindow : public Window
    {
    public:
        explicit HiddenWindow(App *app) : m_app(app) {}
        bool make(HINSTANCE instance);

    protected:
        LRESULT handle(UINT msg, WPARAM wp, LPARAM lp);

    private:
        App *m_app;
    };
    /* C++98 gives a nested class no access to its enclosing class's private
     * members. */
    friend class HiddenWindow;

    int runLifecycle(const char *cmdLine);
    bool onMessage(UINT msg, WPARAM wp, LPARAM lp, LRESULT *result);
    void shutdown();

    HINSTANCE m_instance;
    HiddenWindow m_window;
    int m_exitCode;
    bool m_quitEarly;
    bool m_ran;
    bool m_shutDown;
    bool m_sessionEnding;
};

/* For stopServices(): join, and if the threads did not stop, LEAK the
 * service rather than let cleanup() delete it under them -- freeing memory
 * a live thread still touches is a crash later and somewhere else, the rule
 * net.cpp's shutdown arrived at the hard way.
 *
 * `app` is not decoration. On a session end this does not join AT ALL: it
 * abandons the service where it stands, because Windows is holding its whole
 * shutdown on this window procedure returning and a thread that will not wake
 * must not be able to hold up the machine (see the header comment). That is
 * why the App is a parameter rather than the timeout being a literal -- the
 * old two-argument form is gone so no call site can quietly keep waiting.
 *
 * The service's threads are then still running with nothing joined, which is
 * exactly the state the leak rule is for: nothing is freed, so nothing they
 * touch goes away under them, and the process is about to end anyway. */
template <class T>
void joinOrLeak(UniqueObj<T> &slot, App &app, DWORD timeoutMs)
{
    if (!slot.valid())
        return;
    if (app.sessionEnding())
    {
        rc_log("app: the session is ending; abandoning %s unjoined",
               slot->name());
        slot.release();
        return;
    }
    if (!slot->join(timeoutMs))
    {
        rc_log("app: leaking %s, its threads did not stop", slot->name());
        slot.release();
    }
}

/* What a WinMain does, once rather than once per exe: run the app, and if
 * Windows ended the session, LEAK it instead of destroying it.
 *
 * The leak is the same rule one level up. On that path the services were
 * abandoned unjoined, so their threads may still be inside the App's own
 * members -- gremote's threads hold a ServerConfig, an ExecConfig and a
 * Processes that all live in GremoteApp, and running ~GremoteApp under them
 * would FreeLibrary the Toolhelp DLL a lane worker is calling into. A fault
 * there is a GPF dialog raised on a desktop that no longer exists, which is
 * the hang this whole path exists to avoid. ExitProcess takes the threads
 * and the memory a moment later regardless.
 *
 * Returns the code already mapped for WinMain: RUN_FAILED becomes 1. */
template <class T>
int runApp(UniqueObj<T> &app, const char *cmdLine)
{
    if (!app.valid())
        return 1;
    int code = app->run(cmdLine);
    if (app->sessionEnding())
        app.release();
    return code == App::RUN_FAILED ? 1 : code;
}

#endif
