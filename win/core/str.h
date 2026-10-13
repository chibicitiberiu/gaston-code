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
 *
 * WHEN THE RETURN MUST BE USED, AND WHEN IGNORING IT IS CORRECT
 * (the convention, written down in stage 4 -- see BACKLOG 4).
 *
 * Most callers ignore it and are right to. As of 2026-10-13 there are 527
 * production call sites across the three helpers and 472 discard the return,
 * which is not 472 latent bugs: a `%lu` into a 16-byte buffer has a static
 * bound, and a status line that loses its tail is cosmetic. Sweeping them all
 * would be 472 edits to find the handful that matter, and the handful is what
 * matters. So the rule is by DESTINATION, not by call count:
 *
 *   A path or an id must take one of these four forms.
 *
 *   1. CHECK IT. The caller can refuse, so it does. `win/app/cache.cpp` is
 *      the model: three checked sites, each with the reason beside it, and
 *      `path_for` returns its verdict to every caller.
 *
 *   2. GUARD IT FIRST, then ignore the return. `win/rpc/rpc.cpp` is the
 *      template -- an explicit `strlen(id) >= sizeof(ctx.id)` test that drops
 *      the request, with the reason written out: a shortened id produces a
 *      reply the bridge cannot match, and the caller then waits out its whole
 *      budget. After that guard the copy cannot truncate, and checking it
 *      again would be noise.
 *
 *   3. LOG AND CARRY ON, where refusing would cost more than truncating.
 *      `win/ui/uirpc.cpp`'s two run-id copies are the case: the id is used
 *      only to match a later cancel, so truncation degrades cancellation and
 *      nothing else -- while refusing would fail a drive that would other-
 *      wise have worked, over a 49th character. The log line is what keeps
 *      it from being silent. (Note this is NOT "the reply cannot carry the
 *      id": a frame header value holds RC_MAX_VAL bytes, so it always can.
 *      That was this convention's first draft and it was wrong.)
 *
 *   4. IGNORE IT, because the destination is display or a log. Say so only if
 *      it is not obvious.
 *
 * Anything that is NOT a path or an id is case 4 by default and needs no
 * ceremony. The guard belongs where form 2 puts it -- before the copy, not
 * after -- because after it you are deciding what to do with a value you have
 * already built wrongly.
 */
#ifndef RC_STR_H
#define RC_STR_H


#ifdef __cplusplus
extern "C" {
#endif
/* Format into `out`, which is `cap` bytes INCLUDING the terminator.
 * Always leaves `out` NUL-terminated (given cap > 0).
 * Returns 1 if the whole string fit, 0 if it was truncated. */
int rc_strf(char *out, unsigned cap, const char *fmt, ...);

/* The common case: copy a string. Same contract. */
int rc_strcpy(char *out, unsigned cap, const char *src);

/* Copy at most `n` bytes of `src` -- for wire data that is not NUL-terminated.
 * Same contract; the result is always terminated. */
int rc_strncpy(char *out, unsigned cap, const char *src, unsigned n);

#ifdef __cplusplus
}
#endif

#endif
