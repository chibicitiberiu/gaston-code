#include "remote/fsops.h"

#include <stdlib.h>
#include <string.h>
#include "core/log.h"
#include "core/str.h"
#include "fs/dir.h"
#include "fs/drives.h"
#include "fs/file.h"
#include "fs/path.h"
#include "fs/writer.h"
#include "fs/xfer.h"
#include "miniz.h"

/* A DATA body's worth of records. Well under the 64KB frame cap, and small
 * enough that a long listing starts arriving while it is still being made. */
enum { FS_CHUNK = 8192 };

/* Entries in one LS before it stops and says so. A directory with more than
 * this in it is a directory nobody wants printed, and an unbounded listing
 * on a 32MB box is the bounded-means-fixed trap in reverse. */
enum { LS_DEFAULT_MAX = 2000 };

/* Bytes per DATA frame on the way out. Eight kilobytes is what the client's
 * transfers settled on: well inside the 64KB frame cap, and small enough
 * that a slow link sees progress rather than stalls. */
enum { GET_CHUNK = 8192 };

/* What a GET hands back when the caller names no ceiling of its own.
 *
 * THE GUEST ENFORCES THIS, not the caller, and that is the whole point.
 * A limit applied on the receiving side still has this machine read a swap
 * file off a 1998 platter and push it down a socket for as long as it takes.
 * Four megabytes is large enough for any binary anyone ships to this box and
 * small enough that pointing GET at WIN386.SWP is a truncated answer in a
 * few seconds rather than a wedged lane. */
enum { GET_DEFAULT_MAX = 4u * 1024u * 1024u };

FsOps::FsOps(Connection &conn)
    : m_conn(&conn), m_gate(0), m_putCrc(MZ_CRC32_INIT), m_abortWanted(0)
{
    m_putId[0] = '\0';
}

bool FsOps::cancel(const char *id)
{
    /* A GET needs nothing done to it: its next DATA goes through the
     * Responder, the gate refuses it, and the pump stops at the chunk
     * boundary -- which is PUMP_CONSUMER_STOPPED doing the job BACKLOG 6
     * named it for.
     *
     * A PUT does. It is not one frame, so there is no handler in flight to
     * refuse anything: the sink is sitting open with a temp beside somebody
     * else's file, waiting for chunks that are not coming. */
    if (!m_sink.active() || !m_putId[0] || strcmp(m_putId, id) != 0)
        return false;
    InterlockedExchange((LONG *)&m_abortWanted, 1);
    return true;
}

void FsOps::idle()
{
    if (!m_abortWanted)
        return;
    InterlockedExchange((LONG *)&m_abortWanted, 0);
    if (m_sink.active())
    {
        rc_log("fs: cancelled the transfer to %s", m_sink.targetPath());
        /* The temp goes and the file at the target, if there was one, is
         * untouched -- nothing was ever moved aside. */
        abortPut("cancelled");
    }
}

/* NO MOVE-ASIDE, and this is the sharpest decision in the file.
 *
 * The sync layer renames the old file out of the way before renaming the new
 * one in, so no instant has no file at the path. That is only safe because
 * rc_sync_recover sweeps the sync root at startup and finishes anything
 * interrupted. Nothing will ever sweep C:\WINDOWS. A failure between those
 * two renames out here would leave somebody's SYSTEM.INI as
 * SYSTEM.INI.gaston-old with nothing at the real path, in a directory no
 * recovery pass will ever visit -- so: temp beside the target, verify, and
 * only then replace. Either the original is still there or nothing is.
 *
 * 11 is the reserve: the longest working name this adds beside a target. */
static WorkNames putNames()
{
    return WorkNames(".gaston-put", "", 11, false);
}

/* Why a stat did not answer, from the error the platform left behind.
 *
 * statPath returns false when there is nothing at the path -- but "nothing
 * at the path" and "I was not allowed to look" arrive the same way through
 * GetFileAttributesA, and the caller needs them apart: told `locked` about a
 * file that simply is not there, it retries until its budget runs out.
 * Called immediately after the failure, before anything else can overwrite
 * GetLastError. */
