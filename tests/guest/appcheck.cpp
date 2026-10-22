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
 * x signalStop, j join, d destructor, followed by the service's id. A timed
 * case also appends how long its run() took.
 *
 * WHAT THE TIMED CASES ARE FOR. A real Pentium 200 hung during shutdown on
 * 2026-10-01 with gremote running, because App's WM_ENDSESSION handler joined
 * its services from inside the window procedure -- and Windows will not carry
 * on with a 9x shutdown until that procedure returns. The pair
 * `end-session-does-not-wait-for-a-stuck-service` and
 * `an-ordinary-quit-still-joins-and-waits` pin BOTH halves of the answer: the
 * session-ending path must not wait, and the ordinary path still must.
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
/* Somewhere to read a log file back into. Static rather than a local because
 * `make check` gates any stack frame at a page, and these cases want to hold
 * a whole small log at once. */
static char g_file[2048];

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

/* Append a free-form token, for the timed cases: the detail column is what a
 * failure is read from, and "it was slow" is useless without the number. */
static void traceWord(const char *word)
{
    size_t n = strlen(g_trace);
    size_t w = strlen(word);
    if (n + w + 2 < sizeof g_trace)
    {
        if (n)
            g_trace[n++] = ' ';
        memcpy(g_trace + n, word, w + 1);
    }
}

static void traceMs(DWORD ms)
{
    char buf[24];
    _snprintf(buf, sizeof buf, "%lums", (unsigned long)ms);
    buf[sizeof buf - 1] = '\0';
    traceWord(buf);
}

static void say(const char *name, bool pass)
{
    printf("%s\t%s\t%s\n", pass ? "pass" : "FAIL", name, g_trace);
    g_trace[0] = '\0';
}

/* --- reading a log back ----------------------------------------------------
 *
 * The rotation and the shutdown trace are both claims about what is IN THE
 * FILE after the program has gone, which is how a person reads them on the
 * real machine, so these cases assert on the bytes rather than on a return
 * value. FILE_SHARE_WRITE as well as READ: some of them read a log while
 * another handle on it is still open. */
static int slurp(const char *path)
{
    DWORD n = 0;

    g_file[0] = '\0';
    Handle f(CreateFileA(path, GENERIC_READ,
                         FILE_SHARE_READ | FILE_SHARE_WRITE, NULL,
                         OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, NULL));
    if (!f.valid())
        return 0;
    if (!ReadFile(f.get(), g_file, sizeof g_file - 1, &n, NULL))
        return 0;
    g_file[n] = '\0';
    return 1;
}

static int has(const char *text)
{
    return strstr(g_file, text) != 0;
}

/* a before b, with either missing counting as no. */
static int before(const char *a, const char *b)
{
    const char *pa = strstr(g_file, a);
    const char *pb = strstr(g_file, b);
    return pa && pb && pa < pb;
}

static int exists(const char *path)
{
    return GetFileAttributesA(path) != (DWORD)-1;
}

enum
{
    FAIL_INIT = 1,
    FAIL_START = 2,
    QUIT_ON_START = 4,
    STUCK_JOIN = 8,
    QUIT_FROM_THREAD = 16,
    END_SESSION_ON_START = 32,
    POST_FILTERED_ON_START = 64,
    /* A thread that never wakes. STUCK_JOIN only SAYS it did not stop and
     * returns at once; this one burns the whole budget first, which is what
     * a real WaitForSingleObject on a thread parked in recv() does and the
     * only way to measure that a handler waited. */
    SLOW_JOIN = 128,
    /* Is App's hidden window really in the set USER broadcasts the session
     * messages to? EnumWindows walks top-level windows, which is that set. */
    CHECK_TOP_LEVEL = 256,
    /* WM_ENDSESSION with ENDSESSION_LOGOFF in the lParam. gremote is meant to
     * survive a logoff and not a shutdown, so the log has to say which one it
     * saw -- which means the handler has to read the lParam at all. */
    LOGOFF_ON_START = 512,
    /* The session end WE asked for: noteSessionEndAsked(true) first, then
     * WM_ENDSESSION, which is the order ExitWindowsEx produces. The handler
     * must then NOT end the message loop -- on 9x the thread inside
     * ExitWindowsEx is driving the whole shutdown and ExitProcess would kill
     * it mid-broadcast. A thread quits the loop a moment later so the case can
     * finish; the code and the elapsed time are what say whether App quit by
     * itself or waited to be told. */
    ASKED_FOR_IT_ON_START = 1024
};

