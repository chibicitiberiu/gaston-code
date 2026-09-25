/* The frame codec: libwire's wire format, shared by every exe that speaks it
 * (the client to the bridge, gremote to its Python client). Mirror of
 * bridge/wire/protocol.py's codec -- keep in sync.
 *
 *   frame   := uint32le(len) payload        (len <= RC_MAX_FRAME)
 *   payload := TYPE "\n" *(key "=" value "\n") "\n" body
 *
 * Deliberately not JSON: this side has no JSON parser and is not getting one.
 * Header values are percent-encoded for %, controls and non-ASCII, so
 * the wire format is strictly printable ASCII.
 *
 * The codec knows no frame TYPES. Which types exist, what they mean and which
 * thread handles them belong to each protocol built on it: the client's are
 * in net/frameproto.h.
 */
#ifndef RC_WIRE_FRAME_H
#define RC_WIRE_FRAME_H

#ifdef __cplusplus
extern "C" {
#endif

/* 64KB, NOT retro-agent's 32MB. 32MB is the entire target machine: one
 * corrupted length prefix would allocate the box away. */
#define RC_MAX_FRAME     (64 * 1024)

#define RC_MAX_TYPE      32
#define RC_MAX_HEADERS   16
#define RC_MAX_KEY       32
#define RC_MAX_VAL       256

typedef struct {
    char key[RC_MAX_KEY];
    char val[RC_MAX_VAL];
} rc_header;

typedef struct {
    char       type[RC_MAX_TYPE];
    rc_header  headers[RC_MAX_HEADERS];
    int        nheaders;
    char      *body;        /* into _raw, or into _body_alloc if inflated */
    unsigned   body_len;
    char      *_raw;        /* owning allocation; free with rc_frame_free */
    char      *_body_alloc; /* set only when the body was inflated */
} rc_frame;

const char *rc_frame_get(const rc_frame *f, const char *key);
int         rc_frame_parse(char *payload, unsigned len, rc_frame *out);
void        rc_frame_free(rc_frame *f);
/* rc_frame_free, then the frame struct itself -- for frames that were
 * HeapAlloc'd whole, which is every frame the reader hands on. */
void        rc_frame_delete(rc_frame *f);

/* Builds "TYPE\nk=v\n...\n\n" + body into buf. Returns total bytes or -1. */
int rc_frame_build(char *buf, unsigned bufsz, const char *type,
                   const char *const *keys, const char *const *vals, int nkv,
                   const void *body, unsigned body_len);

#ifdef __cplusplus
}
#endif

#endif
