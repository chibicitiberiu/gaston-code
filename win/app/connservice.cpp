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
    (void)timeoutMs;
    rc_net_stop(m_net);
    return true;
}