/* Long enough to tell a wait from a return, short enough not to slow the
 * suite. The handler must come back in a small fraction of it.
 *
 * TICK_SLACK_MS is not padding for its own sake. `Sleep(1500)` timed with
 * GetTickCount reads back as 1491 often enough to fail about one run in
 * three, because GetTickCount advances in ~15ms steps and the Sleep is
 * measured against a counter that may have ticked just before it started.
 * The two thresholds are 1500 and 500, so a third of a tick's worth of slack
 * cannot blur them. */
enum { SLOW_JOIN_MS = 1500, PROMPT_MS = 500, TICK_SLACK_MS = 32 };

/* How long the asked-for-it case leaves the app running before telling it to
 * stop. Comfortably above PROMPT_MS, so "App quit on its own" and "App had to
 * be told" cannot read the same. */
enum { STAY_MS = 800 };

static HWND g_wantWindow;
static int g_foundWindow;

static BOOL CALLBACK lookForWindow(HWND h, LPARAM lp)
{
    (void)lp;
    if (h != g_wantWindow)
        return TRUE;
    g_foundWindow = 1;
    return FALSE;
}

static unsigned __stdcall quitLater(void *arg)
{
    Sleep(50);
    ((App *)arg)->quit(3);
    return 0;
}

/* Windows' job, played by a thread: the thing that ends a process which has
 * decided not to end itself. Code 11 so the verdict can tell this ending from
 * App's own PostQuitMessage(0). */
static unsigned __stdcall quitMuchLater(void *arg)
{
    Sleep(STAY_MS);
    ((App *)arg)->quit(11);
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
        if (m_flags & ASKED_FOR_IT_ON_START)
        {
            unsigned tid;
            m_thread.reset((HANDLE)_beginthreadex(0, 0, quitMuchLater, m_app,
                                                  0, &tid));
            m_app->noteSessionEndAsked(true);
            PostMessageA(m_app->window(), WM_ENDSESSION, TRUE, 0);
        }
        if (m_flags & LOGOFF_ON_START)
        {
            /* Both, in the order USER sends them: the query line is how
             * "WM_ENDSESSION never arrived" is told from "the broadcast never
             * reached us", so it has to be exercised too. */
            PostMessageA(m_app->window(), WM_QUERYENDSESSION, 0,
                         ENDSESSION_LOGOFF);
            PostMessageA(m_app->window(), WM_ENDSESSION, TRUE,
                         ENDSESSION_LOGOFF);
        }
        if (m_flags & CHECK_TOP_LEVEL)
        {
            g_wantWindow = m_app->window();
            g_foundWindow = 0;
            EnumWindows(lookForWindow, 0);
            m_app->quit(0);
        }
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
        if (m_flags & SLOW_JOIN)
        {
            /* The whole budget, then the truth. A service that will not come
             * back costs its caller the timeout -- which is fine after the
             * message loop and fatal inside WM_ENDSESSION. */
            Sleep(timeoutMs);
            return false;
        }
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
    APP_NO_SERVICES = 4,
    /* What gaston and gremote really do: close the log as the last act of
     * cleanup(). That is what makes the handler's own closing line need
     * rc_log_tail, so the cases that read the file back set this. */
    APP_CLOSES_LOG = 8
};

/* Three services, A B C, created in order with their dependencies (here
 * just the app) in initializeServices, and taken down by hand. */
class TestApp : public App
{
public:
    /* `joinMs` is the budget stopServices() hands joinOrLeak. Ten for the
     * ordering cases, where waiting is only overhead; SLOW_JOIN_MS for the
     * two that measure whether it waited at all. */
    TestApp(unsigned a, unsigned b, unsigned c, unsigned appFlags = 0,
            DWORD joinMs = 10)
        : App(g_instance), m_flagsA(a), m_flagsB(b), m_flagsC(c),
          m_appFlags(appFlags), m_joinMs(joinMs)
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
        joinOrLeak(m_c, *this, m_joinMs);
        joinOrLeak(m_b, *this, m_joinMs);
        joinOrLeak(m_a, *this, m_joinMs);
    }

    void cleanup()
    {
        m_c.reset();
        m_b.reset();
        m_a.reset();
        trace('C', '.');
        if (m_appFlags & APP_CLOSES_LOG)
            rc_log_close();
    }

