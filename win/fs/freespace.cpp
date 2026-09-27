#include "fs/drives.h"

#include <string.h>

typedef BOOL (WINAPI *PFN_FREEEX)(LPCSTR, PULARGE_INTEGER, PULARGE_INTEGER,
                                  PULARGE_INTEGER);

/* "C:\" -- the volume, not the directory. Both calls want a root. */
static bool volumeRoot(const char *path, char *out)
{
    if (!path || !path[0] || path[1] != ':') return false;
    out[0] = path[0];
    out[1] = ':';
    out[2] = '\\';
    out[3] = '\0';
    return true;
}

bool freeBytes64(const char *pathOrRoot, ULARGE_INTEGER &out)
{
    char root[8];
    HMODULE k32;
    PFN_FREEEX ex;

    out.QuadPart = 0;
    if (!volumeRoot(pathOrRoot, root)) return false;

    k32 = GetModuleHandleA("kernel32.dll");
    ex = k32 ? (PFN_FREEEX)GetProcAddress(k32, "GetDiskFreeSpaceExA") : NULL;
    if (ex)
    {
        ULARGE_INTEGER avail, total, freeb;
        if (ex(root, &avail, &total, &freeb))
        {
            out = avail;
            return true;
        }
    }

    {
        DWORD spc = 0, bps = 0, freec = 0, totalc = 0;
        if (GetDiskFreeSpaceA(root, &spc, &bps, &freec, &totalc))
        {
            /* The OS clamps these counts to about 2GB on a larger volume, so
             * this answer is a floor and not a measurement. It is the
             * fallback for a machine without the Ex call, which by now means
             * an original Win95. */
            out.QuadPart = (unsigned __int64)spc * bps * freec;
            return true;
        }
    }
    return false;
}

bool totalBytes64(const char *pathOrRoot, ULARGE_INTEGER &out)
{
    char root[8];
    HMODULE k32;
    PFN_FREEEX ex;

    out.QuadPart = 0;
    if (!volumeRoot(pathOrRoot, root)) return false;

    k32 = GetModuleHandleA("kernel32.dll");
    ex = k32 ? (PFN_FREEEX)GetProcAddress(k32, "GetDiskFreeSpaceExA") : NULL;
    if (ex)
    {
        ULARGE_INTEGER avail, total, freeb;
        if (ex(root, &avail, &total, &freeb))
        {
            out = total;
            return true;
        }
    }

    {
        DWORD spc = 0, bps = 0, freec = 0, totalc = 0;
        if (GetDiskFreeSpaceA(root, &spc, &bps, &freec, &totalc))
        {
            out.QuadPart = (unsigned __int64)spc * bps * totalc;
            return true;
        }
    }
    return false;
}

unsigned long freeBytesSaturating(const char *pathOrRoot)
{
    ULARGE_INTEGER free_;

    if (!freeBytes64(pathOrRoot, free_)) return 0;
    if (free_.HighPart) return 0xFFFFFFFFUL;
    return free_.LowPart;
}
