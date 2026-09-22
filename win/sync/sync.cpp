/* Scanning and file I/O for the sync layer.
 *
 * The subtle part is not the directory walk, it is agreeing with the bridge
 * about what a file IS. Both sides compute a class and a checksum, and if they
 * ever disagree about which function to apply they compare the outputs of
 * different functions forever. So the rules here mirror bridge/paths.py
 * exactly, and the reasons are repeated rather than cross-referenced -- a
 * comment in another language in another directory is not a comment anybody
 * reads while fixing this file.
 */

#include <windows.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "sync/sync.h"
#include "core/log.h"
#include "core/str.h"
#include "core/rc_cpp.h"
#include "miniz.h"

/* --- classification + checksum, in ONE read pass ------------------------- */
/*
 * The class is not known until the last byte (a NUL 30KB in still makes the
 * whole file byte-exact), but the CRC depends on the class. Reading twice on a
 * 200MHz machine with a 1998 IDE disk is not affordable, so BOTH checksums are
 * accumulated as we go and the right one is chosen at the end. Two CRC
 * accumulations cost far less than a second pass over the platter.
 */
/* The definition behind sync.h's opaque `rc_sync_scanner` -- same type, so
 * rc_sync_stat_file (below) and the public rc_sync_scanner_begin/feed/finish
 * API are the ONE accumulation, not two copies that could drift apart. */
struct rc_sync_scanner {
    unsigned long crc_raw;      /* every byte as it is on disk */
    unsigned long crc_norm;     /* CRLF collapsed to LF */
    int  saw_nul;
    int  saw_crlf;
    int  saw_bare_lf;
    int  saw_lone_cr;           /* a CR not followed by LF -- includes \r\r\n */
    int  pending_cr;            /* last byte of the previous chunk was CR */
    /* The normalised copy of the chunk being scanned. It lives HERE, in a
     * heap-allocated state block, and not as a local in scan_chunk -- see the
     * note on rc_sync_stat_file. */
    unsigned char nb[RC_SYNC_CHUNK];
};
typedef struct rc_sync_scanner scan_state;

static void scan_chunk(scan_state *s, const unsigned char *buf, unsigned n)
{
    unsigned i;
    unsigned char *nb = s->nb;
    unsigned nn = 0;

    s->crc_raw = mz_crc32(s->crc_raw, buf, n);

    for (i = 0; i < n; i++) {
        unsigned char c = buf[i];

        if (s->pending_cr) {
            /* Resolve the CR carried over from the previous chunk or byte. */
            if (c == '\n') {
                s->saw_crlf = 1;
                nb[nn++] = '\n';        /* CRLF collapses to LF */
                s->pending_cr = 0;
                continue;
            }
            /* A CR followed by anything else -- including another CR, which is
             * how \r\r\n gets here. Both are byte-exact cases: normalisation
             * is NOT idempotent on them, and a file that is rewritten on every
             * pass never converges. */
            s->saw_lone_cr = 1;
            nb[nn++] = '\r';
            s->pending_cr = 0;
        }

        if (c == 0) s->saw_nul = 1;

        if (c == '\r') {
            s->pending_cr = 1;          /* decided by the next byte */
            continue;
        }
        if (c == '\n') s->saw_bare_lf = 1;
        nb[nn++] = c;
    }
    if (nn) s->crc_norm = mz_crc32(s->crc_norm, nb, nn);
}

static char scan_class(const scan_state *s)
{
    if (s->saw_nul)      return RC_CLS_EXACT;   /* anywhere, not a sniff window */
    if (s->saw_lone_cr)  return RC_CLS_EXACT;   /* lone CR and \r\r\n */
    if (s->saw_crlf && s->saw_bare_lf) return RC_CLS_EXACT;   /* mixed */
    return RC_CLS_TEXT;
}

/* --- the incremental API, for a caller that must do something else with
 * each chunk (do_get sends it to the bridge) in the SAME pass that scans it,
 * rather than paying rc_sync_stat_file's own read first. ------------------ */

/* Resolve the CR pending at EOF (if any), classify, and hand back whichever
 * of cls/crc_raw/crc_norm/crc the caller asked for (any out pointer may be
 * NULL). rc_sync_stat_file (below) uses this for its own tail instead of a
 * second copy of the same four lines, and that is not tidiness: it is the
 * only reason a mistake here is guaranteed to be caught. do_get's read is
 * exercised only by SYNCGET over the wire, which nothing in this suite
 * drives end to end; rc_sync_stat_file's is exercised directly by
 * synccheck.exe under the guest-agreement tests. One function used by both
 * means a wrong crc/cls choice fails THOSE tests -- proven by deliberately
 * breaking this function during development and watching
 * test_guest_agreement.py's checksum tests name the exact mismatch, with
 * every other test in the suite (do_get has no test of its own) staying
 * green. Two copies would have let that break through silently. */
