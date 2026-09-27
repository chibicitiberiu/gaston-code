/* libexec under Wine: ExecJob driven for real, one case per property.
 *
 * Run by tests/python/guest/test_guest_exec.py. Prints
 * `verdict<TAB>case<TAB>detail` rows.
 *
 * RUNS ON THE GUEST TOO. Every child is this exe itself in a child mode
 * (--print, --exit, --tick, --spam, --parent), run directly or through the
 * shell COMSPEC names -- cmd.exe under Wine, COMMAND.COM on 98 -- so the
 * same cases hold on both and nothing depends on a shell's own syntax. On
 * 98 the grandchild case is the REDIR32 regression test: a Win32 program
 * under COMMAND.COM, stopped, and nothing left behind (docs/BACKLOG.md 14).
 *
 * (Formerly: Wine has no COMMAND.COM, so the
 * commands that needed a shell named cmd.exe in the body and left `shell`
 * empty -- the same sidestep rpccheck uses. Which shell runs is not what
 * these cases are about; the guest behaviour of COMMAND.COM is measured by
 * execprobe on the VM instead (docs/measurements/execprobe-vm-*).
 */
#include <windows.h>
#include <process.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <string>
#include "core/rc_cpp.h"
#include "core/log.h"
#include "core/str.h"
#include "exec/execjob.h"
#include "exec/process.h"
#include "exec/system.h"

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

static char g_self[MAX_PATH];
static char g_shell[MAX_PATH + 4];     /* "<COMSPEC> /c" */
static char g_dir[MAX_PATH];

/* "<this exe> <args>" into `out`. */
static const char *self(char *out, unsigned cap, const char *args)
{
    rc_strf(out, cap, "%s %s", g_self, args);
    return out;
}

/* A marker file beside the exe. */
static const char *marker(char *out, unsigned cap, const char *name)
{
    rc_strf(out, cap, "%s\\%s", g_dir, name);
    return out;
}

