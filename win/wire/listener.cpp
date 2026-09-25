#include "wire/listener.h"

#include <string.h>
#include "core/log.h"

Listener::Listener()
    : m_wsaUp(false),
      m_port(0)
{
}

Listener::~Listener()
{
    close();
}

bool Listener::open(const char *host, int port, int backlog)
{
    if (m_sock.valid())
    {
        rc_log("listen: already open on port %d", m_port);
        return false;
    }
    if (!m_wsaUp)
    {
        WSADATA wsa;
        if (WSAStartup(MAKEWORD(1, 1), &wsa) != 0)
        {
            rc_log("listen: WSAStartup failed");
            return false;
        }
        m_wsaUp = true;
    }

    SocketGuard s(socket(AF_INET, SOCK_STREAM, 0));
    if (!s.valid())
    {
        rc_log("listen: socket() failed: %d", WSAGetLastError());
        return false;
    }

    struct sockaddr_in addr;
    memset(&addr, 0, sizeof(addr));
    addr.sin_family = AF_INET;
    addr.sin_port = htons((u_short)port);
    addr.sin_addr.s_addr = htonl(INADDR_ANY);
    if (host && *host)
    {
        unsigned long ip = inet_addr(host);
        if (ip == INADDR_NONE)
        {
            rc_log("listen: %s is not a dotted address", host);
            return false;
        }
        addr.sin_addr.s_addr = ip;
    }

    /* No SO_REUSEADDR: see the header. */
    if (bind(s.get(), (struct sockaddr *)&addr, sizeof(addr)) == SOCKET_ERROR)
    {
        rc_log("listen: bind to %s:%d failed: %d", host && *host ? host : "*",
               port, WSAGetLastError());
        return false;
    }
    if (listen(s.get(), backlog) == SOCKET_ERROR)
    {
        rc_log("listen: listen() failed: %d", WSAGetLastError());
        return false;
    }

    struct sockaddr_in bound;
    int len = sizeof(bound);
    if (getsockname(s.get(), (struct sockaddr *)&bound, &len) == SOCKET_ERROR)
    {
        rc_log("listen: getsockname failed: %d", WSAGetLastError());
        return false;
    }
    m_port = ntohs(bound.sin_port);
    m_sock.reset(s.release());
    rc_log("listen: on %s:%d", host && *host ? host : "*", m_port);
    return true;
}

bool Listener::accept(SocketGuard &out, DWORD timeoutMs)
{
    out.reset();
    if (!m_sock.valid())
        return false;

    /* select() first, so a wait is bounded: a blocking accept() has no
     * timeout, and a stop would wait behind a peer that never comes. */
    fd_set ready;
    FD_ZERO(&ready);
    FD_SET(m_sock.get(), &ready);
    struct timeval tv;
    tv.tv_sec = (long)(timeoutMs / 1000);
    tv.tv_usec = (long)((timeoutMs % 1000) * 1000);
    int n = select(0, &ready, NULL, NULL, &tv);
    if (n == SOCKET_ERROR)
    {
        rc_log("listen: select failed: %d", WSAGetLastError());
        return false;
    }
    if (n == 0)
        return false;

    struct sockaddr_in peer;
    int len = sizeof(peer);
    out.reset(::accept(m_sock.get(), (struct sockaddr *)&peer, &len));
    if (!out.valid())
    {
        rc_log("listen: accept failed: %d", WSAGetLastError());
        return false;
    }
    rc_log("listen: connection from %s:%d", inet_ntoa(peer.sin_addr),
           ntohs(peer.sin_port));
    return true;
}

void Listener::close()
{
    m_sock.reset();
    m_port = 0;
    if (m_wsaUp)
    {
        WSACleanup();
        m_wsaUp = false;
    }
}
