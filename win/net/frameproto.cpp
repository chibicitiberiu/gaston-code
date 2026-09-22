#include <windows.h>
#include <string.h>
#include <stdio.h>
#include "net/frameproto.h"
#include "core/str.h"

static int hexval(char c)
{
    if (c >= '0' && c <= '9') return c - '0';
    if (c >= 'a' && c <= 'f') return c - 'a' + 10;
    if (c >= 'A' && c <= 'F') return c - 'A' + 10;
    return -1;
}

/* Decodes %XX in place. */
static void unquote(char *s)
{
    char *r = s, *w = s;
    while (*r) {
        if (r[0] == '%' && r[1] && r[2]) {
            int hi = hexval(r[1]), lo = hexval(r[2]);
            if (hi >= 0 && lo >= 0) {
                *w++ = (char)((hi << 4) | lo);
                r += 3;
                continue;
            }
        }
        *w++ = *r++;
    }
    *w = '\0';
}

/* THE LANE TABLE. See the big comment in frameproto.h for what each lane
 * means; this is the one place a Phase 6 tool's frame type gets classified.
 *
 * SYNC* is not a row here because it is a whole FAMILY of frame types
 * (SYNCSCAN, SYNCPUT, SYNCPUTEND, SYNCMKDIR, SYNCGET today) sharing one
 * four-byte prefix; rc_frame_lane matches it as a wildcard below rather than
 * listing five rows that would drift the moment a sixth SYNC* type is added
 * on one side and not the other. */
const rc_lane_row RC_LANE_TABLE[] = {
    { "EXEC",     RC_LANE_JOB },
    { "EXECKILL", RC_LANE_IMMEDIATE }
};
const int RC_LANE_TABLE_N = sizeof(RC_LANE_TABLE) / sizeof(RC_LANE_TABLE[0]);

rc_lane rc_frame_lane(const char *type)
{
    int i;
    for (i = 0; i < RC_LANE_TABLE_N; i++)
        if (strcmp(type, RC_LANE_TABLE[i].type) == 0)
            return RC_LANE_TABLE[i].lane;
    if (strncmp(type, "SYNC", 4) == 0) return RC_LANE_STREAM;
    return RC_LANE_UI;
}

const char *rc_frame_get(const rc_frame *f, const char *key)
{
    int i;
    for (i = 0; i < f->nheaders; i++)
        if (strcmp(f->headers[i].key, key) == 0)
            return f->headers[i].val;
    return NULL;
}

/* Parses in place. `payload` must stay alive for the lifetime of the frame;
 * ownership passes to the frame via _raw. */
int rc_frame_parse(char *payload, unsigned len, rc_frame *out)
{
    char *p, *end, *nl, *eq;

    memset(out, 0, sizeof(*out));
    out->_raw = payload;

    if (len < 2) return -1;
    end = payload + len;

    /* Type line */
    nl = (char *)memchr(payload, '\n', (size_t)(end - payload));
    if (!nl) return -1;
    *nl = '\0';
    if (nl == payload) return -1;                 /* empty type */
    /* Bounded, and the bound is checked by the copy itself rather than by a
     * separate statement above it. The guard is kept -- an over-long type is
     * a dropped frame, not a truncated one -- but a future edit that moves or
     * loses it can no longer turn this into an overflow. */
    if ((size_t)(nl - payload) >= RC_MAX_TYPE) return -1;
    if (!rc_strcpy(out->type, RC_MAX_TYPE, payload)) return -1;

    p = nl + 1;

    /* Headers, terminated by a blank line */
    while (p < end) {
        if (*p == '\n') { p++; break; }           /* blank line: headers done */

        nl = (char *)memchr(p, '\n', (size_t)(end - p));
        if (!nl) return -1;
        *nl = '\0';

        eq = strchr(p, '=');
        if (!eq) return -1;                       /* header without '=' */
        *eq = '\0';

        /* A frame with more headers than we can hold is REFUSED, not quietly
         * shortened. This used to be `if (nheaders < MAX)` with no else, so
         * header seventeen simply disappeared -- while the two checks just
         * below already dropped the frame for an over-long key or value. The
         * one case that lost data in silence was the one where the data was
         * perfectly well-formed and there was merely too much of it. If the
         * missing header is `id`, an ack is answered to nobody and the far
         * side waits out its whole timeout.
         *
         * Safe to be strict: the bridge's largest frame carries six headers,
         * and tests/python/test_guest_frame.py counts them from protocol.py
         * so that adding a seventeenth fails there rather than here. */
        if (out->nheaders >= RC_MAX_HEADERS) return -1;

        /* A REPEATED key drops the frame, which is what the bridge already
         * did and what PLAN.md always specified. The guest had no check at
         * all: every k=v line was appended, and rc_frame_get returns the
         * FIRST match on a linear scan, so a second `id=` was accepted,
         * stored, and then never read. Silent first-wins is the worst of the
         * three available answers -- the sender believes it set a value, the
         * receiver acts on a different one, and nothing anywhere disagrees
         * out loud. bridge/wire/protocol.py raises ProtocolError and tears
         * the connection down; this now matches it. Safe to be strict: the
         * bridge builds headers from a dict, so it cannot emit a duplicate
         * even by accident. */
        {
            int d;
            for (d = 0; d < out->nheaders; d++)
                if (strcmp(out->headers[d].key, p) == 0) return -1;
        }

        {
            rc_header *h = &out->headers[out->nheaders];
            /* Silently truncating a key would turn two distinct headers into
             * one; drop the frame instead. */
            if (strlen(p) >= RC_MAX_KEY) return -1;
            if (!rc_strcpy(h->key, RC_MAX_KEY, p)) return -1;
            /* A VALUE that does not fit is now a dropped frame too, and that
             * is a behaviour change made deliberately.
             *
             * This line clipped at 255 in silence while the KEY path four
             * lines up dropped the frame -- an asymmetry the old comment
             * described as intentional. It is not survivable for `cwd`, which
             * travels as a header and goes straight to CreateProcessA: a
             * clipped path names a DIFFERENT, existing directory, so the
             * command runs somewhere nobody chose and nothing reports it.
             * Losing the frame is loud; running in the wrong directory is not.
             *
             * The bridge refuses to SEND an over-long cwd as well, so in
             * practice neither side can reach this. Both, because the guest's
             * checks exist for the case where the bridge is what is wrong. */
            if (!rc_strcpy(h->val, RC_MAX_VAL, eq + 1)) return -1;
            unquote(h->val);
            out->nheaders++;
        }
        p = nl + 1;
    }

    out->body = p;
    out->body_len = (unsigned)(end - p);
    return 0;
}

