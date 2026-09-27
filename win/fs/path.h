/* Path rules for the filesystem library. libfs; libcore only.
 *
 * Nothing here touches the disk. These are the refusals that have to happen
 * BEFORE a path reaches Win32, because by then a wrong answer is a wrong file.
 */
#ifndef RC_FS_PATH_H
#define RC_FS_PATH_H

#ifndef __cplusplus
#error "fs/path.h is C++"
#endif

#include <windows.h>

/* The names a writer forms beside its target, and what it does with the old
 * file. Supplied by the caller rather than fixed here: the sync layer's
 * `.gaston-tmp`/`.gaston-old` pair is declared to the bridge in HELLO and is
 * not libfs's to choose, and the three writers in this tree want three
 * different policies.
 *
 * `aside` is the one that matters. Moving the old file aside and renaming the
 * new one over it means no instant has no file at that path -- but it leaves a
 * second name behind if the process dies between the two moves, and that is
 * only safe where something sweeps up afterwards. The sync root has such a
 * sweep (rc_sync_recover, at startup). An arbitrary directory on the guest
 * does not and never will, so a writer pointed outside the mirror must take
 * `aside = false` and accept the smaller window instead: on failure the
 * original is untouched, and there is nothing to recover.
 */
struct WorkNames
{
    const char *tmpSuffix;   /* e.g. ".gaston-tmp" */
    const char *oldSuffix;   /* e.g. ".gaston-old"; ignored when !aside */
    unsigned    reserve;     /* chars to keep free for the longest of them */
    bool        aside;       /* move the old file aside before renaming in */

    /* Every member set: C++98 does not default-initialise a non-POD. */
    WorkNames();
    WorkNames(const char *tmp, const char *old, unsigned res, bool moveAside);
};

/* Is `rel` a relative path with no way out of its root?
 *
 * Refuses absolute paths, drive specs, empty components, "." and ".." -- by
 * inspection, never by resolving, because resolving means deciding what a path
 * means and refusing means the question never arises.
 *
 * BOTH separators are split on. The wire format says components are
 * '/'-separated and '\' is illegal inside one, but that is a rule the BRIDGE
 * enforces, and this function exists for the case where the bridge is the
 * thing that is wrong. It cannot lean on a guarantee made by the party it is
 * checking. (`a\..\..\x` once passed here as one nine-character component.)
 */
bool isContainedRelative(const char *rel);

/* Non-zero if `name` is one of the writer's own working names. A walk must
 * skip these: an interrupted write otherwise leaves a file the next manifest
 * reports as ordinary, the bridge has never seen it, so it pulls it, and both
 * sides hold a working file that syncs forever. */
bool isWorkingName(const char *name, const WorkNames &names);

/* Would `<root>\<rel>` still leave room for a working name beside it, in a
 * buffer of `cap` chars?
 *
 * MEASURED FROM THE INPUTS, never from the joined string. The join truncates
 * silently, so a length test on its OUTPUT is a test on the evidence the
 * truncation already destroyed. See the suffix reserve in sync.h for what a
 * truncated working name does: at some lengths it reports success having
 * written nothing, and at others it destroys the user's file outright.
 */
bool fitsWithReserve(const char *root, const char *rel, unsigned cap,
                     const WorkNames &names);

#endif
