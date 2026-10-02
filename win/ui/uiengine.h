/* uiengine.h -- the desktop handlers both programs run.
 *
 * The analogue of `fs/ops.h`: behaviour that used to live in one exe's RPC
 * file, lifted out so a second exe can run it rather than copy it. Each
 * caller supplies a `UiLink` (ui/uilink.h) and keeps its own op names, its
 * own frame types and its own id.
 *
 * Everything here runs on the caller's desktop worker -- a thread with a
 * message queue, an STA, and a pump. See `UiContext` for why that is a
 * contract and not an arrangement.
 */
#ifndef RC_UI_UIENGINE_H
#define RC_UI_UIENGINE_H

#include "wire/frame.h"
#include "automation/steps.h"

class UiLink;
class UiContext;

/* How many steps one list may carry. Bounded by what the bridge may send
 * (bridge/agent/steps.py's MAX_LINES) inside the frame's own 64KB. */
#define RC_RUN_MAX 64

/* Decode a step list IN PLACE. `body` is the frame's own body and is
 * modified: percent-decoding only ever shrinks, so each Step's strings point
 * straight into it and the body must outlive the Step array. Returns the
 * count, or -1 with a reason in `why`.
 *
 * Exposed for uicheck, which is the only way the sixteen verbs and the
 * escaping are reachable without a socket and a desktop. */
int rc_ui_decode_steps(char *body, unsigned len, Step *out, char *why,
                       unsigned whyCap);

/* UIRUN / RUN: a list of steps, run in order, stopping at the first
 * failure. The body is the list; `link` gets one report and one terminator.
 * Refuses `automation_off` below RC_AUTOMATION_STEPS, because a step list
 * types and the middle capability value says not to. */
void rc_ui_run(UiLink &link, UiContext *ctx, rc_frame *f);

#endif
