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
 * Memory: ONE 8KB static buffer, not the 32KB the plan assumed. Output is
 * streamed as it is read, so the total is a counter rather than an
 * allocation -- which is what makes this fit on a machine measured at 28KB
 * free after a turn. It is static rather than a local because the default
 * _beginthreadex stack commit is not somewhere to put 8KB.
 */

#include <windows.h>
#include <stdio.h>
#include <string.h>
#include "rpc/exec.h"
#include "core/log.h"
#include "core/str.h"
#include "core/rc_cpp.h"

#define EXEC_CHUNK   8192
#define EXEC_MAX_OUT (32u * 1024u)
/* Long enough for any real command line; anything longer is rejected, never
 * silently shortened. COMMAND.COM itself gives up well before this. */
#define EXEC_MAX_CMD 1024

static char g_chunk[EXEC_CHUNK];

/* Set by rc_exec_cancel() from another thread to kill a running child. */
static volatile LONG g_cancel;
static HANDLE        g_child;      /* guarded by g_child_lock */
static CRITICAL_SECTION g_child_lock;
static int           g_child_lock_ready;

void rc_exec_init(void)
{
    if (!g_child_lock_ready) {
        InitializeCriticalSection(&g_child_lock);
        g_child_lock_ready = 1;
    }
}

/* Clear a stale cancel before starting a new command. Call it BEFORE
 * re-checking whether you should still be running: that ordering means a
 * cancel landing after the arm is seen by the run loop, and one landing before
 * it is seen by the caller's own running check. */
void rc_exec_arm(void)
{
    g_cancel = 0;
}

void rc_exec_cancel(void)
{
    g_cancel = 1;
    /* Reachable, and correct: no section means rc_exec_init never ran, which
     * means rc_rpc_start never ran, which means there is no child. g_cancel
     * is set above regardless, so a worker that starts later still sees it. */
    if (!g_child_lock_ready) return;
    {
        ScopedLock lock(&g_child_lock);
        if (g_child) TerminateProcess(g_child, 1);
    }
}

/* Worker thread only, and it cannot run before the section exists:
 * rc_rpc_start calls rc_exec_init() at rpc.cpp:599 and only creates the
 * worker at :612, both on the UI thread. So the branch below is unreachable
 * today. It used to be a silent UNLOCKED write to g_child, which read like
 * the one unsynchronised access in this file; it says so now instead, so a
 * change to that ordering announces itself rather than quietly dropping the
 * lock. (mem-audit L5) */
static void set_child(HANDLE h)
{
    if (!g_child_lock_ready) {
        rc_log("exec: set_child before rc_exec_init -- start-up ordering "
               "changed; g_child is being written without the lock");
        g_child = h;
        return;
    }
    {
        ScopedLock lock(&g_child_lock);
        g_child = h;
    }
}

/* THE SHELL USED TO BE CHOSEN HERE.
 *
 *     static const char *shell_name(void)
 *     {
 *         return (GetVersion() & 0x80000000) ? "command.com" : "cmd.exe";
 *     }
 *
 * It was called "the only place the guest's vintage should appear", and it
 * was one place too many. A machine has as many shells as it has -- 4DOS
 * beside COMMAND.COM here, sh/bash/zsh/csh on the Unix box this surface is
 * supposed to survive being pointed at -- and a test that returns one name
 * cannot express that. Worse, it was a decision made where nobody could see
 * it: the bridge's command guard judged each command against a grammar it
 * had guessed at, which on a two-shell machine need not be the grammar that
 * would run it.
 *
 * So the machine now REPORTS its shells (detection, HELLO's body), the model
 * picks one per call from that list, and the bridge sends the invocation
 * prefix on the EXEC frame. This file splices a string and knows nothing
 * else -- which is what "the guest transforms nothing" means when applied to
 * the one decision it was still making. */
