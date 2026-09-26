/* uirpc.h -- the desktop lane's request handlers.
 *
 * The analogue of `sync/syncrpc.*`: one entry point that takes a frame off
 * the lane and answers it. Everything here runs on the UiService's thread,
 * which is NOT the client's window thread -- see `rc_lane`'s RC_LANE_DESKTOP
 * for why that separation is load-bearing rather than tidy.
 *
 * The exe's job and not libautomation's, per the rule in win/Makefile:
 * turning a tree into frames is the exe's business, and libautomation knows
 * nothing of the wire.
 */
#ifndef RC_UI_UIRPC_H
#define RC_UI_UIRPC_H

#include "net/net.h"
#include "wire/frame.h"

class UiContext;

/* Hand a request to the desktop worker. Returns 0 if it was queued.
 *
 * Non-zero means the worker is not running or its queue is full, and the
 * CALLER then answers `UIEND status=busy` and frees the frame -- silence
 * would make the bridge wait out its whole timeout for an answer that was
 * never coming. */
int rc_uirpc_post(rc_frame *f);

/* Called by UiService as its thread starts and stops. The thread id lives in
 * this module because `rc_rpc_route` is C and has no object to reach
 * through; it is written once at start and once at stop, both from the
 * owning thread, and read by the reader thread through an interlocked
 * load. */
void rc_uirpc_attach(DWORD threadId, rc_net *net, UiContext *ctx);
void rc_uirpc_detach(void);

/* WM_APP rather than WM_USER: a WM_USER value belongs to a window class, and
 * this queue has no window at all -- these are posted to a THREAD.
 *
 * Its own block at +0x300, clear of net.h's +1..2, the client's low offsets,
 * core/'s +0x100 and the checks' +0x200. tools/check/msgids.py audits this
 * and caught the first version of these two sitting exactly on top of
 * WM_RC_FRAME and WM_RC_STATE. */
#define RC_WM_UI_REQUEST (WM_APP + 0x300)
#define RC_WM_UI_QUIT    (WM_APP + 0x301)

/* Dispatch one request. `ctx` carries the walker, MSAA, the event feed and
 * the deadline, all of which live for the length of the thread. */
void rc_uirpc_handle(rc_net *net, UiContext *ctx, rc_frame *f);

/* Answer a request we are not going to perform. Every request ends in
 * exactly one UIEND whatever happens: silence costs the bridge its whole
 * timeout and tells it nothing. */
void rc_uirpc_refuse(rc_net *net, rc_frame *f, const char *why);

#endif
