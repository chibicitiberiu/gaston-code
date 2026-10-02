#include "fs/tree.h"
#include "fs/dir.h"
#include "fs/file.h"
#include "fs/ops.h"
#include "core/rc_cpp.h"
#include "core/str.h"

#include <shellapi.h>
#include <string.h>

TreeStats::TreeStats() : files(0), dirs(0), bytes(0), isDir(false),
                         sampleN(0)
{
    memset(sample, 0, sizeof(sample));
}

TreeDone::TreeDone() : files(0), dirs(0), bytes(0), renamed(false),
                       recycled(false)
{
}

/* ---- paths -----------------------------------------------------------
 *
 * Fixed MAX_PATH buffers, not std::string: every one of these is handed to
 * a Win32 call that has that limit anyway, and the string version cost 11KB
 * of an image whose load size is gated. */

typedef char PathBuf[MAX_PATH];

/* Absolute and drive-lettered, or refused. A relative path here would be
 * relative to the CLIENT's directory, which no caller means, and a UNC path
 * has no drive to ask about a Recycle Bin. */
static bool isFull(const char *p)
{
    return p && p[0] && p[1] == ':' && (p[2] == '\\' || p[2] == '/');
}

static bool norm(const char *p, PathBuf out)
{
    unsigned n = (unsigned)strlen(p);
    if (n >= MAX_PATH)
        return false;
    for (unsigned i = 0; i <= n; i++)
        out[i] = p[i] == '/' ? '\\' : p[i];
    while (n > 3 && out[n - 1] == '\\')
        out[--n] = '\0';
    return true;
}

/* base + '\' + rel (rel uses '/'). False when it does not fit. */
static bool join(const char *base, const char *rel, PathBuf out)
{
    unsigned b = (unsigned)strlen(base), r = (unsigned)strlen(rel);
    bool sep = b && base[b - 1] != '\\';
    if (b + (sep ? 1 : 0) + r >= MAX_PATH)
        return false;
    memcpy(out, base, b);
    if (sep)
        out[b++] = '\\';
    for (unsigned i = 0; i <= r; i++)
        out[b + i] = rel[i] == '/' ? '\\' : rel[i];
    return true;
}

static const char *baseName(const char *p)
{
    const char *s = strrchr(p, '\\');
    return s ? s + 1 : p;
}

static bool sameVolume(const char *a, const char *b)
{
    return (a[0] | 0x20) == (b[0] | 0x20);
}

/* `inner` is `outer` or below it -- copying a folder into itself would walk
 * what it is writing. Case-insensitive, at a separator boundary. */
static bool isInside(const char *inner, const char *outer)
{
    unsigned n = (unsigned)strlen(outer);
    if (strlen(inner) < n || CompareStringA(LOCALE_SYSTEM_DEFAULT,
                                            NORM_IGNORECASE, inner, n,
                                            outer, n) != CSTR_EQUAL)
        return false;
    return inner[n] == '\0' || inner[n] == '\\';
}

static void say(char *detail, unsigned cap, const char *what)
{
    if (detail && cap)
        rc_strf(detail, cap, "%s", what);
}

/* ---- the walk, collected ---------------------------------------------
 *
 * Plain data on the heap: an entry table sized to the cap and one growing
 * arena for the names. Transient -- it lives for one call. */

struct Entry
{
    unsigned off;           /* into the name arena */
    char     type;
    DWORD    size;
};

class Collect : public DirSink
{
public:
    Collect(TreeStats &st)
        : m_st(st), m_n(0), m_used(0), m_cap(0), m_failed(false)
    {
        m_entries = (Entry *)HeapAlloc(GetProcessHeap(), 0,
                                       sizeof(Entry) * (RC_TREE_MAX_ENTRIES
                                                        + 1));
        m_names = 0;
    }
    ~Collect()
    {
        if (m_entries) HeapFree(GetProcessHeap(), 0, m_entries);
        if (m_names) HeapFree(GetProcessHeap(), 0, m_names);
    }

    bool ok() const { return m_entries && !m_failed; }
    unsigned count() const { return m_n; }
    const char *rel(unsigned i) const { return m_names + m_entries[i].off; }
    char type(unsigned i) const { return m_entries[i].type; }
    DWORD size(unsigned i) const { return m_entries[i].size; }

