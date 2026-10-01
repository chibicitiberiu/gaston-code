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
 * nothing at all. The ONE exception is Prevent sleep's nudge below, and it
 * exists only while that option is on, which is never by default.
 *
 * ---------------------------------------------------------------------------
 *
 * AND PREVENT SLEEP, WHICH IS ALSO HERE. It looks like a second subject in one
 * file and it is not quite: this is the half of gremote that faces the PERSON
 * AND THE MACHINE rather than the socket -- the icon, the menu, and now
 * whether the box is allowed to put itself to sleep while somebody is working
 * on it over the wire. It lives here rather than in its own file for one more
 * reason that is not aesthetic: a `WM_POWERBROADCAST` veto needs a top-level
 * window to arrive at, this class already owns one, and a new translation unit
 * costs ~2,144 bytes of `-xs` exception machinery before a line of its own code
 * (win/Makefile, gremote's budget note).
 *
 * THE PROBLEM. A real Windows 98 box with APM goes into standby on its idle
 * timer, the network goes with it, and every connection gremote was holding
 * drops. `[machine] prevent_sleep=1` in gremote.ini, or Prevent sleep on the
 * menu, stops that. OFF by default: it changes how the hardware behaves, and
 * an install that predates the option must come up exactly as it did before.
 *
 * FOUR MECHANISMS, IN THE ORDER OF HOW WELL THEY ARE DOCUMENTED FOR 9x,
 * because no one of them covers the whole of it on this platform. Two refuse a
 * suspend that is being proposed; two try to stop one being proposed at all.
 *
 * 1. THE VETO. `WM_POWERBROADCAST` / `PBT_APMQUERYSUSPEND`, answered with
 *    `BROADCAST_QUERY_DENY`. The Win32 Programmer's Reference (the Win95/NT4
 *    edition) marks `WM_POWERBROADCAST` "Windows NT Yes, Win95 Yes" with no
 *    platform note, and `PBT_APMQUERYSUSPEND` "Windows NT: No, Win95: Yes" --
 *    so this is a 9x facility rather than something retrofitted, which the
 *    modern documentation confirms from the other end by saying support was
 *    REMOVED IN VISTA. `SetSystemPowerState`'s own page describes the
 *    mechanism: it asks every application, and if any one denies, it sends
 *    `PBT_APMQUERYSUSPENDFAILED` and gives up. The constants are all behind
 *    `WINVER >= 0x0400`, which is what this build pins.
 *
 *    `PBT_APMQUERYSTANDBY` is handled with it and has never been observed. The
 *    Win95-era event list does not contain it, and the modern page says the
 *    STANDBY family "are not supported on any of the operating systems
 *    specified" -- on 9x, standby IS the suspend. It costs one case label.
 *
 * 2. THE OLD VETO, and it is here because of what IS documented about it.
 *    `WM_POWER` / `PWR_SUSPENDREQUEST`, answered `PWR_FAIL`. Microsoft calls
 *    it obsolete -- "provided to simplify porting of 16-bit Windows-based
 *    applications" -- and marks it Win95 Yes all the same, and says returning
 *    `PWR_FAIL` "prevent[s] the system from entering the suspended state". The
 *    reason to answer it as well is the delivery set: `WM_POWER`'s page says
 *    the power-management driver sends it to EACH WINDOW RETURNED BY
 *    EnumWindows, and `appcheck`'s `app-window-is-top-level` case already
 *    proves this process's hidden windows are in exactly that set.
 *    `WM_POWERBROADCAST`'s delivery set is nowhere documented. So mechanism 2
 *    is the one whose arrival here can be argued from the documentation, and
 *    mechanism 1 is the one Microsoft tells new code to use. Answering both
 *    costs one more case and settles which of them this platform really sends,
 *    because the log names the message.
 *
 *    WHAT NEITHER OF THEM SETTLES: whether 9x asks at all for an IDLE-TIMEOUT
 *    standby, which is the case the person at the keyboard actually hits. The
 *    best evidence that it does is Intel's 1997 "USB - APM Interactions" paper
 *    on Win95 OSR2.1 -- VPOWERD polls the APM BIOS, the OS then asks drivers
 *    and applications, and "device drivers cannot distinguish between suspend
 *    requests coming from the user... those generated by the system as a
 *    result of 'system inactivity', and battery low suspend requests" -- plus
 *    the fact that Windows 2000 works that way. NO MICROSOFT SOURCE SAYS IT OF
 *    WINDOWS 98. The QEMU guest cannot answer it either: it has NO APM at all,
 *    which is why `REBOOT mode=poweroff` there degrades to a logoff
 *    (docs/measurements/gremote-shutdown-fix-vm-20261001.txt).
 *
 *    So this code LOGS EVERY POWER EVENT IT SEES, EVEN WHEN THE OPTION IS OFF,
 *    and the real machine's GREMOTE.LOG is the instrument. Leaving a box idle
 *    with `prevent_sleep=0` and reading the log back is the clean measurement:
 *    a query line means an idle standby does ask, and silence means it does
 *    not and no veto can ever help. `PBT_APMSUSPEND` after a refusal is the
 *    line that says the veto was asked and ignored.
 *
 *    ONE VISIBLE CONSEQUENCE, worth knowing before turning it on: Intel's
 *    paper says 9x puts up a "some application or device has rejected the
 *    suspend" message when a request is denied. So pressing Standby at this
 *    keyboard with the option on gets a complaint rather than silence. That is
 *    the option working, and it is why it is off by default.
 *
 * 3. `SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)`, which is
 *    the right answer everywhere it exists and MAY NOT EXIST HERE. Microsoft
 *    documents a minimum of Windows XP and says nothing about 9x -- but the
 *    Windows 2000 Application Specification makes the OnNow requirement
 *    "indicate busy application status properly" mandatory for WINDOWS 98 as
 *    well as 2000, and names this call as the way to do it, so Microsoft
 *    expected a 98 program to have something. Whether 98's KERNEL32 exports it
 *    could not be established from any document, and `probe power` on the real
 *    machine answers it in seconds.
 *
 *    Resolved and never imported, for the usual reason: a single NT-only
 *    static import kills the image at LOAD, before WinMain, with no log
 *    (CLAUDE.md). `tools/check/imports.py` now names it so an edit that
 *    reaches for it directly is stopped by `make check` rather than by a guest
 *    that will not start. When it IS there it REPLACES mechanism 4, because
 *    faking input on a machine somebody is sitting at is a last resort and not
 *    a default.
 *
 * 4. THE NUDGE, and it is the crude one. A zero-delta
 *    `mouse_event(MOUSEEVENTF_MOVE, 0, 0, ...)` on a timer, to put an event
 *    through the input queue and reset whatever idle timer feeds standby.
 *    MOVE with zero delta and nothing else: the pointer does not move, nothing
 *    is clicked, and -- unlike `keybd_event` -- it cannot type into whatever
 *    window the person at this keyboard has focused, which is exactly why a
 *    keystroke is not used.
 *
 *    WHAT COULD NOT BE ESTABLISHED, and it is said here rather than implied:
 *    NO Microsoft document says synthetic input resets a 9x idle timer, and
 *    there is no 9x "user activity" call (`SystemIdleTimerReset` is Windows CE
 *    only). The claim is 9x-era practice. Intel's paper is the reason to
 *    doubt it as well as the reason to try: APM activity is "typically"
 *    measured in HARDWARE, by interrupt and IO assertions, and input that
 *    never reaches a PS/2 controller may never reach a BIOS-side idle monitor
 *    -- though APM 1.2 also lets the OS own the timeouts, in which case it
 *    would. It is last, it is only used when mechanism 3 is absent, and the
 *    log says it is running so an outcome can be attributed to it.
 *
 * NOT DONE, AND DELIBERATELY: `SystemParametersInfo` with
 * `SPI_SETLOWPOWERACTIVE` / `SPI_SETPOWEROFFACTIVE` /
 * `SPI_SETSCREENSAVEACTIVE`. Three reasons, and the first alone is enough.
 *
 *   They MUTATE A PERSISTENT SYSTEM SETTING. This option is a temporary thing
 *   and nobody should have to put the machine back by hand afterwards, so a
 *   restore would have to survive not only the menu and a clean exit but a
 *   crash, a power cut and the abandoned-service session-end path -- and it
 *   cannot. A box left permanently unable to go into standby, with nothing on
 *   screen to say why, is a worse bug than the one being fixed.
 *
 *   They are aimed at the WRONG THING. Both are documented as the low-power
 *   and power-off phases of SCREEN SAVING -- the monitor -- and no Microsoft
 *   page connects either to the APM system standby that takes the network down
 *   with it. (The modern page even says to use GUID_VIDEO_POWERDOWN_TIMEOUT
 *   instead, which is a display timeout.)
 *
 *   And the platform wording does not hold up. The Win95-era reference marks
 *   the four GET forms "not supported for 32-bit applications on Windows NT or
 *   Windows 95", and the SET forms "Windows 95 only" with no such qualifier --
 *   an inconsistency that means a 32-bit program cannot even reliably READ the
 *   value it would have to put back.
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
                const MachineConfig &machine, const ServerService &server);
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

    /* Turn the whole option on or off, log it, and put the tooltip right.
     * `why` names who asked -- the ini at startup, the menu, or the stop --
     * because "it is on" and "somebody turned it on" are different lines to
     * read back afterwards. Idempotent. */
    void setPreventSleep(bool on, const char *why);
    /* The answer to one WM_POWERBROADCAST or one WM_POWER -- both, because
     * which of them 9x really sends is not documented and the log names it.
     * Logs what arrived EVEN WHEN THE OPTION IS OFF: on the real machine this
     * log is the only evidence of whether an idle standby asks permission at
     * all, and that measurement has to be takeable without changing how the
     * box behaves. */
    LRESULT onPower(UINT msg, WPARAM code);
    /* Mechanism 3. True if it was there and took the call, which on 9x it
     * never is. Resolved, never imported (see the header comment). */
    bool holdSystemAwake(bool on);
    /* Mechanism 2, one event. */
    void nudge();
    /* What the server is doing, in one line, for the tooltip AND the menu.
     * The shell caps szTip at 63 characters here, so every branch of it is
     * written to fit. */
    void describe(char *out, unsigned cap) const;

    App *m_app;
    HINSTANCE m_instance;
    const ServerConfig *m_cfg;
    const MachineConfig *m_machine;
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
    /* Prevent sleep, live. Starts at the ini's value and the menu moves it. */
    bool m_preventSleep;
    /* SetThreadExecutionState took the hold, so there is one to release.
     * False on 9x, always -- which is also what keeps the release path from
     * calling LoadLibrary from inside the WM_ENDSESSION handler. */
    bool m_heldAwake;
    /* The first nudge has been logged. See TrayService::nudge for why exactly
     * one of them is, and why that one line matters more than it looks. */
    bool m_nudged;
    /* Suspend requests refused since startup. Counted rather than logged every
     * time: nothing says how often 9x re-asks after a refusal, and a line per
     * ask could fill the disk of a machine nobody is watching. */
    unsigned long m_denied;
    /* Which MESSAGES have had a refusal logged: bit 0 WM_POWERBROADCAST, bit 1
     * WM_POWER. The rate limit is per message and not global, because which of
     * the two Windows 98 actually sends is the thing this is here to find out
     * -- with one counter, a WM_POWER arriving after eight broadcasts would be
     * silently rate-limited away and the answer lost. */
    unsigned char m_loggedKinds;
    TrayWindow m_window;
    char m_tip[64];
};

#endif
