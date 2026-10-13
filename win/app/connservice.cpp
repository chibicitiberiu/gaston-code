#include "app/connservice.h"

ConnectionService::ConnectionService(rc_net *net)
    : m_net(net)
{
}

const char *ConnectionService::name() const
{
    return "connection";
}

bool ConnectionService::init()
{
    return true;
}

void ConnectionService::signalStop()
{
    /* Never blocks, per the Service contract -- which is the point on the
     * session-ending path, where this may be the only pass that runs. */
    rc_net_signal_stop(m_net);
}

bool ConnectionService::join(DWORD timeoutMs)
{
    /* rc_net_stop times its own joins; the budget is its, not the App's. */
    (void)timeoutMs;
    return rc_net_stop(m_net) != 0;
}
