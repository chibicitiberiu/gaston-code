#include "fs/dir.h"

#include "fs/finddata.h"

#include <string.h>

#include "core/log.h"
#include "core/rc_cpp.h"
#include "core/str.h"


WalkLimits::WalkLimits()
    : maxDepth(16), maxFiles(2000)
{
}

WalkLimits::WalkLimits(unsigned depth, unsigned files)
    : maxDepth(depth), maxFiles(files)
{
}



namespace {

struct WalkState
{
    const char   *root;
    WalkFilter   *filter;
    DirSink      *sink;
    WalkLimits    limits;
    int           count;
    WalkResult    result;
};

} /* namespace */

static void walkInto(WalkState &w, const char *rel, unsigned depth)
{
    Path pattern;
    WIN32_FIND_DATAA fd;

    if (depth > w.limits.maxDepth) return;
    if (w.result != WALK_OK) return;

    if (*rel) pattern.join(w.root, rel);
    else      pattern.set(w.root);

    /* Bounded, and CHECKED. A half-appended pattern either fails (the subtree
     * vanishes) or succeeds on the bare directory (the walk invents
     * directories). The caller has already refused anything that would not
     * fit; this is the assertion that it did. */
    if (!pattern.cat("\\*")) return;

    FindHandle h(FindFirstFileA(pattern.str(), &fd));
    if (!h.valid()) return;

    do
    {
        FileInfo info;
        Path child;

        if (fsIsDotEntry(fd.cFileName)) continue;
        if (w.filter && w.filter->skip(fd.cFileName)) continue;

        /* ALWAYS the long name, never cAlternateFileName. The 8.3 alias is
         * not stable -- deleting one file renumbers ~1/~2 for the others --
         * so keying on it would rename files at random.
         *
         * CHECKED, because `child` feeds joins that reach a DeleteFileA and a
         * MoveFileA. A truncated relative path names a different file.
         * Skipping the entry loses a recovery; acting on a truncated one
         * loses a file. */
        if (*rel)
        {
            child.set(rel);
            child.cat("/");
            child.cat(fd.cFileName);
        }
        else
        {
            child.set(fd.cFileName);
        }
        if (!child.ok()) continue;

        if ((unsigned)w.count >= w.limits.maxFiles)
        {
            w.result = WALK_OVERFLOW;
            return;
        }

        fsFromFindData(fd, info);

        if (info.type == 'd')
        {
            /* Can this directory's own listing pattern even be formed?
             * root + '\' + child + "\*" + NUL. See dir.h for what each way of
             * being one byte out does. */
            if (strlen(w.root) + 1 + strlen(child.str()) + 3 > MAX_PATH)
            {
                rc_log("fs: cannot list %s -- no room for a search pattern",
                       child.str());
                info.type = 'u';
                info.size = 0;
                if (!w.sink->onEntry(child.str(), info))
                {
                    w.result = WALK_STOPPED;
                    return;
                }
                w.count++;
                continue;                       /* and do NOT recurse */
            }
            if (!w.sink->onEntry(child.str(), info))
            {
                w.result = WALK_STOPPED;
                return;
            }
            w.count++;
            walkInto(w, child.str(), depth + 1);
            if (w.result != WALK_OK) return;
        }
        else
        {
            if (!w.sink->onEntry(child.str(), info))
            {
                w.result = WALK_STOPPED;
                return;
            }
            w.count++;
        }
    } while (FindNextFileA(h.get(), &fd));
}

WalkResult walkTree(const char *root, const WalkLimits &limits,
                    WalkFilter *filter, DirSink &sink, int &count)
{
    WalkState w;
    DWORD attr;

    count = 0;
    if (!root || !*root) return WALK_NO_ROOT;

    attr = GetFileAttributesA(root);
    if (attr == 0xFFFFFFFF || !(attr & FILE_ATTRIBUTE_DIRECTORY))
        return WALK_NO_ROOT;

    w.root = root;
    w.filter = filter;
    w.sink = &sink;
    w.limits = limits;
    w.count = 0;
    w.result = WALK_OK;

    walkInto(w, "", 0);

    count = w.count;
    return w.result;
}
