/* Processes on this machine: list, kill, launch. libexec; libcore only.
 *
 * Toolhelp32 is the only way to enumerate processes on 9x -- Windows 98 has
 * no tasklist and no taskkill -- and it is ABSENT on NT 4, so it is resolved
 * with GetProcAddress, never imported: one NT-only or 9x-only static import
 * kills the image at load. Without it list/kill/descendants refuse
 * (init() returns false); launch works everywhere.
 *
 * What the guest showed (docs/measurements/execprobe-vm-*-20260926.txt):
 *   - a snapshot is cheap: 20 of them in 5-10ms, 9-12 processes on a stock
 *     box, so every call takes a fresh one and nothing is cached;
 *   - pids have the high bit set (they are obfuscated pointers): always
 *     DWORD, printed unsigned;
 *   - TerminateProcess returning TRUE does NOT mean the process is gone.
 *     REDIR32.EXE never goes; a DOS VM takes up to ~30s. Hence KILL_PENDING.
 *
 * THREADS. Any thread; no state is shared between calls. kill() waits up to
 * the caller's budget, and gremote runs KILL inline on its reader, which must
 * stay responsive for the heartbeat, so it passes a short one.
 */
#ifndef RC_EXEC_PROCESS_H
#define RC_EXEC_PROCESS_H

#ifndef __cplusplus
#error "exec/process.h is C++"
#endif

#include <windows.h>
#include <tlhelp32.h>
#include <vector>

struct ProcessInfo
{
    DWORD pid;
    DWORD ppid;
    DWORD threads;
    char exe[MAX_PATH];     /* full path on 9x, a bare name on NT */
    bool win16;             /* a 16-bit NE image: Win16 app or the DOS VM */
};

/* Called once per process; false stops the listing. */
class ProcessSink
{
public:
    virtual ~ProcessSink() {}
    virtual bool onProcess(const ProcessInfo &p) = 0;
};

enum KillStatus
{
    KILL_OK = 0,            /* gone within the wait */
    KILL_PENDING,           /* TerminateProcess said yes; still there */
    KILL_DENIED,            /* on the deny list (see kill) */
    KILL_DOS_SESSION,       /* runs a DOS VM; refused without force */
    KILL_NO_SUCH_PID,
    KILL_FAILED,            /* OpenProcess or TerminateProcess refused */
    KILL_NO_TOOLHELP        /* cannot see names, so cannot apply the list */
};

class Processes
{
public:
    Processes();

    /* Resolves Toolhelp. False: it is not there (NT 4). */
    bool init();
    bool available() const { return m_snap != 0; }

    /* Streams one ProcessInfo per process. False if no snapshot.
     * `readKinds` false skips opening every exe for its header (`win16`
     * stays false): the client lists on its READER thread, which must
     * never wait on the disk. */
    bool list(ProcessSink &sink, bool readKinds = true);

    /* Every process below `root`, by parent pid, root excluded. The order is
     * breadth-first: children before grandchildren. */
    bool descendants(DWORD root, std::vector<DWORD> &out);

    /* Terminate `pid` and wait up to `waitMs` for it to go.
     *
     * Always denied: this process, KERNEL32.DLL, MSGSRV32.EXE, MPREXE.EXE and
     * MMTASK.TSK (killing them can hang 9x), and REDIR32.EXE (it cannot be
     * killed: the call succeeds and leaves a zombie). Denied unless `force`:
     * EXPLORER.EXE, and any 16-bit process, because what killing a Win16
     * task holding the Win16Mutex does to the rest of the box is untested.
     *
     * KILL_DOS_SESSION unless `force`: a process with a DOS VM
     * (WINOA386.MOD) below it -- a console program's console, or a DOS
     * session it runs. Ending it leaves that VM half-dead until reboot
     * (measured, docs/BACKLOG.md 17), and a guest carrying such VMs later
     * blue-screened in VMM (docs/measurements/bsod-vm-20260927.txt).
     * Decided 2026-09-27: no such kill without force.
     *
     * WITH `force`, since 2026-10-13: the Win32 descendants are terminated
     * first, and a WINOA386.MOD that was below the target is reclaimed
     * afterwards IF nothing is above it any more. `force` used to be a way
     * to opt into the leak this refusal describes; now it is a way to take
     * the whole thing down. BACKLOG 17. */
    KillStatus kill(DWORD pid, bool force, DWORD waitMs);

private:
    /* Terminate a DOS VM that was below a process now confirmed dead and
     * that nothing else owns. Best effort: never turns a kill into a
     * failure. */
    void reclaimOrphanVms(const std::vector<DWORD> &wasBelow);

public:
    /* ExecJob's stop, and nothing else's: end `root` (whose handle is
     * `rootProcess`) and everything below it, WITHOUT the deny list -- the
     * tree is ours. True if `root` is gone in the end.
     *
     * Measured on the VM (execprobe --stops, --redir, --gentle): on 98 a
     * TerminateProcess on the DOS VM leaves it half-dead, and REDIR32 (or
     * any process waiting on it) can then never exit -- ~150KB until
     * reboot. But killing only the Win32 programs in the tree lets the job
     * END ITSELF within seconds, leaving nothing. So: the Win32 side first
     * and a wait of `graceMs`; only a job still running after that (a DOS
     * program in the VM) gets the VM killed, then `finalMs`. */
    bool stopJobTree(DWORD root, HANDLE rootProcess, DWORD graceMs,
                     DWORD finalMs);

    /* Start `command` detached from us: no handles inherited, both of its
     * handles closed, `show` as ShowWindow takes it (measured honoured for
     * DOS and Win32 console programs alike). False if it did not start or
     * the command line would not fit; *error says why. */
    static bool launch(const char *command, const char *cwd, int show,
                       DWORD &pid, DWORD &error);

    /* True for a 16-bit NE image. Reads the header; false when unreadable. */
    static bool isWin16Exe(const char *path);

private:
    Processes(const Processes &);
    Processes &operator=(const Processes &);

    bool snapshot(std::vector<ProcessInfo> &out, bool withKind);

    typedef HANDLE (WINAPI *SnapFn)(DWORD, DWORD);
    typedef BOOL (WINAPI *WalkFn)(HANDLE, PROCESSENTRY32 *);
    SnapFn m_snap;
    WalkFn m_first;
    WalkFn m_next;
};

#endif
