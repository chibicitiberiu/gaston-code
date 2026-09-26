/* libexec under Wine: ExecJob driven for real, one case per property.
 *
 * Run by tests/python/guest/test_guest_exec.py. Prints
 * `verdict<TAB>case<TAB>detail` rows.
 *
 * Wine has no COMMAND.COM (its prefix reports 9x, but ships cmd.exe), so the
 * commands that need a shell name cmd.exe in the body and leave `shell`
 * empty -- the same sidestep rpccheck uses. Which shell runs is not what
 * these cases are about; the guest behaviour of COMMAND.COM is measured by
 * execprobe on the VM instead (docs/measurements/execprobe-vm-*).
 */
#include <windows.h>
#include <process.h>
#include <stdio.h>
#include <string.h>
#include <string>
#include "core/rc_cpp.h"
#include "core/log.h"
#include "core/str.h"
#include "exec/execjob.h"

static int g_failures;

static void say(const char *name, bool pass, const char *detail)
{
    if (!pass)
        g_failures++;
    printf("%s\t%s\t%s\n", pass ? "pass" : "FAIL", name, detail);
    fflush(stdout);
}

/* Keeps what the job hands over. */
class Collect : public OutputSink
{
public:
    Collect() : m_calls(0) {}
    void onOutput(const char *data, unsigned len)
    {
        m_text.append(data, len);
        m_calls++;
    }
    std::string m_text;
    int m_calls;
};

/* A command that never ends by itself: FOR /L inside cmd.exe's own
 * interpreter, no external process. */
static const char *FOREVER = "cmd.exe /c for /l %i in (1,1,50000000) do rem";

/* The guard that refuses rather than truncates. It once read
 * `n < 0 || n >= sizeof(cmd)` -- _snprintf's contract, not rc_strf's -- so
 * it never fired and a command over 1024 bytes was cut at byte 1023 and run:
 * `del c:\temp\*.tmp` cut short becomes `del c:\temp\*`. The bridge applies
 * no length cap of its own, so this is the only guard in the system. Nothing
 * is spawned: the refusal returns before CreateProcessA. (audit 01.3) */
static void tooLong()
{
    static char big[4096];
    char detail[64];
    memset(big, 'x', sizeof(big) - 1);
    big[sizeof(big) - 1] = '\0';
    {
        ExecJob job;
        ExecOptions opt;
        ExecResult r;
        Collect c;
        bool spawned = job.run(big, opt, c, r);
        rc_strf(detail, sizeof detail, "spawned=%d status=%d", spawned,
                (int)r.status);
        say("overlong-refused", !spawned && r.status == EXEC_TOO_LONG, detail);
    }
    {
        /* The shell form crosses the limit at a LOWER input length -- the
         * branch a fix could easily miss. */
        memset(big, 'y', 1100);
        big[1100] = '\0';
        ExecJob job;
        ExecOptions opt;
        opt.shell = "C:\\WINDOWS\\COMMAND.COM /c";
        ExecResult r;
        Collect c;
        bool spawned = job.run(big, opt, c, r);
        rc_strf(detail, sizeof detail, "spawned=%d status=%d", spawned,
                (int)r.status);
        say("overlong-with-shell-refused",
            !spawned && r.status == EXEC_TOO_LONG, detail);
    }
    {
        /* Without this, a guard that refuses everything passes. */
        ExecJob job;
        ExecOptions opt;
        ExecResult r;
        Collect c;
        job.run("", opt, c, r);
        rc_strf(detail, sizeof detail, "status=%d", (int)r.status);
        say("short-not-refused", r.status != EXEC_TOO_LONG, detail);
    }
}