static void scan_finish(scan_state *s, char *cls_out, unsigned long *raw_out,
                        unsigned long *norm_out, unsigned long *crc_out)
{
    char cls;

    /* A file ending in CR: the carry never resolved, so it is a lone CR. */
    if (s->pending_cr) {
        unsigned char cr = '\r';
        s->saw_lone_cr = 1;
        s->crc_norm = mz_crc32(s->crc_norm, &cr, 1);
    }

    cls = scan_class(s);
    if (cls_out)  *cls_out  = cls;
    if (raw_out)  *raw_out  = s->crc_raw;
    if (norm_out) *norm_out = s->crc_norm;
    /* Which of the two THIS class wants -- see RC_CRC_FOR's own comment for
     * why getting this wrong is a permanent disagreement with the bridge,
     * not a glitch. */
    if (crc_out)  *crc_out  = RC_CRC_FOR(*s, cls);
}

rc_sync_scanner *rc_sync_scanner_begin(void)
{
    rc_sync_scanner *s = (rc_sync_scanner *)HeapAlloc(GetProcessHeap(), 0,
                                                       sizeof(*s));
    if (!s) return NULL;
    memset(s, 0, sizeof(*s));
    s->crc_raw = s->crc_norm = MZ_CRC32_INIT;
    return s;
}

void rc_sync_scanner_feed(rc_sync_scanner *s, const void *buf, unsigned n)
{
    scan_chunk(s, (const unsigned char *)buf, n);
}

void rc_sync_scanner_finish(rc_sync_scanner *s, char *cls_out,
                            unsigned long *crc_out)
{
    if (!s) return;
    scan_finish(s, cls_out, NULL, NULL, crc_out);
    HeapFree(GetProcessHeap(), 0, s);
}

static DWORD to_unix(const FILETIME *ft)
{
    /* FILETIME is 100ns since 1601; unix epoch is 11644473600 seconds later.
     * Only ever compared against other timestamps from THIS side -- mtime is a
     * cache key, never an argument about which side is right. */
    ULARGE_INTEGER u;
    u.LowPart = ft->dwLowDateTime;
    u.HighPart = ft->dwHighDateTime;
    return (DWORD)((u.QuadPart / 10000000ULL) - 11644473600ULL);
}

/* The scratch for one file scan: the 8KB read buffer and the scan state, which
 * carries another 8KB for the normalised copy.
 *
 * ON THE HEAP, DELIBERATELY, and this is not a micro-optimisation -- it is the
 * fix for a real invalid-page-fault on the guest. The RPC worker thread gets
 * the image's 64KB stack COMMIT, and do_scan already holds a ~62KB manifest
 * buffer in its frame when it calls down here. Open Watcom emits a plain
 * `sub esp, 0x202c` for an 8KB frame with no stack probe, so the next push
 * lands BELOW the guard page rather than on it: the page is never committed,
 * and the process dies with "invalid page fault in module RETROCL.EXE".
 *
 * It is also why this looked intermittent. It ran for a whole session against
 * a flat tree and faulted the first time the tree gained a subdirectory --
 * one more `walk` frame, about 1.1KB, was enough to move the skip past the
 * guard. Nothing about the new directory was wrong; it just shifted the
 * arithmetic. A bug you cannot reproduce by trying the same thing twice. */
typedef struct {
    scan_state    st;
    unsigned char buf[RC_SYNC_CHUNK];
} scan_scratch;

