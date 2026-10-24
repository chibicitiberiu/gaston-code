/* The conversation's work directory, on this machine.
 *
 * ONE MIRROR PER CONVERSATION (notes/BACKLOG.md 7). The directory a chat works
 * in IS the tree that is mirrored for it: files the assistant writes appear
 * there and files put there reach it. So changing it is not a preference, it
 * is re-pointing the sync layer -- which is why this is its own module rather
 * than three lines in a menu handler.
 *
 * WHO DECIDES. The bridge does, and the guest applies what it is told: the
 * work directory arrives in the OPTION batch like the model and the mode, and
 * `rc_workdir_apply` is what makes it real on this side. The user picking a
 * folder is a PROPOSAL -- it goes up as SETWORK and comes back down in the
 * next batch. One direction of authority, so the two sides cannot disagree
 * about which tree is being mirrored, which is the disagreement that ends
 * with files in the wrong place.
 */
#ifndef RC_WORKDIR_H
#define RC_WORKDIR_H

#ifndef __cplusplus
#error "workdir.h is C++"
#endif

#include <windows.h>

/* Point the sync layer at `path`, creating it if it is not there.
 *
 * Safe to call with the path that is already in force -- it returns without
 * touching anything, which is what makes it safe to call on every OPTION
 * batch.
 *
 * AN EMPTY PATH SWITCHES FILE SHARING OFF, and that is the state every
 * conversation starts in. Nothing is mirrored while a chat is only being
 * talked to: sharing begins when the conversation commits to a folder, which
 * is the moment its first tool runs. Before that there is no tree, so the two
 * sides cannot disagree about which tree they are pairing -- which is exactly
 * how a bridge that had just restarted came to pick one conversation's
 * baseline and then run its first pass against its whole mirrored directory.
 *
 * Non-zero if the directory is now in use. Zero means it could not be made
 * or opened, and the caller should say so rather than leaving the user to
 * work out why nothing arrives. */
int rc_workdir_apply(const char *path);

/* What is in force right now. Never NULL; "" means sharing is off. */
const char *rc_workdir_current(void);

/* Ask the user for a folder, with the native browser.
 *
 * SHBrowseForFolder rather than a text field, which is what a Windows user
 * expects and what stops a typo becoming a directory. Resolved through
 * LoadLibrary: shell32 is on every Windows this targets, but an import that
 * cannot resolve kills the image at load, before WinMain, with no log -- and
 * a folder picker is not worth the program refusing to start.
 *
 * Non-zero if the user chose one, with `out` holding it. */
int rc_workdir_browse(HWND owner, const char *start, char *out, unsigned cap);

#endif
