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

/* The commands on the menu. */
enum { TRAY_CMD_EXIT = 100, TRAY_CMD_SLEEP = 101 };

/* Prevent sleep's nudge: see tray.h, mechanism 4.
 *
 * 2 AND NOT 1, which matters and is not style. gremote's session-end watchdog
 * uses timer 1 on App's window, and GremoteApp::preTranslate consumes WM_TIMER
 * with wParam 1 whenever the shutdown is one we asked for -- it does not look
 * at which window the message came from, because until now only one window in
 * this process had a timer at all. A nudge on timer 1 would be eaten on that
 * path and, worse, read as a watchdog tick.
 *
 * 30 seconds because the shortest standby a 9x Power Management panel offers is
 * one minute, so the interval has to be comfortably inside it; and because the
 * cost is one posted message on a Pentium 200, which is nothing beside the
 * thing this program exists to measure. */
enum { NUDGE_TIMER = 2, NUDGE_MS = 30000 };

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
                         const ServerConfig &cfg, const MachineConfig &machine,
                         const ServerService &server)
    : m_app(&app), m_instance(instance), m_cfg(&cfg), m_machine(&machine),
      m_server(&server), m_notify(0), m_icon(0), m_taskbarCreated(0),
      m_shown(false), m_warned(false), m_preventSleep(false),
      m_heldAwake(false), m_nudged(false), m_denied(0), m_loggedKinds(0),
      m_window(*this)
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
    /* " [awake]" when Prevent sleep is on, and it goes directly after the
     * program name rather than at the end of the line for an arithmetic reason
     * worth writing out, because the comment this replaces had it wrong.
     *
     * Win98's shell takes 63 characters of szTip. At their widest the branches
     * below are: connected 54 (port 65535 from 255.255.255.255) -- which is the
     * number the old comment gave as the maximum -- and LISTENING 57, because
     * `%lu served` is ten digits at its widest and nobody had counted it. With
     * eight more for the marker those become 62 and 65, so the listening branch
     * can in principle be two characters over.
     *
     * That is left as it is rather than contorted, and this is why: it needs
     * more than a hundred thousand connections served since boot before the
     * tenth digit exists at all, rc_strf truncates at the cap and always
     * terminates (core/str.h) so nothing overflows, and the marker LEADS -- so
     * the two characters a theoretical box would lose come off the tail of a
     * connection count, never off the port number or off the marker itself. Put
     * the marker at the end instead and the marker is what disappears, which
     * defeats the whole point of having one. */
    const char *awake = m_preventSleep ? " [awake]" : "";

    switch (m_server->status())
    {
    case ServerService::STATUS_CONNECTED:
        /* The octets by hand. inet_ntoa has one static buffer per process
         * and the accept thread is already a caller of it; a tooltip is
         * not worth a torn string. Network order, so the bytes come out in
         * the order they are printed. */
        rc_strf(out, cap, "%s%s: client from %u.%u.%u.%u, port %d", TRAY_TITLE,
                awake,
                (unsigned)(a & 0xff), (unsigned)((a >> 8) & 0xff),
                (unsigned)((a >> 16) & 0xff), (unsigned)((a >> 24) & 0xff),
                m_server->boundPort());
        break;
    case ServerService::STATUS_LISTENING:
        rc_strf(out, cap, "%s%s: listening on port %d, %lu served", TRAY_TITLE,
                awake, m_server->boundPort(), m_server->served());
        break;
    case ServerService::STATUS_DOWN:
        rc_strf(out, cap, "%s%s: NOT listening on port %ld", TRAY_TITLE,
                awake, m_cfg->port);
        break;
    default:
        rc_strf(out, cap, "%s%s: starting on port %ld", TRAY_TITLE, awake,
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
    /* THE CHECK MARK IS THE STATE AND THE CONTROL, both. A second greyed line
     * saying "sleep is prevented" would cost image for something the tick
     * already says, and the status line above carries "[awake]" for the hover
     * case where no menu is open. remote/tray.h has the four mechanisms. */
    AppendMenuA(menu.get(),
                MF_STRING | (m_preventSleep ? MF_CHECKED : MF_UNCHECKED),
                TRAY_CMD_SLEEP, "Prevent &sleep");
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

    if (cmd == TRAY_CMD_SLEEP)
        setPreventSleep(!m_preventSleep, "the menu");
    else if (cmd == TRAY_CMD_EXIT)
    {
        rc_log("tray: Exit chosen at the keyboard; stopping");
        m_app->quit(0);
    }
}

/* ------------------------------------------------------- preventing sleep */

/* Mechanism 3. True when this machine HAS it and took the call.
 *
 * On the main thread only, and that is not incidental: the execution state is
 * a property of the CALLING THREAD and is released when that thread ends. Every
 * caller here -- init(), the menu, signalStop() -- runs on the thread that owns
 * the message loop, which is the one thread that lives as long as the option
 * does.
 *
 * LoadLibrary rather than an import, because a static import of a symbol 98 may
 * not export kills the image at LOAD with no log (CLAUDE.md, and
 * tools/check/imports.py now names this one). KERNEL32 is already mapped in
 * every process, so this is a refcount and not a disk read, and the pointer
 * stays good after the Dll goes out of scope for the same reason -- the same
 * shape remote/main.cpp uses for RegisterServiceProcess.
 */
bool TrayService::holdSystemAwake(bool on)
{
    typedef DWORD (WINAPI *ExecStateFn)(DWORD);

    Dll k32(LoadLibraryA("KERNEL32.DLL"));
    ExecStateFn set = k32.valid()
        ? (ExecStateFn)GetProcAddress(k32.get(), "SetThreadExecutionState")
        : 0;
    if (!set)
        return false;
    /* ES_SYSTEM_REQUIRED and NOT ES_DISPLAY_REQUIRED. What must stay up is the
     * machine and its network; this box is being worked on over a socket and
     * its monitor may as well go dark, which on a 1998 CRT is a kindness. */
    return set(on ? (ES_CONTINUOUS | ES_SYSTEM_REQUIRED) : ES_CONTINUOUS) != 0;
}

/* Mechanism 4, one event. See remote/tray.h for what is and is not known about
 * whether this does anything on 9x.
 *
 * THE FIRST ONE IS LOGGED AND THE REST ARE NOT, and the line is not decoration.
 * This is the only one of the four mechanisms with NO automated coverage
 * anywhere: it runs only when SetThreadExecutionState is absent, and under Wine
 * -- where every gremote test runs -- that call is present, so the nudge branch
 * is never taken by any test on any machine but the real one. One line saying
 * the timer did fire is what makes it attributable there. Only the first,
 * because 30 seconds apart for days would be the log filling a disk with the
 * same sentence. */
void TrayService::nudge()
{
    mouse_event(MOUSEEVENTF_MOVE, 0, 0, 0, 0);
    if (m_nudged)
        return;
    m_nudged = true;
    rc_log("tray: the first nudge went out, so the timer is running; no more "
           "of these will be logged");
}

void TrayService::setPreventSleep(bool on, const char *why)
{
    /* Idempotent, and that is what keeps the startup call silent on a machine
     * that left the option off: setPreventSleep(false) when it is already
     * false writes nothing. */
    if (on == m_preventSleep)
        return;
    m_preventSleep = on;

    if (on)
    {
        m_heldAwake = holdSystemAwake(true);
        /* The nudge ONLY when there is nothing better. A machine that has
         * SetThreadExecutionState is told properly and never has input faked
         * at it. */
        if (!m_heldAwake && m_window.valid())
            SetTimer(m_window.hwnd(), NUDGE_TIMER, NUDGE_MS, NULL);
        /* Which mechanisms are actually running, by name. The whole point of
         * the line: when this box stays awake, or does not, the next person has
         * to be able to say WHICH of four things was responsible. */
        rc_log("tray: Prevent sleep ON (%s); %s", why,
               !m_window.valid()
                   ? "but this copy has no window, so no suspend query can "
                     "reach it and NOTHING is being refused"
                   : m_heldAwake
                       ? "refusing suspend requests, and "
                         "SetThreadExecutionState is holding the system awake"
                       : "refusing suspend requests, and nudging the input "
                         "queue every 30s because there is no "
                         "SetThreadExecutionState here");
    }
    else
    {
        if (m_window.valid())
            KillTimer(m_window.hwnd(), NUDGE_TIMER);
        /* Only where a hold was actually taken -- which on 9x is never -- so
         * this path does not call LoadLibrary from inside the WM_ENDSESSION
         * handler, where Windows is holding its whole shutdown on our return. */
        if (m_heldAwake)
            holdSystemAwake(false);
        m_heldAwake = false;
        rc_log("tray: Prevent sleep OFF (%s); %lu suspend request%s refused "
               "while it was on", why, m_denied, m_denied == 1 ? "" : "s");
    }
    refreshTip();
}

LRESULT TrayService::onPower(UINT msg, WPARAM code)
{
    /* WM_POWER's codes and WM_POWERBROADCAST's do not share a numbering, so
     * the message has to be part of the decision. Which one it was also goes
     * in the log:
     * which of the two Windows 98 really sends is not documented anywhere, and
     * this is how the real machine tells us. */
    bool legacy = msg == WM_POWER;
    bool query = legacy ? code == PWR_SUSPENDREQUEST
                     : (code == PBT_APMQUERYSUSPEND ||
                        code == PBT_APMQUERYSTANDBY);

    if (query)
    {
        if (!m_preventSleep)
        {
            /* LOGGED EVEN THOUGH THE ANSWER IS YES, and this is the line the
             * whole measurement rests on: leaving a real box idle with
             * prevent_sleep=0 and finding this in GREMOTE.LOG is what proves an
             * idle-timeout standby asks permission at all. Finding nothing
             * proves no veto could ever have helped. */
            rc_log("tray: the machine asks to suspend (%s); Prevent sleep is "
                   "off, so yes", legacy ? "WM_POWER" : "WM_POWERBROADCAST");
            return legacy ? PWR_OK : TRUE;
        }
        m_denied++;
        /* The first of EACH MESSAGE in full, then every eighth refusal. Nothing
         * documents how often 9x re-asks after a refusal, so a line per ask
         * could fill the disk of a machine nobody is watching -- but the limit
         * is per message rather than global on purpose: which of the two
         * Windows 98 really sends is exactly what this log is here to settle,
         * and a global counter would rate-limit the first WM_POWER away. */
        unsigned char kind = (unsigned char)(legacy ? 2 : 1);
        if (!(m_loggedKinds & kind) || (m_denied % 8) == 0)
        {
            m_loggedKinds |= kind;
            rc_log("tray: the machine asks to suspend (%s); REFUSED (%lu so "
                   "far)", legacy ? "WM_POWER" : "WM_POWERBROADCAST", m_denied);
        }
        return legacy ? PWR_FAIL : BROADCAST_QUERY_DENY;
    }

    if (!legacy)
    {
        switch (code)
        {
        case PBT_APMQUERYSUSPENDFAILED:
        case PBT_APMQUERYSTANDBYFAILED:
            /* THE POSITIVE EVIDENCE. Somebody's refusal was accepted and the
             * suspend was abandoned; if m_denied moved, it was ours. */
            rc_log("tray: the suspend was abandoned, so a refusal took (%lu "
                   "of them were ours)", m_denied);
            return TRUE;
        case PBT_APMSUSPEND:
        case PBT_APMSTANDBY:
            /* AND THE NEGATIVE EVIDENCE, which is the more useful of the two:
             * it means the veto either was not asked or was ignored, and that
             * the nudge is the only layer left that can help. */
            rc_log("tray: the machine is suspending%s", m_preventSleep
                       ? " ANYWAY, with Prevent sleep on -- refusing does not "
                         "cover this path on this machine"
                       : "");
            return TRUE;
        case PBT_APMRESUMESUSPEND:
        case PBT_APMRESUMESTANDBY:
        case PBT_APMRESUMECRITICAL:
            /* Worth a line because of what it implies for the socket: a
             * connection that was live before the suspend is gone, and the
             * caller on the other end has to dial again. */
            rc_log("tray: the machine has resumed; any connection it had is "
                   "gone");
            return TRUE;
        }
        /* BATTERYLOW, POWERSTATUSCHANGE and OEMEVENT deliberately say nothing.
         * A machine on a UPS or a laptop can send POWERSTATUSCHANGE often, and
         * none of the three is about suspending. */
    }
    return legacy ? PWR_OK : TRUE;
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
    /* Both power messages, and the window proc is the only place either can be
     * answered: they are SENT and not posted (the sender is waiting for the
     * return value), so they never pass through GetMessage and GremoteApp's
     * preTranslate cannot see them. */
    if (msg == WM_POWERBROADCAST || msg == WM_POWER)
        return m_owner->onPower(msg, wp);
    if (msg == WM_TIMER && wp == NUDGE_TIMER)
    {
        m_owner->nudge();
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
    }
    else
    {
        addIcon();
    }
    /* AFTER the window either way, and called even when there is none: every
     * one of the four mechanisms needs a window or needs to say that it does
     * not have one, and a machine whose ini asked for Prevent sleep must not
     * come up silently without it. */
    setPreventSleep(m_machine->preventSleep, "gremote.ini");
    return true;
}

void TrayService::signalStop()
{
    /* Before the message loop ends, so NIM_DELETE goes out while there is
     * still a process to own it. A tray icon that outlives its process
     * leaves a ghost in the notification area until somebody mouses over
     * it. */
    removeIcon();
    /* AND THE OPTION GOES OFF ON THE WAY OUT, on both teardown paths -- every
     * service is signalled on the session-ending one too (core/app.h), so this
     * always runs. It is after removeIcon so the tooltip refresh inside it
     * finds no icon and does nothing. Nothing here waits: KillTimer returns at
     * once, and the only LoadLibrary is behind a hold this platform never
     * takes.
     *
     * The line it writes is the point. An option that changes how the hardware
     * behaves has to be visible going off as well as coming on, and the count
     * of refused requests is the one number that says whether it did anything
     * at all this run. */
    setPreventSleep(false, "gremote is stopping");
}
