/* One lane: one thread, one request in flight, and an answer either way.
 *
 * The reader thread hands a frame over and carries straight on -- that is
 * the point, since the reader also carries the heartbeat and answers the
 * control ops. If the lane is already busy the hand-over FAILS rather than
 * queues, and the reader answers `ERR busy op= age_ms=` on the spot.
 *
 * WHY REFUSE RATHER THAN QUEUE. A queue turns "the machine is working" into
 * "the machine will answer eventually", which on a 32MB box with one disk is
 * a promise nobody can keep, and it hides the state the caller actually
 * wants to know. `age_ms` is in the refusal because it is the difference
 * between "try again in a moment" and "something is stuck".
 *
 * WHY EVERY PATH ANSWERS. A request that is refused, and a request still
 * sitting in the slot when the worker stops, both get a reply. Silence costs
 * the caller its whole budget and tells it nothing.
 */
#ifndef RC_REMOTE_LANE_H
#define RC_REMOTE_LANE_H

#ifndef __cplusplus
#error "remote/lane.h is C++"
#endif

#include <windows.h>
#include "core/rc_cpp.h"
#include "core/thread.h"
#include "wire/frame.h"

class LaneHandler
{
public:
    virtual ~LaneHandler() {}

    /* On the lane's thread. Owns `f` and must free it. */
    virtual void handle(rc_frame *f) = 0;

    /* For a frame that will never run -- the worker is stopping. Owns `f`.
     * Still has to answer it. */
    virtual void abandon(rc_frame *f) = 0;
};

class LaneWorker
{
public:
    LaneWorker();
    ~LaneWorker();

    bool start(LaneHandler &handler, const char *name, unsigned stackBytes);

    /* READER THREAD. True: the lane took the frame and owns it. False: the
     * lane is busy, the frame is still the caller's, and `busyOp`/`ageMs`
     * describe what is holding it. */
    bool submit(rc_frame *f, const char **busyOp, DWORD *ageMs);

    /* What is running, or NULL. Reader thread; for CANCEL, later. */
    bool inFlight() const { return m_inFlight != 0; }

    void signalStop();
    bool join(DWORD timeoutMs);

private:
    class Body : public Runnable
    {
    public:
        Body() : m_lane(0) {}
        void attach(LaneWorker *l) { m_lane = l; }
        void run();
    private:
        LaneWorker *m_lane;
    };
    friend class Body;

    LaneWorker(const LaneWorker &);
    LaneWorker &operator=(const LaneWorker &);

    void loop();

    LaneHandler *m_handler;
    Body m_body;
    Thread m_thread;
    CRITICAL_SECTION m_lock;
    bool m_lockUp;
    Handle m_wake;
    rc_frame *m_slot;           /* handed over, not yet picked up */
    volatile LONG m_running;
    volatile LONG m_inFlight;   /* handed over OR being handled */
    char m_op[RC_MAX_TYPE];     /* what is in flight, under m_lock */
    DWORD m_startedAt;
};

#endif
