/* gaston.exe -- native Windows 98 client for Claude Code.
 *
 * ClientApp is the composition root (docs/REMOTE.md, C4-C5a): App runs the
 * lifecycle and ClientApp fills in each phase. initialize() does what can
 * end the process before there is anything to stop -- log, the
 * single-instance check, common controls, the config. initializeServices()
 * then creates the services in dependency order, each handed what it needs
 * through its constructor, and stopServices()/cleanup() take them down in
 * reverse. WinMain only makes the object and runs it.
 *
 * Build: see Makefile. Target is Win98 SE on a Pentium MMX 200 with 32MB.
 */
#include <windows.h>
#include <commctrl.h>
#include <string.h>

#include <string>

#include "app/config.h"
#include "app/connservice.h"
#include "ui/uiservice.h"
#include "app/machineid.h"
#include "app/mainwindow.h"
#include "app/oleservice.h"
#include "app/protect.h"
#include "core/app.h"
#include "core/log.h"
#include "core/path.h"
#include "core/str.h"
#include "net/caps.h"
#include "net/net.h"
#include "sync/syncrpc.h"


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

/* Owns what the services are handed, and the services themselves. Heap
 * allocated by WinMain: a file-scope object would construct before WinMain
 * and the log, and a local would put it all in one stack frame. */
class ClientApp : public App
{
public:
    ClientApp(HINSTANCE instance, int show);

protected:
    bool initialize();
    bool initializeServices();
    bool startServices();
    bool preTranslate(MSG &msg);
    void stopServices();
    void cleanup();

private:
    int m_show;
    char m_exeDir[MAX_PATH];
    ClientConfig m_config;
    rc_net m_net;
    UniqueObj<ConfigStore> m_store;
    /* Which machine this is, for HELLO. A plain member and not a service:
     * it has no thread, nothing to start or stop, and it is read once. It
     * still lives on the app rather than in a file static, so its lifetime
     * is the app's (docs/REMOTE.md, C4-C5a). */
    MachineId m_machine;
    /* The PREFS body the link points at (net.h: not copied). A member, so
     * it lives exactly as long as the link that may send it. */
    std::string m_prefs;
    /* Services, in dependency order: OLE before the window that hosts
     * MSHTML, the window before the connection that posts to it. */
    UniqueObj<OleService> m_ole;
    UniqueObj<MainWindow> m_window;
    UniqueObj<ConnectionService> m_connection;
    /* Last, because it depends on nothing but the net, and stopped FIRST:
     * it drives other people's applications and should let go of them
     * before anything else starts tearing down. */
    UniqueObj<UiService> m_ui;
};

ClientApp::ClientApp(HINSTANCE instance, int show)
    : App(instance), m_show(show)
{
    /* rc_net and ClientConfig are C structs that used to be zero-filled
     * statics. */
    m_exeDir[0] = '\0';
    memset(&m_config, 0, sizeof m_config);
    memset(&m_net, 0, sizeof m_net);
}

