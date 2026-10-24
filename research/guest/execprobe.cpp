/* execprobe: measures on the real guest what libexec's design depends on and
 * Wine cannot answer (docs/REMOTE.md step 3). Not a pass/fail check: it
 * prints `key<TAB>value` rows to read back over COM1.
 *
 *   1. Toolhelp32: present, what a snapshot costs, what a stock box runs.
 *   2. A DOS child of COMMAND.COM /c: does it show in a snapshot, with the
 *      shell as its parent, and does it outlive TerminateProcess(shell)?
 *   3. The same for a Win32 console child.
 *   4. Exit codes, direct and through COMMAND.COM /c.
 *   5. Does an inherited listening socket stay bound after the parent
 *      closes it (exec must spawn with bInherit=TRUE for its pipes)?
 *   6. Which running processes and C:\WINDOWS exes are 16-bit (NE).
 *   7. Does CreateProcess' show value reach a DOS box, and does the window
 *      outlive its process?
 *   8. --stops (notes/libexec-plan.md step 0b): probes for a CLEAN stop of a
 *      piped COMMAND.COM /c spawn, since TerminateProcess on the returned
 *      pid (REDIR32 on 98) does nothing and TerminateProcess on the DOS VM
 *      (WINOA386.MOD) still leaves REDIR32 a zombie (notes/BACKLOG.md #14).
 *      Five strategies (close our pipes; GenerateConsoleCtrlEvent; WM_CLOSE
 *      to the hidden "tty" window, with its confirmation dialog if any;
 *      Ctrl+C keystrokes to that window; kill the VM and just wait) each run
 *      twice on a DOS batch loop and a Win32 child spawned through the
 *      shell, and once each on a Win32 child and a DOS program (CHOICE.COM)
 *      spawned directly with no shell. 30 cases; each spends about 10s
 *      watching whether ticking stops, whether the spawned pid exits, and
 *      GlobalMemoryStatus, so the run is expected to take roughly 5-7
 *      minutes (vmthenwait's up-to-15s wait is the main variable).
 *      Every strategy that fails leaves a REDIR32 zombie on purpose, and
 *      nothing but a reboot removes one: reboot the guest before (to start
 *      clean) and after. The run stops itself below 3MB free or at 20
 *      zombies. Run it DETACHED: GenerateConsoleCtrlEvent to group 0 from a
 *      DOS prompt would reach that prompt.
 *
 * Modes: no argument relaunches detached (like a GUI parent) running
 * everything; --detach MODE relaunches detached running MODE; --run all,
 * --kills only 2 and 3, --stops only 8, --redir only 9, --redir2 its stub
 * variants, --gentle stop orders that spare the VM (each also runnable as
 * --detach MODE),
 * --list only the process list, in place.
 * Child modes, run by the probe itself: --sleep N, --tick FILE N, --exit N.
 * Writes EXECPRB.TXT beside itself.
 */
#include <windows.h>
#include <winsock.h>
#include <tlhelp32.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <vector>
#include "core/rc_cpp.h"
#include "core/str.h"

/* Rows go to EXECPRB.TXT beside the exe, not stdout: a child launched
 * without its own std handles writes into an inherited stdout. */
static FILE *g_out;

static void row(const char *key, const char *fmt, ...)
{
    char buf[512];
    va_list ap;
    va_start(ap, fmt);
    vsnprintf(buf, sizeof buf, fmt, ap);
    va_end(ap);
    fprintf(g_out, "%s\t%s\n", key, buf);
    fflush(g_out);
}

/* ---- Toolhelp, resolved at runtime ---- */

typedef HANDLE (WINAPI *SnapFn)(DWORD, DWORD);
typedef BOOL (WINAPI *WalkFn)(HANDLE, PROCESSENTRY32 *);

static SnapFn g_snap;
static WalkFn g_first;
static WalkFn g_next;

struct Proc
{
    DWORD pid;
    DWORD ppid;
    DWORD threads;
    char exe[MAX_PATH];
};

static bool snapshot(std::vector<Proc> &out)
{
    out.clear();
    if (!g_snap)
        return false;
    Handle h(g_snap(TH32CS_SNAPPROCESS, 0));
    if (!h.valid())
        return false;
    PROCESSENTRY32 pe;
    memset(&pe, 0, sizeof pe);
    pe.dwSize = sizeof pe;
    for (BOOL ok = g_first(h.get(), &pe); ok; ok = g_next(h.get(), &pe))
    {
        Proc p;
        p.pid = pe.th32ProcessID;
        p.ppid = pe.th32ParentProcessID;
        p.threads = pe.cntThreads;
        rc_strf(p.exe, sizeof p.exe, "%s", pe.szExeFile);
        out.push_back(p);
        pe.dwSize = sizeof pe;
    }
    return true;
}

static bool had(const std::vector<Proc> &v, DWORD pid)
{
    for (unsigned i = 0; i < v.size(); i++)
        if (v[i].pid == pid)
            return true;
    return false;
}

/* 'P' for PE, 'N' for NE (16-bit Windows), 'M' for plain MZ (DOS), '?'. */
static char exeKind(const char *path)
{
    Handle f(CreateFileA(path, GENERIC_READ, FILE_SHARE_READ | FILE_SHARE_WRITE,
                         NULL, OPEN_EXISTING, 0, NULL));
    if (!f.valid())
        return '?';
    unsigned char mz[64];
    DWORD got = 0;
    if (!ReadFile(f.get(), mz, sizeof mz, &got, NULL) || got < 64 ||
        mz[0] != 'M' || mz[1] != 'Z')
        return '?';
    DWORD off = mz[0x3c] | (mz[0x3d] << 8) | ((DWORD)mz[0x3e] << 16) |
                ((DWORD)mz[0x3f] << 24);
    unsigned char sig[2];
    if (SetFilePointer(f.get(), (LONG)off, NULL, FILE_BEGIN) == 0xFFFFFFFF ||
        !ReadFile(f.get(), sig, 2, &got, NULL) || got != 2)
        return 'M';
    if (sig[0] == 'P' && sig[1] == 'E')
        return 'P';
    if (sig[0] == 'N' && sig[1] == 'E')
        return 'N';
    return 'M';
}

static void printNew(const char *key, const std::vector<Proc> &base,
                     const std::vector<Proc> &now, DWORD shellPid)
{
    int n = 0;
    for (unsigned i = 0; i < now.size(); i++)
    {
        const Proc &p = now[i];
        if (had(base, p.pid))
            continue;
        row(key, "pid=%08lx ppid=%08lx%s threads=%lu kind=%c exe=%s",
            p.pid, p.ppid, p.ppid == shellPid ? "(shell)" : "", p.threads,
            exeKind(p.exe), p.exe);
        n++;
    }
    row(key, "new=%d", n);
}

/* Every pid this probe spawned. The VM is shared, so cleanup touches only
 * these and their descendants, never "whatever is new since the baseline":
 * under Wine that killed explorer.exe. */
static std::vector<DWORD> g_spawned;

static bool inList(const std::vector<DWORD> &v, DWORD pid)
{
    for (unsigned i = 0; i < v.size(); i++)
        if (v[i] == pid)
            return true;
    return false;
}

/* Our spawned pids plus everything descended from them in `now`. */
static char g_self[MAX_PATH];

static void family(const std::vector<Proc> &now, std::vector<DWORD> &fam)
{
    fam = g_spawned;
    /* On 9x a Win32 grandchild of COMMAND.COM has the DOS VM
     * (WINOA386.MOD) as its parent, not the shell, so copies of this exe
     * are claimed by name. */
    for (unsigned i = 0; i < now.size(); i++)
        if (lstrcmpiA(now[i].exe, g_self) == 0 &&
            now[i].pid != GetCurrentProcessId() && !inList(fam, now[i].pid))
            fam.push_back(now[i].pid);
    for (bool grew = true; grew;)
    {
        grew = false;
        for (unsigned i = 0; i < now.size(); i++)
            if (!inList(fam, now[i].pid) && inList(fam, now[i].ppid))
            {
                fam.push_back(now[i].pid);
                grew = true;
            }
    }
}

/* Kills whatever of our own family is still alive. */
static void sweep(const char *key, const std::vector<Proc> &base)
{
    std::vector<Proc> now;
    snapshot(now);
    std::vector<DWORD> fam;
    family(now, fam);
    for (unsigned i = 0; i < now.size(); i++)
    {
        const Proc &p = now[i];
        const char *name = strrchr(p.exe, '\\');
        name = name ? name + 1 : p.exe;
        /* Under Wine explorer.exe can descend from a child's conhost. */
        if (had(base, p.pid) || p.pid == GetCurrentProcessId() ||
            !inList(fam, p.pid) || lstrcmpiA(name, "EXPLORER.EXE") == 0)
            continue;
        HANDLE h = OpenProcess(PROCESS_TERMINATE, FALSE, p.pid);
        BOOL ok = h ? TerminateProcess(h, 1) : FALSE;
        if (h)
            CloseHandle(h);
        row(key, "swept pid=%08lx exe=%s open=%d kill=%d", p.pid, p.exe,
            h != NULL, ok);
    }
}