int rc_sync_stat_file(const char *path, rc_sync_stat *out)
{
    scan_scratch *sc;
    scan_state *stp;
    unsigned char *buf;
    DWORD got = 0;
    FILETIME ft;

    DWORD attr;

    memset(out, 0, sizeof(*out));

    /* Ask what the path IS before trying to read it.
     *
     * Without this, every CreateFileA failure came back as type 'u', so a
     * MISSING file and a DIRECTORY were both indistinguishable from a file
     * held under a share-deny lock. The bridge treats `locked` as transient
     * and retries forever, so a pull of something the user had just deleted
     * never resolved and never reported anything. Caught by driving the real
     * guest: SYNCGET on a nonexistent path answered `locked`.
     *
     * type 0 means "nothing here", which is distinct from 'u' ("something is
     * here and we cannot read it") and from 'f'. */
    attr = GetFileAttributesA(path);
    if (attr == 0xFFFFFFFF) return 0;
    if (attr & FILE_ATTRIBUTE_DIRECTORY) { out->type = 'd'; return 0; }

    sc = (scan_scratch *)HeapAlloc(GetProcessHeap(), 0, sizeof(scan_scratch));
    if (!sc) {
        /* Out of memory is not "the file is fine". Unreadable is the honest
         * answer and the one the bridge already knows to skip for a pass. */
        out->type = 'u';
        return 0;
    }
    stp = &sc->st;
    buf = sc->buf;

    memset(stp, 0, sizeof(*stp));
    stp->crc_raw = stp->crc_norm = MZ_CRC32_INIT;

    /* FILE_SHARE_READ | FILE_SHARE_WRITE: a Win98 editor holds a share-deny
     * lock, and a file we cannot read must be reported UNREADABLE rather than
     * omitted. Omitting it reads as absent, which becomes a tombstone, and then
     * the user's later edits are invisible forever. */
    Handle h(CreateFileA(path, GENERIC_READ, FILE_SHARE_READ | FILE_SHARE_WRITE,
                    NULL, OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, NULL));
    if (!h.valid()) {
        out->type = 'u';
        HeapFree(GetProcessHeap(), 0, sc);
        return 0;
    }

    for (;;) {
        if (!ReadFile(h.get(), buf, RC_SYNC_CHUNK, &got, NULL)) {
            out->type = 'u';
            HeapFree(GetProcessHeap(), 0, sc);
            return 0;
        }
        if (!got) break;
        scan_chunk(stp, buf, got);
        out->size += got;
    }
    if (GetFileTime(h.get(), NULL, NULL, &ft)) out->mtime = to_unix(&ft);

    out->type = 'f';
    /* The tail fixup (CR pending at EOF), classification and crc_raw/
     * crc_norm/crc choice are ALL in scan_finish, shared with the public
     * rc_sync_scanner_finish that do_get uses -- see that function's comment
     * for why sharing it, rather than keeping two copies of this tail, is
     * what makes a wrong choice here provably fail this suite. */
    scan_finish(stp, &out->cls, &out->crc_raw, &out->crc_norm, &out->crc);
    HeapFree(GetProcessHeap(), 0, sc);
    return 0;
}

/* --- containment --------------------------------------------------------- */

/* Parse a SYNCPUT body's length-prefixed path into a bounded, validated
 * relative path. One function, so the thing that runs is the thing the tests
 * run -- `synccheck --putpath` drives THIS, over bodies with real file
 * content glued to the tail.
 *
 * The body is "<len> <rel>" followed IMMEDIATELY by up to 8KB of the file's
 * own bytes, with no separator (bridge/files/synctransport.py builds it as
 * prefix + piece). Everything here follows from that:
 *
 *   - the separator must actually be present, or `body + plen + 1` is one
 *     past the frame allocation and every read after it walks adjacent heap
 *     (Win98 has no guard page behind a small block, so it does not fault);
 *   - the copy is bounded by the bridge's `<len>`, which is unbounded wire
 *     data, and its return is checked -- a silently cut path is a DIFFERENT
 *     file, and it goes on to rc_sync_begin_write and rc_sync_commit, which
 *     delete and rename;
 *   - containment is checked on the BOUNDED COPY, never on the raw pointer.
 *     Checking the pointer scanned the path AND the file data as one string,
 *     so any push whose first chunk held `//` -- an empty component, and the
 *     line-comment token of half the languages the agent writes -- came back
 *     bad_path forever, naming the one thing that was not wrong with it.
 *
 * Returns RC_PUTPATH_OK, or one of the refusals below. (audit 01.2/01.6/01.30)
 */
