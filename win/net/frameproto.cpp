#include <string.h>
#include "net/frameproto.h"

const rc_lane_row RC_LANE_TABLE[] = {
    { "EXEC",     RC_LANE_JOB },
    { "EXECKILL", RC_LANE_IMMEDIATE },
    { "ATTGET",   RC_LANE_STREAM },
    /* Phase 6. Listed one by one rather than matched on a "UI" prefix: the
     * SYNC* wildcard below exists because that family was already a dozen
     * types, and a prefix rule silently claims every future name that
     * happens to start the same way. */
    { "UITREE",   RC_LANE_DESKTOP },
    { "UICLICK",  RC_LANE_DESKTOP },
    { "UIKEYS",   RC_LANE_DESKTOP },
    { "UISHOT",   RC_LANE_DESKTOP },
    { "UIFOCUS",  RC_LANE_DESKTOP }
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
           strcmp(type, "SYNCENT") == 0;
}
