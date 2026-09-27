#include "remote/lane.h"

#include "core/log.h"
#include "core/str.h"

/* How long the worker sleeps between looks at its stop flag when there is
 * nothing to do. The wake event makes the busy path immediate; this only
 * bounds how long a stop waits on an idle lane. */
enum { LANE_IDLE_SLICE_MS = 100 };

LaneWorker::LaneWorker()
    : m_handler(0), m_lockUp(false), m_slot(0), m_contHead(0), m_contN(0),
      m_running(0), m_inFlight(0), m_startedAt(0)
{
    m_op[0] = '\0';
    for (int i = 0; i < CONT_MAX; ++i)
        m_cont[i] = 0;
}

LaneWorker::~LaneWorker()
{
    /* Reached only after join() returned true, so nothing is in here. A
     * frame still in the slot would be one nobody ever answered, which is
     * why the loop drains it on the way out instead. */
    if (m_slot)
        rc_frame_delete(m_slot);
    while (m_contN > 0)
    {
        rc_frame_delete(m_cont[m_contHead]);
        m_cont[m_contHead] = 0;
        m_contHead = (m_contHead + 1) % CONT_MAX;
        m_contN--;
    }
    if (m_lockUp)
        DeleteCriticalSection(&m_lock);
}

bool LaneWorker::start(LaneHandler &handler, const char *name,
                       unsigned stackBytes)
{
    m_handler = &handler;
    m_wake.reset(CreateEventA(NULL, FALSE, FALSE, NULL));
    m_room.reset(CreateEventA(NULL, FALSE, FALSE, NULL));
    if (!m_wake.valid() || !m_room.valid())
    {
        rc_log("lane: %s could not make its events", name);
        return false;
    }
    InitializeCriticalSection(&m_lock);
    m_lockUp = true;
    m_running = 1;
    m_body.attach(this);
    if (!m_thread.start(m_body, name, stackBytes))
    {
        m_running = 0;
        return false;
    }
    return true;
}

bool LaneWorker::submit(rc_frame *f, const char **busyOp, DWORD *ageMs)
{
    ScopedLock lock(&m_lock);
    if (!m_running)
    {
        *busyOp = "stopping";
        *ageMs = 0;
        return false;
    }
    if (m_inFlight)
    {
        /* m_op is stable while m_inFlight is set and we hold the lock: the
         * worker only clears it under the same lock. */
        *busyOp = m_op;
        *ageMs = GetTickCount() - m_startedAt;
        return false;
    }
    m_slot = f;
    m_inFlight = 1;
    rc_strcpy(m_op, sizeof m_op, f->type);
    m_startedAt = GetTickCount();
    SetEvent(m_wake.get());
    return true;
}

bool LaneWorker::submitContinuation(rc_frame *f, DWORD waitMs)
{
    DWORD waited = 0;

    for (;;)
    {
        {
            ScopedLock lock(&m_lock);
            if (!m_running)
                return false;
            if (m_contN < CONT_MAX)
            {
                m_cont[(m_contHead + m_contN) % CONT_MAX] = f;
                m_contN++;
                SetEvent(m_wake.get());
                return true;
            }
        }
        if (waited >= waitMs)
        {
            rc_log("lane: the continuation queue stayed full for %lums",
                   (unsigned long)waited);
            return false;
        }
        /* Not reading the socket for a moment IS the backpressure: the TCP
         * window closes and the sender stops pushing faster than the disk
         * can take it. */
        WaitForSingleObject(m_room.get(), 50);
        waited += 50;
    }
}

void LaneWorker::signalStop()
{
    m_running = 0;
    if (m_wake.valid())
        SetEvent(m_wake.get());
    if (m_room.valid())
        SetEvent(m_room.get());
}

bool LaneWorker::join(DWORD timeoutMs)
{
    return m_thread.join(timeoutMs);
}

void LaneWorker::Body::run()
{
    m_lane->loop();
}

/* Continuations first, in order, then the request slot. A PUT's own chunks
 * must not wait behind a request that arrived after them. */
rc_frame *LaneWorker::takeNext(bool *stopping)
{
    ScopedLock lock(&m_lock);
    *stopping = m_running == 0;
    if (m_contN > 0)
    {
        rc_frame *f = m_cont[m_contHead];
        m_cont[m_contHead] = 0;
        m_contHead = (m_contHead + 1) % CONT_MAX;
        m_contN--;
        SetEvent(m_room.get());
        return f;
    }
    if (m_slot)
    {
        rc_frame *f = m_slot;
        m_slot = 0;
        return f;
    }
    return 0;
}

void LaneWorker::loop()
{
    for (;;)
    {
        bool stopping = false;
        bool fromSlot;
        rc_frame *f;

        WaitForSingleObject(m_wake.get(), LANE_IDLE_SLICE_MS);
        {
            ScopedLock lock(&m_lock);
            fromSlot = m_contN == 0 && m_slot != 0;
        }
        f = takeNext(&stopping);

        if (f)
        {
            /* Outside the lock: a handler runs for as long as the disk or a
             * child takes, and holding the lock across that would make
             * submit() -- on the reader thread -- wait for it. */
            if (stopping)
                m_handler->abandon(f);
            else
                m_handler->handle(f);
            if (fromSlot)
            {
                ScopedLock lock(&m_lock);
                m_inFlight = 0;
                m_op[0] = '\0';
            }
            /* More may be waiting; go round before sleeping again. */
            if (!stopping)
                continue;
        }
        if (stopping)
            break;
    }

    /* Everything still queued gets an answer, not a drop. */
    for (;;)
    {
        bool ignored;
        rc_frame *f = takeNext(&ignored);
        if (!f)
            break;
        m_handler->abandon(f);
    }
    {
        ScopedLock lock(&m_lock);
        m_inFlight = 0;
    }
}