/* ---- spawning ---- */
static char g_dir[MAX_PATH];
static char g_shell[MAX_PATH];

/* As ExecJob does it: one inheritable pipe for stdout+stderr, a stdin whose
 * write end is closed, the read end made non-inheritable, SW_HIDE. */
struct Child
{
    PROCESS_INFORMATION pi;
    Handle out;
};

static bool spawnLikeExec(const char *cmdline, Child &c)
{
    SECURITY_ATTRIBUTES sa;
    memset(&sa, 0, sizeof sa);
    sa.nLength = sizeof sa;
    sa.bInheritHandle = TRUE;
    HANDLE rd, wr, inRd, inWr;
    if (!CreatePipe(&rd, &wr, &sa, 16384))
        return false;
    if (!CreatePipe(&inRd, &inWr, &sa, 0))
        return false;
    HANDLE dup = NULL;
    if (DuplicateHandle(GetCurrentProcess(), rd, GetCurrentProcess(), &dup, 0,
                        FALSE, DUPLICATE_SAME_ACCESS))
    {
        CloseHandle(rd);
        rd = dup;
    }
    CloseHandle(inWr);

    STARTUPINFOA si;
    memset(&si, 0, sizeof si);
    si.cb = sizeof si;
    si.dwFlags = STARTF_USESTDHANDLES | STARTF_USESHOWWINDOW;
    si.wShowWindow = SW_HIDE;
    si.hStdOutput = wr;
    si.hStdError = wr;
    si.hStdInput = inRd;
    char cmd[1024];
    rc_strf(cmd, sizeof cmd, "%s", cmdline);
    BOOL ok = CreateProcessA(NULL, cmd, NULL, NULL, TRUE, 0, NULL, g_dir, &si,
                             &c.pi);
    CloseHandle(wr);
    CloseHandle(inRd);
    if (!ok)
    {
        CloseHandle(rd);
        return false;
    }
    c.out.reset(rd);
    g_spawned.push_back(c.pi.dwProcessId);
    return true;
}

static void drain(Child &c)
{
    char buf[512];
    DWORD avail = 0, got = 0;
    while (PeekNamedPipe(c.out.get(), NULL, 0, NULL, &avail, NULL) && avail)
    {
        if (!ReadFile(c.out.get(), buf, avail < sizeof buf ? avail : sizeof buf,
                      &got, NULL) || !got)
            break;
    }
}

/* Waits up to `ms` for the child, draining its pipe. True if it exited. */
static bool waitChild(Child &c, DWORD ms)
{
    DWORD t0 = GetTickCount();
    for (;;)
    {
        drain(c);
        if (WaitForSingleObject(c.pi.hProcess, 100) == WAIT_OBJECT_0)
        {
            drain(c);
            return true;
        }
        if (GetTickCount() - t0 >= ms)
            return false;
    }
}

static void closeChild(Child &c)
{
    CloseHandle(c.pi.hThread);
    CloseHandle(c.pi.hProcess);
    c.out.reset();
}

static DWORD fileSize(const char *path)
{
    WIN32_FIND_DATAA fd;
    HANDLE h = FindFirstFileA(path, &fd);
    if (h == INVALID_HANDLE_VALUE)
        return 0xFFFFFFFF;
    FindClose(h);
    return fd.nFileSizeLow;
}

/* ---- 1. Toolhelp ---- */

static void toolhelp(std::vector<Proc> &base)
{
    HMODULE k = GetModuleHandleA("KERNEL32.DLL");
    g_snap = (SnapFn)GetProcAddress(k, "CreateToolhelp32Snapshot");
    g_first = (WalkFn)GetProcAddress(k, "Process32First");
    g_next = (WalkFn)GetProcAddress(k, "Process32Next");
    row("toolhelp", "snap=%d first=%d next=%d", g_snap != NULL,
        g_first != NULL, g_next != NULL);
    if (!g_snap || !g_first || !g_next)
    {
        g_snap = NULL;
        return;
    }
    DWORD t0 = GetTickCount();
    for (int i = 0; i < 20; i++)
        snapshot(base);
    row("snap-cost", "20 snapshots in %lums, %u processes",
        GetTickCount() - t0, (unsigned)base.size());
    row("self", "pid=%08lx", GetCurrentProcessId());
    for (unsigned i = 0; i < base.size(); i++)
    {
        const Proc &p = base[i];
        row("proc", "pid=%08lx ppid=%08lx threads=%lu kind=%c exe=%s", p.pid,
            p.ppid, p.threads, exeKind(p.exe), p.exe);
    }
}

/* ---- 2 and 3. Does the grandchild outlive the shell? ---- */

static void killShell(const char *key, const char *cmdline, const char *tick,
                      const std::vector<Proc> &base)
{
    DeleteFileA(tick);
    Child c;
    if (!spawnLikeExec(cmdline, c))
    {
        row(key, "spawn failed %lu", GetLastError());
        return;
    }
    row(key, "shell pid=%08lx cmd=%s", c.pi.dwProcessId, cmdline);
    if (waitChild(c, 4000))
    {
        DWORD code = 0;
        GetExitCodeProcess(c.pi.hProcess, &code);
        row(key, "shell exited early, code=%lu", code);
    }
    std::vector<Proc> now;
    snapshot(now);
    printNew(key, base, now, c.pi.dwProcessId);
    DWORD before = fileSize(tick);
    BOOL killed = TerminateProcess(c.pi.hProcess, 1);
    DWORD w = WaitForSingleObject(c.pi.hProcess, 3000);
    row(key, "terminate=%d shell-gone=%d tick-before=%ld", killed,
        w == WAIT_OBJECT_0, (long)before);
    DWORD sizes[3];
    for (int i = 0; i < 3; i++)
    {
        Sleep(2000);
        sizes[i] = fileSize(tick);
    }
    row(key, "tick after 2s/4s/6s: %ld %ld %ld -> %s", (long)sizes[0],
        (long)sizes[1], (long)sizes[2],
        sizes[2] != sizes[0] ? "GRANDCHILD STILL RUNNING" : "stopped");
    snapshot(now);
    printNew(key, base, now, c.pi.dwProcessId);

    /* Stage two: kill the DOS VM (WINOA386.MOD) under what we spawned. */
    std::vector<DWORD> fam;
    family(now, fam);
    for (unsigned i = 0; i < now.size(); i++)
    {
        const Proc &p = now[i];
        const char *name = strrchr(p.exe, '\\');
        name = name ? name + 1 : p.exe;
        if (had(base, p.pid) || !inList(fam, p.pid) ||
            lstrcmpiA(name, "WINOA386.MOD") != 0)
            continue;
        HANDLE h = OpenProcess(PROCESS_TERMINATE | SYNCHRONIZE, FALSE, p.pid);
        BOOL ok = h ? TerminateProcess(h, 1) : FALSE;
        DWORD gone = h ? WaitForSingleObject(h, 3000) : WAIT_FAILED;
        if (h)
            CloseHandle(h);
        row(key, "vm pid=%08lx open=%d kill=%d gone=%d", p.pid, h != NULL, ok,
            gone == WAIT_OBJECT_0);
    }
    for (int i = 0; i < 3; i++)
    {
        Sleep(2000);
        sizes[i] = fileSize(tick);
    }
    row(key, "after vm kill, tick 2s/4s/6s: %ld %ld %ld -> %s",
        (long)sizes[0], (long)sizes[1], (long)sizes[2],
        sizes[2] != sizes[0] ? "GRANDCHILD STILL RUNNING" : "stopped");
    snapshot(now);
    printNew(key, base, now, c.pi.dwProcessId);
    closeChild(c);
    sweep(key, base);
    Sleep(1000);
    DeleteFileA(tick);
}

/* ---- 2b. Kill the DOS VM first and leave REDIR32 alone ---- */

