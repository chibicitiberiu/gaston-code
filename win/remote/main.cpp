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
 * services stop before Windows ends the process.
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

static const char TITLE[] = "Gaston Remote";

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
    void stopServices();
    void cleanup();

private:
    void refuse(const char *what);

    char m_exeDir[MAX_PATH];
    bool m_interactive;
    ServerConfig m_config;
    ExecConfig m_exec;
    /* Toolhelp, resolved once: HELLO reports whether this machine has it,
     * and the control lane's PS and KILL will want the same object. */
    Processes m_procs;
    UniqueObj<ServerConfigStore> m_store;
    UniqueObj<ServerService> m_server;
};

GremoteApp::GremoteApp(HINSTANCE instance)
    : App(instance), m_interactive(false)
{
    m_exeDir[0] = '\0';
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
    if (!m_store->load(&m_config, &m_exec))
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
    return initService(m_server,
                       new ServerService(m_config, m_exec, m_procs));
}

bool GremoteApp::startServices()
{
    return m_server->start();
}

void GremoteApp::stopServices()
{
    if (m_server.valid()) m_server->signalStop();
    /* Generous: the accept loop wakes within a quarter second, then drains
     * and joins the live client's Connection. */
    joinOrLeak(m_server, 8000);
}

void GremoteApp::cleanup()
{
    m_server.reset();
    m_store.reset();
    rc_log_mem("exiting");
    rc_log_close();
}

int WINAPI WinMain(HINSTANCE inst, HINSTANCE prev, LPSTR cmd, int show)
{
    (void)prev;
    (void)show;
    UniqueObj<GremoteApp> app(new GremoteApp(inst));
    if (!app.valid())
        return 1;
    int code = app->run(cmd);
    return code == App::RUN_FAILED ? 1 : code;
}