int rc_syncrpc_put_path(const char *body, unsigned body_len,
                        char *out, unsigned cap, unsigned *data_off)
{
    unsigned plen = 0, rn;
    const char *rel;

    if (data_off) *data_off = 0;
    if (!body || !out || !cap) return RC_PUTPATH_NOSEP;
    out[0] = '\0';

    while (plen < 16 && plen < body_len && body[plen] && body[plen] != ' ')
        plen++;
    if (plen >= body_len || body[plen] != ' ') return RC_PUTPATH_NOSEP;

    rn = (unsigned)atoi(body);
    rel = body + plen + 1;
    /* The declared length must lie inside the frame. A bridge that says 900
     * on a 40-byte body is wrong, and believing it reads past the end. */
    if (rn == 0 || rn > body_len - (plen + 1)) return RC_PUTPATH_NOSEP;

    if (!rc_strf(out, cap, "%.*s", (int)rn, rel)) return RC_PUTPATH_TOOLONG;
    if (!rc_sync_contained(out)) { out[0] = '\0'; return RC_PUTPATH_BADPATH; }
    /* Where the file's own bytes start. Derived HERE rather than recomputed
     * by the caller, so the offset the data is written from cannot drift from
     * the prefix this function actually consumed. */
    if (data_off) *data_off = plen + 1 + rn;
    return RC_PUTPATH_OK;
}

int rc_sync_contained(const char *rel)
{
    const char *p = rel;

    if (!rel || !*rel) return 0;
    if (rel[0] == '/' || rel[0] == '\\') return 0;
    if (rel[1] == ':') return 0;                     /* a drive spec */

    /* Reject "..", and "." as a whole component, without resolving anything.
     * Resolving would mean deciding what a path means; refusing means the
     * question never arises.
     *
     * BOTH separators, and that is the whole of the bug this replaces. The
     * split was on '/' alone, so `a\..\..\x` contained no '/' at all and was
     * read as ONE nine-character component -- not "..", not ".", not empty --
     * and passed. join() then wrote `<root>\a\..\..\x` and handed it to Win32,
     * which resolves `..` and leaves the sync root entirely. A bridge could
     * write anywhere the guest's user can.
     *
     * The wire format says components are '/'-separated and '\' is on the
     * illegal-character list precisely so it cannot appear inside one -- but
     * that is a rule the BRIDGE enforces, and this function exists for the
     * case where the bridge is the thing that is wrong. It cannot lean on a
     * guarantee made by the party it is checking. */
    while (*p) {
        const char *seg = p;
        unsigned len = 0;
        while (*p && *p != '/' && *p != '\\') { p++; len++; }
        if (len == 2 && seg[0] == '.' && seg[1] == '.') return 0;
        if (len == 1 && seg[0] == '.') return 0;
        if (len == 0) return 0;                      /* empty component */
        if (*p == '/' || *p == '\\') p++;
    }
    return 1;
}

/* Returns 1 if the whole path fit, 0 if it was truncated.
 *
 * It used to return void, and that is not a detail. A truncated join produces
 * a VALID-LOOKING path to a different file, so every caller went on to stat,
 * open or rename the wrong thing with no way to find out. Two of the bugs
 * fixed on 2026-09-18 came through here. Callers that act on the result now
 * check it.
 *
 * Exported (was `static`) because it is the one place that turns a root and a
 * '/'-separated relative path into the backslashed form Win32 wants, and
 * syncrpc.c had open-coded the same six lines at three of its own call sites
 * instead of linking against it -- so "the shared helper" was shared with
 * nobody. A join bug fixed here used to need fixing again there. */
int rc_sync_join(char *out, unsigned cap, const char *root, const char *rel)
{
    char *q;
    int ok = rc_strf(out, cap, "%s\\%s", root, rel);
    for (q = out + strlen(root); *q; q++)
        if (*q == '/') *q = '\\';
    return ok;
}

/* --- the walk ------------------------------------------------------------ */

typedef struct {
    rc_sync_sink sink;
    void        *ctx;
    int          count;
    int          overflow;
} walk_ctx;

unsigned rc_sync_manifest_row(char *out, unsigned cap, const char *rel,
                              const rc_sync_stat *st)
{
    char head[128];
    unsigned hn, rn;

    if (!out || !cap || !rel || !st) return 0;
    rn = (unsigned)strlen(rel);
    /* Both checksums. The bridge decides the class -- it is the side that
     * reads .gitattributes -- so it must be able to pick either without
     * asking again, and neither is derivable from the other.
     *
     * Length-prefixed rather than separated, because TAB and newline are both
     * legal in a filename and a separator-based format breaks on the first
     * one. That also means a truncated row is not a SHORT row: the prefix
     * stops matching the bytes after it and the parser reads the next row's
     * fields as this one's path. Hence the refusal below rather than a clamp. */
    rc_strf(head, sizeof(head), "%c %c %lu %lu %lu %lu %u ",
            st->type, st->cls ? st->cls : '-',
            (unsigned long)st->size, (unsigned long)st->mtime,
            st->crc_norm, st->crc_raw, rn);
    hn = (unsigned)strlen(head);
    if (hn + rn + 1 > cap) return 0;

    memcpy(out, head, hn);
    memcpy(out + hn, rel, rn);
    out[hn + rn] = '\n';
    return hn + rn + 1;
}

