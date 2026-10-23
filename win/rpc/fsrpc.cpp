#include "rpc/fsrpc.h"

#include <new>
#include <stdlib.h>
#include <string.h>

#include "core/log.h"
#include "core/rc_cpp.h"
#include "core/str.h"
#include "fs/dir.h"
#include "fs/drives.h"
#include "fs/file.h"
#include "fs/ops.h"
#include "fs/tree.h"
#include "fs/xfer.h"
#include "miniz.h"

/* How much of a file FSGET will send when the caller names no limit.
 *
 * A ceiling and not a preference. Without one, `get_file` pointed at
 * C:\WINDOWS\WIN386.SWP is a socket held open for as long as the swap file
 * takes -- on a modem link, indefinitely -- and the person cannot see why
 * their machine has stopped answering. The caller lowers it with `max_bytes`
 * and is told `truncated=1` when it bit. exec's 32KB output cap is the
 * precedent; this is larger because a file is the point of the op rather
 * than a side effect of it. */
enum { RC_FS_MAX_GET = 4u * 1024u * 1024u };

/* The fs lane's own chunk, not the sync layer's. They happen to agree today,
 * and the point of saying so here is that nothing makes them have to: this
 * side has no manifest and no classifier whose carry depends on the size. */
enum { RC_FS_CHUNK = 8192 };

/* Working names for a path OUTSIDE any root we sweep.
 *
 * aside = false, and this is the whole reason WorkNames carries the flag.
 * Moving the old file aside means no instant has no file there, which is
 * right in the sync root because rc_sync_recover walks it at startup. Nothing
 * will ever walk C:\WINDOWS. A crash between the two moves would leave the
 * person's file under a name they have never heard of, with nothing at the
 * real path and nothing that will ever put it back. */
static WorkNames fsWorkNames()
{
    return WorkNames(".gaston-put", "", 11, false);
}

/* --- the receiving side --------------------------------------------------- */

static struct {
    char id[64];
    char path[MAX_PATH];
} g_put;

static ChunkSink *g_put_sink;
static volatile LONG g_put_aborted;

/* Heap and lazily created: a ChunkSink has a constructor, and a file-scope
 * object with a non-trivial constructor runs before rc_log_open -- which is
 * what `make check`'s static-constructor gate is for. */
static ChunkSink *putSink(void)
{
    if (!g_put_sink) {
        void *p = HeapAlloc(GetProcessHeap(), 0, sizeof(ChunkSink));
        if (!p) return NULL;
        g_put_sink = new (p) ChunkSink();
    }
    return g_put_sink;
}

void rc_fsrpc_abort(void)
{
    g_put_aborted = 1;
}

static void put_ack(rc_net *net, const char *id, const char *status,
                    unsigned long bytes, unsigned long crc)
{
    const char *keys[4];
    const char *vals[4];
    char bs[16], cs[16];

    rc_strf(bs, sizeof(bs), "%lu", bytes);
    rc_strf(cs, sizeof(cs), "%lu", crc);
    keys[0] = "id";     vals[0] = id;
    keys[1] = "status"; vals[1] = status;
    keys[2] = "bytes";  vals[2] = bs;
    keys[3] = "crc";    vals[3] = cs;
    rc_net_send(net, "FSPUTACK", keys, vals, 4, NULL, 0);
}

static void put_fail(rc_net *net, const char *id, const char *why)
{
    rc_log("fs: put %s failed: %s", id ? id : "(none)", why);
    if (g_put_sink) g_put_sink->abort(why);
    put_ack(net, id, why, 0, 0);
}

namespace {

/* Byte-exact, always. gremote's transfers are not the sync layer's: nothing
 * here classifies, folds line endings or consults .gitattributes, so there is
 * one checksum and it is over the bytes as they are. */
class RawCrc : public ByteSink
{
public:
    RawCrc() : m_crc(MZ_CRC32_INIT) {}
    virtual bool write(const void *data, unsigned len)
    {
        m_crc = mz_crc32(m_crc, (const unsigned char *)data, len);
        return true;
    }
    unsigned long value() const { return m_crc; }

private:
    unsigned long m_crc;
};

class RawCrcVerifier : public CommitVerifier
{
public:
    RawCrcVerifier(unsigned long want, unsigned long got)
        : m_want(want), m_got(got) {}
    virtual VerifyResult verify(const char *)
    {
        /* Compared against what was accumulated on the way in, not by
         * re-reading the temp: the bytes went past once and there is no
         * reason to make the platter do it again. */
        return m_want == m_got ? VERIFY_OK : VERIFY_MISMATCH;
    }

private:
    unsigned long m_want;
    unsigned long m_got;
};

RawCrc g_put_crc;

} /* namespace */

