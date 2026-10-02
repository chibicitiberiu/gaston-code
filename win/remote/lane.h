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
 *
 * CANCEL, AND WHAT ITS ACK MEANS. 9P's Tflush, copied rather than
 * re-derived (docs/BACKLOG.md 6): the ack means the cancelled request will
 * NEVER be answered, and the caller may reuse its id once the ack arrives.
 * The hard half is the race -- did the cancel beat the reply? -- and the
 * answer here is a mark rather than a wait. An id marked cancelled has its
 * replies dropped at the Responder, so a handler that was already most of
 * the way through its answer still produces no frame; the mark is cleared by
 * the worker when that frame is finally done with. Since a lane runs one
 * request at a time, a reused id cannot start until the old work has
 * cleared, so the mark can never suppress the wrong request.
 *
 * AND WHY THERE IS A SECOND QUEUE. A PUT is not one frame: it opens with
 * PUT, continues with DATA chunks and closes with END, and each of those is
 * a short unit of work rather than one long-running request. Sent through
 * the single slot they would refuse each other the moment the platter fell
 * behind the wire -- a transfer refused its own bytes, which on a 200MHz box
 * writing to a 1998 disk is the normal case rather than the unlucky one.
 *
 * So continuations have their own small FIFO, drained ahead of the slot and
 * in order. When it is full the READER WAITS, briefly, and that is not an
 * oversight: not reading the socket is what closes the TCP window and makes
 * the sender stop, which is the backpressure this wants. It does not starve
 * the heartbeat, because the heartbeat is about frames ARRIVING and during a
 * transfer they are arriving constantly.
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

/* Asked before a reply goes out, so a handler does not have to remember
 * that its request might have been cancelled underneath it. */
class ReplyGate
{
public:
    virtual ~ReplyGate() {}
    virtual bool mayAnswer(const char *id) = 0;
};

class LaneHandler
{
public:
    virtual ~LaneHandler() {}

    /* On the lane's thread. Owns `f` and must free it. */
    virtual void handle(rc_frame *f) = 0;

    /* For a frame that will never run -- the worker is stopping. Owns `f`.
     * Still has to answer it. */
    virtual void abandon(rc_frame *f) = 0;

    /* READER THREAD, while `id` is the request in flight. Stop it, and
     * return without waiting: the reader carries the heartbeat. Whether the
     * work actually ends is the handler's business; whether the caller ever
     * hears about it is the gate's.
     *
     * It may only SIGNAL. Anything the lane's own thread could be inside --
     * a handle it is mid-write on -- is cleaned up in idle(), not here.
     *
     * Returns whether this was the handler's to cancel. That matters for
     * work with no frame in flight to match against: a PUT is a sequence of
     * short frames, so between two of them the lane is idle and the only
     * thing that knows a transfer is open is the handler. */
    virtual bool cancel(const char *id) { (void)id; return false; }

    /* On the lane's thread, every time round the loop, frame or no frame.
     * Where the work cancel() asked for actually happens. */
    virtual void idle() {}

    /* ON THE LANE'S THREAD, once each, around everything else.
     *
     * For a handler whose state is THREAD-BOUND rather than merely private.
     * The desktop lane is the reason they exist: it needs a message queue
     * forced into existence before anything is told the thread is there, an
     * STA to make MSAA calls from, and a message filter that belongs to the
     * thread that registered it -- none of which can be set up by whoever
     * constructed the handler, because that was a different thread.
     *
     * `onThreadStart` returning false stops the lane before it serves
     * anything, and `start()` reports it: a desktop worker with no apartment
     * would answer every request wrongly rather than not at all.
     *
     * The job and fs lanes want neither and say so by not overriding. */
    virtual bool onThreadStart() { return true; }
    virtual void onThreadStop() {}
};

enum CancelResult
{
    CANCEL_DROPPED = 0,     /* it had not started; it is gone, unanswered */
    CANCEL_STOPPED,         /* it was running; it has been told to stop */
    CANCEL_NO_SUCH_ID       /* nothing here by that name */
};

class LaneWorker : public ReplyGate
{
public:
    LaneWorker();
    ~LaneWorker();

    bool start(LaneHandler &handler, const char *name, unsigned stackBytes);

    /* READER THREAD. True: the lane took the frame and owns it. False: the
     * lane is busy, the frame is still the caller's, and `busyOp`/`ageMs`
     * describe what is holding it. */
    bool submit(rc_frame *f, const char **busyOp, DWORD *ageMs);

    /* READER THREAD, for a frame that continues a request already open on
     * this lane. Waits up to `waitMs` for room in the FIFO rather than
     * refusing at once -- see the note above. False only if the wait ran
     * out, which means the transfer is going faster than the disk by more
     * than the queue can absorb. */
    bool submitContinuation(rc_frame *f, DWORD waitMs);

    /* READER THREAD. Take `id` out of this lane, by whichever of the three
     * routes applies. See the CANCEL note at the top for what the answer
     * promises. */
    CancelResult cancel(const char *id);

    /* ReplyGate, for the Responders this lane's handler makes. */
    bool mayAnswer(const char *id);

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
    rc_frame *takeNext(bool *stopping);

    /* Deep enough to cover a platter stalling for a moment at 8KB a frame,
     * shallow enough that the memory is bounded and visible: 64KB at worst,
     * on a machine with 32MB. */
    enum { CONT_MAX = 8 };

    LaneHandler *m_handler;
    Body m_body;
    Thread m_thread;
    CRITICAL_SECTION m_lock;
    bool m_lockUp;
    Handle m_wake;
    Handle m_room;              /* a full-FIFO writer waits on this */
    rc_frame *m_slot;           /* handed over, not yet picked up */
    rc_frame *m_cont[CONT_MAX]; /* continuations, in order */
    int m_contHead;
    int m_contN;
    volatile LONG m_running;
    volatile LONG m_inFlight;   /* handed over OR being handled */
    char m_op[RC_MAX_TYPE];     /* what is in flight, under m_lock */
    char m_inFlightId[64];      /* its id, under m_lock */
    char m_cancelled[64];       /* whose replies to drop, under m_lock */
    DWORD m_startedAt;
};

#endif
