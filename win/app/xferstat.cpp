/* Transfer progress. See app/xferstat.h for the design. */
#include "app/xferstat.h"
#include "core/str.h"
#include "core/rc_cpp.h"
#include <stdlib.h>

/* Transfer progress. The byte counters used to sit in the status bar all the
 * time as a running total, which told you nothing: the interesting question is
 * whether something is moving RIGHT NOW and how much of it is left. These are
 * shown while a transfer is in flight and cleared when it goes quiet.
 *
 * Two shapes, because there are two kinds of traffic. A page load arrives in
 * several chunks and has a known size, so it shows as done/total. Streamed
 * text arrives as many small frames with no total worth speaking of, so it
 * shows as a running count for the burst. */

rc_xferstat *rc_xferstat::create()
{
    return new rc_xferstat();
}

/* Bytes, readably. Under 10K the exact figure is the interesting one -- "rx
 * 412/900" says something that "rx 0k/0k" does not. */
static void fmt_bytes(char *out, unsigned cap, unsigned long n)
{
    if (n < 10240) rc_strf(out, cap, "%lu", n);
    else           rc_strf(out, cap, "%luk", n / 1024);
    out[cap - 1] = '\0';
}

void rc_xferstat::note_rx(rc_frame *f)
{
    /* Progress accounting, before anything else looks at the frame. A
     * chunked transfer carries total/off in decompressed bytes; anything
     * else just counts towards "something is arriving". */
    const char *tot = rc_frame_get(f, "total");
    const char *off = rc_frame_get(f, "off");
    unsigned long total = tot ? (unsigned long)atol(tot) : 0;
    xfer_ms = GetTickCount();
    if (tot && off && total > f->body_len) {
        /* Genuinely chunked, so there is a meaningful denominator. */
        rx_total = total;
        rx_done = (unsigned long)atol(off) + f->body_len;
        if (rx_done > rx_total) rx_done = rx_total;
    } else {
        /* One frame on its own. Counting these up over the burst is more
         * use than showing each frame's own size as a fraction of itself. */
        rx_total = 0;
        rx_done += f->body_len;
    }
}

void rc_xferstat::note_tx_start(unsigned long n)
{
    tx_total = n;
    tx_done = 0;
    xfer_ms = GetTickCount();
}

void rc_xferstat::note_tx_done(unsigned long n)
{
    tx_done = n;
}

void rc_xferstat::text(char *out, unsigned cap)
{
    DWORD now = GetTickCount();

    /* Sample the rate before deciding whether to draw anything, so the
     * figure is over a real interval rather than over however long it has
     * been since the pane last happened to be visible. */
    if (rate_last_ms && now > rate_last_ms) {
        DWORD span = now - rate_last_ms;
        if (span >= 500) {
            unsigned long moved = rx_done > rate_last_rx
                                ? rx_done - rate_last_rx : 0;
            rate_bps = (unsigned long)((moved * 1000UL) / span);
            rate_last_rx = rx_done;
            rate_last_ms = now;
        }
    } else {
        rate_last_rx = rx_done;
        rate_last_ms = now;
    }

    /* Only while something is actually moving. A transfer is "over" when
     * nothing has arrived for a moment, which is also what makes a stalled
     * one visible: the numbers stop but the pane does not clear instantly.
     *
     * 2500ms rather than 1200. At 1200 the pane blinked out between frames
     * during an ordinary thinking pause and came back for the next burst, so
     * it read as flicker rather than as an indicator -- measured on the
     * guest. Note this branch is also the RESET, so the constant decides how
     * long a burst accumulates before "rx 12k" starts again from zero. */
    if (now - xfer_ms < 2500) {
        char rx[64], tx[64], a[16], b[16], r[24];
        rx[0] = tx[0] = r[0] = '\0';
        if (rate_bps) {
            char rb[16];
            /* fmt_bytes, not a hand-rolled divide: it already renders under
             * 10K exactly and above it in k, and two formatters disagreeing
             * about where that boundary is would be a pointless way to make
             * the pane inconsistent with itself. */
            fmt_bytes(rb, sizeof(rb), rate_bps);
            rc_strf(r, sizeof(r), " @ %s/s", rb);
        }
        if (rx_total) {
            fmt_bytes(a, sizeof(a), rx_done);
            fmt_bytes(b, sizeof(b), rx_total);
            rc_strf(rx, sizeof(rx), "rx %s/%s", a, b);
        } else if (rx_done) {
            /* No denominator exists for streamed text -- nobody knows how
             * long a reply is until it ends -- so the rate stands in for it. */
            fmt_bytes(a, sizeof(a), rx_done);
            rc_strf(rx, sizeof(rx), "rx %s%s", a, r);
        }
        if (tx_total) {
            fmt_bytes(a, sizeof(a), tx_done);
            fmt_bytes(b, sizeof(b), tx_total);
            rc_strf(tx, sizeof(tx), "%stx %s/%s", rx[0] ? "  " : "", a, b);
        } else if (tx_done) {
            fmt_bytes(a, sizeof(a), tx_done);
            rc_strf(tx, sizeof(tx), "%stx %s", rx[0] ? "  " : "", a);
        }
        rc_strf(out, cap, " %s%s", rx, tx);
    } else {
        rx_done = rx_total = tx_done = tx_total = 0;
        rate_bps = 0;
        rate_last_rx = 0;
        rc_strcpy(out, cap, "");
    }
}
