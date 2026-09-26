#include "exec/process.h"

#include <string.h>
#include "core/rc_cpp.h"
#include "core/log.h"
#include "core/str.h"

/* The command-line cap, as ExecJob's: refused beyond it, never shortened. */
#define LAUNCH_MAX_CMD 1024

Processes::Processes()
    : m_snap(0),
      m_first(0),
      m_next(0)
{
}

bool Processes::init()
{
    HMODULE k = GetModuleHandleA("KERNEL32.DLL");
    if (!k)
        return false;
    SnapFn snap = (SnapFn)GetProcAddress(k, "CreateToolhelp32Snapshot");
    WalkFn first = (WalkFn)GetProcAddress(k, "Process32First");
    WalkFn next = (WalkFn)GetProcAddress(k, "Process32Next");
    if (!snap || !first || !next)
    {
        rc_log("process: no Toolhelp32 here; list and kill are unavailable");
        return false;
    }
    m_snap = snap;
    m_first = first;
    m_next = next;
    return true;
}

static void fill(ProcessInfo &p, const PROCESSENTRY32 &pe, bool withKind)
{
    p.pid = pe.th32ProcessID;
    p.ppid = pe.th32ParentProcessID;
    p.threads = pe.cntThreads;
    rc_strcpy(p.exe, sizeof p.exe, pe.szExeFile);
    p.win16 = withKind && Processes::isWin16Exe(p.exe);
}

bool Processes::list(ProcessSink &sink)
{
    if (!m_snap)
        return false;
    Handle h(m_snap(TH32CS_SNAPPROCESS, 0));
    if (!h.valid())
        return false;
    PROCESSENTRY32 pe;
    memset(&pe, 0, sizeof pe);
    pe.dwSize = sizeof pe;
    for (BOOL ok = m_first(h.get(), &pe); ok; ok = m_next(h.get(), &pe))
    {
        ProcessInfo p;
        fill(p, pe, true);
        if (!sink.onProcess(p))
            break;
        pe.dwSize = sizeof pe;
    }
    return true;
}

bool Processes::snapshot(std::vector<ProcessInfo> &out, bool withKind)
{
    out.clear();
    if (!m_snap)
        return false;
    Handle h(m_snap(TH32CS_SNAPPROCESS, 0));
    if (!h.valid())
        return false;
    PROCESSENTRY32 pe;
    memset(&pe, 0, sizeof pe);
    pe.dwSize = sizeof pe;
    for (BOOL ok = m_first(h.get(), &pe); ok; ok = m_next(h.get(), &pe))
    {
        ProcessInfo p;
        fill(p, pe, withKind);
        out.push_back(p);
        pe.dwSize = sizeof pe;
    }
    return true;
}

static bool contains(const std::vector<DWORD> &v, DWORD pid)
{
    for (unsigned i = 0; i < v.size(); i++)
        if (v[i] == pid)
            return true;
    return false;
}

bool Processes::descendants(DWORD root, std::vector<DWORD> &out)
{
    out.clear();
    std::vector<ProcessInfo> all;
    if (!snapshot(all, false))
        return false;
    /* Breadth-first over parent pids. A pid can repeat as a parent only if
     * 9x reused it inside one snapshot, which the containment check makes
     * harmless rather than a loop. */
    std::vector<DWORD> frontier;
    frontier.push_back(root);
    while (!frontier.empty())
    {
        std::vector<DWORD> next;
        for (unsigned i = 0; i < all.size(); i++)
        {
            DWORD pid = all[i].pid;
            if (pid == root || contains(out, pid))
                continue;
            if (contains(frontier, all[i].ppid))
            {
                out.push_back(pid);
                next.push_back(pid);
            }
        }
        frontier.swap(next);
    }
    return true;
}

static const char *baseName(const char *path)
{
    const char *s = strrchr(path, '\\');
    return s ? s + 1 : path;
}

