/* The server half of libwire: a bound, listening socket and a timed accept.
 * What an accepted socket becomes is Connection::adopt's business; who is
 * allowed to hold it (one live client, everyone else answered busy) is the
 * server's (gremote, docs/REMOTE.md).
 *
 * NEVER SO_REUSEADDR. On Winsock it does not mean what it means on Unix: it
 * lets a second process bind the SAME port while the first is still
 * listening, and which of the two gets a connection is then undefined. The
 * TIME_WAIT case it would help with is the peer's, not ours -- the server
 * closes with a FIN and the client ends in TIME_WAIT -- so there is nothing
 * to gain for the risk of a hijacked port.
 */
#ifndef RC_WIRE_LISTENER_H
#define RC_WIRE_LISTENER_H

#ifndef __cplusplus
#error "wire/listener.h is C++"
#endif

#include <windows.h>
#include <winsock.h>
#include "wire/socket.h"

class Listener
{
public:
    Listener();
    ~Listener();

    /* Bind to `host` (a dotted address; NULL or "" for every interface) and
     * `port` (0 picks a free one, for tests), and listen. False, logged, on
     * any failure, including a port already bound. */
    bool open(const char *host, int port, int backlog);

    /* The port actually bound; 0 before open(). */
    int port() const { return m_port; }

    /* Wait up to `timeoutMs` for a connection and hand it to `out`. False on
     * a timeout (out left empty), which is the ordinary answer: the caller
     * polls its stop flag between waits, so a stop never waits on a peer
     * that never comes. */
    bool accept(SocketGuard &out, DWORD timeoutMs);

    /* Stop listening. Safe twice; the destructor does it too. */
    void close();

private:
    Listener(const Listener &);
    Listener &operator=(const Listener &);

    SocketGuard m_sock;
    bool m_wsaUp;
    int m_port;
};

#endif
