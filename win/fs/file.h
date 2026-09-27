/* One-shot operations on one path. libfs; libcore only.
 *
 * None of these read a file's contents -- that is fs/reader.h. What a path IS
 * has to be answerable without reading it, because the three answers are
 * acted on differently and conflating them has cost real bugs: a MISSING file
 * and a DIRECTORY both used to come back as "unreadable", the bridge treats
 * unreadable as transient and retries forever, and a pull of something the
 * user had just deleted never resolved and never reported anything.
 */
#ifndef RC_FS_FILE_H
#define RC_FS_FILE_H

#ifndef __cplusplus
#error "fs/file.h is C++"
#endif

#include <windows.h>

/* `type` is the vocabulary the whole tree already uses:
 *   'f'  a file
 *   'd'  a directory
 *   'u'  something is here and we cannot read or address it
 *   0    nothing here -- distinct from 'u', and the distinction is the bug fix
 */
struct FileInfo
{
    char  type;
    DWORD size;
    DWORD mtime;    /* unix seconds, local; a per-side cache key only */
    DWORD attrs;

    FileInfo();
};

/* Fills `out` from the directory entry alone. False when there is nothing at
 * `full`, in which case `out.type` is 0. */
bool statPath(const char *full, FileInfo &out);

/* Clears FILE_ATTRIBUTE_READONLY first: a read-only file is still the user's
 * to delete when they asked for it, and DeleteFileA refuses one silently. */
bool removeFile(const char *full);

/* MoveFileA, never MoveFileExA -- the latter is NT-only and a static import
 * of it kills the image at load on 9x. So this cannot replace an existing
 * target atomically, which is why fs/writer.h exists rather than this being
 * enough on its own. */
bool movePath(const char *from, const char *to);

/* Creates `full` and any missing parents. True if it exists as a directory
 * afterwards, whether or not this call was what made it. */
bool makeDirectory(const char *full);

#endif
