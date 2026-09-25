/* retroclaude.exe -- native Windows 98 client for Claude Code.
 *
 * WinMain is the composition root (docs/REMOTE.md, C4-C5a). Log, the
 * single-instance check, common controls and the config run first, because
 * each can end the process before there is anything to stop. Then it builds
 * the services, hands each its dependencies, and App runs their lifecycle.
 * Everything it hands out lives until App has shut down.
 *
 * Build: see Makefile. Target is Win98 SE on a Pentium MMX 200 with 32MB.
 */
#include <windows.h>
#include <commctrl.h>
#include <string.h>

#include "app/config.h"
#include "app/connservice.h"
#include "app/mainwindow.h"
#include "app/oleservice.h"
#include "core/app.h"
#include "core/log.h"
#include "core/path.h"
#include "core/str.h"
#include "net/net.h"
#include "sync/syncrpc.h"

/* POD, so no static constructor runs before WinMain (core/rc_cpp.h). They
 * outlive App, which is deleted before WinMain returns. */
static ClientConfig g_config;
static rc_net g_connection;
static char g_exeDir[MAX_PATH];

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

int WINAPI WinMain(HINSTANCE inst, HINSTANCE prev, LPSTR cmd, int show)
{
    INITCOMMONCONTROLSEX icc;

    (void)prev; (void)cmd;

    /* Log from the very first statement. On this platform "no log file was
     * created" is the primary diagnostic: an NT-only static import kills the
     * process at load, before WinMain, leaving nothing else behind. */
    exeDir(g_exeDir, sizeof(g_exeDir));
    rc_log_open(g_exeDir);
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
        HANDLE once = CreateMutexA(NULL, FALSE, "RetroClaudeSingleInstance");
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
            return 0;
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
    ConfigStore *store = new ConfigStore(g_exeDir);
    if (!store || !store->load(&g_config)) {
        char msg[512];
        rc_strf(msg, sizeof(msg),
                "retroclaude.ini has a setting that cannot be used:\n\n%s\n\n"
                "Fix or remove it and start again.\n\n%s",
                store ? store->error() : "out of memory",
                store ? store->path() : "");
        MessageBoxA(0, msg, RC_TITLE, MB_OK | MB_ICONSTOP);
        delete store;
        rc_log_close();
        return 1;
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
    rc_syncrpc_set_home(g_config.syncRoot);
    if (g_config.syncRoot[0]) {
        /* CREATE IT if it is not there, parents and all. A sync root is a
         * place the user named in the INI, not a place they are asserting
         * already exists -- the first run after setting one has no folder
         * yet, and leaving it missing left synchronisation dead with "guest
         * scan returned no_root", which is a sentence nobody should have to
         * decode to find out that a directory needed making. A typo'd drive
         * still fails, and still says so. */
        if (GetFileAttributesA(g_config.syncRoot) == 0xFFFFFFFF) {
            if (rc_mkpath(g_config.syncRoot))
                rc_log("sync: created %s", g_config.syncRoot);
            else
                rc_log("sync: cannot create %s (%lu); synchronisation will "
                       "not start", g_config.syncRoot,
                       (unsigned long)GetLastError());
        }
        /* No recovery pass here any more. An interrupted commit is staged
         * inside the tree it belongs to, and the trees are now the
         * conversation folders UNDER this root -- rc_workdir_apply recovers
         * each one as it is applied, which is both the right place and the
         * right moment (before the first scan of that tree, so it cannot
         * see the file missing and report a deletion nobody made). */
        rc_log("sync: conversations get their folders under %s",
               g_config.syncRoot);
    }

    /* Registration order is dependency order: OLE before the window that
     * hosts MSHTML, the window before the connection that posts to it.
     * Shutdown runs the other way. addService takes ownership even when it
     * refuses, so each service is allocated only once the one before it is
     * in, and nothing is left unowned on a failure. */
    App *app = App::create(inst);
    bool built = app && app->addService(new OleService(RC_TITLE));
    MainWindow *window = built ? new MainWindow(inst, show, app, &g_config, store,
                                           g_exeDir, &g_connection) : 0;
    built = built && app->addService(window) &&
            app->addMessageFilter(window) &&
            app->addService(new ConnectionService(&g_connection));
    int code = built ? app->run() : App::RUN_FAILED;
    if (!built) rc_log("startup: could not build the application");

    rc_log_mem("exiting");
    delete app;
    delete store;
    rc_log_close();
    return code == App::RUN_FAILED ? 1 : code;
}
