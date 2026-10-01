/* The lane that touches the disk: LS, STAT, GET, PUT, RM, MKDIR, MV, DRIVES.
 *
 * All of it over libfs (win/fs/), which the shipping client has been using
 * since step 5a -- so what runs here is code that has already moved real
 * files on a real 98 box, not a second implementation of the same ideas.
 * What is new is the wire in front of it.
 *
 * ITS OWN THREAD because the disk is slow and sometimes absent: a floppy
 * that is not there, a network share that has gone, a file somebody else has
 * open. None of that may reach the reader thread, which carries the
 * heartbeat.
 *
 * NO ROOT AND NO SWEEP, which is the difference from the sync layer and the
 * reason PUT works the way it does. win/sync moves files inside one folder
 * the person chose, and rc_sync_recover walks that folder at startup to
 * finish anything interrupted. Nothing will ever run a recovery pass over
 * C:\WINDOWS. So a PUT here never moves the old file aside -- a failure
 * between two renames would leave somebody's SYSTEM.INI as SYSTEM.INI.old
 * with nothing at the real path, in a directory nobody will ever sweep. It
 * writes a temp beside the target, verifies the CRC, and only then replaces:
 * either the original is still there or nothing is.
 */
#ifndef RC_REMOTE_FSOPS_H
#define RC_REMOTE_FSOPS_H

#ifndef __cplusplus
#error "remote/fsops.h is C++"
#endif

#include "fs/xfer.h"
#include "remote/lane.h"
#include "remote/reply.h"
#include "wire/connection.h"

class FsOps : public LaneHandler
{
public:
    explicit FsOps(Connection &conn);

    /* LaneHandler, on the fs thread. Both free `f`. */
    void handle(rc_frame *f);
    void abandon(rc_frame *f);

    /* LaneHandler, on the READER thread. A GET stops at its next chunk
     * because the gate refuses its next DATA; an open PUT is thrown away,
     * temp and all, because nothing will ever close it. */
    bool cancel(const char *id);

    /* On the fs thread: where a cancelled transfer is actually taken down. */
    void idle();

    void setGate(ReplyGate *gate) { m_gate = gate; }

private:
    FsOps(const FsOps &);
    FsOps &operator=(const FsOps &);

    void ls(Responder &r, rc_frame *f);
    void stat(Responder &r, rc_frame *f);
    void drives(Responder &r, rc_frame *f);
    void get(Responder &r, rc_frame *f);
    void put(Responder &r, rc_frame *f);
    void putChunk(Responder &r, rc_frame *f);
    void putEnd(Responder &r, rc_frame *f);
    void rm(Responder &r, rc_frame *f);
    void mkdir(Responder &r, rc_frame *f);
    void mv(Responder &r, rc_frame *f);
    void abortPut(const char *why);

    Connection *m_conn;
    ReplyGate *m_gate;

    /* One transfer at a time, and it belongs to the SESSION rather than to
     * the process: a new client gets a new FsOps, so a half-finished PUT
     * cannot outlive the caller that started it. */
    ChunkSink m_sink;
    char m_putId[64];
    unsigned long m_putCrc;
    /* Set by cancel() on the reader, acted on by idle() on this lane's own
     * thread. The sink holds a live handle this thread can be inside
     * WriteFile on, so the reader may not touch it -- the same reason, and
     * the same shape, as rc_syncrpc_abort. */
    volatile LONG m_abortWanted;
};

#endif
