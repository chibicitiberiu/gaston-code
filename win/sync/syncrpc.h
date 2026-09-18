/* Handling SYNC* frames on the RPC worker thread.
 *
 * 03.52: this file and sync.cpp both used to define functions under the bare
 * `rc_sync_` prefix -- unlike every other module, which gets its own segment
 * (rc_net_, rc_rpc_, rc_html_, rc_exec_, rc_frame_) -- so "which file is
 * rc_sync_X in" was not answerable from the name alone. sync.h/sync.cpp keep
 * `rc_sync_` (the larger file: scanning, checksums, the manifest row format,
 * the module's actual name); the three functions declared here, which are
 * specifically this file's RPC-thread glue, moved to `rc_syncrpc_`, matching
 * this header's own name and file name.
 */
#ifndef RC_SYNCRPC_H
#define RC_SYNCRPC_H


#ifdef __cplusplus
extern "C" {
#endif
#include "net/net.h"
#include "net/frameproto.h"

/* Handle one SYNC frame. Called on the worker; the frame is still owned by the
 * caller, which frees it afterwards. */
void rc_syncrpc_handle(rc_net *net, rc_frame *f);

/* Flag an in-flight SYNCPUT as abandoned. Call this on disconnect -- see the
 * comment at the call site (app/main.cpp, WM_RC_STATE) for why. Only sets a
 * flag: the RPC worker thread, which is the only thing that ever touches the
 * transfer's open file handle, does the actual cleanup the next time a PUT
 * arrives. Without this, a transfer cut off by a dropped link wedges every
 * PUT after the reconnect as "busy", forever. Safe to call from any thread,
 * any number of times, whether or not a transfer is in flight. */
void rc_syncrpc_abort(void);

/* The configured sync root, or "" when sync is off. Set from the INI. */
void        rc_syncrpc_set_root(const char *root);
const char *rc_syncrpc_root(void);

#ifdef __cplusplus
}
#endif

#endif
