/* The lane that runs a child: EXEC and LAUNCH.
 *
 * One at a time, on its own thread, because an EXEC may take the two
 * minutes the config allows and nothing else on the machine should wait for
 * it. A second one while it runs is answered `busy` with the age of the one
 * in the slot -- see remote/lane.h for why that is a refusal and not a
 * queue.
 *
 * `exit` IS ONLY MEANINGFUL WITHOUT A SHELL, measured on the VM
 * (docs/measurements/execprobe-vm-gui-20260926.txt): COMMAND.COM /c on 98
 * exits 0 whatever happens, even for a program that does not exist, while a
 * program spawned directly returns its real code. So END carries
 * `via_shell`, and it is a fact rather than a verdict -- the caller decides
 * what `exit=0` is worth once it knows how the thing was started.
 *
 * THE GUEST DOES NOT CHOOSE THE SHELL. HELLO says what this machine is; the
 * caller sends the invocation prefix it wants spliced, or none at all. A
 * guest that picked for itself would be guessing on behalf of something that
 * knows better.
 */
#ifndef RC_REMOTE_JOBOPS_H
#define RC_REMOTE_JOBOPS_H

#ifndef __cplusplus
#error "remote/jobops.h is C++"
#endif

#include "exec/execjob.h"
#include "remote/config.h"
#include "remote/lane.h"
#include "remote/reply.h"
#include "wire/connection.h"

class JobOps : public LaneHandler
{
public:
    JobOps(const ExecConfig &cfg, Connection &conn);

    /* LaneHandler, on the job thread. Both free `f`. */
    void handle(rc_frame *f);
    void abandon(rc_frame *f);

private:
    JobOps(const JobOps &);
    JobOps &operator=(const JobOps &);

    void exec(Responder &r, rc_frame *f);
    void launch(Responder &r, rc_frame *f);

    const ExecConfig *m_cfg;
    Connection *m_conn;
};

#endif
