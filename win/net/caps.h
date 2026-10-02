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
 * Every value here is a compile-time literal except two: the codepage, which
 * is one GetACP() call, and `automation`, which is a setting the person at the
 * machine chose. This client holds no tables and computes nothing: it reports
 * what it is, and the bridge holds what that implies. Same split as the rest
 * of the codebase.
 *
 * Mirrored in bridge/caps.py. Change one, change the other.
 */
#ifndef RC_CAPS_H
#define RC_CAPS_H


/* For RC_SYNC_WORK_SUFFIX, checked against RC_WORKRESERVE_GASTON below.
 * net and sync are sibling layers (see win/Makefile's VPATH comment) and
 * net.cpp already depends on sync (sync/syncrpc.h) at the .cpp level, so this
 * does not invert anything -- it just lets a compile-time check reach the
 * other header instead of trusting the prose cross-reference to stay true. */
#include "sync/sync.h"

#ifdef __cplusplus
extern "C" {
#endif
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

#define RC_DEFLATE_NONE     0
#define RC_DEFLATE_STREAM   1

/* Whether this client can be DRIVEN -- read its own screen, click, type,
 * photograph it, raise a window. The second value here that is not a
 * compile-time literal (the codepage is the first): it comes from
 * `[ui] automation` in gaston.ini, because a machine that can drive a screen
 * is not the same question as whether the person at it wants their mouse
 * moved. rc_net_set_automation carries it to the handshake.
 *
 * Room to grow, which is why it is an enum and not a flag: a client that can
 * photograph a screen but not type into it, or enumerate windows without
 * being able to capture them, is a third and a fourth value here and no new
 * header. A bridge that does not recognise the value REFUSES rather than
 * assuming 0 or 1 -- see bridge/files/caps.py.
 *
 * NONE is the restrictive reading and so it is 0, which also means a client
 * that never sets the field (the check harnesses in win/checks, which have
 * no screen to drive) declares the safe thing by construction. */
#define RC_AUTOMATION_NONE   0  /* no screen: the five ui tools do not exist */
#define RC_AUTOMATION_SCREEN 1  /* read the screen, click, type, shoot, focus */
/* SCREEN plus UIRUN: a list of steps in one request, run on the guest with
 * held-key release, the focus check and its own timeouts. A client that
 * declares it is offered the `input` tool in place of click/keys/focus. */
#define RC_AUTOMATION_STEPS  2

/* Which key NAMES this client resolves -- the version of the bridge's
 * frozen, append-only table (bridge/agent/keynames.py, KEYS_V1). A name
 * newer than this is refused on the bridge, naming the step, rather than
 * sent to a guest that would refuse it. */
#define RC_CAP_KEYS 1

/* How many characters short of the path limit a DIRECTORY must stop.
 * CreateDirectory refuses within 12 of MAX_PATH so an 8.3 name still fits
 * inside; Unix reserves none. A NUMBER, not a flag, so a client with a
 * different reservation says so without anyone adding a field. */
#define RC_DIRRESERVE_WIN32 12
#define RC_DIRRESERVE_NONE  0

/* How many characters short of the path limit a FILE must stop, so that this
 * client can still form its own working names beside it. Ours are
 * `.gaston-tmp` and `.gaston-old`, eleven characters each -- see
 * RC_SYNC_WORK_SUFFIX in sync.h for what happens when they do not fit, which
 * is worse than a failed write.
 *
 * Directories had a reserve from the first day and files did not, which is the
 * entire bug. A client that commits in place declares 0 and gets the bytes. */
#define RC_WORKRESERVE_GASTON 11
#define RC_WORKRESERVE_NONE   0

/* 03.13: this and RC_SYNC_WORK_SUFFIX (sync/sync.h) name the same eleven
 * bytes from two headers, tied together only by the prose comments above and
 * on RC_SYNC_WORK_SUFFIX itself. If sync.h's reserve ever grew while this one
 * did not, the guest would DECLARE a workreserve the bridge honours and then
 * refuse paths one character short of that promise -- reported per file as
 * `too_long` forever, with nothing here to say why. Caught at compile time,
 * because a drift here is wrong on every build from the moment it happens,
 * not just the ones a test run happens to cover. A negative array size is
 * the C89 static_assert; this codebase has no other, so it is spelled out
 * here rather than adding a macro used exactly once. */
typedef char rc_workreserve_matches_sync_h
    [(RC_WORKRESERVE_GASTON == RC_SYNC_WORK_SUFFIX) ? 1 : -1];

/* This client. A Windows 3.1 or MS-DOS build changes these lines and nothing
 * else; a Unix build sets them all to the permissive value. */
#define RC_CAP_CASE        RC_CASE_FOLD
#define RC_CAP_DEVICES     RC_DEVICES_DOS
#define RC_CAP_TRAILING    RC_TRAILING_STRIP
#define RC_CAP_ILLEGAL     RC_ILLEGAL_DOS
#define RC_CAP_EOL         RC_EOL_CRLF
#define RC_CAP_SHORTNAMES  RC_SHORTNAMES_83
#define RC_CAP_DIRRESERVE  RC_DIRRESERVE_WIN32
#define RC_CAP_WORKRESERVE RC_WORKRESERVE_GASTON
#define RC_CAP_MAXPATH     MAX_PATH
#define RC_CAP_MAXCOMP     255

/* How deep our own scanner walks. THE SYMBOL ITSELF, not a copy of its value:
 * the bridge refuses a path deeper than this before pushing it, because the
 * walk gives NO signal when it gives up -- a deeper path is written, acked,
 * and then never reported by a scan again, which reads as "deleted on the
 * guest" forever.
 *
 * RC_WORKRESERVE_GASTON needed a static assert to stay tied to sync.h because
 * the two are different numbers that must agree. This one is the same number,
 * so there is nothing to drift and nothing to assert. */
#define RC_CAP_MAXDEPTH    RC_SYNC_MAX_DEPTH

/* How many files the client's scanner will count before aborting with
 * too_many.  THE SYMBOL ITSELF, like maxdepth: a change in sync.h is
 * picked up by the handshake without a second edit.  The bridge quotes
 * this number in the too_many message (syncrun.py) -- if it has to guess
 * instead, the message says 2000 and the client may have moved on. */
#define RC_CAP_MAXFILES    RC_SYNC_MAX_FILES

/* Manifest row format. v2 carries both checksums per row -- the normalised one
 * and the raw one -- so the bridge can honour a .gitattributes `-text` without
 * asking us to scan the tree a second time under a different rule. v1 carried
 * whichever one our own classifier chose, and the other was not derivable, so
 * an override compared the output of one function against the other and the
 * path conflicted on every pass forever.
 *
 * Declared rather than sniffed: a path may contain spaces and the rows are
 * length-prefixed, so both formats parse either way and the bridge would guess
 * wrong on some trees and not others. */
#define RC_CAP_MANIFEST    2
#define RC_CAP_DEFLATE     RC_DEFLATE_STREAM

#ifdef __cplusplus
}
#endif

#endif
