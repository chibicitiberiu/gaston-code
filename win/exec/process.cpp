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

bool Processes::list(ProcessSink &sink, bool readKinds)
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
        fill(p, pe, readKinds);
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
    if (!force)
    {
        std::vector<DWORD> below;
        descendants(pid, below);
        for (unsigned i = 0; i < below.size(); i++)
            for (unsigned j = 0; j < all.size(); j++)
                if (all[j].pid == below[i] &&
                    lstrcmpiA(baseName(all[j].exe), "WINOA386.MOD") == 0)
                {
                    rc_log("process: kill %08lx (%s) refused without force: "
                           "a DOS VM runs below it", pid, name);
                    return KILL_DOS_SESSION;
                }
    }

    /* FORCE: TAKE THE WIN32 DESCENDANTS DOWN FIRST, AND RECLAIM AN ORPHANED
     * VM AFTERWARDS. BACKLOG 17.
     *
     * Without this, forcing a console program leaves two kinds of wreckage.
     * Its Win32 children are reparented to nothing and keep running; and
     * the WINOA386.MOD hosting its console stays, one thread, until reboot
     * -- the leak the refusal above exists to avoid, which `force` was
     * previously just a way to opt into. A guest carrying those later
     * blue-screened in VMM.
     *
     * Children first, then the target, because the other order gives a
     * child time to notice its parent went and do something about it.
     *
     * THE VM IS ONLY TOUCHED WHEN IT IS DEMONSTRABLY ORPHANED: after the
     * target is confirmed gone we look again, and terminate a WINOA386.MOD
     * that was below it only if nothing else is above it any more. A VM
     * still owned by a live process is somebody else's DOS box. */
    std::vector<DWORD> below;
    if (force)
    {
        descendants(pid, below);
        for (unsigned i = 0; i < below.size(); i++)
        {
            const ProcessInfo *c = 0;
            for (unsigned j = 0; j < all.size(); j++)
                if (all[j].pid == below[i])
                    c = &all[j];
            if (!c)
                continue;
            const char *cn = baseName(c->exe);
            /* The VM is left for the orphan check below, and the
             * never-kill list applies to a descendant exactly as it does
             * to a target -- REDIR32 in particular cannot be killed at
             * all, and trying leaves a zombie. */
            if (lstrcmpiA(cn, "WINOA386.MOD") == 0)
                continue;
            bool skip = false;
            for (unsigned k = 0; k < sizeof always / sizeof always[0]; k++)
                if (lstrcmpiA(cn, always[k]) == 0)
                    skip = true;
            if (skip)
                continue;
            Handle ch(OpenProcess(PROCESS_TERMINATE, FALSE, c->pid));
            if (ch.valid() && TerminateProcess(ch.get(), 1))
                rc_log("process: killed %08lx (%s), below %08lx", c->pid,
                       cn, pid);
        }
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
    if (WaitForSingleObject(h.get(), waitMs) != WAIT_OBJECT_0)
    {
        rc_log("process: %08lx (%s) told to die, still there after %lums",
               pid, name, waitMs);
        return KILL_PENDING;
    }
    if (force && !below.empty())
        reclaimOrphanVms(below);
    return KILL_OK;
}

/* A DOS VM that was below a process we have just confirmed dead, and that
 * nothing else is above any more. See the note in kill(). Best effort by
 * design: failing to reclaim one is the behaviour this project had all
 * along, so it is logged and never turned into a failed kill. */
void Processes::reclaimOrphanVms(const std::vector<DWORD> &wasBelow)
{
    std::vector<ProcessInfo> now;
    if (!snapshot(now, false))
        return;
    for (unsigned i = 0; i < now.size(); i++)
    {
        if (!contains(wasBelow, now[i].pid))
            continue;
        if (lstrcmpiA(baseName(now[i].exe), "WINOA386.MOD") != 0)
            continue;
        /* Still owned? Then it is not ours to reclaim. */
        bool owned = false;
        for (unsigned j = 0; j < now.size(); j++)
            if (now[j].pid == now[i].ppid)
                owned = true;
        if (owned)
        {
            rc_log("process: DOS VM %08lx stays: %08lx is still above it",
                   now[i].pid, now[i].ppid);
            continue;
        }
        Handle vh(OpenProcess(PROCESS_TERMINATE, FALSE, now[i].pid));
        if (vh.valid() && TerminateProcess(vh.get(), 1))
            rc_log("process: reclaimed orphaned DOS VM %08lx", now[i].pid);
        else
            rc_log("process: orphaned DOS VM %08lx could not be reclaimed "
                   "(%lu); it will stay until reboot", now[i].pid,
                   GetLastError());
    }
}

