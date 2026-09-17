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
#include <string.h>
#include "sync.h"
#include "log.h"
#include "miniz.h"

void rc_sync_init(void) { }

/* --- classification + checksum, in ONE read pass ------------------------- */
/*
 * The class is not known until the last byte (a NUL 30KB in still makes the
 * whole file byte-exact), but the CRC depends on the class. Reading twice on a
 * 200MHz machine with a 1998 IDE disk is not affordable, so BOTH checksums are
 * accumulated as we go and the right one is chosen at the end. Two CRC
 * accumulations cost far less than a second pass over the platter.
 */
typedef struct {
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
} scan_state;

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
    HANDLE h;
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
    h = CreateFileA(path, GENERIC_READ, FILE_SHARE_READ | FILE_SHARE_WRITE,
                    NULL, OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, NULL);
    if (h == INVALID_HANDLE_VALUE) {
        out->type = 'u';
        HeapFree(GetProcessHeap(), 0, sc);
        return 0;
    }

    for (;;) {
        if (!ReadFile(h, buf, RC_SYNC_CHUNK, &got, NULL)) {
            CloseHandle(h);
            out->type = 'u';
            HeapFree(GetProcessHeap(), 0, sc);
            return 0;
        }
        if (!got) break;
        scan_chunk(stp, buf, got);
        out->size += got;
    }
    /* A file ending in CR: the carry never resolved, so it is a lone CR. */
    if (stp->pending_cr) {
        unsigned char cr = '\r';
        stp->saw_lone_cr = 1;
        stp->crc_norm = mz_crc32(stp->crc_norm, &cr, 1);
    }

    if (GetFileTime(h, NULL, NULL, &ft)) out->mtime = to_unix(&ft);
    CloseHandle(h);

    out->type = 'f';
    out->cls = scan_class(stp);
    out->crc_raw  = stp->crc_raw;
    out->crc_norm = stp->crc_norm;
    out->crc = RC_CRC_FOR(*out, out->cls);
    HeapFree(GetProcessHeap(), 0, sc);
    return 0;
}

/* --- containment --------------------------------------------------------- */

int rc_sync_contained(const char *rel)
{
    const char *p = rel;

    if (!rel || !*rel) return 0;
    if (rel[0] == '/' || rel[0] == '\\') return 0;
    if (rel[1] == ':') return 0;                     /* a drive spec */

    /* Reject "..", and "." as a whole component, without resolving anything.
     * Resolving would mean deciding what a path means; refusing means the
     * question never arises. */
    while (*p) {
        const char *seg = p;
        unsigned len = 0;
        while (*p && *p != '/') { p++; len++; }
        if (len == 2 && seg[0] == '.' && seg[1] == '.') return 0;
        if (len == 1 && seg[0] == '.') return 0;
        if (len == 0) return 0;                      /* empty component */
        if (*p == '/') p++;
    }
    return 1;
}

static void join(char *out, unsigned cap, const char *root, const char *rel)
{
    char *q;
    _snprintf(out, cap - 1, "%s\\%s", root, rel);
    out[cap - 1] = '\0';
    for (q = out + strlen(root); *q; q++)
        if (*q == '/') *q = '\\';
}

/* --- the walk ------------------------------------------------------------ */

typedef struct {
    rc_sync_sink sink;
    void        *ctx;
    int          count;
    int          overflow;
} walk_ctx;

int rc_sync_is_working_name(const char *name)
{
    unsigned len = (unsigned)strlen(name);
    if (len > 11 && strcmp(name + len - 11, RC_SYNC_TMP_SUFFIX) == 0) return 1;
    if (len > 11 && strcmp(name + len - 11, RC_SYNC_OLD_SUFFIX) == 0) return 1;
    return 0;
}

