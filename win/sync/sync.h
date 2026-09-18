/* Scanning and file I/O for the sync layer.
 *
 * The guest is STATELESS across restarts: it reports what it sees and holds no
 * baseline. All history lives on the bridge, which has a real disk. The only
 * durable guest state is the sync root and the tree id, both in the INI.
 */
#ifndef RC_SYNC_H
#define RC_SYNC_H


#ifdef __cplusplus
extern "C" {
#endif
#include <windows.h>

/* Comparison class. Must match bridge/paths.py exactly -- the two sides compare
 * the outputs of these functions, so a disagreement is permanent. */
#define RC_CLS_TEXT  't'    /* uniform line endings; compared normalised */
#define RC_CLS_EXACT 'b'    /* binary, mixed endings, lone CR: byte-exact */

#define RC_SYNC_MAX_DEPTH   16
#define RC_SYNC_MAX_FILES 2000
#define RC_SYNC_CHUNK     8192

/* The longest working name we form beside a target. `.gaston-tmp` and
 * `.gaston-old` are both eleven characters, and both are built by appending to
 * the target's own path.
 *
 * This is declared to the bridge as `workreserve` in HELLO so it can stop
 * short BEFORE pushing, which is the only place the problem can actually be
 * prevented. Without the reserve, a target path of 251..257 characters makes
 * the two names truncate to the SAME string: rc_sync_commit then deletes its
 * own temp file, moves the user's file aside and straight back, and returns
 * success while this machine still holds the old bytes -- so the bridge
 * advances its baseline on that ack and the next pass pulls the stale content
 * back over the agent's edit. At 258 and 259 the temp name resolves to the
 * target itself, because Win32 strips a trailing dot, and CREATE_ALWAYS
 * truncates the user's file in place before a single byte has been verified.
 *
 * Neither outcome is recoverable and neither is reported. Hence: refuse. */
#define RC_SYNC_WORK_SUFFIX 11

typedef struct {
    char  type;             /* 'f', 'd', or 'u' (unreadable -- NOT absent) */
    char  cls;              /* RC_CLS_* */
    DWORD size;             /* bytes on THIS side; never compared cross-side */
    DWORD mtime;            /* unix seconds, local; a per-side cache key only */
    unsigned long crc;      /* over the bytes `cls` selects; a convenience */
    /* BOTH checksums, always. The guest classifies from content alone, but
     * content is not the only input any more: the bridge honours .gitattributes
     * and may declare a path byte-exact that we would have called text. It has
     * no way to ask for the other checksum after the fact and we are already
     * computing both in the same read, so both go on the wire and the bridge
     * picks. Sending one was what made a `-text` override impossible: our
     * normalised CRC against their raw one never matches, and the path
     * conflicts on every pass forever. */
    unsigned long crc_raw;  /* over the file's literal bytes */
    unsigned long crc_norm; /* over the bytes with CRLF folded to LF */
} rc_sync_stat;

/* Which of the two a given class wants. One place, because getting it wrong
 * in one of the four call sites is a permanent disagreement, not a glitch. */
#define RC_CRC_FOR(st, cls) \
    ((cls) == RC_CLS_TEXT ? (st).crc_norm : (st).crc_raw)

/* Called once per entry during a walk. `rel` is relative to the root with '/'
 * separators, in the guest's ANSI bytes -- never decoded, never folded. */
typedef void (*rc_sync_sink)(void *ctx, const char *rel, const rc_sync_stat *st);

/* Walk the root. Returns the entry count, or -1 if the root is missing, or
 * -2 if the tree exceeds RC_SYNC_MAX_FILES (which must abort the whole pass:
 * a truncated manifest is indistinguishable from a mass deletion). */
int  rc_sync_scan(const char *root, rc_sync_sink sink, void *ctx);

/* Classify and checksum one file. Both CRCs are accumulated in a single read
 * pass, because the class is not known until the last byte and this machine
 * cannot afford to read twice. */
int  rc_sync_stat_file(const char *path, rc_sync_stat *out);

/* Is `rel` inside `root` and free of escapes? The guest checks too; the bridge
 * checking is not enough when the bridge is what might be wrong. */
int  rc_sync_contained(const char *rel);

/* Open a file for writing through a temp; rc_sync_commit renames it into place,
 * having first moved any existing file aside so no instant has no file at all. */
HANDLE rc_sync_begin_write(const char *root, const char *rel, char *tmp_out,
                           unsigned tmp_cap);
int    rc_sync_commit(const char *root, const char *rel, const char *tmp,
                      unsigned long want_crc, char cls);
/* Our own working names. The scanner must SKIP both, or an interrupted write
 * leaves a file the next manifest reports as ordinary -- the bridge has never
 * seen it, so it pulls it, and now both sides hold a permanent `.gaston-tmp`
 * that syncs forever. Observed: the bridge's tree grew `cafÃ©.md.gaston-tmp`
 * from a put that failed on a path bug, and nothing ever removed it. */
#define RC_SYNC_TMP_SUFFIX  ".gaston-tmp"
#define RC_SYNC_OLD_SUFFIX  ".gaston-old"
/* Both are the same length, which RC_SYNC_WORK_SUFFIX above depends on and
 * declares to the bridge as `workreserve`. Derived rather than written as 11,
 * because the literal 11 appeared at four more sites in this file alone --
 * two of them as `len - 11` on a filename, where being wrong walks off the
 * front of the string rather than producing a short one. */
#define RC_SYNC_SUFFIX_LEN  (sizeof(RC_SYNC_TMP_SUFFIX) - 1)

/* Format ONE manifest row into `out`, exactly as it goes on the wire.
 *
 * Extracted so there is one producer rather than two. The format was written
 * four times -- here, the bridge's parser, the test suite's fake producer and
 * syncprobe's fake parser -- and verified zero times, because synccheck.exe
 * printed a DIFFERENT tab-separated format of its own. So it proved the
 * classifier and the checksums and never touched the wire format, and a field
 * added on one side passed the whole suite.
 *
 * `synccheck --wire` now emits through THIS function, and the Python side
 * parses that output with the bridge's own parser, which closes the loop.
 *
 * Returns the number of bytes written, or 0 if the row would not fit. */
unsigned rc_sync_manifest_row(char *out, unsigned cap, const char *rel,
                              const rc_sync_stat *st);

/* Non-zero if `name` is one of ours and must not appear in a manifest. */
int    rc_sync_is_working_name(const char *name);

/* Recover an interrupted commit: if <p>.gaston-old exists and <p> does not,
 * put it back. Called once at startup, before the first scan. */
void   rc_sync_recover(const char *root);

#ifdef __cplusplus
}
#endif

#endif
