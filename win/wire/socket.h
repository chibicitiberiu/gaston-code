/* The socket guard, beside rc_cpp.h's rather than in it: rc_cpp.h is
 * included everywhere and winsock.h should not be. Same Unique<> as every
 * other handle; release() is how a stuck thread's socket is deliberately
 * leaked (wire/connection.h).
 *
 * closesocket, never an abortive close: an RST is reported to crash Win98's
 * Winsock outright. Callers shutdown(SD_SEND) first where the peer is owed a
 * FIN. */
#ifndef RC_WIRE_SOCKET_H
#define RC_WIRE_SOCKET_H

#ifndef __cplusplus
#error "wire/socket.h is C++"
#endif

#include <windows.h>
#include <winsock.h>
#include "core/rc_cpp.h"

struct P_Socket {
    static SOCKET none() { return INVALID_SOCKET; }
    static int  live(SOCKET s) { return s != INVALID_SOCKET; }
    static void release(SOCKET s) { closesocket(s); }
};

typedef Unique<SOCKET, P_Socket> SocketGuard;

#endif
