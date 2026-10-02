/* gremote's ops and the lane each one runs on.
 *
 * ITS OWN TABLE, not the client's. `win/net/frameproto.cpp` carries
 * RC_LANE_TABLE for the bridge protocol; this is a separate op namespace on
 * the same frame grammar and REMOTE.md is explicit that the two must not
 * drift into each other. libwire knows no ops at all -- each exe supplies
 * its own table, which is the whole reason the codec and the lane table were
 * split apart in step 2.
 *
 * Four lanes, and the boundaries are about what an op may BLOCK ON rather
 * than how quick it looks:
 *
 *   control  the reader thread, inline. kernel32 and Winsock only. Never
 *            USER or GDI: on 9x a hung 16-bit app holding Win16Mutex blocks
 *            those from every 32-bit thread, and the reader carries the
 *            heartbeat. PS and KILL are here rather than on the job lane
 *            because behind a stuck EXEC is exactly when you need them.
 *   job      one child process at a time. May block for the two minutes an
 *            EXEC is allowed.
 *   fs       one disk operation at a time. May block on a platter, a
 *            floppy that is not there, or a file somebody else has open.
 *   ui       one desktop operation at a time. May block on USER, on GDI,
 *            and on another application's message loop -- which is why it
 *            is not the control lane's, and why it is the one lane whose
 *            handler has thread-bound state to set up (remote/uiops.h).
 *
 * Unknown is not a lane. An op nobody recognises is answered `unknown_op`
 * rather than dropped -- silence costs the caller its whole budget and tells
 * it nothing, which is the duty reject_busy has on the client.
 */
#ifndef RC_REMOTE_OPTABLE_H
#define RC_REMOTE_OPTABLE_H

#ifndef __cplusplus
#error "remote/optable.h is C++"
#endif

enum GLane
{
    G_CONTROL = 0,
    G_JOB,
    G_FS,
    G_UI
};

struct GOpRow
{
    const char *op;
    GLane lane;
    /* True for the frames that CONTINUE a request already open on the lane:
     * a PUT's DATA chunks and its END. They route to the open request by id
     * and must NOT go through the lane's busy check -- the request holding
     * the lane is the one they belong to, so a busy refusal would refuse the
     * transfer its own bytes. */
    bool continues;
};

extern const GOpRow G_OP_TABLE[];
extern const int G_OP_TABLE_N;

/* False if nothing in the table matches, which is the caller's cue to answer
 * `unknown_op`. */
bool gremoteOp(const char *type, const GOpRow **out);

#endif
