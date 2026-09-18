/* Transfer progress: the byte counters shown in the status bar's middle pane
 * while a request or a page load is actually moving, and cleared once it
 * goes quiet.
 *
 * The counters used to sit there ALL the time as a running total, which told
 * you nothing: the interesting question is whether something is moving
 * RIGHT NOW and how much of it is left. Two shapes, because there are two
 * kinds of traffic. A page load arrives in several chunks and has a known
 * size, so it shows as done/total. Streamed text arrives as many small
 * frames with no total worth speaking of, so it shows as a running count for
 * the burst.
 *
 * One of the three remaining §1.2 extraction targets out of main.c (audit
 * finding 01.58) -- the bar and the chat list are already their own files;
 * this is the smallest of what was left.
 */
#ifndef RC_XFERSTAT_H
#define RC_XFERSTAT_H

#include <windows.h>
#include "net/frameproto.h"

#ifdef __cplusplus
extern "C" {
#endif

/* render_frame's own progress-accounting prelude, called first, before
 * anything else looks at the frame -- see render_frame's comment on why.
 * Parses total/off itself and updates the rx side of the counters this
 * module now owns. */
void rc_xfer_note_rx(rc_frame *f);

/* do_send's side of the same counters: nothing has been received yet when a
 * message is sent, so this is not part of render_frame's prelude, but it is
 * the same four counters -- do_send needs a way to record its own side
 * without reaching into globals this module now owns. start: about to send
 * `n` bytes. done: confirms `n` actually went (rc_net_send returned 0). */
void rc_xfer_note_tx_start(unsigned long n);
void rc_xfer_note_tx_done(unsigned long n);

/* status_refresh's middle pane (pane 1): the rx/tx text while something has
 * moved in the last 1200ms, or an empty string once that has passed -- and
 * the reset of all four counters that goes with the empty-string case. out/
 * cap exactly like rc_strf's other callers. */
void rc_xfer_text(char *out, unsigned cap);

#ifdef __cplusplus
}
#endif

#endif
