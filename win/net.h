/* Winsock connection, framing, and the reader thread.
 *
 * Threading contract, and it is not negotiable:
 *   - the reader thread NEVER calls a window API that can block or re-enter.
 *     PostMessage only. SendMessage from a worker into a window sitting in a
 *     modal loop is a textbook 9x deadlock.
 *   - every posted frame is heap-allocated and owned by the UI thread, which
 *     frees it. If PostMessage fails the READER frees it -- otherwise the
 *     pointer leaks, on a machine with 8-12MB free, at exactly the moment it
 *     is already struggling.
 */
#ifndef RC_NET_H
#define RC_NET_H

#include <windows.h>
#include <winsock.h>
#include "frameproto.h"

#define WM_RC_FRAME      (WM_APP + 1)   /* lParam: rc_frame*, UI thread frees */
#define WM_RC_STATE      (WM_APP + 2)   /* wParam: rc_state                   */

typedef enum {
    RC_DISCONNECTED = 0,
    RC_CONNECTING,
    RC_CONNECTED,
    RC_FAILED
} rc_state;

typedef struct {
    HWND     notify;
    char     host[64];
    int      port;
    char     secret[128];
    SOCKET   sock;
    HANDLE   thread;
    CRITICAL_SECTION send_lock;
    volatile LONG running;
    volatile LONG inflight;     /* frames posted but not yet freed by the UI  */
    volatile rc_state state;
    unsigned long rx_bytes;
    unsigned long tx_bytes;
    unsigned      next_seq;     /* resume point */
    char          sess[64];
} rc_net;

int  rc_net_start(rc_net *n, HWND notify, const char *host, int port,
                 const char *secret);
void rc_net_stop(rc_net *n);
int  rc_net_send(rc_net *n, const char *type,
                 const char *const *keys, const char *const *vals, int nkv,
                 const void *body, unsigned body_len);
const char *rc_state_name(rc_state s);

#endif