/* A command that never ends by itself (a minute of ticks into NUL). */
static char g_forever[MAX_PATH + 32];

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
        char cmd[MAX_PATH + 32];
        bool spawned = job.run(self(cmd, sizeof cmd, "--print hello"), opt,
                               c, r);
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
        char cmd[MAX_PATH + 32];
        job.run(self(cmd, sizeof cmd, "--exit 7"), opt, c, r);
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
        char cmd[MAX_PATH + 64];
        job.run(self(cmd, sizeof cmd,
                     "--print 0123456789abcdefghijklmnopqrstuvwxyz"),
                opt, c, r);
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
        viaShell.shell = g_shell;
        ExecResult ra, rb;
        Collect ca, cb;
        a.run("echo hi", viaShell, ca, ra);
        char cmd[MAX_PATH + 32];
        b.run(self(cmd, sizeof cmd, "--print hi"), direct, cb, rb);
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
        job.run(g_forever, opt, c, r);
        rc_strf(detail, sizeof detail, "status=%d ms=%lu", (int)r.status,
                (unsigned long)r.ms);
        say("timeout", r.status == EXEC_TIMEOUT && r.ms >= 1000 && r.ms < 6000,
            detail);
    }
    {
        /* The stop reaches the whole tree. The ticking program runs under
         * the shell, a grandchild; killing only the one run() spawned --
         * all this did before -- left it appending to the marker, and on 98
         * killed nothing at all (the pid is REDIR32). stopLeaked false is
         * the REDIR32 regression check when this runs on the guest. */
        ExecJob job;
        ExecOptions opt;
        ExecResult r;
        Collect c;
        char mark[MAX_PATH], args[MAX_PATH + 32], cmd[2 * MAX_PATH + 64];
        marker(mark, sizeof mark, "EXTREE.TXT");
        DeleteFileA(mark);
        rc_strf(args, sizeof args, "--tick %s 60", mark);
        char inner[MAX_PATH * 2];
        self(inner, sizeof inner, args);
        opt.shell = g_shell;
        /* Through the shell: the ticking program is then the shell's
         * child (on 98 the DOS VM's), not the process run() spawned. */
        opt.timeoutMs = 3000;
        rc_strf(cmd, sizeof cmd, "%s", inner);
        job.run(cmd, opt, c, r);
        DWORD a = fileSize(mark);
        Sleep(2500);
        DWORD b = fileSize(mark);
        rc_strf(detail, sizeof detail, "status=%d marker %lu -> %lu leaked=%d",
                (int)r.status, (unsigned long)a, (unsigned long)b,
                r.stopLeaked);
        say("stop-reaches-the-grandchild", r.status == EXEC_TIMEOUT && a > 0 &&
                                               a == b && !r.stopLeaked,
            detail);
        DeleteFileA(mark);
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
        char cmd[MAX_PATH + 32];
        job.run(self(cmd, sizeof cmd, "--spam"), opt, v, r);
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
        bool spawned = job.run(g_forever, opt, c, r);
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
        job.run(g_forever, opt, c, r);
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

/* Every process, for looking one up by pid. */
class Gather : public ProcessSink
{
public:
    bool onProcess(const ProcessInfo &p)
    {
        m_all.push_back(p);
        return true;
    }
    std::vector<ProcessInfo> m_all;
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
        /* A tree: this exe running a detached copy of itself. The inner one
         * is a descendant, the kill ends it within the wait, and the outer
         * one then ENDS BY ITSELF. It is not killed: on 98 the outer is a
         * console program, and killing one leaves its console's DOS VM
         * half-dead (measured, docs/BACKLOG.md 17). Hidden, never
         * inheriting anything of ours. */
        DWORD outer = 0, err = 0;
        char parentCmd[MAX_PATH + 32];
        bool started = Processes::launch(
            self(parentCmd, sizeof parentCmd, "--parent"), NULL,
            SW_HIDE, outer, err);
        std::vector<DWORD> kids;
        DWORD innerPid = 0;
        const char *me = strrchr(g_self, '\\');
        me = me ? me + 1 : g_self;
        /* Until the copy of this exe shows up. On 98 the console's DOS VM
         * is listed as a descendant first, so "any child" is too early. */
        for (int i = 0; started && i < 100 && !innerPid; i++)
        {
            Sleep(100);
            ps.descendants(outer, kids);
            Gather g;
            ps.list(g);
            for (unsigned k = 0; k < kids.size(); k++)
                for (unsigned j = 0; j < g.m_all.size(); j++)
                {
                    const char *n = strrchr(g.m_all[j].exe, '\\');
                    n = n ? n + 1 : g.m_all[j].exe;
                    if (g.m_all[j].pid == kids[k] && lstrcmpiA(n, me) == 0)
                        innerPid = kids[k];
                }
        }
        rc_strf(detail, sizeof detail, "started=%d outer=%08lx kids=%u "
                "inner=%08lx", started, (unsigned long)outer,
                (unsigned)kids.size(), (unsigned long)innerPid);
        say("launch-and-descendants", started && innerPid != 0, detail);

        KillStatus inner = !innerPid ? KILL_NO_SUCH_PID
                                     : ps.kill(innerPid, false, 3000);
        bool outerEnded = false;
        {
            Handle h(OpenProcess(SYNCHRONIZE, FALSE, outer));
            outerEnded = !h.valid() ||
                         WaitForSingleObject(h.get(), 5000) == WAIT_OBJECT_0;
        }
        rc_strf(detail, sizeof detail, "inner=%d outer-ended=%d", (int)inner,
                outerEnded);
        say("kill-waits-for-the-end", inner == KILL_OK && outerEnded, detail);
    }
    {
        /* A console program launched with a console of its own. On 98 that
         * console is a DOS VM below it, and kill() without force must say
         * dos_session -- killing it strands the VM (BACKLOG 17). It is
         * never forced here; it ends by itself. Under Wine there is no VM
         * and the kill goes through as for any program. */
        DWORD pid = 0, err = 0;
        char cmd[MAX_PATH + 32];
        bool started = Processes::launch(self(cmd, sizeof cmd, "--tick NUL 8"),
                                         NULL, SW_HIDE, pid, err);
        bool vm = false;
        for (int i = 0; started && i < 30 && !vm; i++)
        {
            Sleep(100);
            std::vector<DWORD> below;
            ps.descendants(pid, below);
            Gather g;
            ps.list(g);
            for (unsigned k = 0; k < below.size(); k++)
                for (unsigned j = 0; j < g.m_all.size(); j++)
                {
                    const char *n = strrchr(g.m_all[j].exe, '\\');
                    n = n ? n + 1 : g.m_all[j].exe;
                    if (g.m_all[j].pid == below[k] &&
                        lstrcmpiA(n, "WINOA386.MOD") == 0)
                        vm = true;
                }
        }
        KillStatus k = started ? ps.kill(pid, false, 3000) : KILL_FAILED;
        bool ended = true;
        if (vm)
        {
            Handle h(OpenProcess(SYNCHRONIZE, FALSE, pid));
            ended = !h.valid() ||
                    WaitForSingleObject(h.get(), 10000) == WAIT_OBJECT_0;
        }
        rc_strf(detail, sizeof detail, "vm=%d kill=%d ended=%d", vm, (int)k,
                ended);
        say("kill-refuses-a-dos-session",
            started && (vm ? (k == KILL_DOS_SESSION && ended) : k == KILL_OK),
            detail);
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

static void system()
{
    char detail[160];
    SystemInfo si;
    readSystemInfo(si);
    rc_strf(detail, sizeof detail,
            "mem=%lu free=%lu os=%lu.%lu.%lu cpu=%u/%u x%lu acp=%u oemcp=%u",
            (unsigned long)si.memKb, (unsigned long)si.memFreeKb,
            (unsigned long)si.osMajor, (unsigned long)si.osMinor,
            (unsigned long)si.osBuild, si.cpuFamily, si.cpuModel,
            (unsigned long)si.cpuCount, si.acp, si.oemcp);
    say("sysinfo-is-the-machine", si.memKb > 0 && si.memFreeKb <= si.memKb &&
                                      si.osKnown && si.osMajor >= 4 &&
                                      si.cpuCount >= 1 &&
                                      si.acp == GetACP() &&
                                      si.oemcp == GetOEMCP(),
        detail);
    /* The one reboot case safe to run: a mode that does not exist is
     * refused before ExitWindowsEx is ever called. */
    say("reboot-refuses-an-unknown-mode",
        !requestReboot((RebootMode)99, false), "");
}

/* Child modes: the programs the cases run. Plain Win32 console code, so
 * they behave the same under cmd.exe, COMMAND.COM or no shell. */
static int child(int argc, char **argv)
{
    if (argc >= 3 && strcmp(argv[1], "--print") == 0)
    {
        printf("%s\n", argv[2]);
        return 0;
    }
    if (argc >= 3 && strcmp(argv[1], "--exit") == 0)
        return atoi(argv[2]);
    if (argc >= 4 && strcmp(argv[1], "--tick") == 0)
    {
        for (int i = 0; i < atoi(argv[3]); i++)
        {
            FILE *f = fopen(argv[2], "a");
            if (f)
            {
                fputs("t\n", f);
                fclose(f);
            }
            Sleep(500);
        }
        return 0;
    }
    if (argc >= 2 && strcmp(argv[1], "--spam") == 0)
    {
        for (int i = 0; i < 100000; i++)
        {
            printf("line %d of output nobody is going to read\n", i);
            fflush(stdout);
        }
        return 0;
    }
    if (argc >= 2 && strcmp(argv[1], "--parent") == 0)
    {
        char cmd[MAX_PATH + 32];
        rc_strf(cmd, sizeof cmd, "%s --tick NUL 120", g_self);
        STARTUPINFOA si;
        memset(&si, 0, sizeof si);
        si.cb = sizeof si;
        PROCESS_INFORMATION pi;
        /* DETACHED: no console for the child. On 98 a console program's
         * console is a DOS VM, and killing that program leaves the VM
         * half-dead (measured) -- which then degraded every later run. */
        if (!CreateProcessA(NULL, cmd, NULL, NULL, FALSE, DETACHED_PROCESS,
                            NULL, NULL, &si, &pi))
            return 2;
        WaitForSingleObject(pi.hProcess, 120000);
        CloseHandle(pi.hThread);
        CloseHandle(pi.hProcess);
        return 0;
    }
    return -1;
}

int main(int argc, char **argv)
{
    GetModuleFileNameA(NULL, g_self, sizeof g_self);
    rc_strf(g_dir, sizeof g_dir, "%s", g_self);
    char *slash = strrchr(g_dir, '\\');
    if (slash)
        *slash = 0;
    /* --detached-to FILE: run the whole check again as a DETACHED process
     * with its output in FILE. From a DOS prompt this exe is a console
     * process, and a console parent's DOS children share its console and
     * never get REDIR32 -- which would hide exactly what the grandchild
     * case is for. The client is a GUI process; detached is the same. */
    if (argc >= 3 && strcmp(argv[1], "--detached-to") == 0)
    {
        SECURITY_ATTRIBUTES sa;
        memset(&sa, 0, sizeof sa);
        sa.nLength = sizeof sa;
        sa.bInheritHandle = TRUE;
        Handle out(CreateFileA(argv[2], GENERIC_WRITE, FILE_SHARE_READ, &sa,
                               CREATE_ALWAYS, 0, NULL));
        if (!out.valid())
            return 3;
        STARTUPINFOA si;
        memset(&si, 0, sizeof si);
        si.cb = sizeof si;
        si.dwFlags = STARTF_USESTDHANDLES;
        si.hStdOutput = out.get();
        si.hStdError = out.get();
        si.hStdInput = NULL;
        PROCESS_INFORMATION pi;
        char cmd[MAX_PATH + 4];
        rc_strf(cmd, sizeof cmd, "%s", g_self);
        if (!CreateProcessA(NULL, cmd, NULL, NULL, TRUE, DETACHED_PROCESS,
                            NULL, g_dir, &si, &pi))
            return 4;
        CloseHandle(pi.hThread);
        CloseHandle(pi.hProcess);
        printf("running detached; verdicts in %s\n", argv[2]);
        return 0;
    }
    if (argc > 1)
        return child(argc, argv);
    const char *comspec = getenv("COMSPEC");
    rc_strf(g_shell, sizeof g_shell, "%s /c",
            comspec ? comspec : "COMMAND.COM");
    rc_strf(g_forever, sizeof g_forever, "%s --tick NUL 120", g_self);
    rc_log_open(".");
    tooLong();
    output();
    stopping();
    processes();
    system();
    rc_log_close();
    return g_failures ? 1 : 0;
}
