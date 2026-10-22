/* Prints what tool detection found, in the wire encoding.
 *
 * ONE PRODUCER for the guest and for the tests. `rc_tooldet_cached` builds
 * the exact bytes that go into HELLO's body, and this prints them, so the
 * format the Python side parses is the format that ships rather than a
 * second implementation that agrees today. That is the `synccheck`
 * precedent, and it is what `test_guest_agreement.py` exists to exploit:
 * feed this straight into the bridge's parser and assert they match.
 *
 *   toolcheck                 -- read the INIs beside this exe
 *   toolcheck <dir>           -- read them from <dir> instead
 *
 * The second form is what makes this testable under Wine: point it at a
 * synthetic tree with its own INI and assert on what comes back, without
 * needing Open Watcom to actually be installed.
 */
#include <windows.h>
#include <stdio.h>
#include <string.h>
#include <fcntl.h>
#include <io.h>

#include "app/tooldet.h"
#include "core/str.h"

static void exe_dir(char *out, unsigned n)
{
    char path[MAX_PATH];
    char *cut;
    if (!GetModuleFileNameA(NULL, path, sizeof(path))) {
        rc_strcpy(out, n, ".");
        return;
    }
    cut = strrchr(path, '\\');
    if (cut) *cut = '\0';
    rc_strcpy(out, n, path);
}

int main(int argc, char **argv)
{
    char dir[MAX_PATH];
    const char *rows;
    unsigned len = 0;

    if (argc > 1) rc_strcpy(dir, sizeof(dir), argv[1]);
    else          exe_dir(dir, sizeof(dir));

    rows = rc_tooldet_cached(dir, &len);

    /* BINARY MODE, and it is not a detail.
     *
     * The C runtime opens stdout in text mode, which turns every '\n' into
     * a CRLF on the way out. The guest sends this buffer to the bridge
     * verbatim, so the rows on the wire end in one byte and the rows this
     * printed ended in two -- which made every stated length one short, and
     * the bridge's parser correctly refused every single row. The failure
     * looked like a broken parser and was a broken test harness: precisely
     * the "second implementation that happens to agree" this binary exists
     * to rule out. Caught by test_tooldet_agreement.py on its first run.
     *
     * Raw, with no reformatting -- the point is the bytes. Writing through
     * fwrite rather than printf because a value can contain a '%' (a path
     * can) and printf would eat it. */
    setmode(fileno(stdout), O_BINARY);
    fwrite(rows, 1, len, stdout);
    return 0;
}
