/* "Change work directory..." -- the dialog, not the bare folder browser.
 *
 * Two things can be true at once: the folder being left has work in it, and
 * the folder being chosen is an existing project with its own. Requiring an
 * empty target would rule out the case that makes the feature worth having,
 * so instead the user is shown what is at stake and asked what to do with
 * it.
 *
 * IT ONLY APPEARS WHEN THERE IS SOMETHING TO LOSE. An unset or empty current
 * folder goes straight to `rc_workdir_browse`, because a radio group about
 * zero files is noise.
 *
 * The folder is still only a PROPOSAL when this returns -- it goes up as
 * SETWORK and comes back in the next OPTION batch, which is what makes it
 * real. See app/workdir.h for why the authority runs that way. The copy,
 * though, happens HERE and before the proposal is sent: it is local to this
 * machine, the bridge has no part in it, and doing it first means the tree
 * the bridge is about to mirror is the tree the user asked for.
 */
#ifndef RC_WORKDLG_H
#define RC_WORKDLG_H

#ifndef __cplusplus
#error "workdlg.h is C++"
#endif

#include <windows.h>

/* Send the chosen folder to the bridge. Called from inside the dialog's own
 * flow, between the copy and the wait, so ONE progress window covers both --
 * from where the user sits it is one wait, and two bars for one action
 * invite the belief that the first one finishing means it is done. */
typedef void (*rc_workdlg_propose)(const char *path);

/* Ask for a new work directory, deal with the files in the old one, and stay
 * up until the new folder has reached the assistant.
 *
 * `current` is the folder in force, which may be "". `out` receives the
 * folder chosen. Non-zero if the user chose one; by then the copying they
 * asked for has been done and `propose` has been called.
 *
 * `maxpath` is what the machine declared at handshake; it is what the
 * pre-flight measures destination paths against. */
int rc_workdlg_run(HWND parent, HINSTANCE inst, const char *current,
                   unsigned maxpath, rc_workdlg_propose propose,
                   char *out, unsigned cap);

#endif