    virtual bool onEntry(const char *rel, const FileInfo &info)
    {
        unsigned l = (unsigned)strlen(rel) + 1;
        if (!m_entries || m_n > RC_TREE_MAX_ENTRIES || !grow(l))
        {
            m_failed = true;
            return false;
        }
        memcpy(m_names + m_used, rel, l);
        m_entries[m_n].off = m_used;
        m_entries[m_n].type = info.type ? info.type : 'u';
        m_entries[m_n].size = info.size;
        m_used += l;
        m_n++;
        if (info.type == 'd')
            m_st.dirs++;
        else
        {
            m_st.files++;
            m_st.bytes += info.size;
        }
        if (m_st.sampleN < RC_TREE_SAMPLE)
            rc_strf(m_st.sample[m_st.sampleN++], MAX_PATH, "%s", rel);
        return m_st.bytes <= RC_TREE_MAX_BYTES;
    }

private:
    bool grow(unsigned need)
    {
        if (m_used + need <= m_cap)
            return true;
        unsigned cap = m_cap ? m_cap * 2 : 16384;
        while (cap < m_used + need)
            cap *= 2;
        char *p = m_names
            ? (char *)HeapReAlloc(GetProcessHeap(), 0, m_names, cap)
            : (char *)HeapAlloc(GetProcessHeap(), 0, cap);
        if (!p)
            return false;
        m_names = p;
        m_cap = cap;
        return true;
    }

    Collect(const Collect &);
    Collect &operator=(const Collect &);

    TreeStats &m_st;
    Entry     *m_entries;
    char      *m_names;
    unsigned   m_n, m_used, m_cap;
    bool       m_failed;
};

/* Everything below a folder, in walk order (a folder before its contents),
 * or `too_big`. A 'u' entry -- a folder whose path would not fit MAX_PATH --
 * is `too_long`: copying around it would silently drop a subtree. */
static const char *collect(const char *root, Collect &all, TreeStats &st,
                           char *detail, unsigned cap)
{
    WalkLimits lim(16, RC_TREE_MAX_ENTRIES);
    int count = 0;
    WalkResult r = walkTree(root, lim, NULL, all, count);
    if (r == WALK_OVERFLOW || st.bytes > RC_TREE_MAX_BYTES)
        return "too_big";
    if (!all.ok())
        return all.count() > RC_TREE_MAX_ENTRIES ? "too_big" : "no_memory";
    if (r == WALK_NO_ROOT)
        return "missing";
    for (unsigned i = 0; i < all.count(); i++)
        if (all.type(i) == 'u')
        {
            say(detail, cap, all.rel(i));
            return "too_long";
        }
    return 0;
}

const char *fsTreeStats(const char *path, TreeStats &out)
{
    FileInfo info;
    PathBuf p;
    if (!isFull(path) || !norm(path, p))
        return "bad_path";
    if (!statPath(p, info) || info.type == 0)
        return fsWhyNoStat();
    if (info.type != 'd')
    {
        out.files = 1;
        out.bytes = info.size;
        return 0;
    }
    out.isDir = true;
    Collect all(out);
    return collect(p, all, out, NULL, 0);
}

/* ---- copy ------------------------------------------------------------ */

static const char *whyNoCopy(void)
{
    switch (GetLastError())
    {
    case ERROR_FILE_EXISTS:
    case ERROR_ALREADY_EXISTS:      return "exists";
    case ERROR_DISK_FULL:
    case ERROR_HANDLE_DISK_FULL:    return "no_space";
    case ERROR_PATH_NOT_FOUND:      return "no_parent";
    case ERROR_ACCESS_DENIED:       return "denied";
    case ERROR_WRITE_PROTECT:       return "readonly";
    case ERROR_FILENAME_EXCED_RANGE:return "too_long";
    default:                        return "locked";
    }
}

/* One file, and the size checked afterwards: "copied" must mean the bytes
 * are there, whatever CopyFileA said about a disk that filled mid-file. */
static const char *copyOne(const char *src, const char *dst, bool overwrite,
                           DWORD size)
{
    FileInfo after;
    if (!CopyFileA(src, dst, overwrite ? FALSE : TRUE))
        return whyNoCopy();
    if (!statPath(dst, after) || after.size != size)
        return "short_copy";
    return 0;
}

/* Where a copy or move of `from` lands: `to`, or -- the Unix rule every
 * agent expects -- inside `to` when `to` is an existing folder. */
static bool landing(const char *from, const char *to, PathBuf out)
{
    FileInfo info;
    if (statPath(to, info) && info.type == 'd')
        return join(to, baseName(from), out);
    return norm(to, out);
}

