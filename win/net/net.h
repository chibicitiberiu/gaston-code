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
    /* How many records this client puts on a page, declared in HELLO and
     * kept so the page bar can ask for exactly that many without redoing
     * the arithmetic. Computed from this machine's memory and the user's
     * Small/Normal/Large setting; 0 until rc_net_start has run. */
    unsigned      page_blocks;
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
    /* WHICH CONVERSATION is on screen -- READY's or SWITCHED's id, never
     * the per-connection handle above. Sent back in HELLO as `conv`, so a
     * bridge that RESTARTED reopens it instead of starting a new, empty
     * one with nothing said (measured on the Athlon, 2026-10-03). Same
     * 64 as `sess`, for the same reason. */
    char          conv[64];
    /* WHICH MACHINE THIS IS -- HELLO's `machine` header, minted once and
     * kept beside the exe (app/machineid.h). Empty means "not reported",
     * and the bridge then files conversations exactly as it did before this
     * header existed. 40 and not 64: the id is 36 characters by
     * construction, and anything that is not that shape is refused before
     * it reaches here. */
    char          machine[40];
    /* CAN THIS CLIENT BE DRIVEN -- HELLO's `automation` header, one of
     * caps.h's RC_AUTOMATION_* values, from `[ui] automation` in the INI.
     *
     * Zero is RC_AUTOMATION_NONE, so a client that never sets it (the check
     * harnesses, which have no screen) declares the restrictive thing without
     * a line of code. Read in two places: rc_net_start puts it on the wire so
     * the bridge knows whether to offer the five screen tools at all, and
     * rc_uirpc_handle refuses every screen op when it is NONE. The header is
     * the optimisation; that refusal is the control, and it reads this field
     * live so turning the capability off never waits for a reconnect.
     * (BACKLOG.md 19.) */
    int           automation;
    /* THE CLIENT'S SETTINGS, sent as the PREFS frame straight after HELLO
     * (bridge/prefs.py). Not owned: the caller keeps the buffer alive for
     * as long as the link may start, and rc_net_set_prefs only points at
     * it. NULL means this client declares no settings and the bridge is not
     * told to wait for any. */
    const char   *prefs;
    unsigned      prefs_len;
} rc_net;

/* Tell the link which machine it is. Call it BEFORE rc_net_start; the value
 * is copied, and one too long to fit is dropped with a log line rather than
 * truncated into a second machine's identity. */
void rc_net_set_machine(rc_net *n, const char *id);

/* Tell the link whether this client may be driven -- an RC_AUTOMATION_* from
 * net/caps.h. Call it BEFORE rc_net_start for the handshake to carry it;
 * calling it later still takes effect for the guest-side refusal, which is
 * how "off" can be immediate while "on" waits for the next HELLO. */
void rc_net_set_automation(rc_net *n, int mode);

/* The PREFS body to send after each HELLO, `key=value` lines in the ANSI
 * codepage. The buffer is NOT copied -- see `prefs` above. A body over the
 * bridge's 16KB cap is refused here with a log line and nothing is
 * declared, rather than sent to be refused at the handshake. */
void rc_net_set_prefs(rc_net *n, const char *body, unsigned len);

/* `tools` and `tools_len` are the machine's own inventory, carried as
 * HELLO's BODY rather than as headers -- the header array caps at 32 with
 * most of it spent, values cap at 256, and an inventory is neither a
 * capability enum nor twenty bytes. NULL and 0 are fine and mean "nothing
 * to say", which is what every client built before this sent. */
int  rc_net_start(rc_net *n, HWND notify, const char *host, int port,
                 const char *secret, const void *tools, unsigned tools_len,
                 int page_size);

/* How many records fit on one page of THIS machine, from its memory and the
 * user's RC_PAGE_* setting.
 *
 * It lives here rather than in app/ because the memory figure is read here,
 * and because stage 3b moved this arithmetic across the wire: the bridge
 * computed it from HELLO's mem_kb and scaled it by a PREFS key, which made
 * how much the CLIENT can hold a thing the client had to be told. Two
 * callers: the HELLO header, and the page bar's `limit`.
 */
unsigned rc_page_blocks(unsigned long mem_kb, int page_size);

/* What was declared at the last handshake, or the default before one. */
unsigned rc_net_page_blocks(rc_net *n);
/* 1 if everything stopped and was freed (or there was nothing to stop); 0
 * if a thread would not stop, its Connection was left alive and `leaked` is
 * now set. */
int  rc_net_stop(rc_net *n);
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
/* Bytes and microseconds spent receiving large frames since the last call. */
void rc_net_get_link_sample(rc_net *n, unsigned long *bytes, unsigned long *us);
void rc_net_set_link_bps(rc_net *n, unsigned long bps);

#ifdef __cplusplus
}
#endif

#endif
