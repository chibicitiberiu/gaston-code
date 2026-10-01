/* listDirectory, in its own translation unit.
 *
 * Nothing in the client lists a single directory -- the sync layer walks whole
 * trees and the agent has `dir` -- so keeping it apart from walkTree keeps it
 * out of gaston.exe's image. gremote's LS is its caller.
 */
#include "fs/dir.h"

#include "fs/finddata.h"

#include "core/rc_cpp.h"

bool listDirectory(const char *full, DirSink &sink)
{
    Path pattern;
    WIN32_FIND_DATAA fd;
    unsigned n;
    char last;

    if (!full || !*full) return false;

    /* A drive root arrives as "C:\" with its separator already on it.
     * Appending another gave "C:\\*", which FindFirstFileA refuses, and the
     * refusal came back from a real 98 machine as `LS C:\ -> 0 entries` --
     * an empty listing that reads like an empty disk. */
    pattern.set(full);
    n = pattern.len();
    last = n ? pattern.str()[n - 1] : '\0';
    if (!pattern.cat(last == '\\' || last == '/' ? "*" : "\\*"))
    {
        /* A name with no room left for its wildcard, not a directory with
         * nothing in it. Said in the error so the caller can tell which. */
        SetLastError(ERROR_FILENAME_EXCED_RANGE);
        return false;
    }

    FindHandle h(FindFirstFileA(pattern.str(), &fd));
    if (!h.valid()) return false;

    do
    {
        FileInfo info;

        if (fsIsDotEntry(fd.cFileName)) continue;
        fsFromFindData(fd, info);
        if (!sink.onEntry(fd.cFileName, info)) break;
    } while (FindNextFileA(h.get(), &fd));

    return true;
}
