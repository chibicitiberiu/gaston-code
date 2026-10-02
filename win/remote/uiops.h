/* uiops.h -- gremote's desktop lane (docs/REMOTE.md step 6).
 *
 * TREE, SHOT and RUN, over the same handlers the client runs: the behaviour
 * lives in `win/ui/uiengine.cpp` behind `UiLink`, and this file is the half
 * that knows gremote's wire. SHOT is the one handler deliberately not
 * shared -- 55 lines of header clamping over a sink that has to wrap a
 * `Responder` rather than an `rc_net` whatever we do.
 *
 * It is the only lane with THREAD-BOUND state, which is why LaneHandler has
 * onThreadStart/onThreadStop at all: a message queue has to exist before
 * anything is posted to the thread, COM's apartment belongs to the thread
 * that initialised it, a message filter belongs to the thread that
 * registered it, and a WinEvent hook delivers only to the thread that
 * installed it. None of that can be done by whoever constructed this
 * object, because that was the reader.
 */
#ifndef RC_REMOTE_UIOPS_H
#define RC_REMOTE_UIOPS_H

#ifndef __cplusplus
#error "remote/uiops.h is C++"
#endif

#include "remote/config.h"
#include "remote/lane.h"
#include "remote/reply.h"
#include "wire/connection.h"

class UiContext;

class UiOps : public LaneHandler
{
public:
    UiOps(Connection &conn, const UiConfig &cfg);
    ~UiOps();

    /* LaneHandler, on the ui thread. Both free `f`. */
    void handle(rc_frame *f);
    void abandon(rc_frame *f);

    /* LaneHandler, on the ui thread, once each around everything else. */
    bool onThreadStart();
    void onThreadStop();

    /* LaneHandler, on the READER thread. CANCEL: stop the work AND suppress
     * the reply, which is what the ack promises. */
    bool cancel(const char *id);

    /* On the ui thread: the event feed is stopped here when the lane has
     * been quiet, and the message queue is drained every time round. */
    void idle();

    /* STOP, from the Session on the reader thread. Stops the work and lets
     * the request answer anyway -- `cancelled`, with whatever it had.
     *
     * The difference from cancel() is one line and it is the whole point: a
     * step list that failed at step 5 still ran four steps the caller has to
     * hear about, and CANCEL's ack promises silence. Returns false when
     * nothing by that name is here. */
    bool stop(const char *id);

    void setGate(ReplyGate *gate) { m_gate = gate; }

    /* What is running, for a caller that wants to stop whatever it is --
     * Session::end() does, before it joins. Empty when the lane is idle. */
    const char *runningId() const { return m_runId; }

private:
    UiOps(const UiOps &);
    UiOps &operator=(const UiOps &);

    void shot(Responder &r, rc_frame *f);

    Connection     *m_conn;
    const UiConfig *m_cfg;
    ReplyGate      *m_gate;

    /* Built on the ui thread and destroyed there: every member of it is
     * apartment- or thread-bound. */
    UiContext      *m_ctx;
    bool            m_com;

    /* Raised by STOP or CANCEL, read by the walk, the capture and the step
     * runner. */
    volatile LONG   m_stopFlag;
    /* The request running, and one a stop arrived for too early to catch --
     * the same pair the client keeps, for the same reason: a stop is
     * answered on the reader, so between the submit and the worker picking
     * the frame up there is a window where nothing yet knows the id. */
    char            m_runId[64];
    char            m_stopId[64];

    /* The event feed is expensive to leave running: a system-wide WinEvent
     * hook wakes this process on every window event on the machine, and
     * WINEVENT_SKIPOWNPROCESS skips only our own. The client pays that while
     * somebody is sitting at it; gremote would pay it from RunServices until
     * the box is switched off. So it starts on the first request that needs
     * it and stops when the lane has been quiet -- which is also what
     * remote/tray.h's "nothing polls" rule asks of a program that runs
     * BESIDE the thing being measured. */
    bool            m_watching;
    int             m_quietTicks;
};

#endif
