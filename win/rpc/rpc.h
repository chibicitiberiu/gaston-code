/* The RPC worker thread: runs EXEC frames off the socket without blocking
 * either the UI thread or the reader.
 *
 * The split is mandatory, not tidiness. A command can run for a minute: on the
 * UI thread that freezes the window, and on the reader thread it stops display
 * frames being read, so the transcript stalls while the command runs.
 */
#ifndef RC_RPC_H
#define RC_RPC_H


#ifdef __cplusplus
extern "C" {
#endif
#include "net/frameproto.h"
#include "net/net.h"

/* Start the worker. Safe to call more than once. */
int  rc_rpc_start(rc_net *net);

/* Offer a freshly parsed frame to the RPC layer.
 *
 * THE ROUTING VOCABULARY LIVES HERE, not in the networking layer. net.c used
 * to hard-code EXEC, EXECKILL and the SYNC prefix in its reader loop, which
 * made a real net<->rpc cycle -- the transport knowing the names of the tools
 * it carries -- and meant every tool Phase 6 adds would put another branch in
 * the socket code. There are eight such tools planned.
 *
 * Returns RC_ROUTE_TAKEN when the frame has been consumed AND FREED by this
 * layer, so the caller must not touch it again; RC_ROUTE_PASS when it is not
 * ours and belongs to the UI thread.
 *
 * Called on the READER thread. Anything slow must be queued rather than done
 * here, with one deliberate exception documented at the implementation. */
#define RC_ROUTE_PASS   0
#define RC_ROUTE_TAKEN  1
int  rc_rpc_route(rc_frame *f, volatile LONG *running);

/* Hand a parsed EXEC frame to the worker. OWNERSHIP TRANSFERS: the worker
 * frees the frame. Returns 0 if queued. */
int  rc_rpc_submit(rc_frame *f);
/* True when the sync FIFO cannot take another frame; the reader waits. */
int  rc_rpc_syncq_full(void);

/* Ask the worker to stop and kill any child it is running. Returns at once. */
void rc_rpc_stop(void);

/* Wait for the worker to exit. Non-zero if it actually did. */
int  rc_rpc_join(DWORD timeout_ms);

#ifdef __cplusplus
}
#endif

#endif