void rc_frame_free(rc_frame *f)
{
    if (f->_body_alloc) {
        HeapFree(GetProcessHeap(), 0, f->_body_alloc);
        f->_body_alloc = NULL;
    }
    if (f->_raw) { HeapFree(GetProcessHeap(), 0, f->_raw); f->_raw = NULL; }
    f->body = NULL;
    f->body_len = 0;
}

static int append(char *buf, unsigned bufsz, unsigned *pos, const char *s)
{
    unsigned n = (unsigned)strlen(s);
    if (*pos + n > bufsz) return -1;
    memcpy(buf + *pos, s, n);
    *pos += n;
    return 0;
}

/* Append a header VALUE, percent-encoding the three bytes that would
 * otherwise change the shape of the frame.
 *
 * This is the write half of unquote() above, and for a long time it did not
 * exist: rc_frame_build appended values raw while the BRIDGE percent-decodes
 * everything it receives. frameproto.h has said "header values are
 * percent-encoded" since the file was written, and it was true in exactly one
 * direction.
 *
 * Two ways that bites, both from ordinary data rather than from an attack. A
 * '%' followed by two hex digits is DECODED by the bridge, so a value of
 * "50%41" arrives as "50A" -- silent, self-consistent, and wrong on every
 * layer downstream. A '\n' is worse: it ends the header line early and what
 * follows is parsed as another header, so a frame can arrive carrying an `id`
 * nobody sent, and `id` is what routes an ack. Values here come from the INI,
 * from GetVersionExA and from the filesystem.
 *
 * Percent-encoding matches bridge/wire/protocol.py _SAFE: printable ASCII
 * (0x20..0x7E) except '%'. Control characters and high bytes (non-ASCII)
 * are escaped as %XX so the header block is strictly ASCII on the wire. */
static int append_quoted(char *buf, unsigned bufsz, unsigned *pos,
                         const char *s)
{
    static const char HEX[] = "0123456789ABCDEF";
    unsigned i;

    for (i = 0; s[i]; i++) {
        unsigned char c = (unsigned char)s[i];
        if (c < 0x20 || c > 0x7E || c == '%') {
            if (*pos + 3 > bufsz) return -1;
            buf[(*pos)++] = '%';
            buf[(*pos)++] = HEX[(c >> 4) & 0xF];
            buf[(*pos)++] = HEX[c & 0xF];
        } else {
            if (*pos + 1 > bufsz) return -1;
            buf[(*pos)++] = (char)c;
        }
    }
    return 0;
}

int rc_frame_build(char *buf, unsigned bufsz, const char *type,
                   const char *const *keys, const char *const *vals, int nkv,
                   const void *body, unsigned body_len)
{
    unsigned pos = 4;                             /* leave room for length */
    int i;

    if (append(buf, bufsz, &pos, type) < 0) return -1;
    if (append(buf, bufsz, &pos, "\n") < 0) return -1;

    for (i = 0; i < nkv; i++) {
        if (append(buf, bufsz, &pos, keys[i]) < 0) return -1;
        if (append(buf, bufsz, &pos, "=") < 0) return -1;
        /* The VALUE is encoded; the KEY is not. Keys in this tree are all
         * string literals chosen here, so there is nothing to escape, and
         * leaving them raw keeps a malformed key a parse error on the far
         * side rather than something that quietly encodes into legality. */
        if (append_quoted(buf, bufsz, &pos, vals[i]) < 0) return -1;
        if (append(buf, bufsz, &pos, "\n") < 0) return -1;
    }
    if (append(buf, bufsz, &pos, "\n") < 0) return -1;

    if (body_len) {
        if (pos + body_len > bufsz) return -1;
        memcpy(buf + pos, body, body_len);
        pos += body_len;
    }

    {
        unsigned payload = pos - 4;
        if (payload > RC_MAX_FRAME) return -1;
        buf[0] = (char)(payload & 0xFF);
        buf[1] = (char)((payload >> 8) & 0xFF);
        buf[2] = (char)((payload >> 16) & 0xFF);
        buf[3] = (char)((payload >> 24) & 0xFF);
    }
    return (int)pos;
}
