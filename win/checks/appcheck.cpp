/* Does App run the lifecycle it documents?
 *
 * Drives the real `win/core/app.cpp` with services that record every call
 * into one trace, and prints `verdict<TAB>case<TAB>trace` rows for
 * tests/python/guest/test_guest_app.py. The trace is the detail column, so a
 * failure shows the order that actually happened.
 *
 * Trace letters: i init, s start, x signalStop, j join, d destructor, each
 * followed by the service's id.
 */
#include <windows.h>
#include <process.h>
#include <stdio.h>
#include <string.h>
#include "core/rc_cpp.h"
#include "core/app.h"

/* POD, so no static constructor (make check fails the build on one). */
static char g_trace[512];

static void trace(char what, char id)
{
    size_t n = strlen(g_trace);
    if (n + 4 < sizeof g_trace)
    {
        if (n)
            g_trace[n++] = ' ';
        g_trace[n++] = what;
        g_trace[n++] = id;
        g_trace[n] = '\0';
    }
}

static void say(const char *name, bool pass)
{
    printf("%s\t%s\t%s\n", pass ? "pass" : "FAIL", name, g_trace);
    g_trace[0] = '\0';
}

enum
{
    FAIL_INIT = 1,
    FAIL_START = 2,
    QUIT_ON_START = 4,
    STUCK_JOIN = 8,
    QUIT_FROM_THREAD = 16,
    END_SESSION_ON_START = 32
};

static unsigned __stdcall quitLater(void *arg)
{
    Sleep(50);
    ((App *)arg)->quit(3);
    return 0;
}

class Recorder : public Service
{
public:
    Recorder(App *app, char id, unsigned flags)
        : m_app(app), m_id(id), m_flags(flags)
    {
        m_name[0] = 'R';
        m_name[1] = id;
        m_name[2] = '\0';
    }

    ~Recorder()
    {
        trace('d', m_id);
    }

    const char *name() const { return m_name; }

    bool init()
    {
        trace('i', m_id);
        return !(m_flags & FAIL_INIT);
    }

    bool start()
    {
        trace('s', m_id);
        if (m_flags & QUIT_ON_START)
            m_app->quit(7);
        if (m_flags & QUIT_FROM_THREAD)
        {
            unsigned tid;
            m_thread.reset((HANDLE)_beginthreadex(0, 0, quitLater, m_app, 0,
                                                  &tid));
        }
        if (m_flags & END_SESSION_ON_START)
            PostMessageA(m_app->window(), WM_ENDSESSION, TRUE, 0);
        return !(m_flags & FAIL_START);
    }

    void signalStop() { trace('x', m_id); }

    bool join(DWORD timeoutMs)
    {
        trace('j', m_id);
        if (m_thread.valid())
            WaitForSingleObject(m_thread.get(), timeoutMs);
        return !(m_flags & STUCK_JOIN);
    }

private:
    App *m_app;
    char m_id;
    unsigned m_flags;
    Handle m_thread;
    char m_name[3];
};

/* A message only the filters below care about. Its own block, clear of
 * core/'s WM_APP + 0x100 (tools/check/msgids.py keeps it that way). */
#define WM_CHECK_FILTERED (WM_APP + 0x200)

class Filter : public MessageFilter
{
public:
    Filter(char id, bool consume) : m_id(id), m_consume(consume) {}

    bool filter(MSG &msg)
    {
        if (msg.message != WM_CHECK_FILTERED)
            return false;
        trace('f', m_id);
        return m_consume;
    }

private:
    char m_id;
    bool m_consume;
};

/* Posts the filtered message, then asks to quit behind it. */
class Poster : public Service
{
public:
    explicit Poster(App *app) : m_app(app) {}
    const char *name() const { return "poster"; }
    bool init() { return true; }
    bool start()
    {
        PostMessageA(m_app->window(), WM_CHECK_FILTERED, 0, 0);
        m_app->quit(0);
        return true;
    }

private:
    App *m_app;
};

