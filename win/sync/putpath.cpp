/* The SYNCPUT body's path parser, in a translation unit of its own.
 *
 * It belongs beside the handler that calls it, and it is not there, because
 * tests/guest/synccheck drives it directly: syncrpc.h pulls in net.h and
 * frameproto.h, so linking that handler into a console test binary for one
 * path parser would drag the whole net layer, libwire and Winsock in with it,
 * and put that binary under the import gate for nothing.
 *
 * ONE parser either way. The thing the tests run is the thing that runs.
 */
#include <string.h>
#include <stdlib.h>

#include <windows.h>

#include "core/log.h"
#include "core/str.h"
#include "fs/path.h"
#include "sync/sync.h"

/* Parse a SYNCPUT body's length-prefixed path into a bounded, validated
 * relative path. One function, so the thing that runs is the thing the tests
 * run -- `synccheck --putpath` drives THIS, over bodies with real file
 * content glued to the tail.
 *
 * The body is "<len> <rel>" followed IMMEDIATELY by up to 8KB of the file's
 * own bytes, with no separator (bridge/files/synctransport.py builds it as
 * prefix + piece). Everything here follows from that:
 *
 *   - the separator must actually be present, or `body + plen + 1` is one
 *     past the frame allocation and every read after it walks adjacent heap
 *     (Win98 has no guard page behind a small block, so it does not fault);
 *   - the copy is bounded by the bridge's `<len>`, which is unbounded wire
 *     data, and its return is checked -- a silently cut path is a DIFFERENT
 *     file, and it goes on to rc_sync_begin_write and rc_sync_commit, which
 *     delete and rename;
 *   - containment is checked on the BOUNDED COPY, never on the raw pointer.
 *     Checking the pointer scanned the path AND the file data as one string,
 *     so any push whose first chunk held `//` -- an empty component, and the
 *     line-comment token of half the languages the agent writes -- came back
 *     bad_path forever, naming the one thing that was not wrong with it.
 *
 * Returns RC_PUTPATH_OK, or one of the refusals below. (audit 01.2/01.6/01.30)
 */
int rc_syncrpc_put_path(const char *body, unsigned body_len,
                        char *out, unsigned cap, unsigned *data_off)
{
    unsigned plen = 0, rn;
    const char *rel;

    if (data_off) *data_off = 0;
    if (!body || !out || !cap) return RC_PUTPATH_NOSEP;
    out[0] = '\0';

    while (plen < 16 && plen < body_len && body[plen] && body[plen] != ' ')
        plen++;
    if (plen >= body_len || body[plen] != ' ') return RC_PUTPATH_NOSEP;

    rn = (unsigned)atoi(body);
    rel = body + plen + 1;
    /* The declared length must lie inside the frame. A bridge that says 900
     * on a 40-byte body is wrong, and believing it reads past the end. */
    if (rn == 0 || rn > body_len - (plen + 1)) return RC_PUTPATH_NOSEP;

    if (!rc_strf(out, cap, "%.*s", (int)rn, rel)) return RC_PUTPATH_TOOLONG;
    if (!isContainedRelative(out)) { out[0] = '\0'; return RC_PUTPATH_BADPATH; }
    /* Where the file's own bytes start. Derived HERE rather than recomputed
     * by the caller, so the offset the data is written from cannot drift from
     * the prefix this function actually consumed. */
    if (data_off) *data_off = plen + 1 + rn;
    return RC_PUTPATH_OK;
}