private:
    unsigned m_flagsA, m_flagsB, m_flagsC, m_appFlags;
    DWORD m_joinMs;
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

/* Same thing, with the wall clock around it. The only slow thing any TestApp
 * does is join a SLOW_JOIN service, so the run's duration IS the time spent
 * waiting -- and when the stop came from WM_ENDSESSION, that is time spent
 * inside the window procedure with Windows waiting on it. */
static DWORD g_elapsedMs;

static int runTimed(TestApp *app)
{
    DWORD t0 = GetTickCount();
    int code = app->run("");
    g_elapsedMs = GetTickCount() - t0;
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
        /* Windows ending the session signals every service once and cleans
         * up from inside the message, and the loop then ends. No j and no d:
         * the fast path abandons each service where it stands rather than
         * joining it, and abandoned means released, so nothing is freed
         * under a thread that may still be live. */
        int code = runOnce(new TestApp(0, 0, END_SESSION_ON_START));
        say("end-session-stops-once", code == 0 && strcmp(g_trace,
            "I. P. iA iB iC sA sB sC xC xB xA C.") == 0);
    }
    {
        /* AND THE OTHER HALF OF THE SAME 2026-10-01 MEASUREMENT. When the
         * shutdown is one WE asked for, the handler must tear down and then
         * STAY: on 9x the thread sitting inside ExitWindowsEx is driving the
         * broadcast, so a PostQuitMessage here is an ExitProcess a second
         * later that kills the driver after Explorer has gone and before
         * Systray is dealt with -- a bare desktop and no reboot, four runs out
         * of four (research/measurements/gremote-shutdown-vm-20261001.txt).
         *
         * Wine cannot test the shutdown, but it can test the decision: the
         * services are signalled and abandoned exactly as before, and the loop
         * is still running afterwards. Code 11 and STAY_MS on the clock are
         * what say it was the thread playing Windows that ended this, not App
         * quitting by itself -- App quitting by itself would be code 0 and
         * promptly. */
        int code = runTimed(new TestApp(0, 0, ASKED_FOR_IT_ON_START));
        bool stayed = g_elapsedMs + TICK_SLACK_MS >= STAY_MS;
        bool signalled = strstr(g_trace, "xA") != 0;
        bool noJoin = strstr(g_trace, "jA") == 0;
        traceMs(g_elapsedMs);
        say("a-session-end-we-asked-for-does-not-quit-the-loop",
            code == 11 && stayed && signalled && noJoin);
    }
    {
        /* THE REGRESSION a forced reset paid for. C's threads never wake, so
         * joining it costs the full budget. Inside WM_ENDSESSION that budget
         * is Windows 98's shutdown held on our window procedure -- and when
         * the reboot arrived over gremote's own connection, a deadlock
         * against our reader thread as well (core/app.h). The handler must
         * come back at once. */
        int code = runTimed(new TestApp(0, 0, SLOW_JOIN | END_SESSION_ON_START,
                                        0, SLOW_JOIN_MS));
        bool prompt = g_elapsedMs < PROMPT_MS;
        bool signalled = strstr(g_trace, "xC") != 0;
        bool noJoin = strstr(g_trace, "jC") == 0;
        traceMs(g_elapsedMs);
        say("end-session-does-not-wait-for-a-stuck-service",
            code == 0 && prompt && signalled && noJoin);
    }
    {
        /* The other half of the same decision, and the reason it is not just
         * "never join": here the process goes on living, so an orphan thread
         * matters. The ordinary path waits out the budget and then LEAKS --
         * jC present, dC absent. */
        int code = runTimed(new TestApp(0, 0, SLOW_JOIN | QUIT_ON_START,
                                        0, SLOW_JOIN_MS));
        bool waited = g_elapsedMs + TICK_SLACK_MS >= SLOW_JOIN_MS;
        bool joined = strstr(g_trace, "jC") != 0;
        bool leaked = strstr(g_trace, "dC") == 0;
        traceMs(g_elapsedMs);
        say("an-ordinary-quit-still-joins-and-waits",
            code == 7 && waited && joined && leaked);
    }
    {
        /* runApp leaks the App on a session end, because the threads it just
         * abandoned hold pointers into the App's own members. */
        UniqueObj<TestApp> app(new TestApp(0, 0, END_SESSION_ON_START));
        int code = runApp(app, "");
        say("run-app-leaks-the-app-on-a-session-end",
            code == 0 && !app.valid());
    }
    {
        /* And keeps it on an ordinary quit, so the destructors run. */
        UniqueObj<TestApp> app(new TestApp(0, 0, QUIT_ON_START));
        int code = runApp(app, "");
        bool kept = app.valid();
        app.reset();
        say("run-app-keeps-the-app-on-an-ordinary-quit",
            code == 7 && kept && strstr(g_trace, "dA") != 0);
    }
    {
        /* App's window is a hidden zero-size WS_POPUP, chosen because
         * HWND_MESSAGE is Win2000+. EnumWindows walks TOP-LEVEL windows,
         * which is the set USER broadcasts WM_QUERYENDSESSION and
         * WM_ENDSESSION to -- so finding it there is what says the whole
         * session-end path can reach it at all. */
        runOnce(new TestApp(0, 0, CHECK_TOP_LEVEL));
        say("app-window-is-top-level", g_foundWindow != 0);
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

    /* --- core/log.h ---------------------------------------------------
     *
     * THE LOG USED TO DELETE THE EVIDENCE. rc_log_open opened with
     * CREATE_ALWAYS, and the first act of recovering a hung Windows 98 box is
     * a forced restart -- so the lines written on the way down, the only
     * record of the hang, were destroyed by the act of recovering from it.
     * The user's real Pentium hung during shutdown twice and both reboots
     * learned nothing. rc_log_open now renames <program>.log to
     * <program>.prv first.
     *
     * These cases come LAST on purpose: everything above runs with no log
     * open, so App's own rc_log calls go nowhere and cannot land in the files
     * read back here. They run in whatever directory the binary was started
     * in, which for the suite is a fresh temp dir (tests/python/conftest.py,
     * run_guest). */
    {
        rc_log_open(".", "lrot");
        rc_log("MARKER-ONE");
        rc_log_close();
        int second = rc_log_open(".", "lrot");
        rc_log("MARKER-TWO");
        rc_log_close();

        slurp("lrot.prv");
        bool kept = has("MARKER-ONE") && has("=== lrot exiting ===");
        slurp("lrot.log");
        bool fresh = has("MARKER-TWO") && !has("MARKER-ONE") &&
                     has("the previous run is now lrot.prv");
        say("log-keeps-the-previous-run", second == 0 && kept && fresh);
    }
    {
        /* Nothing to keep, and no empty .prv pretending there was. */
        int opened = rc_log_open(".", "lfirst");
        rc_log_close();
        slurp("lfirst.log");
        say("log-first-run-keeps-nothing",
            opened == 0 && !exists("lfirst.prv") &&
            has("=== lfirst starting ===") && !has("previous run"));
    }
    {
        /* THE NAME. `.prv` has an 8.3 spelling; `.log.old` has none, and on
         * FAT a name with no 8.3 form is a DIFFERENT file from DOS and from
         * the installer -- which this project has paid for before. Pinned on
         * the longest stem that uses this (gremote, 7 characters). */
        rc_log_open(".", "gremote");
        rc_log_close();
        rc_log_open(".", "gremote");
        rc_log_close();
        say("log-rotation-name-has-an-8-3-spelling",
            exists("gremote.prv") && !exists("gremote.log.old") &&
            strlen("gremote") <= 8);
    }
    {
        /* Several of our exes share one directory on the test box, so the
         * rotation has to be per program or one of them eats the other's
         * previous run. */
        rc_log_open(".", "lpa");
        rc_log("AAA");
        rc_log_close();
        rc_log_open(".", "lpb");
        rc_log("BBB");
        rc_log_close();
        rc_log_open(".", "lpa");
        rc_log_close();
        rc_log_open(".", "lpb");
        rc_log_close();

        slurp("lpa.prv");
        bool a = has("AAA") && !has("BBB");
        slurp("lpb.prv");
        bool b = has("BBB") && !has("AAA");
        say("log-rotation-is-per-program", a && b);
    }
    {
        /* A FAILED ROTATION MUST STILL LOG. A diagnostic feature that can
         * prevent logging is worse than the bug it fixes, so this forces the
         * rename to fail -- .prv held open with no sharing, which is what a
         * read-only directory or a file somebody else has open looks like
         * from here -- and asks for the current run anyway. */
        rc_log_open(".", "lfail");
        rc_log("MARKER-OLD");
        rc_log_close();

        Handle hold(CreateFileA("lfail.prv", GENERIC_WRITE, 0, NULL,
                                CREATE_ALWAYS, FILE_ATTRIBUTE_NORMAL, NULL));
        DWORD written = 0;
        bool held = hold.valid() != 0;
        if (held)
            WriteFile(hold.get(), "HELD\r\n", 6, &written, NULL);

        int opened = rc_log_open(".", "lfail");
        rc_log("MARKER-NEW");
        rc_log_close();
        hold.reset();

        slurp("lfail.log");
        bool logs = opened == 0 && has("=== lfail starting ===") &&
                    has("MARKER-NEW") &&
                    has("could NOT be kept as lfail.prv");
        slurp("lfail.prv");
        bool untouched = has("HELD");
        say("log-survives-a-failed-rotation",
            held && written == 6 && logs && untouched);
    }
    {
        /* THE SHUTDOWN TRACE, read back the way a person reads it after a
         * forced restart. Four markers, each settling one case: the handler
         * was entered; the signalling got through; cleanup() ran; and the
         * handler returned to Windows. The last of those lands AFTER
         * cleanup() closed the log, so it proves rc_log_tail's reopen as
         * well -- without it, a fault on the way out of the handler and a
         * clean exit end with the same line. */
        rc_log_open(".", "lend");
        runOnce(new TestApp(0, 0, END_SESSION_ON_START, APP_CLOSES_LOG));
        slurp("lend.log");
        bool entered = has("Windows is ending the session "
                           "(a shutdown or restart)");
        bool signalled = has("abandoning RC unjoined");
        bool cleaning = has("every service is signalled and abandoned; "
                            "cleaning up");
        bool returned = before("=== lend exiting ===",
                               "the session-end handler is returning to "
                               "Windows");
        say("end-session-writes-entry-through-exit-to-the-log",
            entered && signalled && cleaning && returned);
    }
    {
        /* AND THE LOG HAS TO SAY WHICH ENDING IT TOOK. The closing line used
         * to promise "nothing below this line is ours", which on the staying-up
         * path is not true -- the watchdog writes under it. A line that says
         * the wrong one of these is worse than no line: it is the line a
         * person reads first after a forced restart. */
        rc_log_open(".", "lstay");
        runOnce(new TestApp(0, 0, ASKED_FOR_IT_ON_START, APP_CLOSES_LOG));
        slurp("lstay.log");
        say("the-log-says-when-the-process-is-staying-up",
            has("the session-end handler is returning to Windows") &&
            has("stays up to drive the shutdown it asked for") &&
            !has("nothing below this line is ours"));
    }
    {
        /* And it reads the lParam, because "gremote outlives a logoff" is a
         * claim the log has to be able to confirm or deny. */
        rc_log_open(".", "lloff");
        runOnce(new TestApp(0, 0, LOGOFF_ON_START, APP_CLOSES_LOG));
        slurp("lloff.log");
        say("end-session-says-whether-it-was-a-logoff",
            has("asks whether the session may end (a logoff)") &&
            has("Windows is ending the session (a logoff)"));
    }
    {
        /* The ordinary path stays quiet: these lines mean a session end, and
         * a line that appears either way settles nothing. */
        rc_log_open(".", "lquit");
        runOnce(new TestApp(0, 0, QUIT_ON_START, APP_CLOSES_LOG));
        slurp("lquit.log");
        say("an-ordinary-quit-writes-no-session-lines",
            has("=== lquit exiting ===") && !has("session") &&
            !has("cleaning up"));
    }
    return 0;
}
