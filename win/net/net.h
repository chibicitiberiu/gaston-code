/* The client's link to the bridge: an adapter over libwire's Connection
 * (wire/connection.h), which owns the socket, the reader and sender threads
 * and the outbound queue. What lives here is what the frames mean to this
 * program -- HELLO, handing EXEC and SYNC* to the RPC worker in rpc.cpp,
 * posting the rest to the window -- and the worker's place in the stop
 * sequence.
 *
 * FOUR threads: UI, reader, sender, and the RPC worker.
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
#include "wire/link.h"

#define WM_RC_FRAME      (WM_APP + 1)   /* lParam: rc_frame*, UI thread frees */
#define WM_RC_STATE      (WM_APP + 2)   /* wParam: rc_state                   */

typedef struct {
    HWND     notify;
    char     host[64];
    int      port;
    char     secret[128];
    /* The transport: a ClientLink (net.cpp) holding a Connection, or NULL.
     * Opaque because C translation units include this header. Made by
     * rc_net_start, deleted by rc_net_stop -- unless a thread would not
     * stop, in which case it is left alive and `leaked` is set. */
    void    *conn;
    volatile LONG inflight;     /* frames posted but not yet freed by the UI  */
    volatile rc_state state;
    /* The last connection's byte totals, kept for after rc_net_stop; while
     * connected, rc_net_rx_bytes/tx_bytes read the live ones. */
    unsigned long rx_bytes;
    unsigned long tx_bytes;
    int           started;      /* rc_net_start ran; rc_net_stop has work */
    /* A PREVIOUS rc_net_stop GAVE UP. Set when a thread failed to join, which
     * is the case where the Connection deliberately leaks the socket, the
     * critical section and both events rather than free them under a live
     * thread. Those objects are then still in use, so rc_net_start refuses
     * rather than build a second link beside a thread from the first
     * (mem-audit F5). Sticky: once a thread from a dead connection is loose
     * in the process there is no way back to a known state without a
     * restart. */
    int           leaked;
    /* THE SESSION ID, and 64 is a limit this client cannot exceed rather
     * than a guess. app/chatlist.h's chat_ids[] is 64 too, deliberately
     * symmetric: the strcmp that decides "is this conversation already open"
     * compares the two, so a size mismatch would make them disagree about
     * ids longer than the smaller buffer. The bridge's ids are UUID-shaped
     * (36 bytes) and halt ids are 9, so nothing approaches this -- but the
     * guest CANNOT represent an id of 64 bytes or more at all, and that had
     * never been written down anywhere. If the bridge ever lengthens them,
     * both buffers move together. (audit 01.38) */
    char          sess[64];
} rc_net;

/* `tools` and `tools_len` are the machine's own inventory, carried as
 * HELLO's BODY rather than as headers -- the header array caps at 32 with
 * most of it spent, values cap at 256, and an inventory is neither a
 * capability enum nor twenty bytes. NULL and 0 are fine and mean "nothing
 * to say", which is what every client built before this sent. */
int  rc_net_start(rc_net *n, HWND notify, const char *host, int port,
                 const char *secret, const void *tools, unsigned tools_len);
void rc_net_stop(rc_net *n);
int  rc_net_send(rc_net *n, const char *type,
                 const char *const *keys, const char *const *vals, int nkv,
                 const void *body, unsigned body_len);

/* Same, with the short budget. For frames the user just asked for. */
int  rc_net_send_ui(rc_net *n, const char *type,
                 const char *const *keys, const char *const *vals, int nkv,
                 const void *body, unsigned body_len);

/* The UI thread's half of the frame-ownership contract above, spelled out
 * once instead of by hand at every WM_RC_FRAME site: rc_frame_free() releases
 * the frame's own allocations, HeapFree() releases the frame struct itself (a
 * SECOND, separate allocation made by the reader thread), and only then is
 * `inflight` decremented -- get the order wrong, or drop the decrement, and
 * the reader thread's backpressure gate wedges at RC_MAX_INFLIGHT with no
 * symptom but a stalled transcript.
 *
 * DEFINED IN net.cpp, beside the field it touches and the reader loop that
 * spins on it. It was a `static __inline` here for one pass, with a comment
 * saying it belonged in net.c and only lived in the header because net.c was
 * outside that pass's file set (01.24). That stopped being true, so it moved
 * (01.57), and nothing outside net.cpp writes `inflight` any more. */
void rc_net_frame_done(rc_net *n, rc_frame *f);

/* Read the in-flight count. Exists so a caller that wants to ASSERT on it --
 * netcheck.exe does, as the only end-to-end check of the ownership rule --
 * does not have to reach into the struct to do it.
 *
 * The field itself is still reachable: net.h is included by C translation
 * units (the check binaries), rc_net is a plain struct, and C has no way to
 * make one member private without making the whole type opaque and giving
 * every other field an accessor too. What CAN be true is true -- no code
 * outside net.cpp touches it -- and that is the property worth having, since
 * the bug this guards against is a miscounted decrement rather than a read. */
long rc_net_inflight(const rc_net *n);

/* Bytes received and sent on this connection so far, or on the last one
 * once it has stopped. */
unsigned long rc_net_rx_bytes(const rc_net *n);
unsigned long rc_net_tx_bytes(const rc_net *n);

/* Snapshot of the outbound queue's current byte total, taken under the
 * connection's lock; 0 with no connection. Exists for the same reason
 * rc_net_inflight does: netcheck.exe's slow-reader scenario is the only
 * thing that asserts the queue stays bounded under backpressure. */
unsigned rc_net_outq_bytes(rc_net *n);

/* Instrumentation for STAT frame: bytes compressed and time spent in mz_deflate. */
void rc_net_get_deflate_sample(rc_net *n, unsigned long *bytes_in, unsigned long *us);
void rc_net_set_link_bps(rc_net *n, unsigned long bps);

#ifdef __cplusplus
}
#endif

#endif
