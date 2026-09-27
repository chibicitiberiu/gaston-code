#include "remote/fsops.h"

#include <stdlib.h>
#include <string.h>
#include "core/log.h"
#include "core/str.h"
#include "fs/dir.h"
#include "fs/drives.h"
#include "fs/file.h"

/* A DATA body's worth of records. Well under the 64KB frame cap, and small
 * enough that a long listing starts arriving while it is still being made. */
enum { FS_CHUNK = 8192 };

/* Entries in one LS before it stops and says so. A directory with more than
 * this in it is a directory nobody wants printed, and an unbounded listing
 * on a 32MB box is the bounded-means-fixed trap in reverse. */
enum { LS_DEFAULT_MAX = 2000 };

FsOps::FsOps(Connection &conn) : m_conn(&conn)
{
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
    listDirectory(path, *rows.get());
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

        rc_strf(line, sizeof line, "%c %u %d %08lx %lu %lu %s\n",
                d.letter, d.type, d.hasMedia ? 1 : 0,
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

void FsOps::drives(Responder &r)
{
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
     * by a read-only call, blocking this thread until it is dismissed. */
    enumerateDrives(*rows.get());
    rows->flush();
    if (rows->stopped())
        return;
    rc_strf(count, sizeof count, "%d", rows->count());
    keys[0] = "count";
    vals[0] = count;
    r.end(keys, vals, 1);
}

void FsOps::handle(rc_frame *f)
{
    Responder r(*m_conn, rc_frame_get(f, "id"));
    if (strcmp(f->type, "LS") == 0)
        ls(r, f);
    else if (strcmp(f->type, "STAT") == 0)
        stat(r, f);
    else if (strcmp(f->type, "DRIVES") == 0)
        drives(r);
    else
        r.err("not_implemented");
    rc_frame_delete(f);
}

void FsOps::abandon(rc_frame *f)
{
    Responder r(*m_conn, rc_frame_get(f, "id"));
    r.err("stopping");
    rc_frame_delete(f);
}