bool ClientApp::initialize()
{
    INITCOMMONCONTROLSEX icc;

    /* Log from the very first statement. On this platform "no log file was
     * created" is the primary diagnostic: an NT-only static import kills the
     * process at load, before WinMain, leaving nothing else behind. */
    exeDir(m_exeDir, sizeof(m_exeDir));
    rc_log_open(m_exeDir, "gaston");
    rc_log("os version: %08lx", GetVersion());

    /* One instance. A second one is not a harmless duplicate: the bridge holds
     * ONE session and refuses a second client with `busy` -- so the new window
     * sits there saying "Cannot reach ... retrying" while the old one, often
     * behind it, has the conversation. That looked like a network fault for a
     * while, and it also explains a run of duplicated "Connected to bridge"
     * lines that had been put down to reconnect churn.
     *
     * A named mutex rather than FindWindow alone, because the window does not
     * exist yet during the seconds this actually matters -- two launches in
     * quick succession. FindWindow then raises the one that is already there,
     * so a second launch behaves like clicking the taskbar button. */
    {
        HANDLE once = CreateMutexA(NULL, FALSE, "GastonSingleInstance");
        if (once && GetLastError() == ERROR_ALREADY_EXISTS) {
            HWND other = FindWindowA(RC_MAIN_CLASS, NULL);
            rc_log("another instance is running; raising it and exiting");
            /* IsWindow, because FindWindow can hand back a window that is
             * already being destroyed.
             *
             * The window outlives the mutex during teardown -- the handle is
             * released when the process exits, and the class is unregistered
             * after WM_DESTROY -- so relaunching immediately after closing
             * (which is exactly what a ship-and-test loop does) lands here
             * with a HWND that is on its way out. It does not close the race,
             * nothing here can; it closes the window where we would act on a
             * handle already known to be dead. */
            if (other && IsWindow(other)) {
                if (IsIconic(other)) ShowWindow(other, SW_RESTORE);
                SetForegroundWindow(other);
            }
            CloseHandle(once);
            return exitWith(0);
        }
        /* Deliberately never closed: the handle IS the lock, and it is
         * released when the process exits, however it exits. */
    }

    memset(&icc, 0, sizeof(icc));
    icc.dwSize = sizeof(icc);
    /* ICC_BAR_CLASSES covers the toolbar, status bar, trackbar and tooltip.
     * It does NOT cover the progress bar -- that is ICC_PROGRESS_CLASS, and
     * without it CreateWindow on PROGRESS_CLASS fails at the moment a
     * transfer first wants to show progress, which is Phase 5's attachment
     * upload. One word now against a failure that would present as "the
     * progress bar does not appear" on a machine with no debugger. */
    icc.dwICC = ICC_BAR_CLASSES | ICC_PROGRESS_CLASS;
    InitCommonControlsEx(&icc);

    /* A value present but wrong refuses startup, naming the key, rather
     * than connecting somewhere nobody configured (docs/REMOTE.md, C3). */
    m_store.reset(new ConfigStore(m_exeDir));
    if (!m_store.valid() || !m_store->load(&m_config)) {
        char msg[512];
        rc_strf(msg, sizeof(msg),
                "gaston.ini has a setting that cannot be used:\n\n%s\n\n"
                "Fix or remove it and start again.\n\n%s",
                m_store.valid() ? m_store->error() : "out of memory",
                m_store.valid() ? m_store->path() : "");
        MessageBoxA(0, msg, RC_TITLE, MB_OK | MB_ICONSTOP);
        return false;
    }
    /* The configured root, which is NOT the same thing as the tree being
     * mirrored. Nothing is mirrored at startup and nothing is mirrored while
     * a chat is only being talked to: sharing begins when a conversation
     * commits to a folder, which is the moment its first tool runs. So the
     * live root (rc_syncrpc_set_root / rc_rpc_set_sync_root) stays empty
     * here, and app/workdir.cpp sets it when the bridge names a tree.
     *
     * This one is still recorded, because it is what HELLO reports and what
     * a conversation's folder gets created under. */
    rc_syncrpc_set_home(m_config.syncRoot);
    if (m_config.syncRoot[0]) {
        /* CREATE IT if it is not there, parents and all. A sync root is a
         * place the user named in the INI, not a place they are asserting
         * already exists -- the first run after setting one has no folder
         * yet, and leaving it missing left synchronisation dead with "guest
         * scan returned no_root", which is a sentence nobody should have to
         * decode to find out that a directory needed making. A typo'd drive
         * still fails, and still says so. */
        if (GetFileAttributesA(m_config.syncRoot) == 0xFFFFFFFF) {
            if (rc_mkpath(m_config.syncRoot))
                rc_log("sync: created %s", m_config.syncRoot);
            else
                rc_log("sync: cannot create %s (%lu); synchronisation will "
                       "not start", m_config.syncRoot,
                       (unsigned long)GetLastError());
        }
        /* No recovery pass here any more. An interrupted commit is staged
         * inside the tree it belongs to, and the trees are now the
         * conversation folders UNDER this root -- rc_workdir_apply recovers
         * each one as it is applied, which is both the right place and the
         * right moment (before the first scan of that tree, so it cannot
         * see the file missing and report a deletion nobody made). */
        rc_log("sync: conversations get their folders under %s",
               m_config.syncRoot);
    }
    return true;
}

