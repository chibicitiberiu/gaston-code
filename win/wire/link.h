/* What a libwire link can say about itself: its state, and what a send can
 * answer. C-compatible, because the C check programs read both through
 * net/net.h; the Connection that produces them is C++ (wire/connection.h).
 */
#ifndef RC_WIRE_LINK_H
#define RC_WIRE_LINK_H

#ifdef __cplusplus
extern "C" {
#endif

typedef enum {
    RC_DISCONNECTED = 0,
    RC_CONNECTING,
    RC_CONNECTED,
    RC_FAILED
} rc_state;

const char *rc_state_name(rc_state s);

/* Bounded in BYTES, not items: sixteen 64KB frames would be a megabyte.
 *
 * It is a HARD cap: a send waits up to its budget for the sender to drain
 * room, and if the queue is still full after that it fails the send rather
 * than enqueueing past the limit. It used to be soft -- a writer that waited
 * out its budget enqueued anyway, reasoning that losing a frame on a stream
 * with no resync is worse than overshooting by one. Measured against a peer
 * that genuinely stops reading (tests/python/test_guest_net.py's slow-reader
 * scenario), that "by one" was false: each blocked caller enqueued past the
 * cap in turn, without bound, on a machine with 28KB free. A failed send is
 * a return value every caller already has to handle for other reasons (OOM,
 * oversized frame, a dead link); a queue that grows without bound under
 * backpressure is not. */
#define RC_OUTQ_MAX_BYTES (64 * 1024)

/* What a send can answer.
 *
 * RC_SEND_FULL is NOT a dead link and must not be reported as one: the wire
 * is fine and a bulk transfer is simply ahead of this frame in the queue.
 * Telling the user "the connection is down" for it sent them to look at the
 * network for a backpressure event and promised a reconnect that was never
 * coming (audit 01.17). Every caller may still test `!= 0` / `< 0`; only a
 * caller that wants to SAY something needs to tell the two apart. */
#define RC_SEND_OK    0
#define RC_SEND_DEAD (-1)
#define RC_SEND_FULL (-2)

/* 100ms slices spent waiting for the queue to drain. Bulk senders (the RPC
 * worker: manifest rows, SYNCDAT, EXECOUT) can afford the full five seconds,
 * because waiting for the wire is what backpressure is for. A USER GESTURE
 * cannot: it runs on the window thread and the message pump does not run
 * while it waits, so five seconds there is a frozen window and a ghost
 * frame. */
#define RC_SEND_WAIT_BULK 50
#define RC_SEND_WAIT_UI    5

#ifdef __cplusplus
}
#endif

#endif
