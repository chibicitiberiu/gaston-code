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
