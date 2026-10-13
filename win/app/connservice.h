/* The bridge connection, as a service.
 *
 * Reconnector starts the connection from the UI; this service owns stopping
 * it. The composition root registers it after the main window, so App stops
 * it first, while the window its reader thread posts to still exists.
 * rc_net_stop bounds and times its own joins (up to about 42 s) and leaks
 * what did not stop; join() passes on its answer, so App reports a thread
 * that would not stop instead of hearing "done". The transport itself is
 * libwire's Connection; this is the client's adapter over it (net.cpp).
 *
 * BOTH HALVES OF THE Service CONTRACT SINCE 2026-10-13 (BACKLOG 23 item 4).
 * This used to declare only join() and hand everything to rc_net_stop, which
 * signals, drains and joins in one call. On the ordinary path that was
 * merely unusual. On the SESSION-ENDING path, where App abandons a service
 * rather than joining it, it meant nothing was signalled at all -- so a
 * child process the RPC worker had launched outlived the shutdown.
 *
 * signalStop() is now rc_rpc_stop: the worker is told, any child is killed,
 * and it returns at once. The FIN still waits for join(), and the comment
 * on rc_net_signal_stop says why moving it earlier would be wrong.
 */
#ifndef RC_CONNSERVICE_H
#define RC_CONNSERVICE_H

#ifndef __cplusplus
#error "app/connservice.h is C++"
#endif

#include "core/app.h"
#include "net/net.h"

class ConnectionService : public Service
{
public:
    explicit ConnectionService(rc_net *net);

    const char *name() const;
    bool init();
    void signalStop();
    bool join(DWORD timeoutMs);

private:
    rc_net *m_net;
};

#endif