static void killVmFirst(const char *key, const char *cmdline, const char *tick,
                        const std::vector<Proc> &base)
{
    DeleteFileA(tick);
    Child c;
    if (!spawnLikeExec(cmdline, c))
    {
        row(key, "spawn failed %lu", GetLastError());
        return;
    }
    row(key, "spawned pid=%08lx cmd=%s", c.pi.dwProcessId, cmdline);
    waitChild(c, 4000);
    std::vector<Proc> now;
    snapshot(now);
    printNew(key, base, now, c.pi.dwProcessId);
    DWORD vm = 0;
    for (unsigned i = 0; i < now.size(); i++)
        if (now[i].ppid == c.pi.dwProcessId && !had(base, now[i].pid))
            vm = now[i].pid;
    row(key, "vm=%08lx tick-before=%ld", vm, (long)fileSize(tick));
    if (!vm)
    {
        TerminateProcess(c.pi.hProcess, 1);
        closeChild(c);
        return;
    }
    HANDLE h = OpenProcess(PROCESS_TERMINATE | SYNCHRONIZE, FALSE, vm);
    BOOL ok = h ? TerminateProcess(h, 1) : FALSE;
    DWORD t0 = GetTickCount();
    DWORD vmGone = h ? WaitForSingleObject(h, 10000) : WAIT_FAILED;
    DWORD vmMs = GetTickCount() - t0;
    if (h)
        CloseHandle(h);
    /* The spawned pid (REDIR32 on 98) is never terminated: does it end by
     * itself once the VM is gone? Drain its pipe meanwhile, as exec does. */
    bool spawnedGone = waitChild(c, 10000);
    DWORD allMs = GetTickCount() - t0;
    DWORD code = 0;
    GetExitCodeProcess(c.pi.hProcess, &code);
    row(key, "vm kill=%d vm-gone=%d after %lums; spawned-gone=%d after %lums code=%lu",
        ok, vmGone == WAIT_OBJECT_0, vmMs, spawnedGone, allMs, code);
    DWORD sizes[3];
    for (int i = 0; i < 3; i++)
    {
        Sleep(2000);
        sizes[i] = fileSize(tick);
    }
    row(key, "tick 2s/4s/6s: %ld %ld %ld -> %s", (long)sizes[0],
        (long)sizes[1], (long)sizes[2],
        sizes[2] != sizes[0] ? "GRANDCHILD STILL RUNNING" : "stopped");
    snapshot(now);
    printNew(key, base, now, c.pi.dwProcessId);
    closeChild(c);
    Sleep(1000);
    DeleteFileA(tick);
}

/* ---- 4. Exit codes ---- */

static void exitCode(const char *key, const char *cmdline)
{
    Child c;
    if (!spawnLikeExec(cmdline, c))
    {
        row(key, "spawn failed %lu: %s", GetLastError(), cmdline);
        return;
    }
    bool done = waitChild(c, 15000);
    DWORD code = 0;
    GetExitCodeProcess(c.pi.hProcess, &code);
    if (!done)
        TerminateProcess(c.pi.hProcess, 1);
    row(key, "exited=%d code=%lu cmd=%s", done, code, cmdline);
    closeChild(c);
}

/* ---- 5. Socket inheritance ---- */

static bool connectTo(int port)
{
    SOCKET s = socket(AF_INET, SOCK_STREAM, 0);
    struct sockaddr_in a;
    memset(&a, 0, sizeof a);
    a.sin_family = AF_INET;
    a.sin_port = htons((u_short)port);
    a.sin_addr.s_addr = inet_addr("127.0.0.1");
    bool ok = connect(s, (struct sockaddr *)&a, sizeof a) == 0;
    closesocket(s);
    return ok;
}

static bool bindAgain(int port)
{
    SOCKET s = socket(AF_INET, SOCK_STREAM, 0);
    struct sockaddr_in a;
    memset(&a, 0, sizeof a);
    a.sin_family = AF_INET;
    a.sin_port = htons((u_short)port);
    a.sin_addr.s_addr = inet_addr("127.0.0.1");
    bool ok = bind(s, (struct sockaddr *)&a, sizeof a) == 0;
    closesocket(s);
    return ok;
}

static void inherit(const char *key, const char *cmdline, bool viaExec)
{
    SOCKET l = socket(AF_INET, SOCK_STREAM, 0);
    struct sockaddr_in a;
    memset(&a, 0, sizeof a);
    a.sin_family = AF_INET;
    a.sin_addr.s_addr = inet_addr("127.0.0.1");
    int len = sizeof a;
    if (bind(l, (struct sockaddr *)&a, sizeof a) || listen(l, 2) ||
        getsockname(l, (struct sockaddr *)&a, &len))
    {
        row(key, "listen failed %d", WSAGetLastError());
        return;
    }
    int port = ntohs(a.sin_port);

    HANDLE dup = NULL;
    BOOL dupOk = DuplicateHandle(GetCurrentProcess(), (HANDLE)l,
                                 GetCurrentProcess(), &dup, 0, FALSE,
                                 DUPLICATE_SAME_ACCESS);
    row(key, "port=%d duplicatehandle-on-socket=%d err=%lu", port, dupOk,
        dupOk ? 0 : GetLastError());
    if (dupOk)
        CloseHandle(dup);

    Child c;
    bool spawned;
    if (viaExec)
        spawned = spawnLikeExec(cmdline, c);
    else
    {
        STARTUPINFOA si;
        memset(&si, 0, sizeof si);
        si.cb = sizeof si;
        char cmd[1024];
        rc_strf(cmd, sizeof cmd, "%s", cmdline);
        spawned = CreateProcessA(NULL, cmd, NULL, NULL, FALSE, 0, NULL, g_dir,
                                 &si, &c.pi) != 0;
        if (spawned)
            g_spawned.push_back(c.pi.dwProcessId);
    }
    if (!spawned)
    {
        row(key, "spawn failed %lu", GetLastError());
        closesocket(l);
        return;
    }
    Sleep(1500);
    closesocket(l);
    Sleep(500);
    row(key, "after parent close, child alive: connect=%d rebind=%d",
        connectTo(port), bindAgain(port));
    if (!waitChild(c, 1))
    {
        TerminateProcess(c.pi.hProcess, 1);
        WaitForSingleObject(c.pi.hProcess, 3000);
    }
    Sleep(1000);
    row(key, "after child exit: connect=%d rebind=%d", connectTo(port),
        bindAgain(port));
    closeChild(c);
}

/* ---- 6. 16-bit exes ---- */

static void win16(const std::vector<Proc> &base)
{
    for (unsigned i = 0; i < base.size(); i++)
        if (exeKind(base[i].exe) == 'N')
            row("win16-running", "pid=%08lx exe=%s", base[i].pid,
                base[i].exe);
    char pat[MAX_PATH];
    char dir[MAX_PATH];
    GetWindowsDirectoryA(dir, sizeof dir);
    rc_strf(pat, sizeof pat, "%s\\*.EXE", dir);
    WIN32_FIND_DATAA fd;
    HANDLE h = FindFirstFileA(pat, &fd);
    int n = 0;
    if (h != INVALID_HANDLE_VALUE)
    {
        do
        {
            char p[MAX_PATH];
            rc_strf(p, sizeof p, "%s\\%s", dir, fd.cFileName);
            char k = exeKind(p);
            if (k != 'P')
                row("win16-disk", "kind=%c %s", k, p);
            n++;
        } while (FindNextFileA(h, &fd));
        FindClose(h);
    }
    row("win16-disk", "scanned=%d", n);
}

/* ---- 7. launch's show value ---- */

struct WinList
{
    std::vector<HWND> v;
};

static BOOL CALLBACK collect(HWND h, LPARAM lp)
{
    ((WinList *)lp)->v.push_back(h);
    return TRUE;
}

static bool hadWin(const WinList &w, HWND h)
{
    for (unsigned i = 0; i < w.v.size(); i++)
        if (w.v[i] == h)
            return true;
    return false;
}

static void newWindows(const char *key, const WinList &base, bool close)
{
    std::vector<Proc> procs;
    snapshot(procs);
    std::vector<DWORD> famv;
    family(procs, famv);
    const std::vector<DWORD> *fam = &famv;
    WinList now;
    EnumWindows(collect, (LPARAM)&now);
    int n = 0;
    for (unsigned i = 0; i < now.v.size(); i++)
    {
        HWND h = now.v[i];
        if (hadWin(base, h))
            continue;
        char cls[64], title[128];
        cls[0] = title[0] = 0;
        GetClassNameA(h, cls, sizeof cls);
        GetWindowTextA(h, title, sizeof title);
        DWORD pid = 0;
        GetWindowThreadProcessId(h, &pid);
        row(key, "hwnd=%08lx pid=%08lx visible=%d iconic=%d class=%s title=%s",
            (DWORD)h, pid, IsWindowVisible(h) != 0, IsIconic(h) != 0, cls,
            title);
        /* Close only our own: another agent may be using the box. */
        /* A finished DOS box belongs to the VM, not to anything we
         * spawned; one that appeared during our launch and says
         * "Finished" is ours by timing. */
        bool finished = strncmp(title, "Finished", 8) == 0;
        if (close && fam && (inList(*fam, pid) || finished))
        {
            PostMessageA(h, WM_CLOSE, 0, 0);
            row(key, "closed hwnd=%08lx", (DWORD)h);
        }
        n++;
    }
    row(key, "new-windows=%d", n);
}

