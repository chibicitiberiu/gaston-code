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

/* The configured sync root, or "" when sync is off. Set from the INI. */
void        rc_syncrpc_set_root(const char *root);
const char *rc_syncrpc_root(void);

#ifdef __cplusplus
}
#endif

#endif
