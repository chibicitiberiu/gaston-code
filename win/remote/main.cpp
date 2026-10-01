/* gremote.exe -- the Win98 box, driven from Linux.
 *
 * The reverse of the client: this listens and the caller connects. It exists
 * because the real Pentium 200 has no QMP and no ssh, so every measurement
 * on it has meant burning a floppy and reading COM1 (docs/BACKLOG.md 1b).
 *
 * GremoteApp is the composition root, the same shape as the client's
 * ClientApp: App runs the lifecycle and this fills in each phase.
 *
 * WHY A GUI SUBSYSTEM EXE WITH NO WINDOW. A console image opens a console
 * when it starts, and this is meant to be started from RunServices before
 * anyone logs on. App's hidden window is not decoration either: quit() works
 * from any thread through it, and it is what receives WM_ENDSESSION so the
 * services stop before Windows ends the process. Nothing is ever shown: the
 * one thing on the screen is a notification-area icon, and the window behind
 * that is hidden too (remote/tray.h).
 *
 * WHY A REFUSAL IS NOT A DIALOG. A bad setting on an unattended machine must
 * not put up a modal nobody will ever dismiss -- it would hold the box until
 * somebody walks over, which is the thing this program exists to stop. The
 * refusal goes to the log, and to a message box only when -i says a person
 * started it.
 */
#include <windows.h>
#include <string.h>

#include "core/app.h"
#include "core/log.h"
#include "core/path.h"
#include "core/str.h"
#include "exec/process.h"
#include "remote/config.h"
#include "remote/server.h"
#include "remote/tray.h"

static const char TITLE[] = "Gaston Remote";

/* THE WATCH THAT COMES WITH STAYING UP.
 *
 * When REBOOT asks Windows to end the session, this process must not exit
 * while its own thread is inside ExitWindowsEx driving the broadcast -- doing
 * so wedges a Windows 98 on a bare desktop, measured four runs out of four
 * (core/app.h, docs/measurements/gremote-shutdown-vm-20261001.txt). App
 * therefore skips its PostQuitMessage on that path and the message loop keeps
 * running until Windows ends the process itself.
 *
 * But a session end can fail to take. `mode=poweroff` on a machine with no
 * power management runs the whole teardown, cannot switch the machine off,
 * and brings Windows back to the logon prompt -- measured on the same guest,
 * same day. What is left then is this process with every service abandoned,
 * nothing listening, and GastonRemoteSingleInstance still held, so a new copy
 * started from Start > Run says "another copy is already running" and exits.
 * A test box that answers nothing and cannot be told to is worse than the
 * hang.
 *
 * Shell_TrayWnd is the first signal. Explorer goes before WM_ENDSESSION and
 * stays gone for the rest of a shutdown that is really happening; the taskbar
 * existing again is Windows saying it carried on living, and at that moment
 * there is nothing left to outlive.
 *
 * It is not sufficient on its own, and that was MEASURED rather than reasoned
 * about: a failed poweroff lands on the LOGON prompt, where Explorer is not
 * running either, so an unattended box would sit there with the mutex held
 * until somebody walked over and logged in. Hence the second signal, which is
 * just time. Three minutes is far beyond any shutdown that is going to
 * finish -- on this guest a reboot is through POST in nine seconds -- so a
 * machine still here at three minutes is one where the session end did not
 * happen or already wedged, and in neither case is there a broadcast left for
 * this process to drive. It exits and says so.
 *
 * In between, the timer keeps watching and stops WRITING after a minute: a
 * machine that never comes back must not fill its own disk explaining it. The
 * lines it does write are how the next person tells "gremote left too early
 * again" from "gremote stayed and the shutdown stalled somewhere else". */
enum { STAY_TICK_MS = 15000, STAY_QUIET_TICKS = 4, STAY_GIVEUP_TICKS = 12 };
static const UINT_PTR STAY_TIMER = 1;

/* Stay running when the person logs off.
 *
 * On 9x a process started from RunServices runs before anyone logs on, and
 * would ordinarily be torn down at logoff along with everything else.
 * RegisterServiceProcess marks it as a service so it survives -- which is
 * the difference between a test box you can reach after a reboot and one
 * somebody has to walk over to and log into first.
 *
 * Resolved, never imported: it is 9x-only, and a single NT-only static
 * import kills the image at load before WinMain with no log written. Absent
 * on NT, where the whole idea does not apply -- so a failure here is logged
 * and shrugged at rather than fatal.
 */