static void launch(const char *key, const char *cmdline, WORD show,
                   const std::vector<Proc> &base)
{
    WinList before;
    EnumWindows(collect, (LPARAM)&before);
    STARTUPINFOA si;
    memset(&si, 0, sizeof si);
    si.cb = sizeof si;
    si.dwFlags = STARTF_USESHOWWINDOW;
    si.wShowWindow = show;
    PROCESS_INFORMATION pi;
    char cmd[1024];
    rc_strf(cmd, sizeof cmd, "%s", cmdline);
    if (!CreateProcessA(NULL, cmd, NULL, NULL, FALSE, 0, NULL, g_dir, &si, &pi))
    {
        row(key, "spawn failed %lu", GetLastError());
        return;
    }
    g_spawned.push_back(pi.dwProcessId);
    row(key, "show=%u pid=%08lx cmd=%s", show, pi.dwProcessId, cmdline);
    Sleep(2000);
    newWindows(key, before, false);
    DWORD w = WaitForSingleObject(pi.hProcess, 10000);
    row(key, "process-exited=%d", w == WAIT_OBJECT_0);
    if (w != WAIT_OBJECT_0)
        TerminateProcess(pi.hProcess, 1);
    Sleep(1500);
    row(key, "windows left after exit:");
    newWindows(key, before, true);
    CloseHandle(pi.hThread);
    CloseHandle(pi.hProcess);
    Sleep(1000);
    sweep(key, base);
}

/* ---- 8. probe for a clean stop (notes/libexec-plan.md step 0b) ---- */

/* The family's hidden DOS-box window, if any -- class "tty", owned by a pid
 * in `fam`. There is at most one per job; the first match is enough. */
static bool findTtyWindow(const std::vector<DWORD> &fam, HWND &out)
{
    WinList all;
    EnumWindows(collect, (LPARAM)&all);
    for (unsigned i = 0; i < all.v.size(); i++)
    {
        HWND h = all.v[i];
        char cls[32];
        cls[0] = 0;
        GetClassNameA(h, cls, sizeof cls);
        if (lstrcmpiA(cls, "tty") != 0)
            continue;
        DWORD pid = 0;
        GetWindowThreadProcessId(h, &pid);
        if (inList(fam, pid))
        {
            out = h;
            return true;
        }
    }
    return false;
}

static BOOL CALLBACK findButton(HWND h, LPARAM lp)
{
    char cls[32];
    cls[0] = 0;
    GetClassNameA(h, cls, sizeof cls);
    if (lstrcmpiA(cls, "Button") != 0)
        return TRUE;
    char txt[64];
    txt[0] = 0;
    GetWindowTextA(h, txt, sizeof txt);
    const char *t = txt;
    if (t[0] == '&')
        t++;
    if (t[0] == 'Y' || t[0] == 'y' || _strnicmp(txt, "OK", 2) == 0)
    {
        *(HWND *)lp = h;
        return FALSE;
    }
    return TRUE;
}

/* a. Close our end of the pipes (spawnLikeExec already closed the stdin
 * write end); the generic wait in runCase does the rest. */
static void stratPipes(const char *key, Child &c)
{
    c.out.reset();
    row(key, "closed our pipe ends (stdin write end already closed)");
}

/* b. GenerateConsoleCtrlEvent, resolved at runtime (9x may not export it),
 * against the spawned pid and against process group 0. */
static void stratCtrlBreak(const char *key, Child &c)
{
    typedef BOOL (WINAPI *GenFn)(DWORD, DWORD);
    HMODULE k = GetModuleHandleA("KERNEL32.DLL");
    GenFn gen = (GenFn)GetProcAddress(k, "GenerateConsoleCtrlEvent");
    row(key, "generateconsolectrlevent-resolved=%d", gen != NULL);
    if (!gen)
        return;
    BOOL b1 = gen(CTRL_BREAK_EVENT, c.pi.dwProcessId);
    DWORD e1 = b1 ? 0 : GetLastError();
    BOOL c1 = gen(CTRL_C_EVENT, c.pi.dwProcessId);
    DWORD ec1 = c1 ? 0 : GetLastError();
    row(key, "pgrp=pid(%08lx) break=%d err=%lu ctrlc=%d err=%lu",
        c.pi.dwProcessId, b1, e1, c1, ec1);
    BOOL b0 = gen(CTRL_BREAK_EVENT, 0);
    DWORD e0 = b0 ? 0 : GetLastError();
    BOOL c0 = gen(CTRL_C_EVENT, 0);
    DWORD ec0 = c0 ? 0 : GetLastError();
    row(key, "pgrp=0 break=%d err=%lu ctrlc=%d err=%lu", b0, e0, c0, ec0);
}

/* c. WM_CLOSE to the family's tty window, then look for a confirmation
 * dialog and press its Yes/OK button. Only windows belonging to our family,
 * or appearing within the 2s wait with pid 0 or in family, are touched --
 * the VM is shared with other agents. */
static void stratTtyClose(const char *key, const std::vector<DWORD> &fam)
{
    HWND tty;
    if (!findTtyWindow(fam, tty))
    {
        row(key, "no tty window");
        return;
    }
    row(key, "tty hwnd=%08lx", (DWORD)tty);
    WinList before;
    EnumWindows(collect, (LPARAM)&before);
    PostMessageA(tty, WM_CLOSE, 0, 0);
    Sleep(2000);
    WinList after;
    EnumWindows(collect, (LPARAM)&after);
    int newCount = 0, clicked = 0;
    for (unsigned i = 0; i < after.v.size(); i++)
    {
        HWND h = after.v[i];
        if (hadWin(before, h))
            continue;
        DWORD pid = 0;
        GetWindowThreadProcessId(h, &pid);
        if (!inList(fam, pid) && pid != 0)
            continue;
        newCount++;
        char cls[32], title[128];
        cls[0] = title[0] = 0;
        GetClassNameA(h, cls, sizeof cls);
        GetWindowTextA(h, title, sizeof title);
        row(key, "confirm hwnd=%08lx pid=%08lx class=%s title=%s", (DWORD)h,
            pid, cls, title);
        HWND btn = NULL;
        EnumChildWindows(h, findButton, (LPARAM)&btn);
        if (btn)
        {
            char btxt[64];
            btxt[0] = 0;
            GetWindowTextA(btn, btxt, sizeof btxt);
            SendMessageA(btn, BM_CLICK, 0, 0);
            row(key, "clicked button=%s on hwnd=%08lx", btxt, (DWORD)h);
            clicked++;
        }
    }
    row(key, "new-windows=%d clicked=%d", newCount, clicked);
}

/* d. Ctrl+C keystrokes posted to the family's tty window. */
static void stratTtyKeys(const char *key, const std::vector<DWORD> &fam)
{
    HWND tty;
    if (!findTtyWindow(fam, tty))
    {
        row(key, "no tty window");
        return;
    }
    row(key, "tty hwnd=%08lx sending ctrl+c", (DWORD)tty);
    PostMessageA(tty, WM_KEYDOWN, VK_CONTROL, 0);
    PostMessageA(tty, WM_CHAR, 3, 0);
    PostMessageA(tty, WM_KEYUP, VK_CONTROL, 0);
}

/* e. Kill the DOS VM and just wait for the spawned pid (REDIR32) to leave
 * by itself, up to 15s. Returns whether it did. */
static bool stratVmThenWait(const char *key, Child &c,
                            const std::vector<DWORD> &fam,
                            const std::vector<Proc> &now)
{
    DWORD vm = 0;
    for (unsigned i = 0; i < now.size(); i++)
    {
        const Proc &p = now[i];
        const char *name = strrchr(p.exe, '\\');
        name = name ? name + 1 : p.exe;
        if (inList(fam, p.pid) && lstrcmpiA(name, "WINOA386.MOD") == 0)
            vm = p.pid;
    }
    if (!vm)
    {
        row(key, "no vm");
        c.out.reset();
        return false;
    }
    HANDLE h = OpenProcess(PROCESS_TERMINATE | SYNCHRONIZE, FALSE, vm);
    BOOL ok = h ? TerminateProcess(h, 1) : FALSE;
    DWORD gone = h ? WaitForSingleObject(h, 5000) : WAIT_FAILED;
    if (h)
        CloseHandle(h);
    row(key, "vm pid=%08lx kill=%d vm-gone=%d", vm, ok, gone == WAIT_OBJECT_0);
    c.out.reset();
    DWORD t0 = GetTickCount();
    bool spawnedGone = waitChild(c, 15000);
    row(key, "spawned-gone-after-vmkill=%d after %lums", spawnedGone,
        GetTickCount() - t0);
    return spawnedGone;
}

/* Kills whatever of our family is left except REDIR32, which a
 * TerminateProcess never actually stops (notes/BACKLOG.md #14) -- leaving it
 * is the point of this probe, not a bug, so it is recorded, not swept. */
