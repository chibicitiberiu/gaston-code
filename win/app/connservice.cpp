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

bool ConnectionService::join(DWORD timeoutMs)
{
    /* rc_net_stop times its own joins; the budget is its, not the App's. */
    (void)timeoutMs;
    return rc_net_stop(m_net) != 0;
}
