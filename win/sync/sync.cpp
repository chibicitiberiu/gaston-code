/* Scanning and file I/O for the sync layer.
 *
 * The subtle part is not the directory walk, it is agreeing with the bridge
 * about what a file IS. Both sides compute a class and a checksum, and if they
 * ever disagree about which function to apply they compare the outputs of
 * different functions forever. So the rules here mirror bridge/files/paths.py
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
#include "fs/dir.h"
#include "fs/path.h"
#include "fs/writer.h"
#include "fs/reader.h"
#include "sync/scanner.h"
#include "sync/verify.h"
#include "sync/worknames.h"
#include "miniz.h"

/* --- classification + checksum, in ONE read pass ------------------------- */
/*
 * The class is not known until the last byte (a NUL 30KB in still makes the
 * whole file byte-exact), but the CRC depends on the class. Reading twice on a
 * 200MHz machine with a 1998 IDE disk is not affordable, so BOTH checksums are
 * accumulated as we go and the right one is chosen at the end. Two CRC
 * accumulations cost far less than a second pass over the platter.
 */

int rc_sync_stat_file(const char *path, rc_sync_stat *out)
{
    DWORD attr;

    memset(out, 0, sizeof(*out));

    /* Ask what the path IS before trying to read it.
     *
     * Without this, every open failure came back as type 'u', so a MISSING
     * file and a DIRECTORY were both indistinguishable from a file held under
     * a share-deny lock. The bridge treats `locked` as transient and retries
     * forever, so a pull of something the user had just deleted never resolved
     * and never reported anything. Caught by driving the real guest: SYNCGET
     * on a nonexistent path answered `locked`.
     *
     * type 0 means "nothing here", which is distinct from 'u' ("something is
     * here and we cannot read it") and from 'f'. */
    attr = GetFileAttributesA(path);
    if (attr == 0xFFFFFFFF) return 0;
    if (attr & FILE_ATTRIBUTE_DIRECTORY) { out->type = 'd'; return 0; }

    {
        FileReader reader;
        ContentScanner *scan;

        if (!reader.open(path)) { out->type = 'u'; return 0; }

        /* Out of memory is not "the file is fine". Unreadable is the honest
         * answer and the one the bridge already knows to skip for a pass. */
        scan = ContentScanner::create();
        if (!scan) { out->type = 'u'; return 0; }

        if (reader.readAll(*scan, RC_SYNC_CHUNK, 0) != READ_DONE)
        {
            scan->destroy();
            out->type = 'u';
            return 0;
        }

        out->size  = reader.delivered();
        out->mtime = reader.mtime();
        out->type  = 'f';
        /* The tail fixup (a CR pending at EOF), the classification and the
         * crc_raw/crc_norm/crc choice are ALL in ContentScanner::finish,
         * shared with the C facade do_get uses -- one function, so a wrong
         * choice here provably fails the guest-agreement tests rather than
         * slipping through the one path that has no test of its own. */
        scan->finish(&out->cls, &out->crc_raw, &out->crc_norm, &out->crc);
        scan->destroy();
    }
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

/* rc_sync_contained and rc_sync_is_working_name now live in libfs
 * (fs/path.h, isContainedRelative / isWorkingName). sync/compat.cpp keeps the
 * C names for the two C check programs. */

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

/* --- the walk ------------------------------------------------------------
 *
 * The walk itself is libfs (fs/dir.h). What stays here is the part that is
 * about sync rather than about directories: the content stat that produces a
 * class and two checksums, and the rule that our own working names never
 * reach a manifest.
 */

namespace {

class SyncWalkFilter : public WalkFilter
{
public:
    virtual bool skip(const char *name)
    {
        /* Never report our own working files. rc_sync_recover knows these
         * names; the scanner did not, so a half-written `.gaston-tmp` was
         * offered to the bridge as an ordinary new file and pulled. */
        return isWorkingName(name, syncWorkNames());
    }
};

class SyncWalkSink : public DirSink
{
public:
    SyncWalkSink(const char *root, rc_sync_sink sink, void *ctx)
        : m_root(root), m_sink(sink), m_ctx(ctx) {}

    virtual bool onEntry(const char *rel, const FileInfo &info)
    {
        rc_sync_stat st;
        Path full;

        memset(&st, 0, sizeof(st));

        if (info.type != 'f')
        {
            st.type = info.type;
            m_sink(m_ctx, rel, &st);
            return true;
        }

        /* A truncated join names a DIFFERENT file, so stat'ing it would
         * report someone else's size and checksum under this path. `u` says
         * what is true: it is there and we cannot address it. */
        full.join(m_root, rel);
        if (!full.ok())
        {
            st.type = 'u';
            m_sink(m_ctx, rel, &st);
            return true;
        }

        rc_sync_stat_file(full.str(), &st);

        /* type 0 means the stat found nothing where the enumeration had just
         * seen something. Emitting it as-is put a row on the wire whose first
         * field was a NUL: the line carried no fields at all, the bridge
         * failed to parse it, and the manifest count then disagreed with
         * `files=` -- which aborts THE WHOLE PASS, with a log warning as the
         * only symptom.
         *
         * `u` rather than skipping the row. Skipping is right for the race
         * that suggests itself (the file really was deleted between the
         * enumerate and the stat, so absent is true and the next pass
         * agrees). But the stat also fails PERMANENTLY for a name the
         * enumeration returns with a substitute character for a byte outside
         * the codepage -- and omitting that row tells the bridge the user
         * deleted a file they can see in Explorer. `u` says "something is
         * here and I cannot read it", which is true in both cases, keeps the
         * count honest, and costs the race one pass. */
        if (!st.type) st.type = 'u';
        m_sink(m_ctx, rel, &st);
        return true;
    }

private:
    const char   *m_root;
    rc_sync_sink  m_sink;
    void         *m_ctx;
};

} /* namespace */

int rc_sync_scan(const char *root, rc_sync_sink sink, void *ctx)
{
    SyncWalkFilter filter;
    SyncWalkSink walkSink(root, sink, ctx);
    WalkLimits limits(RC_SYNC_MAX_DEPTH, RC_SYNC_MAX_FILES);
    int count = 0;

    switch (walkTree(root, limits, &filter, walkSink, count))
    {
    case WALK_NO_ROOT:
        return -1;
    case WALK_OVERFLOW:
        /* A truncated manifest is indistinguishable from a mass deletion, so
         * the caller must abort the whole pass rather than act on what it
         * received. */
        return -2;
    default:
        return count;
    }
}

/* --- writing ------------------------------------------------------------- */

/* The write path is libfs now (fs/writer.h). These two keep the C shape the
 * sync RPC and win/checks/synccheck.c already use: begin_write hands back a
 * HANDLE the caller writes to and a temp name it holds until a later frame
 * arrives with the checksum, and commit is a separate call because SYNCPUTEND
 * is a separate frame. A FileWriter object cannot span those two RPCs, so the
 * pair calls commitInto directly -- the same code the object runs, not a
 * second copy of it. */

HANDLE rc_sync_begin_write(const char *root, const char *rel, char *tmp_out,
                           unsigned tmp_cap)
{
    Path full;
    Path tmp;
    WorkNames names = syncWorkNames();

    /* Terminate the caller's buffer FIRST, on every path including the
     * refusals below. Both used to return without touching tmp_out, so a
     * caller that logged or measured it afterwards read uninitialised stack --
     * win/checks/synccheck.c does exactly that, and it is the binary that
     * verifies this function. An empty string is the honest answer: no temp
     * file was created, so there is no name. */
    if (tmp_out && tmp_cap) tmp_out[0] = '\0';

    if (!isContainedRelative(rel)) return INVALID_HANDLE_VALUE;

    /* Refuse rather than truncate, and refuse BEFORE anything is opened.
     * Measured from the inputs rather than from the joined string, because
     * the join itself truncates silently and would hide the very thing being
     * tested. See RC_SYNC_WORK_SUFFIX for what a truncated working name does:
     * at some lengths it reports success having written nothing, and at
     * others it destroys the user's file outright.
     *
     * ERROR_FILENAME_EXCED_RANGE because do_put maps it to `too_long`.
     * Setting it explicitly also stops that classifier reading a STALE error
     * from whatever ran last, which is what a bare failing return would leave
     * it doing -- reporting `readonly` or `no_space` at random. */
    if (!fitsWithReserve(root, rel, MAX_PATH, names) ||
        !fitsWithReserve(root, rel, tmp_cap, names))
    {
        rc_log("sync: refusing %s -- no room for a .gaston-tmp beside it", rel);
        SetLastError(ERROR_FILENAME_EXCED_RANGE);
        return INVALID_HANDLE_VALUE;
    }

    full.join(root, rel);
    tmp.set(full.str());
    tmp.cat(RC_SYNC_TMP_SUFFIX);
    if (!full.ok() || !tmp.ok() || !rc_strcpy(tmp_out, tmp_cap, tmp.str()))
    {
        SetLastError(ERROR_FILENAME_EXCED_RANGE);
        return INVALID_HANDLE_VALUE;
    }

    /* Binary mode by construction -- CreateFileA does no translation. Using
     * the CRT in text mode would translate on top of the conversion the bridge
     * already did and add a CR to every line, on every pass. */
    return CreateFileA(tmp_out, GENERIC_WRITE, 0, NULL, CREATE_ALWAYS,
                       FILE_ATTRIBUTE_NORMAL, NULL);
}

int rc_sync_commit(const char *root, const char *rel, const char *tmp,
                   unsigned long want_crc, char cls)
{
    Path full;
    SyncCrcVerifier verifier(want_crc, cls);
    WorkNames names = syncWorkNames();

    /* MEASURED FROM THE INPUTS, not from the joined string -- the same rule
     * begin_write states, and for the same reason. It matters more here,
     * because this is the function that deletes and renames: a truncated
     * `full` would have moved an unrelated file aside, written the synced
     * bytes over it and deleted the displaced original, then returned success
     * so the bridge advanced its baseline over content this machine never
     * took. */
    full.join(root, rel);
    if (!full.ok())
    {
        rc_log("sync: refusing to commit %s -- the joined path does not fit",
               rel);
        DeleteFileA(tmp);
        return -4;
    }

    switch (commitInto(full.str(), tmp, &verifier, names))
    {
    case COMMIT_OK:             return 0;
    case COMMIT_UNVERIFIABLE:   return -1;
    case COMMIT_VERIFY_FAILED:  return -2;
    case COMMIT_NO_ROOM:        return -4;
    default:                    return -3;
    }
}

void rc_sync_recover(const char *root)
{
    recoverInterrupted(root, syncWorkNames());
}
