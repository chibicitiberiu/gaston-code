/* rate.h -- bytes per second, without dragging in floating point.
 *
 * Four places computed this as `(double)bytes * 1000000.0 / us`, which is
 * exactly the right arithmetic and the wrong instruction set. Open Watcom
 * reaches its printf float formatter through the ___EFG_printf hook on float
 * USE rather than on printf, so those four casts linked math387r.lib --
 * _EFG_Format_, __cvtld_, strtod_ and the long-double conversion set, about
 * 6.2 KB in gaston and gremote -- into a program that formats no floats.
 * gupdate.exe and fscheck.exe prove the mechanism: both call _vsnprintf and
 * neither has a math387r module in its map.
 *
 * The cast was there to stop the multiply overflowing 32 bits, and a 64-bit
 * integer does that for a few hundred bytes of __U8DIV instead. The worst
 * case is bounded: 4.29e9 bytes at 1e6 is 4.29e15, and an unsigned 64-bit
 * holds 1.8e19.
 *
 * It is a header with one inline and no translation unit of its own, because
 * `-xs` costs about 2,144 bytes of exception machinery per TU before any code
 * in it and this is nine lines.
 *
 * The result feeds the bridge's slow-link decision through the STAT frame's
 * `bps` and `deflate_bps` headers, so a wrong answer here does not crash
 * anything -- it silently turns compression on or off at the wrong time.
 * win/checks/wirecheck.cpp pins the arithmetic for that reason.
 */
#ifndef RC_CORE_RATE_H
#define RC_CORE_RATE_H

/* Bytes per second, given a byte count and a span in microseconds. A zero
 * span is "no measurement yet" and answers 0 rather than dividing by it. */
static unsigned long rc_bps(unsigned long bytes, unsigned long us)
{
    if (us == 0)
        return 0;
    return (unsigned long)((unsigned __int64)bytes * 1000000UL
                           / (unsigned __int64)us);
}

#endif /* RC_CORE_RATE_H */
