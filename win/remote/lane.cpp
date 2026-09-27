#include "remote/lane.h"

#include "core/log.h"
#include "core/str.h"

/* How long the worker sleeps between looks at its stop flag when there is
 * nothing to do. The wake event makes the busy path immediate; this only
 * bounds how long a stop waits on an idle lane. */
enum { LANE_IDLE_SLICE_MS = 100 };

LaneWorker::LaneWorker()
    : m_handler(0), m_lockUp(false), m_slot(0), m_running(0), m_inFlight(0),
      m_startedAt(0)
{
    m_op[0] = '\0';
}

LaneWorker::~LaneWorker()
{
    /* Reached only after join() returned true, so nothing is in here. A
     * frame still in the slot would be one nobody ever answered, which is
     * why the loop drains it on the way out instead. */
    if (m_slot)
        rc_frame_delete(m_slot);
    if (m_lockUp)
        DeleteCriticalSection(&m_lock);
}

bool LaneWorker::start(LaneHandler &handler, const char *name,
                       unsigned stackBytes)
{
    m_handler = &handler;
    m_wake.reset(CreateEventA(NULL, FALSE, FALSE, NULL));
    if (!m_wake.valid())
    {
        rc_log("lane: %s could not make its wake event", name);
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

void LaneWorker::signalStop()
{
    m_running = 0;
    if (m_wake.valid())
        SetEvent(m_wake.get());
}

bool LaneWorker::join(DWORD timeoutMs)
{
    return m_thread.join(timeoutMs);
}

void LaneWorker::Body::run()
{
    m_lane->loop();
}

void LaneWorker::loop()
{
    for (;;)
    {
        rc_frame *f = 0;
        bool stopping;

        WaitForSingleObject(m_wake.get(), LANE_IDLE_SLICE_MS);
        {
            ScopedLock lock(&m_lock);
            f = m_slot;
            m_slot = 0;
            stopping = m_running == 0;
        }

        if (f)
        {
            /* Outside the lock: a handler runs for as long as the disk or a
             * child takes, and holding the lock across that would make
             * submit() -- on the reader thread -- wait for it. */
            if (stopping)
                m_handler->abandon(f);
            else
                m_handler->handle(f);
            ScopedLock lock(&m_lock);
            m_inFlight = 0;
            m_op[0] = '\0';
        }
        if (stopping)
            break;
    }

    /* One last look, for a frame handed over between the drain above and
     * the stop flag being read. It gets an answer, not a drop. */
    {
        rc_frame *f;
        ScopedLock lock(&m_lock);
        f = m_slot;
        m_slot = 0;
        if (f)
        {
            LeaveCriticalSection(&m_lock);
            m_handler->abandon(f);
            EnterCriticalSection(&m_lock);
        }
        m_inFlight = 0;
    }
}
