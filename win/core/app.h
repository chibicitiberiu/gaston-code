/* The application container: owns the services and runs their lifecycle.
 *
 * One composition root builds every service, hands each its dependencies
 * through its constructor, and registers them with addService() in
 * dependency order. App then brings them up in that order and takes them
 * down in reverse. Nothing looks a service up at runtime: without RTTI a
 * type-keyed registry would be a service locator with casts. See
 * docs/REMOTE.md, "Services and lifecycle".
 *
 * LIFECYCLE. A constructor only stores dependencies and cannot fail.
 * Everything that can -- heap, sockets, threads, DLLs -- happens in init() or
 * start(), which return false to abort startup. Shutdown is two passes,
 * because one stop() cannot express it: signalStop() on every service that
 * initialised, in reverse, and only then join() on each, in reverse. A
 * service whose join times out is LEAKED, not deleted: freeing memory that a
 * live thread still touches is a crash later and somewhere else, and
 * net.cpp's shutdown arrived at the same rule the hard way.
 *
 * WHY A HIDDEN WINDOW. quit() must work from any thread. PostThreadMessage to
 * a thread that has no queue yet fails silently on 9x, and a window also
 * receives WM_ENDSESSION, so a Windows shutdown stops the services cleanly.
 * It is a plain hidden top-level window: HWND_MESSAGE is 2000+.
 */
#ifndef RC_APP_H
#define RC_APP_H

#ifndef __cplusplus
#error "core/app.h is C++"
#endif

#include <windows.h>
#include "core/rc_cpp.h"

class Service
{
public:
    virtual ~Service() {}

    /* For the log. A literal; never freed. */
    virtual const char *name() const = 0;

    /* Acquire resources. False aborts startup; the service must have
     * released whatever it took before returning false. */
    virtual bool init() = 0;

    /* Start threads, begin listening. False aborts startup. A service whose
     * start fails is still signalled and joined, so signalStop() and join()
     * must cope with a start that got halfway. */
    virtual bool start() { return true; }

    /* Ask threads to finish. Never blocks. Idempotent. */
    virtual void signalStop() {}

    /* Wait up to timeoutMs for threads to finish. False if they did not:
     * App then leaks this service rather than delete it under them. */
    virtual bool join(DWORD timeoutMs) { (void)timeoutMs; return true; }
};

class App
{
public:
    enum { MAX_SERVICES = 16 };
    enum { RUN_FAILED = -1 };

    /* Null if the hidden window cannot be made or the heap is out. */
    static App *create(HINSTANCE instance);
    ~App();

    /* Takes ownership. On failure (null service, or full) the service is
     * deleted here and false returned, so a composition root can write
     * `if (!app->addService(new X(...)))` and handle OOM in the same test. */
    bool addService(Service *service);

    /* init all, start all, pump messages until quit(), then shut down.
     * Returns the quit() code, or RUN_FAILED if startup failed or run() was
     * already called. */
    int run();

    /* Safe from any thread, and before run(): the request waits in the
     * window's queue. */
    void quit(int exitCode);

    /* For services that post to the main thread. */
    HWND window() const;

    /* Per-service join budget at shutdown. Default 5000 ms. */
    void setJoinTimeout(DWORD timeoutMs);

private:
    explicit App(HINSTANCE instance);
    App(const App &);
    App &operator=(const App &);

    bool createWindow();
    void shutdown();
    LRESULT handle(UINT msg, WPARAM wp, LPARAM lp);
    static LRESULT CALLBACK windowProc(HWND hwnd, UINT msg, WPARAM wp,
                                       LPARAM lp);

    HINSTANCE m_instance;
    WindowGuard m_window;
    UniqueObj<Service> m_services[MAX_SERVICES];
    bool m_leaked[MAX_SERVICES];
    unsigned m_count;
    unsigned m_initialised;
    DWORD m_joinTimeout;
    bool m_ran;
    bool m_shutDown;
};

#endif
