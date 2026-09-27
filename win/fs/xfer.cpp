#include "fs/xfer.h"

#include "core/log.h"

/* --- receiving ------------------------------------------------------------ */

ChunkSink::ChunkSink()
    : m_received(0), m_active(false)
{
}

bool ChunkSink::begin(const char *root, const char *rel,
                      const WorkNames &names)
{
    m_received = 0;
    m_active = m_writer.begin(root, rel, names);
    return m_active;
}

bool ChunkSink::beginAbsolute(const char *target, const WorkNames &names)
{
    m_received = 0;
    m_active = m_writer.beginAbsolute(target, names);
    return m_active;
}

ChunkStatus ChunkSink::write(unsigned long off, const void *data, unsigned n)
{
    if (!m_active) return CHUNK_NOT_OPEN;
    if (off != m_received) return CHUNK_OUT_OF_ORDER;
    if (!m_writer.write(data, n)) return CHUNK_FAILED;
    m_received += n;
    return CHUNK_OK;
}

CommitStatus ChunkSink::commit(CommitVerifier *verifier)
{
    CommitStatus st;
    if (!m_active) return COMMIT_NOT_OPEN;
    st = m_writer.commit(verifier);
    m_active = false;
    return st;
}

void ChunkSink::abort(const char *why)
{
    if (m_active)
        rc_log("fs: abandoning the transfer to %s after %lu bytes -- %s",
               m_writer.targetPath(), m_received,
               why ? why : "no reason given");
    m_writer.abort();
    m_received = 0;
    m_active = false;
}

/* --- sending -------------------------------------------------------------- */

namespace {

/* Turns FileReader's flat byte stream into offset-carrying chunks, and feeds
 * the tee on the way past. One read, two destinations. */
class ChunkFanout : public ByteSink
{
public:
    ChunkFanout(ChunkConsumer &consumer, ByteSink *tee, unsigned long total)
        : m_consumer(consumer), m_tee(tee), m_total(total), m_off(0) {}

    virtual bool write(const void *data, unsigned len)
    {
        /* The tee first, and unconditionally. It is accumulating a checksum
         * or a classification over the whole file; skipping it because the
         * consumer is about to stop would leave it holding a partial answer
         * that looks complete. */
        if (m_tee && !m_tee->write(data, len))
            return false;
        if (!m_consumer.onChunk(data, len, m_off, m_total))
            return false;
        m_off += len;
        return true;
    }

    unsigned long sent() const { return m_off; }

private:
    ChunkConsumer &m_consumer;
    ByteSink      *m_tee;
    unsigned long  m_total;
    unsigned long  m_off;
};

} /* namespace */

ChunkSource::ChunkSource()
    : m_sent(0)
{
}

bool ChunkSource::open(const char *full)
{
    m_sent = 0;
    return m_reader.open(full);
}

PumpResult ChunkSource::pump(ChunkConsumer &consumer, ByteSink *tee,
                             unsigned chunkBytes, DWORD maxBytes)
{
    if (!m_reader.valid()) return PUMP_OPEN_FAILED;

    {
        ChunkFanout fanout(consumer, tee, m_reader.size());
        ReadResult r = m_reader.readAll(fanout, chunkBytes, maxBytes);

        m_sent = fanout.sent();
        switch (r)
        {
        case READ_DONE:         return PUMP_DONE;
        case READ_TRUNCATED:    return PUMP_TRUNCATED;
        case READ_SINK_STOPPED: return PUMP_CONSUMER_STOPPED;
        default:                return PUMP_READ_FAILED;
        }
    }
}
