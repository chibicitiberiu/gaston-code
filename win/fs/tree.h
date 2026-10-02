/* tree.h -- copy, move and delete the way an agent expects any OS to.
 *
 * Windows 98 gives MoveFileA, which renames within a drive and copies a FILE
 * across drives but refuses a folder (measured on the Athlon, 2026-10-02);
 * CopyFileA, which copies one file; and RemoveDirectoryA, which removes an
 * empty folder. The agent on the other end has been trained on `cp -r`,
 * `mv` and `rm -r`. These close the gap on the guest, where the bytes are,
 * instead of one round trip per file over a slow link.
 *
 * Three rules, the reason this file exists rather than a loop somewhere:
 *
 *   - NOTHING IS OVERWRITTEN UNLESS ASKED. Every conflict is found by a
 *     preflight walk before the first byte is copied, so a refusal leaves
 *     both sides as they were.
 *   - A MOVE NEVER LOSES THE SOURCE. Across drives it is copy, verify every
 *     file's size, and only then remove the source; any failure before that
 *     leaves the source whole.
 *   - DELETE GOES TO THE RECYCLE BIN by default (SHFileOperation with
 *     FOF_ALLOWUNDO, the shell's own), so a person can put it back. Only a
 *     caller that says `permanent` removes for good, and a drive with no
 *     Recycle Bin -- floppy, network -- is refused without it.
 *
 * Bounded: RC_TREE_MAX_ENTRIES entries and RC_TREE_MAX_BYTES per call, so a
 * copy cannot hold the worker past what the bridge waits for. Bigger trees
 * are refused with `too_big`, and the caller splits the work.
 *
 * Every function returns NULL or a status word, like fs/ops.h. `detail`,
 * when given, receives the path a failure stopped at.
 */
#ifndef RC_FS_TREE_H
#define RC_FS_TREE_H

#include <windows.h>

#define RC_TREE_MAX_ENTRIES 5000
#define RC_TREE_MAX_BYTES   (200UL * 1024UL * 1024UL)
#define RC_TREE_SAMPLE      5

struct TreeStats
{
    DWORD     files;
    DWORD     dirs;
    ULONGLONG bytes;
    bool      isDir;        /* the path itself */
    int       sampleN;
    char      sample[RC_TREE_SAMPLE][MAX_PATH];   /* first names, relative */
    TreeStats();
};

/* What is at `path`: a file (files=1) or a folder and everything below it.
 * `too_big` past the caps, with the counts so far. */
const char *fsTreeStats(const char *path, TreeStats &out);

struct TreeDone
{
    DWORD     files;
    DWORD     dirs;
    ULONGLONG bytes;
    bool      renamed;      /* MOVE: a rename, nothing copied */
    bool      recycled;     /* DELETE: to the Recycle Bin */
    TreeDone();
};

/* A file or a whole folder to `to`. A folder MERGES into an existing folder
 * only with `overwrite`, and files replace files only with `overwrite`. */
const char *fsCopyTree(const char *from, const char *to, bool overwrite,
                       TreeDone &done, char *detail, unsigned detailCap);

/* Rename when the platform can; otherwise copy, verify, remove the source. */
const char *fsMoveTree(const char *from, const char *to, bool overwrite,
                       TreeDone &done, char *detail, unsigned detailCap);

/* A file or a folder and everything in it: to the Recycle Bin, or -- only
 * with `permanent` -- gone. */
const char *fsDeleteTree(const char *path, bool permanent, TreeDone &done,
                         char *detail, unsigned detailCap);

#endif
