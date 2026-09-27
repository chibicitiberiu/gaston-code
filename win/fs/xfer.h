/* Moving a file in pieces, in either direction. libfs; libcore only.
 *
 * This is the primitive docs/BACKLOG.md 6 describes. The same shape had been
 * written three times independently -- the sync layer's PUT, the sync layer's
 * GET, and the export sink -- and each one had learned a different subset of
 * the lessons. What every version converged on is here once: an offset that
 * must continue where the last chunk stopped, a temporary that is renamed only
 * after the bytes verify, and an abort that leaves nothing behind.
 *
 * NEITHER SIDE KNOWS THE WIRE. Chunks arrive as bytes with an offset and leave
 * as bytes with an offset; which frame carried them, and what to answer, is
 * the exe's business (REMOTE.md).
 */
#ifndef RC_FS_XFER_H
#define RC_FS_XFER_H

#ifndef __cplusplus
#error "fs/xfer.h is C++"
#endif

#include <windows.h>

#include "core/bytesink.h"
#include "fs/reader.h"
#include "fs/writer.h"

enum ChunkStatus
{
    CHUNK_OK = 0,
    CHUNK_NOT_OPEN,
    CHUNK_OUT_OF_ORDER,   /* this is not where the last chunk stopped */
    CHUNK_FAILED          /* the write itself failed; GetLastError says why */
};

/* Receiving a file. One transfer at a time per sink, which is a property
 * rather than a limitation: two interleaving into one temporary is the bug
 * `busy` exists to refuse. */
class ChunkSink
{
public:
    ChunkSink();

    bool begin(const char *root, const char *rel, const WorkNames &names);
    bool beginAbsolute(const char *target, const WorkNames &names);

    /* `off` is where the sender says this chunk belongs. It is CHECKED, not
     * trusted: a chunk that does not continue where the last one stopped means
     * a frame was lost, duplicated or reordered, and writing it anyway
     * produces a file that is the right length and the wrong contents -- which
     * then checksums as a mismatch with no clue as to why. Two of the three
     * implementations this replaces did not check. */
    ChunkStatus write(unsigned long off, const void *data, unsigned n);

    CommitStatus commit(CommitVerifier *verifier);

    /* `why` is logged, not returned: an abort is somebody else's decision
     * arriving here, and the reason is worth keeping in the log next to the
     * temp file that just disappeared. */
    void abort(const char *why);

    bool active() const { return m_active; }
    unsigned long received() const { return m_received; }
    const char *tempPath() const { return m_writer.tempPath(); }
    const char *targetPath() const { return m_writer.targetPath(); }

private:
    ChunkSink(const ChunkSink &);
    ChunkSink &operator=(const ChunkSink &);

    FileWriter    m_writer;
    unsigned long m_received;
    bool          m_active;
};

class ChunkConsumer
{
public:
    virtual ~ChunkConsumer() {}

    /* False stops the transfer at this boundary and is not an error: a
     * consumer whose connection has gone says so rather than letting a
     * megabyte be produced for nobody. */
    virtual bool onChunk(const void *data, unsigned len, unsigned long off,
                         unsigned long total) = 0;
};

enum PumpResult
{
    PUMP_DONE = 0,
    PUMP_TRUNCATED,          /* stopped at maxBytes with more on disk */
    PUMP_CONSUMER_STOPPED,
    PUMP_OPEN_FAILED,
    PUMP_READ_FAILED
};

/* Sending a file. */
class ChunkSource
{
public:
    ChunkSource();

    bool open(const char *full);
    bool valid() const { return m_reader.valid(); }

    /* Known BEFORE the first chunk, so a receiver can size or refuse up
     * front rather than discovering the size when it runs out of room. */
    unsigned long total() const { return m_reader.size(); }

    /* Read once and hand the bytes to both. `tee` may be NULL; when it is not,
     * it sees every byte exactly once and in order, which is what the sync
     * layer's classifier needs -- its CR carry depends on it. That tee is the
     * whole reason this reads once instead of twice.
     *
     * `maxBytes` is honoured HERE, on the machine holding the file. A cap
     * applied by the receiver still has this side read and send everything,
     * so a request for a swap file is a socket held open for as long as it
     * takes. */
    PumpResult pump(ChunkConsumer &consumer, ByteSink *tee,
                    unsigned chunkBytes, DWORD maxBytes);

    unsigned long sent() const { return m_sent; }

private:
    ChunkSource(const ChunkSource &);
    ChunkSource &operator=(const ChunkSource &);

    FileReader    m_reader;
    unsigned long m_sent;
};

#endif
