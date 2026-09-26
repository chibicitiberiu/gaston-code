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
 *
 * Modes: no argument relaunches detached (like a GUI parent) running
 * everything; --detach MODE relaunches detached running MODE; --run all,
 * --kills only 2 and 3, --list only the process list, in place.
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

/* As exec.cpp does it: one inheritable pipe for stdout+stderr, a stdin whose
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

/* ---- child modes ---- */

static int childMode(int argc, char **argv)
{
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
                                strcmp(argv[1], "--vmfirst") == 0);
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
    if (!killsOnly && !vmFirst)
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
