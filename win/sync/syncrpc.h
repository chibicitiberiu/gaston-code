/* Handling SYNC* frames on the RPC worker thread. */
#ifndef RC_SYNCRPC_H
#define RC_SYNCRPC_H


#ifdef __cplusplus
extern "C" {
#endif
#include "net/net.h"
#include "net/frameproto.h"

/* Handle one SYNC frame. Called on the worker; the frame is still owned by the
 * caller, which frees it afterwards. */
void rc_sync_handle(rc_net *net, rc_frame *f);

/* The configured sync root, or "" when sync is off. Set from the INI. */
void        rc_sync_set_root(const char *root);
const char *rc_sync_root(void);

#ifdef __cplusplus
}
#endif

#endif
