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

    /* Streams one ProcessInfo per process. False if no snapshot. */
    bool list(ProcessSink &sink);

    /* Every process below `root`, by parent pid, root excluded. The order is
     * breadth-first: children before grandchildren. */
    bool descendants(DWORD root, std::vector<DWORD> &out);

    /* Terminate `pid` and wait up to `waitMs` for it to go.
     *
     * Always denied: this process, KERNEL32.DLL, MSGSRV32.EXE, MPREXE.EXE and
     * MMTASK.TSK (killing them can hang 9x), and REDIR32.EXE (it cannot be
     * killed: the call succeeds and leaves a zombie). Denied unless `force`:
     * EXPLORER.EXE, and any 16-bit process, because what killing a Win16
     * task holding the Win16Mutex does to the rest of the box is untested. */
    KillStatus kill(DWORD pid, bool force, DWORD waitMs);

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
