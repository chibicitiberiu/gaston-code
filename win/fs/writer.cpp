#include "fs/writer.h"

#include <string.h>

#include "core/log.h"
#include "core/str.h"
#include "fs/dir.h"
#include "fs/file.h"

/* --- the replace ---------------------------------------------------------- */

CommitStatus commitInto(const char *target, const char *temp,
                        CommitVerifier *verifier, const WorkNames &names)
{
    Path aside;
    DWORD attr;

    if (!target || !*target || !temp || !*temp) return COMMIT_NOT_OPEN;

    /* Verify before replacing anything. A transfer that cannot establish that
     * the bytes on disk are the bytes that were sent is reported, never
     * retried over the top of the user's file. */
    {
        FileInfo got;
        if (!statPath(temp, got) || got.type != 'f')
        {
            rc_log("fs: cannot read back %s after writing it -- not "
                   "committing", target);
            removeFile(temp);
            return COMMIT_UNVERIFIABLE;
        }
    }
    if (verifier)
    {
        VerifyResult v = verifier->verify(temp);
        if (v != VERIFY_OK)
        {
            /* DeleteFileA on a file another handle holds open fails, and it
             * should: guessing that the file is ours to remove while somebody
             * else is reading it is worse than leaving it for the recovery
             * pass. So this is a request, not a guarantee. */
            removeFile(temp);
            return v == VERIFY_UNREADABLE ? COMMIT_UNVERIFIABLE
                                          : COMMIT_VERIFY_FAILED;
        }
    }

    /* A read-only target: DeleteFileA fails on it, then MoveFileA fails
     * because the target still exists, and the temp is left in the tree where
     * the next scan picks it up as a new file and pushes it to the bridge.
     * Clear the attribute now and restore it on every path out, so a failed
     * commit does not leave the file silently stripped of write protection. */
    attr = GetFileAttributesA(target);
    ReadOnlyGuard ro(target, attr);

    if (!names.aside)
    {
        /* No aside. The window is smaller -- one delete and one rename with
         * nothing in between -- but if the rename fails the target is already
         * gone, so the temp is NOT deleted: it holds verified bytes, and
         * leaving it findable beats leaving nothing. This is the only policy
         * that is safe outside a root something sweeps at startup. */
        if (attr != 0xFFFFFFFF && !removeFile(target))
        {
            rc_log("fs: cannot remove %s to replace it", target);
            return COMMIT_RENAME_FAILED;
        }
        if (!MoveFileA(temp, target))
        {
            rc_log("fs: wrote %s but could not rename it to %s -- the bytes "
                   "are verified and the temp is left in place", temp, target);
            return COMMIT_RENAME_FAILED;
        }
        return COMMIT_OK;
    }

    /* Move the old file ASIDE rather than deleting it. There is still an
     * instant with no file at the path, but every intermediate state holds a
     * full copy under a name the recovery pass knows.
     *
     * The reserve is checked again HERE, not only where the temp was opened,
     * because this is the function that deletes and renames. If `aside` were
     * to alias `temp`, the delete below would destroy the verified new bytes
     * and the two moves would put the user's old file straight back -- and we
     * would return OK, telling the caller to advance over content this machine
     * never took. Refusing costs a retry; not refusing costs the edit. */
    if (strlen(target) + names.reserve > MAX_PATH - 1)
    {
        rc_log("fs: refusing to commit %s -- no room for a %s beside it",
               target, names.oldSuffix);
        removeFile(temp);
        return COMMIT_NO_ROOM;
    }

    aside.set(target);
    aside.cat(names.oldSuffix);
    if (!aside.ok())
    {
        removeFile(temp);
        return COMMIT_NO_ROOM;
    }

    DeleteFileA(aside.str());
    if (attr != 0xFFFFFFFF) MoveFileA(target, aside.str());

    if (!MoveFileA(temp, target))
    {
        /* Put the user's file back. */
        if (attr != 0xFFFFFFFF) MoveFileA(aside.str(), target);
        removeFile(temp);
        return COMMIT_RENAME_FAILED;
    }
    DeleteFileA(aside.str());
    return COMMIT_OK;
}

/* --- recovery ------------------------------------------------------------- */

namespace {

class RecoverSink : public DirSink
{
public:
    RecoverSink(const char *root, const WorkNames &names)
        : m_root(root), m_names(names) {}

