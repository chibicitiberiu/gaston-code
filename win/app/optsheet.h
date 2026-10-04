/* Configure > Options...: one property sheet for every per-client setting
 * (docs/OPTIONS-PLAN.md).
 *
 * The sheet works on a COPY of the configuration. Pages write their fields
 * into the copy when the sheet applies; once every page that was opened has
 * done so, the copy goes to the OptionsSink in one piece -- for OK and for
 * Apply alike -- and the sink saves it, rebuilds the PREFS body and either
 * re-sends it or reconnects. A page never opened keeps the copy's value,
 * which is the live one.
 *
 * WHILE IT IS OPEN the main window is disabled: a permission bar cannot be
 * answered, and its deadline keeps running. Frames still arrive and render,
 * because the sheet's modal loop dispatches them.
 */
#ifndef RC_OPTSHEET_H
#define RC_OPTSHEET_H

#ifndef __cplusplus
#error "app/optsheet.h is C++"
#endif

#include <windows.h>

#include "app/config.h"

/* The pages, in tab order. */
#define RC_OPT_CONNECTION  0
#define RC_OPT_COUNT       1

class OptionsSink
{
public:
    virtual ~OptionsSink() {}
    /* The person pressed OK or Apply; `next` is the whole configuration. */
    virtual void optionsApply(const ClientConfig &next) = 0;
};

/* Show the sheet, starting on page `start`. True if anything was applied. */
bool rc_options_run(HWND parent, HINSTANCE inst, const ClientConfig &cur,
                    const char *exeDir, OptionsSink *sink, int start);

#endif
