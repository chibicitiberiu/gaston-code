/* What this machine is, and asking it to end the session. libexec; libcore
 * only. The numbers are raw: turning them into HELLO headers or an op's
 * reply is the exe's job, so a caller's wire format never lives here.
 *
 * Disk space is libfs's and the screen is libautomation's (REMOTE.md), so
 * neither is here.
 */
#ifndef RC_EXEC_SYSTEM_H
#define RC_EXEC_SYSTEM_H

#ifndef __cplusplus
#error "exec/system.h is C++"
#endif

#include <windows.h>

struct SystemInfo
{
    DWORD memKb;
    DWORD memFreeKb;
    DWORD uptimeMs;         /* GetTickCount: wraps after 49.7 days */
    bool osKnown;           /* GetVersionExA answered */
    DWORD osMajor;
    DWORD osMinor;
    DWORD osBuild;          /* low word only: 9x packs the version above it */
    char osExtra[128];      /* szCSDVersion, e.g. " A" on 98 SE */
    unsigned cpuFamily;     /* wProcessorLevel */
    unsigned cpuModel;      /* high byte of wProcessorRevision */
    DWORD cpuCount;
    unsigned acp;           /* ANSI codepage: file names, window text */
    unsigned oemcp;         /* OEM codepage: what console children write */
};

/* Fills `out`. Kernel32 only, never USER or GDI: safe on gremote's control
 * lane, which must not block behind a hung 16-bit app (REMOTE.md). */
void readSystemInfo(SystemInfo &out);

enum RebootMode
{
    REBOOT_LOGOFF = 0,
    REBOOT_RESTART,
    REBOOT_POWEROFF
};

/* ExitWindowsEx. `force` closes applications without asking them, which can
 * lose a user's unsaved work -- the caller's gate decides, never this.
 * On NT the shutdown privilege is enabled first, through advapi32 resolved
 * at runtime (9x needs none). False if Windows refused; the session is
 * otherwise already ending when this returns. */
bool requestReboot(RebootMode mode, bool force);

#endif
