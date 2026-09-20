/* Making a directory, including the ones above it.
 *
 * `CreateDirectory` does not make parents. Every caller in this program that
 * wanted a directory had the same three lines around it -- stat, create,
 * complain -- and every one of them worked only one level deep, so a path
 * whose parent was also missing failed with ERROR_PATH_NOT_FOUND. That reads
 * like a missing source rather than a missing destination, and the comment
 * at one of those sites said as much.
 *
 * It stopped being theoretical when the work directory became something the
 * user picks: `C:\Projects\Retro\Build` is an ordinary answer to "where
 * should this conversation work", and three of its four components can be
 * absent.
 */
#ifndef RC_PATH_H
#define RC_PATH_H

#ifdef __cplusplus
extern "C" {
#endif

/* Make `path` and every missing directory above it. `mkdir -p`.
 *
 * Non-zero if the directory exists when this returns, INCLUDING when it was
 * already there -- the question a caller has is "can I write here", not "did
 * I win the race to create it". Zero means it does not and could not be
 * made, and the reason is in GetLastError.
 *
 * Understands what it must not try to create: a drive root (`C:\`), and a
 * UNC server and share (`\\box\files`), neither of which is a directory
 * anybody creates. Accepts `/` as a separator, because paths reach this from
 * the wire as well as from the shell. An existing FILE in the way is a
 * failure, not a success. */
int rc_mkpath(const char *path);

#ifdef __cplusplus
}
#endif

#endif