static void do_fsput(rc_net *net, rc_frame *f)
{
    const char *id = rc_frame_get(f, "id");
    const char *offs = rc_frame_get(f, "off");
    const char *path = rc_frame_get(f, "path");
    unsigned long off = offs ? (unsigned long)atol(offs) : 0;
    ChunkSink *sink = putSink();

    if (!id) {
        rc_log("FSPUT: frame carries no id -- dropped, and the caller will "
               "wait out its timeout");
        return;
    }
    if (!sink) { put_fail(net, id, "no_space"); return; }

    if (off == 0) {
        if (g_put_aborted) {
            sink->abort("the connection that owned it dropped");
            g_put_aborted = 0;
        }
        if (sink->active()) { put_fail(net, id, "busy"); return; }
        if (!path || !*path) { put_fail(net, id, "bad_path"); return; }
        if (!rc_strcpy(g_put.path, sizeof(g_put.path), path)) {
            put_fail(net, id, "too_long");
            return;
        }
        /* The id gets the same test the path above already had, which it did
         * not until 2026-10-13: a truncated id produces a PUTACK the bridge
         * cannot match, and put_fail can name the whole id because a header
         * value holds RC_MAX_VAL bytes. BACKLOG 4's convention, form 2. */
        if (!rc_strf(g_put.id, sizeof(g_put.id), "%s", id ? id : "")) {
            put_fail(net, id, "bad_id");
            return;
        }
        g_put_crc = RawCrc();

        if (!sink->beginAbsolute(g_put.path, fsWorkNames())) {
            DWORD err = GetLastError();
            const char *why = "readonly";
            if (err == ERROR_PATH_NOT_FOUND)      why = "no_parent";
            else if (err == ERROR_DISK_FULL)      why = "no_space";
            else if (err == ERROR_SHARING_VIOLATION ||
                     err == ERROR_LOCK_VIOLATION) why = "locked";
            else if (err == ERROR_FILENAME_EXCED_RANGE ||
                     err == ERROR_BUFFER_OVERFLOW) why = "too_long";
            else if (err == ERROR_INVALID_NAME)   why = "bad_path";
            rc_log("fs: cannot open a temp beside %s: err=%lu (%s)",
                   g_put.path, (unsigned long)err, why);
            put_fail(net, id, why);
            return;
        }
    }
    if (!sink->active() || strcmp(g_put.id, id) != 0) {
        put_fail(net, id, "busy");
        return;
    }
    if (f->body_len) {
        switch (sink->write(off, f->body, f->body_len)) {
        case CHUNK_OK:
            g_put_crc.write(f->body, f->body_len);
            break;
        case CHUNK_OUT_OF_ORDER:
            rc_log("fs: put %s: chunk at %lu, expected %lu", id, off,
                   (unsigned long)sink->received());
            put_fail(net, id, "out_of_order");
            break;
        default:
            put_fail(net, id, "no_space");
            break;
        }
    }
}

static void do_fsputend(rc_net *net, rc_frame *f)
{
    const char *id = rc_frame_get(f, "id");
    const char *crc = rc_frame_get(f, "crc");
    ChunkSink *sink = g_put_sink;
    unsigned long got;

    if (!id || !sink || !sink->active() || strcmp(g_put.id, id) != 0) {
        put_fail(net, id ? id : "?", "busy");
        return;
    }
    got = (unsigned long)sink->received();

    {
        RawCrcVerifier v(crc ? strtoul(crc, NULL, 10) : g_put_crc.value(),
                         g_put_crc.value());
        switch (sink->commit(&v)) {
        case COMMIT_OK:
            rc_log("fs: wrote %s (%lu bytes)", g_put.path, got);
            put_ack(net, id, "ok", got, g_put_crc.value());
            return;
        case COMMIT_VERIFY_FAILED:  put_fail(net, id, "crc_mismatch"); return;
        case COMMIT_NO_ROOM:        put_fail(net, id, "too_long");     return;
        case COMMIT_UNVERIFIABLE:
        case COMMIT_RENAME_FAILED:  put_fail(net, id, "locked");       return;
        default:                    put_fail(net, id, "no_space");     return;
        }
    }
}

