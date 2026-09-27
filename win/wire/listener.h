/* The server half of libwire: a bound, listening socket and a timed accept.
 * What an accepted socket becomes is Connection::adopt's business; who is
 * allowed to hold it (one live client, everyone else answered busy) is the
 * server's (gremote, docs/REMOTE.md).
 *
 * A CHILD CAN INHERIT THIS SOCKET, and there is no way to stop it here.
 * CreateProcess with bInheritHandles = TRUE hands over every inheritable
 * handle, and libexec passes TRUE because the child needs its output pipe.
 * Both mechanisms were tried and neither works: SetHandleInformation is
 * Win2000+ and on the import deny list (win/exec/execjob.cpp says why), and
 * the DuplicateHandle trick that file uses for a PIPE destroys a SOCKET --
 * closesocket and CloseHandle both take it with them, and the port then
 * never comes free at all.
 *
 * What bounds it is measured rather than assumed, in wirecheck: on NT and
 * Wine an inheriting child holds the port for as long as it lives and
 * releases it the moment it goes; on Windows 98, the platform this ships to,
 * a child does not hold it at all (docs/REMOTE.md, P200 checklist). A server
 * restarting into a port a stray child still holds is what the bind retry is
 * for.
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
