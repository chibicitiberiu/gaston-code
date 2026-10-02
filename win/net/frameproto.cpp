#include <string.h>
#include "net/frameproto.h"

const rc_lane_row RC_LANE_TABLE[] = {
    { "EXEC",     RC_LANE_JOB },
    { "EXECKILL", RC_LANE_IMMEDIATE },
    /* Processes: answered on the reader, because behind a stuck EXEC is
     * exactly when an agent needs to see and end a program. A Toolhelp
     * snapshot is kernel32 alone, and a kill waits at most 250ms. (No
     * semicolon in here: the frame-contract test reads this table up to
     * the first one.) */
    { "PSLIST",   RC_LANE_IMMEDIATE },
    { "PSKILL",   RC_LANE_IMMEDIATE },
    { "PSLAUNCH", RC_LANE_IMMEDIATE },
    { "ATTGET",   RC_LANE_STREAM },
    /* Phase 6. Listed one by one rather than matched on a "UI" prefix: the
     * SYNC* wildcard below exists because that family was already a dozen
     * types, and a prefix rule silently claims every future name that
     * happens to start the same way. */
    { "UITREE",   RC_LANE_DESKTOP },
    { "UISHOT",   RC_LANE_DESKTOP },
    /* The `input` step list, and its stop. UICANCEL is IMMEDIATE because
     * the desktop worker is the thread running the list it is meant to
     * stop -- queued behind it, it would arrive when the list had ended. */
    { "UIRUN",    RC_LANE_DESKTOP },
    { "UICANCEL", RC_LANE_IMMEDIATE },
    /* The fs lane (step 5a). STREAM, alongside the sync frames, because that
     * is what they are: bounded file I/O on the worker that already blocks on
     * the disk. REMOTE.md gives gremote a thread of its own for these, and
     * gremote should have one -- it has no UI thread and no sync pass to
     * share with. The client does not, and a fifth thread's committed stack
     * is real on a 32MB box. What makes sharing safe is the cap: FSGET stops
     * at max_bytes, so it cannot hold the worker the way an unbounded read of
     * a swap file could. */
    { "FSGET",    RC_LANE_STREAM },
    { "FSPUT",    RC_LANE_STREAM },
    { "FSPUTEND", RC_LANE_STREAM },
    { "FSDRIVES", RC_LANE_STREAM },
    /* FSLS joins them (2026-10-02). One level of one directory, capped at
     * 2,000 entries, so it is bounded the same way FSGET is -- which is the
     * property that makes sharing the worker safe. */
    { "FSLS",     RC_LANE_STREAM },
    /* The four mutations (2026-10-02): one syscall each, the same bound. */
    { "FSMKDIR",  RC_LANE_STREAM },
    { "FSMV",     RC_LANE_STREAM },
    { "FSRM",     RC_LANE_STREAM },
    { "FSRMDIR",  RC_LANE_STREAM },
    /* A whole tree, but capped (fs/tree.h): 5,000 entries, 200MB. */
    { "FSCOPY",   RC_LANE_STREAM },
    { "FSSTATS",  RC_LANE_STREAM }
};
const int RC_LANE_TABLE_N = sizeof(RC_LANE_TABLE) / sizeof(RC_LANE_TABLE[0]);

rc_lane rc_frame_lane(const char *type)
{
    int i;
    for (i = 0; i < RC_LANE_TABLE_N; i++)
        if (strcmp(type, RC_LANE_TABLE[i].type) == 0)
            return RC_LANE_TABLE[i].lane;
    if (strncmp(type, "SYNC", 4) == 0) return RC_LANE_STREAM;
    return RC_LANE_UI;
}

int rc_frame_compressible(const char *type)
{
    return strcmp(type, "EXECOUT") == 0 ||
           strcmp(type, "SYNCDAT") == 0 ||
           strcmp(type, "SYNCENT") == 0 ||
           strcmp(type, "FSDAT") == 0 ||
           /* Rows of text, exactly like SYNCENT's: names, sizes and dates
            * compress well and there are up to 2,000 of them. */
           strcmp(type, "FSENT") == 0 ||
           /* THE SCREENSHOT, which this list has never carried. shot.h has
            * always said "Compression is NOT done here ... The wire codec
            * does it", and the wire codec was never asked: a 640x480 8bpp
            * capture is 300KB of palettised pixels and every one of them
            * went out raw. Found 2026-10-03 while turning deflate on for
            * gremote (REMOTE.md step 6).
            *
            * UIENT with it, for SYNCENT's reason: control records are rows
            * of class names and window text, and a desktop is a hundred of
            * them.
            *
            * Both still go through Codec::shouldCompress, which declines
            * when the link is faster than half what deflate manages -- so
            * on a LAN this changes nothing and on a slow link it is the
            * difference the file was written about. */
           strcmp(type, "UIDAT") == 0 ||
           strcmp(type, "UIENT") == 0;
}
