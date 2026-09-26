# libexec plan (REMOTE.md step 3)

*Approved 2026-09-26. Progress is at the end; each step is a commit.*

## Context

Step 2 (libwire) is done and pushed (903fc6f). Step 3 extracts `win/rpc/exec.cpp` into
`libexec` (`win/exec/`): a plain library that depends only on libcore and knows nothing of
frames, ids or lanes. It covers process execution, process management and the other
non-UI system calls.

Decided with the user:
- File management stays in **libfs** (step 5).
- Calls are **synchronous on the caller's thread**. The exe provides the thread: the
  client's rpc worker now, gremote's lanes later.
- The client is the first consumer.
- **Probe for a clean stop before the stop path is written.**
- Exit codes: the lib flags whether a code is meaningful, and the wire stays unchanged.

A probe on the VM (2026-09-26) changed the design. Its raw results are in
`docs/measurements/execprobe-vm-*-20260926.txt`, from `win/checks/execprobe.cpp`. Only the detached runs count; they behave like a GUI parent. The run
from the DOS prompt shared its console with the children and misled.

| Finding (Win98, VM) | Design consequence |
|---|---|
| A piped `COMMAND.COM` spawn is `REDIR32.EXE` (the pid we get) → `WINOA386.MOD` (the DOS VM) → any Win32 program it runs | A job is a tree, and Toolhelp's `ppid` chain finds all of it |
| `TerminateProcess(REDIR32)` returns TRUE, but the process never exits; the job keeps running, and a one-thread zombie is left for good | **The client leaks one on every exec timeout and EXECKILL today.** The stop path must never rely on it |
| `TerminateProcess(WINOA386)` kills a Win32 grandchild at once. A DOS program runs on ~30 s. REDIR32 always stays as a zombie. The VM stayed in the `--vmfirst` run but was gone after a second kill in the `--kills` run, so the VM result is **inconsistent** (one sample each) | Better than today, but still leaky. Step 0b looks for a clean stop and repeats each case |
| A job that ends by itself leaks nothing | Only the stop path is broken |
| `COMMAND.COM /c` always returns 0, even for a missing program or a child that exits 7; a direct spawn returns the real code | `ExecResult::viaShell`: a fact, not a verdict (cmd.exe on NT does pass codes) |
| Sockets are **not** inherited on 98 (the listener dies on the parent's `closesocket`); they are on Wine/NT; `DuplicateHandle` works on a SOCKET on 98 | Not a libexec problem. gremote (step 4) makes its listener non-inheritable by DuplicateHandle, for NT |
| The show value is honoured for DOS and Win32 console children; a `/c` DOS box closes its window on exit | `launch` needs no workaround |
| Toolhelp: 20 snapshots take 5–10 ms, 9–12 processes on a stock box; pids have the high bit set | Snapshot per call. No cache |
| 16-bit processes running: `MSGSRV32.EXE` and each `WINOA386.MOD` | Deny list, below |

## API

### `exec/execjob.h`: one command, single-use (like `Connection`)

```cpp
class OutputSink { public: virtual bool onOutput(const char *data, unsigned len) = 0; };
struct ExecOptions { const char *shell; const char *cwd; DWORD timeoutMs; DWORD maxOutput; };
enum ExecStatus { EXEC_OK, EXEC_TIMEOUT, EXEC_SPAWN_FAILED, EXEC_CANCELLED,
                  EXEC_TOO_LONG, EXEC_SINK_GONE };
struct ExecResult { ExecStatus status; DWORD exitCode, bytes, ms, error;
                    bool truncated; bool viaShell; bool stopLeaked; };
class ExecJob {
public:
    ExecJob();                                   // trivial
    bool run(const char *command, const ExecOptions &, OutputSink &, ExecResult &);
    void cancel();                               // any thread, any time, sticky
};
```

- **The spawn moves over unchanged** from `exec.cpp`, since it is proven on the guest:
  - stdin gets EOF;
  - `DuplicateHandle` makes the read end non-inheritable;
  - `SW_HIDE`, not `CREATE_NO_WINDOW`;
  - a bounded drain after exit;
  - a command that is too long is refused rather than truncated.
- **Cancel is sticky and valid before `run`, and it only signals.** Today
  `rc_exec_cancel` calls TerminateProcess from the **reader thread**. Once stopping means a
  Toolhelp walk and bounded waits, that can't stay on the reader. `cancel()` sets the flag
  and an event. The `run` loop, which already polls every 100 ms on the worker, does the
  stop. It replaces `rc_exec_arm` and its race.
- **The sink can end the job.** `onOutput` returning false stops the job and gives
  `EXEC_SINK_GONE`. That fixes the run loop that stalls behind a blocked sink.
- **`viaShell` records that `shell` was set.** The lib can't judge the code, because it
  never knows which shell it ran (the OS-neutral rule), and cmd.exe on NT passes codes
  through. The client still sends `code=` as today; the bridge, which picked the shell,
  documents that it means nothing through command.com.
- **The stop path is private (`stopTree`), so the API does not depend on step 0b.** It
  snapshots the tree under our pid and stops it in the order step 0b finds. It **never**
  calls TerminateProcess on `REDIR32`. It sets `stopLeaked` when the spawned pid has not
  exited within a bounded wait, so the adapter can log it.
  - `stopTree` calls a private tree-stop in `Processes`, **not** `Processes::kill`: the
    deny list would refuse our own job's `WINOA386.MOD` (16-bit).
  - Without Toolhelp (NT4), it falls back to TerminateProcess on the spawned pid, which
    works on NT.
- **The 8KB chunk is a per-job `HeapPtr`.** Ids stay in the adapter, which fixes BACKLOG §9.

### `exec/process.h`: process management (Toolhelp32, resolved at runtime)

```cpp
struct ProcessInfo { DWORD pid, ppid, threads; char exe[MAX_PATH]; bool win16; };
class ProcessSink { public: virtual bool onProcess(const ProcessInfo &) = 0; };
enum KillStatus { KILL_OK, KILL_PENDING, KILL_DENIED, KILL_NO_SUCH_PID,
                  KILL_FAILED, KILL_NO_TOOLHELP };
class Processes {
public:
    bool init();                                  // false = no toolhelp (NT4)
    bool list(ProcessSink &);                     // streams, no table held
    KillStatus kill(DWORD pid, bool force, DWORD waitMs);
    bool launch(const char *cmd, const char *cwd, int show, DWORD &pid);  // bInherit=FALSE
    bool descendants(DWORD root, std::vector<DWORD> &out);  // for ExecJob::stopTree
};
```

- **`kill` waits up to `waitMs` and reports `KILL_PENDING`** when TerminateProcess
  returned TRUE but the process is still there. The probe showed that "TRUE" is not
  "gone" on 98.
  - The caller picks the wait because REMOTE.md puts `KILL` on the **control lane,
    inline on the reader**, which must stay responsive for the heartbeat. gremote passes
    a short one (≤250 ms).
- **`win16` comes from the exe's NE header**, using the probe's `exeKind`.
- **Deny list:**
  - always denied: `KERNEL32.DLL`, `MSGSRV32.EXE`, `MPREXE.EXE`, `MMTASK.TSK`, our own pid,
    and `REDIR32.EXE` (killing it does nothing and leaves a zombie);
  - denied without `force`: `EXPLORER.EXE`, and any 16-bit (NE) process, because Win16
    kill safety is untested.

### `exec/system.h`

- `SystemInfo`: `mem_kb`, `mem_free_kb`, `uptime_ms`, `ver`, `acp`, `oemcp`. HELLO in
  `net.cpp` reads these from here.
- `reboot(mode)`: logoff, reboot or poweroff, gated by the caller.

## Order of work (small commits, each verified)

0. **Commit the probe** (`execprobe.cpp`, the Makefile and ISO entries, the measurements),
   and add a **BACKLOG** entry: the client leaks a REDIR32 on every exec timeout and
   EXECKILL.
0b. **Probe for a clean stop**, as new `execprobe --stops` cases on a job that is
   ticking, for both the DOS loop and the Win32 child. Each case records whether the
   ticking stops and whether REDIR32 and the VM exit.
   - Close our pipe ends first (stdin write, stdout read), then wait.
   - Send Ctrl+Break or Ctrl+C: `GenerateConsoleCtrlEvent` if it resolves on 98, or
     keystrokes posted to the VM's hidden `tty` window.
   - Send `WM_CLOSE` to the hidden `tty` window, with and without a pending
     confirmation.
   - Kill the VM, then wait for REDIR32 with the pipes closed.
   - A **direct** spawn (`shell` NULL) of a Win32 console program and of a DOS program.
     This path is untested on 98 so far.
   - Run every case **at least twice**, since the VM result above was inconsistent.
   - Record `mem_free` before and after (GlobalMemoryStatus). It tells us what a zombie
     costs on a 32MB box, and so how urgent the client's leak is.
   - Before this runs, a guest reboot clears the zombies from 2026-09-26 (5 REDIR32 and
     2 WINOA386). The VM is shared, so ask first.
1. **Move** `rpc/exec.cpp` into `exec/execjob.{h,cpp}`, with `EXEC_SRCS` and `LIBEXEC`
   in the Makefile.
   - `rpc.cpp` creates one job per EXEC. Behaviour is identical, and the stop path is
     still today's.
   - A new `checks/execcheck.cpp` takes over the overlong-command case from `rpccheck.c`.
   - This can land in parallel with 0b.
2. **Id-checked cancel.** An EXECKILL whose id is stale or missing is logged and ignored.
   BACKLOG §9.
3. **The sink can end the job**, the output cap becomes an option, and `viaShell` is
   added.
4. **`Processes`:** `list`, `descendants`, `kill` (with `KILL_PENDING` and the deny
   list), `launch`.
5. **`stopTree`**, implementing the winner from 0b, on timeout, cancel and sink-gone.
   This closes the new BACKLOG leak entry, or narrows it if 0b finds no clean way.
6. **`SystemInfo` and `reboot`.** HELLO reads from `SystemInfo`.
7. **Docs.**
   - REMOTE.md: step 3 marked done; the op table's `exit` note gets the answer; the
     socket-inheritance note is corrected (98 doesn't inherit, NT does; gremote
     DuplicateHandles its listener); `KILL` gets `pending`.
   - BACKLOG §9 closed. Stale `exec.cpp:NNN` references become symbols.

The timeout clamp stays in `rpc.cpp`, and `meta/test_exec_timeout_contract.py` is
unchanged.

## Verification

- **Every commit:** `make check` (no static Toolhelp import; committed-at-load under the
  cap), `make lint`, `make test-fast`, `make wine-smoke`, and the new
  `tests/python/guest/test_guest_exec.py` over `execcheck.exe` under Wine.
- **On the VM, after the owner OKs it:**
  - Ship with a probe-only ISO (eject, insert), then **put `tools.iso` back**.
  - Run the exe detached and read its results with `dos.fetch`.
  - For step 4: `execcheck` on the guest (list; kill with deny and pending; launch).
  - For step 5: the client runs an exec turn, a timeout on a DOS loop and an EXECKILL
    mid-run. `execprobe --list` afterwards shows **no new REDIR32 or WINOA386**. A late
    EXECKILL with a stale id kills nothing.

## Progress

All in code and tested locally (Wine) on 2026-09-26. **Nothing here has run
on the guest yet**: the user asked for local testing only.

- 0 `78bf95c`: probe, measurements, BACKLOG 14, this file.
- 0b `a146cf1`: `execprobe --stops` written and smoke-run under Wine. **Not
  run on the VM.** Reboot the guest before (5 REDIR32 and 2 WINOA386 zombies
  from the first probe are still there) and after.
- 1 `c176f44`: `ExecJob`; one job per EXEC. execcheck found a cancel
  reported as `ok` (the killed child's exit beat the flag); fixed.
- 2 `65261a8`: EXECKILL id-checked under `g_lock`; a kill for a queued EXEC
  takes it out of the slot. BACKLOG 9's exec part closed.
- 3 `07f62e4`: `OutputSink` returning false ends the job (`EXEC_SINK_GONE`),
  only for a dead link; `viaShell`.
- 4 `f6273e6`: `Processes`: list, descendants, kill (`KILL_PENDING`, deny
  list), launch, `isWin16Exe`.
- 5 `24f6493`: `cancel()` only signals; `stopJobTree` on the job's thread,
  never TerminateProcess on REDIR32, `stopLeaked` logged. Provisional until
  0b's results. BACKLOG 14 narrowed, not closed.
- 6 `5288800`: `readSystemInfo`, `requestReboot`; HELLO reads the numbers.
- 7: docs (this entry).

Committed at load: 387,417 before, 394,601 after (the static 8KB chunk
went; `Processes` and its vector code came in).

Still to do on the guest, in order: reboot; `execprobe --detach --stops`,
and pick the stop from its results; `execcheck`; the client's exec turn,
timeout on a DOS loop, EXECKILL mid-run and a stale EXECKILL, then
`execprobe --list` shows no new REDIR32 or WINOA386 (or, if 0b finds no
clean stop, exactly one REDIR32 per stop, as logged).
