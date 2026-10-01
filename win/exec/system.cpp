#include "exec/system.h"

#include <string.h>
#include "core/rc_cpp.h"
#include "core/log.h"
#include "core/str.h"
#include "exec/cpuid.h"

void readSystemInfo(SystemInfo &out)
{
    memset(&out, 0, sizeof out);

    MEMORYSTATUS ms;
    memset(&ms, 0, sizeof ms);
    ms.dwLength = sizeof ms;
    GlobalMemoryStatus(&ms);
    out.memKb = ms.dwTotalPhys / 1024;
    out.memFreeKb = ms.dwAvailPhys / 1024;
    out.uptimeMs = GetTickCount();

    OSVERSIONINFOA os;
    memset(&os, 0, sizeof os);
    os.dwOSVersionInfoSize = sizeof os;
    if (GetVersionExA(&os))
    {
        out.osKnown = true;
        out.osMajor = os.dwMajorVersion;
        out.osMinor = os.dwMinorVersion;
        out.osBuild = os.dwBuildNumber & 0xFFFF;
        rc_strcpy(out.osExtra, sizeof out.osExtra, os.szCSDVersion);
    }

    SYSTEM_INFO si;
    memset(&si, 0, sizeof si);
    GetSystemInfo(&si);
    out.cpuFamily = si.wProcessorLevel;
    out.cpuModel = si.wProcessorRevision >> 8;
    out.cpuCount = si.dwNumberOfProcessors;
    /* Both may legitimately come back empty -- a 386 has no CPUID at all and
     * a Pentium has no brand string -- so neither is checked. The caller
     * composes whatever it got; `memset` above already made them empty. */
    cpuBrand(out.cpuBrandName, sizeof out.cpuBrandName);
    cpuVendor(out.cpuVendorName, sizeof out.cpuVendorName);

    out.acp = GetACP();
    out.oemcp = GetOEMCP();
}

/* NT refuses ExitWindowsEx without SE_SHUTDOWN_NAME enabled; 9x has no
 * privileges at all. advapi32 is resolved rather than imported, so linking
 * this costs the client no new import. */
static void enableShutdownPrivilege()
{
    if (GetVersion() & 0x80000000)
        return;                             /* 9x */
    typedef BOOL (WINAPI *OpenTokenFn)(HANDLE, DWORD, PHANDLE);
    typedef BOOL (WINAPI *LookupFn)(LPCSTR, LPCSTR, PLUID);
    typedef BOOL (WINAPI *AdjustFn)(HANDLE, BOOL, PTOKEN_PRIVILEGES, DWORD,
                                    PTOKEN_PRIVILEGES, PDWORD);
    Dll adv(LoadLibraryA("ADVAPI32.DLL"));
    if (!adv.valid())
        return;
    OpenTokenFn openToken =
        (OpenTokenFn)GetProcAddress(adv.get(), "OpenProcessToken");
    LookupFn lookup =
        (LookupFn)GetProcAddress(adv.get(), "LookupPrivilegeValueA");
    AdjustFn adjust =
        (AdjustFn)GetProcAddress(adv.get(), "AdjustTokenPrivileges");
    if (!openToken || !lookup || !adjust)
        return;
    HANDLE tokenH = NULL;
    if (!openToken(GetCurrentProcess(),
                   TOKEN_ADJUST_PRIVILEGES | TOKEN_QUERY, &tokenH))
        return;
    Handle token(tokenH);
    TOKEN_PRIVILEGES tp;
    memset(&tp, 0, sizeof tp);
    if (!lookup(NULL, "SeShutdownPrivilege", &tp.Privileges[0].Luid))
        return;
    tp.PrivilegeCount = 1;
    tp.Privileges[0].Attributes = SE_PRIVILEGE_ENABLED;
    adjust(token.get(), FALSE, &tp, 0, NULL, NULL);
}

bool requestReboot(RebootMode mode, bool force)
{
    UINT how;
    switch (mode)
    {
    case REBOOT_LOGOFF:   how = EWX_LOGOFF;   break;
    case REBOOT_RESTART:  how = EWX_REBOOT;   break;
    case REBOOT_POWEROFF: how = EWX_POWEROFF; break;
    default:
        return false;
    }
    if (force)
        how |= EWX_FORCE;
    enableShutdownPrivilege();
    rc_log("system: ExitWindowsEx(%u)", how);
    if (!ExitWindowsEx(how, 0))
    {
        rc_log("system: ExitWindowsEx refused: %lu", GetLastError());
        return false;
    }
    return true;
}
