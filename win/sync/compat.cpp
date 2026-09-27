/* The sync layer's C names for things that now live in libfs.
 *
 * TEMPORARY, and deliberately its own translation unit so its removal is a
 * deletion rather than an edit. win/checks/synccheck.c and win/checks/
 * rpccheck.c are C programs that call these by name; they are ported (or
 * dropped) at the end of step 5a, and this file goes with them.
 *
 * The suffixes and the reserve stay on this side because they are declared to
 * the bridge in HELLO and asserted against net/caps.h -- libfs takes them as a
 * parameter and has no opinion about what they are.
 */
#include "sync/sync.h"
#include "fs/path.h"

static WorkNames syncWorkNames()
{
    return WorkNames(RC_SYNC_TMP_SUFFIX, RC_SYNC_OLD_SUFFIX,
                     RC_SYNC_WORK_SUFFIX, true);
}

extern "C" int rc_sync_contained(const char *rel)
{
    return isContainedRelative(rel) ? 1 : 0;
}

extern "C" int rc_sync_is_working_name(const char *name)
{
    return isWorkingName(name, syncWorkNames()) ? 1 : 0;
}
