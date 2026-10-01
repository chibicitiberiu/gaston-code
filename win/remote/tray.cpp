/* TrayService. See tray.h for the three things this has to survive. */
#include "remote/tray.h"

#include <shellapi.h>
#include <string.h>

#include "core/log.h"
#include "core/str.h"

/* The shell's callback, in core/'s own block of private messages
 * (tools/check/msgids.py keeps these unique across win/). */
#define WM_RC_TRAY (WM_APP + 0x400)

/* One icon, one id. */
enum { TRAY_ICON_ID = 1 };

/* The only command on the menu. */
enum { TRAY_CMD_EXIT = 100 };

static const char TRAY_CLASS[] = "GastonRemoteTray";
static const char TRAY_TITLE[] = "Gaston Remote";

/* WHY A STOCK ICON AND NOT A RESOURCE.
 *
 * gremote has no .rc and no resources, unlike the client, and giving it one
 * is not free: a WRC rule, a .res the link has to bind, a dependency list
 * that goes stale quietly, and an artwork file in the image. What the icon
 * has to communicate is "this process is alive" -- the answer to the
 * question that prompted it -- and a stock icon answers that as well as a
 * drawn one, with the tooltip and the menu carrying everything specific.
 *
 * LoadImage rather than LoadIcon because LoadIcon hands back the 32x32 and
 * the shell then shrinks it to SM_CXSMICON itself, which on 9x looks like
 * exactly what it is. LR_SHARED means the system owns the handle: nothing
 * to destroy, and nothing taken off the USER heap, which on a 32MB guest is
 * the scarcer of the two reasons.
 *
 * If this ever wants gremote's own artwork, the cheap path is resources/'s
 * existing .ico plus a two-line gremote.rc -- not a second icon file.
 */
static HICON stockIcon()
{
    HICON icon = (HICON)LoadImageA(NULL, IDI_APPLICATION, IMAGE_ICON,
                                   GetSystemMetrics(SM_CXSMICON),
                                   GetSystemMetrics(SM_CYSMICON),
                                   LR_SHARED | LR_DEFAULTCOLOR);
    if (!icon)
        icon = LoadIconA(NULL, IDI_APPLICATION);
    return icon;
}

TrayService::TrayService(App &app, HINSTANCE instance,
                         const ServerConfig &cfg, const ServerService &server)
    : m_app(&app), m_instance(instance), m_cfg(&cfg), m_server(&server),
      m_notify(0), m_icon(0), m_taskbarCreated(0), m_shown(false),
      m_warned(false), m_window(*this)
{
    m_tip[0] = '\0';
}

TrayService::~TrayService()
{
    /* Both idempotent, and in this order: the icon refers to the window.
     * Explicit rather than left to the members, because ~Window runs the
     * window's WM_DESTROY with the derived part of the object already gone
     * (core/window.h says so), and the icon must come off before that. */
    removeIcon();
    m_window.destroy();
}

bool TrayService::openShell()
{
    if (m_notify)
        return true;
    if (!m_shell32.valid())
        m_shell32.reset(LoadLibraryA("SHELL32.DLL"));
    if (!m_shell32.valid())
        return false;
    m_notify = (NotifyFn)GetProcAddress(m_shell32.get(), "Shell_NotifyIconA");
    if (!m_notify)
        closeShell();
    return m_notify != 0;
}

void TrayService::closeShell()
{
    m_notify = 0;
    m_shell32.reset();
}

bool TrayService::notify(DWORD message, const char *tip)
{
    NOTIFYICONDATAA nid;

    if (!m_notify || !m_window.valid())
        return false;

    ZeroMemory(&nid, sizeof nid);
    /* V1, not sizeof(nid). The header's struct is whatever NTDDI_VERSION
     * makes it (szTip[128], state, balloon text), and a shell that predates
     * those fields validates cbSize and refuses anything it does not
     * recognise. NOTIFYICONDATAA_V1_SIZE is the 88 bytes Win98's shell
     * knows, and nothing past szTip[64] is touched below. */
    nid.cbSize = NOTIFYICONDATAA_V1_SIZE;
    nid.hWnd = m_window.hwnd();
    nid.uID = TRAY_ICON_ID;
    if (message != NIM_DELETE)
    {
        nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP;
        nid.uCallbackMessage = WM_RC_TRAY;
        nid.hIcon = m_icon;
        if (tip)
            rc_strcpy(nid.szTip, 64, tip);
    }
    return m_notify(message, &nid) != FALSE;
}