static void walk(const char *root, const char *rel, int depth, walk_ctx *w)
{
    char pattern[MAX_PATH];
    char child[MAX_PATH];
    char full[MAX_PATH];
    WIN32_FIND_DATAA fd;
    HANDLE h;

    if (depth > RC_SYNC_MAX_DEPTH || w->overflow) return;

    if (*rel) join(pattern, sizeof(pattern), root, rel);
    else      _snprintf(pattern, sizeof(pattern) - 1, "%s", root);
    pattern[sizeof(pattern) - 1] = '\0';
    strncat(pattern, "\\*", sizeof(pattern) - strlen(pattern) - 1);

    h = FindFirstFileA(pattern, &fd);
    if (h == INVALID_HANDLE_VALUE) return;

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
        if (*rel) _snprintf(child, sizeof(child) - 1, "%s/%s", rel, fd.cFileName);
        else      _snprintf(child, sizeof(child) - 1, "%s", fd.cFileName);
        child[sizeof(child) - 1] = '\0';

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
            join(full, sizeof(full), root, child);
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
    } while (FindNextFileA(h, &fd));

    FindClose(h);
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

    join(full, sizeof(full), root, rel);
    _snprintf(tmp_out, tmp_cap - 1, "%s.gaston-tmp", full);
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

    join(full, sizeof(full), root, rel);

    /* Verify before replacing anything. A transfer that cannot establish
     * crc(guest) == what was sent is reported, never retried. */
    if (rc_sync_stat_file(tmp, &got) != 0 || got.type != 'f') return -1;
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
     * picks it up as a new file and pushes it to the bridge. */
    attr = GetFileAttributesA(full);
    if (attr != 0xFFFFFFFF && (attr & FILE_ATTRIBUTE_READONLY))
        SetFileAttributesA(full, attr & ~FILE_ATTRIBUTE_READONLY);

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
        rc_log("sync: refusing to commit %s -- no room for a .gaston-old", rel);
        DeleteFileA(tmp);
        return -4;
    }

    _snprintf(aside, sizeof(aside) - 1, "%s.gaston-old", full);
    aside[sizeof(aside) - 1] = '\0';
    DeleteFileA(aside);
    if (attr != 0xFFFFFFFF) MoveFileA(full, aside);

    if (!MoveFileA(tmp, full)) {
        if (attr != 0xFFFFFFFF) MoveFileA(aside, full);   /* put it back */
        DeleteFileA(tmp);
        return -3;
    }
    DeleteFileA(aside);
    if (attr != 0xFFFFFFFF && (attr & FILE_ATTRIBUTE_READONLY))
        SetFileAttributesA(full, attr);
    return 0;
}

static void recover_dir(const char *root, const char *rel, int depth)
{
    char pattern[MAX_PATH], child[MAX_PATH], aside[MAX_PATH], real[MAX_PATH];
    WIN32_FIND_DATAA fd;
    HANDLE h;

    if (depth > RC_SYNC_MAX_DEPTH) return;
    if (*rel) join(pattern, sizeof(pattern), root, rel);
    else      _snprintf(pattern, sizeof(pattern) - 1, "%s", root);
    pattern[sizeof(pattern) - 1] = '\0';
    strncat(pattern, "\\*", sizeof(pattern) - strlen(pattern) - 1);

    h = FindFirstFileA(pattern, &fd);
    if (h == INVALID_HANDLE_VALUE) return;
    do {
        unsigned len = (unsigned)strlen(fd.cFileName);
        if (fd.cFileName[0] == '.' &&
            (fd.cFileName[1] == '\0' ||
             (fd.cFileName[1] == '.' && fd.cFileName[2] == '\0')))
            continue;
        if (*rel) _snprintf(child, sizeof(child) - 1, "%s/%s", rel, fd.cFileName);
        else      _snprintf(child, sizeof(child) - 1, "%s", fd.cFileName);
        child[sizeof(child) - 1] = '\0';

        if (fd.dwFileAttributes & FILE_ATTRIBUTE_DIRECTORY) {
            recover_dir(root, child, depth + 1);
            continue;
        }
        if (len > 11 && strcmp(fd.cFileName + len - 11, ".gaston-old") == 0) {
            join(aside, sizeof(aside), root, child);
            _snprintf(real, sizeof(real) - 1, "%s", aside);
            real[sizeof(real) - 1] = '\0';
            real[strlen(real) - 11] = '\0';
            if (GetFileAttributesA(real) == 0xFFFFFFFF) {
                rc_log("sync: recovering %s -- an interrupted commit", real);
                MoveFileA(aside, real);
            } else {
                DeleteFileA(aside);
            }
        } else if (len > 11 &&
                   strcmp(fd.cFileName + len - 11, ".gaston-tmp") == 0) {
            join(aside, sizeof(aside), root, child);
            DeleteFileA(aside);       /* an abandoned transfer */
        }
    } while (FindNextFileA(h, &fd));
    FindClose(h);
}

void rc_sync_recover(const char *root)
{
    recover_dir(root, "", 0);
}