/* --- the sending side ----------------------------------------------------- */

namespace {

class FsDatSender : public ChunkConsumer
{
public:
    FsDatSender(rc_net *net, const char *id) : m_net(net), m_id(id) {}

    virtual bool onChunk(const void *data, unsigned len, unsigned long off,
                         unsigned long total)
    {
        char totals[16], offs[16];
        const char *keys[3];
        const char *vals[3];

        rc_strf(totals, sizeof(totals), "%lu", total);
        rc_strf(offs, sizeof(offs), "%lu", off);
        keys[0] = "id";    vals[0] = m_id;
        keys[1] = "total"; vals[1] = totals;
        keys[2] = "off";   vals[2] = offs;
        return rc_net_send(m_net, "FSDAT", keys, vals, 3, data, len) == 0;
    }

private:
    rc_net     *m_net;
    const char *m_id;
};

} /* namespace */

static void do_fsget(rc_net *net, rc_frame *f)
{
    const char *id = rc_frame_get(f, "id");
    const char *path = rc_frame_get(f, "path");
    const char *maxs = rc_frame_get(f, "max_bytes");
    const char *keys[5];
    const char *vals[5];
    char crcs[16], trunc[4], bytes[16];
    const char *status = "ok";
    unsigned long crc = 0;
    unsigned long sent = 0;
    int truncated = 0;
    DWORD cap;

    if (!id) {
        rc_log("FSGET: frame carries no id -- dropped, and the caller will "
               "wait out its timeout");
        return;
    }

    cap = maxs ? (DWORD)strtoul(maxs, NULL, 10) : 0;
    if (!cap || cap > RC_FS_MAX_GET) cap = RC_FS_MAX_GET;

    if (!path || !*path) status = "bad_path";
    else {
        FileInfo info;
        if (!statPath(path, info))            status = "missing";
        else if (info.type == 'd')            status = "is_directory";
        else {
            ChunkSource source;
            if (!source.open(path)) status = "locked";
            else {
                RawCrc sum;
                FsDatSender sender(net, id);
                switch (source.pump(sender, &sum, RC_FS_CHUNK, cap)) {
                case PUMP_DONE:      break;
                case PUMP_TRUNCATED: truncated = 1; break;
                default:             status = "locked"; break;
                }
                crc = sum.value();
                sent = source.sent();
            }
        }
    }

    rc_strf(crcs, sizeof(crcs), "%lu", crc);
    rc_strf(bytes, sizeof(bytes), "%lu", sent);
    rc_strf(trunc, sizeof(trunc), "%d", truncated);
    keys[0] = "id";        vals[0] = id;
    keys[1] = "status";    vals[1] = status;
    keys[2] = "crc";       vals[2] = crcs;
    keys[3] = "bytes";     vals[3] = bytes;
    keys[4] = "truncated"; vals[4] = trunc;
    rc_net_send(net, "FSDATEND", keys, vals, 5, NULL, 0);
}

/* --- listing one directory ------------------------------------------------- */
/*
 * WHY A TOOL AND NOT `dir`. CLAUDE.md's bar is that a tool earns its place
 * only if `exec` does the job badly, and measured on real hardware
 * (research/measurements/batch-eol-and-dir-b-p200-20261001.txt) `dir` does:
 * twelve files cost about 1,100 bytes of volume label, serial number,
 * directory header, `.` and `..`, a free-space footer and BOTH name forms per
 * row. `dir /b` is 131 bytes but drops the sizes and dates. Neither can be
 * asked for a machine-readable answer, both come back in the console codepage
 * rather than the ANSI one, and a path with a space in it has to be quoted for
 * a shell that splits on whitespace. A live session ran `dir` six times to
 * check single files it had just compiled.
 *
 * `listDirectory` has been in libfs since step 5a with no production caller on
 * this side -- gremote has used it since step 4 (win/remote/fsops.cpp). This
 * is the client's half, and it is deliberately the SAME wire shape: rows in
 * the body, one per line, `<type> <size> <mtime> <attrs> <name>`.
 */

enum { RC_FS_LS_MAX = 2000 };

