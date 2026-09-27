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

    if (!full || !*full) return false;

    pattern.set(full);
    if (!pattern.cat("\\*")) return false;

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