/* ONE line for both the tooltip and the menu, deliberately.
 *
 * It was two -- a short tooltip and a longer menu line -- and the second
 * one cost 300-odd bytes of image for a sentence nobody reads twice. The
 * cap that matters is the tooltip's: Win98's shell takes 63 characters of
 * szTip and silently drops the rest, so every branch below is written to
 * fit at its widest (port 65535 from 255.255.255.255 is 54 characters).
 */
void TrayService::describe(char *out, unsigned cap) const
{
    unsigned long a = m_server->peerAddr();

    switch (m_server->status())
    {
    case ServerService::STATUS_CONNECTED:
        /* The octets by hand. inet_ntoa has one static buffer per process
         * and the accept thread is already a caller of it; a tooltip is
         * not worth a torn string. Network order, so the bytes come out in
         * the order they are printed. */
        rc_strf(out, cap, "%s: client from %u.%u.%u.%u, port %d", TRAY_TITLE,
                (unsigned)(a & 0xff), (unsigned)((a >> 8) & 0xff),
                (unsigned)((a >> 16) & 0xff), (unsigned)((a >> 24) & 0xff),
                m_server->boundPort());
        break;
    case ServerService::STATUS_LISTENING:
        rc_strf(out, cap, "%s: listening on port %d, %lu served", TRAY_TITLE,
                m_server->boundPort(), m_server->served());
        break;
    case ServerService::STATUS_DOWN:
        rc_strf(out, cap, "%s: NOT listening on port %ld", TRAY_TITLE,
                m_cfg->port);
        break;
    default:
        rc_strf(out, cap, "%s: starting on port %ld", TRAY_TITLE,
                m_cfg->port);
        break;
    }
}

void TrayService::addIcon()
{
    char tip[64];

    if (m_shown)
        return;
    if (!openShell())
    {
        if (!m_warned)
            rc_log("tray: no Shell_NotifyIcon on this machine; running "
                   "without an icon");
        m_warned = true;
        return;
    }
    describe(tip, sizeof tip);
    if (notify(NIM_ADD, tip))
    {
        rc_strcpy(m_tip, sizeof m_tip, tip);
        m_shown = true;
        /* Worth a line: "there is an icon" and "there is no icon" are the
         * two things somebody reading this log after a reboot wants to
         * know, and only one of them is otherwise visible. */
        rc_log("tray: icon added to the notification area");
        return;
    }
    /* No notification area: RunServices runs before anyone logs on, so this
     * is the ordinary case on a cold boot and NOT an error. One line, once,
     * and then the library goes back -- SHELL32 pulls four more DLLs in
     * behind it and there is no point holding them for a shell that is not
     * running. TaskbarCreated brings us back here when it starts. */
    if (!m_warned)
        rc_log("tray: no notification area yet (no shell is running); the "
               "icon will appear when the taskbar does");
    m_warned = true;
    closeShell();
}

void TrayService::removeIcon()
{
    if (m_shown)
        notify(NIM_DELETE, 0);
    m_shown = false;
    closeShell();
}

void TrayService::refreshTip()
{
    char tip[64];

    if (!m_shown)
        return;
    describe(tip, sizeof tip);
    /* Only when it actually changed: the shell sends WM_MOUSEMOVE for every
     * pixel the pointer crosses over the icon. */
    if (strcmp(tip, m_tip) == 0)
        return;
    if (notify(NIM_MODIFY, tip))
        rc_strcpy(m_tip, sizeof m_tip, tip);
}

