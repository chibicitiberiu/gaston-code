/* Running a command on this machine and streaming its output back.
 *
 * Derived from run_capture() in probe.c, which is what experiment 0.9 proved
 * on real Windows 98 SE: command.com /c ver returned 36 bytes, dir c:\ /w 352,
 * and `echo hello | find "hello"` came back correct through the known-bad
 * 16-bit FIND. Capture works; that was never the question.
 *
 * It is a rewrite rather than a lift, because probe.c's version has six
 * defects that do not matter in a probe and matter a great deal here:
 *
 *   1. a hardcoded 20 second timeout with no parameter;
 *   2. `char cmd[512]` filled with _snprintf, which TRUNCATES SILENTLY -- and
 *      a truncated command is still executed, so `del c:\temp\*.tmp` cut short
 *      becomes `del c:\temp\*`. Rejected here instead;
 *   3. a drain loop that stops reading once the buffer is full, at which point
 *      the child blocks writing to a pipe nobody empties and the whole thing
 *      spins until the timeout. Streaming removes the buffer entirely;
 *   4. hStdInput = NULL, so a child that reads stdin gets an invalid handle
 *      rather than a defined EOF. (This does NOT fix interactive prompts --
 *      see the measured note at the CreatePipe call.);
 *   5. no working directory;
 *   6. NUL-terminating the output, which truncates binary at the first zero.
 *
 * Memory: ONE 8KB chunk, not the 32KB the plan assumed. Output is streamed
 * as it is read, so the total is a counter rather than an allocation --
 * which is what makes this fit on a machine measured at 28KB free after a
 * turn. It is on the heap for the life of one run() rather than a local,
 * because the default _beginthreadex stack commit is not somewhere to put
 * 8KB, and rather than a static, because libexec holds no module state.
 */

#include "exec/execjob.h"

#include <stdio.h>
#include <string.h>
#include "core/log.h"
#include "core/str.h"

#define EXEC_CHUNK   8192
/* Long enough for any real command line; anything longer is rejected, never
 * silently shortened. COMMAND.COM itself gives up well before this. */
#define EXEC_MAX_CMD 1024

ExecJob::ExecJob()
    : m_cancel(0),
      m_child(NULL)
{
    InitializeCriticalSection(&m_lock);
}

ExecJob::~ExecJob()
{
    DeleteCriticalSection(&m_lock);
}

/* Sticky: set before run() starts, it stops run() from spawning at all, so
 * there is no window between "decided to run" and "spawned" for a cancel to
 * fall into -- the race rc_exec_arm() used to narrow. */
void ExecJob::cancel()
{
    m_cancel = 1;
    ScopedLock lock(&m_lock);
    if (m_child)
        TerminateProcess(m_child, 1);
}

void ExecJob::setChild(HANDLE h)
{
    ScopedLock lock(&m_lock);
    m_child = h;
}