namespace {

class FsEntRows : public DirSink
{
public:
    FsEntRows(rc_net *net, const char *id, int max)
        : m_net(net), m_id(id), m_len(0), m_count(0), m_max(max),
          m_truncated(false), m_stopped(false) {}

    virtual bool onEntry(const char *rel, const FileInfo &info)
    {
        char line[MAX_PATH + 64];
        unsigned n;

        if (m_count >= m_max)
        {
            /* TRUNCATED IS A FACT, not a short listing. A caller that reads a
             * capped listing as the whole directory has been told files were
             * deleted. */
            m_truncated = true;
            return false;
        }
        rc_strf(line, sizeof(line), "%c %lu %lu %08lx %s\n",
                info.type ? info.type : 'u', (unsigned long)info.size,
                (unsigned long)info.mtime, (unsigned long)info.attrs, rel);
        n = (unsigned)strlen(line);
        if (m_len + n > sizeof(m_buf) && !flush())
            return false;
        memcpy(m_buf + m_len, line, n);
        m_len += n;
        m_count++;
        return true;
    }

    bool flush()
    {
        const char *keys[1];
        const char *vals[1];
        if (!m_len)
            return true;
        keys[0] = "id"; vals[0] = m_id;
        if (rc_net_send(m_net, "FSENT", keys, vals, 1, m_buf, m_len) != 0)
        {
            m_stopped = true;
            return false;
        }
        m_len = 0;
        return true;
    }

    int count() const { return m_count; }
    bool truncated() const { return m_truncated; }
    bool stopped() const { return m_stopped; }

private:
    rc_net     *m_net;
    const char *m_id;
    char        m_buf[RC_FS_CHUNK];
    unsigned    m_len;
    int         m_count, m_max;
    bool        m_truncated, m_stopped;
};

} /* namespace */

static void do_fsls(rc_net *net, rc_frame *f)
{
    const char *id = rc_frame_get(f, "id");
    const char *path = rc_frame_get(f, "path");
    const char *maxText = rc_frame_get(f, "max_entries");
    const char *keys[4];
    const char *vals[4];
    const char *status = "ok";
    char count[16], trunc[4];
    int max = maxText ? atoi(maxText) : RC_FS_LS_MAX;
    int n = 0;
    int truncated = 0;
    bool gone = false;

    if (!id) {
        rc_log("FSLS: frame carries no id -- dropped, and the caller will "
               "wait out its timeout");
        return;
    }
    if (max <= 0 || max > RC_FS_LS_MAX) max = RC_FS_LS_MAX;

    if (!path || !*path) status = "bad_path";
    else {
        FileInfo info;
        /* STAT FIRST, so "there is no such directory" and "it is a file" are
         * different answers rather than both arriving as an empty listing.
         * The count alone cannot tell them apart and the caller acts on it. */
        if (!statPath(path, info) || info.type == 0)
                                              status = "missing";
        else if (info.type != 'd') {
            /* A FILE answers with itself: one row, the same shape as a
             * directory's, so `list_dir` on a path is also the `stat` the
             * agent would otherwise have had to spend a tool on. The name
             * is the last component, as a listing's would be. */
            UniqueObj<FsEntRows> rows(new FsEntRows(net, id, 1));
            const char *base = strrchr(path, '\\');
            const char *slash = strrchr(path, '/');
            if (slash && (!base || slash > base)) base = slash;
            base = base ? base + 1 : path;
            if (!rows.valid()) status = "no_memory";
            else {
                rows->onEntry(base, info);
                rows->flush();
                n = rows->count();
                gone = rows->stopped();
            }
        }
        else {
            /* ON THE HEAP: FsEntRows carries an 8KB buffer and the build gate
             * refuses any stack frame reaching one page. */
            UniqueObj<FsEntRows> rows(new FsEntRows(net, id, max));
            if (!rows.valid()) status = "no_memory";
            else if (!listDirectory(path, *rows.get()))
            {
                /* Not an empty directory: one that was never read. */
                status = "locked";
            }
            else
            {
                rows->flush();
                n = rows->count();
                truncated = rows->truncated() ? 1 : 0;
                gone = rows->stopped();
            }
        }
    }

    if (gone)
        return;                 /* the link is gone; END would not arrive */
    rc_strf(count, sizeof(count), "%d", n);
    rc_strf(trunc, sizeof(trunc), "%d", truncated);
    keys[0] = "id";        vals[0] = id;
    keys[1] = "status";    vals[1] = status;
    keys[2] = "count";     vals[2] = count;
    keys[3] = "truncated"; vals[3] = trunc;
    rc_net_send(net, "FSENTEND", keys, vals, 4, NULL, 0);
}

