/* Writing a file so that a failure never leaves the old one destroyed.
 * libfs; libcore only.
 *
 * Every write goes to a temporary beside the target and is renamed into place
 * only after it verifies. MoveFileExA(REPLACE_EXISTING) is NT-only and a
 * static import of it kills the image at load on 9x, so the replace cannot be
 * made atomic -- what follows is the best 9x allows, and the two policies in
 * WorkNames are the two honest ways to lose the race.
 */
#ifndef RC_FS_WRITER_H
#define RC_FS_WRITER_H

#ifndef __cplusplus
#error "fs/writer.h is C++"
#endif

#include <windows.h>

#include "core/rc_cpp.h"
#include "fs/path.h"

enum CommitStatus
{
    COMMIT_OK = 0,
    COMMIT_NOT_OPEN,        /* nothing was begun, or it already finished */
    COMMIT_UNVERIFIABLE,    /* the temp cannot be read back at all */
    COMMIT_VERIFY_FAILED,   /* the verifier said these are not the bytes */
    COMMIT_NO_ROOM,         /* a working name would not fit beside the target */
    COMMIT_RENAME_FAILED
};

/* Why a verifier said no. The two refusals are NOT interchangeable and must
 * not be collapsed: UNREADABLE is transient and worth retrying, MISMATCH says
 * the bytes that arrived are not the bytes that were sent and a retry of the
 * same transfer will fail the same way. Reporting one as the other sends the
 * caller down the wrong branch -- and reporting a mismatch as unreadable makes
 * a corrupt transfer retry forever. */
enum VerifyResult
{
    VERIFY_OK = 0,
    VERIFY_MISMATCH,
    VERIFY_UNREADABLE
};

/* Asked once, after the last byte is on disk and before anything is replaced.
 * Injected rather than built in, because the checksum is miniz and libfs
 * depends on libcore alone -- and because the three writers in this tree want
 * three different answers: the sync layer's dual CRC, gremote's raw one, and
 * export's none at all (pass NULL). */
class CommitVerifier
{
public:
    virtual ~CommitVerifier() {}
    virtual VerifyResult verify(const char *tempPath) = 0;
};

/* Verify `temp` and put it at `target`. Exposed as well as used by FileWriter
 * because the sync layer's commit arrives as a separate RPC from its writes,
 * so the object that did the writing is long gone by then. One implementation
 * either way: this is the function that deletes and renames, and two copies of
 * it is how a file gets lost.
 *
 * On any refusal the temp is removed, EXCEPT after a failed rename with
 * `aside` off -- there the temp holds verified bytes and the target may be
 * gone, so it is left findable and named in the log rather than deleted.
 */
CommitStatus commitInto(const char *target, const char *temp,
                        CommitVerifier *verifier, const WorkNames &names);

/* Put back what an interrupted commit left behind, across a whole tree.
 * Only meaningful where `aside` was on -- which is to say, only in a root that
 * something sweeps at startup. */
void recoverInterrupted(const char *root, const WorkNames &names);

class FileWriter
{
public:
    FileWriter();

    /* Explicit, and NOT left to the members. They destruct in reverse
     * DECLARATION order, so TempFile would try to delete the file while
     * Handle still had it open -- DeleteFileA fails on an open file, and the
     * temp survives exactly the case this is meant to clean up. abort()
     * closes first. (fscheck's writer.abort case caught this.) */
    ~FileWriter();

    /* Opens `<root>/<rel>`'s temporary. False if the path is not contained, if
     * a working name would not fit beside it (measured from the inputs, never
     * from the joined string), or if the temp cannot be created; GetLastError
     * says which, and is set explicitly so a caller's classifier cannot read a
     * stale error from whatever ran last. */
    bool begin(const char *root, const char *rel, const WorkNames &names);

    /* The same, for a caller that already holds one absolute path and has no
     * root to be contained by. This is what a remote PUT uses. */
    bool beginAbsolute(const char *target, const WorkNames &names);

    bool write(const void *data, unsigned len);

    /* Closes the temp and replaces the target. The writer is finished
     * afterwards either way. */
    CommitStatus commit(CommitVerifier *verifier);

    /* Closes and removes the temp, leaving the target untouched. Idempotent,
     * and the destructor does it for a writer that was never committed. */
    void abort();

    bool active() const { return m_open; }
    const char *tempPath() const { return m_temp.get(); }
    const char *targetPath() const { return m_target.str(); }

private:
    bool openTemp(const char *target, const WorkNames &names);

    FileWriter(const FileWriter &);
    FileWriter &operator=(const FileWriter &);

    Handle    m_handle;
    TempFile  m_temp;       /* owns the NAME: the dtor deletes it unless kept */
    Path      m_target;
    WorkNames m_names;
    bool      m_open;
};

#endif