static void output()
{
    char detail[96];
    {
        ExecJob job;
        ExecOptions opt;
        ExecResult r;
        Collect c;
        bool spawned = job.run("cmd.exe /c echo hello", opt, c, r);
        bool ok = spawned && r.status == EXEC_OK &&
                  c.m_text.find("hello") != std::string::npos &&
                  !r.truncated && r.bytes == c.m_text.size();
        rc_strf(detail, sizeof detail, "status=%d bytes=%lu got=%u", (int)r.status,
                (unsigned long)r.bytes, (unsigned)c.m_text.size());
        say("output-arrives", ok, detail);
    }
    {
        ExecJob job;
        ExecOptions opt;
        ExecResult r;
        Collect c;
        job.run("cmd.exe /c exit 7", opt, c, r);
        rc_strf(detail, sizeof detail, "status=%d code=%lu", (int)r.status,
                (unsigned long)r.exitCode);
        say("exit-code", r.status == EXEC_OK && r.exitCode == 7, detail);
    }
    {
        /* Past the cap the sink hears nothing more, the rest is still drained
         * and counted, and the result says it was cut. */
        ExecJob job;
        ExecOptions opt;
        opt.maxOutput = 10;
        ExecResult r;
        Collect c;
        job.run("cmd.exe /c echo 0123456789abcdefghijklmnopqrstuvwxyz", opt, c,
                r);
        bool ok = r.status == EXEC_OK && c.m_text.size() == 10 &&
                  r.truncated && r.bytes > 10;
        rc_strf(detail, sizeof detail, "got=%u bytes=%lu truncated=%d",
                (unsigned)c.m_text.size(), (unsigned long)r.bytes,
                r.truncated);
        say("cap-truncates", ok, detail);
    }
    {
        ExecJob job;
        ExecOptions opt;
        ExecResult r;
        Collect c;
        bool spawned = job.run("C:\\NO\\SUCH\\PROGRAM.EXE", opt, c, r);
        rc_strf(detail, sizeof detail, "spawned=%d status=%d error=%lu",
                spawned, (int)r.status, (unsigned long)r.error);
        say("spawn-failed", !spawned && r.status == EXEC_SPAWN_FAILED &&
                                r.error != 0, detail);
    }
}

struct Canceller
{
    ExecJob *job;
    DWORD afterMs;
};

static unsigned __stdcall cancelLater(void *p)
{
    Canceller *c = (Canceller *)p;
    Sleep(c->afterMs);
    c->job->cancel();
    return 0;
}

static void stopping()
{
    char detail[96];
    {
        ExecJob job;
        ExecOptions opt;
        opt.timeoutMs = 1000;
        ExecResult r;
        Collect c;
        job.run(FOREVER, opt, c, r);
        rc_strf(detail, sizeof detail, "status=%d ms=%lu", (int)r.status,
                (unsigned long)r.ms);
        say("timeout", r.status == EXEC_TIMEOUT && r.ms >= 1000 && r.ms < 6000,
            detail);
    }
    {
        /* A cancel that lands before run() means nothing is spawned: the
         * race rc_exec_arm() used to narrow is gone, not narrowed. */
        ExecJob job;
        job.cancel();
        ExecOptions opt;
        ExecResult r;
        Collect c;
        DWORD t0 = GetTickCount();
        bool spawned = job.run(FOREVER, opt, c, r);
        DWORD spent = GetTickCount() - t0;
        rc_strf(detail, sizeof detail, "spawned=%d status=%d in %lums",
                spawned, (int)r.status, (unsigned long)spent);
        say("cancel-before-run", !spawned && r.status == EXEC_CANCELLED &&
                                     spent < 1000, detail);
    }
    {
        ExecJob job;
        Canceller cl;
        cl.job = &job;
        cl.afterMs = 500;
        unsigned tid;
        Handle t((HANDLE)_beginthreadex(NULL, 0, cancelLater, &cl, 0, &tid));
        ExecOptions opt;
        opt.timeoutMs = 20000;
        ExecResult r;
        Collect c;
        job.run(FOREVER, opt, c, r);
        WaitForSingleObject(t.get(), 5000);
        rc_strf(detail, sizeof detail, "status=%d ms=%lu", (int)r.status,
                (unsigned long)r.ms);
        say("cancel-mid-run", r.status == EXEC_CANCELLED && r.ms < 5000,
            detail);
    }
}

int main(void)
{
    rc_log_open(".");
    tooLong();
    output();
    stopping();
    rc_log_close();
    return g_failures ? 1 : 0;
}