int rc_sync_is_working_name(const char *name)
{
    unsigned len = (unsigned)strlen(name);
    if (len <= RC_SYNC_SUFFIX_LEN) return 0;
    if (strcmp(name + len - RC_SYNC_SUFFIX_LEN, RC_SYNC_TMP_SUFFIX) == 0) return 1;
    if (strcmp(name + len - RC_SYNC_SUFFIX_LEN, RC_SYNC_OLD_SUFFIX) == 0) return 1;
    return 0;
}

static void walk(const char *root, const char *rel, int depth, walk_ctx *w)
{
    char pattern[MAX_PATH];
    char child[MAX_PATH];
    char full[MAX_PATH];
    WIN32_FIND_DATAA fd;

    if (depth > RC_SYNC_MAX_DEPTH || w->overflow) return;

    if (*rel) rc_sync_join(pattern, sizeof(pattern), root, rel);
    else      rc_strf(pattern, sizeof(pattern), "%s", root);
    /* Bounded, but strncat's failure is a pattern with the "\\*" missing or
     * half-appended rather than an error -- and FindFirstFileA then either
     * fails (the subtree vanishes) or succeeds on the bare directory (the
     * scan invents directories). The caller has already refused anything that
     * would not fit; this is the assertion that it did. */
    if (!rc_strcpy(pattern + strlen(pattern),
                   sizeof(pattern) - strlen(pattern), "\\*"))
        return;

    FindHandle h(FindFirstFileA(pattern, &fd));
    if (!h.valid()) return;

    do {
        rc_sync_stat st;

        if (fd.cFileName[0] == '.' &&
            (fd.cFileName[1] == '\0' ||
             (fd.cFileName[1] == '.' && fd.cFileName[2] == '\0')))
            continue;

        /* Never report our own working files. rc_sync_recover knows these
         * names; the scanner did not, so a half-written `.gaston-tmp` was
         * offered to the bridge as an ordinary new file and pulled. */
        if (rc_sync_is_working_name(fd.cFileName)) continue;

        /* ALWAYS the long name, never cAlternateFileName. The 8.3 alias is not
         * stable -- deleting one file renumbers ~1/~2 for the others -- so
         * keying on it would rename files at random. */
        /* CHECKED, because `child` feeds the joins below, and those feed a
         * DeleteFileA and a MoveFileA. A truncated relative path names a
         * different file, and this function runs at the top of every
         * rc_workdir_apply -- i.e. whenever a conversation commits to a
         * folder -- inside the user's own work directory. Skipping the entry
         * loses a recovery; acting on a truncated one loses a file. */
        if (*rel) {
            if (!rc_strf(child, sizeof(child), "%s/%s", rel, fd.cFileName))
                continue;
        } else {
            if (!rc_strf(child, sizeof(child), "%s", fd.cFileName))
                continue;
        }

        if (w->count >= RC_SYNC_MAX_FILES) { w->overflow = 1; break; }

        if (fd.dwFileAttributes & FILE_ATTRIBUTE_DIRECTORY) {
            memset(&st, 0, sizeof(st));
            /* Can this directory's own listing pattern even be formed?
             * root + '\' + child + "\*" + NUL. If not, both ways of finding
             * out are silent and both are wrong:
             *
             *   - one byte short, and strncat appends only the backslash;
             *     FindFirstFileA then fails and the ENTIRE SUBTREE vanishes
             *     from the manifest, which the bridge reads as the user having
             *     deleted every file in it.
             *   - exactly full, and strncat appends nothing; FindFirstFileA
             *     succeeds on the bare directory path, returns the directory's
             *     own entry, and walk recurses on the same truncated pattern
             *     to RC_SYNC_MAX_DEPTH -- inventing a chain of directories the
             *     bridge then dutifully creates with os.makedirs.
             *
             * `u` says what is true: it is there and we cannot read it. */
            if (strlen(root) + 1 + strlen(child) + 3 > sizeof(pattern)) {
                rc_log("sync: cannot list %s -- no room for a search pattern",
                       child);
                st.type = 'u';
                w->sink(w->ctx, child, &st);
                w->count++;
                continue;                       /* and do NOT recurse */
            }
            st.type = 'd';
            w->sink(w->ctx, child, &st);
            w->count++;
            walk(root, child, depth + 1, w);
        } else {
            /* A truncated join names a DIFFERENT file, so stat'ing it would
             * report someone else's size and checksum under this path. `u`
             * says what is true: it is there and we cannot address it. */
            if (!rc_sync_join(full, sizeof(full), root, child)) {
                memset(&st, 0, sizeof(st));
                st.type = 'u';
                w->sink(w->ctx, child, &st);
                w->count++;
                continue;
            }
            rc_sync_stat_file(full, &st);
            /* type 0 means the stat found nothing where FindNextFileA had just
             * seen something. Emitting it as-is put a row on the wire whose
             * first field was a NUL: _snprintf("%c", 0) writes an empty string,
             * so the line carried no fields at all, the bridge failed to parse
             * it, and the manifest count then disagreed with `files=` -- which
             * aborts THE WHOLE PASS, with a log warning as the only symptom.
             * Same shape as the directory-versus-file crash.
             *
             * `u` rather than skipping the row. Skipping is right for the race
             * that suggests itself (the file really was deleted between the
             * enumerate and the stat, so absent is true and the next pass
             * agrees). But the stat also fails PERMANENTLY for a name
             * FindFirstFileA returns with a substitute character for a byte
             * outside the codepage -- and omitting that row tells the bridge
             * the user deleted a file they can see in Explorer. `u` says
             * "something is here and I cannot read it", which is true in both
             * cases, keeps the count honest, and costs the race one pass. */
            if (!st.type) st.type = 'u';
            w->sink(w->ctx, child, &st);
            w->count++;
        }
    } while (FindNextFileA(h.get(), &fd));
}