/* --- drives --------------------------------------------------------------- */

namespace {

class DriveRows : public DriveSink
{
public:
    DriveRows(rc_net *net, const char *id) : m_net(net), m_id(id), m_n(0) {}

    virtual bool onDrive(const DriveInfo &info)
    {
        const char *keys[8];
        const char *vals[8];
        char letter[4], type[8], total[24], freeb[24], serial[16];

        rc_strf(letter, sizeof(letter), "%c", info.letter);
        rc_strf(type, sizeof(type), "%u", info.type);
        rc_strf(serial, sizeof(serial), "%lu", (unsigned long)info.serial);
        /* 64-bit, printed as decimal rather than folded into a DWORD of KB:
         * the caller can divide, and a number that saturates on this side is
         * a number nobody downstream can recover. */
        rc_strf(total, sizeof(total), "%lu",
                (unsigned long)(info.totalBytes.QuadPart / 1024ULL));
        rc_strf(freeb, sizeof(freeb), "%lu",
                (unsigned long)(info.freeBytes.QuadPart / 1024ULL));

        keys[0] = "id";       vals[0] = m_id;
        keys[1] = "letter";   vals[1] = letter;
        keys[2] = "type";     vals[2] = type;
        keys[3] = "label";    vals[3] = info.label;
        keys[4] = "serial";   vals[4] = serial;
        keys[5] = "free_kb";  vals[5] = freeb;
        keys[6] = "total_kb"; vals[6] = total;
        /* Sent separately from the free figure, because they are different
         * questions: a mounted CD has zero bytes free and is not an empty
         * drive. Without this the caller has to guess, and it guesses wrong
         * for every disc. A third spelling, "unknown", is what a removable
         * or a CD-ROM gets when FSDRIVES ran with probe=0 (BACKLOG 20) --
         * nobody looked, which is not the same fact as "0". */
        keys[7] = "media";
        switch (info.media)
        {
        case MEDIA_PRESENT: vals[7] = "1";       break;
        case MEDIA_ABSENT:  vals[7] = "0";       break;
        default:            vals[7] = "unknown"; break;
        }
        if (rc_net_send(m_net, "FSDRIVE", keys, vals, 8, NULL, 0) != 0)
            return false;
        m_n++;
        return true;
    }

    int count() const { return m_n; }

private:
    rc_net     *m_net;
    const char *m_id;
    int         m_n;
};

} /* namespace */

static void do_fsdrives(rc_net *net, rc_frame *f)
{
    const char *id = rc_frame_get(f, "id");
    const char *probeText = rc_frame_get(f, "probe");
    bool probe = probeText && probeText[0] == '1';
    const char *keys[2];
    const char *vals[2];
    char n[16];

    if (!id) {
        rc_log("FSDRIVES: frame carries no id -- dropped");
        return;
    }
    {
        DriveRows rows(net, id);
        /* probe=0 by default (BACKLOG 20): a removable or a CD-ROM is not
         * asked for its volume label or free space unless the caller sets
         * probe=1. The letter and the type are unconditional either way. */
        enumerateDrives(rows, probe);
        rc_strf(n, sizeof(n), "%d", rows.count());
    }
    keys[0] = "id";    vals[0] = id;
    keys[1] = "count"; vals[1] = n;
    rc_net_send(net, "FSDRIVEEND", keys, vals, 2, NULL, 0);
}

/* --- the mutations -------------------------------------------------------- */

/* MKDIR (with parents, an existing directory is fine) and RMDIR (empty) are
 * fs/ops.h; RM, MV and COPY are fs/tree.h -- a file or a whole folder, the
 * way an agent expects any OS to do them, with nothing overwritten unless
 * `overwrite=1`, a move that never loses its source, and a delete that goes
 * to the Recycle Bin unless `permanent=1`. STATS is what a delete or copy is
 * about to touch, for the permission bar.
 *
 * One FSOPEND each, with libfs's reason and what was done. Bounded by
 * fs/tree.h's caps, so a copy cannot hold this worker past the bridge's
 * budget. */
