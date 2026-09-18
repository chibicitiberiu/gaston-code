#include <windows.h>
#include <stdio.h>
#include <stdarg.h>
#include <string.h>
#include "core/str.h"

int rc_strf(char *out, unsigned cap, const char *fmt, ...)
{
    va_list ap;
    int n;

    if (!out || cap == 0) return 0;

    /* Pass the FULL capacity and terminate by hand. Passing `cap - 1` is the
     * idiom this function exists to replace: it reserves a byte that nothing
     * then writes, so a string of exactly that length is unterminated. */
    va_start(ap, fmt);
    n = _vsnprintf(out, cap, fmt, ap);
    va_end(ap);

    /* _vsnprintf returns the length written, or negative when the result did
     * not fit. It does not terminate in the second case, and on some lengths
     * it does not terminate in the first either. */
    out[cap - 1] = '\0';
    return (n >= 0 && (unsigned)n < cap) ? 1 : 0;
}

int rc_strcpy(char *out, unsigned cap, const char *src)
{
    unsigned n;

    if (!out || cap == 0) return 0;
    if (!src) { out[0] = '\0'; return 1; }

    n = (unsigned)strlen(src);
    if (n >= cap) {
        memcpy(out, src, cap - 1);
        out[cap - 1] = '\0';
        return 0;
    }
    memcpy(out, src, n + 1);
    return 1;
}

int rc_strncpy(char *out, unsigned cap, const char *src, unsigned n)
{
    if (!out || cap == 0) return 0;
    if (!src) { out[0] = '\0'; return 1; }

    if (n >= cap) {
        memcpy(out, src, cap - 1);
        out[cap - 1] = '\0';
        return 0;
    }
    memcpy(out, src, n);
    out[n] = '\0';
    return 1;
}