static void stayRunningAfterLogoff()
{
    typedef DWORD (WINAPI *RegisterServiceFn)(DWORD, DWORD);
    enum { RSP_SIMPLE_SERVICE = 1 };

    Dll k32(LoadLibraryA("KERNEL32.DLL"));
    RegisterServiceFn reg = k32.valid()
        ? (RegisterServiceFn)GetProcAddress(k32.get(),
                                            "RegisterServiceProcess")
        : 0;
    if (!reg)
    {
        rc_log("gremote: no RegisterServiceProcess here; this copy will "
               "stop when the session ends (NT does it this way)");
        return;
    }
    if (reg(0, RSP_SIMPLE_SERVICE))
        rc_log("gremote: registered as a service process; it will outlive "
               "a logoff");
    else
        rc_log("gremote: RegisterServiceProcess refused (%lu)",
               (unsigned long)GetLastError());
}

static void exeDir(char *out, unsigned n)
{
    char path[MAX_PATH];
    char *slash;
    GetModuleFileNameA(NULL, path, sizeof(path));
    slash = strrchr(path, '\\');
    if (slash) *slash = '\0';
    rc_strf(out, n, "%s", path);
    out[n - 1] = '\0';
}

class GremoteApp : public App
{
public:
    explicit GremoteApp(HINSTANCE instance);

protected:
    bool initialize();
    bool parseArgs(const char *cmdLine);
    bool initializeServices();
    bool startServices();
    bool preTranslate(MSG &msg);
    void stopServices();
    void cleanup();

private:
    void refuse(const char *what);
    bool stayTick();

    char m_exeDir[MAX_PATH];
    bool m_interactive;
    unsigned m_stayTicks;
    ServerConfig m_config;
    ExecConfig m_exec;
    MachineConfig m_machine;
    /* Toolhelp, resolved once: HELLO reports whether this machine has it,
     * and the control lane's PS and KILL will want the same object. */
    Processes m_procs;
    UniqueObj<ServerConfigStore> m_store;
    UniqueObj<ServerService> m_server;
    UniqueObj<TrayService> m_tray;
};

GremoteApp::GremoteApp(HINSTANCE instance)
    : App(instance), m_interactive(false), m_stayTicks(0)
{
    m_exeDir[0] = '\0';
}

/* One tick of the watch described at the top of this file. True consumes the
 * message. */
bool GremoteApp::stayTick()
{
    m_stayTicks++;
    /* rc_log_tail, not rc_log, throughout: cleanup() closed the log before the
     * session-end handler returned, and this reopens the file and appends. */
    if (FindWindowA("Shell_TrayWnd", NULL))
    {
        KillTimer(window(), STAY_TIMER);
        rc_log_tail("gremote: the shell is back, so the session end did not "
                    "take; exiting so a new copy can be started");
        quit(0);
    }
    else if (m_stayTicks >= STAY_GIVEUP_TICKS)
    {
        KillTimer(window(), STAY_TIMER);
        rc_log_tail("gremote: three minutes after the session end this "
                    "machine is still here, so nothing is left to drive; "
                    "exiting so a new copy can be started");
        quit(0);
    }
    else if (m_stayTicks == STAY_QUIET_TICKS)
    {
        rc_log_tail("gremote: the shell is still gone a minute after the "
                    "session end, so the shutdown is still going or this "
                    "machine is wedged; watching on without saying more");
    }
    return true;
}

bool GremoteApp::preTranslate(MSG &msg)
{
    return msg.message == WM_TIMER && msg.wParam == STAY_TIMER &&
           sessionEndAskedByUs() && stayTick();
}

void GremoteApp::refuse(const char *what)
{
    rc_log("gremote: %s", what);
    if (!m_interactive)
        return;
    char msg[512];
    rc_strf(msg, sizeof msg,
            "gremote.ini has a setting that cannot be used:\n\n%s\n\n"
            "Fix it and start again.\n\n%s", what,
            m_store.valid() ? m_store->path() : "");
    MessageBoxA(0, msg, TITLE, MB_OK | MB_ICONSTOP);
}

/* Before parseArgs, so -i is not read yet: initialize() only refuses things
 * that cannot depend on it. */
bool GremoteApp::initialize()
{
    exeDir(m_exeDir, sizeof m_exeDir);
    /* From the first statement. On this platform "no log file was created"
     * is the primary diagnostic: an NT-only static import kills the process
     * at load, before WinMain, leaving nothing else behind. */
    rc_log_open(m_exeDir, "gremote");
    rc_log("os version: %08lx", GetVersion());

    /* One instance. Two would fight over the port and the second would sit
     * in the bind retry for four minutes looking like a hang. */
    {
        HANDLE once = CreateMutexA(NULL, FALSE, "GastonRemoteSingleInstance");
        if (once && GetLastError() == ERROR_ALREADY_EXISTS)
        {
            rc_log("gremote: another copy is already running; exiting");
            CloseHandle(once);
            return exitWith(0);
        }
        /* Deliberately never closed: the handle IS the lock, and the
         * process exiting releases it however it exits. */
    }
    return true;
}