void TrayService::showMenu()
{
    POINT pt;
    char line[64];
    int cmd;

    MenuGuard menu(CreatePopupMenu());
    if (!menu.valid())
        return;

    /* The greyed first item IS the answer to "what is it doing" -- it
     * already names the program, so there is no separate title item. */
    describe(line, sizeof line);
    AppendMenuA(menu.get(), MF_STRING | MF_GRAYED | MF_DISABLED, 0, line);
    AppendMenuA(menu.get(), MF_SEPARATOR, 0, NULL);
    /* EXIT IS NOT THE REMOTE QUIT, and nobody should "fix" this by adding
     * one. docs/REMOTE.md omits a QUIT op on purpose -- "stopping gremote
     * remotely is EXEC of something that kills it, which is deliberate
     * friction" -- and that friction is aimed at the CALLER on the other
     * end of the socket, who could otherwise strand the machine with one
     * frame and no way back. The person at the keyboard is in front of the
     * box; they can already pull the power. Making them hunt for the
     * process in a task list instead of clicking Exit protects nothing. */
    AppendMenuA(menu.get(), MF_STRING, TRAY_CMD_EXIT, "E&xit");

    GetCursorPos(&pt);
    /* The two calls around TrackPopupMenu are the documented workaround
     * (MS KB135788) for a tray menu that will not go away when the next
     * click lands outside it: the owner window is not foreground, so the
     * menu never learns it lost activation. SetForegroundWindow first, a
     * posted no-op after. */
    SetForegroundWindow(m_window.hwnd());
    cmd = (int)TrackPopupMenu(menu.get(),
                              TPM_RIGHTBUTTON | TPM_RETURNCMD | TPM_NONOTIFY,
                              pt.x, pt.y, 0, m_window.hwnd(), NULL);
    PostMessageA(m_window.hwnd(), WM_NULL, 0, 0);

    if (cmd == TRAY_CMD_EXIT)
    {
        rc_log("tray: Exit chosen at the keyboard; stopping");
        m_app->quit(0);
    }
}

/* ------------------------------------------------------------ TrayWindow */

bool TrayService::TrayWindow::make(HINSTANCE instance)
{
    /* WS_EX_TOOLWINDOW keeps it out of Alt-Tab; it is never shown, so it
     * never reaches the taskbar either. A real top-level window and not
     * HWND_MESSAGE, which is NT 5.0+ and would not receive the shell's
     * broadcast in any case. */
    return Window::registerClass(instance, TRAY_CLASS, 0) &&
           create(WS_EX_TOOLWINDOW, TRAY_CLASS, TRAY_TITLE, WS_POPUP,
                  0, 0, 0, 0, 0, 0, instance);
}

LRESULT TrayService::TrayWindow::handle(UINT msg, WPARAM wp, LPARAM lp)
{
    if (msg == WM_RC_TRAY && wp == TRAY_ICON_ID)
    {
        switch (lp)
        {
        case WM_MOUSEMOVE:
            /* The tooltip is only ever rebuilt here, which is the whole of
             * why this service needs no timer. */
            m_owner->refreshTip();
            return 0;
        case WM_LBUTTONUP:
        case WM_RBUTTONUP:
            /* Left as well as right: there is no window to open, so a
             * left click has nothing better to do than show the state. */
            m_owner->showMenu();
            return 0;
        }
        return 0;
    }
    /* Explorer's broadcast: the taskbar has just been created, either at
     * logon (gremote was already up, started from RunServices) or because
     * Explorer crashed and restarted. Either way the icon we had is gone
     * with the old taskbar, so this re-adds from scratch. */
    if (m_owner->m_taskbarCreated && msg == m_owner->m_taskbarCreated)
    {
        rc_log("tray: the taskbar appeared; adding the icon");
        m_owner->m_shown = false;
        m_owner->m_warned = false;
        m_owner->addIcon();
        return 0;
    }
    return Window::handle(msg, wp, lp);
}

/* ----------------------------------------------------------- the service */

bool TrayService::init()
{
    /* Registered before the window exists, so the first broadcast after
     * CreateWindow is already understood. Zero means the shell does not do
     * this (Win95 without IE4), which is only a missed re-add. */
    m_taskbarCreated = RegisterWindowMessageA("TaskbarCreated");
    if (!m_taskbarCreated)
        rc_log("tray: TaskbarCreated is not a known message here; the icon "
               "will not come back if Explorer restarts");

    m_icon = stockIcon();
    if (!m_window.make(m_instance))
    {
        /* Not fatal. The server is the program; the icon is a courtesy,
         * and refusing to start a test box because a hidden window would
         * not create would be exactly the wrong trade. */
        rc_log("tray: no window, so no icon; the server is unaffected");
        return true;
    }
    addIcon();
    return true;
}

void TrayService::signalStop()
{
    /* Before the message loop ends, so NIM_DELETE goes out while there is
     * still a process to own it. A tray icon that outlives its process
     * leaves a ghost in the notification area until somebody mouses over
     * it. */
    removeIcon();
}
