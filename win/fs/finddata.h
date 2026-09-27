/* Internal to libfs: the bits every enumerator and stat needs.
 *
 * Not part of the library's interface -- no caller outside win/fs/ includes
 * this. It exists because toUnix had started to appear in three translation
 * units, and a timestamp conversion that disagrees with itself between two
 * code paths is the kind of thing that shows up as one file re-syncing
 * forever.
 */
#ifndef RC_FS_FINDDATA_H
#define RC_FS_FINDDATA_H

#ifndef __cplusplus
#error "fs/finddata.h is C++"
#endif

#include <windows.h>

#include "fs/file.h"

/* FILETIME is 100ns since 1601; the unix epoch is 11644473600 seconds later.
 * Only ever compared against timestamps from THIS side -- mtime is a cache
 * key, never an argument about which side is right. */
inline DWORD fsToUnix(const FILETIME &ft)
{
    ULARGE_INTEGER u;
    u.LowPart = ft.dwLowDateTime;
    u.HighPart = ft.dwHighDateTime;
    return (DWORD)((u.QuadPart / 10000000ULL) - 11644473600ULL);
}

inline bool fsIsDotEntry(const char *name)
{
    return name[0] == '.' &&
           (name[1] == '\0' || (name[1] == '.' && name[2] == '\0'));
}

inline void fsFromFindData(const WIN32_FIND_DATAA &fd, FileInfo &info)
{
    info.attrs = fd.dwFileAttributes;
    info.type  = (fd.dwFileAttributes & FILE_ATTRIBUTE_DIRECTORY) ? 'd' : 'f';
    info.size  = fd.nFileSizeLow;
    info.mtime = fsToUnix(fd.ftLastWriteTime);
}

#endif