    virtual bool onEntry(const char *rel, const FileInfo &info)
    {
        Path work;
        unsigned len;

        if (info.type == 'd') return true;

        len = (unsigned)strlen(rel);

        /* Both joins checked. A truncated one names a DIFFERENT file, and one
         * of these branches renames while the other deletes. */
        if (endsIn(rel, len, m_names.oldSuffix))
        {
            Path real;
            work.join(m_root, rel);
            if (!work.ok())
            {
                rc_log("fs: cannot recover %s -- the joined path does not fit",
                       rel);
                return true;
            }
            real.set(work.str());
            if (!real.ok()) return true;
            /* Safe only transitively -- the suffix test above proved the name
             * is longer than the suffix -- but nothing at THIS line says so,
             * and an unsigned subtraction that underflows here indexes
             * wildly. State the invariant. */
            {
                unsigned rl = (unsigned)strlen(real.str());
                unsigned sl = (unsigned)strlen(m_names.oldSuffix);
                if (rl <= sl) return true;
                real.raw()[rl - sl] = '\0';
            }
            if (GetFileAttributesA(real.str()) == 0xFFFFFFFF)
            {
                rc_log("fs: recovering %s -- an interrupted commit",
                       real.str());
                MoveFileA(work.str(), real.str());
            }
            else
            {
                DeleteFileA(work.str());
            }
        }
        else if (endsIn(rel, len, m_names.tmpSuffix))
        {
            work.join(m_root, rel);
            if (!work.ok())
            {
                rc_log("fs: not deleting a temp for %s -- the joined path does "
                       "not fit, and a truncated one names another file", rel);
                return true;
            }
            DeleteFileA(work.str());      /* an abandoned transfer */
        }
        return true;
    }

private:
    static bool endsIn(const char *name, unsigned len, const char *suffix)
    {
        unsigned sn;
        if (!suffix || !*suffix) return false;
        sn = (unsigned)strlen(suffix);
        if (len <= sn) return false;
        return strcmp(name + len - sn, suffix) == 0;
    }

    const char *m_root;
    WorkNames   m_names;
};

} /* namespace */

void recoverInterrupted(const char *root, const WorkNames &names)
{
    RecoverSink sink(root, names);
    /* No filter: the working names are exactly what this pass is looking for,
     * and no cap, because stopping halfway through a recovery would leave the
     * rest of the tree in the state it is trying to fix. */
    WalkLimits limits(16, 0xFFFFFFFFu);
    int count = 0;
    walkTree(root, limits, 0, sink, count);
}

/* --- the writer ----------------------------------------------------------- */

FileWriter::FileWriter()
    : m_handle(INVALID_HANDLE_VALUE), m_open(false)
{
}

FileWriter::~FileWriter()
{
    abort();
}

bool FileWriter::begin(const char *root, const char *rel,
                       const WorkNames &names)
{
    if (!isContainedRelative(rel))
    {
        SetLastError(ERROR_INVALID_NAME);
        return false;
    }
    /* Refuse rather than truncate, and refuse BEFORE anything is opened.
     * Measured from the inputs, because the join itself truncates silently and
     * would hide the very thing being tested. */
    if (!fitsWithReserve(root, rel, MAX_PATH, names))
    {
        rc_log("fs: refusing %s -- no room for a working name beside it", rel);
        SetLastError(ERROR_FILENAME_EXCED_RANGE);
        return false;
    }
    m_target.join(root, rel);
    if (!m_target.ok())
    {
        SetLastError(ERROR_FILENAME_EXCED_RANGE);
        return false;
    }
    return openTemp(m_target.str(), names);
}

bool FileWriter::beginAbsolute(const char *target, const WorkNames &names)
{
    Path full;

    full.set(target);
    if (!full.ok())
    {
        SetLastError(ERROR_FILENAME_EXCED_RANGE);
        return false;
    }
    return openTemp(full.str(), names);
}

bool FileWriter::openTemp(const char *target, const WorkNames &names)
{
    Path tmp;

    abort();                       /* a second begin replaces the first */

    m_names = names;
    m_target.set(target);
    if (!m_target.ok())
    {
        SetLastError(ERROR_FILENAME_EXCED_RANGE);
        return false;
    }

    tmp.set(m_target.str());
    tmp.cat(names.tmpSuffix);
    if (!tmp.ok() || !m_temp.arm(tmp.str()))
    {
        rc_log("fs: refusing %s -- no room for a %s beside it",
               m_target.str(), names.tmpSuffix);
        SetLastError(ERROR_FILENAME_EXCED_RANGE);
        return false;
    }

    /* Binary by construction -- CreateFileA does no translation. The CRT in
     * text mode would translate on top of whatever the sender already did and
     * add a CR to every line, on every pass. */
    m_handle.reset(CreateFileA(m_temp.get(), GENERIC_WRITE, 0, NULL,
                               CREATE_ALWAYS, FILE_ATTRIBUTE_NORMAL, NULL));
    if (!m_handle.valid())
    {
        DWORD err = GetLastError();
        m_temp.discard();
        SetLastError(err);
        return false;
    }
    m_open = true;
    return true;
}

bool FileWriter::write(const void *data, unsigned len)
{
    DWORD wrote = 0;
    if (!m_open) return false;
    if (!len) return true;
    if (!WriteFile(m_handle.get(), data, len, &wrote, NULL)) return false;
    return wrote == len;
}

CommitStatus FileWriter::commit(CommitVerifier *verifier)
{
    CommitStatus st;

    if (!m_open) return COMMIT_NOT_OPEN;

    /* Closed first: the verifier has to be able to read it. */
    m_handle.reset(INVALID_HANDLE_VALUE);
    m_open = false;

    st = commitInto(m_target.str(), m_temp.get(), verifier, m_names);

    /* commitInto owns the temp from here: it removed it on every refusal
     * except the rename failure it deliberately leaves behind. Either way the
     * guard must not delete it again. */
    m_temp.keep();
    return st;
}

void FileWriter::abort()
{
    m_handle.reset(INVALID_HANDLE_VALUE);
    m_temp.discard();
    m_open = false;
}