static const char *whyNoStat()
{
    switch (GetLastError())
    {
    case ERROR_FILE_NOT_FOUND:
    case ERROR_PATH_NOT_FOUND:
    case ERROR_BAD_NETPATH:
    case ERROR_NOT_READY:           /* an empty floppy bay is not a lock */
        return "missing";
    case ERROR_FILENAME_EXCED_RANGE:
    case ERROR_BUFFER_OVERFLOW:
        return "too_long";
    case ERROR_INVALID_NAME:
    case ERROR_INVALID_DRIVE:
        return "bad_path";
    default:
        return "locked";
    }
}

/* Lines of "type size mtime attrs name", flushed every FS_CHUNK. The name
 * is LAST because it is the only field that can contain a space; a Windows
 * path cannot contain a newline, which is what makes a line format safe
 * here at all. */
class LsChunks : public DirSink
{
public:
    LsChunks(Responder &r, int max)
        : m_r(&r), m_len(0), m_count(0), m_max(max), m_truncated(false),
          m_stopped(false) {}

    bool onEntry(const char *rel, const FileInfo &info)
    {
        char line[MAX_PATH + 64];
        unsigned n;

        if (m_count >= m_max)
        {
            m_truncated = true;
            return false;
        }
        rc_strf(line, sizeof line, "%c %lu %lu %08lx %s\n",
                info.type ? info.type : 'u', (unsigned long)info.size,
                (unsigned long)info.mtime, (unsigned long)info.attrs, rel);
        n = (unsigned)strlen(line);
        if (m_len + n > sizeof m_buf && !flush())
            return false;
        memcpy(m_buf + m_len, line, n);
        m_len += n;
        m_count++;
        return true;
    }

    bool flush()
    {
        if (!m_len)
            return true;
        if (!m_r->data(m_buf, m_len))
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
    Responder *m_r;
    char m_buf[FS_CHUNK];
    unsigned m_len;
    int m_count, m_max;
    bool m_truncated, m_stopped;
};

void FsOps::ls(Responder &r, rc_frame *f)
{
    const char *path = rc_frame_get(f, "path");
    const char *maxText = rc_frame_get(f, "max_entries");
    FileInfo info;
    char count[16], trunc[4];
    const char *keys[2];
    const char *vals[2];
    int max = maxText ? atoi(maxText) : LS_DEFAULT_MAX;

    if (!path || !*path)
    {
        r.err("bad_path");
        return;
    }
    if (max <= 0 || max > LS_DEFAULT_MAX)
        max = LS_DEFAULT_MAX;
    /* Stat first, so "there is no such directory" and "it is a file" are
     * different answers rather than both being an empty listing. */
    if (!statPath(path, info) || info.type == 0)
    {
        r.err(whyNoStat());
        return;
    }
    if (info.type != 'd')
    {
        r.err("not_a_directory");
        return;
    }

    /* On the heap: LsChunks carries an 8KB buffer and the frame gate refuses
     * any stack frame reaching one page. */
    UniqueObj<LsChunks> rows(new LsChunks(r, max));
    if (!rows.valid())
    {
        r.err("no_memory");
        return;
    }
    if (!listDirectory(path, *rows.get()))
    {
        /* Not an empty directory: a directory that was never read. The
         * `not_a_directory` answer above exists for the same reason -- a
         * count of zero is indistinguishable from the truth, and the caller
         * acts on it. Classified here, immediately, before flush() or
         * anything else can overwrite GetLastError. */
        r.err(whyNoStat());
        return;
    }
    rows->flush();
    if (rows->stopped())
        return;                 /* the link is gone; END would not arrive */
    rc_strf(count, sizeof count, "%d", rows->count());
    rc_strcpy(trunc, sizeof trunc, rows->truncated() ? "1" : "0");
    keys[0] = "count";     vals[0] = count;
    keys[1] = "truncated"; vals[1] = trunc;
    r.end(keys, vals, 2);
}

void FsOps::stat(Responder &r, rc_frame *f)
{
    const char *path = rc_frame_get(f, "path");
    FileInfo info;
    char type[4], size[16], mtime[16], attrs[16];
    const char *keys[4];
    const char *vals[4];

    if (!path || !*path)
    {
        r.err("bad_path");
        return;
    }
    if (!statPath(path, info) || info.type == 0)
    {
        r.err(whyNoStat());
        return;
    }
    rc_strf(type, sizeof type, "%c", info.type);
    rc_strf(size, sizeof size, "%lu", (unsigned long)info.size);
    rc_strf(mtime, sizeof mtime, "%lu", (unsigned long)info.mtime);
    rc_strf(attrs, sizeof attrs, "%08lx", (unsigned long)info.attrs);
    keys[0] = "type";  vals[0] = type;
    keys[1] = "size";  vals[1] = size;
    keys[2] = "mtime"; vals[2] = mtime;
    keys[3] = "attrs"; vals[3] = attrs;
    r.ok(keys, vals, 4);
}

/* "1" (probed, media present), "0" (probed, nothing in the drive) or "?"
 * (never asked -- a removable or a CD-ROM `DRIVES` did not probe). The
 * third spelling is the point: collapsing it into "0" would report a
 * floppy that was never looked at as one known to be empty. */
static const char *mediaToken(DriveMedia m)
{
    switch (m)
    {
    case MEDIA_PRESENT: return "1";
    case MEDIA_ABSENT:  return "0";
    default:            return "?";
    }
}

/* One line per drive: "letter type media serial total_kb free_kb label".
 * The label is last, for the same reason a filename is. */
class DriveRows : public DriveSink
{
public:
    explicit DriveRows(Responder &r)
        : m_r(&r), m_len(0), m_count(0), m_stopped(false) {}

