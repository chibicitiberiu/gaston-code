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
#include "app/turn.h"
#include "app/sender.h"
#include "app/chatlist.h"

#include "app/title.h"

/* The window class. WinMain's single-instance check finds the running copy
 * by it, so it is named once, here. */
#define RC_MAIN_CLASS "RetroClaudeWnd"

class MainWindow : public Service, public ReconnectListener, public TurnListener,
                   public FrameSender, public ChatListListener
{
public:
    MainWindow(HINSTANCE inst, int show, App *app, ClientConfig *cfg,
               ConfigStore *store, const char *exeDir, rc_net *net);

    const char *name() const;
    bool init();
    bool start();
    bool join(DWORD timeoutMs);

    /* The loop's own keys, in the order they always ran, for the app's
     * preTranslate. True consumes. */
    bool filter(MSG &msg);

    /* ReconnectListener */
    void refreshStatus();
    void restoreCache();

    /* TurnListener */
    void setBusy(bool busy);

    /* ChatListListener */
    bool confirmSwitch();
    void onSwitching();
    void onSwitched(rc_frame *f);

    /* FrameSender */
    bool sendFrame(const char *what, const char *type,
                   const char *const *keys, const char *const *vals,
                   int nkv, const void *body, unsigned bodyLen);

private:
    MainWindow(const MainWindow &);
    MainWindow &operator=(const MainWindow &);

    HINSTANCE m_inst;
    int m_show;
};

#endif
