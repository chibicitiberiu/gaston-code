/* path.c -- see core/path.h. */
#include <windows.h>
#include <string.h>

#include "core/path.h"
#include "core/str.h"

static int is_sep(char c) { return c == '\\' || c == '/'; }

/* Is there a directory at `p` already? A FILE there is NOT a yes: the caller
 * is about to write inside it, and reporting success would send it looking
 * for its files in a place that cannot hold any. */
static int is_dir(const char *p)
{
    DWORD a = GetFileAttributesA(p);
    return a != 0xFFFFFFFF && (a & FILE_ATTRIBUTE_DIRECTORY);
}

/* How much of the front of `path` is a prefix nobody creates.
 *
 * Three shapes, and getting any of them wrong turns the loop below into a
 * string of failed CreateDirectory calls before the part that would have
 * worked:
 *
 *   C:\a\b     -- the drive root is not a directory anybody makes
 *   \\box\f\a  -- neither is the server, nor the share on it
 *   \a\b       -- rooted on the current drive; the root itself is not ours
 *
 * Returns the number of leading bytes to skip. A relative path gets 0.
 */
static unsigned skip_root(const char *path)
{
    unsigned i = 0;

    if (is_sep(path[0]) && is_sep(path[1])) {
        /* \\server\share -- step over two components, not one. Creating a
         * share is not a filesystem operation and CreateDirectory on one
         * fails in a way that reads like a permissions problem. */
        i = 2;
        while (path[i] && !is_sep(path[i])) i++;      /* server */
        while (is_sep(path[i])) i++;
        while (path[i] && !is_sep(path[i])) i++;      /* share */
        while (is_sep(path[i])) i++;
        return i;
    }
    if (path[0] && path[1] == ':') {
        i = 2;
        while (is_sep(path[i])) i++;
        return i;
    }
    while (is_sep(path[i])) i++;
    return i;
}

int rc_mkpath(const char *path)
{
    char work[MAX_PATH];
    unsigned i;

    if (!path || !*path) return 0;
    /* Refuse rather than truncate. A path one byte too long, silently cut,
     * names a DIFFERENT directory -- and the caller then writes its files
     * into it and reports success, which is the shape of the truncation bugs
     * this codebase has already paid for once. */
    if (!rc_strcpy(work, sizeof(work), path)) {
        SetLastError(ERROR_FILENAME_EXCED_RANGE);
        return 0;
    }

    /* Trailing separators are not a component. `C:\a\b\` must not try to
     * create the empty name after the last one. */
    i = (unsigned)strlen(work);
    while (i > 0 && is_sep(work[i - 1])) work[--i] = '\0';
    if (!work[0]) return 0;

    for (i = skip_root(work); ; i++) {
        char had;
        if (work[i] && !is_sep(work[i])) continue;

        /* Cut here, make what is to the left, put the separator back. */
        had = work[i];
        work[i] = '\0';
        if (work[0] && !is_dir(work)) {
            if (!CreateDirectoryA(work, NULL)) {
                DWORD err = GetLastError();
                /* ALREADY_EXISTS after is_dir said no means something is
                 * there that is not a directory -- or another process won
                 * the race and made one, which is_dir now settles. */
                if (err != ERROR_ALREADY_EXISTS || !is_dir(work)) {
                    work[i] = had;
                    SetLastError(err);
                    return 0;
                }
            }
        }
        work[i] = had;
        if (!had) break;
    }
    return is_dir(work);
}