static void cleanupStops(const char *key, const std::vector<Proc> &base)
{
    std::vector<Proc> now;
    snapshot(now);
    std::vector<DWORD> fam;
    family(now, fam);
    int left = 0, killed = 0;
    for (unsigned i = 0; i < now.size(); i++)
    {
        const Proc &p = now[i];
        if (had(base, p.pid) || p.pid == GetCurrentProcessId() ||
            !inList(fam, p.pid))
            continue;
        const char *name = strrchr(p.exe, '\\');
        name = name ? name + 1 : p.exe;
        if (lstrcmpiA(name, "REDIR32.EXE") == 0)
        {
            row(key, "left zombie pid=%08lx exe=%s", p.pid, p.exe);
            left++;
            continue;
        }
        HANDLE h = OpenProcess(PROCESS_TERMINATE, FALSE, p.pid);
        BOOL ok = h ? TerminateProcess(h, 1) : FALSE;
        if (h)
            CloseHandle(h);
        row(key, "cleaned pid=%08lx exe=%s kill=%d", p.pid, p.exe, ok);
        killed++;
    }
    row(key, "cleanup left-redir=%d killed=%d", left, killed);
}

/* One case: spawn `cmdline` (its ticking observed through `tick`, or no tick
 * file if `tick[0]==0`), apply `strategy` (0=pipes 1=ctrlbreak 2=ttyclose
 * 3=ttykeys 4=vmthenwait), then watch for about 10s. */
static void runCase(const char *key, const char *cmdline, const char *tick,
                    const std::vector<Proc> &base, int strategy)
{
    MEMORYSTATUS ms;
    ms.dwLength = sizeof ms;
    GlobalMemoryStatus(&ms);
    DWORD memBefore = ms.dwAvailPhys / 1024;

    if (tick[0])
        DeleteFileA(tick);
    Child c;
    if (!spawnLikeExec(cmdline, c))
    {
        row(key, "spawn failed %lu", GetLastError());
        return;
    }
    row(key, "spawned pid=%08lx cmd=%s", c.pi.dwProcessId, cmdline);

    GlobalMemoryStatus(&ms);
    DWORD memAfterSpawn = ms.dwAvailPhys / 1024;

    Sleep(1500);   /* let the batch loop or --tick child get going */

    std::vector<Proc> now;
    snapshot(now);
    std::vector<DWORD> fam;
    family(now, fam);

    DWORD t0 = GetTickCount();
    bool spawnedGone = false;
    switch (strategy)
    {
    case 0: stratPipes(key, c); break;
    case 1: stratCtrlBreak(key, c); break;
    case 2: stratTtyClose(key, fam); break;
    case 3: stratTtyKeys(key, fam); break;
    case 4: spawnedGone = stratVmThenWait(key, c, fam, now); break;
    }

    DWORD sizes[3];
    for (int i = 0; i < 3; i++)
    {
        DWORD target = (DWORD)(i + 1) * 2000;
        DWORD elapsed = GetTickCount() - t0;
        if (elapsed < target)
        {
            if (!spawnedGone)
                spawnedGone = waitChild(c, target - elapsed);
            else
                Sleep(target - elapsed);
        }
        sizes[i] = tick[0] ? fileSize(tick) : 0xFFFFFFFF;
    }
    if (tick[0])
        row(key, "tick 2s/4s/6s: %ld %ld %ld -> %s", (long)sizes[0],
            (long)sizes[1], (long)sizes[2],
            sizes[2] != sizes[0] ? "STILL TICKING" : "stopped");
    else
        row(key, "tick n/a (job has no tick file)");

    DWORD code = 0;
    GetExitCodeProcess(c.pi.hProcess, &code);
    row(key, "spawned-exited=%d code=%lu", spawnedGone, code);

    snapshot(now);
    family(now, fam);
    int n = 0;
    for (unsigned i = 0; i < now.size(); i++)
    {
        const Proc &p = now[i];
        if (!inList(fam, p.pid))
            continue;
        row(key, "surviving pid=%08lx exe=%s threads=%lu", p.pid, p.exe,
            p.threads);
        n++;
    }
    row(key, "surviving=%d", n);

    DWORD elapsed = GetTickCount() - t0;
    if (elapsed < 10000)
    {
        if (!spawnedGone)
            spawnedGone = waitChild(c, 10000 - elapsed);
        else
            Sleep(10000 - elapsed);
    }
    GlobalMemoryStatus(&ms);
    DWORD memLater = ms.dwAvailPhys / 1024;
    row(key, "mem-kb before=%lu after-spawn=%lu 10s-after-action=%lu",
        memBefore, memAfterSpawn, memLater);

    cleanupStops(key, base);
    closeChild(c);
    if (tick[0])
        DeleteFileA(tick);
    Sleep(500);
}

/* Every failed case leaves a REDIR32 zombie by design, and the box has 32MB
 * and other users. Stop before the zombies or the memory they hold become
 * the experiment. */
static bool roomForAnother(const char *key)
{
    MEMORYSTATUS ms;
    ms.dwLength = sizeof ms;
    GlobalMemoryStatus(&ms);
    DWORD freeKb = ms.dwAvailPhys / 1024;
    std::vector<Proc> now;
    snapshot(now);
    int zombies = 0;
    for (unsigned i = 0; i < now.size(); i++)
    {
        const char *name = strrchr(now[i].exe, '\\');
        name = name ? name + 1 : now[i].exe;
        if (now[i].ppid == GetCurrentProcessId() &&
            lstrcmpiA(name, "REDIR32.EXE") == 0)
            zombies++;
    }
    if (freeKb < 3072 || zombies >= 20)
    {
        row(key, "NOT RUN, and the rest stopped: free=%luKB zombies=%d",
            freeKb, zombies);
        return false;
    }
    return true;
}

struct StopJob
{
    const char *tag;
    char cmd[1024];
    char tick[MAX_PATH];
    int reps;
};

static void runStops(const std::vector<Proc> &base, const char *bat,
                     const char *tickDos)
{
    char tick32[MAX_PATH], tick32d[MAX_PATH];
    char winDir[MAX_PATH], choicePath[MAX_PATH];
    rc_strf(tick32, sizeof tick32, "%s\\TICK32.TXT", g_dir);
    rc_strf(tick32d, sizeof tick32d, "%s\\TICK32D.TXT", g_dir);
    GetWindowsDirectoryA(winDir, sizeof winDir);
    rc_strf(choicePath, sizeof choicePath, "%s\\COMMAND\\CHOICE.COM", winDir);

    StopJob jobs[4];
    jobs[0].tag = "dos";
    rc_strf(jobs[0].cmd, sizeof jobs[0].cmd, "%s /c %s", g_shell, bat);
    rc_strcpy(jobs[0].tick, sizeof jobs[0].tick, tickDos);
    jobs[0].reps = 2;

    jobs[1].tag = "w32";
    rc_strf(jobs[1].cmd, sizeof jobs[1].cmd, "%s /c %s --tick %s 30", g_shell,
            g_self, tick32);
    rc_strcpy(jobs[1].tick, sizeof jobs[1].tick, tick32);
    jobs[1].reps = 2;

    jobs[2].tag = "w32direct";
    rc_strf(jobs[2].cmd, sizeof jobs[2].cmd, "%s --tick %s 30", g_self,
            tick32d);
    rc_strcpy(jobs[2].tick, sizeof jobs[2].tick, tick32d);
    jobs[2].reps = 1;

    jobs[3].tag = "dosdirect";
    rc_strf(jobs[3].cmd, sizeof jobs[3].cmd, "%s /c:y /t:y,30", choicePath);
    jobs[3].tick[0] = 0;
    jobs[3].reps = 1;

    static const char *stratNames[5] =
        { "pipes", "ctrlbreak", "ttyclose", "ttykeys", "vmthenwait" };

    /* Repetition outermost: every strategy gets its first try on every job
     * before anything is repeated, so the guard below cannot starve a
     * strategy of its only run. */
    for (int r = 0; r < 2; r++)
        for (int s = 0; s < 5; s++)
            for (int j = 0; j < 4; j++)
            {
                if (r >= jobs[j].reps)
                    continue;
                char key[64];
                rc_strf(key, sizeof key, "stop-%s-%s-%d", stratNames[s],
                        jobs[j].tag, r + 1);
                if (!roomForAnother(key))
                    return;
                runCase(key, jobs[j].cmd, jobs[j].tick, base, s);
            }
}

/* ---- 9. --redir: can output be captured without REDIR32? ----
 *
 * A piped COMMAND.COM job always gets REDIR32.EXE, which no stop can end
 * (--stops). A DOS program spawned directly left nothing. So: is it the
 * PIPE that brings REDIR32, or any std handle? Three ways to capture, each
 * on a quick command (does output arrive, does a normal end leave
 * anything?) and on a looping one (after a stop, does anything remain?):
 *   0 pipes        -- what exec does today
 *   1 file handle  -- an inheritable temp file as stdout/stderr, NUL as
 *                     stdin, STARTF_USESTDHANDLES
 *   2 no handles   -- " > file" on the command line, bInherit FALSE; the
 *                     shell does the redirection inside the VM */