static HINSTANCE g_instance;

static App *three(unsigned a, unsigned b, unsigned c)
{
    App *app = App::create(g_instance);
    if (!app)
        return 0;
    app->addService(new Recorder(app, 'A', a));
    app->addService(new Recorder(app, 'B', b));
    app->addService(new Recorder(app, 'C', c));
    return app;
}

int main(void)
{
    g_instance = GetModuleHandleA(0);

    {
        App *app = three(0, 0, QUIT_ON_START);
        int code = app->run();
        delete app;
        say("normal-order", code == 7 && strcmp(g_trace,
            "iA iB iC sA sB sC xC xB xA jC jB jA dC dB dA") == 0);
    }
    {
        /* B never initialised, so only A is torn down; C is still owned and
         * deleted. B's own init must clean up after itself. */
        App *app = three(0, FAIL_INIT, 0);
        int code = app->run();
        delete app;
        say("init-failure-unwinds", code == App::RUN_FAILED && strcmp(g_trace,
            "iA iB xA jA dC dB dA") == 0);
    }
    {
        /* All three initialised, so all three are signalled and joined,
         * including B whose start failed and C which never started. */
        App *app = three(0, FAIL_START, 0);
        int code = app->run();
        delete app;
        say("start-failure-unwinds", code == App::RUN_FAILED && strcmp(g_trace,
            "iA iB iC sA sB xC xB xA jC jB jA dC dB dA") == 0);
    }
    {
        /* B's threads did not stop: it is joined, reported, and NOT deleted. */
        App *app = three(0, STUCK_JOIN, QUIT_ON_START);
        app->setJoinTimeout(10);
        app->run();
        delete app;
        say("stuck-join-is-leaked", strcmp(g_trace,
            "iA iB iC sA sB sC xC xB xA jC jB jA dC dA") == 0);
    }
    {
        App *app = three(0, QUIT_FROM_THREAD, 0);
        int code = app->run();
        delete app;
        say("quit-from-another-thread", code == 3);
    }
    {
        /* Windows ending the session stops the services from inside the
         * message, once, and the loop then ends. */
        App *app = three(0, 0, END_SESSION_ON_START);
        int code = app->run();
        delete app;
        say("end-session-stops-once", code == 0 && strcmp(g_trace,
            "iA iB iC sA sB sC xC xB xA jC jB jA dC dB dA") == 0);
    }
    {
        App *app = App::create(g_instance);
        app->quit(5);
        int code = app->run();
        int again = app->run();
        delete app;
        say("quit-before-run-and-run-once",
            code == 5 && again == App::RUN_FAILED);
    }
    {
        App *app = App::create(g_instance);
        bool nullRefused = !app->addService(0);
        bool filled = true;
        for (int i = 0; i < App::MAX_SERVICES; i++)
            filled = app->addService(new Recorder(app, 'a', 0)) && filled;
        g_trace[0] = '\0';
        bool overflowRefused = !app->addService(new Recorder(app, 'Z', 0));
        bool overflowDeleted = strcmp(g_trace, "dZ") == 0;
        delete app;
        say("add-refuses-null-and-full-and-owns-the-reject",
            nullRefused && filled && overflowRefused && overflowDeleted);
    }
    {
        App *app = three(0, 0, 0);
        delete app;
        say("never-run-deletes-without-stopping",
            strcmp(g_trace, "dC dB dA") == 0);
    }
    {
        /* Filters run in the order added, and the first to consume the
         * message stops the rest from seeing it. */
        App *app = App::create(g_instance);
        Filter one('1', false), two('2', true), three_('3', true);
        bool added = app->addMessageFilter(&one) &&
                     app->addMessageFilter(&two) &&
                     app->addMessageFilter(&three_) &&
                     !app->addMessageFilter(0);
        app->addService(new Poster(app));
        int code = app->run();
        delete app;
        say("filters-in-order-first-consumer-wins",
            added && code == 0 && strcmp(g_trace, "f1 f2") == 0);
    }
    return 0;
}
