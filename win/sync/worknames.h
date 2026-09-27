/* The working names the sync layer forms beside a target.
 *
 * Here rather than in libfs because these are declared to the bridge in HELLO
 * (`workreserve`) and asserted against net/caps.h -- they are this protocol's
 * contract, not a filesystem constant. libfs takes them as a parameter.
 */
#ifndef RC_SYNC_WORKNAMES_H
#define RC_SYNC_WORKNAMES_H

#ifndef __cplusplus
#error "sync/worknames.h is C++"
#endif

#include "fs/path.h"
#include "sync/sync.h"

inline WorkNames syncWorkNames()
{
    /* aside = true: the sync root is swept by rc_sync_recover at startup, so
     * the two-move window is recoverable here and nowhere else. */
    return WorkNames(RC_SYNC_TMP_SUFFIX, RC_SYNC_OLD_SUFFIX,
                     RC_SYNC_WORK_SUFFIX, true);
}

#endif
