/* What is installed on this machine, as facts rather than conclusions.
 *
 * THE GUEST LOOKS, THE BRIDGE INTERPRETS. This module answers exactly two
 * questions per entry -- what is the value of this environment variable, and
 * does this file exist -- and reports the answers. It does not decide that a
 * toolchain "is" Open Watcom, does not walk up from a marker to a root, and
 * has no notion of one source overriding another. All of that lives on the
 * bridge, in Python, where it can change without rebuilding this program and
 * be tested without a Windows machine.
 *
 * That is not tidiness, it is the standing rule (`CLAUDE.md`: "the guest
 * makes syscalls and moves bytes"), and it is also what keeps this small
 * enough to run on a machine with 28KB free after a turn. An earlier draft
 * put an ordered rule engine here -- source dispatch, parent hops, OR-ed
 * proof files -- and it was several hundred lines in the wrong layer.
 *
 * WHY THE ANSWERS ARE EVIDENCE AND NOT PROOF. A file existing at
 * `C:\WATCOM\BINNT\WCC386.EXE` is a good reason to believe Open Watcom is
 * there. It is not a guarantee: the path is read from an INI the user can
 * edit, and any directory can be made to contain a file of that name. The
 * bridge says as much when it tells the assistant, and never treats a
 * section name as an assertion about what the tool IS -- only as a label.
 */
#ifndef RC_TOOLDET_H
#define RC_TOOLDET_H

#ifndef __cplusplus
#error "tooldet.h is C++"
#endif

#include <windows.h>

/* Caps, in the house style: bounded, and every overflow logged BY NAME
 * rather than silently dropped -- the lesson from `HDR()` in net.cpp, where
 * a full header array used to lose a header without saying which. */
#define RC_TOOLDET_MAX_SECTIONS 32
#define RC_TOOLDET_MAX_N         8   /* env1..env8, file1..file8 */

/* One fact. `kind` is "env" or "file".
 *
 *   name  the INI section it came from, or "machine" for the ones reported
 *         unconditionally
 *   kind  "env"  -- `value` is `NAME=contents`
 *         "file" -- `value` is the path, and it exists
 *
 * Return 0 to stop early; the caller stops looking. */
typedef int (*rc_tooldet_sink)(void *ctx, const char *name, const char *kind,
                               const char *value);

/* Look, and report. Returns the number of facts emitted, or -1 if neither
 * INI could be read.
 *
 * Reads `tools_detect.ini` beside the exe, then `tools.ini` beside it. The
 * second is the user's: it is never shipped, never overwritten by an
 * install, and read SECOND so a section in it wins. The first is ours and is
 * replaced on every deploy, which is why the two are separate files -- the
 * installer copies over what it ships (`tools/gen/mktools-iso.sh`), so a single
 * file would force a choice between shipping fixes and keeping user edits.
 *
 * Does not allocate. Does not write anything anywhere. Safe to call more
 * than once, though the caller should not: see `rc_tooldet_cached`. */
int rc_tooldet_run(const char *exe_dir, rc_tooldet_sink sink, void *ctx);

/* The same, done once per process and remembered.
 *
 * `rc_net_start` runs on every connection attempt, and a bridge that is down
 * produces one attempt every few seconds under backoff -- so calling the
 * uncached form from there would rescan the disk on a loop, on a machine
 * where a directory lookup is a linear scan of a FAT. The result cannot go
 * stale in a way that matters within one run of the program: if the user
 * installs something, they will be told to reconnect, which restarts it.
 *
 * Returns the cached row block, NUL-terminated, and its length through
 * `len` when that is not NULL. Empty when nothing was found. */
const char *rc_tooldet_cached(const char *exe_dir, unsigned *len);

/* The programs this machine's tools let run without a second look: every
 * `trustN=` of a section that is FOUND (the same env/file questions the
 * report asks) and not switched off (`[name] enabled=0` in tools.ini).
 * Called once per section/program; returns how many. These become the
 * `trust=` lines of PREFS -- a client fact, the bridge holds no program
 * names (CLAUDE.md). */
typedef void (*rc_tooldet_trust_sink)(void *ctx, const char *section,
                                      const char *program);
int rc_tooldet_trust(const char *exe_dir, rc_tooldet_trust_sink sink,
                     void *ctx);

#endif