static const char *copyTree(const char *src, const char *dst, bool overwrite,
                            TreeDone &done, char *detail, unsigned cap)
{
    FileInfo info;
    TreeStats st;
    PathBuf s, d;

    if (isInside(dst, src))
        return "into_itself";
    Collect all(st);
    const char *why = collect(src, all, st, detail, cap);
    if (why)
        return why;

    /* PREFLIGHT: every conflict before the first byte moves. */
    bool rootThere = statPath(dst, info) && info.type != 0;
    if (rootThere && info.type != 'd')
    {
        say(detail, cap, dst);
        return "type_clash";
    }
    if (rootThere && !overwrite)
    {
        say(detail, cap, dst);
        return "exists";
    }
    for (unsigned i = 0; i < all.count(); i++)
    {
        if (!join(dst, all.rel(i), d))
        {
            say(detail, cap, all.rel(i));
            return "too_long";
        }
        if (rootThere && statPath(d, info) && info.type != 0
            && (info.type == 'd') != (all.type(i) == 'd'))
        {
            say(detail, cap, d);
            return "type_clash";
        }
    }

    if (!rootThere && !CreateDirectoryA(dst, NULL))
    {
        say(detail, cap, dst);
        return whyNoCopy();
    }
    done.dirs++;
    for (unsigned i = 0; i < all.count(); i++)
    {
        join(src, all.rel(i), s);
        join(dst, all.rel(i), d);
        if (all.type(i) == 'd')
        {
            if (!CreateDirectoryA(d, NULL)
                && !(statPath(d, info) && info.type == 'd'))
            {
                say(detail, cap, d);
                return whyNoCopy();
            }
            done.dirs++;
            continue;
        }
        why = copyOne(s, d, overwrite, all.size(i));
        if (why)
        {
            say(detail, cap, d);
            return why;
        }
        done.files++;
        done.bytes += all.size(i);
    }
    return 0;
}

const char *fsCopyTree(const char *from, const char *to, bool overwrite,
                       TreeDone &done, char *detail, unsigned detailCap)
{
    FileInfo info, there;
    PathBuf src, toN, dst;
    if (!isFull(from) || !isFull(to) || !norm(from, src) || !norm(to, toN))
        return "bad_path";
    if (!statPath(src, info) || info.type == 0)
        return fsWhyNoStat();
    if (!landing(src, toN, dst))
        return "too_long";

    if (info.type != 'd')
    {
        if (statPath(dst, there) && there.type != 0)
        {
            if (there.type == 'd')
                return "type_clash";
            if (!overwrite)
                return "exists";
        }
        const char *why = copyOne(src, dst, overwrite, info.size);
        if (why)
        {
            say(detail, detailCap, dst);
            return why;
        }
        done.files = 1;
        done.bytes = info.size;
        return 0;
    }
    return copyTree(src, dst, overwrite, done, detail, detailCap);
}

/* ---- delete ---------------------------------------------------------- */

typedef int (WINAPI *FileOpFn)(LPSHFILEOPSTRUCTA);

/* The shell's own delete, to the Recycle Bin. Resolved, never imported --
 * shell32 is on every 9x, but an import is a load-time commitment this
 * image makes for nothing else. */
static const char *recycle(const char *path)
{
    char root[4];
    root[0] = path[0]; root[1] = ':'; root[2] = '\\'; root[3] = 0;
    if (GetDriveTypeA(root) != DRIVE_FIXED)
        return "no_recycle_bin";
    Dll shell(LoadLibraryA("shell32.dll"));
    FileOpFn op = shell.valid()
        ? (FileOpFn)GetProcAddress(shell.get(), "SHFileOperationA") : 0;
    if (!op)
        return "no_recycle_bin";

    /* pFrom is a LIST: double-NUL terminated, or the shell reads past it. */
    char from[MAX_PATH + 2];
    unsigned n = (unsigned)strlen(path);
    memcpy(from, path, n);
    from[n] = from[n + 1] = '\0';
    SHFILEOPSTRUCTA fo;
    memset(&fo, 0, sizeof(fo));
    fo.wFunc = FO_DELETE;
    fo.pFrom = from;
    /* ALLOWUNDO is the Recycle Bin; the rest keep the shell from asking a
     * question on a screen nobody may be watching. */
    fo.fFlags = FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_SILENT
              | FOF_NOERRORUI;
    int rc = op(&fo);
    FileInfo info;
    if (rc != 0 || fo.fAnyOperationsAborted)
        return "locked";
    /* Verified, not assumed: the shell has answered 0 for nothing done. */
    if (statPath(path, info) && info.type != 0)
        return "not_removed";
    return 0;
}

