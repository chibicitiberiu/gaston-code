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

/* The configured sync root, pushed once from main.c's startup -- the same
 * moment it also calls rc_syncrpc_set_root (app/main.cpp, right next to it)
 * -- rather than this module reading it back through syncrpc.c's own
 * accessor. Audit finding 01.25: rpc_watch_start/rpc_maybe_nudge used to call
 * rc_syncrpc_root() to decide whether to start a directory watcher, which is
 * this module reaching into a different module's state for what is really
 * its own configuration. Call before the first rc_rpc_start() -- the watcher
 * is set up once, on the first call, not on every reconnect. */
void rc_rpc_set_sync_root(const char *root);

/* Offer a freshly parsed frame to the RPC layer.
 *
 * THE ROUTING VOCABULARY LIVES HERE, not in the networking layer. net.c used
 * to hard-code EXEC, EXECKILL and the SYNC prefix in its reader loop, which
 * made a real net<->rpc cycle -- the transport knowing the names of the tools
 * it carries -- and meant every tool Phase 6 adds would put another branch in
 * the socket code. There are eight such tools planned.
 *
 * Which of the four LANES a frame belongs to -- UI, JOB, STREAM or IMMEDIATE
 * -- is a table in frameproto.h, not a branch here either. See its comment
 * for what each lane means; rc_rpc_route below only switches on the answer.
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
 * frees the frame in every case that returns 0 -- queued, rejected as a
 * family mismatch, or answered `busy` and dropped are all "the worker took
 * ownership," which is the only question net.c's caller actually needs
 * answered. Returns -1, leaving the frame for the CALLER to free, only when
 * the worker was never started or has already been told to stop. */
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
