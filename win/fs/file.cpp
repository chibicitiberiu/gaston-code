#include "fs/file.h"

#include "fs/finddata.h"

#include "core/path.h"
#include "core/rc_cpp.h"


FileInfo::FileInfo()
    : type(0), size(0), mtime(0), attrs(0)
{
}

bool statPath(const char *full, FileInfo &out)
{
    WIN32_FIND_DATAA fd;
    DWORD attr;

    out = FileInfo();
    if (!full || !*full) return false;

    attr = GetFileAttributesA(full);
    if (attr == 0xFFFFFFFF) return false;

    out.attrs = attr;
    if (attr & FILE_ATTRIBUTE_DIRECTORY)
    {
        out.type = 'd';
        return true;
    }
    out.type = 'f';

    /* Size and time come from the directory entry, not from opening the file:
     * this must answer for a file somebody else holds under a share-deny
     * lock. FindFirstFileA refuses a bare drive root, which is a directory
     * and has already returned above. */
    {
        FindHandle h(FindFirstFileA(full, &fd));
        if (h.valid())
        {
            out.size  = fd.nFileSizeLow;
            out.mtime = fsToUnix(fd.ftLastWriteTime);
        }
    }
    return true;
}

bool removeFile(const char *full)
{
    DWORD attr;

    if (!full || !*full) return false;

    attr = GetFileAttributesA(full);
    if (attr != 0xFFFFFFFF && (attr & FILE_ATTRIBUTE_READONLY))
        SetFileAttributesA(full, attr & ~FILE_ATTRIBUTE_READONLY);

    return DeleteFileA(full) != 0;
}

bool movePath(const char *from, const char *to)
{
    if (!from || !*from || !to || !*to) return false;
    return MoveFileA(from, to) != 0;
}

bool makeDirectory(const char *full)
{
    DWORD attr;
    DWORD err;

    if (!full || !*full)
    {
        SetLastError(ERROR_INVALID_NAME);
        return false;
    }

    if (rc_mkpath(full)) return true;

    /* Captured BEFORE the confirmation below, and restored after it.
     * GetFileAttributesA sets its own last-error, so asking "well, is it
     * there anyway?" destroys the reason the caller needs to answer with --
     * and those reasons are not interchangeable: a missing drive, a path too
     * long, a full disk and a file already sitting at the name all arrive
     * here and all mean different things to whoever asked. */
    err = GetLastError();
    attr = GetFileAttributesA(full);
    if (attr != 0xFFFFFFFF && (attr & FILE_ATTRIBUTE_DIRECTORY)) return true;
    SetLastError(err);
    return false;
}
