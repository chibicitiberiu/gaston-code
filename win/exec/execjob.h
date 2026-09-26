/* Run one command on this machine, streaming its output to a sink as it
 * arrives. libexec: depends on libcore only, and knows nothing of frames,
 * ids or lanes -- the caller maps requests to jobs and results to replies.
 *
 * Streaming rather than buffering is the whole design: the caller never holds
 * the output, so a 32KB result costs one 8KB chunk rather than 32KB of heap
 * on a machine with 28KB free.
 *
 * THREADS. run() blocks on the caller's thread for the life of the command;
 * the exe provides that thread (the client's RPC worker, gremote's job lane).
 * cancel() is for any other thread. docs/libexec-plan.md has the design and
 * the guest measurements behind it.
 *
 * One command per object: a job is made for a request and deleted after it,
 * so a cancel meant for one command can never reach the next.
 */
#ifndef RC_EXEC_EXECJOB_H
#define RC_EXEC_EXECJOB_H

#ifndef __cplusplus
#error "exec/execjob.h is C++"
#endif

#include <windows.h>
#include "core/rc_cpp.h"

/* Where the output goes. Called on run()'s thread, zero or more times. */
class OutputSink
{
public:
    virtual ~OutputSink() {}
    virtual void onOutput(const char *data, unsigned len) = 0;
};

enum ExecStatus
{
    EXEC_OK = 0,
    EXEC_TIMEOUT,
    EXEC_SPAWN_FAILED,
    EXEC_CANCELLED,
    EXEC_TOO_LONG
};

struct ExecOptions
{
    /* The invocation prefix spliced in front of the command, e.g.
     * "C:\\WINDOWS\\COMMAND.COM /c"; NULL or empty spawns it directly.
     *
     * THE GUEST DOES NOT CHOOSE. This used to be a flag, with the shell
     * picked here from GetVersion() -- so a machine with a second shell
     * (4DOS beside COMMAND.COM) could not be asked to use it, and the
     * bridge's command guard judged a command against whichever grammar it
     * had guessed rather than the one that would run it. The machine
     * reports which shells it HAS; the model picks one per call; this
     * splices what it is told and knows nothing else about shells. */
    const char *shell;
    const char *cwd;        /* NULL or empty: inherit ours */
    DWORD timeoutMs;        /* taken as given; clamping is the caller's policy */
    DWORD maxOutput;        /* bytes handed to the sink; the rest is drained */

    ExecOptions() : shell(0), cwd(0), timeoutMs(20000), maxOutput(32u * 1024u)
    {
    }
};

struct ExecResult
{
    ExecStatus status;
    DWORD exitCode;
    DWORD bytes;            /* total produced, including any discarded */
    DWORD ms;
    DWORD error;            /* GetLastError when spawning failed */
    bool truncated;         /* output exceeded maxOutput */

    ExecResult()
        : status(EXEC_SPAWN_FAILED), exitCode(0), bytes(0), ms(0), error(0),
          truncated(false)
    {
    }
};

class ExecJob
{
public:
    ExecJob();
    ~ExecJob();

    /* Run `command` to completion, timeout or cancel. False when nothing
     * was spawned (empty, too long, spawn failed); `out` says which. */
    bool run(const char *command, const ExecOptions &opt, OutputSink &sink,
             ExecResult &out);

    /* Any thread, any time, idempotent. A cancel before run() means run()
     * spawns nothing. */
    void cancel();

    bool cancelled() const { return m_cancel != 0; }

private:
    ExecJob(const ExecJob &);
    ExecJob &operator=(const ExecJob &);

    void setChild(HANDLE h);
    static void deliver(const char *data, DWORD got, const ExecOptions &opt,
                        OutputSink &sink, ExecResult &out);

    CRITICAL_SECTION m_lock;
    volatile LONG m_cancel;
    HANDLE m_child;         /* guarded by m_lock */
};

#endif