/* The shell is spliced, never chosen: see ExecOptions::shell. */
bool ExecJob::run(const char *command, const ExecOptions &opt,
                  OutputSink &sink, ExecResult &out)
{
    out = ExecResult();

    if (!command || !*command)
        return false;

    /* Build the command line, and REFUSE rather than truncate.
     *
     * `rc_strf` returns 1 if the whole string fit and 0 if it was truncated
     * (win/core/str.h). This guard once read `n < 0 || n >= sizeof(cmd)`,
     * which is `_snprintf`'s contract, not this one, so it never fired and an
     * over-long command was cut at byte 1023 and run: `del c:\temp\*.tmp`
     * cut short becomes `del c:\temp\*`. The bridge applies no length cap of
     * its own, so this is the only guard in the system; execcheck drives it
     * for real. */
    char cmd[EXEC_MAX_CMD];
    int fit;
    if (opt.shell && *opt.shell)
        fit = rc_strf(cmd, sizeof(cmd), "%s %s", opt.shell, command);
    else
        fit = rc_strf(cmd, sizeof(cmd), "%s", command);
    if (!fit)
    {
        rc_log("exec: command too long (%u bytes), refused",
               (unsigned)strlen(command));
        out.status = EXEC_TOO_LONG;
        return false;
    }

    if (m_cancel)
    {
        out.status = EXEC_CANCELLED;
        return false;
    }

    HeapPtr<char> chunk((char *)HeapAlloc(GetProcessHeap(), 0, EXEC_CHUNK));
    if (!chunk.valid())
    {
        out.error = ERROR_NOT_ENOUGH_MEMORY;
        rc_log("exec: no memory for the output chunk");
        return false;
    }

    /* Guarded, and the CLOSE TIMING is still explicit where it is
     * load-bearing: `wr` and `inRd` must survive CreateProcess (the child
     * inherits them) and must be gone immediately after it, or ReadFile on
     * `rd` never sees EOF because this process still holds the write end.
     * A destructor cannot express "close here, in the middle"; reset() can,
     * and the guard still covers every path out. */
    SECURITY_ATTRIBUTES sa;
    memset(&sa, 0, sizeof(sa));
    sa.nLength = sizeof(sa);
    sa.bInheritHandle = TRUE;
    HANDLE rdH = NULL, wrH = NULL, inRdH = NULL, inWrH = NULL;
    if (!CreatePipe(&rdH, &wrH, &sa, 16384))
    {
        out.error = GetLastError();
        return false;
    }
    Handle rd(rdH), wr(wrH);

    /* An empty, immediately-closed stdin.
     *
     * This does NOT answer COMMAND.COM's "Are you sure (Y/N)?". Measured on
     * the guest: `del c:\stdintst\*.*` timed out. COMMAND.COM reads those
     * confirmations from CON via INT 21h, not from handle 0, so no stdin
     * arrangement reaches them; an interactive command hangs until the
     * timeout, and the tool description says so. The EOF pipe earns its
     * place anyway: a child that genuinely reads handle 0 (`sort`, `more`,
     * `find` with no pipe) gets a defined immediate EOF instead of an
     * invalid handle. */
    if (!CreatePipe(&inRdH, &inWrH, &sa, 256))
    {
        out.error = GetLastError();
        return false;
    }
    Handle inRd(inRdH), inWr(inWrH);
    inWr.reset();               /* EOF the moment the child reads */

    /* The read end must not be inherited, or the pipe never reports EOF.
     * SetHandleInformation is the obvious call and it is Win2000+ -- a single
     * NT-only STATIC import kills the whole image at load on Win9x, before
     * WinMain, with no log written and nothing to debug. DuplicateHandle is
     * the pre-2000 way and works everywhere. */
    {
        HANDLE dup = NULL;
        if (DuplicateHandle(GetCurrentProcess(), rd.get(), GetCurrentProcess(),
                            &dup, 0, FALSE, DUPLICATE_SAME_ACCESS))
            rd.reset(dup);      /* closes the inheritable original */
    }

    STARTUPINFOA si;
    memset(&si, 0, sizeof(si));
    si.cb = sizeof(si);
    si.dwFlags = STARTF_USESTDHANDLES | STARTF_USESHOWWINDOW;
    si.wShowWindow = SW_HIDE;   /* CREATE_NO_WINDOW is invalid for 16-bit */
    si.hStdOutput = wr.get();
    si.hStdError = wr.get();
    si.hStdInput = inRd.get();

    PROCESS_INFORMATION pi;
    DWORD start = GetTickCount();
    if (!CreateProcessA(NULL, cmd, NULL, NULL, TRUE, 0, NULL,
                        (opt.cwd && *opt.cwd) ? opt.cwd : NULL, &si, &pi))
    {
        out.error = GetLastError();
        rc_log("exec: CreateProcess failed: %lu (%s)",
               (unsigned long)out.error, cmd);
        return false;           /* rd/wr/inRd close on the way out */
    }
    Handle process(pi.hProcess);
    Handle thread(pi.hThread);
    wr.reset();                 /* so ReadFile ends when the child exits */
    inRd.reset();
    setChild(process.get());
    out.status = EXEC_OK;

    for (;;)
    {
        DWORD avail = 0, got = 0;

        if (m_cancel)
        {
            out.status = EXEC_CANCELLED;
            break;
        }
        if (GetTickCount() - start > opt.timeoutMs)
        {
            out.status = EXEC_TIMEOUT;
            break;
        }

        if (PeekNamedPipe(rd.get(), NULL, 0, NULL, &avail, NULL) && avail > 0)
        {
            DWORD want = avail > EXEC_CHUNK ? EXEC_CHUNK : avail;
            if (ReadFile(rd.get(), chunk.get(), want, &got, NULL) && got)
            {
                /* Keep DRAINING past the cap. Stopping would leave the child
                 * blocked writing to a pipe nobody empties, and we would sit
                 * until the timeout with a half-finished command. */
                deliver(chunk.get(), got, opt, sink, out);
                continue;       /* drain hard before waiting again */
            }
        }

        if (WaitForSingleObject(process.get(), 100) == WAIT_OBJECT_0)
        {
            /* Drain what is still in the pipe after exit -- but BOUNDED.
             *
             * Unbounded, this loop never ends if a grandchild inherited the
             * write end and keeps writing: COMMAND.COM backgrounding
             * something, or a pipeline's second stage outliving the shell.
             * The kill reaches the shell, not the grandchild, so the loop
             * must read the cancel flag and the clock itself. */
            while (PeekNamedPipe(rd.get(), NULL, 0, NULL, &avail, NULL) &&
                   avail)
            {
                DWORD want = avail > EXEC_CHUNK ? EXEC_CHUNK : avail;
                if (m_cancel)
                {
                    out.status = EXEC_CANCELLED;
                    break;
                }
                if (GetTickCount() - start > opt.timeoutMs)
                {
                    out.status = EXEC_TIMEOUT;
                    break;
                }
                if (out.bytes >= opt.maxOutput)
                {
                    /* Past the cap there is nothing to gain by reading on. */
                    out.truncated = true;
                    break;
                }
                if (!ReadFile(rd.get(), chunk.get(), want, &got, NULL) || !got)
                    break;
                deliver(chunk.get(), got, opt, sink, out);
            }
            break;
        }
    }

    /* A cancel kills the child from another thread, so the loop above can
     * see the process EXIT before it sees the flag, and would call a killed
     * command a finished one. The flag is the truth. (Found by execcheck's
     * cancel-mid-run: an EXECKILL could come back as status=ok.) */
    if (m_cancel && out.status == EXEC_OK)
        out.status = EXEC_CANCELLED;

    if (out.status == EXEC_TIMEOUT || out.status == EXEC_CANCELLED)
        TerminateProcess(process.get(), 1);

    GetExitCodeProcess(process.get(), &out.exitCode);
    out.ms = GetTickCount() - start;
    setChild(NULL);
    return true;
}

/* Hand what fits under the cap to the sink, and count all of it. */
void ExecJob::deliver(const char *data, DWORD got, const ExecOptions &opt,
                      OutputSink &sink, ExecResult &out)
{
    if (out.bytes >= opt.maxOutput)
    {
        out.truncated = true;
    }
    else
    {
        DWORD room = opt.maxOutput - out.bytes;
        DWORD use = got > room ? room : got;
        sink.onOutput(data, use);
        if (use < got)
            out.truncated = true;
    }
    out.bytes += got;
}
