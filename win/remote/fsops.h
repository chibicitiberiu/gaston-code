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

private:
    FsOps(const FsOps &);
    FsOps &operator=(const FsOps &);

    void ls(Responder &r, rc_frame *f);
    void stat(Responder &r, rc_frame *f);
    void drives(Responder &r);

    Connection *m_conn;
};

#endif