static bool spawnRedir(int kind, const char *cmdline, const char *outPath,
                       Child &c)
{
    if (kind == 0)
        return spawnLikeExec(cmdline, c);
    if (kind == 3 || kind == 4)
    {
        char stub[1200];
        rc_strf(stub, sizeof stub, "%s %s %s", g_self,
                kind == 3 ? "--stub" : "--stubc", cmdline);
        return spawnLikeExec(stub, c);
    }
    char cmd[1200];
    STARTUPINFOA si;
    memset(&si, 0, sizeof si);
    si.cb = sizeof si;
    si.dwFlags = STARTF_USESHOWWINDOW;
    si.wShowWindow = SW_HIDE;
    DeleteFileA(outPath);
    BOOL ok;
    if (kind == 1)
    {
        SECURITY_ATTRIBUTES sa;
        memset(&sa, 0, sizeof sa);
        sa.nLength = sizeof sa;
        sa.bInheritHandle = TRUE;
        Handle out(CreateFileA(outPath, GENERIC_WRITE,
                               FILE_SHARE_READ | FILE_SHARE_WRITE, &sa,
                               CREATE_ALWAYS, 0, NULL));
        Handle in(CreateFileA("NUL", GENERIC_READ,
                              FILE_SHARE_READ | FILE_SHARE_WRITE, &sa,
                              OPEN_EXISTING, 0, NULL));
        if (!out.valid() || !in.valid())
            return false;
        si.dwFlags |= STARTF_USESTDHANDLES;
        si.hStdOutput = out.get();
        si.hStdError = out.get();
        si.hStdInput = in.get();
        rc_strf(cmd, sizeof cmd, "%s", cmdline);
        ok = CreateProcessA(NULL, cmd, NULL, NULL, TRUE, 0, NULL, g_dir, &si,
                            &c.pi);
    }
    else
    {
        rc_strf(cmd, sizeof cmd, "%s > %s", cmdline, outPath);
        ok = CreateProcessA(NULL, cmd, NULL, NULL, FALSE, 0, NULL, g_dir, &si,
                            &c.pi);
    }
    if (!ok)
        return false;
    g_spawned.push_back(c.pi.dwProcessId);
    return true;
}

static const char *nameOf(const char *path)
{
    const char *n = strrchr(path, '\\');
    return n ? n + 1 : path;
}

/* Everything new since `pre`, as "NAME xN" counts, into one row. */
static int reportNew(const char *key, const char *what,
                     const std::vector<Proc> &pre)
{
    std::vector<Proc> now;
    snapshot(now);
    char line[400];
    line[0] = 0;
    int n = 0;
    for (unsigned i = 0; i < now.size(); i++)
    {
        if (had(pre, now[i].pid) || now[i].pid == GetCurrentProcessId())
            continue;
        char one[80];
        rc_strf(one, sizeof one, "%s%s(t%lu,pp%08lx)", n ? " " : "",
                nameOf(now[i].exe), now[i].threads, now[i].ppid);
        size_t used = strlen(line);
        rc_strcpy(line + used, (unsigned)(sizeof line - used), one);
        n++;
    }
    row(key, "%s new=%d %s", what, n, line);
    return n;
}

/* Stop the tree under `root` deepest first, and `root` too unless it is
 * REDIR32 -- what ExecJob's stop does. */
static void stopTree(Child &c)
{
    std::vector<Proc> now;
    snapshot(now);
    std::vector<DWORD> order;
    order.push_back(c.pi.dwProcessId);
    for (unsigned k = 0; k < order.size(); k++)
        for (unsigned i = 0; i < now.size(); i++)
            if (now[i].ppid == order[k] && !inList(order, now[i].pid))
                order.push_back(now[i].pid);
    const char *rootName = "";
    for (unsigned i = 0; i < now.size(); i++)
        if (now[i].pid == c.pi.dwProcessId)
            rootName = nameOf(now[i].exe);
    for (unsigned k = order.size(); k-- > 1;)
    {
        HANDLE h = OpenProcess(PROCESS_TERMINATE, FALSE, order[k]);
        if (h)
        {
            TerminateProcess(h, 1);
            CloseHandle(h);
        }
    }
    if (lstrcmpiA(rootName, "REDIR32.EXE") != 0)
        TerminateProcess(c.pi.hProcess, 1);
}

static void redirCase(const char *key, int kind, const char *cmdline,
                      bool loops, const char *tick)
{
    std::vector<Proc> pre;
    snapshot(pre);
    MEMORYSTATUS ms;
    ms.dwLength = sizeof ms;
    GlobalMemoryStatus(&ms);
    DWORD memBefore = ms.dwAvailPhys / 1024;
    char outPath[MAX_PATH];
    rc_strf(outPath, sizeof outPath, "%s\\REDIROUT.TXT", g_dir);
    if (tick[0])
        DeleteFileA(tick);
    Child c;
    if (!spawnRedir(kind, cmdline, outPath, c))
    {
        row(key, "spawn failed %lu", GetLastError());
        return;
    }
    row(key, "kind=%d spawned pid=%08lx cmd=%s", kind, c.pi.dwProcessId,
        cmdline);
    DWORD piped = 0;
    if (!loops)
    {
        /* Count what a pipe delivers, as exec would read it. */
        DWORD t0 = GetTickCount();
        bool done = false;
        char buf[512];
        while (GetTickCount() - t0 < 15000)
        {
            DWORD avail = 0, got = 0;
            if (c.out.valid() &&
                PeekNamedPipe(c.out.get(), NULL, 0, NULL, &avail, NULL) &&
                avail &&
                ReadFile(c.out.get(), buf, avail < sizeof buf ? avail
                                                              : sizeof buf,
                         &got, NULL))
            {
                piped += got;
                continue;
            }
            if (WaitForSingleObject(c.pi.hProcess, 100) == WAIT_OBJECT_0)
            {
                done = true;
                break;
            }
        }
        reportNew(key, "while-running", pre);
        DWORD code = 0;
        GetExitCodeProcess(c.pi.hProcess, &code);
        Sleep(1500);
        DWORD fsz = fileSize(outPath);
        row(key, "exited=%d code=%lu piped=%lu file=%ld", done, code, piped,
            (long)fsz);
        if (!done)
            stopTree(c);
        Sleep(1500);
        reportNew(key, "left-after-normal-end", pre);
    }
    else
    {
        Sleep(3000);
        reportNew(key, "while-running", pre);
        DWORD before = tick[0] ? fileSize(tick) : 0;
        stopTree(c);
        c.out.reset();
        DWORD w = WaitForSingleObject(c.pi.hProcess, 5000);
        DWORD sizes[3];
        for (int i = 0; i < 3; i++)
        {
            Sleep(2000);
            sizes[i] = tick[0] ? fileSize(tick) : 0;
        }
        row(key, "spawned-gone=%d tick %ld -> %ld %ld %ld -> %s",
            w == WAIT_OBJECT_0, (long)before, (long)sizes[0],
            (long)sizes[1], (long)sizes[2],
            !tick[0] ? "n/a" : sizes[2] != sizes[0] ? "STILL TICKING"
                                                    : "stopped");
        Sleep(2000);
        reportNew(key, "left-after-stop", pre);
    }
    GlobalMemoryStatus(&ms);
    row(key, "mem-kb before=%lu after=%lu", memBefore, ms.dwAvailPhys / 1024);
    closeChild(c);
    DeleteFileA(outPath);
    if (tick[0])
        DeleteFileA(tick);
}

static void runRedir(const char *bat, const char *tickDos)
{
    char winDir[MAX_PATH], choice[MAX_PATH], tick32[MAX_PATH];
    GetWindowsDirectoryA(winDir, sizeof winDir);
    rc_strf(choice, sizeof choice, "%s\\COMMAND\\CHOICE.COM", winDir);
    rc_strf(tick32, sizeof tick32, "%s\\TICK32.TXT", g_dir);
    char quick[512], dosLoop[512], w32Loop[512], dQuick[512], dLoop[512];
    rc_strf(quick, sizeof quick, "%s /c dir %s", g_shell, g_dir);
    rc_strf(dosLoop, sizeof dosLoop, "%s /c %s", g_shell, bat);
    rc_strf(w32Loop, sizeof w32Loop, "%s /c %s --tick %s 30", g_shell, g_self,
            tick32);
    rc_strf(dQuick, sizeof dQuick, "%s /c:y /t:y,1", choice);
    rc_strf(dLoop, sizeof dLoop, "%s /c:y /t:y,60", choice);
    static const char *kinds[3] = { "pipe", "file", "cmdline" };
    for (int k = 0; k < 3; k++)
    {
        char key[64];
        rc_strf(key, sizeof key, "redir-%s-quick", kinds[k]);
        redirCase(key, k, quick, false, "");
        rc_strf(key, sizeof key, "redir-%s-dosloop", kinds[k]);
        redirCase(key, k, dosLoop, true, tickDos);
        rc_strf(key, sizeof key, "redir-%s-w32loop", kinds[k]);
        redirCase(key, k, w32Loop, true, tick32);
        if (k == 2)
            continue;           /* no shell to read a "> file" */
        rc_strf(key, sizeof key, "redir-%s-dosdirect-quick", kinds[k]);
        redirCase(key, k, dQuick, false, "");
        rc_strf(key, sizeof key, "redir-%s-dosdirect-loop", kinds[k]);
        redirCase(key, k, dLoop, true, "");
    }
}

