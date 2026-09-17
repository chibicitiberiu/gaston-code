/* What this client IS, for the bridge's path and content rules.
 *
 * The bridge used to have Windows 9x baked in: CP1252, MAX_PATH 260, the DOS
 * device names, `< > : " \ | ? *`, trailing dots stripped, case-insensitive,
 * CRLF. All of that is a property of THIS machine, and the same bridge is
 * meant to serve MS-DOS, NT 4, or something with no relation to either.
 *
 * One HELLO header per option, each holding a small integer. Not packed bits:
 * `devices=1` answers "why did that file not sync" in a log at three in the
 * morning and `caps=0x7F` does not, a bit can only ever answer yes or no, and
 * a value a bridge does not recognise has to be distinguishable from a bit
 * that is clear -- which is the permissive reading, arrived at silently.
 * Ten headers is about a hundred and twenty bytes, once per connection, on a
 * link that carries kilobytes of transcript per turn.
 *
 * Every value here is a compile-time literal except the codepage, which is one
 * GetACP() call. This client holds no tables and computes nothing: it reports
 * what it is, and the bridge holds what that implies. Same split as the rest
 * of the codebase.
 *
 * Mirrored in bridge/caps.py. Change one, change the other.
 */
#ifndef RC_CAPS_H
#define RC_CAPS_H

#define RC_CASE_SENSITIVE   0   /* README.md and readme.md are two files */
#define RC_CASE_FOLD        1   /* ...and one. Case-preserving either way. */

#define RC_DEVICES_NONE     0
#define RC_DEVICES_DOS      1   /* CON PRN AUX NUL COMn LPTn CLOCK$, any ext */

#define RC_TRAILING_KEPT    0
#define RC_TRAILING_STRIP   1   /* Win32 strips a trailing dot or space */

#define RC_ILLEGAL_POSIX    0   /* only NUL and the separator */
#define RC_ILLEGAL_DOS      1   /* also < > : " \ | ? * */

#define RC_EOL_LF           0
#define RC_EOL_CRLF         1
#define RC_EOL_CR           2

#define RC_SHORTNAMES_NONE  0
#define RC_SHORTNAMES_83    1

/* How many characters short of the path limit a DIRECTORY must stop.
 * CreateDirectory refuses within 12 of MAX_PATH so an 8.3 name still fits
 * inside; Unix reserves none. A NUMBER, not a flag, so a client with a
 * different reservation says so without anyone adding a field. */
#define RC_DIRRESERVE_WIN32 12
#define RC_DIRRESERVE_NONE  0

/* This client. A Windows 3.1 or MS-DOS build changes these lines and nothing
 * else; a Unix build sets them all to the permissive value. */
#define RC_CAP_CASE        RC_CASE_FOLD
#define RC_CAP_DEVICES     RC_DEVICES_DOS
#define RC_CAP_TRAILING    RC_TRAILING_STRIP
#define RC_CAP_ILLEGAL     RC_ILLEGAL_DOS
#define RC_CAP_EOL         RC_EOL_CRLF
#define RC_CAP_SHORTNAMES  RC_SHORTNAMES_83
#define RC_CAP_DIRRESERVE  RC_DIRRESERVE_WIN32
#define RC_CAP_MAXPATH     MAX_PATH
#define RC_CAP_MAXCOMP     255

#endif
