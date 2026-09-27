/* Thread. See thread.h for why the stack size is a parameter at all. */
#include <new>
#include <process.h>
#include "core/heap.h"
#include "core/log.h"
#include "core/thread.h"

Thread::Thread() : m_body(0), m_name("?")
{
}

bool Thread::start(Runnable &body, const char *name, unsigned stackBytes)
{
    unsigned tid = 0;

    if (m_thread.valid())
    {
        rc_log("thread: %s is already started", m_name);
        return false;
    }
    m_body = &body;
    m_name = name ? name : "?";
    /* &tid, not NULL: Win95/98 refuse a null thread-id slot. */
    m_thread.reset((HANDLE)_beginthreadex(NULL, stackBytes, entry, this, 0,
                                          &tid));
    if (!m_thread.valid())
    {
        rc_log("thread: could not start %s", m_name);
        m_body = 0;
        return false;
    }
    return true;
}

bool Thread::join(DWORD timeoutMs)
{
    if (!m_thread.valid())
        return true;
    if (WaitForSingleObject(m_thread.get(), timeoutMs) != WAIT_OBJECT_0)
    {
        rc_log("thread: %s did not stop within %lums", m_name,
               (unsigned long)timeoutMs);
        return false;
    }
    return true;
}

/* Catches std::bad_alloc, which operator new throws (core/heap.cpp): an
 * exception leaving a thread's entry point ends the process with no log,
 * which on this platform is the whole diagnostic gone. */
unsigned __stdcall Thread::entry(void *self)
{
    Thread *t = (Thread *)self;
    try
    {
        t->m_body->run();
    }
    catch (std::bad_alloc &)
    {
        rc_out_of_memory(t->m_name);
    }
    return 0;
}