    bool onDrive(const DriveInfo &d)
    {
        char line[160];
        unsigned n;

        rc_strf(line, sizeof line, "%c %u %s %08lx %lu %lu %s\n",
                d.letter, d.type, mediaToken(d.media),
                (unsigned long)d.serial,
                (unsigned long)(d.totalBytes.QuadPart / 1024),
                (unsigned long)(d.freeBytes.QuadPart / 1024), d.label);
        n = (unsigned)strlen(line);
        if (m_len + n > sizeof m_buf)
            return flushAnd(line, n);
        memcpy(m_buf + m_len, line, n);
        m_len += n;
        m_count++;
        return true;
    }

    bool flush()
    {
        if (!m_len)
            return true;
        if (!m_r->data(m_buf, m_len))
        {
            m_stopped = true;
            return false;
        }
        m_len = 0;
        return true;
    }

    int count() const { return m_count; }
    bool stopped() const { return m_stopped; }

private:
    bool flushAnd(const char *line, unsigned n)
    {
        if (!flush())
            return false;
        memcpy(m_buf, line, n);
        m_len = n;
        m_count++;
        return true;
    }

    Responder *m_r;
    char m_buf[FS_CHUNK];
    unsigned m_len;
    int m_count;
    bool m_stopped;
};

void FsOps::drives(Responder &r, rc_frame *f)
{
    const char *probeText = rc_frame_get(f, "probe");
    bool probe = probeText && probeText[0] == '1';
    char count[16];
    const char *keys[1];
    const char *vals[1];

    UniqueObj<DriveRows> rows(new DriveRows(r));
    if (!rows.valid())
    {
        r.err("no_memory");
        return;
    }
    /* enumerateDrives sets SEM_FAILCRITICALERRORS for the length of the
     * walk. Without it, GetVolumeInformationA on an empty A: trips the 9x
     * critical-error handler -- a modal dialog on somebody's screen, raised
     * by a read-only call, blocking this thread until it is dismissed.
     *
     * probe=0 (the default -- BACKLOG 20) is also why that dialog is the
     * rarer case now: a removable or a CD-ROM is not asked for its volume
     * label or its free space at all unless the caller sets probe=1, so
     * there is usually nothing left to trip the handler on A:. SEM_FAIL-
     * CRITICALERRORS stays in enumerateDrives regardless, because `probe=1`
     * still has to not pop a dialog. */
    enumerateDrives(*rows.get(), probe);
    rows->flush();
    if (rows->stopped())
        return;
    rc_strf(count, sizeof count, "%d", rows->count());
    keys[0] = "count";
    vals[0] = count;
    r.end(keys, vals, 1);
}

/* The CRC of what actually went out, taken from the same pass that sent it.
 * Byte-exact and nothing else: this is not the sync layer, so nothing here
 * classifies text, folds line endings or consults .gitattributes. A GET and
 * the file on disk are the same bytes or the transfer was wrong. */
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

/* One DATA frame per chunk, carrying where it goes and how much there is in
 * total -- so a caller can show progress without having asked STAT first,
 * and can tell a short transfer from a finished one. */
class GetSender : public ChunkConsumer
{
public:
    explicit GetSender(Responder &r) : m_r(&r), m_stopped(false) {}