bool ClientApp::initializeServices()
{
    if (!initService(m_ole, new OleService(RC_TITLE)))
        return false;

    /* AFTER OLE and BEFORE the connection. CoCreateGuid lives in OLE32, and
     * this is the first point at which that library is certainly up; the
     * connection service is what eventually calls rc_net_start, which is
     * what sends the id. A failure in here is not a failure to start: the
     * client runs without an identity and says so in the log. */
    m_machine.load(m_exeDir);
    rc_net_set_machine(&m_net, m_machine.id());

    /* WHETHER THE AGENT MAY DRIVE THIS SCREEN, from the INI and declared at
     * the handshake rather than assumed (BACKLOG.md 19). Set before the
     * connection service exists, so the very first HELLO carries it -- the
     * bridge builds the tool list from it, and a capability that arrived
     * after the handshake would arrive after the session it has to shape. */
    rc_net_set_automation(&m_net, m_config.automation ? RC_AUTOMATION_STEPS
                                                      : RC_AUTOMATION_NONE);

    /* THE CLIENT'S SETTINGS, declared at every handshake (bridge/prefs.py).
     * An empty body is a real answer -- "every default" -- and still makes
     * the bridge wait for it, which is what keeps a future setting from
     * arriving after the session it has to shape. */
    m_prefs = rc_prefs_body(m_config, m_exeDir);
    rc_log("prefs: %u bytes declared", (unsigned)m_prefs.size());
    rc_net_set_prefs(&m_net, m_prefs.c_str(), (unsigned)m_prefs.size());

    return initService(m_window, new MainWindow(instance(), m_show, this,
                                                &m_config, m_store.get(),
                                                m_exeDir, &m_net)) &&
           initService(m_connection, new ConnectionService(&m_net)) &&
           initService(m_ui, new UiService(&m_net));
}

bool ClientApp::startServices()
{
    return m_ole->start() && m_window->start() && m_connection->start() &&
           m_ui->start();
}

bool ClientApp::preTranslate(MSG &msg)
{
    return m_window.valid() && m_window->filter(msg);
}

void ClientApp::stopServices()
{
    /* Signal all, then join each, in reverse. The connection stops first,
     * while the window its reader posts frames to still exists; OLE goes
     * last, after the window has released MSHTML.
     *
     * THE 5000s ARE NOT THE REAL BUDGET and that is the point of passing the
     * App. ConnectionService::join ignores the timeout and hands the job to
     * rc_net_stop, which times itself: 30s for the RPC worker, 2s of drain,
     * 5s each for the reader and the sender. MainWindow::join ignores it too
     * and runs WM_DESTROY, which tears MSHTML down. Inside WM_ENDSESSION that
     * is up to about three quarters of a minute of Windows 98 shutdown held
     * on this window procedure, with no desktop left to show the End Task
     * dialog on -- so on a session end joinOrLeak abandons each service
     * unjoined instead (core/app.h). Nothing the person typed is lost by it:
     * the settings are saved when they change, not here. */
    if (m_ui.valid()) m_ui->signalStop();
    if (m_connection.valid()) m_connection->signalStop();
    if (m_window.valid()) m_window->signalStop();
    if (m_ole.valid()) m_ole->signalStop();
    joinOrLeak(m_ui, *this, 5000);
    joinOrLeak(m_connection, *this, 5000);
    joinOrLeak(m_window, *this, 5000);
    joinOrLeak(m_ole, *this, 5000);
}

void ClientApp::cleanup()
{
    m_ui.reset();
    m_connection.reset();
    m_window.reset();
    m_ole.reset();
    m_store.reset();
    rc_log_mem("exiting");
    rc_log_close();
}

int WINAPI WinMain(HINSTANCE inst, HINSTANCE prev, LPSTR cmd, int show)
{
    (void)prev;
    /* runApp, not run(): on a session end the services were abandoned with
     * their threads live, and those threads reach into this object's rc_net
     * and config. runApp leaks it rather than destroying it under them
     * (core/app.h). */
    UniqueObj<ClientApp> app(new ClientApp(inst, show));
    return runApp(app, cmd);
}
