/* Listing a directory, and walking a tree. libfs; libcore only.
 *
 * Entries go to a sink as they are found. Nothing is collected: 2,000 entries
 * at MAX_PATH each is 500KB on a machine with tens of megabytes, and the
 * callers all want to do something per entry anyway.
 */
#ifndef RC_FS_DIR_H
#define RC_FS_DIR_H

#ifndef __cplusplus
#error "fs/dir.h is C++"
#endif

#include <windows.h>

#include "fs/file.h"

class DirSink
{
public:
    virtual ~DirSink() {}

    /* `rel` uses '/' separators and is relative to the walk's root; for a
     * single-directory listing it is the bare name. False stops the walk. */
    virtual bool onEntry(const char *rel, const FileInfo &info) = 0;
};

/* Names a walk must not report. The writer's own working files are the case
 * this exists for: an interrupted write otherwise leaves a file the next
 * manifest reports as ordinary, the bridge has never seen it, so it pulls it,
 * and both sides hold a working file that syncs forever. */
class WalkFilter
{
public:
    virtual ~WalkFilter() {}
    virtual bool skip(const char *name) = 0;
};

struct WalkLimits
{
    unsigned maxDepth;
    unsigned maxFiles;

    WalkLimits();
    WalkLimits(unsigned depth, unsigned files);
};

enum WalkResult
{
    WALK_OK = 0,
    WALK_NO_ROOT,     /* the root is missing, or is not a directory */
    WALK_OVERFLOW,    /* maxFiles reached; the listing is INCOMPLETE */
    WALK_STOPPED      /* the sink said stop */
};

/* Walk `root` depth-first, emitting every entry below it.
 *
 * WALK_OVERFLOW is a distinct result and not a short listing, because a
 * truncated manifest is indistinguishable from a mass deletion: a caller that
 * treats it as success tells the bridge the user deleted everything past the
 * cap. `count` is filled either way.
 *
 * Directories are emitted before their contents. A directory whose own search
 * pattern would not fit in MAX_PATH is emitted as 'u' and NOT descended into
 * -- both silent alternatives are worse. One byte short and the pattern loses
 * its wildcard, FindFirstFileA fails, and the entire subtree vanishes from the
 * listing. Exactly full and the wildcard is dropped, FindFirstFileA succeeds
 * on the bare directory, and the walk recurses on the same truncated pattern
 * to maxDepth -- inventing a chain of directories the bridge then creates.
 */
WalkResult walkTree(const char *root, const WalkLimits &limits,
                    WalkFilter *filter, DirSink &sink, int &count);

/* One level, no recursion, no caps. `.` and `..` are never emitted. */
bool listDirectory(const char *full, DirSink &sink);

#endif
