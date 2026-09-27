#include "remote/reply.h"

#include "core/str.h"
#include "wire/link.h"

Responder::Responder(Connection &conn, const char *id) : m_conn(&conn)
{
    rc_strcpy(m_id, sizeof m_id, id ? id : "");
}

bool Responder::say(const char *type, const char *const *keys,
                    const char *const *vals, int n, const void *body,
                    unsigned len, bool bulk)
{
    const char *k[MAX_REPLY_HEADERS + 1];
    const char *v[MAX_REPLY_HEADERS + 1];
    int total = 0;

    if (n > MAX_REPLY_HEADERS)
        n = MAX_REPLY_HEADERS;
    if (m_id[0])
    {
        k[0] = "id";
        v[0] = m_id;
        total = 1;
    }
    for (int i = 0; i < n; ++i)
    {
        k[total] = keys[i];
        v[total] = vals[i];
        total++;
    }
    return m_conn->send(type, k, v, total, body, len,
                        bulk ? RC_SEND_WAIT_BULK : RC_SEND_WAIT_UI, bulk,
                        bulk ? (unsigned)RC_OUTQ_RESERVE : 0u) == RC_SEND_OK;
}

bool Responder::ok(const char *const *keys, const char *const *vals, int n)
{
    return say("OK", keys, vals, n, 0, 0, false);
}

bool Responder::errWith(const char *code, const char *const *keys,
                        const char *const *vals, int n)
{
    const char *k[MAX_REPLY_HEADERS];
    const char *v[MAX_REPLY_HEADERS];
    int total = 1;

    k[0] = "code";
    v[0] = code;
    if (n > MAX_REPLY_HEADERS - 1)
        n = MAX_REPLY_HEADERS - 1;
    for (int i = 0; i < n; ++i)
    {
        k[total] = keys[i];
        v[total] = vals[i];
        total++;
    }
    return say("ERR", k, v, total, 0, 0, false);
}

bool Responder::err(const char *code, const char *detail)
{
    const char *k[1];
    const char *v[1];
    if (!detail || !*detail)
        return errWith(code, 0, 0, 0);
    k[0] = "detail";
    v[0] = detail;
    return errWith(code, k, v, 1);
}

/* Compressible is passed as `bulk` too: DATA is the only thing here big
 * enough for deflate to pay for itself, and with compression off in v0 the
 * flag costs nothing either way. */
bool Responder::data(const void *body, unsigned len, const char *const *keys,
                     const char *const *vals, int n)
{
    return say("DATA", keys, vals, n, body, len, true);
}

bool Responder::end(const char *const *keys, const char *const *vals, int n)
{
    return say("END", keys, vals, n, 0, 0, false);
}