int rc_sync_scan(const char *root, rc_sync_sink sink, void *ctx)
{
    walk_ctx w;
    DWORD attr = GetFileAttributesA(root);

    if (attr == 0xFFFFFFFF || !(attr & FILE_ATTRIBUTE_DIRECTORY)) return -1;

    w.sink = sink; w.ctx = ctx; w.count = 0; w.overflow = 0;
    walk(root, "", 0, &w);
    /* A truncated manifest is indistinguishable from a mass deletion, so the
     * caller must abort the whole pass rather than act on what it received. */
    return w.overflow ? -2 : w.count;
}

/* --- writing ------------------------------------------------------------- */

HANDLE rc_sync_begin_write(const char *root, const char *rel, char *tmp_out,
                           unsigned tmp_cap)
{
    char full[MAX_PATH];
    unsigned need;

    /* Terminate the caller's buffer FIRST, on every path including the two
     * refusals below.
     *
     * Both used to return without touching tmp_out, so a caller that logged
     * or measured it afterwards read uninitialised stack -- win/checks/
     * synccheck.c does exactly that, and it is the binary that verifies this
     * function. An empty string is the honest answer: no temp file was
     * created, so there is no name. */
    if (tmp_out && tmp_cap) tmp_out[0] = '\0';

    if (!rc_sync_contained(rel)) return INVALID_HANDLE_VALUE;

    /* Refuse rather than truncate, and refuse BEFORE anything is opened.
     *
     * Measured from the inputs rather than from the joined string, because the
     * join itself truncates silently and would hide the very thing being
     * tested. See RC_SYNC_WORK_SUFFIX for what a truncated working name does:
     * at some lengths it reports success having written nothing, and at others
     * it destroys the user's file outright.
     *
     * ERROR_FILENAME_EXCED_RANGE because do_put maps it to `too_long`. Setting
     * it explicitly also stops that classifier reading a STALE error from
     * whatever ran last, which is what a bare failing return would leave it
     * doing -- reporting `readonly` or `no_space` at random. */
    need = (unsigned)strlen(root) + 1 + (unsigned)strlen(rel);
    if (need + RC_SYNC_WORK_SUFFIX > sizeof(full) - 1 ||
        need + RC_SYNC_WORK_SUFFIX > tmp_cap - 1) {
        rc_log("sync: refusing %s -- %u chars leaves no room for a "
               ".gaston-tmp beside it", rel, need);
        SetLastError(ERROR_FILENAME_EXCED_RANGE);
        return INVALID_HANDLE_VALUE;
    }

    rc_sync_join(full, sizeof(full), root, rel);
    rc_strf(tmp_out, tmp_cap, "%s" RC_SYNC_TMP_SUFFIX, full);
    tmp_out[tmp_cap - 1] = '\0';

    /* Binary mode by construction -- CreateFileA does no translation. Using
     * the CRT in text mode would translate on top of the conversion the bridge
     * already did and add a CR to every line, on every pass. */
    return CreateFileA(tmp_out, GENERIC_WRITE, 0, NULL, CREATE_ALWAYS,
                       FILE_ATTRIBUTE_NORMAL, NULL);
}

