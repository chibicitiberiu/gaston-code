/* Does App run the lifecycle it documents?
 *
 * Drives the real `win/core/app.cpp` with a derived app whose hooks and
 * services record every call into one trace, and prints
 * `verdict<TAB>case<TAB>trace` rows for tests/python/guest/test_guest_app.py.
 * The trace is the detail column, so a failure shows the order that actually
 * happened.
 *
 * Trace tokens: I. initialize, P. parseArgs, C. cleanup, f. preTranslate
 * consumed the check message; then per service i init, s start,
 * x signalStop, j join, d destructor, followed by the service's id.
 */
#include <windows.h>
#include <process.h>
#include <stdio.h>
#include <string.h>
#include "core/rc_cpp.h"
#include "core/app.h"
#include "core/thread.h"

/* POD, so no static constructor (make check fails the build on one). */
static char g_trace[512];
static HINSTANCE g_instance;

/* Its own block, clear of core/'s WM_APP + 0x100 (tools/check/msgids.py). */
#define WM_CHECK_FILTERED (WM_APP + 0x200)

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
    END_SESSION_ON_START = 32,
    POST_FILTERED_ON_START = 64
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

    ~Recorder() { trace('d', m_id); }

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
        if (m_flags & POST_FILTERED_ON_START)
        {
            PostMessageA(m_app->window(), WM_CHECK_FILTERED, 0, 0);
            m_app->quit(0);
        }
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

enum
{
    APP_FAIL_INITIALIZE = 1,
    APP_EXIT_ZERO = 2,
    APP_NO_SERVICES = 4
};

/* Three services, A B C, created in order with their dependencies (here
 * just the app) in initializeServices, and taken down by hand. */
class TestApp : public App
{
public:
    TestApp(unsigned a, unsigned b, unsigned c, unsigned appFlags = 0)
        : App(g_instance), m_flagsA(a), m_flagsB(b), m_flagsC(c),
          m_appFlags(appFlags)
    {
    }

protected:
    bool initialize()
    {
        trace('I', '.');
        if (m_appFlags & APP_EXIT_ZERO)
            return exitWith(0);
        return !(m_appFlags & APP_FAIL_INITIALIZE);
    }

    bool parseArgs(const char *cmdLine)
    {
        (void)cmdLine;
        trace('P', '.');
        return true;
    }

    bool initializeServices()
    {
        if (m_appFlags & APP_NO_SERVICES)
            return true;
        return initService(m_a, new Recorder(this, 'A', m_flagsA)) &&
               initService(m_b, new Recorder(this, 'B', m_flagsB)) &&
               initService(m_c, new Recorder(this, 'C', m_flagsC));
    }

    bool startServices()
    {
        return (!m_a.valid() || m_a->start()) &&
               (!m_b.valid() || m_b->start()) &&
               (!m_c.valid() || m_c->start());
    }

    bool preTranslate(MSG &msg)
    {
        if (msg.message != WM_CHECK_FILTERED)
            return false;
        trace('f', '.');
        return true;
    }

    void stopServices()
    {
        if (m_c.valid()) m_c->signalStop();
        if (m_b.valid()) m_b->signalStop();
        if (m_a.valid()) m_a->signalStop();
        joinOrLeak(m_c, 10);
        joinOrLeak(m_b, 10);
        joinOrLeak(m_a, 10);
    }

    void cleanup()
    {
        m_c.reset();
        m_b.reset();
        m_a.reset();
        trace('C', '.');
    }

private:
    unsigned m_flagsA, m_flagsB, m_flagsC, m_appFlags;
    UniqueObj<Recorder> m_a, m_b, m_c;
};

/* Somewhere for burn()'s frames to end up, so they are not optimised away.
 * POD, so no static constructor (make check fails the build on one). */
static volatile LONG g_sink;

class Body : public Runnable
{
public:
    Body() : ran(0), gate(0), deep(0) {}

    void run()
    {
        if (gate)
            WaitForSingleObject(gate, 10000);
        if (deep)
            burn(deep);
        ran = 1;
    }

    /* Frames under a page, several of them: what the stack-frame gate
     * permits, and therefore what a chosen stack size has to hold. */
    static void burn(int n)
    {
        char frame[1024];
        memset(frame, n & 0xff, sizeof frame);
        if (n > 1)
            burn(n - 1);
        g_sink += frame[0];
    }

    volatile LONG ran;
    HANDLE gate;
    int deep;
};

static int runOnce(TestApp *app)
{
    int code = app->run("");
    delete app;
    return code;
}

