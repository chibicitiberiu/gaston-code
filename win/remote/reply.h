/* How a handler answers, so that no handler has to remember how.
 *
 * Every reply carries the request's `id`, and a handler that forgot one
 * would leave the caller waiting out its whole budget for something that had
 * already happened. So the id is not a header a handler passes -- it is the
 * Responder, and it goes on first.
 *
 * THE RESERVE IS POLICY, AND IT LIVES HERE. `Connection::send` takes a number
 * of bytes to leave free for somebody else (wire/link.h). Getting that right
 * per call site would mean getting it right at every call site, so this makes
 * the decision once: bulk `DATA` leaves RC_OUTQ_RESERVE and waits the long
 * budget, because a transfer can afford to wait for the wire; `OK`, `ERR` and
 * `END` leave nothing and wait the short one, because they are what the
 * reserve exists to protect and they are the last thing a request does.
 *
 * A false return means the frame did not reach the queue -- a dead link, or
 * a bulk chunk that could not get room inside its budget. A streaming
 * handler stops on it rather than reading a file to the end for nobody.
 */
#ifndef RC_REMOTE_REPLY_H
#define RC_REMOTE_REPLY_H

#ifndef __cplusplus
#error "remote/reply.h is C++"
#endif

#include "wire/connection.h"

class Responder
{
public:
    /* `id` may be empty, for a request that arrived without one; the reply
     * then carries none either, which is as much as can honestly be said. */
    Responder(Connection &conn, const char *id);

    /* At most MAX_REPLY_HEADERS beyond the id. A handler that needs more
     * than that is describing a record, and records go in a body. */
    enum { MAX_REPLY_HEADERS = 10 };

    bool ok(const char *const *keys = 0, const char *const *vals = 0,
            int n = 0);
    bool err(const char *code, const char *detail = 0);
    bool data(const void *body, unsigned len, const char *const *keys = 0,
              const char *const *vals = 0, int n = 0);
    bool end(const char *const *keys = 0, const char *const *vals = 0,
             int n = 0);

    const char *id() const { return m_id; }

private:
    Responder(const Responder &);
    Responder &operator=(const Responder &);

    bool say(const char *type, const char *const *keys,
             const char *const *vals, int n, const void *body, unsigned len,
             bool bulk);

    Connection *m_conn;
    char m_id[64];
};

#endif