bool GremoteApp::parseArgs(const char *cmdLine)
{
    /* One switch, and it only changes how a refusal is reported. Anything
     * else is ignored rather than refused: a service started from the
     * registry is not a place to be strict about argv. */
    if (cmdLine && (strstr(cmdLine, "-i") || strstr(cmdLine, "/i")))
        m_interactive = true;

    m_store.reset(new ServerConfigStore(m_exeDir));
    if (!m_store.valid())
        return false;
    if (!m_store->load(&m_config, &m_exec, &m_machine))
    {
        refuse(m_store->error());
        return false;
    }
    stayRunningAfterLogoff();
    if (!m_procs.init())
        rc_log("gremote: no Toolhelp on this machine; PS and KILL will "
               "refuse (NT 4 does this, 9x does not)");
    return true;
}

bool GremoteApp::initializeServices()
{
    /* The App goes in because UPDATE stops it: a self-update has to end
     * this process before the replacement can take the image, and the
     * updater is already waiting for that to happen. Why that is not the
     * remote QUIT docs/REMOTE.md forbids is argued above
     * ControlOps::update. */
    if (!initService(m_server,
                     new ServerService(m_config, m_exec, m_procs, *this)))
        return false;
    /* Dependency order: the tray reads the server's status, so the server
     * exists first. The tray never fails startup -- see TrayService::init
     * -- but it is built here rather than in startServices so that the
     * icon is up before the accept loop begins, and so that a cold boot
     * with no shell has already logged the one line that explains why
     * there is no icon. */
    return initService(m_tray,
                       new TrayService(*this, instance(), m_config, m_machine,
                                       *m_server.get()));
}

bool GremoteApp::startServices()
{
    return m_server->start();
}

void GremoteApp::stopServices()
{
    /* The icon first: NIM_DELETE wants a live process behind it, and the
     * server's join below can take the better part of a second.
     *
     * THIS IS RIGHT ON BOTH TEARDOWN PATHS, and for the same reason either
     * way: signalStop() is called on every service on both of them
     * (core/app.h), so the icon comes off before the process goes, and a
     * session end does not get a ghost in a notification area that may
     * outlive us. It is also safe to do from inside the WM_ENDSESSION
     * handler, which must return promptly: by then Explorer is gone, so
     * Shell_NotifyIcon finds no tray window and fails at once, and the tray
     * has no threads to join -- which is why it takes no joinOrLeak below.
     * Nothing here waits. */
    if (m_tray.valid()) m_tray->signalStop();
    if (m_server.valid()) m_server->signalStop();
    /* Generous: the accept loop wakes within a quarter second, then drains
     * and joins the live client's Connection.
     *
     * ON A SESSION END THIS WAITS FOR NOTHING. Those 8 seconds are 8 seconds
     * of a Windows 98 shutdown held on our window procedure, and when the
     * reboot came in over the connection they are also a deadlock against our
     * own reader thread (core/app.h). joinOrLeak takes the App for exactly
     * that reason and abandons the server unjoined instead. */
    joinOrLeak(m_server, *this, 8000);

    /* AND ARM THE WATCH, when this is the session end we asked for. Here
     * rather than anywhere else because here is the one place that runs
     * inside the WM_ENDSESSION handler, while the window still exists and
     * before cleanup() closes the log. Nothing waits: SetTimer returns at
     * once, and the thread that is driving the shutdown has released
     * Win16Mutex into the SendMessage it is blocked in. See the top of this
     * file for what the watch is for. */
    if (sessionEndAskedByUs())
        SetTimer(window(), STAY_TIMER, STAY_TICK_MS, NULL);
}

void GremoteApp::cleanup()
{
    /* Reverse of initializeServices: the tray holds a reference to the
     * server. */
    m_tray.reset();
    m_server.reset();
    m_store.reset();
    rc_log_mem("exiting");
    rc_log_close();
}

int WINAPI WinMain(HINSTANCE inst, HINSTANCE prev, LPSTR cmd, int show)
{
    (void)prev;
    (void)show;
    /* runApp, not run(): on a session end the services were abandoned with
     * their threads live, and those threads hold m_config, m_exec and
     * m_procs, which are members of this object. runApp leaks it rather than
     * destroying it under them (core/app.h). */
    UniqueObj<GremoteApp> app(new GremoteApp(inst));
    return runApp(app, cmd);
}
