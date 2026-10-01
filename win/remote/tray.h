/* gremote's notification-area icon: the only thing on the screen that says
 * this process is alive.
 *
 * WHY IT EXISTS. gremote has no window, by design (remote/main.cpp says
 * why), and a headless server that has crashed looks exactly like one that
 * is working -- the only way to tell has been to open gremote.log in
 * Notepad. An icon in the tray answers "is it up" at a glance, and its menu
 * answers "what is it doing" without a text editor.
 *
 * THREE THINGS THIS HAS TO SURVIVE, none of them hypothetical:
 *
 * 1. NO SHELL AT ALL. gremote autostarts from RunServices, which runs
 *    BEFORE anyone logs on -- measured on the guest, five processes and no
 *    EXPLORER. There is no notification area then, Shell_NotifyIcon returns
 *    FALSE, and that must be a shrug: the server starts anyway and the icon
 *    appears later. Later is the shell's own broadcast, the registered
 *    message "TaskbarCreated", which Explorer sends to every top-level
 *    window when the taskbar comes up -- at logon, and again every time
 *    Explorer is restarted after a crash. So the window is a plain hidden
 *    top-level popup and not a message-only window: HWND_MESSAGE is
 *    NT 5.0+, and a message-only window would not be in the broadcast set
 *    even if it existed here.
 *
 * 2. NO SHELL32 IMPORT. Shell_NotifyIcon lives in SHELL32.DLL, which is not
 *    on tools/check/imports.py's allow-list, and the rule in CLAUDE.md is
 *    that a new static import is a deliberate act with a dependency behind
 *    it. It is resolved with LoadLibrary/GetProcAddress instead, the same
 *    way exec/system.cpp takes ADVAPI32 -- and with a second benefit that
 *    matters more here than the gate does: when there is no shell, the
 *    library is let go again immediately rather than held resident for the
 *    whole pre-logon life of the process. On a 32MB guest, SHELL32 and the
 *    four DLLs behind it are not a rounding error.
 *
 * 3. ITS OWN DEATH. A tray icon whose process is gone leaves a ghost in the
 *    notification area until somebody mouses over it, so NIM_DELETE happens
 *    in signalStop() and again in the destructor, both idempotent.
 *
 * NO TIMER, DELIBERATELY. This program runs BESIDE the thing being measured
 * on a Pentium 200 -- the Makefile's budget comment makes that point about
 * memory and it is just as true of wakeups. So nothing polls: the tooltip
 * is rebuilt when the shell tells us the pointer is over the icon, and the
 * menu text is built when the menu is opened. An idle tray icon costs
 * nothing at all.
 */
#ifndef RC_REMOTE_TRAY_H
#define RC_REMOTE_TRAY_H

#ifndef __cplusplus
#error "remote/tray.h is C++"
#endif

#include <windows.h>

#include "core/app.h"
#include "core/rc_cpp.h"
#include "core/window.h"
#include "remote/config.h"
#include "remote/server.h"

class TrayService : public Service
{
public:
    /* `app` is held only to quit() from the menu; App::instance() is
     * protected, so the composition root passes the HINSTANCE in. `server`
     * is read for its status only, through the four scalars ServerService
     * exposes for exactly this. */
    TrayService(App &app, HINSTANCE instance, const ServerConfig &cfg,
                const ServerService &server);
    ~TrayService();

    const char *name() const { return "tray"; }
    bool init();
    void signalStop();

private:
    TrayService(const TrayService &);
    TrayService &operator=(const TrayService &);

    /* Shell_NotifyIconA, resolved rather than imported. */
    typedef BOOL (WINAPI *NotifyFn)(DWORD, void *);

    /* The callback needs a window, and App's hidden one is App's private
     * business (core/app.h), so the tray brings its own. */
    class TrayWindow : public Window
    {
    public:
        explicit TrayWindow(TrayService &owner) : m_owner(&owner) {}
        bool make(HINSTANCE instance);

    protected:
        LRESULT handle(UINT msg, WPARAM wp, LPARAM lp);

    private:
        TrayService *m_owner;
    };
    friend class TrayWindow;

    /* True once SHELL32 is loaded and Shell_NotifyIconA is in hand. */
    bool openShell();
    void closeShell();
    bool notify(DWORD message, const char *tip);

    void addIcon();
    void removeIcon();
    void refreshTip();
    void showMenu();
    /* What the server is doing, in one line, for the tooltip AND the menu.
     * The shell caps szTip at 63 characters here, so every branch of it is
     * written to fit. */
    void describe(char *out, unsigned cap) const;

    App *m_app;
    HINSTANCE m_instance;
    const ServerConfig *m_cfg;
    const ServerService *m_server;
    Dll m_shell32;
    NotifyFn m_notify;
    /* A stock icon, shared: LoadImage with LR_SHARED hands back one the
     * system owns, so there is nothing to destroy and nothing on our USER
     * heap. See the note in tray.cpp about why gremote has no .rc. */
    HICON m_icon;
    UINT m_taskbarCreated;
    bool m_shown;
    bool m_warned;
    TrayWindow m_window;
    char m_tip[64];
};

#endif
