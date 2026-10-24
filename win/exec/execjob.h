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
 * cancel() is for any other thread. notes/libexec-plan.md has the design and
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

/* Where the output goes. Called on run()'s thread, zero or more times.
 * Returning false means nobody will ever read the output again (the
 * caller's connection is gone): the job stops the command and returns
 * EXEC_SINK_GONE instead of running to its timeout for no one. A sink that
 * only lost a chunk returns true and records the hole itself. */
class OutputSink
{
public:
    virtual ~OutputSink() {}
    virtual bool onOutput(const char *data, unsigned len) = 0;
};

enum ExecStatus
{
    EXEC_OK = 0,
    EXEC_TIMEOUT,
    EXEC_SPAWN_FAILED,
    EXEC_CANCELLED,
    EXEC_TOO_LONG,
    EXEC_SINK_GONE
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
    /* The command ran through ExecOptions::shell. A fact, not a verdict on
     * exitCode: COMMAND.COM /c on 98 always exits 0 (measured,
     * research/measurements/execprobe-vm-gui-20260926.txt), cmd.exe passes the
     * child's code through, and which shell ran is the caller's knowledge,
     * never this library's. */
    bool viaShell;
    /* The command was stopped (timeout, cancel, reader gone) and the process
     * run() started was still there afterwards. On 98 that is REDIR32.EXE,
     * which nothing short of a reboot removes (notes/BACKLOG.md 14); the
     * caller logs it. */
    bool stopLeaked;

    ExecResult()
        : status(EXEC_SPAWN_FAILED), exitCode(0), bytes(0), ms(0), error(0),
          truncated(false), viaShell(false), stopLeaked(false)
    {
    }
};

class ExecJob
{
public:
    ExecJob();

    /* Run `command` to completion, timeout or cancel. False when nothing
     * was spawned (empty, too long, spawn failed); `out` says which. */
    bool run(const char *command, const ExecOptions &opt, OutputSink &sink,
             ExecResult &out);

    /* Any thread, any time, idempotent. A cancel before run() means run()
     * spawns nothing. It only SIGNALS: the stop -- a Toolhelp walk and a
     * bounded wait -- runs on run()'s thread, never on the caller's, which
     * in the client is the reader. */
    void cancel();

    bool cancelled() const { return m_cancel != 0; }

private:
    ExecJob(const ExecJob &);
    ExecJob &operator=(const ExecJob &);

    static bool deliver(const char *data, DWORD got, const ExecOptions &opt,
                        OutputSink &sink, ExecResult &out);

    void stop(const PROCESS_INFORMATION &pi, ExecResult &out);

    volatile LONG m_cancel;
    Handle m_wake;          /* set by cancel(), waited on by run() */
};

#endif