int main(void)
{
    g_instance = GetModuleHandleA(0);

    {
        int code = runOnce(new TestApp(0, 0, QUIT_ON_START));
        say("normal-order", code == 7 && strcmp(g_trace,
            "I. P. iA iB iC sA sB sC xC xB xA jC jB jA dC dB dA C.") == 0);
    }
    {
        /* B was created but its init failed; C never was. Stop reaches
         * every service that exists, and B must cope with it. */
        int code = runOnce(new TestApp(0, FAIL_INIT, 0));
        say("init-failure-unwinds", code == App::RUN_FAILED && strcmp(g_trace,
            "I. P. iA iB xB xA jB jA dB dA C.") == 0);
    }
    {
        int code = runOnce(new TestApp(0, FAIL_START, 0));
        say("start-failure-unwinds", code == App::RUN_FAILED && strcmp(g_trace,
            "I. P. iA iB iC sA sB xC xB xA jC jB jA dC dB dA C.") == 0);
    }
    {
        /* B's threads did not stop: joined, reported, and NOT deleted. */
        runOnce(new TestApp(0, STUCK_JOIN, QUIT_ON_START));
        say("stuck-join-is-leaked", strcmp(g_trace,
            "I. P. iA iB iC sA sB sC xC xB xA jC jB jA dC dA C.") == 0);
    }
    {
        int code = runOnce(new TestApp(0, QUIT_FROM_THREAD, 0));
        say("quit-from-another-thread", code == 3);
    }
    {
        /* Windows ending the session stops and cleans up from inside the
         * message, once, and the loop then ends. */
        int code = runOnce(new TestApp(0, 0, END_SESSION_ON_START));
        say("end-session-stops-once", code == 0 && strcmp(g_trace,
            "I. P. iA iB iC sA sB sC xC xB xA jC jB jA dC dB dA C.") == 0);
    }
    {
        TestApp *app = new TestApp(0, 0, 0, APP_NO_SERVICES);
        app->quit(5);
        int code = app->run("");
        int again = app->run("");
        delete app;
        say("quit-before-run-and-run-once",
            code == 5 && again == App::RUN_FAILED);
    }
    {
        /* The single-instance shape: stop early, and it is not a failure. */
        int code = runOnce(new TestApp(0, 0, 0, APP_EXIT_ZERO));
        say("exit-with-skips-the-rest", code == 0 &&
            strcmp(g_trace, "I. C.") == 0);
    }
    {
        int code = runOnce(new TestApp(0, 0, 0, APP_FAIL_INITIALIZE));
        say("initialize-failure", code == App::RUN_FAILED &&
            strcmp(g_trace, "I. C.") == 0);
    }
    {
        runOnce(new TestApp(0, 0, POST_FILTERED_ON_START));
        say("pretranslate-consumes", strstr(g_trace, "f.") != 0);
    }
    {
        delete new TestApp(0, 0, 0);
        say("never-run-creates-nothing", g_trace[0] == '\0');
    }

    /* --- core/thread.h ------------------------------------------------
     *
     * Thread is libcore's, not App's, but it is what App's services start
     * and this is the exe that already links libcore and runs under Wine.
     * A second check binary for one class would be four more hand-kept
     * lists (all:, .PHONY, guest-checks, the ISO) for no more coverage. */
    {
        Body b;
        Thread t;
        bool started = t.start(b, "runs-and-joins", 0);
        bool joined = t.join(5000);
        say("thread-runs-and-joins", started && joined && b.ran);
    }
    {
        /* The point of the class. 16 KB against the image's 64 KB commit,
         * with four 1 KB frames actually used on it -- the frame gate caps
         * any one of them below a page, so the depth is what is left to
         * get wrong. */
        Body b;
        b.deep = 4;
        Thread t;
        bool started = t.start(b, "small-stack", 16u * 1024u);
        bool joined = t.join(5000);
        say("thread-takes-a-small-stack", started && joined && b.ran);
    }
    {
        /* A service whose start() failed is still signalled and joined
         * (core/app.h), so this must not be an error. */
        Thread t;
        say("join-without-start-is-true", t.join(0) && !t.started());
    }
    {
        Body b;
        Thread t;
        bool first = t.start(b, "once", 0);
        bool second = t.start(b, "twice", 0);
        t.join(5000);
        say("a-thread-starts-once", first && !second);
    }
    {
        /* join() says false rather than lying, which is what joinOrLeak()
         * turns into a leak instead of a free under a live thread. */
        Body b;
        Handle gate(CreateEventA(NULL, TRUE, FALSE, NULL));
        b.gate = gate.get();
        Thread t;
        bool started = t.start(b, "still-running", 0);
        bool early = t.join(50);
        SetEvent(gate.get());
        bool late = t.join(5000);
        say("join-reports-a-thread-still-running",
            started && !early && late && b.ran);
    }
    return 0;
}
