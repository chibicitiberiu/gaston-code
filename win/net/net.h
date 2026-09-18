/* Winsock connection, framing, and the reader and sender threads.
 *
 * FOUR threads now: UI, reader, sender, and the RPC worker in rpc.c.
 *
 * Threading contract, and it is not negotiable:
 *   - the reader thread NEVER calls a window API that can block or re-enter.
 *     PostMessage only. SendMessage from a worker into a window sitting in a
 *     modal loop is a textbook 9x deadlock.
 *   - frame ownership is THREE-WAY, not two, since the RPC worker arrived:
 *       * a frame posted with WM_RC_FRAME is owned by the UI thread, which
 *         frees it and decrements `inflight`;
 *       * a frame handed to the RPC worker is owned by the WORKER, which
 *         frees it -- EXCEPT on the two paths where the worker refuses it
 *         (already busy, or not running), where the frame is freed on the
 *         READER thread that offered it. The rule is "whoever ends up
 *         holding it frees it", not "the worker always frees it";
 *       * if PostMessage fails the READER frees it -- otherwise the pointer
 *         leaks on a machine with single-digit MB free, at exactly the moment
 *         it is already struggling.
 *     Get this wrong and it is a double free, which on 9x is a reboot.
 *   - NOBODY sends on the socket except the sender thread. rc_net_send queues
 *     a built frame and returns. It used to hold a critical section across a
 *     blocking send(), which meant a 32KB result on a slow link froze the UI
 *     thread -- and with a second sender it would have interleaved two frames
 *     on a stream that has no resync, dropping the link.
 */
#ifndef RC_NET_H
#define RC_NET_H


#ifdef __cplusplus
extern "C" {
#endif
#include <windows.h>
#include <winsock.h>
#include "net/frameproto.h"

#define WM_RC_FRAME      (WM_APP + 1)   /* lParam: rc_frame*, UI thread frees */
#define WM_RC_STATE      (WM_APP + 2)   /* wParam: rc_state                   */

typedef enum {
    RC_DISCONNECTED = 0,
    RC_CONNECTING,
    RC_CONNECTED,
    RC_FAILED
} rc_state;

/* One queued outbound frame: already encoded, owned by the queue. */
typedef struct rc_outq_item {
    struct rc_outq_item *next;
    unsigned             len;
    unsigned             sent;
    char                 data[1];   /* len bytes follow */
} rc_outq_item;

/* Bounded in BYTES, not items: sixteen 64KB frames would be a megabyte.
 *
 * It is a SOFT cap. A writer that has waited its limit enqueues anyway rather
 * than dropping the frame, because losing a frame on a stream with no resync
 * is worse than overshooting the budget by one. So the true ceiling is
 * RC_OUTQ_MAX_BYTES plus one frame per blocked writer. */
#define RC_OUTQ_MAX_BYTES (64 * 1024)

typedef struct {
    HWND     notify;
    char     host[64];
    int      port;
    char     secret[128];
    SOCKET   sock;
    HANDLE   thread;          /* reader */
    HANDLE   sender;          /* owns the socket for sending */
    CRITICAL_SECTION send_lock;   /* guards the queue below, never the wire */
    rc_outq_item    *outq_head, *outq_tail;
    unsigned         outq_bytes;
    HANDLE           outq_wake;   /* sender waits on this */
    HANDLE           outq_room;   /* a full-queue writer waits on this */
    volatile LONG running;
    volatile LONG inflight;     /* frames posted but not yet freed by the UI  */
    volatile rc_state state;
    unsigned long rx_bytes;
    unsigned long tx_bytes;
    int           started;      /* rc_net_start succeeded; needs teardown */
    char          sess[64];
} rc_net;

int  rc_net_start(rc_net *n, HWND notify, const char *host, int port,
                 const char *secret);
void rc_net_stop(rc_net *n);
int  rc_net_send(rc_net *n, const char *type,
                 const char *const *keys, const char *const *vals, int nkv,
                 const void *body, unsigned body_len);
const char *rc_state_name(rc_state s);

/* The UI thread's half of the frame-ownership contract above, spelled out
 * once instead of by hand at every WM_RC_FRAME site: rc_frame_free() releases
 * the frame's own allocations, HeapFree() releases the frame struct itself (a
 * SECOND, separate allocation from the reader thread), and only then is
 * `inflight` decremented -- get the order wrong, or drop the decrement, and
 * the reader thread's backpressure gate wedges at RC_MAX_INFLIGHT with no
 * symptom but a stalled transcript.
 *
 * This belongs in net.c as rc_net_frame_done(), next to the `inflight` field
 * it touches and the reader loop that spins on it -- net.c is outside this
 * pass's file set (audit finding 01.24), so it lives here, inline, as the
 * closest safe substitute: callers still make one call instead of three
 * hand-ordered steps, even though the struct field stays reachable directly
 * rather than becoming properly private to net.c. */
static __inline void rc_net_frame_done(rc_net *n, rc_frame *f)
{
    rc_frame_free(f);
    HeapFree(GetProcessHeap(), 0, f);
    InterlockedDecrement(&n->inflight);
}

#ifdef __cplusplus
}
#endif

#endif
