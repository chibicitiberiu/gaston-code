#include "fs/ops.h"
#include "fs/dir.h"
#include "fs/file.h"

#include <windows.h>

const char *fsWhyNoStat()
{
    switch (GetLastError())
    {
    case ERROR_FILE_NOT_FOUND:
    case ERROR_PATH_NOT_FOUND:
    case ERROR_BAD_NETPATH:
    case ERROR_NOT_READY:           /* an empty floppy bay is not a lock */
        return "missing";
    case ERROR_FILENAME_EXCED_RANGE:
    case ERROR_BUFFER_OVERFLOW:
        return "too_long";
    case ERROR_INVALID_NAME:
    case ERROR_INVALID_DRIVE:
        return "bad_path";
    default:
        return "locked";
    }
}

bool fsIsDriveRoot(const char *path)
{
    if (!path || !path[0] || path[1] != ':')
        return false;
    for (int i = 2; path[i]; i++)
        if (path[i] != '\\' && path[i] != '/')
            return false;
    return true;
}

const char *fsRemoveFile(const char *path)
{
    FileInfo info;
    if (!path || !*path)
        return "bad_path";
    if (!statPath(path, info) || info.type == 0)
        return fsWhyNoStat();
    /* Removing a directory is a different operation. Saying so beats
     * deleting the wrong thing. */
    if (info.type == 'd')
        return "is_directory";
    if (!removeFile(path))
        return GetLastError() == ERROR_ACCESS_DENIED ? "readonly" : "locked";
    return 0;
}

const char *fsMakeDirectory(const char *path, bool existingOk)
{
    FileInfo info;
    if (!path || !*path)
        return "bad_path";
    if (statPath(path, info) && info.type != 0)
    {
        if (info.type != 'd')
            return "is_file";
        return existingOk ? 0 : "exists";
    }
    if (!makeDirectory(path))
    {
        switch (GetLastError())
        {
        case ERROR_PATH_NOT_FOUND:      return "no_parent";
        case ERROR_FILENAME_EXCED_RANGE:
        case ERROR_BUFFER_OVERFLOW:     return "too_long";
        case ERROR_INVALID_NAME:
        case ERROR_INVALID_DRIVE:       return "bad_path";
        default:                        return "locked";
        }
    }
    return 0;
}

/* A sink that stops at the first entry: listDirectory already skips "." and
 * "..", so any callback at all means there is something real in there. */
class AnyEntry : public DirSink
{
public:
    AnyEntry() : m_found(false) {}
    bool onEntry(const char *, const FileInfo &)
    {
        m_found = true;
        return false;
    }
    bool found() const { return m_found; }

private:
    bool m_found;
};

/* Why RemoveDirectoryA refused.
 *
 * MEASURED on Windows 98 SE, 2026-10-01: a directory holding one file
 * refused with ERROR_ACCESS_DENIED, not ERROR_DIR_NOT_EMPTY -- the latter is
 * an NT-era code this platform does not appear to send. Trusting the number
 * alone answered `readonly` for the most likely failure. The same run found
 * a directory with the read-only attribute SET removed without complaint,
 * so there is no read-only-directory case to name either. So ACCESS_DENIED
 * asks the disk which case it actually is. */
static const char *whyNoRmdir(const char *path)
{
    DWORD err = GetLastError();
    switch (err)
    {
    case ERROR_DIR_NOT_EMPTY:
        return "not_empty";
    case ERROR_ACCESS_DENIED:
    {
        AnyEntry probe;
        listDirectory(path, probe);
        return probe.found() ? "not_empty" : "denied";
    }
    case ERROR_CURRENT_DIRECTORY:
        /* Defensive, not measured: Win32 documents it, 98 was not seen
         * sending it. */
        return "is_cwd";
    case ERROR_FILENAME_EXCED_RANGE:
    case ERROR_BUFFER_OVERFLOW:
        return "too_long";
    case ERROR_INVALID_NAME:
    case ERROR_INVALID_DRIVE:
        return "bad_path";
    default:
        return "locked";
    }
}

const char *fsRemoveDirectory(const char *path)
{
    FileInfo info;
    if (!path || !*path)
        return "bad_path";
    if (fsIsDriveRoot(path))
        return "drive_root";
    if (!statPath(path, info) || info.type == 0)
        return fsWhyNoStat();
    if (info.type != 'd')
        return "is_file";
    if (!RemoveDirectoryA(path))
        return whyNoRmdir(path);
    return 0;
}

const char *fsMovePath(const char *from, const char *to)
{
    FileInfo info;
    if (!from || !*from || !to || !*to)
        return "bad_path";
    if (fsIsDriveRoot(from))
        return "drive_root";
    if (!statPath(from, info) || info.type == 0)
        return fsWhyNoStat();
    /* MoveFileA refuses an existing target, and that refusal is the useful
     * one: a move that silently replaced something would be a delete nobody
     * asked for. Checked first so the answer says so. */
    if (statPath(to, info) && info.type != 0)
        return "exists";
    if (!movePath(from, to))
    {
        switch (GetLastError())
        {
        case ERROR_NOT_SAME_DEVICE:      return "cross_volume";
        case ERROR_PATH_NOT_FOUND:       return "no_parent";
        case ERROR_FILENAME_EXCED_RANGE: return "too_long";
        case ERROR_INVALID_NAME:         return "bad_path";
        case ERROR_ACCESS_DENIED:        return "denied";
        default:                         return "locked";
        }
    }
    return 0;
}
