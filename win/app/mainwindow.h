/* The main window as a service: its class, the window and its children
 * (WM_CREATE), accelerators, and the keys the message loop handles before
 * dispatch. Destroying it in join() runs WM_DESTROY, which deletes the
 * children.
 *
 * Built by WinMain (app/main.cpp), which owns everything passed in and keeps
 * it alive for as long as App runs.
 */
#ifndef RC_MAINWINDOW_H
#define RC_MAINWINDOW_H

#ifndef __cplusplus
#error "app/mainwindow.h is C++"
#endif

#include <windows.h>
#include "core/app.h"
#include "app/config.h"
#include "net/net.h"
#include "app/reconnect.h"

/* Not "Claude Code": that is Anthropic's product name and this is a
 * third-party client for it. Gaston is a French given name of roughly the
 * right vintage for the machine it runs on. */
#define RC_TITLE "Gaston Code"

/* The window class. WinMain's single-instance check finds the running copy
 * by it, so it is named once, here. */
#define RC_MAIN_CLASS "RetroClaudeWnd"

class MainWindow : public Service, public MessageFilter,
                   public ReconnectListener
{
public:
    MainWindow(HINSTANCE inst, int show, App *app, ClientConfig *cfg,
               ConfigStore *store, const char *exeDir, rc_net *net);

    const char *name() const;
    bool init();
    bool start();
    bool join(DWORD timeoutMs);

    /* The loop's own keys, in the order they always ran. True consumes. */
    bool filter(MSG &msg);

    /* ReconnectListener */
    void refreshStatus();
    void restoreCache();

private:
    MainWindow(const MainWindow &);
    MainWindow &operator=(const MainWindow &);

    HINSTANCE m_inst;
    int m_show;
};

#endif