int rc_sync_commit(const char *root, const char *rel, const char *tmp,
                   unsigned long want_crc, char cls)
{
    char full[MAX_PATH], aside[MAX_PATH];
    rc_sync_stat got;
    DWORD attr;

    /* MEASURED FROM THE INPUTS, not from the joined string -- the same rule
     * rc_sync_begin_write states, and for the same reason: the join truncates
     * silently, so a length test on its OUTPUT is a test on the evidence the
     * truncation already destroyed.
     *
     * This mattered more here than anywhere else, because this is the function
     * that deletes and renames. A truncated `full` would have made
     * GetFileAttributesA, MoveFileA(full, aside) and MoveFileA(tmp, full) all
     * act on an unrelated file -- moving the user's file aside, writing the
     * synced bytes over it, then deleting the displaced original -- and then
     * `return 0`, so the bridge advances its baseline over content this
     * machine never took.
     *
     * It was safe before this only by accident: `full` and `aside` are both
     * MAX_PATH, so a truncated `full` was always long enough to trip the
     * `aside` reserve check below. Enlarging `aside` -- the obvious way to
     * stop the -4 refusals, which read like an over-strict limit -- would have
     * removed the guard with nothing to say so. */
    if (!rc_sync_join(full, sizeof(full), root, rel)) {
        rc_log("sync: refusing to commit %s -- the joined path does not fit",
               rel);
        DeleteFileA(tmp);
        return -4;
    }

    /* Verify before replacing anything. A transfer that cannot establish
     * crc(guest) == what was sent is reported, never retried. */
    /* Two things were wrong here. The temp was NOT deleted on this path,
     * alone among the refusals in this function (-4 above, -2, -4 and -3
     * below all delete first), so a transient stat failure right after the
     * CloseHandle left the .gaston-tmp behind. And the `!= 0` half could
     * never fire: rc_sync_stat_file reports through out->type and every one
     * of its six returns is `return 0`, so this read as though it checked
     * two things and checked one. See docs/mem-audit F2. */
    rc_sync_stat_file(tmp, &got);
    if (got.type != 'f') {
        rc_log("sync: cannot verify %s after writing it -- not committing", rel);
        DeleteFileA(tmp);
        return -1;
    }
    /* Against the checksum the DECLARED class selects, not against our own
     * classification of the bytes. `got.cls != cls` used to be a hard failure
     * here, which made a bridge-side `-text` override unusable: the bridge
     * declares byte-exact, we look at the content and call it text, and every
     * transfer comes back crc_mismatch forever. The bridge is the side that
     * knows about .gitattributes; our classifier is a default, not a veto.
     * The checksum comparison is what actually proves the bytes arrived
     * intact, and it still runs. */
    if (RC_CRC_FOR(got, cls) != want_crc) {
        DeleteFileA(tmp);
        return -2;
    }

    /* A read-only target: DeleteFileA fails on it, then MoveFileA fails because
     * the target exists, and the temp is left in the tree where the next scan
     * picks it up as a new file and pushes it to the bridge. ReadOnlyGuard
     * clears the attribute now and restores it on every path out (-3, -4, or 0),
     * so a failed commit does not leave the file silently stripped of write
     * protection (05.4). */
    attr = GetFileAttributesA(full);
    ReadOnlyGuard ro(full, attr);

    /* Move the old file ASIDE rather than deleting it. There is still an
     * instant with no file at the path -- MoveFileExA(REPLACE_EXISTING) is
     * NT-only so this cannot be made atomic -- but every intermediate state
     * now holds a full copy under a name rc_sync_recover knows. */
    /* The same reserve as begin_write, checked again here because THIS is the
     * function that deletes and renames. If `aside` were to alias `tmp`, the
     * DeleteFileA below would destroy the verified new bytes and the two moves
     * would put the user's old file straight back -- and we would return 0,
     * telling the bridge to advance its baseline over content this machine
     * never took. Refusing costs a retry; not refusing costs the edit. */
    if (strlen(full) + RC_SYNC_WORK_SUFFIX > sizeof(aside) - 1) {
        rc_log("sync: refusing to commit %s -- no room for a %s", rel,
               RC_SYNC_OLD_SUFFIX);
        DeleteFileA(tmp);
        return -4;
    }

    rc_strf(aside, sizeof(aside), "%s" RC_SYNC_OLD_SUFFIX, full);
    DeleteFileA(aside);
    if (attr != 0xFFFFFFFF) MoveFileA(full, aside);

    if (!MoveFileA(tmp, full)) {
        if (attr != 0xFFFFFFFF) MoveFileA(aside, full);   /* put it back */
        DeleteFileA(tmp);
        return -3;
    }
    DeleteFileA(aside);
    return 0;
}