static const char *removeTree(const char *root, TreeDone &done,
                              char *detail, unsigned cap)
{
    TreeStats st;
    PathBuf p;
    Collect all(st);
    const char *why = collect(root, all, st, detail, cap);
    if (why)
        return why;
    /* Files first; then folders deepest first, which is the reverse of the
     * walk (a folder is emitted before its contents). */
    for (unsigned i = 0; i < all.count(); i++)
    {
        if (all.type(i) == 'd')
            continue;
        join(root, all.rel(i), p);
        if (!removeFile(p))
        {
            say(detail, cap, p);
            return GetLastError() == ERROR_ACCESS_DENIED ? "denied" : "locked";
        }
        done.files++;
        done.bytes += all.size(i);
    }
    for (unsigned i = all.count(); i-- > 0; )
    {
        if (all.type(i) != 'd')
            continue;
        join(root, all.rel(i), p);
        if (!RemoveDirectoryA(p))
        {
            say(detail, cap, p);
            return "locked";
        }
        done.dirs++;
    }
    if (!RemoveDirectoryA(root))
    {
        say(detail, cap, root);
        return "locked";
    }
    done.dirs++;
    return 0;
}

const char *fsDeleteTree(const char *path, bool permanent, TreeDone &done,
                         char *detail, unsigned detailCap)
{
    FileInfo info;
    PathBuf p;
    if (!isFull(path) || !norm(path, p))
        return "bad_path";
    if (fsIsDriveRoot(p))
        return "drive_root";
    if (!statPath(p, info) || info.type == 0)
        return fsWhyNoStat();

    /* The counts the reply carries, and the caps, for both routes. */
    TreeStats st;
    if (info.type == 'd')
    {
        Collect all(st);
        const char *why = collect(p, all, st, detail, detailCap);
        if (why)
            return why;
    }
    else
    {
        st.files = 1;
        st.bytes = info.size;
    }

    if (!permanent)
    {
        const char *why = recycle(p);
        if (why)
            return why;
        done.recycled = true;
        done.files = st.files;
        done.dirs = st.dirs + (info.type == 'd' ? 1 : 0);
        done.bytes = st.bytes;
        return 0;
    }
    if (info.type != 'd')
    {
        const char *why = fsRemoveFile(p);
        if (!why)
        {
            done.files = 1;
            done.bytes = info.size;
        }
        return why;
    }
    return removeTree(p, done, detail, detailCap);
}

/* ---- move ------------------------------------------------------------ */

const char *fsMoveTree(const char *from, const char *to, bool overwrite,
                       TreeDone &done, char *detail, unsigned detailCap)
{
    FileInfo info, there;
    PathBuf src, toN, dst;
    if (!isFull(from) || !isFull(to) || !norm(from, src) || !norm(to, toN))
        return "bad_path";
    if (fsIsDriveRoot(src))
        return "drive_root";
    if (!statPath(src, info) || info.type == 0)
        return fsWhyNoStat();
    if (!landing(src, toN, dst))
        return "too_long";
    bool isDir = info.type == 'd';
    if (isDir && isInside(dst, src))
        return "into_itself";

    bool dstThere = statPath(dst, there) && there.type != 0;
    if (dstThere && !overwrite)
    {
        say(detail, detailCap, dst);
        return "exists";
    }
    if (dstThere && (there.type == 'd') != isDir)
    {
        say(detail, detailCap, dst);
        return "type_clash";
    }

    /* A RENAME WHEN THE PLATFORM CAN: nothing in the way, or a file
     * replacing a file -- the old one removed only now, right before the
     * move. 98 also moves a FILE across drives itself (measured). */
    if (!dstThere || !isDir)
    {
        if (dstThere)
        {
            const char *why = fsRemoveFile(dst);
            if (why)
            {
                say(detail, detailCap, dst);
                return why;
            }
        }
        if (MoveFileA(src, dst))
        {
            done.renamed = sameVolume(src, dst);
            if (isDir)
                done.dirs = 1;
            else
            {
                done.files = 1;
                done.bytes = info.size;
            }
            return 0;
        }
        DWORD err = GetLastError();
        /* Anything but "a folder to another drive" is a real refusal, and
         * copying around it would hide it. */
        if (!isDir || sameVolume(src, dst))
        {
            say(detail, detailCap, dst);
            switch (err)
            {
            case ERROR_PATH_NOT_FOUND:       return "no_parent";
            case ERROR_FILENAME_EXCED_RANGE: return "too_long";
            case ERROR_ACCESS_DENIED:        return "denied";
            default:                         return "locked";
            }
        }
    }

    /* COPY, VERIFY, THEN REMOVE: a folder to another drive, or a folder
     * merging into one that exists. The source is removed only after every
     * file arrived at its full size. */
    const char *why = copyTree(src, dst, overwrite, done, detail, detailCap);
    if (why)
        return why;
    TreeDone gone;
    if (removeTree(src, gone, detail, detailCap))
        return "copied_not_removed";
    return 0;
}
