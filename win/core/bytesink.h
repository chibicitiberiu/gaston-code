/* A stream of bytes going somewhere, and a way for the somewhere to stop it.
 *
 * In libcore because two libraries produce one: libautomation streams a
 * screenshot in strips, libfs streams a file in chunks, and gremote links
 * both. Two identical interfaces with the same name in the global namespace
 * would be an ODR violation that links quietly and behaves badly.
 *
 * `write` returning false is the whole design. A producer on this machine can
 * generate a megabyte the consumer no longer wants -- the connection dropped,
 * the request was cancelled, a cap was reached -- and on a 32MB box the only
 * affordable answer is to stop at the next chunk rather than to buffer or to
 * run to completion. Every producer checks it; none of them treats it as an
 * error.
 */
#ifndef RC_CORE_BYTESINK_H
#define RC_CORE_BYTESINK_H

#ifndef __cplusplus
#error "core/bytesink.h is C++"
#endif

class ByteSink
{
public:
    virtual ~ByteSink() {}

    /* False stops the producer at the next chunk boundary. */
    virtual bool write(const void *data, unsigned len) = 0;
};

#endif