static void recover_dir(const char *root, const char *rel, int depth)
{
    char pattern[MAX_PATH], child[MAX_PATH], aside[MAX_PATH], real[MAX_PATH];
    WIN32_FIND_DATAA fd;

    if (depth > RC_SYNC_MAX_DEPTH) return;
    if (*rel) rc_sync_join(pattern, sizeof(pattern), root, rel);
    else      rc_strf(pattern, sizeof(pattern), "%s", root);
    /* Bounded, but strncat's failure is a pattern with the "\\*" missing or
     * half-appended rather than an error -- and FindFirstFileA then either
     * fails (the subtree vanishes) or succeeds on the bare directory (the
     * scan invents directories). The caller has already refused anything that
     * would not fit; this is the assertion that it did. */
    if (!rc_strcpy(pattern + strlen(pattern),
                   sizeof(pattern) - strlen(pattern), "\\*"))
        return;

    FindHandle h(FindFirstFileA(pattern, &fd));
    if (!h.valid()) return;
    do {
        unsigned len = (unsigned)strlen(fd.cFileName);
        if (fd.cFileName[0] == '.' &&
            (fd.cFileName[1] == '\0' ||
             (fd.cFileName[1] == '.' && fd.cFileName[2] == '\0')))
            continue;
        if (*rel) rc_strf(child, sizeof(child), "%s/%s", rel, fd.cFileName);
        else      rc_strf(child, sizeof(child), "%s", fd.cFileName);

        if (fd.dwFileAttributes & FILE_ATTRIBUTE_DIRECTORY) {
            recover_dir(root, child, depth + 1);
            continue;
        }
        if (len > RC_SYNC_SUFFIX_LEN &&
            strcmp(fd.cFileName + len - RC_SYNC_SUFFIX_LEN,
                   RC_SYNC_OLD_SUFFIX) == 0) {
            /* Both returns checked: see the note on `child` above, and
             * rc_sync_join's own header -- "a caller that ignores the return
             * value stats, opens or renames the wrong thing with no way to
             * find out." walk() at the top of this file is the model. */
            if (!rc_sync_join(aside, sizeof(aside), root, child)) {
                rc_log("sync: cannot recover %s -- the joined path does not "
                       "fit", child);
                continue;
            }
            if (!rc_strf(real, sizeof(real), "%s", aside)) continue;
            /* Safe only transitively -- fd.cFileName was proven longer than
             * the suffix by the strcmp guard above, and MAX_PATH dwarfs it --
             * but nothing at THIS line says so, and an unsigned subtraction
             * that underflows here indexes wildly. State the invariant. */
            if (strlen(real) <= RC_SYNC_SUFFIX_LEN) continue;
            real[strlen(real) - RC_SYNC_SUFFIX_LEN] = '\0';
            if (GetFileAttributesA(real) == 0xFFFFFFFF) {
                rc_log("sync: recovering %s -- an interrupted commit", real);
                MoveFileA(aside, real);
            } else {
                DeleteFileA(aside);
            }
        } else if (len > RC_SYNC_SUFFIX_LEN &&
                   strcmp(fd.cFileName + len - RC_SYNC_SUFFIX_LEN,
                          RC_SYNC_TMP_SUFFIX) == 0) {
            /* Checked for the same reason, and this one DELETES. */
            if (!rc_sync_join(aside, sizeof(aside), root, child)) {
                rc_log("sync: not deleting a temp for %s -- the joined path "
                       "does not fit, and a truncated one names another file",
                       child);
                continue;
            }
            DeleteFileA(aside);       /* an abandoned transfer */
        }
    } while (FindNextFileA(h.get(), &fd));
}

void rc_sync_recover(const char *root)
{
    recover_dir(root, "", 0);
}