int rc_exec_run(const char *command, const char *cwd, const char *shell,
                DWORD timeout_ms, rc_exec_sink sink, void *ctx,
                rc_exec_result *out)
{
    SECURITY_ATTRIBUTES sa;
    STARTUPINFOA si;
    PROCESS_INFORMATION pi;
    /* Guarded, and the CLOSE TIMING is still explicit where it is
     * load-bearing: `wr` and `in_rd` must survive CreateProcess (the child
     * inherits them) and must be gone immediately after it, or ReadFile on
     * `rd` never sees EOF because this process still holds the write end.
     * A destructor cannot express "close here, in the middle"; reset() can,
     * and the guard still covers every path out. */
    HANDLE rd_h = NULL, wr_h = NULL, in_rd_h = NULL, in_wr_h = NULL;
    DWORD start;
    char cmd[EXEC_MAX_CMD];
    int fit;

    memset(out, 0, sizeof(*out));
    out->status = RC_EXEC_SPAWN_FAILED;
    /* NOT `g_cancel = 0` here. Clearing the flag on entry swallowed a cancel
     * raised between the worker dequeueing a frame and this call starting --
     * shutdown would ask to stop, the flag would be wiped, and the command
     * would spawn anyway and run its full clamp while the app tried to exit.
     * rc_exec_arm() is called by the worker before it decides to run. */

    if (!command || !*command) return -1;

    /* Build the command line, and REFUSE rather than truncate.
     *
     * `rc_strf` returns 1 if the whole string fit and 0 if it was truncated
     * (win/core/str.h). It never returns a length and never a negative. This
     * guard used to read `if (n < 0 || n >= (int)sizeof(cmd))`, which is
     * `_snprintf`'s contract, not this one -- neither 0 nor 1 can satisfy
     * either half, so the guard never fired and RC_EXEC_TOO_LONG was
     * unreachable. An over-long command was truncated at byte 1023 and handed
     * to the shell, which is exactly the defect this file's own header (see
     * the top of this file, item 2) says was "rejected here instead":
     * `del c:\temp\*.tmp` cut short becomes `del c:\temp\*`.
     *
     * The bridge applies no length cap of its own, so this is the only guard
     * in the system. Test it through rc_exec_run rather than trusting the
     * shape of the expression -- see win/checks/rpccheck.c. */
    if (shell && *shell)
        fit = rc_strf(cmd, sizeof(cmd), "%s %s", shell, command);
    else
        fit = rc_strf(cmd, sizeof(cmd), "%s", command);
    if (!fit) {
        rc_log("exec: command too long (%u bytes), refused",
               (unsigned)strlen(command));
        out->status = RC_EXEC_TOO_LONG;
        return -1;
    }

    memset(&sa, 0, sizeof(sa));
    sa.nLength = sizeof(sa);
    sa.bInheritHandle = TRUE;
    if (!CreatePipe(&rd_h, &wr_h, &sa, 16384)) return -1;
    Handle rd(rd_h), wr(wr_h);

    /* An empty, immediately-closed stdin.
     *
     * The justification originally written here was WRONG, and measuring it
     * is the only reason we know. The claim was that a pipe at EOF answers
     * COMMAND.COM's "Are you sure (Y/N)?" where probe.c's NULL left the child
     * hanging. Measured on the guest:
     *
     *     del c:\stdintst\*.*  ->  status=timeout, killed at the limit
     *
     * It does not answer the prompt. COMMAND.COM reads those confirmations
     * from CON via INT 21h, not from handle 0, so no stdin arrangement
     * reaches them. An interactive command WILL hang until the timeout kills
     * it, and the tool description says so.
     *
     * The EOF pipe is kept anyway, for the reason it actually earns: a child
     * that genuinely reads handle 0 (`sort`, `more`, `find` with no pipe)
     * gets a defined immediate EOF instead of an invalid handle. That is a
     * real improvement over NULL; answering prompts never was. */
    if (!CreatePipe(&in_rd_h, &in_wr_h, &sa, 256)) return -1;
    Handle in_rd(in_rd_h), in_wr(in_wr_h);
    in_wr.reset();              /* EOF the moment the child reads */

    /* The read end must not be inherited, or the pipe never reports EOF.
     * SetHandleInformation is the obvious call and it is Win2000+ -- a single
     * NT-only STATIC import kills the whole image at load on Win9x, before
     * WinMain, with no log written and nothing to debug. DuplicateHandle is
     * the pre-2000 way and works everywhere. */
    {
        HANDLE dup = NULL;
        if (DuplicateHandle(GetCurrentProcess(), rd.get(), GetCurrentProcess(),
                            &dup, 0, FALSE, DUPLICATE_SAME_ACCESS)) {
            rd.reset(dup);      /* closes the inheritable original */
        }
    }

    memset(&si, 0, sizeof(si));
    si.cb = sizeof(si);
    si.dwFlags = STARTF_USESTDHANDLES | STARTF_USESHOWWINDOW;
    si.wShowWindow = SW_HIDE;   /* CREATE_NO_WINDOW is invalid for 16-bit */
    si.hStdOutput = wr.get();
    si.hStdError  = wr.get();
    si.hStdInput  = in_rd.get();

    start = GetTickCount();
    if (!CreateProcessA(NULL, cmd, NULL, NULL, TRUE, 0, NULL,
                        (cwd && *cwd) ? cwd : NULL, &si, &pi)) {
        out->error = GetLastError();
        rc_log("exec: CreateProcess failed: %lu (%s)",
               (unsigned long)out->error, cmd);
        return -1;              /* rd/wr/in_rd close on the way out */
    }
    wr.reset();                 /* so ReadFile ends when the child exits */
    in_rd.reset();
    set_child(pi.hProcess);
    out->status = RC_EXEC_OK;

    for (;;) {
        DWORD avail = 0, got = 0;

        if (g_cancel) { out->status = RC_EXEC_CANCELLED; break; }
        if (GetTickCount() - start > timeout_ms) {
            out->status = RC_EXEC_TIMEOUT;
            break;
        }

        if (PeekNamedPipe(rd.get(), NULL, 0, NULL, &avail, NULL) && avail > 0) {
            DWORD want = avail > EXEC_CHUNK ? EXEC_CHUNK : avail;
            if (ReadFile(rd.get(), g_chunk, want, &got, NULL) && got) {
                if (out->bytes >= EXEC_MAX_OUT) {
                    /* Keep DRAINING past the cap. Stopping here is defect (3):
                     * the child blocks writing to a pipe nobody empties and we
                     * sit until the timeout with a half-finished command. */
                    out->truncated = 1;
                } else {
                    DWORD room = EXEC_MAX_OUT - out->bytes;
                    DWORD use = got > room ? room : got;
                    if (sink) sink(ctx, g_chunk, use);
                    if (use < got) out->truncated = 1;
                }
                out->bytes += got;
                continue;       /* drain hard before waiting again */
            }
        }

        if (WaitForSingleObject(pi.hProcess, 100) == WAIT_OBJECT_0) {
            /* Drain what is still in the pipe after exit -- but BOUNDED.
             *
             * Unbounded, this loop never ends if a grandchild inherited the
             * write end and keeps writing: COMMAND.COM backgrounding
             * something, or a pipeline's second stage outliving the shell.
             * TerminateProcess kills the shell, not the grandchild, and the
             * loop read no flag, so cancel could not break it either -- which
             * made exec.h's "cancellable from another thread" false exactly
             * here. The worker would hang and shutdown would GPF. */
            while (PeekNamedPipe(rd.get(), NULL, 0, NULL, &avail, NULL) && avail) {
                DWORD want = avail > EXEC_CHUNK ? EXEC_CHUNK : avail;
                if (g_cancel) { out->status = RC_EXEC_CANCELLED; break; }
                if (GetTickCount() - start > timeout_ms) {
                    out->status = RC_EXEC_TIMEOUT;
                    break;
                }
                if (out->bytes >= EXEC_MAX_OUT) {
                    /* Past the cap there is nothing to gain by reading on. */
                    out->truncated = 1;
                    break;
                }
                if (!ReadFile(rd.get(), g_chunk, want, &got, NULL) || !got) break;
                if (out->bytes < EXEC_MAX_OUT) {
                    DWORD room = EXEC_MAX_OUT - out->bytes;
                    DWORD use = got > room ? room : got;
                    if (sink) sink(ctx, g_chunk, use);
                    if (use < got) out->truncated = 1;
                } else {
                    out->truncated = 1;
                }
                out->bytes += got;
            }
            break;
        }
    }

    if (out->status == RC_EXEC_TIMEOUT || out->status == RC_EXEC_CANCELLED)
        TerminateProcess(pi.hProcess, 1);

    GetExitCodeProcess(pi.hProcess, &out->code);
    out->ms = GetTickCount() - start;
    set_child(NULL);
    CloseHandle(pi.hThread);
    CloseHandle(pi.hProcess);
    return 0;                   /* rd closes here */
}
