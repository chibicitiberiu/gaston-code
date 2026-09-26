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
#include "exec/process.h"

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
    bool onOutput(const char *data, unsigned len)
    {
        m_text.append(data, len);
        m_calls++;
        return true;
    }
    std::string m_text;
    int m_calls;
};

/* A reader that goes away after the first chunk, as a dead link does. */
class Vanish : public OutputSink
{
public:
    Vanish() : m_calls(0) {}
    bool onOutput(const char *, unsigned)
    {
        m_calls++;
        return false;
    }
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
        /* Through a shell and not: recorded, never judged. */
        ExecJob a, b;
        ExecOptions viaShell, direct;
        viaShell.shell = "cmd.exe /c";
        ExecResult ra, rb;
        Collect ca, cb;
        a.run("echo hi", viaShell, ca, ra);
        b.run("cmd.exe /c echo hi", direct, cb, rb);
        rc_strf(detail, sizeof detail, "shell: status=%d via=%d; direct: via=%d",
                (int)ra.status, ra.viaShell, rb.viaShell);
        say("via-shell-recorded", ra.status == EXEC_OK && ra.viaShell &&
                                      !rb.viaShell &&
                                      ca.m_text.find("hi") != std::string::npos,
            detail);
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

static DWORD fileSize(const char *path)
{
    WIN32_FIND_DATAA fd;
    HANDLE h = FindFirstFileA(path, &fd);
    if (h == INVALID_HANDLE_VALUE)
        return 0;
    FindClose(h);
    return fd.nFileSizeLow;
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
        /* The stop reaches the whole tree. The loop runs in a SECOND
         * cmd.exe, a grandchild; killing only the one run() spawned -- all
         * this did before -- leaves it appending to the marker forever. */
        DeleteFileA("execcheck_tree.txt");
        ExecJob job;
        ExecOptions opt;
        opt.timeoutMs = 1500;
        ExecResult r;
        Collect c;
        job.run("cmd.exe /c cmd.exe /c for /l %i in (1,1,50000000) do "
                "echo t>>execcheck_tree.txt", opt, c, r);
        DWORD a = fileSize("execcheck_tree.txt");
        Sleep(800);
        DWORD b = fileSize("execcheck_tree.txt");
        rc_strf(detail, sizeof detail, "status=%d marker %lu -> %lu leaked=%d",
                (int)r.status, (unsigned long)a, (unsigned long)b,
                r.stopLeaked);
        say("stop-reaches-the-grandchild", r.status == EXEC_TIMEOUT && a > 0 &&
                                               a == b && !r.stopLeaked,
            detail);
        DeleteFileA("execcheck_tree.txt");
    }
    {
        /* A reader that is gone stops the command: without this the job
         * would run to its timeout producing output for no one, and in the
         * client the worker sat blocked in the sink meanwhile. */
        ExecJob job;
        ExecOptions opt;
        opt.timeoutMs = 20000;
        ExecResult r;
        Vanish v;
        job.run("cmd.exe /c for /l %i in (1,1,50000000) do echo x", opt, v, r);
        rc_strf(detail, sizeof detail, "status=%d calls=%d ms=%lu",
                (int)r.status, v.m_calls, (unsigned long)r.ms);
        say("sink-gone-stops-it", r.status == EXEC_SINK_GONE &&
                                      v.m_calls == 1 && r.ms < 5000, detail);
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

/* Finds one pid in a listing, and remembers what it said about it. */
class FindPid : public ProcessSink
{
public:
    explicit FindPid(DWORD pid) : m_pid(pid), m_seen(0), m_found(false)
    {
        m_exe[0] = 0;
    }
    bool onProcess(const ProcessInfo &p)
    {
        m_seen++;
        if (p.pid == m_pid)
        {
            m_found = true;
            rc_strcpy(m_exe, sizeof m_exe, p.exe);
        }
        return true;
    }
    DWORD m_pid;
    int m_seen;
    bool m_found;
    char m_exe[MAX_PATH];
};

static void processes()
{
    char detail[160];
    Processes ps;
    bool up = ps.init();
    say("toolhelp-resolves", up && ps.available(), up ? "" : "not found");
    if (!up)
        return;
    {
        FindPid f(GetCurrentProcessId());
        bool ok = ps.list(f);
        rc_strf(detail, sizeof detail, "seen=%d self=%s", f.m_seen,
                f.m_found ? f.m_exe : "missing");
        say("list-finds-self", ok && f.m_found && f.m_seen > 1, detail);
    }
    {
        KillStatus k = ps.kill(GetCurrentProcessId(), true, 0);
        rc_strf(detail, sizeof detail, "status=%d", (int)k);
        say("kill-self-denied-even-forced", k == KILL_DENIED, detail);
    }
    {
        KillStatus k = ps.kill(0x7ffffff0, false, 0);
        rc_strf(detail, sizeof detail, "status=%d", (int)k);
        say("kill-no-such-pid", k == KILL_NO_SUCH_PID, detail);
    }
    {
        static char big[2048];
        memset(big, 'z', sizeof big - 1);
        big[sizeof big - 1] = 0;
        DWORD pid, err;
        bool started = Processes::launch(big, NULL, SW_HIDE, pid, err);
        rc_strf(detail, sizeof detail, "started=%d err=%lu", started,
                (unsigned long)err);
        say("launch-overlong-refused", !started && pid == 0, detail);
    }
    {
        /* A tree: cmd.exe running cmd.exe running a loop. The inner one is
         * a descendant, the kill ends it within the wait, and the outer one
         * follows. Hidden, never inheriting anything of ours. */
        DWORD outer = 0, err = 0;
        bool started = Processes::launch(
            "cmd.exe /c cmd.exe /c for /l %i in (1,1,50000000) do rem", NULL,
            SW_HIDE, outer, err);
        std::vector<DWORD> kids;
        for (int i = 0; started && i < 30 && kids.empty(); i++)
        {
            Sleep(100);
            ps.descendants(outer, kids);
        }
        rc_strf(detail, sizeof detail, "started=%d outer=%08lx kids=%u",
                started, (unsigned long)outer, (unsigned)kids.size());
        say("launch-and-descendants", started && kids.size() >= 1, detail);

        KillStatus inner = kids.empty() ? KILL_NO_SUCH_PID
                                        : ps.kill(kids[0], false, 3000);
        KillStatus top = started ? ps.kill(outer, false, 3000)
                                 : KILL_NO_SUCH_PID;
        rc_strf(detail, sizeof detail, "inner=%d outer=%d", (int)inner,
                (int)top);
        /* The outer cmd.exe may already have exited once its child died. */
        say("kill-waits-for-the-end", inner == KILL_OK &&
                                          (top == KILL_OK ||
                                           top == KILL_NO_SUCH_PID), detail);
    }
    {
        char self[MAX_PATH], rundll[MAX_PATH], win[MAX_PATH];
        GetModuleFileNameA(NULL, self, sizeof self);
        GetWindowsDirectoryA(win, sizeof win);
        rc_strf(rundll, sizeof rundll, "%s\\RUNDLL.EXE", win);
        bool selfIs = Processes::isWin16Exe(self);
        bool rundllIs = Processes::isWin16Exe(rundll);
        rc_strf(detail, sizeof detail, "self=%d %s=%d", selfIs, rundll,
                rundllIs);
        /* Wine's prefix ships a 16-bit RUNDLL.EXE, as 98 does. */
        say("win16-detected-from-the-header", !selfIs && rundllIs, detail);
    }
}

int main(void)
{
    rc_log_open(".");
    tooLong();
    output();
    stopping();
    processes();
    rc_log_close();
    return g_failures ? 1 : 0;
}