    virtual bool onChunk(const void *data, unsigned len, unsigned long off,
                         unsigned long total)
    {
        char offText[16], totalText[16];
        const char *keys[2];
        const char *vals[2];

        rc_strf(offText, sizeof offText, "%lu", off);
        rc_strf(totalText, sizeof totalText, "%lu", total);
        keys[0] = "off";   vals[0] = offText;
        keys[1] = "total"; vals[1] = totalText;
        if (m_r->data(data, len, keys, vals, 2))
            return true;
        /* The caller is gone, or so far behind that a bulk frame could not
         * find room inside its budget. Stop reading the platter for
         * somebody who is not there. */
        m_stopped = true;
        return false;
    }

    bool stopped() const { return m_stopped; }

private:
    Responder *m_r;
    bool m_stopped;
};

void FsOps::get(Responder &r, rc_frame *f)
{
    const char *path = rc_frame_get(f, "path");
    const char *maxText = rc_frame_get(f, "max_bytes");
    FileInfo info;
    DWORD maxBytes = GET_DEFAULT_MAX;
    char crcText[16], bytesText[16], trunc[4];
    const char *keys[3];
    const char *vals[3];

    if (!path || !*path)
    {
        r.err("bad_path");
        return;
    }
    if (maxText && *maxText)
    {
        DWORD asked = (DWORD)strtoul(maxText, NULL, 10);
        /* The caller may ask for less than the ceiling, never more. */
        if (asked && asked < maxBytes)
            maxBytes = asked;
    }
    if (!statPath(path, info) || info.type == 0)
    {
        r.err(whyNoStat());
        return;
    }
    if (info.type == 'd')
    {
        r.err("is_directory");
        return;
    }

    ChunkSource source;
    if (!source.open(path))
    {
        /* Present a moment ago and not readable now: somebody else has it
         * open under a share-deny lock, which is a different thing from it
         * being absent. */
        r.err("locked");
        return;
    }

    GetSender sender(r);
    RawCrc crc;
    PumpResult result = source.pump(sender, &crc, GET_CHUNK, maxBytes);
    if (sender.stopped())
    {
        rc_log("fs: the caller went away during GET %s", path);
        return;             /* END would not reach them either */
    }
    if (result == PUMP_OPEN_FAILED || result == PUMP_READ_FAILED)
    {
        r.err("locked");
        return;
    }

    rc_strf(crcText, sizeof crcText, "%08lx", crc.value());
    rc_strf(bytesText, sizeof bytesText, "%lu", source.sent());
    rc_strcpy(trunc, sizeof trunc, result == PUMP_TRUNCATED ? "1" : "0");
    keys[0] = "crc";       vals[0] = crcText;
    keys[1] = "bytes";     vals[1] = bytesText;
    keys[2] = "truncated"; vals[2] = trunc;
    r.end(keys, vals, 3);
}

/* What a CRC says about a temp we already checksummed on the way in.
 * Compared rather than re-read: the bytes went past once and there is no
 * reason to make the platter do it again. */
class RawCrcVerifier : public CommitVerifier
{
public:
    RawCrcVerifier(unsigned long want, unsigned long got)
        : m_want(want), m_got(got) {}

