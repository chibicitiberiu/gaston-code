#include "sync/scanner.h"

#include <new>
#include <string.h>

#include "miniz.h"

ContentScanner::ContentScanner()
    : m_crcRaw(MZ_CRC32_INIT), m_crcNorm(MZ_CRC32_INIT),
      m_sawNul(0), m_sawCrlf(0), m_sawBareLf(0), m_sawLoneCr(0),
      m_pendingCr(0)
{
    /* m_nb is scratch, overwritten before it is read, and clearing 8KB per
     * file on a 200MHz box would be a real cost for nothing. */
}

ContentScanner *ContentScanner::create()
{
    /* HeapAlloc + placement new, not `new`: the contract is NULL on OOM, and
     * a plain `new` throws bad_alloc (core/heap.cpp). memset-then-use is NOT
     * available here any more -- this class has a vtable now, and zeroing it
     * would zero the vptr. */
    void *p = HeapAlloc(GetProcessHeap(), 0, sizeof(ContentScanner));
    if (!p) return 0;
    return new (p) ContentScanner();
}

void ContentScanner::destroy()
{
    this->~ContentScanner();
    HeapFree(GetProcessHeap(), 0, this);
}

bool ContentScanner::write(const void *data, unsigned n)
{
    const unsigned char *buf = (const unsigned char *)data;
    unsigned char *nb = m_nb;
    unsigned nn = 0;
    unsigned i;

    m_crcRaw = mz_crc32(m_crcRaw, buf, n);

    for (i = 0; i < n; i++)
    {
        unsigned char c = buf[i];

        if (m_pendingCr)
        {
            /* Resolve the CR carried over from the previous chunk or byte. */
            if (c == '\n')
            {
                m_sawCrlf = 1;
                nb[nn++] = '\n';        /* CRLF collapses to LF */
                m_pendingCr = 0;
                continue;
            }
            /* A CR followed by anything else -- including another CR, which is
             * how \r\r\n gets here. Both are byte-exact cases: normalisation
             * is NOT idempotent on them, and a file that is rewritten on every
             * pass never converges. */
            m_sawLoneCr = 1;
            nb[nn++] = '\r';
            m_pendingCr = 0;
        }

        if (c == 0) m_sawNul = 1;

        if (c == '\r')
        {
            m_pendingCr = 1;            /* decided by the next byte */
            continue;
        }
        if (c == '\n') m_sawBareLf = 1;
        nb[nn++] = c;
    }
    if (nn) m_crcNorm = mz_crc32(m_crcNorm, nb, nn);
    return true;
}

char ContentScanner::classify() const
{
    if (m_sawNul)     return RC_CLS_EXACT;  /* anywhere, not a sniff window */
    if (m_sawLoneCr)  return RC_CLS_EXACT;  /* lone CR and \r\r\n */
    if (m_sawCrlf && m_sawBareLf) return RC_CLS_EXACT;   /* mixed */
    return RC_CLS_TEXT;
}

void ContentScanner::finish(char *clsOut, unsigned long *rawOut,
                            unsigned long *normOut, unsigned long *crcOut)
{
    char cls;

    /* A file ending in CR: the carry never resolved, so it is a lone CR. */
    if (m_pendingCr)
    {
        unsigned char cr = '\r';
        m_sawLoneCr = 1;
        m_crcNorm = mz_crc32(m_crcNorm, &cr, 1);
        m_pendingCr = 0;
    }

    cls = classify();
    if (clsOut)  *clsOut  = cls;
    if (rawOut)  *rawOut  = m_crcRaw;
    if (normOut) *normOut = m_crcNorm;
    if (crcOut)
        *crcOut = (cls == RC_CLS_TEXT) ? m_crcNorm : m_crcRaw;
}

/* --- the C names, for win/checks/rpccheck.c ------------------------------
 *
 * rpccheck.c is a C program and drives these three directly. It never
 * dereferences the pointer, so the opaque `rc_sync_scanner` in sync.h can be
 * this class. Removed with sync/compat.cpp when the last C caller goes.
 */
extern "C" rc_sync_scanner *rc_sync_scanner_begin(void)
{
    return (rc_sync_scanner *)ContentScanner::create();
}

extern "C" void rc_sync_scanner_feed(rc_sync_scanner *s, const void *buf,
                                     unsigned n)
{
    if (s) ((ContentScanner *)s)->write(buf, n);
}

extern "C" void rc_sync_scanner_finish(rc_sync_scanner *s, char *cls_out,
                                       unsigned long *crc_out)
{
    ContentScanner *c = (ContentScanner *)s;
    if (!c) return;
    c->finish(cls_out, 0, 0, crc_out);
    c->destroy();
}