static void do_fsop(rc_net *net, rc_frame *f)
{
    const char *id = rc_frame_get(f, "id");
    const char *path = rc_frame_get(f, "path");
    const char *to = rc_frame_get(f, "to");
    const char *ow = rc_frame_get(f, "overwrite");
    const char *perm = rc_frame_get(f, "permanent");
    bool overwrite = ow && ow[0] == '1';
    const char *why = 0;
    const char *how = 0;
    char detail[MAX_PATH + 8];
    char files[16], dirs[16], bytes[24];
    const char *keys[8];
    const char *vals[8];
    TreeDone done;
    TreeStats st;
    int n = 0;
    bool stats = false;

    detail[0] = '\0';
    if (!id) {
        rc_log("%s: frame carries no id -- dropped", f->type);
        return;
    }
    if (strcmp(f->type, "FSMKDIR") == 0)
        why = fsMakeDirectory(path, true);
    else if (strcmp(f->type, "FSRMDIR") == 0)
        why = fsRemoveDirectory(path);
    else if (strcmp(f->type, "FSRM") == 0) {
        why = fsDeleteTree(path, perm && perm[0] == '1', done, detail,
                           sizeof(detail));
        how = "deleted";
        if (done.recycled) how = "recycled";
    }
    else if (strcmp(f->type, "FSCOPY") == 0) {
        why = fsCopyTree(path, to, overwrite, done, detail, sizeof(detail));
        how = "copied";
    }
    else if (strcmp(f->type, "FSSTATS") == 0) {
        why = fsTreeStats(path, st);
        stats = true;
    }
    else {
        why = fsMoveTree(path, to, overwrite, done, detail, sizeof(detail));
        how = "copied";
        if (done.renamed) how = "renamed";
    }

    if (stats) {
        done.files = st.files;
        done.dirs = st.dirs;
        done.bytes = st.bytes;
        how = "file";
        if (st.isDir) how = "dir";
    }
    rc_strf(files, sizeof(files), "%lu", (unsigned long)done.files);
    rc_strf(dirs, sizeof(dirs), "%lu", (unsigned long)done.dirs);
    rc_strf(bytes, sizeof(bytes), "%lu",
            (unsigned long)(done.bytes > 0xFFFFFFFFUL ? 0xFFFFFFFFUL
                                                       : done.bytes));
    keys[n] = "id";     vals[n++] = id;
    keys[n] = "status"; vals[n++] = why ? why : "ok";
    keys[n] = "files";  vals[n++] = files;
    keys[n] = "dirs";   vals[n++] = dirs;
    keys[n] = "bytes";  vals[n++] = bytes;
    if (how && (!why || stats)) { keys[n] = "how"; vals[n++] = how; }
    if (detail[0])      { keys[n] = "detail"; vals[n++] = detail; }

    /* STATS: the first names, one per line, so the bar can show what a
     * folder actually holds. */
    char sample[RC_TREE_SAMPLE * (MAX_PATH + 1)];
    unsigned len = 0;
    if (stats && !why)
        for (int i = 0; i < st.sampleN; i++) {
            unsigned l = (unsigned)strlen(st.sample[i]);
            memcpy(sample + len, st.sample[i], l);
            len += l;
            sample[len++] = '\n';
        }
    rc_net_send(net, "FSOPEND", keys, vals, n, len ? sample : NULL, len);
}

int rc_fsrpc_handle(rc_net *net, rc_frame *f)
{
    if (strcmp(f->type, "FSGET") == 0)          do_fsget(net, f);
    else if (strcmp(f->type, "FSPUT") == 0)     do_fsput(net, f);
    else if (strcmp(f->type, "FSPUTEND") == 0)  do_fsputend(net, f);
    else if (strcmp(f->type, "FSLS") == 0)      do_fsls(net, f);
    else if (strcmp(f->type, "FSDRIVES") == 0)  do_fsdrives(net, f);
    else if (strcmp(f->type, "FSMKDIR") == 0 || strcmp(f->type, "FSMV") == 0
             || strcmp(f->type, "FSRM") == 0
             || strcmp(f->type, "FSRMDIR") == 0
             || strcmp(f->type, "FSCOPY") == 0
             || strcmp(f->type, "FSSTATS") == 0) do_fsop(net, f);
    else return 0;
    return 1;
}