/* --redir2: the same cases through a console stub (kinds 3 and 4). */
static void runRedir2(const char *bat, const char *tickDos)
{
    char winDir[MAX_PATH], choice[MAX_PATH], tick32[MAX_PATH];
    GetWindowsDirectoryA(winDir, sizeof winDir);
    rc_strf(choice, sizeof choice, "%s\\COMMAND\\CHOICE.COM", winDir);
    rc_strf(tick32, sizeof tick32, "%s\\TICK32.TXT", g_dir);
    char quick[512], dosLoop[512], w32Loop[512], dLoop[512];
    rc_strf(quick, sizeof quick, "%s /c dir %s", g_shell, g_dir);
    rc_strf(dosLoop, sizeof dosLoop, "%s /c %s", g_shell, bat);
    rc_strf(w32Loop, sizeof w32Loop, "%s /c %s --tick %s 30", g_shell, g_self,
            tick32);
    rc_strf(dLoop, sizeof dLoop, "%s /c:y /t:y,60", choice);
    static const char *kinds[2] = { "stub", "stubc" };
    for (int r = 0; r < 2; r++)
        for (int k = 0; k < 2; k++)
        {
            char key[64];
            rc_strf(key, sizeof key, "redir-%s-quick-%d", kinds[k], r + 1);
            redirCase(key, k + 3, quick, false, "");
            rc_strf(key, sizeof key, "redir-%s-dosloop-%d", kinds[k], r + 1);
            redirCase(key, k + 3, dosLoop, true, tickDos);
            rc_strf(key, sizeof key, "redir-%s-w32loop-%d", kinds[k], r + 1);
            redirCase(key, k + 3, w32Loop, true, tick32);
            rc_strf(key, sizeof key, "redir-%s-dosdirect-loop-%d", kinds[k],
                    r + 1);
            redirCase(key, k + 3, dLoop, true, "");
        }
}

/* --gentle: stop orders that never TerminateProcess the DOS VM first.
 * Every run so far says a VM killed that way is left half-dead, and every
 * process waiting on it (REDIR32, or a stub) can then never exit. So:
 *   w32only   -- kill only Win32 processes BELOW the VM, then wait up to 10s
 *                for the job (REDIR32 and the VM) to end by itself
 *   pipesfirst-- close our pipe ends, wait 2s, then kill the VM, then wait */
static void gentleCase(const char *key, const char *cmdline, const char *tick,
                       bool pipesFirst)
{
    std::vector<Proc> pre;
    snapshot(pre);
    MEMORYSTATUS ms;
    ms.dwLength = sizeof ms;
    GlobalMemoryStatus(&ms);
    DWORD memBefore = ms.dwAvailPhys / 1024;
    DeleteFileA(tick);
    Child c;
    if (!spawnLikeExec(cmdline, c))
    {
        row(key, "spawn failed %lu", GetLastError());
        return;
    }
    Sleep(3000);
    reportNew(key, "while-running", pre);
    std::vector<Proc> now;
    snapshot(now);
    std::vector<DWORD> tree;
    tree.push_back(c.pi.dwProcessId);
    for (unsigned k = 0; k < tree.size(); k++)
        for (unsigned i = 0; i < now.size(); i++)
            if (now[i].ppid == tree[k] && !inList(tree, now[i].pid))
                tree.push_back(now[i].pid);
    int killed = 0;
    if (pipesFirst)
    {
        c.out.reset();
        Sleep(2000);
    }
    for (unsigned k = tree.size(); k-- > 1;)
    {
        const char *exe = "";
        for (unsigned i = 0; i < now.size(); i++)
            if (now[i].pid == tree[k])
                exe = now[i].exe;
        bool vm = lstrcmpiA(nameOf(exe), "WINOA386.MOD") == 0;
        if (!pipesFirst && vm)
            continue;           /* w32only: never the VM */
        HANDLE h = OpenProcess(PROCESS_TERMINATE, FALSE, tree[k]);
        if (h)
        {
            TerminateProcess(h, 1);
            CloseHandle(h);
            killed++;
            row(key, "killed %s", nameOf(exe));
        }
    }
    if (!pipesFirst)
        c.out.reset();
    DWORD t0 = GetTickCount();
    DWORD w = WaitForSingleObject(c.pi.hProcess, 10000);
    DWORD sizes[3];
    for (int i = 0; i < 3; i++)
    {
        Sleep(2000);
        sizes[i] = fileSize(tick);
    }
    row(key, "killed=%d spawned-gone=%d after %lums tick %ld %ld %ld -> %s",
        killed, w == WAIT_OBJECT_0, GetTickCount() - t0, (long)sizes[0],
        (long)sizes[1], (long)sizes[2],
        sizes[2] != sizes[0] ? "STILL TICKING" : "stopped");
    Sleep(2000);
    reportNew(key, "left-after-stop", pre);
    GlobalMemoryStatus(&ms);
    row(key, "mem-kb before=%lu after=%lu", memBefore, ms.dwAvailPhys / 1024);
    closeChild(c);
    DeleteFileA(tick);
}

static void runGentle(const char *bat, const char *tickDos)
{
    char tick32[MAX_PATH], w32Loop[512], dosLoop[512];
    rc_strf(tick32, sizeof tick32, "%s\\TICK32.TXT", g_dir);
    rc_strf(w32Loop, sizeof w32Loop, "%s /c %s --tick %s 30", g_shell, g_self,
            tick32);
    rc_strf(dosLoop, sizeof dosLoop, "%s /c %s", g_shell, bat);
    for (int r = 1; r <= 2; r++)
    {
        char key[64];
        rc_strf(key, sizeof key, "gentle-w32only-w32loop-%d", r);
        gentleCase(key, w32Loop, tick32, false);
    }
    gentleCase("gentle-w32only-dosloop-1", dosLoop, tickDos, false);
    gentleCase("gentle-pipesfirst-w32loop-1", w32Loop, tick32, true);
    gentleCase("gentle-pipesfirst-dosloop-1", dosLoop, tickDos, true);
}

/* ---- child modes ---- */

/* --stub CMD / --stubc CMD: a Win32 CONSOLE process between a GUI parent
 * and a DOS job. KB150956's advice for redirecting DOS programs on 9x.
 * --stub hands the job our std handles explicitly; --stubc lets it inherit
 * the console. Exits with the job's code. */
static int stubMode(bool explicitHandles)
{
    const char *line = GetCommandLineA();
    const char *tag = explicitHandles ? "--stub " : "--stubc ";
    const char *at = strstr(line, tag);
    if (!at)
        return 250;
    char cmd[1024];
    rc_strf(cmd, sizeof cmd, "%s", at + strlen(tag));
    STARTUPINFOA si;
    memset(&si, 0, sizeof si);
    si.cb = sizeof si;
    if (explicitHandles)
    {
        si.dwFlags = STARTF_USESTDHANDLES;
        si.hStdInput = GetStdHandle(STD_INPUT_HANDLE);
        si.hStdOutput = GetStdHandle(STD_OUTPUT_HANDLE);
        si.hStdError = GetStdHandle(STD_ERROR_HANDLE);
    }
    PROCESS_INFORMATION pi;
    if (!CreateProcessA(NULL, cmd, NULL, NULL, TRUE, 0, NULL, NULL, &si, &pi))
        return 251;
    WaitForSingleObject(pi.hProcess, INFINITE);
    DWORD code = 0;
    GetExitCodeProcess(pi.hProcess, &code);
    CloseHandle(pi.hThread);
    CloseHandle(pi.hProcess);
    return (int)code;
}

static int childMode(int argc, char **argv)
{
    if (argc >= 3 && strcmp(argv[1], "--stub") == 0)
        return stubMode(true);
    if (argc >= 3 && strcmp(argv[1], "--stubc") == 0)
        return stubMode(false);
    if (argc >= 3 && strcmp(argv[1], "--sleep") == 0)
    {
        Sleep((DWORD)atoi(argv[2]) * 1000);
        return 0;
    }
    if (argc >= 3 && strcmp(argv[1], "--exit") == 0)
        return atoi(argv[2]);
    if (argc >= 4 && strcmp(argv[1], "--tick") == 0)
    {
        int n = atoi(argv[3]);
        for (int i = 0; i < n; i++)
        {
            FILE *f = fopen(argv[2], "a");
            if (f)
            {
                fputs("x\n", f);
                fclose(f);
            }
            Sleep(1000);
        }
        return 0;
    }
    return -1;
}

