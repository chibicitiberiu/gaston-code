/* PSLIST and PSKILL, answered on the net reader thread (the IMMEDIATE lane,
 * net/frameproto.h): a Toolhelp snapshot and a kill that waits at most
 * 250ms. Each sends its reply itself; the caller frees `f`. */
#ifndef RC_RPC_PROCRPC_H
#define RC_RPC_PROCRPC_H

#include "net/net.h"
#include "wire/frame.h"

void rc_procrpc_list(rc_net *net, const rc_frame *f);
void rc_procrpc_kill(rc_net *net, const rc_frame *f);

#endif