KillStatus Processes::kill(DWORD pid, bool force, DWORD waitMs)
{
    if (!m_snap)
        return KILL_NO_TOOLHELP;
    if (pid == GetCurrentProcessId())
        return KILL_DENIED;

    std::vector<ProcessInfo> all;
    if (!snapshot(all, false))
        return KILL_NO_TOOLHELP;
    const ProcessInfo *p = 0;
    for (unsigned i = 0; i < all.size(); i++)
        if (all[i].pid == pid)
            p = &all[i];
    if (!p)
        return KILL_NO_SUCH_PID;

    static const char *const always[] = {
        "KERNEL32.DLL", "MSGSRV32.EXE", "MPREXE.EXE", "MMTASK.TSK",
        "REDIR32.EXE"
    };
    const char *name = baseName(p->exe);
    for (unsigned i = 0; i < sizeof always / sizeof always[0]; i++)
        if (lstrcmpiA(name, always[i]) == 0)
        {
            rc_log("process: kill %08lx (%s) refused: never", pid, name);
            return KILL_DENIED;
        }
    if (!force && (lstrcmpiA(name, "EXPLORER.EXE") == 0 ||
                   isWin16Exe(p->exe)))
    {
        rc_log("process: kill %08lx (%s) refused without force", pid, name);
        return KILL_DENIED;
    }

    Handle h(OpenProcess(PROCESS_TERMINATE | SYNCHRONIZE, FALSE, pid));
    if (!h.valid())
    {
        rc_log("process: OpenProcess %08lx failed: %lu", pid, GetLastError());
        return KILL_FAILED;
    }
    if (!TerminateProcess(h.get(), 1))
    {
        rc_log("process: TerminateProcess %08lx failed: %lu", pid,
               GetLastError());
        return KILL_FAILED;
    }
    if (WaitForSingleObject(h.get(), waitMs) == WAIT_OBJECT_0)
        return KILL_OK;
    rc_log("process: %08lx (%s) told to die, still there after %lums", pid,
           name, waitMs);
    return KILL_PENDING;
}

bool Processes::stopJobTree(DWORD root, HANDLE rootProcess, DWORD waitMs)
{
    const char *rootName = "";
    std::vector<ProcessInfo> all;
    std::vector<DWORD> below;
    if (m_snap && snapshot(all, false))
    {
        for (unsigned i = 0; i < all.size(); i++)
            if (all[i].pid == root)
                rootName = baseName(all[i].exe);
        descendants(root, below);
    }
    /* Deepest first: breadth-first order reversed. On 98 that is the
     * program, then the DOS VM (WINOA386.MOD), whose death takes a Win32
     * program with it at once and a DOS one within about 30s. */
    for (unsigned i = below.size(); i-- > 0;)
    {
        Handle h(OpenProcess(PROCESS_TERMINATE, FALSE, below[i]));
        if (h.valid())
            TerminateProcess(h.get(), 1);
    }
    bool redir = lstrcmpiA(rootName, "REDIR32.EXE") == 0;
    if (!redir)
        TerminateProcess(rootProcess, 1);
    if (WaitForSingleObject(rootProcess, waitMs) == WAIT_OBJECT_0)
        return true;
    rc_log("process: job %08lx (%s) still there %lums after its tree was "
           "stopped, %u below it", root, rootName[0] ? rootName : "?", waitMs,
           (unsigned)below.size());
    return false;
}

bool Processes::launch(const char *command, const char *cwd, int show,
                       DWORD &pid, DWORD &error)
{
    pid = 0;
    error = 0;
    char cmd[LAUNCH_MAX_CMD];
    if (!command || !*command)
    {
        error = ERROR_INVALID_PARAMETER;
        return false;
    }
    if (!rc_strf(cmd, sizeof cmd, "%s", command))
    {
        rc_log("process: launch command too long (%u bytes), refused",
               (unsigned)strlen(command));
        error = ERROR_FILENAME_EXCED_RANGE;
        return false;
    }
    STARTUPINFOA si;
    memset(&si, 0, sizeof si);
    si.cb = sizeof si;
    si.dwFlags = STARTF_USESHOWWINDOW;
    si.wShowWindow = (WORD)show;
    PROCESS_INFORMATION pi;
    /* bInherit FALSE: nothing of ours -- a socket, a pipe -- follows a
     * program that is meant to outlive the request. */
    if (!CreateProcessA(NULL, cmd, NULL, NULL, FALSE, 0, NULL,
                        (cwd && *cwd) ? cwd : NULL, &si, &pi))
    {
        error = GetLastError();
        rc_log("process: launch failed: %lu (%s)", error, cmd);
        return false;
    }
    CloseHandle(pi.hThread);
    CloseHandle(pi.hProcess);
    pid = pi.dwProcessId;
    return true;
}

bool Processes::isWin16Exe(const char *path)
{
    Handle f(CreateFileA(path, GENERIC_READ,
                         FILE_SHARE_READ | FILE_SHARE_WRITE, NULL,
                         OPEN_EXISTING, 0, NULL));
    if (!f.valid())
        return false;
    unsigned char mz[64];
    DWORD got = 0;
    if (!ReadFile(f.get(), mz, sizeof mz, &got, NULL) || got < sizeof mz ||
        mz[0] != 'M' || mz[1] != 'Z')
        return false;
    DWORD off = mz[0x3c] | (mz[0x3d] << 8) | ((DWORD)mz[0x3e] << 16) |
                ((DWORD)mz[0x3f] << 24);
    unsigned char sig[2];
    if (SetFilePointer(f.get(), (LONG)off, NULL, FILE_BEGIN) == 0xFFFFFFFF ||
        !ReadFile(f.get(), sig, 2, &got, NULL) || got != 2)
        return false;
    return sig[0] == 'N' && sig[1] == 'E';
}
