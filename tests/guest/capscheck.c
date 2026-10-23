/* 03.12: caps.h defines RC_CASE_*, RC_DEVICES_*, RC_EOL_* etc as small
 * integers, bridge/files/caps.py mirrors them as CASE_*, DEVICES_*, EOL_*, and
 * `_enum` there only refuses a value OUTSIDE its allowed tuple. A
 * renumbering -- an enum member moved to a value nothing declares -- is
 * refused. A RE-MEANING -- the same number, a different meaning on the two
 * sides, e.g. `eol=2` meaning something other than "classic Mac" -- is
 * accepted silently, because only the integer crosses the wire and nothing
 * ever compares the NAMES.
 *
 * This cannot be checked by connecting and reading `caps.summary()`, the way
 * synccheck's --limits proves a length boundary by exercising the real
 * enforcement: there is no observable guest behaviour that depends on which
 * meaning `eol=2` has, only on which meaning EACH SIDE believes it has. So
 * instead this prints the guest's own headers -- literally, by
 * stringification, not retyped -- and the Python side asserts each one
 * equals the bridge's constant of the matching name. A rename on either side
 * without the other is exactly what this is for.
 */
#include <stdio.h>
#include "net/caps.h"

#define P(name) printf("%s\t%d\n", #name, (int)(name))

int main(void)
{
    P(RC_CASE_SENSITIVE);
    P(RC_CASE_FOLD);

    P(RC_DEVICES_NONE);
    P(RC_DEVICES_DOS);

    P(RC_TRAILING_KEPT);
    P(RC_TRAILING_STRIP);

    P(RC_ILLEGAL_POSIX);
    P(RC_ILLEGAL_DOS);

    P(RC_EOL_LF);
    P(RC_EOL_CRLF);
    P(RC_EOL_CR);

    P(RC_DIRRESERVE_NONE);
    P(RC_DIRRESERVE_WIN32);

    P(RC_WORKRESERVE_NONE);
    P(RC_WORKRESERVE_GASTON);

    P(RC_SHORTNAMES_NONE);
    P(RC_SHORTNAMES_83);

    P(RC_DEFLATE_NONE);
    P(RC_DEFLATE_STREAM);

    /* 03.19: "what this client IS" (RC_CAP_*, what a real Win98 build
     * actually declares in HELLO) and "what the bridge assumes an
     * undeclaring client is" (Caps()'s field defaults in bridge/files/
     * caps.py) are two encodings of the same Win9x machine -- the bridge's
     * own docstring says as much: an absent field means the deployed Win9x
     * client, so every default has to BE that client's real value, not a
     * guess at it. Printed separately from the enum members above so Python
     * can diff them against `Caps()` directly rather than against the
     * matching `RC_CASE_FOLD`-style constant, which only proves the ALIAS
     * target didn't move, not that this client still points at it. */
    P(RC_CAP_CASE);
    P(RC_CAP_DEVICES);
    P(RC_CAP_TRAILING);
    P(RC_CAP_ILLEGAL);
    P(RC_CAP_EOL);
    P(RC_CAP_DIRRESERVE);
    P(RC_CAP_WORKRESERVE);
    P(RC_CAP_SHORTNAMES);
    /* The bridge refuses a path deeper than the guest's own walk, so its
     * assumed default has to equal ours. This is RC_SYNC_MAX_DEPTH by
     * definition, and the point of printing it is the BRIDGE side. */
    P(RC_CAP_MAXDEPTH);
    /* Same shape: the too_many message quotes this number, so the bridge's
     * default (MAXFILES_GASTON) has to match the guest's. */
    P(RC_CAP_MAXFILES);
    P(RC_CAP_MAXPATH);
    P(RC_CAP_MAXCOMP);

    return 0;
}
