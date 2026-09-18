/* Transfer progress. See app/xferstat.h for the design. */
#include "app/xferstat.h"
#include "core/str.h"
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
static unsigned long g_rx_done, g_rx_total;
static unsigned long g_tx_done, g_tx_total;
static DWORD         g_xfer_ms;

/* Bytes, readably. Under 10K the exact figure is the interesting one -- "rx
 * 412/900" says something that "rx 0k/0k" does not. */
static void fmt_bytes(char *out, unsigned cap, unsigned long n)
{
    if (n < 10240) rc_strf(out, cap, "%lu", n);
    else           rc_strf(out, cap, "%luk", n / 1024);
    out[cap - 1] = '\0';
}

void rc_xfer_note_rx(rc_frame *f)
{
    /* Progress accounting, before anything else looks at the frame. A
     * chunked transfer carries total/off in decompressed bytes; anything
     * else just counts towards "something is arriving". */
    const char *tot = rc_frame_get(f, "total");
    const char *off = rc_frame_get(f, "off");
    unsigned long total = tot ? (unsigned long)atol(tot) : 0;
    g_xfer_ms = GetTickCount();
    if (tot && off && total > f->body_len) {
        /* Genuinely chunked, so there is a meaningful denominator. */
        g_rx_total = total;
        g_rx_done = (unsigned long)atol(off) + f->body_len;
        if (g_rx_done > g_rx_total) g_rx_done = g_rx_total;
    } else {
        /* One frame on its own. Counting these up over the burst is more
         * use than showing each frame's own size as a fraction of itself. */
        g_rx_total = 0;
        g_rx_done += f->body_len;
    }
}

void rc_xfer_note_tx_start(unsigned long n)
{
    g_tx_total = n;
    g_tx_done = 0;
    g_xfer_ms = GetTickCount();
}

void rc_xfer_note_tx_done(unsigned long n)
{
    g_tx_done = n;
}

void rc_xfer_text(char *out, unsigned cap)
{
    /* Only while something is actually moving. A transfer is "over" when
     * nothing has arrived for a moment, which is also what makes a stalled
     * one visible: the numbers stop but the pane does not clear instantly. */
    if (GetTickCount() - g_xfer_ms < 1200) {
        char rx[64], tx[64], a[16], b[16];
        rx[0] = tx[0] = '\0';
        if (g_rx_total) {
            fmt_bytes(a, sizeof(a), g_rx_done);
            fmt_bytes(b, sizeof(b), g_rx_total);
            rc_strf(rx, sizeof(rx), "rx %s/%s", a, b);
        } else if (g_rx_done) {
            fmt_bytes(a, sizeof(a), g_rx_done);
            rc_strf(rx, sizeof(rx), "rx %s", a);
        }
        if (g_tx_total) {
            fmt_bytes(a, sizeof(a), g_tx_done);
            fmt_bytes(b, sizeof(b), g_tx_total);
            rc_strf(tx, sizeof(tx), "%stx %s/%s", rx[0] ? "  " : "", a, b);
        } else if (g_tx_done) {
            fmt_bytes(a, sizeof(a), g_tx_done);
            rc_strf(tx, sizeof(tx), "%stx %s", rx[0] ? "  " : "", a);
        }
        rc_strf(out, cap, " %s%s", rx, tx);
    } else {
        g_rx_done = g_rx_total = g_tx_done = g_tx_total = 0;
        rc_strcpy(out, cap, "");
    }
}
