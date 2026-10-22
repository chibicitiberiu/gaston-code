/* One thread, with a stack size somebody chose.
 *
 * REMOTE.md's libcore row has listed a "thread helper over _beginthreadex"
 * since the library split was planned; this is it, and gremote is why it
 * finally matters. Every thread in the tree until now was started by hand,
 * and all eight sites pass 0 for the stack -- so every one of them takes the
 * image default. MEASURED on build/win/gaston.exe: 1 MB reserve,
 * **64 KB commit**, per thread. gremote has six (its message loop, the accept
 * loop, Connection's reader and sender, the job worker, the fs worker), which
 * is 384 KB of committed stack on a 32 MB machine before it has done
 * anything.
 *
 * WHAT THE SIZE MEANS is not the same on both platforms, and the difference
 * is worth knowing before picking a number. On 9x the value sizes the
 * RESERVATION. On NT and Wine it sizes the initial COMMIT and the reservation
 * still comes from the PE header, unless STACK_SIZE_PARAM_IS_A_RESERVATION is
 * passed -- which is XP and later, so never here. Either way a smaller number
 * is less memory actually taken, which is the thing being bought.
 *
 * HOW SMALL IS SAFE is decided by a gate, not by guesswork: `make check`
 * refuses any function whose frame reaches 4096 bytes (one page), because a
 * bigger one can step over the guard page -- the fault that crashed
 * AUTOCHK.EXE on the real machine while passing every test under Wine, where
 * the stack is committed up front and there is no guard page to jump over.
 * So a reserve of N bytes buys roughly N/4096 maximal frames, and the real
 * depth of these threads is a dispatch, a handler and a syscall. Numbers
 * below the default are still confirmed against runtime working set on the
 * guest rather than against this arithmetic.
 *
 * Two rules this encodes that were previously repeated in comments at every
 * site: _beginthreadex and never CreateThread, because Open Watcom's
 * multi-threaded CRT allocates its per-thread block there and a raw
 * CreateThread thread never gets one and never releases it; and the
 * thread-id out-parameter must be non-NULL, because Win95/98 reject
 * CreateThread(..., NULL) with ERROR_INVALID_PARAMETER while NT accepts it.
 * That second one silently disabled every helper thread in retro-agent for
 * years (research/measurements/p200-run1-20260926.txt, step 0.11).
 */
#ifndef RC_CORE_THREAD_H
#define RC_CORE_THREAD_H

#ifndef __cplusplus
#error "core/thread.h is C++"
#endif

#include <windows.h>
#include "core/rc_cpp.h"

/* What a thread runs. One method, with the object as its context: no
 * context-free function pointer, which is the pattern that forces globals. */
class Runnable
{
public:
    virtual ~Runnable() {}

    /* Returns when the thread should end. An exception other than
     * std::bad_alloc leaving this ends the process with no log, so do not
     * use exceptions for control flow here (nothing in this tree does). */
    virtual void run() = 0;
};

class Thread
{
public:
    Thread();

    /* Closes the handle and does NOT wait. A thread still running here is
     * the owner's bug: closing a handle does not end a thread, so it would
     * run on and touch a destroyed Runnable. join() first, and release() it
     * if it did not stop -- joinOrLeak() in core/app.h is that rule. */
    ~Thread() {}

    /* `body` must outlive the thread -- ordinarily it is the owner itself.
     * `name` is a literal, used in the log and in the out-of-memory report,
     * so it should say which thread this is ("fs worker"), not which class.
     * `stackBytes` of 0 takes the image default; see the note above before
     * choosing anything else. False, logged, if the thread did not start. */
    bool start(Runnable &body, const char *name, unsigned stackBytes);

    bool started() const { return m_thread.valid() != 0; }

    /* True if the thread has ended (or never started, so a service whose
     * start() failed can still be joined). False means it is still running,
     * and the caller must LEAK rather than free what it touches --
     * joinOrLeak() in core/app.h is that rule.
     *
     * A successful join closes the handle, so started() is false afterwards
     * and joining again is a no-op. That is what lets an owner's destructor
     * ask "is a thread handle still open here?" and mean it. */
    bool join(DWORD timeoutMs);

    /* Stop owning the handle without waiting. For the leak path only. */
    void release() { m_thread.release(); }

private:
    static unsigned __stdcall entry(void *self);

    Thread(const Thread &);
    Thread &operator=(const Thread &);

    Handle      m_thread;
    Runnable   *m_body;
    const char *m_name;
};

#endif
