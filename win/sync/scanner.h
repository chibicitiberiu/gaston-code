/* The sync layer's content classifier and its two checksums.
 *
 * NOT in libfs, deliberately. This is bridge agreement, not filesystem
 * behaviour: the classes and the CRC choice must match bridge/files/paths.py
 * byte for byte, the bridge overrides the class from .gitattributes, and
 * gremote's GET is byte-exact and wants none of it. libfs supplies the
 * one-pass read (fs::FileReader) and this plugs into it as a ByteSink.
 *
 * The class is not known until the last byte, and this machine cannot afford
 * to read a file twice, so BOTH checksums accumulate as the bytes go past and
 * the right one is chosen at the end. Two CRC accumulations cost far less than
 * a second pass over the platter.
 */
#ifndef RC_SYNC_SCANNER_H
#define RC_SYNC_SCANNER_H

#ifndef __cplusplus
#error "sync/scanner.h is C++"
#endif

#include "core/bytesink.h"
#include "sync/sync.h"

class ContentScanner : public ByteSink
{
public:
    /* Heap only, and NULL on OOM rather than an exception: it carries an
     * RC_SYNC_CHUNK + 1 normalisation buffer, the RPC worker does not have
     * that much stack under whatever else is on it, and every caller already
     * implements "out of memory means unreadable, never the file is fine".
     *
     * The + 1 is load-bearing. A CR carried in from the previous chunk is
     * emitted in front of this chunk's first byte, so an n-byte chunk that
     * does not itself end in CR normalises to n + 1 bytes. At exactly
     * RC_SYNC_CHUNK that wrote one byte past the block, into the next block's
     * header, for any streamed file with a chunk ending in 0x0D -- the process
     * then died later inside KERNEL32's heap code with nothing logged.
     */
    static ContentScanner *create();
    void destroy();

    /* ByteSink. Always accepts: classifying never asks a producer to stop. */
    virtual bool write(const void *data, unsigned len);

    /* Resolve a CR left pending at end of file, classify, and hand back
     * whichever outputs the caller asked for; any pointer may be NULL.
     * `crcOut` gets the one THIS class wants, chosen by RC_CRC_FOR -- see that
     * macro for why a caller that reimplements the choice can get it wrong in
     * a way only a guest-agreement test ever catches. Does not free. */
    void finish(char *clsOut, unsigned long *rawOut, unsigned long *normOut,
                unsigned long *crcOut);

private:
    ContentScanner();
    ~ContentScanner() {}
    ContentScanner(const ContentScanner &);
    ContentScanner &operator=(const ContentScanner &);

    char classify() const;

    unsigned long m_crcRaw;    /* every byte as it is on disk */
    unsigned long m_crcNorm;   /* CRLF collapsed to LF */
    int m_sawNul;
    int m_sawCrlf;
    int m_sawBareLf;
    int m_sawLoneCr;           /* a CR not followed by LF -- includes \r\r\n */
    int m_pendingCr;           /* last byte of the previous chunk was CR */
    unsigned char m_nb[RC_SYNC_CHUNK + 1];
};

#endif
