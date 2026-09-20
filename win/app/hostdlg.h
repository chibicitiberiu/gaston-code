/* Configure -> Server Host... : the dialog that replaced a message box.
 *
 * Built with DialogBoxIndirectParam from a template composed at runtime, not
 * a compiled resource. gaston.rc says why in its own header: a compiled
 * template is fixed in dialog units, and the guest may be running Large
 * Fonts on a 640x480 screen, where a template laid out against the wrong
 * metrics clips its own buttons. A runtime template is measured against the
 * font the system actually reports.
 *
 * Its own file because main.cpp is where every unfinished Phase 4 item
 * points, and it was 1300 lines before any of them landed.
 */
#ifndef RC_HOSTDLG_H
#define RC_HOSTDLG_H

#ifndef __cplusplus
#error "hostdlg.h is C++"
#endif

#include <windows.h>
#include "core/ini.h"

/* Show the dialog. Non-zero if the user pressed OK AND changed something, in
 * which case `cfg` holds the new values and the caller is expected to save
 * and reconnect.
 *
 * Returning "changed", not merely "accepted", is deliberate: the caller tears
 * down a live connection on a true answer, and doing that because somebody
 * opened the dialog and pressed OK without typing would be a needless
 * interruption of whatever the agent was doing.
 */
int rc_hostdlg_run(HWND parent, HINSTANCE inst, rc_config *cfg);

#endif