    virtual VerifyResult verify(const char *)
    {
        return m_want == m_got ? VERIFY_OK : VERIFY_MISMATCH;
    }

private:
    unsigned long m_want;
    unsigned long m_got;
};

/* Why a temp could not be opened beside its target, from the error the
 * platform left. Every token here is one the client's fs lane already
 * produces; the spellings are deliberately the same across the two
 * programs, even though gremote has its own namespace and could differ. */
static const char *whyNoTemp()
{
    switch (GetLastError())
    {
    case ERROR_PATH_NOT_FOUND:      return "no_parent";
    case ERROR_DISK_FULL:           return "no_space";
    case ERROR_SHARING_VIOLATION:
    case ERROR_LOCK_VIOLATION:      return "locked";
    case ERROR_FILENAME_EXCED_RANGE:
    case ERROR_BUFFER_OVERFLOW:     return "too_long";
    case ERROR_INVALID_NAME:        return "bad_path";
    default:                        return "readonly";
    }
}

void FsOps::abortPut(const char *why)
{
    if (m_sink.active())
        m_sink.abort(why);
    m_putId[0] = '\0';
    m_putCrc = MZ_CRC32_INIT;
}

void FsOps::put(Responder &r, rc_frame *f)
{
    const char *path = rc_frame_get(f, "path");
    const char *off = rc_frame_get(f, "off");
    const char *id = r.id();

    if (!path || !*path)
    {
        r.err("bad_path");
        return;
    }
    if (m_sink.active())
    {
        /* Someone else's transfer is open. Refusing is right: taking it
         * over would lose their bytes, and abandoning theirs silently would
         * be worse. */
        r.errWith("busy", 0, 0, 0);
        return;
    }
    /* A PUT opens at nothing but the beginning. */
    if (off && strtoul(off, NULL, 10) != 0)
    {
        r.err("out_of_order");
        return;
    }
    if (!m_sink.beginAbsolute(path, putNames()))
    {
        r.err(whyNoTemp());
        return;
    }
    rc_strcpy(m_putId, sizeof m_putId, id ? id : "");
    m_putCrc = MZ_CRC32_INIT;
    putChunk(r, f);
}

void FsOps::putChunk(Responder &r, rc_frame *f)
{
    const char *off = rc_frame_get(f, "off");
    const char *id = r.id();
    unsigned long at;

    if (!m_sink.active())
    {
        r.err("no_transfer");
        return;
    }
    if (!id || strcmp(id, m_putId) != 0)
    {
        /* Not this transfer's bytes. Answering rather than writing them is
         * the whole reason the id is checked at all. */
        r.err("no_transfer");
        return;
    }
    at = off ? strtoul(off, NULL, 10) : 0;
    switch (m_sink.write(at, f->body, f->body_len))
    {
    case CHUNK_OK:
        m_putCrc = mz_crc32(m_putCrc, (const unsigned char *)f->body,
                            f->body_len);
        return;                 /* no reply per chunk; END answers */
    case CHUNK_OUT_OF_ORDER:
        /* A gap would be a hole in somebody's file. The transfer ends here
         * rather than committing something that is not what was sent. */
        abortPut("out of order");
        r.err("out_of_order");
        return;
    default:
        abortPut("write failed");
        r.err("no_space");
        return;
    }
}

void FsOps::putEnd(Responder &r, rc_frame *f)
{
    const char *crcText = rc_frame_get(f, "crc");
    const char *id = r.id();
    char bytes[16], crcOut[16];
    const char *keys[2];
    const char *vals[2];
    const char *code = "no_space";

    if (!m_sink.active() || !id || strcmp(id, m_putId) != 0)
    {
        r.err("no_transfer");
        return;
    }
    rc_strf(bytes, sizeof bytes, "%lu", m_sink.received());
    rc_strf(crcOut, sizeof crcOut, "%08lx", m_putCrc);

    unsigned long want = crcText ? strtoul(crcText, NULL, 16) : m_putCrc;
    RawCrcVerifier verifier(want, m_putCrc);
    CommitStatus status = m_sink.commit(&verifier);
    m_putId[0] = '\0';
    m_putCrc = MZ_CRC32_INIT;

    if (status == COMMIT_OK)
    {
        keys[0] = "bytes"; vals[0] = bytes;
        keys[1] = "crc";   vals[1] = crcOut;
        r.ok(keys, vals, 2);
        return;
    }
    switch (status)
    {
    case COMMIT_VERIFY_FAILED: code = "crc_mismatch"; break;
    case COMMIT_NO_ROOM:       code = "too_long";     break;
    case COMMIT_UNVERIFIABLE:
    case COMMIT_RENAME_FAILED: code = "locked";       break;
    default:                   code = "no_space";     break;
    }
    rc_log("fs: PUT commit -> %s", code);
    r.err(code);
}

void FsOps::rm(Responder &r, rc_frame *f)
{
    const char *path = rc_frame_get(f, "path");
    FileInfo info;

    if (!path || !*path)
    {
        r.err("bad_path");
        return;
    }
    if (!statPath(path, info) || info.type == 0)
    {
        r.err(whyNoStat());
        return;
    }
    if (info.type == 'd')
    {
        /* Removing a directory is not the same operation and is not one of
         * the eight. Saying so beats deleting the wrong thing. */
        r.err("is_directory");
        return;
    }
    /* libfs clears FILE_ATTRIBUTE_READONLY first: a read-only file the
     * caller asked to delete is a file the caller asked to delete. */
    if (!removeFile(path))
    {
        r.err(GetLastError() == ERROR_ACCESS_DENIED ? "readonly" : "locked");
        return;
    }
    r.ok();
}

void FsOps::mkdir(Responder &r, rc_frame *f)
{
    const char *path = rc_frame_get(f, "path");
    FileInfo info;

    if (!path || !*path)
    {
        r.err("bad_path");
        return;
    }
    /* makeDirectory is happy to find one already there -- it creates
     * parents, so that is the normal case for every level but the last.
     * REMOTE.md wants the last level distinguished, so it is checked here
     * rather than by weakening libfs. */
    if (statPath(path, info) && info.type != 0)
    {
        r.err(info.type == 'd' ? "exists" : "is_file");
        return;
    }
    if (!makeDirectory(path))
    {
        switch (GetLastError())
        {
        case ERROR_PATH_NOT_FOUND:      r.err("no_parent"); return;
        case ERROR_FILENAME_EXCED_RANGE:
        case ERROR_BUFFER_OVERFLOW:     r.err("too_long");  return;
        case ERROR_INVALID_NAME:
        case ERROR_INVALID_DRIVE:       r.err("bad_path");  return;
        default:                        r.err("locked");    return;
        }
    }
    r.ok();
}

/* True for "C:", "C:\", "C:\\" and the like -- a drive letter, a colon, and
 * nothing after it but separators. Checked by inspection, the same way
 * isContainedRelative is: refusing a drive root outright costs nothing and
 * there is no caller for whom deleting one was ever the intent. */
static bool isDriveRoot(const char *path)
{
    size_t i;

    if (path[1] != ':')
        return false;
    for (i = 2; path[i]; i++)
    {
        if (path[i] != '\\' && path[i] != '/')
            return false;
    }
    return true;
}

/* Why RemoveDirectoryA refused, from the error the platform left behind.
 *
 * Win98 hands back a DIFFERENT code for each of the three real refusals --
 * non-empty, read-only, and "this is some process's current directory" --
 * and collapsing them into one `locked` would tell the caller nothing about
 * which is true, on a machine where "try again" is the right answer to only
 * one of the three. */
static const char *whyNoRmdir()
{
    switch (GetLastError())
    {
    case ERROR_DIR_NOT_EMPTY:       return "not_empty";
    case ERROR_ACCESS_DENIED:       return "readonly";
    case ERROR_CURRENT_DIRECTORY:   return "is_cwd";
    case ERROR_FILENAME_EXCED_RANGE:
    case ERROR_BUFFER_OVERFLOW:     return "too_long";
    case ERROR_INVALID_NAME:
    case ERROR_INVALID_DRIVE:       return "bad_path";
    default:                        return "locked";
    }
}

/* The mirror of mkdir: that one creates a directory and is happy to find one
 * already there; this one removes a directory and only an EMPTY one --
 * RemoveDirectoryA already refuses anything else, so "not recursive" is not
 * a restriction added here, it is what the platform call does. A caller
 * that wants a tree gone enumerates with LS and removes files with RM, the
 * same way it would have to without this op; the dangerous loop belongs on
 * the Linux side, where it can be read (gremote/client.py's rmtree). */
void FsOps::rmdir(Responder &r, rc_frame *f)
{
    const char *path = rc_frame_get(f, "path");
    FileInfo info;

    if (!path || !*path)
    {
        r.err("bad_path");
        return;
    }
    if (isDriveRoot(path))
    {
        r.err("drive_root");
        return;
    }
    if (!statPath(path, info) || info.type == 0)
    {
        r.err(whyNoStat());
        return;
    }
    if (info.type != 'd')
    {
        /* Same spelling MKDIR uses for the reverse case (something there
         * and it is not a directory), whether it is a file or 'u'. */
        r.err("is_file");
        return;
    }
    if (!RemoveDirectoryA(path))
    {
        r.err(whyNoRmdir());
        return;
    }
    r.ok();
}

void FsOps::mv(Responder &r, rc_frame *f)
{
    const char *from = rc_frame_get(f, "from");
    const char *to = rc_frame_get(f, "to");
    FileInfo info;

    if (!from || !*from || !to || !*to)
    {
        r.err("bad_path");
        return;
    }
    if (!statPath(from, info) || info.type == 0)
    {
        r.err(whyNoStat());
        return;
    }
    /* MoveFileA refuses an existing target, and that refusal is the useful
     * one: a move that silently replaced something would be a delete
     * nobody asked for. Checked first so the answer says `exists` rather
     * than a bare failure. */
    if (statPath(to, info) && info.type != 0)
    {
        r.err("exists");
        return;
    }
    /* movePath is MoveFileA and never MoveFileExA, which is NT-only and on
     * the import deny list -- so a move across volumes fails here rather
     * than copying. */
    if (!movePath(from, to))
    {
        switch (GetLastError())
        {
        case ERROR_FILENAME_EXCED_RANGE: r.err("too_long"); return;
        case ERROR_INVALID_NAME:         r.err("bad_path"); return;
        default:                         r.err("locked");   return;
        }
    }
    r.ok();
}

void FsOps::handle(rc_frame *f)
{
    Responder r(*m_conn, rc_frame_get(f, "id"), m_gate);
    if (strcmp(f->type, "LS") == 0)
        ls(r, f);
    else if (strcmp(f->type, "STAT") == 0)
        stat(r, f);
    else if (strcmp(f->type, "DRIVES") == 0)
        drives(r, f);
    else if (strcmp(f->type, "GET") == 0)
        get(r, f);
    else if (strcmp(f->type, "PUT") == 0)
        put(r, f);
    else if (strcmp(f->type, "DATA") == 0)
        putChunk(r, f);
    else if (strcmp(f->type, "END") == 0)
        putEnd(r, f);
    else if (strcmp(f->type, "RM") == 0)
        rm(r, f);
    else if (strcmp(f->type, "MKDIR") == 0)
        mkdir(r, f);
    else if (strcmp(f->type, "RMDIR") == 0)
        rmdir(r, f);
    else if (strcmp(f->type, "MV") == 0)
        mv(r, f);
    else
        r.err("not_implemented");
    rc_frame_delete(f);
}

void FsOps::abandon(rc_frame *f)
{
    Responder r(*m_conn, rc_frame_get(f, "id"), m_gate);
    /* A transfer that will never be finished leaves nothing behind: the
     * temp goes, and the file at the target -- if there was one -- is
     * untouched, because nothing was moved aside. */
    abortPut("the lane is stopping");
    r.err("stopping");
    rc_frame_delete(f);
}
