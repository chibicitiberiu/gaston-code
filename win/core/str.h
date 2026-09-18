/* Bounded string building that cannot leave a buffer unterminated.
 *
 * `_snprintf` on Win32 writes at most `n` characters and does NOT terminate
 * when the result is exactly `n`. The idiom used across this codebase --
 * `_snprintf(buf, sizeof(buf) - 1, ...)` -- therefore leaves the final byte
 * untouched, and a string that exactly fills the buffer is unterminated. For a
 * zeroed global that is harmless luck; for a stack buffer the next `strlen`
 * walks off the end.
 *
 * Two of those sites are reachable from the wire: the manifest id and the put
 * path, both filled from a length the BRIDGE supplies. The guest is supposed
 * to survive a bridge that is wrong.
 *
 * So: one helper, always terminating, and it RETURNS WHETHER IT FIT. That
 * second half matters as much as the first. Three of the bugs fixed on
 * 2026-09-18 were silent truncation -- a destroyed file, a vanished subtree, a
 * cut-off message -- and in every one the information needed to refuse was
 * available at the call site and thrown away, because `_snprintf`'s return was
 * discarded at 93 of 95 sites. A caller that must not truncate can now say so.
 */
#ifndef RC_STR_H
#define RC_STR_H

/* Format into `out`, which is `cap` bytes INCLUDING the terminator.
 * Always leaves `out` NUL-terminated (given cap > 0).
 * Returns 1 if the whole string fit, 0 if it was truncated. */
int rc_strf(char *out, unsigned cap, const char *fmt, ...);

/* The common case: copy a string. Same contract. */
int rc_strcpy(char *out, unsigned cap, const char *src);

/* Copy at most `n` bytes of `src` -- for wire data that is not NUL-terminated.
 * Same contract; the result is always terminated. */
int rc_strncpy(char *out, unsigned cap, const char *src, unsigned n);

#endif
