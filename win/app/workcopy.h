/* Moving a conversation's files when its work directory changes.
 *
 * A COPY, NEVER A MOVE. The old folder is left exactly as it was and the
 * caller says where it is. Three reasons, and the third decides it:
 * deleting a directory as a side effect of changing a setting deserves its
 * own confirmation rather than a radio button; it is what makes "keep the
 * files already there" coherent, because the losing copies stay somewhere
 * real; and it makes an interrupted copy recoverable by construction. A move
 * that deletes as it goes leaves the tree split across two folders with no
 * record of which files made it.
 *
 * PRE-FLIGHT BEFORE A BYTE IS WRITTEN. Every refusal this can make is made
 * by `rc_worksurvey_run` while nothing has changed, so a refusal never
 * leaves half a tree behind. Failures during the copy are still possible --
 * something else fills the disk, a file is locked -- and those are offered
 * to the user one at a time as retry, skip or cancel.
 */
#ifndef RC_WORKCOPY_H
#define RC_WORKCOPY_H

#ifndef __cplusplus
#error "workcopy.h is C++"
#endif

#include <windows.h>

/* --- the survey ---------------------------------------------------------- */

#define RC_WORK_OK       0
#define RC_WORK_NOSRC    1  /* the folder being left is not there */
#define RC_WORK_NESTED   2  /* one folder is inside the other */
#define RC_WORK_TOODEEP  3  /* a destination path would be too long */
#define RC_WORK_TOOBIG   4  /* not enough room on the target volume */
#define RC_WORK_TOOMANY  5  /* more files than the sync layer will carry */
#define RC_WORK_NODST    6  /* the destination cannot be made or written */

/* How many colliding names the question shows before it says "and N more".
 * Three, because the point is to make the answer concrete, not to be a file
 * manager -- and because every one of these is 64 bytes of a 28KB budget. */
#define RC_WORK_SHOWN  3

typedef struct {
    unsigned      files;            /* files that would be copied */
    unsigned      dirs;
    unsigned long bytes;            /* total, for the free-space check */
    unsigned      collisions;       /* names present in BOTH folders */
    char          shown[RC_WORK_SHOWN][64];
    unsigned      longest;          /* longest destination path formed */
    char          deep[64];         /* the name that made it, for the refusal */
    int           refusal;          /* RC_WORK_* */
} rc_worksurvey;

/* Look, and change nothing. Non-zero when the copy may proceed.
 *
 * `maxpath` is the machine's own limit, which the bridge was told at
 * handshake and which is 260 here -- passed in rather than assumed, because
 * a name that fits under C:\Gaston\OK need not fit under a deeper root and
 * that is the failure most likely to bite. */
int rc_worksurvey_run(const char *src, const char *dst, unsigned maxpath,
                      rc_worksurvey *out);

/* Just the size of a folder: how many files and how many bytes.
 *
 * Separate from the survey because it is asked at a different moment and
 * about a different folder -- the dialog needs it before a destination has
 * been chosen, to say what is at stake, and the survey cannot answer without
 * one. Returns non-zero if the folder could be walked. */
int rc_worksurvey_count(const char *dir, unsigned *files, unsigned long *bytes);

/* One sentence for the user, naming the specific thing that is wrong. */
void rc_worksurvey_why(const rc_worksurvey *s, const char *src,
                       const char *dst, char *out, unsigned cap);

/* --- the copy ------------------------------------------------------------ */

/* What to do about a name that exists in both folders. Asked ONCE for the
 * whole set rather than once per file: point a chat at a real project and
 * forty names collide, and forty dialogs is a punishment. */
#define RC_WORK_KEEP     0  /* the destination's copy stays; nothing staged */
#define RC_WORK_REPLACE  1  /* incoming wins; the target's goes aside */
#define RC_WORK_MERGE    2  /* destination stays; incoming staged for the agent */

/* Where a copy that did not win is put, under the destination.
 *
 * ORDINARY VISIBLE FOLDERS, and NOT `.gaston\conflict`, which is what this
 * first used. That directory is the sync layer's own and the bridge ignores
 * it by default (bridge/files/ignores.py) -- so everything staged there was
 * frozen out of the mirror and the agent could not read a byte of it, which
 * is the entire point of RC_WORK_MERGE. Measured, not reasoned about: the
 * first merge on the guest reported ".gaston/conflict/hi.txt: ignored".
 *
 * Two names rather than one, because they hold opposite things and a single
 * folder would make the user guess which: INCOMING is what was brought and
 * did not overwrite, REPLACED is what was overwritten and would otherwise
 * be gone. */
#define RC_WORK_INCOMING  "Gaston-Incoming"
#define RC_WORK_REPLACED  "Gaston-Replaced"

/* Called between files. Return zero to stop; what has been copied stays. */
typedef int (*rc_workprog)(void *ctx, unsigned done, unsigned total,
                           const char *rel);

#define RC_WORK_RETRY   0
#define RC_WORK_SKIP    1
#define RC_WORK_CANCEL  2

/* Called when one file will not copy. `err` is the Win32 code. */
typedef int (*rc_workerr)(void *ctx, const char *rel, DWORD err);

/* Copy `src` into `dst`. Returns non-zero if it ran to the end.
 *
 * `copied` and `failed` are filled in either way, because a cancelled copy
 * that moved two hundred files is a different thing to report than one that
 * moved none.
 */
int rc_workcopy_run(const char *src, const char *dst, int on_collision,
                    const rc_worksurvey *plan,
                    rc_workprog prog, rc_workerr onerr, void *ctx,
                    unsigned *copied, unsigned *failed);

#endif