/* The DOS side of a job on 98: the VM, the redirector, or a 16-bit/DOS
 * image. Terminating any of these leaves the VM half-dead and whatever
 * waits on it stuck for good (measured, execprobe --stops/--redir). A name
 * whose header cannot be read -- NT and Wine list bare names -- counts as
 * Win32, which is what it is there. */
static bool isDosSide(const char *exe)
{
    const char *name = baseName(exe);
    if (lstrcmpiA(name, "WINOA386.MOD") == 0 ||
        lstrcmpiA(name, "REDIR32.EXE") == 0)
        return true;
    Handle f(CreateFileA(exe, GENERIC_READ,
                         FILE_SHARE_READ | FILE_SHARE_WRITE, NULL,
                         OPEN_EXISTING, 0, NULL));
    if (!f.valid())
        return false;
    unsigned char mz[64];
    DWORD got = 0;
    if (!ReadFile(f.get(), mz, sizeof mz, &got, NULL) || got < 2)
        return false;
    if (mz[0] != 'M' || mz[1] != 'Z')
        return true;                        /* a .COM image */
    if (got < sizeof mz)
        return true;
    DWORD off = mz[0x3c] | (mz[0x3d] << 8) | ((DWORD)mz[0x3e] << 16) |
                ((DWORD)mz[0x3f] << 24);
    unsigned char sig[2];
    if (SetFilePointer(f.get(), (LONG)off, NULL, FILE_BEGIN) == 0xFFFFFFFF ||
        !ReadFile(f.get(), sig, 2, &got, NULL) || got != 2)
        return true;
    return !(sig[0] == 'P' && sig[1] == 'E');
}

static void terminate(DWORD pid)
{
    Handle h(OpenProcess(PROCESS_TERMINATE, FALSE, pid));
    if (h.valid())
        TerminateProcess(h.get(), 1);
}

bool Processes::stopJobTree(DWORD root, HANDLE rootProcess, DWORD graceMs,
                            DWORD finalMs)
{
    std::vector<ProcessInfo> all;
    std::vector<DWORD> below;
    const char *rootExe = "";
    if (m_snap && snapshot(all, false))
    {
        for (unsigned i = 0; i < all.size(); i++)
            if (all[i].pid == root)
                rootExe = all[i].exe;
        descendants(root, below);
    }

    /* 1. The Win32 side, deepest first. On 98 the job then ends BY ITSELF
     *    once its program is gone -- COMMAND.COM finishes, the VM exits,
     *    REDIR32 exits -- and nothing is left (execprobe --gentle, twice). */
    int spared = 0;
    for (unsigned i = below.size(); i-- > 0;)
    {
        const char *exe = "";
        for (unsigned j = 0; j < all.size(); j++)
            if (all[j].pid == below[i])
                exe = all[j].exe;
        if (isDosSide(exe))
            spared++;
        else
            terminate(below[i]);
    }
    if (!rootExe[0] || !isDosSide(rootExe))
        TerminateProcess(rootProcess, 1);
    if (WaitForSingleObject(rootProcess, graceMs) == WAIT_OBJECT_0)
        return true;

    /* 2. Still running: a DOS program inside the VM, which nothing gentle
     *    ends. The last resort kills the VM; the command stops, and on 98
     *    the REDIR32 waiting on it becomes a zombie until reboot. */
    rc_log("process: job %08lx (%s) still running %lums after its Win32 "
           "side was stopped; killing the DOS side (%d)", root,
           rootExe[0] ? baseName(rootExe) : "?", graceMs, spared);
    if (m_snap)
        descendants(root, below);
    for (unsigned i = below.size(); i-- > 0;)
        terminate(below[i]);
    if (!rootExe[0] || lstrcmpiA(baseName(rootExe), "REDIR32.EXE") != 0)
        TerminateProcess(rootProcess, 1);
    if (WaitForSingleObject(rootProcess, finalMs) == WAIT_OBJECT_0)
        return true;
    rc_log("process: job %08lx (%s) still there %lums after that",
           root, rootExe[0] ? baseName(rootExe) : "?", finalMs);
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
