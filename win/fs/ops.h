/* ops.h -- the four mutations, each answering WHY when it does not happen.
 *
 * file.h's calls answer true or false; a caller that has to tell a person
 * (or an agent) what went wrong needs the reason, and on Windows 98 the
 * reason is not always the error number. These were gremote's (REMOTE.md
 * step 5) and moved here when the client grew the same four ops, so the
 * measured cases below are classified once.
 *
 * Every function returns NULL when it did what was asked, or a short status
 * word that is the same on both wires: missing, too_long, bad_path, locked,
 * readonly, is_directory, is_file, exists, no_parent, drive_root, not_empty,
 * denied, is_cwd, cross_volume. Call it immediately -- each reads
 * GetLastError from its own failure.
 */
#ifndef RC_FS_OPS_H
#define RC_FS_OPS_H

/* After statPath failed: `missing` for "nothing there" (an empty floppy bay
 * included), rather than `locked`, which a caller retries until its budget
 * runs out. */
const char *fsWhyNoStat();

/* "C:", "C:\" -- refused by every op here, by inspection. */
bool fsIsDriveRoot(const char *path);

/* A file, never a directory. Read-only is cleared first (file.h). */
const char *fsRemoveFile(const char *path);

/* Creates `path` and its missing parents. `existingOk`: a directory already
 * there is success (the client's `mkdir`, -p semantics) or `exists`
 * (gremote's MKDIR, REMOTE.md). Something there that is NOT a directory is
 * `is_file` either way. */
const char *fsMakeDirectory(const char *path, bool existingOk);

/* An EMPTY directory. Not recursive, because RemoveDirectoryA is not -- the
 * loop that would make it so belongs where it can be read. */
const char *fsRemoveDirectory(const char *path);

/* MoveFileA: never replaces (`exists`). Across drives, a FILE moves -- 98
 * copies it, measured -- and a FOLDER is refused with `cross_volume`
 * (MoveFileExA's copy is NT-only and on the deny list). */
const char *fsMovePath(const char *from, const char *to);

#endif
