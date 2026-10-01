/* The ops answered on the reader thread.
 *
 * SYSINFO, PS, KILL, REBOOT and UPDATE. The bar for being here is not "is it quick"
 * but "can it block, and can it re-enter" -- kernel32 and Winsock only,
 * never USER or GDI, because on 9x a hung 16-bit app holding Win16Mutex
 * blocks those from every 32-bit thread and this thread carries the
 * heartbeat. PS and KILL are here rather than on the job lane on purpose:
 * behind a stuck EXEC is exactly when you need to find and end something.
 *
 * AUTH and PING are the Session's own, because they are about the
 * connection rather than about the machine. CANCEL joins this file once
 * there is a cancellable op to test it against.
 */
#ifndef RC_REMOTE_CONTROL_H
#define RC_REMOTE_CONTROL_H

#ifndef __cplusplus
#error "remote/control.h is C++"
#endif

#include "core/app.h"
#include "exec/process.h"
#include "remote/reply.h"
#include "wire/frame.h"

class ControlOps
{
public:
    /* `procs` is resolved once by the app: Toolhelp is absent on NT 4 and
     * the answer does not change while we run. `conn` is here only so
     * REBOOT and UPDATE can get their reply onto the wire before the
     * machine, or this process, goes. `app` is what UPDATE stops -- see the
     * long argument above update() in the .cpp for why a self-update is
     * allowed to do that and a remote QUIT still is not. */
    ControlOps(Processes &procs, Connection &conn, App &app);

    /* True if `f` was one of ours. Does not free it. */
    bool handle(Responder &r, rc_frame *f);

private:
    ControlOps(const ControlOps &);
    ControlOps &operator=(const ControlOps &);

    void sysinfo(Responder &r);
    void ps(Responder &r);
    void kill(Responder &r, rc_frame *f);
    void reboot(Responder &r, rc_frame *f);
    void update(Responder &r, rc_frame *f);

    Processes *m_procs;
    Connection *m_conn;
    App *m_app;
};

#endif
