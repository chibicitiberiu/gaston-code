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
 *
 * A CLASS NOW, not a handle plus three free functions (audit finding 01.60):
 * same reasoning as app/htmlview.h, the pattern this module and its five
 * siblings follow. There is exactly one instance, created explicitly by
 * main.cpp at window-creation time (never a file-scope object -- see
 * core/rc_cpp.h) and destroyed with `delete`.
 */
#ifndef RC_XFERSTAT_H
#define RC_XFERSTAT_H

#include <windows.h>
#include "net/frameproto.h"

#ifndef __cplusplus
#error "xferstat.h is C++; the counters are a class now, see app/htmlview.h"
#endif

class rc_xferstat {
    unsigned long rx_done, rx_total;
    unsigned long tx_done, tx_total;
    DWORD         xfer_ms;
    /* One previous sample, so text() can report a RATE.
     *
     * Most traffic has no denominator and never will: nobody knows how many
     * bytes a model's reply is until it ends, so "rx 12k" could only ever be
     * a running total. A rate answers the question the total was standing in
     * for -- is this moving, and how fast -- and it is the thing that makes
     * a stall visible, because it drops to zero while the total just stops.
     *
     * Sampled in text(), which the 1s status timer drives, so this is a 1Hz
     * measurement and is labelled as bytes per second accordingly. */
    unsigned long rate_last_rx;
    DWORD         rate_last_ms;
    unsigned long rate_bps;

    rc_xferstat(const rc_xferstat &);
    rc_xferstat &operator=(const rc_xferstat &);
    rc_xferstat() : rx_done(0), rx_total(0), tx_done(0), tx_total(0),
                    xfer_ms(0), rate_last_rx(0), rate_last_ms(0),
                    rate_bps(0) {}

public:
    static rc_xferstat *create();
    ~rc_xferstat() {}

    /* render_frame's own progress-accounting prelude, called first, before
     * anything else looks at the frame -- see render_frame's comment on why.
     * Parses total/off itself and updates the rx side of the counters this
     * class now owns. */
    void note_rx(rc_frame *f);

    /* do_send's side of the same counters: nothing has been received yet
     * when a message is sent, so this is not part of render_frame's prelude,
     * but it is the same four counters -- do_send needs a way to record its
     * own side without reaching into members this class now keeps private.
     * start: about to send `n` bytes. done: confirms `n` actually went
     * (rc_net_send returned 0). */
    void note_tx_start(unsigned long n);
    void note_tx_done(unsigned long n);

    /* status_refresh's middle pane (pane 1): the rx/tx text while something
     * has moved in the last 1200ms, or an empty string once that has passed
     * -- and the reset of all four counters that goes with the empty-string
     * case. out/cap exactly like rc_strf's other callers. */
    void text(char *out, unsigned cap);
};

#endif
