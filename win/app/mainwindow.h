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
#include "core/window.h"
#include "app/config.h"
#include "net/net.h"
#include "app/reconnect.h"
#include "app/turn.h"
#include "app/sender.h"
#include "app/chatlist.h"
#include "app/input.h"
#include "app/bar.h"
#include "app/pillbar.h"
#include "app/optmenu.h"
#include "app/workdlg.h"

#include "app/title.h"

/* The children MainWindow holds by pointer; their headers stay out of this
 * one. */
class rc_html;
class rc_xferstat;
class rc_toolbar;
class rc_export;
class rc_cache;
class rc_pagebar;
class rc_statusbar;

/* The window class. WinMain's single-instance check finds the running copy
 * by it, so it is named once, here. */
#define RC_MAIN_CLASS "GastonWnd"

class MainWindow : public Service, public Window, public ReconnectListener, public TurnListener,
                   public FrameSender, public ChatListListener,
                   public ComposeListener, public PermissionBarListener,
                   public AttachmentBarListener, public OptionMenusListener,
                   public WorkDirProposer
{
public:
    MainWindow(HINSTANCE inst, int show, App *app, ClientConfig *cfg,
               ConfigStore *store, const char *exeDir, rc_net *net);
    ~MainWindow();

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

    /* ComposeListener */
    bool isSendBlocked();
    void onSent();

    /* PermissionBarListener */
    void onBarChanged();

    /* AttachmentBarListener */
    void onAttachmentsChanged();

    /* OptionMenusListener */
    bool submitCommand(const char *text, bool compose);

    /* WorkDirProposer */
    void proposeWorkDir(const char *path, bool staged);

    /* FrameSender */
    bool sendFrame(const char *what, const char *type,
                   const char *const *keys, const char *const *vals,
                   int nkv, const void *body, unsigned bodyLen);

protected:
    LRESULT handle(UINT msg, WPARAM wp, LPARAM lp);

private:
    MainWindow(const MainWindow &);
    MainWindow &operator=(const MainWindow &);

    void buildMenu(HWND hwnd);
    void refreshEnables();
    void onReady(rc_frame *f);
    void onDoc(rc_frame *f);
    void onText(rc_frame *f);
    void onTool(rc_frame *f);
    void onTurn(rc_frame *f);
    void onOptions(rc_frame *f);
    void onChats(rc_frame *f);
    void onPageBar(rc_frame *f);
    void onAnchor(rc_frame *f);
    void onBar(rc_frame *f);
    void onError(rc_frame *f);
    void onNotice(rc_frame *f);
    void onStatus(rc_frame *f);
    void onExport(rc_frame *f);
    void onPong(rc_frame *f);
    void onPrefsAck(rc_frame *f);
    void renderFrame(rc_frame *f);
    void layout(HWND hwnd);
    void createChildren(HWND hwnd, HINSTANCE inst);
    void handleAttachPath(HWND hwnd, const char *path);

    HINSTANCE m_inst;
    int m_show;
    /* Owned by the app and handed in; they outlive this window. */
    ClientConfig *m_cfg;
    ConfigStore *m_store;
    App *m_app;
    rc_net *m_net;
    /* The children, created in WM_CREATE and deleted in WM_DESTROY. */
    rc_html *m_html;
    PermissionBar *m_bar;
    ChatList *m_chats;
    rc_xferstat *m_xfer;
    ComposeBox *m_input;
    AttachmentBar *m_attachments;
    rc_toolbar *m_toolbar;
    rc_export *m_export;
    rc_cache *m_cache;
    int m_cacheShown;
    HACCEL m_accel;
    OptionMenus *m_options;
    rc_pagebar *m_pageBar;
    int m_announceConnected;
    rc_statusbar *m_status;
    Reconnector *m_reconnector;
    Turn *m_turn;
    HFONT m_uiFont;
    HFONT m_uiFontBold;
    const char *m_exeDir;
};

#endif