int main(int argc, char **argv)
{
    /* Run from a DOS prompt the probe is a console process, and its
     * children share that console and its DOS VM: not what the client or
     * gremote (GUI, no console) see. So by default it relaunches itself
     * detached and returns; `--run` is that copy, `--console` runs in
     * place for comparison. */
    bool inPlace = argc > 1 && (strcmp(argv[1], "--run") == 0 ||
                                strcmp(argv[1], "--console") == 0 ||
                                strcmp(argv[1], "--list") == 0 ||
                                strcmp(argv[1], "--kills") == 0 ||
                                strcmp(argv[1], "--vmfirst") == 0 ||
                                strcmp(argv[1], "--stops") == 0 ||
                                strcmp(argv[1], "--redir") == 0 ||
                                strcmp(argv[1], "--redir2") == 0 ||
                                strcmp(argv[1], "--gentle") == 0);
    const char *mode = argc > 1 ? argv[1] : "--run";
    if (argc > 1 && !inPlace && strcmp(argv[1], "--detach") != 0)
        return childMode(argc, argv);
    if (!inPlace)
    {
        char self[MAX_PATH], cmd[MAX_PATH + 16];
        GetModuleFileNameA(NULL, self, sizeof self);
        rc_strf(cmd, sizeof cmd, "%s %s", self,
                argc > 2 ? argv[2] : "--run");
        STARTUPINFOA si;
        memset(&si, 0, sizeof si);
        si.cb = sizeof si;
        PROCESS_INFORMATION pi;
        if (!CreateProcessA(NULL, cmd, NULL, NULL, FALSE, DETACHED_PROCESS,
                            NULL, NULL, &si, &pi))
        {
            printf("relaunch failed: %lu\n", GetLastError());
            return 1;
        }
        CloseHandle(pi.hThread);
        CloseHandle(pi.hProcess);
        printf("running detached; results in EXECPRB.TXT (last row: done)\n");
        return 0;
    }

    GetModuleFileNameA(NULL, g_self, sizeof g_self);
    rc_strf(g_dir, sizeof g_dir, "%s", g_self);
    char *slash = strrchr(g_dir, '\\');
    if (slash)
        *slash = 0;
    char outPath[MAX_PATH];
    rc_strf(outPath, sizeof outPath, "%s\\EXECPRB.TXT", g_dir);
    g_out = fopen(outPath, "w");
    if (!g_out)
        return 1;
    const char *comspec = getenv("COMSPEC");
    rc_strf(g_shell, sizeof g_shell, "%s", comspec ? comspec : "COMMAND.COM");
    OSVERSIONINFOA ov;
    memset(&ov, 0, sizeof ov);
    ov.dwOSVersionInfoSize = sizeof ov;
    GetVersionExA(&ov);
    row("os", "platform=%lu %lu.%lu.%lu %s", ov.dwPlatformId,
        ov.dwMajorVersion, ov.dwMinorVersion, ov.dwBuildNumber & 0xFFFF,
        ov.szCSDVersion);
    row("paths", "self=%s shell=%s mode=%s", g_self, g_shell, mode);

    std::vector<Proc> base;
    toolhelp(base);
    if (strcmp(mode, "--list") == 0)
    {
        row("done", "ok");
        fclose(g_out);
        return 0;
    }
    bool killsOnly = strcmp(mode, "--kills") == 0;
    bool vmFirst = strcmp(mode, "--vmfirst") == 0;
    bool stopsMode = strcmp(mode, "--stops") == 0;
    bool redirMode = strcmp(mode, "--redir") == 0;
    bool redir2Mode = strcmp(mode, "--redir2") == 0;
    bool gentleMode = strcmp(mode, "--gentle") == 0;
    if (!killsOnly && !vmFirst && !stopsMode && !redirMode && !redir2Mode &&
        !gentleMode)
        win16(base);

    char cmd[1024], tick[MAX_PATH], bat[MAX_PATH];

    /* 2. DOS grandchild: a batch loop under the shell, CHOICE as the DOS
     * program it runs. */
    rc_strf(tick, sizeof tick, "%s\\TICKD.TXT", g_dir);
    rc_strf(bat, sizeof bat, "%s\\EPLOOP.BAT", g_dir);
    {
        FILE *f = fopen(bat, "wb");   /* text mode made \r\r\n */
        if (f)
        {
            fprintf(f, "@echo off\r\n:top\r\necho x>>%s\r\n"
                       "choice /c:y /t:y,1 /n >nul\r\ngoto top\r\n", tick);
            fclose(f);
        }
    }
    rc_strf(cmd, sizeof cmd, "%s /c %s", g_shell, bat);
    if (gentleMode)
    {
        runGentle(bat, tick);
        DeleteFileA(bat);
        row("done", "ok");
        fclose(g_out);
        return 0;
    }
    if (redir2Mode)
    {
        runRedir2(bat, tick);
        DeleteFileA(bat);
        row("done", "ok");
        fclose(g_out);
        return 0;
    }
    if (redirMode)
    {
        runRedir(bat, tick);
        DeleteFileA(bat);
        row("done", "ok");
        fclose(g_out);
        return 0;
    }
    if (stopsMode)
    {
        runStops(base, bat, tick);
        DeleteFileA(bat);
        sweep("final", base);
        row("done", "ok");
        fclose(g_out);
        return 0;
    }
    if (vmFirst)
    {
        killVmFirst("vmfirst-dos", cmd, tick, base);
        rc_strf(tick, sizeof tick, "%s\\TICK32.TXT", g_dir);
        rc_strf(cmd, sizeof cmd, "%s /c %s --tick %s 30", g_shell, g_self,
                tick);
        killVmFirst("vmfirst-w32", cmd, tick, base);
        DeleteFileA(bat);
        row("done", "ok");
        fclose(g_out);
        return 0;
    }
    killShell("dos-kill", cmd, tick, base);

    /* 3. Win32 console grandchild. */
    rc_strf(tick, sizeof tick, "%s\\TICK32.TXT", g_dir);
    rc_strf(cmd, sizeof cmd, "%s /c %s --tick %s 30", g_shell, g_self, tick);
    killShell("w32-kill", cmd, tick, base);

    if (killsOnly)
    {
        DeleteFileA(bat);
        sweep("final", base);
        row("done", "ok");
        fclose(g_out);
        return 0;
    }

    /* 4. Exit codes. */
    rc_strf(cmd, sizeof cmd, "%s --exit 7", g_self);
    exitCode("exit-direct", cmd);
    rc_strf(cmd, sizeof cmd, "%s /c %s --exit 7", g_shell, g_self);
    exitCode("exit-shell-w32", cmd);
    rc_strf(cmd, sizeof cmd, "%s /c choice /c:abc /t:b,1 /n", g_shell);
    exitCode("exit-shell-dos", cmd);
    rc_strf(cmd, sizeof cmd, "%s /c %s\\NOSUCH.EXE", g_shell, g_dir);
    exitCode("exit-shell-missing", cmd);
    rc_strf(cmd, sizeof cmd, "%s /c exit", g_shell);
    exitCode("exit-shell-plain", cmd);

    /* 5. Socket inheritance. */
    WSADATA wsa;
    if (WSAStartup(MAKEWORD(1, 1), &wsa) == 0)
    {
        rc_strf(cmd, sizeof cmd, "%s --sleep 8", g_self);
        inherit("sock-exec-direct", cmd, true);
        rc_strf(cmd, sizeof cmd, "%s /c %s --sleep 8", g_shell, g_self);
        inherit("sock-exec-shell", cmd, true);
        rc_strf(cmd, sizeof cmd, "%s --sleep 8", g_self);
        inherit("sock-noinherit", cmd, false);
        WSACleanup();
    }
    else
        row("sock", "WSAStartup failed");

    /* 7. launch's show value. */
    rc_strf(cmd, sizeof cmd, "%s /c choice /c:y /t:y,4 /n", g_shell);
    launch("launch-dos-hide", cmd, SW_HIDE, base);
    launch("launch-dos-min", cmd, SW_SHOWMINNOACTIVE, base);
    launch("launch-dos-normal", cmd, SW_SHOWNORMAL, base);
    rc_strf(cmd, sizeof cmd, "%s --sleep 4", g_self);
    launch("launch-w32-hide", cmd, SW_HIDE, base);
    launch("launch-w32-normal", cmd, SW_SHOWNORMAL, base);

    DeleteFileA(bat);
    sweep("final", base);
    row("done", "ok");
    fclose(g_out);
    return 0;
}
