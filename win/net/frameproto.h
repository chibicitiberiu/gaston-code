/* The client's protocol on top of libwire's frame codec (wire/frame.h):
 * its version and which thread handles each frame type. Mirror of
 * bridge/wire/protocol.py -- keep in sync.
 */
#ifndef RC_FRAMEPROTO_H
#define RC_FRAMEPROTO_H

#include "wire/frame.h"

#ifdef __cplusplus
extern "C" {
#endif
/* 3 since stage 3b: GETBLOCKS/RANGE/RANGEEND/GREW replaced the four page
 * frames and TEXT's `total` changed meaning. A bridge that speaks 2 refuses
 * this client at the handshake, which is the outcome worth having -- the
 * alternative is a transcript that appends and will not page, with one
 * `unknown_type` in a log nobody is reading. */
#define RC_PROTO_VERSION 3

/* THE FOUR LANES.
 *
 * A frame's TYPE decides which of four disjoint paths handles it, and that
 * decision is a TABLE, not a branch, so a Phase 6 tool costs a row here and
 * not another `if` in rpc.c's worker.
 *
 *   UI        -- not the RPC layer's. rc_rpc_route passes it back to net.c,
 *                which posts WM_RC_FRAME; the window thread does the rest.
 *   JOB       -- the worker's ONE job slot. Can run for up to two minutes
 *                (rpc.c's EXEC timeout), so a second one arriving while it
 *                runs must be ANSWERED rather than queued -- see rpc.c's
 *                reject_busy.
 *   STREAM    -- the worker's bounded FIFO. A chunk in a transfer, never a
 *                job in its own right: it is milliseconds of file I/O and
 *                must never wait behind a JOB, or a two-minute EXEC stalls
 *                every SYNCPUT chunk behind it (rpc.c's rpc_thread drains
 *                this FIFO before ever looking at the JOB slot).
 *   IMMEDIATE -- answered on the READER thread, before either worker queue
 *                is even touched. Never refused for a JOB or STREAM being
 *                busy, because it never asks them.
 *
 *                THE BAR FOR THIS LANE IS THE READER THREAD'S RULE, not
 *                "is it quick": net.h says, marked not negotiable, that the
 *                reader NEVER calls a window API that can block or re-enter.
 *                So a member must not block and must not re-enter -- which
 *                rules out anything that touches ANOTHER PROCESS'S message
 *                queue, because a cross-process SendMessage blocks until the
 *                target's own message loop answers, and a hung application
 *                never answers. Blocking here stops the socket being read at
 *                all: the transcript freezes, no PERMEND arrives, no SYNCDAT
 *                moves, and the bridge times out every outstanding future
 *                with nothing in the guest's log to say why.
 *
 *                This comment used to name Phase 6's list_windows as a
 *                natural second member. It is not one: PLAN.md requires
 *                WM_GETTEXT per node, which is exactly that cross-process
 *                SendMessage. list_windows belongs in JOB, or in a lane of
 *                its own. BACKLOG.md's note that EnumWindows and
 *                WM_GETTEXT are "safe from the RPC worker thread" is true
 *                and is about a DIFFERENT thread from this one. (audit 01.36)
 *
 *                EXECKILL is the only member today and genuinely qualifies:
 *                it sets a flag and calls TerminateProcess.
 *
 * Add a Phase 6 tool by adding a row to RC_LANE_TABLE (frameproto.c), not by
 * adding a branch to rc_rpc_route or rc_rpc_submit. */
typedef enum {
    RC_LANE_UI = 0,
    RC_LANE_JOB,
    RC_LANE_STREAM,
    RC_LANE_IMMEDIATE,
    /* The DESKTOP, not our own window -- RC_LANE_UI above is the client's
     * message loop and this is the worker that drives other applications.
     * They are deliberately not the same thread: a tree walk sends
     * WM_GETTEXT, a same-thread SendMessage is a direct call, and reaching
     * our own in-place-active HTML control from inside an RPC handler is
     * reentrancy. A wait is seconds long by design, and RC_LANE_UI is the
     * thread that repaints. See docs/UIAUTOMATION.md. */
    RC_LANE_DESKTOP
} rc_lane;

typedef struct {
    const char *type;
    rc_lane     lane;
} rc_lane_row;

/* Frame types not listed here, and not matching the SYNC* wildcard rule in
 * frameproto.c, default to RC_LANE_UI -- the safe choice, since render_frame
 * already has an "unhandled frame type" branch for anything it does not
 * recognise, where a stray JOB or STREAM row would silently eat a frame the
 * window was supposed to see. */
extern const rc_lane_row RC_LANE_TABLE[];
extern const int         RC_LANE_TABLE_N;

/* Looks a frame type up in RC_LANE_TABLE, then falls back to the one
 * wildcard rule (SYNC* is always RC_LANE_STREAM -- see frameproto.c) before
 * defaulting to RC_LANE_UI. */
rc_lane rc_frame_lane(const char *type);

/* Worth deflating: the client's bulk frame types (EXECOUT, SYNCDAT,
 * SYNCENT). Protocol vocabulary, so it lives beside the lane table rather
 * than in the transport. */
int rc_frame_compressible(const char *type);

#ifdef __cplusplus
}
#endif

#endif
