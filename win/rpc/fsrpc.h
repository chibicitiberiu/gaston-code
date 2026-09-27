/* The fs lane's frame handlers: FSGET, FSPUT, FSPUTEND, FSDRIVES.
 *
 * Separate from syncrpc.cpp on purpose, and not only for size. The sync layer
 * moves files inside ONE root that the person chose and that a recovery pass
 * sweeps at startup; these move files anywhere on the machine, with no root
 * and no sweep, and the two want different answers to the same questions --
 * most sharply to "what happens if the rename fails". Keeping them apart
 * keeps that difference visible instead of hiding it behind a shared flag.
 *
 * Also: test_guest_agreement.py scrapes win/sync/syncrpc.cpp for the status
 * vocabulary and asserts set equality in both directions. Every token used
 * here is one that file already produces. A new one has to be added there
 * too, deliberately, rather than arriving by accident from this side.
 */
#ifndef RC_FSRPC_H
#define RC_FSRPC_H

#include "net/net.h"
#include "net/frameproto.h"

#ifdef __cplusplus
extern "C" {
#endif

/* Non-zero if `f` was one of ours. Runs on the RPC worker, like the sync
 * handlers, because it blocks on the disk. */
int rc_fsrpc_handle(rc_net *net, rc_frame *f);

/* A transfer in flight when the connection drops. Called on the UI thread, so
 * it sets a flag and the worker does the cleanup on its next frame -- the
 * same shape, and for the same reason, as rc_syncrpc_abort. */
void rc_fsrpc_abort(void);

#ifdef __cplusplus
}
#endif

#endif
