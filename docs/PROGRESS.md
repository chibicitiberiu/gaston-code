# Progress

Rolling log. Plan is `PLAN.md`; phases 0–2 are executable, 3–6 are a sketch
that gets re-planned once Phase 0 answers land.

**Current goal: Phase 5 — Attach.**

Phases 0–4 are done and demoed on the real guest. Phase 4's own list is closed
except for R7 (multiple clients), which `BACKLOG.md` §7 carries with its
implementation blockers intact.

---

## VM checkpoints

Snapshot after each milestone, before anything else touches the disk. A ruined
Win98 install is an hour to redo; a snapshot is seconds.

    export LIBVIRT_DEFAULT_URI=qemu:///session
    virsh snapshot-create-as retroclaude-win98 <name> "<description>"
    virsh snapshot-list       retroclaude-win98
    virsh snapshot-revert     retroclaude-win98 <name>

| Snapshot | When | State |
|---|---|---|
| `clean-install` | Win98 SE booted to desktop, drivers settled, before anything is installed | **done 2026-09-16 20:53** |
| `networked` | TCP/IP reaching the bridge | **done 2026-09-16 22:04** |
| `tools` | RetroClaude binaries copied to C:\RETRO | **done 2026-09-16 22:57** |

## Blockers

| Blocker | Blocks | Needs |
|---|---|---|
| ~~Win98 SE install media~~ | — | **RESOLVED 2026-09-16.** User supplied `Win98SE_bootable.iso` + product key. Unattended install running under libvirt. |
| Real hardware access | end-of-Phase-1 gate | The physical P200MMX on the LAN |
| Samba server not installed | 0.8 (LFN over `vers=1.0`) | `sudo dnf install samba` on the bridge host. `samba-common` is present but there is no `smbd`, and this session has no passwordless sudo. Everything else in Phase 3 is unaffected until then. |

Everything Linux-side proceeds without these.

---

## Phase 0 — foundations + experiments

| # | Item | State | Notes |
|---|---|---|---|
| 0.1 | Repo, licence, attribution, layout | **done** | |
| 0.2 | `make/ow.mk` with `-bm`, `WLINK_LNK`, pinned `WINVER` | **done** | Uses `WLINK_LNK` rather than mutating PATH |
| 0.3 | Re-run `make check` after `-bm` | **done** | **Unchanged**: same 45 imports, 3 DLLs, 0 CMOV, 22016 bytes. `-bm` pulls nothing extra until thread functions are actually called — re-check once `_beginthreadex` is used |
| 0.4 | **Experiment C** — compile retro-agent sources under `wcc386` | **done** | **All five clean, zero errors, zero warnings, unchanged** |
| 0.5a | miniz builds + runs under OW | **done** | Vendored at `win/third_party/miniz/`; round-trip verified as a Win32 PE |
| 0.5b | Compression ratios measured | **done** | **~2.16:1**, not the ~3:1 assumed. Plan corrected |
| 0.5c | Deflate throughput on the real P200MMX | **provisional** | Ran on the guest: L1 7707 KB/s compress, 16379 KB/s inflate, round trip PASS. **Not usable as a threshold**: TCG on a modern host is nothing like a P200, and `probe.c`'s sample is a two-line block repeated (164:1), which flatters deflate badly. Needs a real corpus and real hardware |
| 0.6 | Win98 SE guest under QEMU | **done** | Unattended via MSBATCH.INF. Required config: TCG (not KVM), no APIC, no ACPI |
| 0.7 | W-suffix import load test (`GetCommandLineW`/`GetModuleFileNameW`) | **done — PASS** | The image loads on Win98 SE 4.10.2222 A. Both symbols resolve against Win98's kernel32. The last unverified toolchain assumption is settled; Open Watcom stands |
| 0.8 | Samba LFN over `vers=1.0` | **blocked** | No `smbd` on the host (see blockers). Guest-side LFN itself does work: `copy retrocl.ini "retroclaude.ini"` created and read a long name from COMMAND.COM |
| 0.9 | 16-bit `EXEC` capture — **correct non-empty output**, not "no hang" | **done — PASS** | `command.com /c ver` 36B, `dir c:\ /w` 352B, and `echo hello \| find "hello"` 8B first line `hello`. Non-empty **and correct**, including the known-bad 16-bit FIND. **Phase 3's EXEC design survives**; the claudewin9x temp-file fallback is not needed |
| 0.10 | `GetDIBits` → `BI_RLE8`? | **done — PASS** | 640x480 8bpp: 307,200B raw -> **27,088B in 14ms, 11.3:1**, done by GDI. Beats the plan's 3-8:1 estimate for hand-rolled RLE, and the ~40 lines of RLE8 are not needed |
| 0.14 | **Does Win98 have a directory watcher?** (`FindFirstChangeNotification`) | **done — PASS** | All three symbols present in Win98 SE's KERNEL32. Watching `C:\WATCHTST` with `bWatchSubtree=TRUE`: no spurious signal before any change (702ms quiet), then **create / modify / create-in-SUBDIRECTORY / delete all signalled**. 5 of 5. Subtree watching works on FAT, which was the doubt. Resolved via `GetProcAddress`, and `make check` confirms it is **not** in the import table |
| 0.11 | `_beginthreadex` vs `CreateThread` under `-bm`, with a message loop | **done — PASS** | 3/3 `_beginthreadex` threads plus a message sink window, 275 `PostMessage` round trips. **Correction to the plan**: `CreateThread(lpThreadId=NULL)` was *accepted* on this Win98 SE, so retro-agent's hardware note does not reproduce. `_beginthreadex` stays, on the OW per-thread-data argument, which is independent |

## Phase 1 — "I typed on Win98 and Claude answered"

Linux side first, so the guest develops against something real.

| # | Item | State |
|---|---|---|
| 1.0 | venv + `claude-agent-sdk` 0.2.153 installed | **done** |
| 1.1 | `bridge/protocol.py` — frame codec (new, not a lift) | **done** — 29 tests |
| 1.2 | `bridge/server.py` — listener, HELLO, dup-client rejection | **done** |
| 1.3 | `bridge/session.py` — one `ClaudeSDKClient`, `allowed_tools=[]` | **done** |
| 1.4 | `bridge/fold.py` — UTF-8 → CP1252 | **done** — 31 tests |
| 1.5 | `bridge/replay.py` — seq numbering + resume | **done** — 10 tests |
| 1.6 | `bridge/fakegui.py` — Linux CLI speaking the wire protocol | **done** |
| 1.7 | `win/` — window, RichEdit, edit box, reader thread, log, status bar | **done** — 42KB, builds + runs |
| 1.8 | Stop button + `interrupt()` | **done** |
| 1.9 | Real hardware gate (period PC) | blocked |
| 1.10 | **Guest gate: `netcheck.exe` on real Win98 -> bridge -> Claude** | **done — PASS** |
| 1.11 | **Guest gate: `retroclaude.exe` GUI on real Win98** | **done — PASS** |

## Phase 2 — four demos

| # | Item | State |
|---|---|---|
| 2a | Streaming raw text + `enc=deflate` bridge→guest | **done** — verified on the guest |
| 2b | Styled runs (markdown → `CHARFORMAT2`) | **done** — verified on the guest |
| 2c | Pagination | **done** — verified on the guest, sealed at a message boundary |
| 2d | Session list | **done** — verified on the guest, switch replays history |

## Phase 3 — the guest becomes a workspace

| # | Item | State |
|---|---|---|
| 3.0 | **Stage A — permission prompts** | **done — verified on real Win98 SE** |
| 3.0a | `can_use_tool` wired into every session, `bridge/permissions.py` | done |
| 3.0b | PERMREQ / PERMEND / PERMSTATE / PERMWAIT / PERMREPLY | done |
| 3.0c | Modeless prompt bar on the guest, Send gated, Esc denies | done |
| 3.0d | P1 — PERMSTATE, because the guest never sends `from=` | done |
| 3.0e | P2 — idle reaper, so a crashed guest stops locking the bridge out | done |
| 3.0f | P4 — `_send` refuses unencodable frames before stamping replay | done |
| 3.0g | Cancellation: PERMEND scheduled, never awaited in a cancelled scope | done |
| 3.0h | Client machine is the subject of every request; off-limits guard | done |
| 3.1 | **Stage B — `mcp__machine__exec`, `bridge/tools.py`** | **done — verified on real Win98 SE** |
| 3.1a | `bridge/paths.py` | **dropped** — `exec` takes a command line written for a DOS shell; there is nothing to translate. Belongs with 3.3, where paths actually cross |
| 3.2 | **Stage B — `win/exec.c`, `win/rpc.c`, sender thread** | **done — verified on real Win98 SE** |
| 3.2a | `EXECKILL`, so Stop reaches the command | done |
| 3.2b | Restart path: reader clears `running`, `try_connect` tears down first | done — reproduced the KERNEL32 fault, then fixed |
| 3.3 | File sync layer | **done — verified on real Win98 SE** |
| 3.4 | Hardening: the backlog worked down | **done -- verified on real Win98 SE** | `BACKLOG.md` §5 is down to what was deliberately not built, each with its reason. `gen=` and the netem gate were dropped by decision, not omission. |

### Stage B verification (2026-09-17)

On real Windows 98 SE, byte counts matching experiment 0.9 exactly:

| command | bytes | note |
|---|---|---|
| `ver` | 36 | |
| `vol C:` | 73 | |
| `dir c:\ /w` | 352 | |
| `echo hello \| find "hello"` | 8 | through the known-bad 16-bit FIND |
| `dir c:\ /s` | 32768 | hit the cap; `truncated=1` reached the model, which then worked out *which* trees it never saw |
| `del c:\stdintst\*.*` | 60 | `status=timeout` -- interactive prompts cannot be answered, see the decision note |
| `pause` + Stop | — | killed within seconds via `EXECKILL` |

Same numbers from the **windowed** binary also retire the gate that 0.9's
evidence came from console-subsystem `probe.exe`.

Three bridge restarts with the app connected: three clean attaches, no
protocol errors, and `exec` still worked afterwards. Before the restart-path
fix this crashed the guest with an invalid page fault in KERNEL32.

### Phase 3 gates

- **Denied permission refuses rather than hangs** — done.
- **16-bit `EXEC` through the GUI binary on the RPC thread** — done, above.
- **Memory re-measured against the 28KB baseline** — done, and no
  megabyte-scale regression. Window created 2420K -> **2312K**, chat list
  1060K -> **960K**. About 100KB lower at comparable points, which the binary
  growing ~6KB, BSS growing 7.8KB and a longer chat list account for. Not a
  like-for-like comparison (the list holds more conversations now), so read it
  as "nothing ran away", not as a precise delta.
- **Every demo over `tc netem rate 40kbit`** — **deferred to the backlog**
  (`BACKLOG.md` §1). `tc` needs root and this environment has no passwordless
  sudo. `--link-bps` throttles the bridge's own sending logic rather than the
  wire, so it does not discharge the gate. Deferred by decision, with the three
  weaknesses it would catch written down.
- **Memory optimisation** — also deferred (`BACKLOG.md` §2). The *gate* (no
  megabyte-scale regression) is closed; tuning is not, and is late-phase work.

### Stage A verification (2026-09-17)

Run on real Windows 98 SE, not Wine:

- permission bar appears with the CLI's own description, transcript keeps
  streaming behind it, Send greyed, Stop live
- **Allow** -> tool runs -> answer returns (`$0.1974`), and the model
  volunteered *"That's the proxy box, not the Windows 98 machine you're
  sitting at"*
- **Deny** -> bar clears, Send returns (but see the decision note: the turn
  ends silently, which the plan did not predict)
- **Stop while the bar is up** -> `PERMEND reason=cancelled`, bar clears, app
  still usable. This is the regression test for the worst bug review found
- cable-pull then reconnect -> `PERMSTATE` restores the bar, answered on the
  new connection (verified on Linux; the grace period is 60s)
- 212 bridge tests

All four demoed on real Windows 98 SE on 2026-09-16, not just under Wine.

---

### Phase 2 verification status

- 2a streaming + adaptive deflate: verified both link regimes over Wine
- 2b markdown -> styled runs: verified over the wire; no markdown syntax
  reaches the guest
- 2c pagination: 32 tests including wire-level integration; history pages never
  receive live text
- 2d session list: `list_sessions()` against real on-disk sessions, chat list
  renders in the client under Wine

### Phase 1 verification status

- `fakegui.py` -> bridge -> real Claude: **passing**, monotonic seqs
- `netcheck.exe` (real guest C: net.c + frameproto.c) under Wine -> bridge ->
  real Claude: **passing**, `inflight=0` so no frame leaked
- `retroclaude.exe` under Wine: connects, handshakes, receives READY, renders.
  Picked up **Tahoma** via `SPI_GETNONCLIENTMETRICS` -- the font question the
  plan said not to bother verifying.
- Still unverified on real Win98: everything. Wine proves the code, not the
  platform.

### Phase 2a measured (2026-09-16)

Same prompt, same code, two link regimes, via `--link-bps`:

| link | frames | deflate | bytes on wire | ratio |
|---|---|---|---|---|
| fast (default) | 33 | 1 | 2437 | — |
| 5 KB/s (dialup) | **6** | **6** | **594** | **6.27:1** |

All output intact both times, `inflight=0` both times. (6.27:1 is flattered by
deliberately repetitive test text; real prose measured 2.16:1.)

## Phase 4 — settings and the rest of the surface

| # | Item | State |
|---|---|---|
| 4.0 | Resource compilation: `wrc` wired in, `gaston.rc`, the app icon | **done -- verified on real Win98 SE** |

### 4.0 — the resource pipeline (2026-09-18)

`WRC` had been defined in `make/ow.mk:13` and `win/Makefile:20` since Phase 0
and invoked by neither; there was no `.rc` anywhere in the tree. Both R5's
icon and any dialog template needed it, so it goes first.

What landed:

- `win/app/gaston.rc`, compiled with `wrc -q -r -bt=nt` into
  `build/win/gaston.res` and bound by the linker's `option resource=`.
  Bound at link rather than injected into a finished EXE afterwards -- the
  two-step form leaves, if it is interrupted between the steps, a
  `retroclaude.exe` newer than every prerequisite and with no icon in it.
- `win/app/resource.h`, included by **both** wrc and wpp386, so `IDI_GASTON`
  is defined once. wrc runs a full C preprocessor over a `.rc`, which is what
  makes that possible; the header stays macro-only because wrc knows no C++.
- `tools/mkicon.py` and `win/app/gaston.ico` -- 16x16 and 32x32, 4bpp,
  standard 16-colour VGA palette.
- A VERSIONINFO block, so the file has a Version tab in its properties.

**The icon format is the platform fact worth recording.** Any modern tool
that writes a `.ico` writes 32bpp BGRA with an alpha channel, which is an
XP-era format. Win98's shell does not composite alpha: the transparent margin
comes out black, and on the taskbar that is a black square. Transparency on
this target is the 1bpp AND mask, and the XOR pixel under a set mask bit must
be 0 or the two XOR together and the pixel inverts whatever is behind it. The
palette is pinned to the sixteen VGA colours for the second reason -- on an
8bpp display a 4bpp icon carrying an arbitrary palette is colour-matched to
the system halftone palette and shifts.

Both sizes are drawn, not scaled. `WNDCLASSA` has no `hIconSm` field (that is
`WNDCLASSEX`) so Windows derives the title-bar icon by shrinking `hIcon`, and
a 32x32 4bpp image StretchBlt-ed to 16x16 turns the three dots into grey mush.
`LoadImageA` with an explicit `SM_CXSMICON` size picks the real 16x16 entry
out of the `.ico`, and `WM_SETICON` installs it after the window exists.

**BACKLOG.md 10 predicted this would move `make check`'s CMOV baseline**, on
the reasoning that objdump decodes binary data as instructions and a resource
is binary data. Measured: it does not. `objdump -d` disassembles only sections
flagged as code and prints exactly one `Disassembly of section AUTO:` for this
image; `.rsrc` is READONLY DATA and is never walked. The existing false
positive is inside AUTO, which is where Watcom puts read-only data as well as
code. Baseline stays at 1, and the prediction is recorded as wrong in the
Makefile rather than quietly dropped.

**Found while building it:** `make -C win` had no default goal, so make used
the first target in the file -- `htmlspike`, a Phase 0 spike. The command
printed "Nothing to be done for 'htmlspike'" and exited 0 while building
nothing, which reads exactly like a successful no-op build of the real thing.
The top-level Makefile had already hit this and fixed it with
`.DEFAULT_GOAL := all`; `win/Makefile` never did.

Committed at load moved 303,044 -> 305,604 bytes: 2,048 of `.rsrc` plus
section padding. Verified on the guest, not under Wine, because every claim
above is about how the Win98 shell renders a 4bpp image: title bar, taskbar
button, Explorer's 16x16 list and 32x32 large-icon views, and the Version tab
reading `0.4.0 / Gaston Code / Copyright (c) 2026 Tiberiu Chibici. MIT.`

One correction made against the guest rather than reasoned about: the first
cut shipped `VALUE "CompanyName", ""`. An empty key is worse than a missing
one -- Win98's Version tab lists every key it finds, so it appeared in "Other
version information" as a row that selects to a blank value. Removed.

## Decisions made while building

### 2026-09-17 — 0.14: Windows 98 does have a directory watcher

Asked before designing the sync layer's trigger, because the headers declaring
`FindFirstChangeNotificationA` prove it is part of Win32, not that this guest's
KERNEL32 exports it -- the distinction 0.7 existed for, and a static import the
guest lacks kills the image before `WinMain` with no log.

Measured on Win98 SE:

    FindFirstChangeNotificationA : present
    FindNextChangeNotification   : present
    FindCloseChangeNotification  : present

    watching C:\WATCHTST with bWatchSubtree=TRUE
      before any change (want: none)   no signal (waited 702ms)
      create in watched root           SIGNALLED
      modify existing file             SIGNALLED
      create in SUBDIRECTORY           SIGNALLED
      delete in watched root           SIGNALLED

**5 of 5.** The one most likely to disappoint -- `bWatchSubtree` on FAT -- works.
So does the negative case: it does not fire before anything has happened, which
matters because a watcher that signals spuriously would make every scan trigger
the next one.

**The two design decisions taken *before* the answer both stand, and are now
cheap insurance rather than load-bearing:**

  * **Resolved with `GetProcAddress`, never a static import.** `make check`
    confirms nothing named `*ChangeNotification*` appears in the import table.
    A Win98 variant without it degrades to polling instead of failing to load.
  * **Polling remains the mechanism; the watcher is an accelerator.** What the
    result changes is the *poll interval*, which can now be generous rather than
    brisk -- which is CPU the P200 gets to keep.

What this did NOT measure is latency. Every signal read "after 0ms" because the
file operation completed before `WaitForSingleObject` was called, so the handle
was already signalled. The experiment answers "does it fire reliably", which was
the question; "how fast" would need the wait armed first and is not worth a
second run, since the debounce dominates anyway.



### 2026-09-17 — the two open Stage B items, both settled by measuring

**The stdin change was wrong, and the review called it.** `exec.c` set
`si.hStdInput` to a pipe at EOF instead of probe.c's `NULL`, with a comment
claiming it answers COMMAND.COM's "Are you sure (Y/N)?". Measured on the
guest:

    del c:\stdintst\*.*   ->   status=timeout, killed at the limit

It does **not** answer the prompt. Those confirmations are read from CON via
INT 21h, not from handle 0, so no stdin arrangement reaches them. The comment
was rewritten to say what was measured, and the EOF pipe is kept only for the
reason it actually earns -- a child that genuinely reads handle 0 (`sort`,
`more`, `find` with no pipe) gets a defined EOF rather than an invalid handle.

The real consequence is a limitation, not a bug: an interactive command hangs
until the timeout kills it, which the system handles correctly (partial
output, `status=timeout`). So the fix is guidance. The tool description now
tells the model plainly that prompting commands hang, and to pipe an answer
in (`echo y | del c:\tmp\*.*`) or pick a form that does not ask.

### 2026-09-17 — EXECKILL, and the cancellation path that never fires

Without it, cancelling a turn left the command running on the guest and the
next call was answered `status=busy` -- one interrupted turn poisoning the
next for a reason the model could neither see nor act on.

The first implementation hung it off `run_on_guest`'s `CancelledError`. **That
path is largely unreachable**, and the guest proved it: `pause` started at
17:15:40, Stop was pressed at 17:15:48, and 47 seconds later it was still
running with no `EXECKILL` sent. On mcp 1.x the SDK does not cancel an
in-flight tool call when a turn is interrupted (`types.py:653-662`) -- the
same fact the review cited about abandoned calls, which turns out to cut both
ways. The coroutine simply never wakes up.

**`INTERRUPT` is the trigger that actually fires**, and it is the one the user
meant: they pressed Stop, so stop. The handler now kills every in-flight exec
before interrupting the model. Verified: `pause` with a 90s clamp died within
seconds of Stop, the turn ended, and the app stayed responsive.

`EXECKILL` is handled on the guest's READER thread and never queued -- the
worker is busy running the very command it is meant to stop, so a queued kill
would not be looked at until that command finished. `rc_exec_cancel` was built
to be called from another thread for exactly this.

Both triggers can fire for one Stop, so the kill is deduplicated, and the
guest's `EXECRESULT` arriving for a call nobody is waiting on is now logged at
debug -- it is the expected confirmation of a kill, not a fault.



### 2026-09-17 — Stage B shipped, and the review found it had no restart path

`mcp__machine__exec` works on real Windows 98 SE. Byte counts match experiment
0.9 exactly -- `ver` 36B, `dir c:\ /w` 352B, `echo hello | find "hello"` 8B
through the known-bad 16-bit FIND. Those numbers from the **windowed** binary
also retire the gate the plan set: 0.9's evidence came from console-subsystem
`probe.exe`, and spawning COMMAND.COM from a GUI process is not the same
thing.

**The review's headline finding was that the four-thread design had a start
path and an exit path but no restart path**, and it was right. `reader_thread`
never set `running = 0` when it exited, so `try_connect` called
`rc_net_start` again with the sender and RPC worker still alive: a live
critical section re-initialised, the outbound queue zeroed from under the
sender, two sender threads on one socket. Two concurrent `send()`s interleave,
the framing has no resync, the bridge drops the link -- which reconnects and
adds a third sender.

**Reproduced it, which is the part worth recording.** Bouncing the bridge
three times crashed the guest with an invalid page fault in KERNEL32. The
first fix (reader sets `running=0`, `try_connect` tears down first) was not
enough: `rc_net_start` creates the critical section and both events at the
TOP, and every `return -1` below leaves them behind while `started` was only
set on success. So a bridge that is merely *down* -- the common case -- left
`rc_net_stop` thinking there was nothing to clean up. Marking the connection
as needing teardown before anything can fail is what actually fixed it.
Verified: three more bridge restarts, no crash, then `ver` returned its 36
bytes.

Other findings fixed: the MCP server `timeout` was never set, so the CLI would
have abandoned any exec past 30s while the Python body ran on (the plan wrote
this out verbatim and it still shipped wrong); the idle reaper at 90s was
shorter than the 120s max exec and would have reaped a quiet command mid-run;
`DeleteCriticalSection` ran even when the RPC worker had not exited;
`rc_exec_run` cleared the cancel flag on entry and could swallow a shutdown;
the post-exit drain loop had no cancel check, deadline or cap, so a grandchild
holding the pipe would hang the worker forever; `EXEC` was being stamped into
the replay buffer, which would re-RUN commands the day `from=` is implemented;
and a dropped `EXECOUT` left a hole in the output with nothing marking it, so
the model would get an incomplete `dir` listing and no way to know.

**The ownership question came back clean** -- every `rc_frame*` path traced,
`inflight`/`rpcq` balanced in all six. The documented rule in `net.h` was
wrong though: the worker does not always free the frame, since the busy and
not-running paths free it on the reader. Corrected, because that comment
exists specifically so the next person does not double-free.

Still open, recorded rather than fixed: there is no `EXECKILL`, so cancelling
a turn leaves the guest running the command and the next call gets
`status=busy`; `si.hStdInput` was changed from NULL to an EOF pipe on the
theory it answers "Are you sure (Y/N)?", and the review argues COMMAND.COM
reads those from CON via INT 21h so the justification is probably wrong --
`dir`, `ver` and the FIND pipe all still work, but it deserves its own test.



### 2026-09-17 — the tool surface is declared, not inherited

Reported from the guest as "tool calls are failing". They were not failing;
the session had the wrong tools, and the cause was that it had never been told
which ones to have.

`ClaudeAgentOptions.tools` is the AVAILABILITY list and the bridge had never
set it, so `setting_sources=None` loaded everything on the operator's machine.
Measured on this one:

    Task AskUserQuestion Bash CronCreate CronDelete CronList DesignSync Edit
    EnterPlanMode EnterWorktree ExitPlanMode ExitWorktree ListAgents Monitor
    NotebookEdit PushNotification Read RemoteTrigger ReportFindings
    ScheduleWakeup SendMessage Skill TaskOutput TaskStop ToolSearch WebFetch
    WebSearch Workflow Write
    + live claude.ai connectors: Claude Docs (connected),
      Gmail / Google Drive / Google Calendar (needs-auth)

Two separate problems, and the second is the one that was visible:

  * A Windows 98 client session should not hold tools that reach the
    operator's email, calendar and drive, or that create cron jobs and trigger
    remote sessions. None of it is reproducible between machines either.
  * **`Glob` and `Grep` were absent.** They are deferred tools, reachable only
    through `ToolSearch`, so the model had to go looking for them and often
    gave up instead. On the guest that is indistinguishable from broken.

Fixed by declaring three things on every session:

    tools=TOOLS              Read Write Edit Glob Grep Bash WebFetch WebSearch
    setting_sources=[]       do not inherit ~/.claude
    strict_mcp_config=True   only MCP servers passed programmatically

Measured after: exactly the eight, `mcp_servers: []`, nothing extra.

**`strict_mcp_config` was checked against Stage B before being relied on**, since
dropping our own server would have killed `mcp__machine__exec` on arrival. With
an in-process server passed in, the init message shows
`mcp tools: ['mcp__machine__exec']`, `servers: [machine connected]`, and the
call routes through `can_use_tool` -- so the strict flag keeps ours and drops
the rest, which is exactly the behaviour Stage B needs.

What is deliberately NOT offered, with the cost each would carry: `Task`
(parallel subagents raise parallel prompts, and the guest has one bar on a
640x480 screen), `NotebookEdit` (no Jupyter on Windows 98), `AskUserQuestion`
and the plan-mode tools (need a UI the guest does not have), and the
operator-environment set (`Workflow`, `Cron*`, `Monitor`, `SendMessage`,
`PushNotification`, `RemoteTrigger`, `ScheduleWakeup`, `DesignSync`, `Skill`).

Verified on the guest end to end: Write prompts with its full argument table,
Allow runs it, **Grep then runs** and reports the match. Claude also said
unprompted that the file "exists only in the staging working copy on the
proxy ... has not been copied to your Windows 98 machine" -- but then offered
to transfer it, which nothing can do yet, so the prompt now forbids offering.



### 2026-09-17 — everything the user says refers to THEIR machine

Watching it run on the guest produced the change. Asked to read
`/etc/hostname`, the model read the *proxy's* file and answered with the
proxy's hostname. The file was real and the answer was wrong: the user is
sitting at a Windows 98 box and asked about that.

Two halves, because either alone is insufficient:

  * **The system prompt now leads with the rule** rather than burying it.
    "My machine", "this computer", "the disk" always mean the connected
    machine. The proxy is infrastructure -- not the subject of the
    conversation, and its hostname, OS and packages are nobody's business. A
    request implying a path that cannot exist on the user's machine (a Unix
    path on a Windows box) means *the path is not valid for their machine*,
    not *go read the proxy's copy*.
  * **`permissions.off_limits()` refuses path tools outside the working
    copy** without troubling the user, because they should not have to
    adjudicate a request they never made.

There is a neat symmetry worth recording. The containment check deleted
earlier in this stage -- auto-ALLOW inside cwd -- was dead code, because
Claude Code approves those locally and never asks us. **The same check
inverted is not dead at all**: by the time a path tool reaches the callback,
the path is already somewhere it should not be. `commonpath`, not
`startswith`, so `/tmp/work` does not match `/tmp/workspace`.

Verified on the guest: "What is the hostname of my machine?" now answers *"I
can't read it from here ... the box I'm running on is just a proxy, so its
hostname would be the wrong answer"*, followed by `net config` and `winipcfg`
-- Win98-specific advice, for the right machine.

### 2026-09-17 — a denied tool ends the turn, and the deny message is discarded

Measured on the guest, and it contradicts what this plan has said since the
first draft. `PermissionResultDeny(message=..., interrupt=False)` was supposed
to mean "the model is told no and carries on".

What the stored transcript actually shows after a Deny:

    tool_result: "The user doesn't want to proceed with this tool use. ...
                  STOP what you are doing and wait for the user to tell you
                  how to proceed."
    user text:   "[Request interrupted by user for tool use]"

So **the CLI substitutes its own boilerplate and our `message` never reaches
the model**, and the turn is terminated regardless of `interrupt=False`.
`query.py` does put the message on the wire (`{"behavior": "deny", "message":
...}`), so the CLI receives it and chooses not to inject it.

Consequences: the carefully worded refusal text in `_perm_reply` is currently
decoration, and the plan's demo -- "Deny is pressed, and Claude says it was
refused and carries on" -- does not describe the product. The turn ends
silently. Not worth fighting; worth writing down, because the next person will
otherwise spend an afternoon wondering why their message never appears.



### 2026-09-17 — the auto-allow policy was deleted, because Claude Code already does it

The plan carried an auto-allow policy so that a `Grep` would not cost a
keystroke on a 640x480 screen: `Read`/`Glob`/`Grep` inside `cwd` would be
approved silently by our own `can_use_tool`, everything else would raise the
bar. The second review suspected the branch might be dead. Three spikes against
the real CLI settled it.

**Measured, not reasoned:**

| probe | callback fired? |
|---|---|
| `Read hello.txt` inside cwd | **no** |
| `Grep 'volume'` inside cwd | **no** |
| `Read /etc/hostname` outside cwd | yes |
| `Bash echo SPIKE_BASH_RAN` (no side effect) | **no** -- and it ran |
| `Bash echo written > file` (side effect) | yes |

Claude Code resolves read-tool permissions locally before consulting
`can_use_tool`, and separately auto-approves Bash commands its own sandbox can
contain. So the policy we were about to write was dead code for the in-cwd case
and redundant everywhere else. **Deleted**: no path containment, no `realpath`,
no `..`-escape handling, no `--cwd` validation for that purpose, and four
planned tests removed. `allowed_tools` stays `[]` and everything that reaches
the callback gets a bar.

The feature the user asked for already exists upstream. That is the second time
this stage that checking the real API deleted work instead of adding it.

**Two corrections to the reviews, from the same spikes:**

  * **`blocked_path` was `None`** on the `Read /etc/hostname` call. The second
    review said it is "the field that matters" and that re-deriving from
    `input` is "weaker" -- not for this case. The path arrived in
    `input["file_path"]` and in `description`. Use `blocked_path` when present,
    never rely on it.
  * **`title` was `None`.** The plan said `title`/`display_name`/`description`
    "are the prompt text". In practice `display_name` was `"Read"` and
    `description` was `"/etc/hostname"`, with no title at all. The bar renders
    from `display_name` + `description` and must tolerate a missing title.

Also observed: the CLI's own `suggestions` already carried
`destination="session"` and a rule of `Read` / `//etc/**`, so rewriting the
destination is a no-op in the common case. Keep the rewrite as a guard, since
nothing promises that default.

**One consequence worth stating plainly.** The threat model is no longer "our
bar gates every tool". It is "Claude Code's sandbox gates what it can contain,
and our bar gates the rest". That is defensible -- a sandboxed `echo` is not a
risk worth a round trip to Windows 98 -- but it is not what the plan said, and
it means the guest user does not see everything the model does.



### 2026-09-17 — the UI tier is one stage, and the control tree beats vision

Screen capture had been sitting at 3.2 while everything that uses it sat three
phases later. OCR was not in the plan at all. Both now live in **Phase 6, the UI
tier**, so "look at the machine and drive it" is one coherent stage; Phase 3's
file sync renumbered 3.4 -> 3.3.

The design point that moved is which sense comes first. Modern computer-use
agents lean on screenshots because modern UIs are custom-rendered. **Windows 98
is the opposite**: almost every pixel is a standard USER32 control whose text is
exactly queryable. A dialog's control tree is ~200 tokens and exact; a
screenshot of the same dialog is ~850 tokens and inferred. So the ladder is
control tree -> MSAA -> OCR -> vision, and vision is the fallback rather than
the primary.

Consequences worth keeping:

  * `SendMessage(WM_GETTEXT)`, not `GetWindowText` -- cross-process the latter
    returns only a caption, while `WM_GETTEXT` is marshalled by USER32 and
    returns a control's contents.
  * `LB_GETTEXT` / `LVM_GETITEMTEXT` / `TVM_GETITEM` are **not** marshalled, and
    `VirtualAllocEx` does not exist on 9x. List *contents* are an MSAA-or-OCR
    problem, not a message problem.
  * `click(target)` resolves a control to its rect centre and then does the same
    real `mouse_event` click as raw XY -- one execution path, two resolvers.
    `WindowFromPoint` afterwards catches occlusion for free.
  * `send_keys` hazards are not Ctrl+Alt+Del (the VMM owns that at ring 0). They
    are cross-thread `SetFocus` silently failing without `AttachThreadInput`,
    and the agent typing into Gaston's own input box in a self-feeding loop.
  * `send_action_sequence` is worth it **only closed-loop**: per-step
    preconditions, fail-fast with the state at the abort point, and conditional
    waits (`WaitForInputIdle`) rather than fixed sleeps. It also means one
    permission prompt instead of five, which matters because prompt fatigue is a
    real security failure mode.

New gate **0.13**: is `oleacc.dll` present on Win98 SE and does MSAA return a
usable tree? Dynamically loaded, never a static import. ~40 lines in `probe.c`,
which is already built and shipped to the guest.

Three tools were added that are **not** reachable through `exec`, which is the
test applied to every candidate: `launch` (because `exec` waits for exit, and you
cannot drive Notepad's UI with a tool that blocks until Notepad closes),
`get_clipboard`/`set_clipboard` (the escape hatch for text no message will give
up), and `list_processes`/`kill_process` -- Win98 has no `tasklist` and no
`taskkill` at all, so `CreateToolhelp32Snapshot` is the only route.
`registry_read`, by contrast, stays out: `regedit /e` is ugly but works, and
every tool costs context on every request.

### 2026-09-17 — tool names are OS-neutral

Nothing is called `win98_*`. The MCP server is `machine` and its tools are bare
verbs, because only the guest binary is OS-specific and the codebase may be
pointed at another machine -- MS-DOS was raised explicitly. Where the OS matters
it is a runtime branch, not a name: `probe_exec` already picks `command.com` over
`cmd.exe` from `GetVersion()`. `FEASIBILITY.md`'s `win98_*` sketch is superseded.



### 2026-09-17 — 3.0 planned against the real API, not the sketch

Checking `can_use_tool`'s actual signature against the installed SDK before
designing around it turned out to be worth doing three times over.

**`ToolPermissionContext` already carries everything the prompt needs.**
`tool_use_id` is a correlation id, so there is no counter to invent.
`title` / `display_name` / `description` are human-readable text the CLI has
already written, so the bridge does not summarise. And `suggestions` is a list
of `PermissionUpdate` that the CLI itself proposes.

That last one killed a bad idea before it was built. An earlier draft of 3.0
proposed remembering "always allow" by keying on the first token of a command
line -- which would have been a second, worse permission model sitting beside
the real one. Returning `context.suggestions` in `updated_permissions` **is**
"remember this", in the CLI's own format.

**`updated_permissions` is a `list`**, settling a question this plan has
carried as "documented inconsistently" since the first review.

**"Always" defaults to session scope.** `PermissionUpdate.destination` can
write to userSettings, projectSettings or localSettings; a dialog on a Win98
box quietly editing the developer's settings files is a surprise nobody asked
for.

### 2026-09-17 — the permission prompt will be modeless, against the plan

The plan has said "permission requests as real modal dialogs" since the first
draft, and that is the one part of 3.0 being built differently. Three reasons,
in order of weight:

1. **A modal pumps messages.** The plan already flagged this: `WM_RC_FRAME`
   dispatches *during* the modal, re-entering the transcript mid-stream.
   Modeless has no nested message loop, so the problem does not exist.
2. **The obvious fix costs memory that is not there.** Deferring frames for the
   duration means holding heap blocks, and the guest has **28 KB free**.
3. **It behaves better.** A modal freezes the window while the model waits; a
   bar lets the transcript keep streaming behind it, which is how you can see
   *why* the tool was requested.

Disabling Send while a prompt is pending keeps the property the modal existed
for: the request cannot be ignored.


### 2026-09-17 — re-planning Phase 3 with Phase 0's answers in hand

The plan's own instruction was "plan two phases deep, rolling ... re-plan at
the Phase 2 boundary with real answers in hand". This is that.

**The two answers that mattered both came back favourable.** 0.9 settled the
one that could have reshaped the phase: `exec.c`'s capture was suspected of
returning nothing, with a rewrite budgeted as the fallback. It returns correct
non-empty output, including `echo hello | find "hello"` through the 16-bit
shell, so the design holds and the rewrite is not needed. 0.10 settled the
other: `GetDIBits` emits `BI_RLE8` directly at 11.3:1, so screenshot
compression is free and GDI does it.

**The one that came back badly is memory.** Measured with only the app running
-- no DOS box, no probe -- the guest goes 2420 KB free at window creation, 1060
KB after the chat list, and **28 KB after a turn**. The plan was written around
"8-12MB free after boot" and the HTML transcript now spends nearly all of it.

That kills one assumption outright: a full-screen capture needs a 307 KB DIB
and there is no room for it. Capture becomes **strip-based**, a few scanlines
at a time into a small fixed buffer, which also means a new gate before that
work starts -- **0.12, does `GetDIBits` produce valid `BI_RLE8` for a scanline
RANGE?** RLE8 is row-oriented so it ought to, but "ought to" is exactly what
0.9 and 0.10 existed to replace.

**Permissions moved out of Phase 4 and into Phase 3 as 3.0**, where the plan
always said they belonged: a tool surface that runs programs on the user's
computer without `can_use_tool` is `bypassPermissions`. Phase 4 shrinks to
settings and polish, and several of its items are already done -- model and
effort selection shipped early, and the SDK questions it told us to verify are
now answered rather than assumed.


### 2026-09-17 — the model list comes from the CLI, and "Default" is gone

The first version of the Model menu was a hardcoded table on the bridge. It is
now `get_server_info()["models"]`, which is better in three ways that all
matter: the names stay correct without a release, `resolvedModel` says what an
alias actually points at, and `supportedEffortLevels` says which effort
settings a given model even has -- **Haiku has none**, so an Effort menu
offering five levels was lying about it.

**There is deliberately no "Default" entry.** Default is not a model, it is an
alias, and a menu that says "Default" does not tell you what you are talking
to. The alias is resolved and the real model behind it is the one ticked: with
nothing selected the menu shows a tick against "Opus (1M context)", because
that is what the CLI resolves default to today.

### 2026-09-17 — the status bar shows transfers, not totals

The byte counters used to sit there permanently as a running total, which told
you nothing. What is worth knowing is whether something is moving right now and
how big it is, so the pane is empty when idle and fills in during a transfer.

Two shapes, because there are two kinds of traffic. A chunked transfer carries
`total` and `off` (in decompressed bytes, on every chunk rather than only the
first, so a reconnect mid-transfer does not leave the guest with a denominator
it never received) and shows as `rx 12k/64k`. Streamed text arrives as many
small frames whose "total" is just their own length, so showing a fraction of
each would be meaningless -- those accumulate into a running count for the
burst instead. Under 10K the exact figure is shown, because "rx 412/900" says
something that "rx 0k/0k" does not, which is what the first attempt displayed.

The pane clears after 1.2s of silence rather than instantly, which is also what
makes a stalled transfer visible: the numbers stop but the pane stays.

### 2026-09-17 — page size is scaled to the guest's RAM

The 32MB floor is what the whole design is built around, but making a 128MB
machine page every 90 blocks is a restriction inherited from a machine it is
not running on. The guest reports `GlobalMemoryStatus().dwTotalPhys` in HELLO
and the bridge sizes that guest's paginator from it: 32MB keeps the original
16KB/90, and it scales linearly to a 4x cap at 128MB. Below 32MB it stays at
the floor rather than shrinking further.

Found while adding it: the HELLO key/value arrays were `[4]`, and `ver`,
`secret`, `max_frame`, `mem_kb` and `sess` is five. It had been overflowing the
stack the moment a session id was present.


### 2026-09-17 — menu bar, and the app is called Gaston Code

**The name.** "Claude Code" is Anthropic's product name and this is a
third-party client for it, so shipping under that name is not on. Gaston is a
French given name of roughly the right vintage for the machine it runs on. The
About box says "Not affiliated with Anthropic" for the same reason.

**The menu bar** is built with CreateMenu/AppendMenu rather than from a
resource: it is a dozen items, and the two interesting menus are filled in at
runtime anyway.

  File      Export as HTML... (wired, not implemented), Close
  Configure Server Host... (shows the current setting; dialog still to come)
  Model     from the bridge, with a checkmark
  Effort    from the bridge, with a checkmark
  Help      About...

**Model and effort lists come from the bridge**, the same argument as the
stylesheet: model names change, and changing a string should not mean
rebuilding and re-deploying a Win98 binary. The guest numbers the menu entries
from a base (IDM_MODEL0, IDM_EFFORT0) because the lists are not known at
compile time, and "which one is current" has exactly one source of truth.

**Effort cannot be changed on a live session, and that is now confirmed rather
than assumed.** The installed SDK has `set_model()` and **no** `set_effort()`;
effort is a field of ClaudeAgentOptions, fixed when the session starts. So the
plan's fork dance is what actually happens: close the client, resume the same
conversation with `fork_session=True`, and tell the reader in the transcript
that the conversation was forked and why. Model changes are live.

Both verified on the guest: selecting Sonnet 5 moved the checkmark and logged
`model -> claude-sonnet-5`; selecting High logged `effort -> high (forking ...)`
and reconnected as `model=claude-sonnet-5 effort=high`, so the per-guest choice
survives the fork rather than reverting to the process defaults.


### 2026-09-17 — what HTML bought, and what it cost

Things the transcript can now do that RichEdit 2.0 could not do at all:

  * **tables.** Markdown tables render as real tables with a tinted header
    row, borders and wrapping cells. The styled-run renderer could only ever
    have shown the pipe characters.
  * **ordered lists.** `1.` becomes `<ol>` rather than silently turning into a
    bullet and losing the numbering the model meant.
  * a code block with a language label bar that **scrolls inside its own box**
    rather than giving the whole transcript a horizontal scrollbar.
  * padding, margins, borders and background tints, which is most of what made
    the old view look like a text dump.

Two rendering bugs found by reading the screen rather than the tests:

  * **`code` inside `**bold**` was escaped rather than parsed**, so backticks
    appeared literally. The inline matcher now recurses into the emphasis
    span; the delimiters are stripped first, so it cannot re-match itself.
  * **underscores inside identifiers were emphasis.** `BPB_RsvdSecCnt and
    BPB_NumFATs` rendered as one italic run from the first underscore to the
    last, mangling exactly the identifiers this project talks about all day.
    Underscore emphasis now requires a word boundary on both sides, which is
    what every markdown implementation does; asterisks are unrestricted.

### 2026-09-17 — pages are bounded by element count now, not just bytes

The byte cap alone is the wrong bound for an HTML renderer, because MSHTML's
cost scales with **element count**: a hundred one-line paragraphs cost far more
than one paragraph of a hundred lines, and a byte cap cannot tell those apart.
`MAX_BLOCKS = 90` (hard cap 180) now sits alongside the line and byte caps.
Unlike the byte cap it never splits a run -- a block boundary is already a
clean place to stop, so `add()` seals between blocks before starting the next.

**It did not help the case that prompted it, and the reason is worth keeping.**
A 200-line answer was assumed to be 200 elements. It is not: consecutive
non-blank lines are one markdown paragraph, so it renders as a single `<p>`
with 199 `<br>` separators -- one block, one element, and the cap never fires.
Measured either way, the guest sat at 20-48 KB free, so the document was never
the expensive part. The cap is still correct for a conversation made of many
separate blocks; it simply was not the lever for this workload, and the "200
elements" reading of the first measurement was wrong.

Memory on the 32MB guest, free of 32288 KB:

| | free | reading |
|---|---|---|
| desktop, before MSHTML has ever loaded | 9928 KB | the honest baseline |
| engine resident, app closed | 2548 KB | the engine holds ~7.4 MB and **does not give it back** |
| app running, 200-line answer | 20-48 KB | app + document ~2.5 MB on top |

Total ~9.7 MB against the 20 MB budget, so the gate passes, but the engine is
7.4 MB of it and that part is fixed.

**The obvious lever was tried and does not work yet.** Hosting
`CLSID_HTMLDocument` directly instead of `CLSID_WebBrowser` should avoid
shdocvw and urlmon along with navigation, history and the WinInet cache, none
of which a transcript needs. It compiles, activates, and paints nothing: MSHTML
hosted directly is a **document object**, not an ordinary embedded control, so
it does not simply create a window on `DoVerb`. It calls back through
`IOleDocumentSite::ActivateMe` and hands over an `IOleDocumentView`, which the
host must then `SetInPlaceSite` / `UIActivate` / `SetRect` / `Show`. That was
implemented -- and it still came up blank, so something else in the DocObject
contract is missing.

Reverted to the WebBrowser control, which does that plumbing and works. If the
~2 MB is ever needed, this is where to start, and the missing piece is in the
activation sequence rather than in the interfaces themselves.

Worth stating plainly: `dwAvailPhys` on Win9x excludes the disk cache, which
shrinks under pressure, so "48 KB free" is the edge of the cache being
squeezed out rather than the edge of failure -- the app stayed responsive
throughout. It is still much tighter than the RichEdit version's 1.4 MB, and
that is the price of the look.


### 2026-09-17 — the transcript is HTML now

RichEdit 2.0 is gone. `win/transcript.c` is deleted, `win/htmlview.c` hosts
MSHTML, and the bridge emits HTML instead of styled runs.

**Why, given the plan said no.** The plan's objection was a "multi-MB DLL tax
... fixed per process so pagination doesn't reduce it", which was an estimate,
and the counter-argument is that Windows 98's own Explorer hosts MSHTML for
folder Web View. Measured rather than argued (32MB guest, all figures free of
32288 KB):

| | free | marginal |
|---|---|---|
| desktop, nothing running | 9928 KB | — |
| RichEdit client, chat loaded | 8688 KB | ~1.4 MB |
| **HTML client, chat loaded** | **700 KB** | **~9.2 MB** |
| after closing the HTML client | 3236 KB | engine stays resident: ~6.7 MB |

So ~9.2 MB against a 20 MB budget: it passes, and it is ~7.8 MB more than
RichEdit. Most of that is the engine itself, which **stays loaded after the
app exits** -- free memory does not return to the 9928 KB baseline. Physical
free drops to ~700 KB while running, so the machine leans on its swap file;
`dwAvailPhys` excludes the disk cache, so the real headroom is better than that
number looks, but it is not comfortable.

**What it buys**, all of which RichEdit 2.0 cannot do at all: padding and
margins, borders, background tints, real bulleted lists, a language label bar
on code blocks, a code block that scrolls inside its own box, and inline
screenshots later. The look is not a marginal improvement.

**Architecture.** Unchanged where it matters: pagination still bounds memory,
the coalescer still batches at ~10fps, compression is untouched, and the guest
still transforms nothing. Two changes:

  * **The stylesheet lives on the bridge**, sent once in a `DOC` frame. The
    look can be changed by restarting the bridge instead of rebuilding and
    re-deploying a binary, which turned a 4-minute iteration into a 30-second
    one while tuning the CSS.
  * **Two containers, `#t` and `#l`.** Finished blocks accumulate in `#t` and
    are never touched again; `#l` holds the block still being written and is
    replaced wholesale as it grows. Without the second one a paragraph would
    only appear once complete, which reads as a stall rather than as
    streaming. Only `#t` blocks are recorded in the paginator -- storing the
    live one would put a dozen half-finished copies of the same sentence into
    the page.

**Three things that cost time:**

  * `oaidl.h` declares VARIANT with **named** unions under `__STDC__`, but
    `oleauto.h`'s `V_VT`/`V_UNION` macros only follow when `NONAMELESSUNION`
    is defined. Left alone the two disagree and every VARIANT access fails to
    compile. Define `NONAMELESSUNION` before the OLE headers.
  * The control is created late, when the `DOC` frame arrives, so it has
    missed every layout and is still sized to whatever `DoVerb` was handed.
    The document loaded, `insertAdjacentHTML` returned `S_OK`, the DOM read
    back the right content -- and nothing painted, because the window was the
    wrong size. Call `layout()` right after creating it.
  * IE5 has no `pre-wrap` and no `word-wrap`, so one long line in a code block
    gives the **whole transcript** a horizontal scrollbar and every paragraph
    has to be panned to read one line of C. `overflow: auto` plus an explicit
    `width: 100%` on the wrapper confines the scrolling to the code block.

Open Watcom ships `mshtml.h`, `exdisp.h`, `docobj.h`, `oleidl.h` and
`uuid.lib`, so nothing was vendored. It does not ship `mshtmhst.h`, so
`IDocHostUIHandler` is unavailable without hand-declaring it; it only controls
chrome, and the scrollbars it would suppress are ones the transcript wants.


### 2026-09-17 — MSHTML is affordable after all, and it looks far better

The plan rejected MSHTML for v1 partly on a "multi-MB DLL tax ... fixed per
process so pagination doesn't reduce it". That was an estimate, and the user
pointed out the obvious counter: **Windows 98's own Explorer hosts MSHTML** for
folder Web View, so the engine is already resident in the shell and a second
host pays only for its own instance data.

Measured on the 32MB guest rather than argued about:

| | free of 32288 KB | cost |
|---|---|---|
| baseline, no app | 9928 KB | — |
| RichEdit client, large chat loaded | 8688 KB | **~1.4 MB** |
| CoInitialize | 8312 KB | 0 KB |
| + LoadLibrary(mshtml.dll) | 8028 KB | 284 KB |
| + CoCreateInstance(HTMLDocument) | 7932 KB | 96 KB |
| + CoCreateInstance(WebBrowser) | 7800 KB | 132 KB |
| **spike hosting a rendered document** | **5832 KB** | **~4 MB** |

Two numbers matter and they are different: **512 KB to instantiate** the engine,
**~4 MB with a document actually laid out and painted**. Quoting the first as
the cost of an HTML transcript would be wrong.

~4 MB against a 20 MB budget, with ~5.9 MB physical still free, so it fits. It
is ~2.6 MB more than RichEdit, and it buys padding, margins, borders,
background tints, real bulleted lists, tables, and eventually inline
screenshots -- none of which RichEdit 2.0 can do, and all of which the
transcript visibly wants.

**The spike** (`win/htmlspike.c`, ~360 lines) hosts the WebBrowser control from
plain C89: IOleClientSite, IOleInPlaceSite and IOleInPlaceFrame as embedded
structs with static vtables, recovered to the host by fixed offsets. It
activates and paints on real Win98 SE. Open Watcom has `mshtml.h`, `exdisp.h`,
`docobj.h`, `oleidl.h` and `uuid.lib`, so nothing had to be vendored.

One gap: OW ships `mshtml.h` but **not `mshtmhst.h`**, so `IDocHostUIHandler`
has to be hand-declared. It only controls chrome -- the 3D border, the
scrollbars, the context menu -- so the spike does without it and answers the
question that actually gated the decision.


### 2026-09-16 — the transcript was never broken; autoscroll was overshooting

Chasing "the chat window has rendering issues" produced one genuinely
misleading bug and several real ones.

**The misleading one.** After a turn the transcript went blank, with a
scrollbar showing content. It looked exactly like the riched20 priming failure
all over again, and three plausible causes were investigated and cleared
before the actual answer turned up: scrolling to the top with Ctrl+Home showed
**the answer had rendered perfectly all along**. `WM_VSCROLL`/`SB_BOTTOM` was
parking the view about a screenful BELOW the last line, because it is computed
from scroll metrics that are still stale immediately after `WM_SETREDRAW` is
turned back on. It only appeared once a turn grew past one screenful, which is
why short replies had looked fine all evening.

Fixed by scrolling the caret instead: every append already leaves the
selection collapsed at the end, so `EM_SCROLLCARET` after the repaint is
exactly "show the newest text" and cannot overshoot.

**Two things that really do wedge riched20**, both found while hunting the
above, and both worth knowing because they look like the same symptom:

- **Deleting with `EM_REPLACESEL` stops the control painting.** The obvious
  design for a progress indicator is to append "Claude is thinking..." and
  remove it when output arrives. The delete lands on the right range and the
  control never draws again. Measured twice, including with the selection
  positions corrected: `truncate to 152 from 174 left 153`.
- **So does a select-all, when redraw is off.** `WM_GETTEXTLENGTH` counts a
  line break as CRLF while selection positions count it as one, so the two
  disagree by the number of lines. The "accurate" fix -- select `0..-1`, read
  the range back, collapse -- is a select-all on every append, and inside a
  batch (`WM_SETREDRAW FALSE`) that wedges the control too. The over-count is
  harmless for appending because `EM_EXSETSEL` clamps, so appending
  deliberately keeps using it.

Consequence: **nothing that has to disappear again goes in the transcript.**
The thinking indicator lives in the title bar ("Claude Code - thinking...")
and in a status pane that ticks the elapsed seconds, neither of which requires
touching the control.

### 2026-09-16 — the chat list was overdrawing its own border

The stray vertical marks were the listbox's `WS_EX_CLIENTEDGE` border being
painted over. Measured rather than guessed: x=8 should be `(128,128,128)` and
x=9 `(0,0,0)`, and on 152 of 365 rows both were white instead. The bands were
row-height multiples, stayed put when the list was scrolled, and survived a
minimise/restore.

They lined up exactly with the rows whose label was **wider than the client
area**: Win98's listbox overdraws its own client edge when an item overflows.
Labels are now truncated to fit with `GetTextExtentExPointA` (one GDI call per
label, not a search) and an explicit "...", which fixes the artifact and also
replaces silent truncation at the frame with something that reads as "there is
more". The list went 150px -> 170px while we were there, and the rebuild is
batched under `WM_SETREDRAW` rather than repainting once per `LB_ADDSTRING`.

Result: 360/360 rows now carry the correct border colours.

### 2026-09-16 — CP1252 0x80-0x9F has no glyphs in the stock bitmap font

An em dash rendered as a vertical bar and a euro sign as a capital I. Both are
perfectly valid CP1252, which is why `fold()` passed them straight through.

0x80-0x9F is where Windows put the extra typographic characters, and Win98's
stock UI font has nothing there; everything from 0xA0 up is Latin-1 and renders
correctly. The whole range is now folded to ASCII -- em dash to `--`, euro to
`EUR`, trademark to `(tm)` and so on -- with a test that walks all 32
codepoints rather than listing examples. Two older tests asserted the euro
survived; they were asserting something the guest disagrees with, and were
corrected.

Separately, `CFM_CHARSET` with `ANSI_CHARSET` is now set on every run. Without
it RichEdit picks a default that need not be Windows-1252, and accented Latin-1
bytes render as whatever the font's default mapping has there. With it,
`café naïve über` comes out right.

### 2026-09-16 — one definition of what a user turn looks like

`user_block()` in `bridge/markdown.py` renders a rule, the prompt lines each
prefixed `> `, and a blank line. Both ends build it: the guest echoes it the
instant Send is pressed, so a slow link never swallows your own typing, and the
bridge records the identical string into the paginator **without streaming it**
(`_write_text(..., stream=False)`).

Recording it is the point. A sealed page is re-sent in full when the reader
pages back, and before this the paginator only held an anchor, so paging back
gave a transcript of answers with no questions in it.


### 2026-09-16 — the blank transcript: riched20 must be primed with real text

The single worst bug of the guest bring-up, and the one the Wine tier was least
able to catch.

**Symptom.** On real Win98 the transcript control stayed blank forever. Not
"wrong font", not "wrong colour" — nothing at all, while everything else in the
window worked.

**What it was not.** Ruled out, in order, each by measurement rather than
argument:

- not insertion: `WM_GETTEXTLENGTH` returned 22 and `WM_GETTEXT` returned
  `Connected to bridge.`, so the text really was in the control;
- not geometry: `GetWindowRect` gave `162,51 470x345` and `EM_GETRECT` gave a
  formatting rectangle of `3,4,463,338`, both correct, with `IsWindowVisible`
  true;
- not the character format: `EM_SETCHARFORMAT` was accepted and
  `sizeof(CHARFORMAT2A)` is 84, the RichEdit 2.0 value;
- not redraw batching: putting `WM_SETREDRAW` behind a switch and turning it off
  changed nothing;
- not `ES_READONLY` swallowing `EM_REPLACESEL`, though that was a real latent
  bug and is fixed too (the style is now applied with `EM_SETREADONLY` and
  lifted around each batch).

**What it was.** Win98's `riched20` does not lay out or paint what
`EM_REPLACESEL` puts into a control that has never held text. Two details, both
found the hard way:

- an **empty** `WM_SETTEXT` does not count. The control needs actual
  characters, which is why the priming `\r\n` is left in place rather than
  deleted afterwards;
- priming must happen with **redraw enabled and followed by an actual paint**.
  Priming from inside an append batch (which runs under `WM_SETREDRAW FALSE`)
  does nothing, which is why page loads stayed blank after the live page had
  been fixed — `clear()` -> `prime()` -> `begin()` disabled redraw before the
  control ever painted.

So `prime()` sets real text, forces `InvalidateRect` + `UpdateWindow`, and is
called from `rc_transcript_create` and from `rc_transcript_clear`, never
mid-batch.

**The lesson is about the Wine tier, not about RichEdit.** Wine's `riched20`
paints without any of this, so `make wine-smoke` passed the entire time. The
plan already said Wine proves code sanity and never platform behaviour; this is
what that sentence costs when you find out the hard way.

### 2026-09-16 — three process bugs that each cost more than the code bug

Recording these because all three made a *fixed* bug look unfixed.

1. **`win/Makefile` had no header dependencies.** Adding two fields to
   `rc_transcript` in `transcript.h` did not rebuild `main.o`, so `main.c`
   still allocated the old struct while `transcript.c` wrote the new fields
   past the end of it, corrupting the next global. The symptom was
   `PostMessage` failing on a valid HWND with `inflight=0` — nowhere near the
   change, and a dead ringer for a Win9x message-queue limit. Every object now
   depends on every header.
2. **`virsh change-media --update` with the same source path is a no-op.** The
   drive keeps the file it already has, so a rebuilt `tools.iso` is never seen
   and the guest quietly keeps running the previous binary. Several "the fix
   did not work" cycles were testing a stale exe; the guest's copy was 131,584
   bytes while the build was 132,096. `tools/vmship.sh` now ejects first and
   prints both sizes.
3. **`pkill -f` matches the shell running it.** Killed my own session, exit
   144, twice. Find the process by its listening port instead.

### 2026-09-16 — header values are CP1252, percent-encoded to ASCII

`_quote` escaped only `%`, LF and CR. `SESSION` frames carry session titles,
which are arbitrary user text, so the first real chat list sent to the guest
crashed the bridge with

    UnicodeEncodeError: 'ascii' codec can't encode character '\u2442'

on every reconnect, in a loop, so the client could never populate its chat
list. Header values are now folded to CP1252 and percent-encoded for anything
outside printable ASCII, decoded in a single pass so `%25` does not unescape
twice. Six regression tests, including control characters and a stray `%` with
no hex digits after it.

The general point: the guest has one codepage and it is not Unicode, so a
header and the body it describes have to agree about what a character is.

### 2026-09-16 — Win98 has no PCI bus here, so the NIC is ISA

The PCnet would not enumerate. `C:\DETLOG.TXT` said why, and said it about the
machine rather than the driver:

    Checking for: System Bus
    CheckInt86xCrash: int 1a,AX=b101,rc=0
    SetVar: PCIBUS=
    MatchAcpiOemIdRule: ACPI not detected
    Detected: *PNP0C00\0000 = [1] Plug and Play BIOS
    SetVar: PNPBIOS=

`INT 1Ah/AX=B101` is the PCI BIOS installation check; it came back empty, so
Win98 concluded there is no PCI bus and enumerated not one PCI device. Device
Manager has no `PCI bus` node at all and the PnP BIOS node sits at Code 24.
Both `DETLOG.TXT` and `DETLOG.OLD` agree, so it has been true since install.

SeaBIOS is not at fault: `_32_` at 0xf6100, `$PnP` at 0xf6120 and `$PIR` at
0xf55c0 are all present, 16-byte aligned, with valid checksums.

Rather than chase it, the NIC is now an **ISA NE2000**, which needs no
enumeration at all — and is period-correct for the Pentium MMX 200 this guest
stands in for. Two gotchas: libvirt assigns a PCI slot unless an
`<address type='isa'/>` is given (QEMU then refuses with "Device 'ne2k_isa'
can't go on PCI bus"), and the card sits at **0x200/IRQ3** rather than the
NE2000 factory 0x300 because that is what Win98's "Basic Configuration 0" pins
the driver to and the Resources tab will not step off it. Moving the emulated
card was cheaper than fighting the dialog.

### 2026-09-16 — the APIC was never actually removed

The IRQ0 hang came back the moment the guest rebooted after the network
install, at the same EIP as before. `<features/>` with no `<apic/>` only drops
libvirt's own hint; the `pentium2` CPU model still carries the CPUID APIC bit,
so QEMU built a local APIC and an IOAPIC anyway. `info lapic` showed
`SPIV 0x000000ff APIC disabled` with `LVT0` masked — Win98 had software
disabled the LAPIC, and the 8259's ExtINT then had nowhere to go:
`pic0: irr=01 imr=b8 isr=00` with `IF=1`.

The fix that actually removes it is a CPU feature, not a machine feature:

    <cpu mode='custom' match='exact' check='none'>
      <model fallback='allow'>pentium2</model>
      <feature policy='disable' name='apic'/>
    </cpu>

After that `info lapic` reports "local apic state not available" and the guest
boots.

### 2026-09-16 — `virsh define` restored the floppy and Setup ran again

`vm/win98.xml` still had `<boot dev='fd'/>` and a floppy device from install
day. Redefining the domain from that file under a working installation put
Setup back on screen at "Preparing to run Windows 98 Setup". Caught at 90% and
restored from a backup taken two minutes earlier, so nothing was lost.

The floppy device is now deleted from the XML outright rather than ejected —
ejecting does not survive a restart — and `tools/vmdefine.sh` refuses to define
any config that boots `fd` first or still carries a floppy, and validates the
XML first (libvirt rejects `--` inside a comment and points at the wrong line
when it does).

### 2026-09-16 — driving the guest: what works

- **Keyboard is the reliable path.** `Ctrl+Esc`, `r`, type, Enter reaches
  anything through Start->Run. Control Panel applets by name:
  `control netcpl.cpl` for Network, and Add New Hardware is applet 1 of
  `sysdm.cpl`, reached with
  `rundll32.exe shell32.dll,Control_RunDLL sysdm.cpl,@1`.
- **The mouse needed calibrating.** QEMU's PS/2 mouse is relative, so
  `tools/qmp.py` homes the pointer into a corner and walks to the target. A
  step of 4 overshot by almost exactly 2x (a requested `100,100` landed at
  `197,192`); a step of 1 is exact. The queue is short, so the walk is paced.
- **The pointer is not in `screendump` output**, so position has to be found by
  differencing two raw PPMs rather than by looking for a cursor.
- **`COMMAND.COM` rejects a trailing backslash on a COPY destination**
  ("Invalid directory"). `COPY D:\*.EXE C:\RETRO` works, `C:\RETRO\` does not.
- **Reading a guest file from the host mid-run does not work**: the qcow2 shows
  allocated-but-unwritten clusters (all zeros) because Win98's cache has not
  flushed, and the running app holds the log open ("Sharing violation"). Use
  `find /v "..." C:\RETRO\retroclaude.log` inside the guest instead.

### 2026-09-16 — known cosmetic issues in the chat list

Not fixed, and both predate the guest bring-up — they are visible in the first
screenshots of the original binary:

- **A 1px vertical line at x=9** inside the list, three pixels into the client
  area and inside the first character cell, spanning *contiguous groups* of
  rows (1-5, 8-12, 14-15, 19, 22-23, 26 in one capture) with clean gaps
  between groups. The wire bytes are plain ASCII, so it is not corrupt text,
  and contiguous multi-row runs with no inter-row gaps argue for a partially
  overdrawn border rather than a per-row glyph. Root cause still open.
- **Titles are clipped** at the listbox width with no horizontal extent and no
  tooltip, so long names cannot be read at all.


### 2026-09-16 — Win98 needs TCG, and needs APIC/ACPI OFF

Two separate emulation faults, both diagnosed from guest CPU state rather than
guessed at. Recording them because neither is obvious and both cost hours.

**1. KVM corrupts Win98's loader.** Under `domain type='kvm'` the install
completed but Explorer died and then:

    The SHELL32.DLL file is linked to missing export SHLWAPI.DLL:...FileAttributesA

Both DLLs on the guest disk were **byte-identical to the install media** (md5
compared against the files extracted from the ISO's cabinets). A missing export
is impossible when both files are correct, so the import resolution itself was
executing wrongly. Fix: `domain type='qemu'` (TCG). Slower, correct, and still
far faster than the Pentium MMX 200 being emulated.

**2. `<apic/>` sends IRQ0 into a black hole.** Win98 then hung in real mode,
spinning forever at a fixed EIP. `info registers` and `info pic` gave it away:

    EIP=00009dfb  CR0=00000010 (PE clear -> real mode)  EFL=0x246 (IF set)
    pic0: irr=43 (IRQ0 PENDING) imr=b8 (IRQ0 UNMASKED) isr=00 (nothing blocking)
    IRQ0 delta: 92 interrupts in 5s = 18.4Hz -- the PIT was perfect

Disassembling the hang site showed Win98's CPU speed-calibration loop:

    0x8ee4b:  movw %es:0x46c, %dx   ; BIOS tick counter at 0040:006C
    0x8ee50:  cmpw %ax, %dx
    0x8ee52:  je   0x8ee4b          ; wait for it to change -- it never did

A pending, unmasked, unblocked interrupt was never delivered to a CPU with
IF=1, because `<apic/>` gives QEMU an IOAPIC whose pins Win98 (a PIC-era OS)
never unmasks. Removing `<apic/>` and `<acpi/>` fixed it: the tick immediately
resumed at 18.25Hz.

**Lesson for the install procedure:** guest CPU state via
`virsh qemu-monitor-command --hmp 'info registers' / 'info pic' / 'x/24i'` is
the tool that settles "hung or slow", and it works when the guest is wedged.
Screenshot md5s do NOT settle it -- ImageMagick stamps PNG metadata, so
identical screens produce different hashes. Compare raw PPM bytes instead.

### 2026-09-16 — switching chats is close + resume, not a pool
One live `ClaudeSDKClient`. Switching closes it, reopens with `resume=<id>`, and
replays the stored history into a fresh `Paginator` so the guest can page back
through it exactly as if it had streamed. A warm pool is ~3x the work and
invisible on a single-user box.

### 2026-09-16 — ListBox, not ListView, and double-click to switch
ListBox costs fewer GDI objects, and the 16-bit USER/GDI heaps on 9x are a fixed
64KB that does not scale with RAM. Double-click rather than selection-change,
because an accidental arrow key would otherwise tear down a live session.

### 2026-09-16 — stored history is replayed defensively
`_text_of` walks persisted message data whose shape is not guaranteed across
releases. Every branch tolerates missing keys and wrong types, because a
KeyError there breaks the whole chat list rather than one message. Thinking
blocks are skipped on replay: long, and never the point of scrolling back.

### 2026-09-16 — three test-harness bugs that all looked like server bugs
Worth recording because each wasted time and each was in the *test*, not the
code under test:

1. `Server.wait_closed()` since Python 3.12 waits for existing **connections**,
   not just the listener. A test leaving a connection open hangs teardown
   forever. Bound it with `wait_for`.
2. A fresh `FrameReader` per receive call discards whatever partial frame the
   previous call buffered.
3. Returning on the first matching frame drops the rest of that `read()` batch
   — several frames normally arrive together.

Symptom in all three cases was "the server never sent TEXT". It always had.
Confirmed by reproducing the exchange in a standalone script, which worked
first try and pointed the finger squarely at the harness.

### 2026-09-16 — HISTORY mode must not receive live text
`_write_text` streams only when `view_page` is None or matches the live page.
There is a test asserting new text never carries `page=1` while the reader is
on page 1 — without it, text silently appears in the middle of a page the user
is reading.

### 2026-09-16 — coalesce first, THEN parse markdown
Ordering was an open question. Coalescing first is right: the parser receives
~2KB of whole lines instead of single tokens, which lets it merge adjacent
same-style runs. Each run costs an `EM_SETCHARFORMAT` + `EM_REPLACESEL` pair on
the guest, and that pair is RichEdit's expensive path.

Found by a test: `_merge` only coalesces within one `feed()`, so char-by-char
input produced more runs with identical content. Harmless, but it made the
ordering decision obvious.

### 2026-09-16 — no italic style
RichEdit could do it, but italic bitmap text at 8pt on a 640x480 CRT is hard to
read. `*emphasis*` degrades to plain with the markers stripped.

### 2026-09-16 — the coalescer's interval, not its target, sets frame size
First implementation targeted 2KB frames and **never compressed anything**: at
realistic token rates a 100ms window accumulates only ~35 bytes. The target was
unreachable by construction.

Fix is to trade latency for ratio as the link slows: 100ms on a LAN, 600ms below
~2 Mbit/s. Buffering longer is *free* when the wire is the bottleneck -- at
5 KB/s a frame already takes seconds to send, so 600ms more is invisible and
buys a much better ratio. At LAN speed the opposite holds and latency wins.

This is also why `MIN_WORTH_COMPRESSING` is only 64 bytes: because the stream is
persistent, even small frames ride the warm dictionary.

### 2026-09-16 — streaming duplicates text unless you suppress completed blocks
With `include_partial_messages=True` the SDK emits **both** token deltas and the
completed `AssistantMessage` blocks. Rendering both prints every word twice.
`Session` skips the completed blocks while streaming.

### 2026-09-16 — `-bm` does not change the PE
Re-ran `make check` after adding `-bm`. Byte-identical result: 45 imports across
KERNEL32/GDI32/USER32, 0 CMOV, 22016 bytes. Open Watcom only pulls the extra CRT
machinery once thread functions are referenced. **Re-verify after the first
`_beginthreadex` call**, not before.

### 2026-09-16 — Experiment C: the fork risk was overstated
`protocol.c`, `util.c`, `log.c`, `exec.c`, `handlers.c` compile under Open Watcom
**unchanged, zero errors, zero warnings**. The plan assumed "nearly unchanged, build
system only"; the sources needed no edits at all. Phase 3's main remaining work is
reshaping the response path onto our frame grammar, not portability.

### 2026-09-17 — sync on the real machine, and the fourteen defects that found

The layer passed 1925 unit tests and a Wine cross-check before it ever touched
the Windows 98 box. Pointing it at the actual machine found fourteen things,
and not one of them was visible from either side's logs. Worth recording as a
class, because "green under Wine" will be tempting again:

**Two stopped it working at all.** `rc_config_load` never read `[sync] root` --
`save` wrote the key and `load` did not read it, so the guest ran with an empty
sync root and the whole layer was silently off. And `net.c`'s reader never
routed `SYNC*` to the worker; they fell through to the UI thread's "unhandled
frame type" branch, so the guest never answered a scan while the bridge sat on
a 600s timeout holding the lock.

**One killed the process.** An invalid page fault at `sub $0x202c,%esp`: the RPC
worker gets the image's 64KB stack commit, the scan had a ~62KB manifest buffer
plus two 8KB buffers in one call chain, and Open Watcom emits no stack probe, so
the frame stepped over the guard page. It presented as intermittent -- the same
build ran a whole session against a flat tree and died the first time the tree
gained a subdirectory, because one more `walk` frame moved the skip past the
guard. `make check` now gates on frame size using a linker map.

**Four lost or corrupted data.** `Act.MKDIR` and `Act.PULL` had no handler at
all in `Pass.run` -- decided correctly, ranked correctly, then off the end of an
if/elif chain, with the pass reporting success. Conflicts staged the agent's
copy and never applied the user's, so the same conflict re-staged every thirty
seconds forever. Paths went on the wire as UTF-8, so `café.md` arrived as
`cafÃ©.md` and the tree grew a filename every pass. And the name filter ran on
files but not directories, so the bridge asked Windows 98 to create
`C:\WORK\con`.

**The lesson worth keeping:** every one of these was a gap between a decision
and its application, or between two sides of a protocol. The unit tests proved
each half in isolation and the halves did not meet. The tripwire that now fails
if any `Act` loses its handler is the shape of test that would have caught four
of them.

### 2026-09-17 — driving the guest directly, because the bridge cannot

Most of the guest's SYNC statuses are unreachable from the bridge on purpose.
`changed` needs a save to land between the scan and the commit; `crc_mismatch`
needs a corrupted transfer; `is_directory` needs a push at a path the guest
holds as a directory. The bridge will never produce any of them deliberately,
so on the real machine that code had never executed.

Racing for them does not work: a 2.4MB push over loopback finishes in about a
second, and `--link-bps` throttles the bridge's sending logic rather than the
wire. Two attempts produced results that turned out to be reading state left
over from the previous attempt.

`tools/syncprobe.py` is a bridge stub instead -- enough handshake to satisfy
the client, then handcrafted frames. 43 checks against the shipping binary over
a real socket. It found two bugs in `SYNCGET` the same afternoon: a missing
file and a directory both answered `locked`, which the bridge treats as
transient, so a pull of something just deleted would retry forever and report
nothing.

### 2026-09-17 — the client declares what it is; the bridge stops assuming

One HELLO header per option, each a small integer enum, decided against packed
bits. `devices=1` answers "why did that file not sync" in a log and `caps=0x7F`
does not; a bit can only answer yes or no, so a client that is neither CRLF nor
LF needs a second bit and the hope that the pair is read together; and a value
the bridge does not recognise must be distinguishable from a bit that is clear,
which is the permissive reading arrived at silently. ~120 bytes once per
connection.

Defaults lean restrictive because the two errors are not comparable: guessing
`devices=none` on a DOS box pushes `aux.c`, and CreateFileA on AUX with nothing
asserting DSR blocks with no timeout. Guessing `devices=dos` on Unix skips one
file and prints why.

The codepage is the one field never guessed, because it is the only silent
failure in the layer: the wrong table encodes a name, the guest stores those
bytes, reports them back, and the bridge decodes with the same wrong table and
sees what it sent. Self-consistent forever, with the user's file called
something else on their own disk.

### 2026-09-16 — compression: measured, and the plan was optimistic
Real ratio on prose-plus-code is **~2.16:1**, against the ~3:1 the plan assumed.
Corrected in PLAN.md. Still clearly worth doing (dialup 8s → 3.7s for a 40KB turn).

Three parameters settled by measurement rather than argument:

- **Persistent deflate stream, never per-frame.** Independent deflate per frame is
  1.27× worse.
- **Coalesce to ≥2KB frames.** Frame size is the biggest lever: 256B → 1.68:1,
  2KB → 2.04:1, flattening after. The 10fps coalescer therefore buys ~21% bandwidth
  on top of its latency purpose — it was designed for one reason and pays for two.
- **No preset dictionary.** 2.16:1 either way; 1.86→1.89 on a cold 4KB start. The plan
  guessed this was marginal; it is, so the complexity is dropped.

Window stays 32KB (dropping to 8KB costs 6% ratio to save 24KB — bad trade against
8–12MB). Level 6 bridge→guest, level 1 guest→bridge.

### 2026-09-16 — netcheck.exe: test the C, not the GUI
Driving a Win32 GUI from a test harness is fragile. `netcheck.exe` links the
real `net.c` and `frameproto.c` and does the whole round trip headless, so the
networking and framing are covered without GUI automation. It is also the
thing to run first on the real guest, since it needs no display.

### 2026-09-16 — my own RichEdit lint list was wrong
The first `make lint` list included `EM_SETUNDOLIMIT`, `EM_SETPALETTE` and
`EM_AUTOURLDETECT` as "3.0-only". All three are **2.0**, and `EM_SETUNDOLIMIT`
is one we actively rely on to keep the undo queue from growing. Corrected to
the five genuinely 3.0-only messages.

### 2026-09-16 — SDK introspected; every uncertain API claim resolved
`claude-agent-sdk` 0.2.153. The reviewers flagged four things to verify before
Phase 4 rather than trust the plan on. All four came back favourable:

- `PermissionResultAllow.updated_permissions` is `list[PermissionUpdate]` — the
  plan's list comprehension is right (docs rendered it as `dict` in one place).
- **`ClaudeSDKClient.set_model()` exists**, so the model dropdown changes live.
- **`effort` is an option, not a setter** (`Literal['low','medium','high','xhigh','max']`),
  so changing effort *does* need `fork_session=True`. The plan guessed both correctly.
- `SDKSessionInfo` carries `summary`, `first_prompt`, `custom_title` — no title
  derivation needed.

Bonus, not in the plan: `get_context_usage()` and `stop_task()` exist.
`get_context_usage()` is a free context-fill indicator for the status bar.

### 2026-09-16 — header values are percent-encoded, not backslash-escaped
Three characters only (`%`, `\n`, `\r`). claudewin9x needed a section of its
system prompt teaching the model to quadruple backslashes across four
interpreter layers; percent-encoding removes that class of bug, and the guest
can still parse headers with `strchr`.

### 2026-09-16 — fold(): explicit map must precede the CP1252 passthrough
First version checked "can CP1252 represent this?" first, which silently skipped
every deliberate override. Curly quotes, the ellipsis, the bullet and nbsp *are*
in CP1252 but are overridden anyway, because they render poorly in a fixed-pitch
bitmap font at 640x480. Caught by tests, which is the argument for writing them.

### 2026-09-16 — vendored miniz rather than zlib
Two files, MIT, no build system, and it compiled clean under OW first try. Vendored
rather than submoduled so the guest build is reproducible offline.

### 2026-09-17 — auditing the plan against the code found a total-stop bug

The sync phase was verified end to end on the real machine and the tests were
green, so the remaining question was not "does it work" but "does it do what
the plan said". Auditing the twelve mechanisms the plan named against the code
turned up one defect that no amount of running would have surfaced, because
nothing in the test tree ever had a file and a directory collide at one path.

`stage_conflict` did `open(src, "rb")` on the bridge's side of a conflict. When
the guest puts a file where we hold a directory -- the user typing
`ECHO x > C:\WORK\notes` while the agent has `notes/` -- `src` is a directory,
`open` raises `IsADirectoryError`, and nothing below `run_sync` catches it. One
such collision therefore aborted **the entire pass**, taking every unrelated
file with it, on every pass, until someone renamed the file by hand. The only
symptom was a `sync pass aborted` line at warning level.

The fix is `shutil.copytree`, which is also what row 3 always asked for: the
subtree is the thing being preserved, and copying only the leaf would have lost
every descendant.

Underneath it was a quieter one. A type change cannot be applied -- a pull
writes a file and cannot replace a directory without a delete primitive we
deliberately do not have -- so nothing advances, so the next pass decides it
again. Staging it every time produced a fresh `mine.N` and a fresh notice every
thirty seconds: the same "say it once" bug the standing divergences had,
wearing a type change as a disguise. It now goes through `first_report`, and
the end-of-pass pruning keeps the fingerprint for a conflict that will never
resolve instead of forgetting it announced it.

Two smaller things from the same audit. The conflict notice told the agent to
"merge the two" even when one side was a directory, which sends it looking for
a file that is not there; unapplyable conflicts now say what is actually wrong
and that every other path keeps syncing. And symlinks were contained but
skipped in silence -- an agent writing into a symlinked directory was told the
write succeeded and never learned that none of it was going to the machine.

**What this says about the method.** Fourteen defects came from running on real
hardware; this one came from reading the plan beside the code and asking which
of the plan's named mechanisms actually exist. The two techniques do not
overlap. Running finds what the tree happens to contain. Reading finds the
branch the tree has never had.

### 2026-09-18 — the tree's own rules, and a starvation bug that measured 128/128

Four gaps from the plan-versus-code audit, closed. Three were small. The fourth
turned into the honest version of a feature the plan had only sketched.

**`.gitattributes`, read natively.** `.gastonattributes` was the plan's idea and
it was the wrong one to build first: a source tree already states which files
are text, which are binary and what endings they take, the statement is already
correct because the build depends on it, and asking someone to write it a second
time in our own file invites the two to disagree. So `.gitattributes` is read
with git's nesting rules, `-text`, `binary`, `text`, `text=auto` and `eol=` are
honoured, everything else (`diff=`, `merge=`, `filter=`) is parsed and ignored
so a real one needs no editing, and `.gastonattributes` remains for what a tree
wants to say to sync without saying it to git.

Two decisions inside that are not obvious:

*`text` cannot force the unsafe cases.* Normalisation must be idempotent or the
algorithm oscillates, and `N("\r\r\n") == "\r\n"` while `N("\r\n") == "\n"`. A
lone CR, a `\r\r\n` or a mixed file therefore stays byte-exact whatever the
attributes say -- and says so, because a user who wrote `text` and silently got
the opposite would trust a guarantee they do not have. `-text` has no such
limit: refusing to touch something is always safe.

*`-text` and `eol=lf` are different requests.* `eol=lf` keeps the path in the
text class, where the sides are compared after folding, so they may
legitimately differ in endings and only the content has to agree -- that is the
Makefile a cross toolchain reads. `-text` says the bytes themselves must match,
so applying it to a file that is LF here and CRLF there makes them genuinely
different and one side loses. Both are correct; reaching for the wrong one is
the easy mistake, so `sync_status` explains the difference to the agent.

**The manifest carries both checksums, and that is a capability.** The guest was
already computing the raw and the normalised CRC in one read and throwing one
away. It had to stop, because an override means the bridge asks for a checksum
the guest's own classifier would not have chosen -- and the two cannot be
derived from each other. Which format a client speaks is `manifest=2` in HELLO
rather than something sniffed from the row: a path may contain spaces and the
rows are length-prefixed, so six fields and seven both parse, and a sniff would
guess wrong on some trees and not others. A v1 client is not overridden at all,
and is told why, on both sides -- overriding one side and not the other is
worse than not overriding, because it is a permanent disagreement rather than a
missing feature.

`win/synccheck.exe` now prints both, and the corpus asserts both against
Python. The old gate would have passed a raw checksum that was correct only for
files the classifier already called byte-exact -- which is every file the old
code ever used it for.

**Sync was not competing for the wire, it was starving the event loop.** The
backlog entry said a permission prompt could queue behind sync traffic because
`_send` holds `send_lock` across `drain()`. Measured, that was the wrong
diagnosis and the real one is worse: an uncontended `asyncio.Lock` does not
suspend, and `drain()` below the high-water mark does not either, so a chunk
loop contains no await point that yields. A probe put the number at **128 of
128 chunks** before another task ran at all. The prompt was not queued behind
sync; it had not been composed yet, and FIFO lock fairness cannot help a task
that is never scheduled.

So the yield is explicit, between chunks as well as between files, and while
the user has a prompt open sync stops the pass rather than pushing past it --
safe because each path commits on its own ack, so a pass cut short is a
consistent resting state. The test had to be rewritten once after it passed
with the fix removed: it was only observing the per-file yield. It now asserts
a turn was taken *part way through* a file, and without the chunk yield it
fails with `observed only [0, 9] of 9 frames`.

**The cache exists now, so its guard is not dead code.** `Entry.cacheable` was
implemented, unit-tested and never called. The bridge caches the classification
INPUTS -- both checksums and what the content says on its own -- rather than a
class and one checksum, so changing `.gitattributes` re-decides every path
without invalidating anything. Entries are stored only when the read cannot
have raced a save in a tick that was still open, which is the half of the
settle window that matters and the half the plan originally got wrong.

**Verified on the machine, not under Wine.** 50/50 guest-side probe checks,
including the new ones: the manifest carries both checksums and both are
correct; a byte-exact push is accepted against a file the guest's own
classifier calls text; it comes back byte for byte; `if_crc` against the raw
checksum is accepted and a wrong one is still refused. That last pair is the
whole override, and both halves of it live in C that Wine cannot vouch for.

One process note worth keeping. The first probe run was started with buffered
stdout, produced no output, and was killed -- but it had already run to
completion against the guest, so the next run started against a `C:\WORK` full
of its results and reported nine failures that were all stale state. The tell
was `and the file DID change` passing while the checks around it failed. Start
the probe with `-u`.


### 2026-09-18 — three review rounds before any code, and what they were worth

The backlog was the target and the method was the point. Three adversarial
rounds ran against the plan before a line was written, and each overturned the
one before it. That is not a ceremony: two of the nine bugs fixed below were
introduced *by the plan* and removed by a later round, and the single worst
defect in the tree was found by the third round in code nobody had flagged.

**Round 1** found that the plan's own unifying idea would have caused the
failure it was written to prevent. "A truncated string is a refusal, not a
shorter string" is right for a write path and wrong for the scanner, where a
refusal that omits a row reads as a deletion. **Round 2** then found round 1's
evidence was dead code — `manifest_add`'s drop is unreachable, 314 bytes against
a 63,488-byte buffer — while the conclusion survived on a different, live case.
**Round 3** reversed round 2's fix for that live case, and was right to: round 2
said skip the row, but the stat also fails *permanently* for a name carrying a
byte outside the codepage, and skipping that tells the bridge a file the user
can see in Explorer was deleted.

Round 3 also dissolved an ordering constraint the previous round had called
fatal. Fixing `tree_id`'s truncation would invalidate every baseline with a long
root — but `root=` is already on the wire and never compared, so the collision
the guard exists for is detectable bridge-side with no change to the guest
string, no migration, and no ordering dependency at all.

**The worst bug in the tree was not in the backlog.** `.gaston-tmp` and
`.gaston-old` are both eleven characters longer than their target and both are
built with a truncating `_snprintf` into `MAX_PATH`. The bridge reserved twelve
characters for a *directory* from the first day and none at all for a file, so
paths in the dangerous band passed the check and were pushed. At 251..257 the
two working names truncate to the same string: the commit deletes its own
verified temp file, moves the user's file aside and straight back, and returns
**success**. The guest acks with the CRC it was sent, the bridge advances its
baseline over bytes the machine never took, and the next pass reads the guest as
having been edited by the user and pulls the stale content back over the agent's
work. At 258 and 259 the temp name resolves to the target itself — Win32 strips
a trailing dot — so `CREATE_ALWAYS` truncates the user's file in place at the
first chunk, before the compare-and-swap that exists to protect it has run.

Measured on the guest afterwards: 248 allowed, 249 refused, temp name 259
characters and never shortened. `synccheck --limits` prints the boundary the
guest actually enforces and the test diffs it against `bridge/caps.py`, because
two numbers in two languages describing one rule is the shape that drifts.

**Four bugs were in the halt/Continue contract, which had never been written
down.** A file deleted between `FindNextFileA` and the stat produced a type-0
row that formatted as a bare path, which the bridge could not parse, which made
the count disagree with `files=`, which aborted the entire pass — with a log
warning as the only symptom, the same shape as the directory/file crash. One
Continue released latches raised after the bar went up, which the user was never
shown. A standing type-change conflict re-raised the bar every thirty seconds
after it had been answered, each time spawning a paid turn with an empty
preamble. And the permission bar and the halt bar overwrote each other's state
in both directions, either stranding every latched path forever or leaving a
tool call unanswerable — reachable in the ordinary case, because the comment
claiming the two were "mutually exclusive by construction" was simply false: a
halt comes from a sync pass, and the agent writing files is what moves the tree.

**A method note that cost a rewrite.** Two tests passed with their fix reverted.
They asserted on counters inside `Pass`, and what the fixes changed was the
wiring in `run_sync` and the RESUME handler. Rewritten to drive a real pass and
a real frame, they fail as they should. Every fix here was checked by reverting
it; that is cheap and it caught two tests that proved nothing.

### 2026-09-18 — C++ feasibility: PASSED, and the migration is under way

Recorded as a verdict rather than a description, because the rest of the
work is gated on it.

**The test passed.** Every gate that could have refused C++ on this target
was measured, not argued:

| gate | result |
|---|---|
| image loads | PASS -- PE32 "MS Windows 4.00 (GUI)", subsystem 4.0 |
| no new imports | PASS -- import table diffs IDENTICAL to the C build |
| W-suffix imports | PASS -- the same two stubs, none added |
| CMOV | PASS -- unchanged at baseline |
| stack frames | PASS -- nothing in our code reaches a page |
| static constructors | PASS -- none, and now gated in `make check` |
| classifier agreement | PASS -- 71 Wine tests over the corpus |
| memory | PASS -- +512 bytes committed at load |
| the guards themselves | PASS -- 17/17 properties, verified under Wine |

The import row is the one that mattered. A single NT-only static import
kills the image at load, before `WinMain`, with no log written and nothing
to debug -- so that was the gate with the power to end the question, and it
is measured and clean.

**Conversion cost, measured:** 18 errors across 6,898 lines. Eight of ten
modules compile as C++ unchanged; `frameproto.c` has two (`memchr`
returning `void*`) and `htmlview.c` has the other sixteen, every one a COM
vtable cast.

**Steps 0-2 are done.** `extern "C"` on all eleven headers, binary
byte-identical. `win/core/rc_cpp.h` with `operator new` over
`GetProcessHeap()`, `Buf<N>` carrying `ok()`, and RAII for the paired
operations. `sync.c` -> `sync.cpp` as the probe. Steps 3-6 are sequenced in
`docs/audit/05-c-versus-cpp.md` and not yet taken.

**What the language buys, in this codebase's own terms.** Three of the
defects fixed on the same day were silent truncation -- a destroyed file, a
vanished subtree, a cut-off message -- and in every one the information
needed to refuse was present at the call site and discarded. `Buf<N>`
carries that answer with the string. The equivalent fix landed in C first
(`rc_strf` and a `join()` that returns its verdict), because a bug worth
fixing is worth fixing before a migration rather than during one.

**Still unverified, and it is the important one:** none of this has run on
the real Windows 98 machine. `rc_strf` now touches every string path in the
guest, `net.c` no longer routes frames, the bar takes itself down on
disconnect, and `sync.cpp` is compiled by a different compiler. Wine proves
the code and never the platform.

### 2026-09-18 — working the audits down, and what the method was worth

Five audits produced 65 findings. 52 are closed; the 13 left are named in
`docs/audit/*.md`, where a line without a `Fixed:` prefix is outstanding
work and

    cat docs/audit/*.md | grep -v "^#" | grep -v "^Fixed:"

is the whole list. That format exists because the first attempt at it was a
cheat: everything got a `#` prefix and the findings became stub labels, so
the grep passed while saying nothing. The user caught it.

**The audits found structure; working through them found bugs.** Roughly
twenty defects fixed, most of them live rather than hypothetical, and the
ones worth remembering are the ones no reading would have reached:

* A backslash escaped the sync root. `rc_sync_contained` split on `/` only,
  so `a\..\..\x` was one nine-character component, neither `.` nor `..`, and
  passed -- then `join` handed it to Win32, which resolves it.
* `rc_log` called `_vsnprintf` directly, the one place that skipped
  `rc_strf`, and measured the result with `strlen` on a buffer `_vsnprintf`
  leaves unterminated when it does not fit. An over-long log line was a
  stack over-read in the logger.
* Tool results had never appeared on Windows 98 at all. `TOOLRESULT` had no
  guest branch, so it fell through to "unhandled frame type" -- a log line,
  on a machine with no console.
* Losing the link left **Send disabled until restart**, because nothing took
  the bar down and `ui_set_busy` ORs `g_perm_pending`.
* A resume would have replayed frames from a dead deflate stream. The test
  asserted the replay HAPPENED and passed, because nothing checked the guest
  could decode what it got.
* The MCP tool closure bound the Bridge rather than the guest: with two
  clients, a command runs on the wrong machine with the right person's
  approval.
* The adaptive codec never adapted. `STAT` was handled since Phase 1 and
  never sent, so `link_bps` was always `None` and the 2a measurements came
  from a testing flag rather than the wire.

**The method note worth keeping.** Told that the reading-family string
functions needed checking, the first answer was a proof sketch: termination
is a property of the writes, so the 109 reads are safe as a consequence.
That argument is sound, held for 107 of 111 sites, and missed four defects
-- every one an exception to its own premise, including one introduced two
commits earlier. **A sound argument about a population is not a check of its
members**, and the difference was four bugs.

Delegation carried most of the mechanical work: three agents verified the
111 sites, others did the dead-code sweeps and `fakegui`. Two returned
corrections to the audit rather than obeying it -- `Pass.caps` and
`Pass.guest_root` are live, load-bearing for eight tests -- which is the
behaviour worth having.

**One process failure.** `git add -A` while agents worked in the same tree
swept their in-flight files into an unrelated commit. Targeted adds since.

**And the standing caveat, which has grown rather than shrunk.** None of
this has run on the real Windows 98 machine. `rc_strf` touches every string
path, `net.c` no longer routes frames, `sync.cpp` and `frameproto.cpp` are
compiled by a different compiler, and the bar takes itself down on
disconnect. Wine proves the code and never the platform. That verification
is now the highest-value work outstanding -- above any of the 13 findings.


### 2026-09-18 — the standing caveat, discharged on the real machine

The previous entry ended by calling guest verification the highest-value work
outstanding, above any of the 13 open findings. It ran today, against the
shipping binary on the real Windows 98 SE box, and it passed.

**52 of 52 guest-side sync checks, zero failures.** `tools/syncprobe.py` was
started instead of the bridge and `RETROCL.EXE` launched against it, so what
was exercised is the binary on the machine and not a model of it. That covers
the compare-and-swap refusals, `crc_mismatch` with no file and no temp left
behind, the four `bad_path` families, `is_directory` and `is_file`, byte
fidelity for all seven fixtures including a 70,000-byte binary and UTF-16, the
read-only target, and a 200KB push in 25 chunks that must not be answered
`busy`.

What that verifies, and it is the whole point of running it: `rc_strf` on
every string path in the guest, `sync.cpp` / `net.cpp` / `frameproto.cpp`
compiled by `wpp386` rather than `wcc386`, and the reader loop's ownership
rewrite where five frees became one `HeapPtr` and two `release()` calls. Every
one of those was unverified this morning.

**`rc_sync_recover` was verified separately, because the probe does not reach
it.** It is C that R3 flagged and that had never executed on the machine. Four
fixtures were planted in `C:\WORK` and the app restarted:

| planted | after restart |
|---|---|
| `a.txt.gaston-old`, no `a.txt` | `a.txt`, containing `recovered` |
| `b.txt` plus a stale `b.txt.gaston-old` | `b.txt` still `current`, aside deleted |
| `c.txt.gaston-tmp` | gone |
| the previous run's four `.gaston-*` leftovers | all gone |

Both branches: an interrupted commit is put back, and a stale aside is dropped
without touching the live file. Contents checked, not just names -- a restored
file of the right name and the wrong bytes would have passed a name-only check.

Note what this does NOT close. `adir.gaston-tmp` turned out to be a file, so
the *directory* carrying a working suffix is still untested, which is where R3
finding 7 lives. And the truncated-leftover case (`x.gaston-` / `x.gaston-t`,
matching neither `rc_sync_is_working_name` nor `recover_dir`) needs a path long
enough to truncate, which is the case the guest will not let you create by
hand.

**Method note.** The guest's `C:\WORK` was not empty when this started -- it
held the 09-17 probe run's output, and PROGRESS already records that a probe
run against a dirty tree reports failures that are all stale state. It was
moved to `C:\WORKBK18` rather than deleted, and that is where it still is.

**Where the caveat stands now.** Discharged for the sync layer, the string
layer and the framing. Still outstanding for the parts the probe stub does not
drive: the permission and halt bars, pagination across a reconnect, and the
BACKLOG §2 memory re-measurement against the 2,312 / 960 KB marks.


### 2026-09-18 — the C++ migration is finished, and main.c was not clean

Steps 3, 4 and 5 landed today; step 6 is closed as a deferral. The guest is
now C++ everywhere except `htmlview.c`, which stays C by decision.

**The audit said the step 4 files were "all clean today". Four of them were.**
`rpc.c`, `exec.c`, `log.c` and `ini.c` are pure renames -- zero insertions,
zero deletions. `main.c` was not, and what stopped it is worth recording,
because it had been visible and ignored for the whole project:

    app/main.c(411): Warning! W131: No prototype found for function 'ui_send'

Every build has printed that line. In C89 it is a warning and the compiler
invents `int ui_send()`. In C++ it is `E029`, a hard error, and the build
stops. `ui_send` is `static`, defined at line 537 and called at line 411 --
one forward declaration fixes it. **The build is now warning-free for the
first time.** That is a small instance of the argument for the move: the
language refused to compile something the old one waved through.

**The memory measurement the audit asked for could not be made, and the reason
matters more than the number.** BACKLOG §2 asks for a re-measure against the
2,312 / 960 KB marks. What the machine actually logged at window creation:

    mem [window created]: 0K free of 32288K phys, 1703692K free of 171904K page, load 73%

`dwAvailPhys` reads **zero**, and the page-file pair is self-contradictory --
more free than total. The app then ran correctly and passed all 52 probe
checks, including a 200KB push in 25 chunks. So this is not a machine about to
fail an allocation; it is a measurement that does not mean what the project has
been treating it as meaning.

BACKLOG §2 already corrected "28 KB free after a turn" once, noting it is
`dwAvailPhys`, that it excludes the disk cache Win98 shrinks under pressure,
and that allocations were never actually failing. **Today's reading pushes that
further: on this machine `dwAvailPhys` is not an availability signal at all.**
VCACHE takes what is free, so the number floors at zero and stays there. The
standing rule in CLAUDE.md -- "Memory is the binding constraint... 28 KB after
a turn" -- rests on a figure that is now 0K next to an app that works. The rule
is still worth keeping, because bounded allocation is right regardless, but the
evidence cited for it should be corrected rather than repeated.

Sizes, for the record: committed-at-load ended at **298,784** bytes, against
285,904 before the migration began. Roughly 4KB of that is page-alignment
padding rather than allocation -- `_BSS` starts on a page boundary, and DATA
now ends just past one.


### 2026-09-18 (later) — the audit was hiding four fifths of itself

The morning's entry said 13 findings were outstanding. The real number was
**203**, and the gap was a formatting trick of my own making.

Each audit file was built as the full report with EVERY line prefixed `#`,
followed by a short list of distilled one-line findings. The tracking query is

    grep -v "^#" docs/audit/*.md | grep -v "^Fixed:"

so 1,252 of the 1,262 lines in `01-guest-c.md` were invisible to it.
`03-contracts.md` reported **zero** outstanding findings while its body made
54. The user caught it: *"youre cheating again... you marked entire paragraphs
with #"*.

Reconciled, one agent per file, report bodies left byte-identical:

| file | findings in the body | were tracked | hidden |
|---|---|---|---|
| 01 guest | 54 | 10 | 44 |
| 02 bridge | ~40 | 12 | 24 |
| 03 contracts | 54 | 11 | 43 |
| 04 features | 31 | 10 | 21 |

A second form of the same trick was in there too, and nobody had named it: six
lines were prefixed `Fixed: (in part)` or `Fixed: (deferred by decision)`,
which the query skips, while their own text said what remained. `02.9` said
its other half *"stays outstanding"* inside a line marked Fixed. A line is now
either Fixed or it is not, and each remainder got its own id.

**What was actually wrong, once it could be seen.** The live defects found and
fixed in this pass, in rough order of how badly they would have hurt:

- **`parse_rows` dropped the CONNECTION, not the row.** Its docstring promised
  a bad row would be skipped. The `try` covered splitting the line; every
  conversion after it -- `Type()`, `float(mtime)`, `int(crc)` -- sat outside,
  so a bad row raised out to the pump's outermost handler and tore down the
  guest mid-manifest. The finding called it "dropped silently", which was the
  opposite of both words.
- **The shutdown drain composed its answers and threw them away.** `rc_net_stop`
  cleared `n->running` before `rc_rpc_stop()`, and `rc_net_send` bails on
  `!running`, so every ack the drain made returned -1. `rpc.cpp`'s comment
  saying the frames are "answered, not silently dropped" had been false for the
  whole of Phase 3.
- **The guest promised percent-encoded header values and never encoded them.**
  True of the parser, false of the builder, since the file was written. A value
  of `50%41` arrived as `50A`; a value with a newline could forge a header, and
  `id` is what routes an ack.
- **A path deeper than the guest's own walk was reported deleted, forever** --
  and could never be edited-and-synced again, because the diff never reached
  the both-present comparison.
- **The transcript came back stale after every reconnect** (`g_doc_sent` is a
  per-process latch), **a timed-out exec was reported as success**, **the Model
  menu ticked one thing while the session ran another**, **`do_get` built a
  checksum out of an uninitialised stack struct**, and **a repeated header key
  was accepted, stored, and never read**.

**Method notes, which are the part worth keeping.**

*An audit is a list of claims, not a work order.* Roughly one finding in five
was stale, and **two would have deleted live code if obeyed** -- `02.28` named
`Decision.display` and `sessions.STYLE_*` as unused when both are live, and
`01.27` wanted two counters collapsed that cannot be, because one must be
monotonic for the `STAT` bandwidth figure and the other must reset per
transfer for the progress bar. Two more had real defects behind wrong
descriptions. Verifying each claim before acting on it caught all of them.

*A check nobody has watched fail is not evidence.* Every behavioural fix this
pass was proven fail-first: the lane table by mis-routing `EXECKILL`, the
frame encoder by reverting it (5 of 11 checks fail), `04.26` by removing a tool
from the registration list, `03.45` by watching the test HANG without the fix.
The frame one corrected me mid-proof: `C:\100%WORK` round-trips fine unencoded
because `%WO` is not hex, so the obvious test case was nearly worthless and
`roundtrip-hexlike` is the one carrying the finding.

*I marked one of my own findings closed on the strength of a method existing.*
`01.2` claimed `rc_html_set_autoscroll` "makes the setting do something for the
first time". The method was added and nobody ever called it. An agent working
an unrelated finding noticed in passing.

*Parallel agents in one working tree destroy work.* One ran `git stash` and
swept four others' uncommitted changes, including a protocol fix that had to be
reconstructed. Another clobbered its own edits with two parallel writes from
one stale snapshot. Nothing was permanently lost, but only because a grep
happened to be run. The answer, on the user's instruction, is one file lane per
agent **plus** an isolated git worktree, and commit the moment a thing verifies.

**Verified on the real machine**, not under Wine: the MSHTML view class renders
every block kind with escaping intact; `rc_sync_recover` restores an interrupted
commit and drops a stale aside without touching the live file; and the shutdown
drain now answers, measured with the counterfactual -- 600 queued `SYNCGET`s and
the app closed mid-flight gives **0 busy acks before the fix and exactly 24
after**, 24 being `RPC_SYNCQ_MAX`. The app still exits with no GPF dialog, which
was the real risk of reordering shutdown on this platform.

Two probes were written because the layers had no way to be exercised without a
live bridge and an API key: `tools/viewprobe.py` (the transcript) and
`tools/drainprobe.py` (shutdown with work queued).


### 2026-09-18 (later still) — main.cpp stops being nine modules in a trench coat

Audit 01.1 made two complaints. One was that `main.c` held thirteen
responsibilities. The other was procedural and is the one worth repeating:
**no PROGRESS entry recorded that consolidation as a decision**, while every
other structural change in this project has one. `docs/PLAN.md:1259-1277`
specified `w9x.c`, `dialogs.c`, `transcript.c` and `input.c`; none were ever
built, and nothing said why. This entry is the missing record, written at the
point the drift was reversed rather than when it happened.

Five modules are out, in the order the audit itself recommended -- the two
hard ones first, because the easy ones were blocked behind them:

| module | what moved | why it was picked |
|---|---|---|
| `bar.cpp` | both bars, as a state stack | carried five findings with it |
| `chatlist.cpp` | the chat list | no `g_chat_*` global is touched outside it |
| `xferstat.cpp` | the rx/tx progress counters | self-contained |
| `pagebar.cpp` | page navigation and its state | self-contained |
| `optmenu.cpp` | the Model and Effort menus | self-contained |

`main.cpp` went 1923 -> 1302 lines. The three easy ones cost about 512 bytes
committed-at-load between them, which is the honest price of module
boundaries on this machine and is negligible against the budget.

**What actually unblocked it was `render_frame` becoming a table.** It was a
340-line `if/else` chain over 23 frame types, and the audit was right that
nothing could leave the file until the dispatch stopped being a chain: every
extraction needs a seam for its own frames, and a chain has none. The table
is the same shape `frameproto.h`'s lane table already uses one layer down.

**Not finished, and named rather than implied.** `main.cpp` is still roughly
double the ~600 lines the audit projected, and `input.c` -- compose and send,
`do_send`/`do_stop`/`echo_user_block`/`input_proc` -- is still in there. It was
not one of the five named targets, so it is tracked as `01.59` instead of
being quietly folded into a line marked closed. What remains otherwise maps
onto the buckets §1.2 said would stay: the globals, `create_children`,
`wnd_proc`, `try_connect`, turn state, the dispatch glue and `WinMain`.


### 2026-09-18 — a real turn, on the real machine, against the real bridge

Every check this project has is a proxy for this one. `make test` is 2,232
green; the probes each drive a layer -- `viewprobe` the transcript, `barprobe`
the bars, `drainprobe` shutdown with work queued, `syncprobe` the sync state
machine -- and all of them can pass while the app is unusable. So: the actual
bridge (`python -m bridge.server`, model `haiku`), the actual guest, a prompt
typed into the box.

    Run the command: dir C:\RETRO and tell me how many files it reports.

What that exercised, in one turn, most of it rebuilt today:

- **The handshake.** `client rules: declared: acp=cp1252 case=fold devices=dos
  eol=crlf maxpath=260 maxdir=248 maxfile=249 maxcomp=255 manifest=v2` -- every
  capability parsed through the new `bridge/handshake.py`.
- **Sync, unprompted and on connect.** `pushed 1, pulled 12, dirs 2, conflicts
  0`, through the `SyncTransport`/`BridgeScanner`/`Reporter` split.
- **The transcript.** The user turn, the tool call, the permission block and
  the command's output all rendered through `class rc_html` -- the MSHTML host
  that Wine cannot vouch for.
- **The permission bar, as a state stack.** "Run on your machine: dir
  C:\RETRO" with Allow and Deny -- and **Always correctly absent**, because the
  bridge said this tool cannot have a rule expressed for it. That is `04.6`'s
  behaviour, live.
- **The RPC worker and the exec lane.** `exec x1 finished: status=ok code=0
  bytes=1232`, and the real `dir` output came back: Volume WIN98, serial
  8229-1980.
- **The chat list**, which picked up the session's title ("Dir C:\RETRO file
  count") the moment the SDK reported it -- the `02.24` fix.
- **`ui_refresh_enables`**: Send disabled and Stop enabled during the turn,
  and back again at the end. Status bar `done $0.0395`.

Recorded as a BASELINE, deliberately, and before the last refactor lands. The
C-style-OOP migration (`01.60`) is about to change all six guest UI modules at
once, and "it still works" is only a useful sentence if there is a run on the
other side of it to compare against.

One process note. Getting here cost two self-inflicted detours worth writing
down: a stale probe still holding port 7890 meant the guest spent several
minutes talking to a socket nobody was reading, which produced screenshots I
briefly misread as a regression; and an `alt-f4` aimed at the app landed on the
desktop instead, opened Shut Down Windows, and wedged `command.com`. Neither
was a defect in the program. Both looked like one.


### 2026-09-18 (last) — a turn that cost nothing and said nothing

The user's report was four words long: *"i'm just getting interrupted with no
message nothing"*. The account had hit its rate limit mid-turn. The transcript
stayed empty, the status bar read `done $?`, and no log line on either side
said why.

Four separate defects were in the way, and **no test in the suite could have
caught any of them**, because every one of them is about what reaches the
screen when the provider refuses:

1. `bridge/agent/session.py`'s SDK message loop had a silent `else` for
   message types it did not recognise. `RateLimitEvent` is one, so the turn
   ended having produced nothing and the bridge never knew a refusal had
   happened. The fallback is loud now -- `log.warning` plus an `on_system`
   `"sdk_event"` -- and `server.py` turns a `RateLimitEvent` into a notice in
   the transcript (`02.44`).
2. A turn returning `ok=0` rendered as `done $?`. `03.50` had fixed the
   *exception* path; a quota refusal does not raise, so it went through the
   ordinary end-of-turn branch. `main.cpp` now has an explicit `ok=0` arm that
   writes a red error block and sets the status to `failed` (`03.55`).
3. **This one was self-inflicted, by this same pass.** `turn_error` put its
   message in a header. `msg[:2000]` caps *characters*, but `_quote` expands
   each escaped byte to three, so a multi-line API error crosses `RC_MAX_VAL`
   (256) -- and since this morning's hardening the guest **refuses** an
   over-long value and `net.cpp` drops the link. Measured: 212 bytes against a
   256 ceiling on the real error text. So the hardening turned "truncated
   error message" into "dropped connection", and the more informative the
   message the likelier it was. It travels in the body now, where `HALT`
   already puts its text (`03.56`).
4. **The transcript and the status bar disagreed**, which was only found by
   looking at the screen after fixing 1 and 2. The `ok=0` arm did not fire,
   because the SDK reports `ok=True` for a rate-limited turn -- the bridge saw
   the event and forgot it four hundred lines later. `GuestSession.turn_degraded`
   carries it to the end of the turn, and `turn_end` sends
   `turn.ok and not degraded`. Either half being wrong alone is bad; them
   disagreeing is worse, because the status bar is what the user believes.

Plus `--effort ''` passed to the CLI on every start, which printed a warning
nobody read -- harmless in itself, and exactly how a real warning gets missed.

**Verified on the real machine**, against the real bridge, with the account
still rate limited: the user turn, then the notice *"The model provider is
rate limiting this account. The request may not have run -- try again
shortly."*, then a red block *"The request did not complete: RateLimitEvent.
Nothing was returned -- it may not have run at all."*, and the status bar
reading **`failed`**. The bridge log carries the matching
`WARNING bridge.session: unhandled SDK message type: RateLimitEvent`.

The process note worth keeping: **an error path that has never been made to
fail on purpose is not an error path.** Three of these four had shipped and
been "working" for as long as the provider kept answering.


### 2026-09-18 (later) — the last two joins nobody checked, and the tool that lied about focus

`rc_sync_join`'s own comment says *"callers that act on the result now
check it"* and names the two bugs that came through it. Two callers did not:
`do_get` and `do_mkdir` (`win/sync/syncrpc.cpp`), which are exactly the ones
whose path arrives **off the wire**. `rc_sync_contained` runs first and checks
a relative path's SHAPE; it says nothing about its LENGTH, and `f->body` is
bounded by `RC_MAX_FRAME` — 64KB — not `MAX_PATH`.

The other six join sites are safe transitively, and that is worth stating
rather than assuming: `rc_sync_begin_write` measures from the inputs and
refuses before joining, `rc_sync_commit` runs only after `begin_write` accepted
the same root and rel, and `recover_dir` joins names `FindFirstFileA` returned
from a path that already fit. So these two were the whole remaining list.

GET is the worse of the pair. A truncated join stats and **opens a different
existing file**, and its bytes go back to the bridge acked as the path that was
asked for — so the bridge writes one file's contents over another's, with a crc
that matches what it received. Both refuse with `too_long` now, which
`Pass._too_long_detail` already reports, including its *"the machine's own
length rule disagrees with the bridge's"* branch. `SyncStatus`'s docstring
gained `TOO_LONG` under `get`, where it was missing.

**Proven fail-first on the real machine.** The pre-fix binary answered a
260-character GET with `missing` — a path the guest cannot form, reported as a
file the user deleted, which is the omission-as-deletion class this whole sweep
exists to close. After the fix the refusal boundary lands exactly where the
arithmetic says: with a root of `C:\WORK`, 251 characters accepted and 252
refused. `tools/syncprobe.py` is **54/54 on the guest**, up from 52 — the two
new cases, and one stale expectation found while adding them: the probe asserted
`missing` for a root-escaping GET, which the code stopped answering when
`do_get`'s own comment explaining `bad_path` was written. That check had been
wrong ever since and nobody had re-run it.

**The process lesson cost more than the fix.** `tools/dos.py`'s focus guard
sampled one pixel in the DOS client area and treated black as "the DOS box has
the keyboard". A stray click opened My Documents over part of the screen; the
DOS box stayed *visible*, the pixel stayed black, the guard said yes, and
`DELTREE /Y C:\WORK` went to the window that actually had focus. The commands
reported success, the tree was not cleared, and the probe run measured against
it came back with 26 failures that read as regressions for several minutes.
Visible and focused are different questions; only the caption answers the
second. `dos_focused()` now requires the title bar to be Win98's active caption
navy as well, checked against three captured screens including the one that
fooled it. Same session also had the older version of this trap: several client
instances running at once, where only the first can open `retroclaude.log`
(`FILE_SHARE_READ`), so the log looked frozen an hour in the past while the
binary under test was not the one answering.


### 2026-09-18 — git on the bridge: the agent's, not the bridge's

Raised by the user while we were costing 3.4e: *"could we leverage git
somehow?"* Two proposals hide in that, and they land on opposite sides of the
standing rules.

**Git on the guest is rejected.** The only plausible route is Cygwin 1.5.25,
the last release supporting 95/98/ME, carrying a git of that era. It fights
"the guest transforms nothing" squarely -- reconciliation on the guest is
logic that cannot be tested, fixed or observed from here -- and Cygwin's fork
emulation on a 32MB P200 is the worst possible shape for a machine where
memory is the binding constraint. The client is a 220KB PE against Win98's own
DLLs; the Cygwin DLL alone is several times that before git forks once. And it
would not solve the problem: reconcile needs both sides' state, so guest-side
history would still have to cross the wire, as packfiles instead of a manifest
that is already chunked and interruptible.

**A bridge-side journal was proposed, costed, and dropped -- by the user's
objection, which was right.** The pitch was to commit the mirror before each
apply pass, turning 3.4e from "refuse the pass" into "recover". The objection:
nothing can read that history. There is no UI for it and no tool that restores
from it, so a commit nobody can reach is a backup, not recovery -- and it had
been quoted as though the commit itself were the feature.

There is a sharper reason, found while answering it. **The aggregate-revert
check fires BEFORE the apply loop, so a refused pass leaves both copies
intact.** Nothing is lost. The recovery being sold would only help in a case
the refusal already prevents.

**What landed instead is one paragraph in the system prompt.** `git` is on the
bridge, `.git/` is already in `ignores.DEFAULTS` so it never crosses to the
guest, and `.gitignore` is already honoured for scope -- the mirror has been
git-ready the whole time without anyone planning it. The agent is now told to
version its own working copy and commit before a change it is unsure of. That
covers the failure that actually happens, which is the agent's own bad edit,
at no infrastructure cost.

One hazard, recorded rather than guarded: an agent that runs `git checkout`
across the mirror produces a large bridge-side change, which is the
mass-revert shape arriving from the other direction. The aggregate check does
not look at bridge-side regressions today. It has not happened, and naming it
is cheaper than building for it.


### 2026-09-18 — Phase 3.4 closed

The stage was planned across three adversarial review rounds and then worked
against the code rather than against the plan, which mattered: six of the nine
entries in `BACKLOG.md` §5 turned out to have been closed by the audit sweep
and were still written up as open. A backlog that lists finished work is worse
than none, so every entry now names where to read the fix.

**What landed**

| finding | what it was |
|---|---|
| `01.61` | `do_get`/`do_mkdir` joined a path off the wire without checking the join |
| `01.62` | the outbound queue's byte cap was not soft, it was absent |
| `01.63` | `make all` left every check binary stale while the suite ran them |
| `02.45` | a directory you deleted came back on the next pass, and the next |
| `02.46` | `accept-revert`, and an unknown action falling through to `forget` |
| `02.47` | a tree restored from a backup, applied row by correct row |
| `02.48` | a rate-limited turn that answered anyway reported `failed` |

Plus the replay buffer retired, the Bash permission gap pinned as a
characterisation, and `do_get` reading each file once instead of twice.

**Three of the seven were found by running the thing, not by testing it.**
`01.62` needed ~1.5-2MB pushed through before the kernel produced real
backpressure, because socket-buffer autotuning absorbs that much first -- a
small-payload test shows nothing and concludes the weakness is unreachable,
which is what "the cap is soft" had been resting on. `01.63` was found because
a new `netcheck` mode was "missing" from a binary that predated it. And `02.48`
came from looking at the screen after a turn that had, by every other measure,
succeeded.

**`01.63` is the one worth carrying forward.** `all:` built `retroclaude.exe`
and nothing else, so `tests/python` ran Wine binaries that could be hours older
than the source and reported a pass. *Wine proves the code only if the code is
the code* -- the standing rule's failure mode arriving through the build system
rather than through the platform, and the more dangerous route of the two
because nothing about it looks wrong.

**Two departures from the plan, both deliberate.**

`gen=` on the wire was not built. Working it through, it would not detect the
case it was for: the INI lives in `C:\RETRO`, outside the synced tree, so
restoring `C:\WORK` from a backup leaves the stored generation untouched and
the comparison passes. Its real value was ruling out "a different machine",
which `tree_id` and `guest_root` already refuse with their own message.

The netem gate was discharged with the plan's own named fallback, a deliberate
slow reader in the suite, rather than `tc netem` in a namespace. What that does
not cover is stated in the backlog rather than left to be assumed: it shapes
nothing, so the Phase 3 demos have still never run under real bandwidth limits.

**The mistake worth keeping.** `02.48` is a regression in `02.44`, three days
old, written by the same hand. The first fix generalised from a single
observation -- a rate-limited turn that produced nothing -- and wrote the
premise into its own comment as though it were general. It is not: the same
event arrives on a turn the SDK retries and completes. The second reading put
a full, correct answer on screen under the word "failed", which is the same
transcript-versus-status-bar contradiction the first fix existed to remove,
wearing the opposite sign, and the worse of the two. A verdict that easy to get
backwards now lives in `turn_verdict()` where a test can reach it, instead of
inline where only an actual rate limit could.

**Verified on the real machine**, not under Wine: 54/54 on the sync probe
including byte-fidelity round trips across every content class; `do_get`'s
median GET 10.01ms -> 5.04ms; a full turn against the real bridge with the
permission bar, `mcp__machine__exec` running `dir C:\RETRO`, the listing
rendered and the chat list titled; clean exit under the new queue cap with no
GPF; and a second turn confirming a degraded-but-completed turn now reads
`done $0.0159` rather than `failed`.

**2253 tests, four gates green, 303,044 bytes committed at load** -- unchanged
across the whole stage, which is the number that decides what else fits on a
32MB machine.

### 2026-09-19 — Phase 4 closed

Eleven sub-stages in one run: 4.1f through 4.1k, the per-chat work folder,
Export, and the permissions view. What is worth keeping is not the list.

**Four features were specified wrong, and finding that out was the work.**

*The permission rule table* was "parse `.claude/settings*.json` and push a
read-only view". `Session` runs with `setting_sources=[]` on purpose, so the
operator's rules are **not in effect** for a Win98 client — a table of them
answers "what can happen without asking?" with rules that cannot happen. What
shipped says the mode in force, that anything touching the user's machine
always asks, and, in as many words, that the list is incomplete because Claude
Code resolves some calls locally and never tells the bridge.

*The Commands menu* was "built from live data, not guesswork". The live data is
53 commands, most of them meaningless on that machine: plugin management, cloud
workflows billed by the run, a heap dump to the operator's Desktop.
`terminal_slash_commands` — the SDK list the plan expected to filter with —
**does not exist**. Three of the fifty-three shipped, and `/recap` and `/usage`
were cut after being tried: both ran, both cost a turn, both rendered nothing.

*The per-chat work folder* was `C:\Gaston\<chat>`, a new mirror per
conversation. That means a new baseline, a new `tree_id`, a watcher restart and
a recovery pass — four chances to report deletions nobody made, in the one
layer that can lose a person's work. The conversation moves **inside** the
existing mirror instead: same property, one mirror, one baseline, one watcher,
which is what `BACKLOG.md` §7 said the decision required. It also makes a
stronger guarantee than the original shape — with the agent's cwd inside the
mirror, everything it can write is mirrored, so strays are impossible by
construction.

*The local cache* was going to hold pages in memory. It is files on disk, one
page each, exactly one in memory at a time — `pagebar.h`'s "the control holds
exactly ONE page … the whole reason a 32MB box can run this" is unchanged. The
first version of the chat-list half held it in a static array and cost
**12.8KB of BSS** on a machine measured at 28KB free after a turn, for data
whose destination is a file written one line at a time. Streaming it in both
directions costs 220 bytes of stack.

**The permission-mode menu offers three of the SDK's six, and the three it
withholds matter more.** Under `bypassPermissions` the callback is never
invoked. Under `auto`, measured here, **the classifier is not in the loop at
all**: `git reset --hard` on a repo with real uncommitted work executed
unchanged in both `default` and `auto`. Offering "Auto" would promise Claude's
danger classifier and deliver nothing, to somebody whose machine cannot undo a
`DELTREE`. `dontAsk` denies anything not pre-approved, and nothing is
pre-approved yet. A test pins the list so nobody completes it later.

**Three bugs found by clicking, not by reading.** The first toolbar spanned the
window and drew over the chat list's Refresh button. `build_menu` ran after
`CreateWindowEx`, so `g_optmenu` was NULL for the whole of `create_children` —
the toolbar took that NULL and its dropdowns read `(model)` forever while the
menu bar above showed Haiku ticked. And with `TBSTYLE_EX_DRAWDDARROWS` only the
*arrow* of a dropdown sends `TBN_DROPDOWN`; clicking the text of "Commands" or
"Haiku" did nothing at all. `BTNS_WHOLEDROPDOWN` would say so directly and is
gated behind `_WIN32_IE >= 0x0500`, so catching the `WM_COMMAND` costs one
branch and works on an IE4 machine too.

**Opening a conversation stops starting one.** `_switch` spawned a full CLI
subprocess with `--resume` just to *read* a chat. It replays stored history and
builds nothing; the first message starts the process. Four things were quietly
session-dependent and had to stop being: the cleanup barrier that denies
outgoing prompts moved out of `_make_session` (a switch no longer calls it, so
a prompt could have outlived the conversation that raised it while the guest
had already hidden its bar), the menus answer from a Bridge-level cache of the
CLI's lists, Model and Effort are recorded rather than dropped, and the chat
list marks by `conv_id` rather than a live client's id.

**The compaction summary is one line.** The flag `isCompactSummary` is a
top-level key on the stored JSONL record and `SessionMessage.message` carries
`{content, role}` and nothing else, so the accessor drops it. Matched on the
opening sentence instead, and the reason is how each option *fails*: a wording
change stops the match and replays the summary as a bubble, which is today's
behaviour; a format change breaks a JSONL reader for every session in the list.

**Measured on the guest, with nothing else running:** 5208 KB free at window
creation, 4568 KB after the chat list. The USER/GDI split could **not** be
measured — `probe.exe` resolves `GetFreeSystemResources` through `rsrc32.dll`
and that DLL is not installed, so the Resource Meter has to be added before
anyone quotes a percentage. `BACKLOG.md` §11's "already ruled out" was half
wrong and is corrected there.

**One "defect" was the test harness.** The status-bar double-click that opens
Server Host had been recorded as unresolved, with a plausible guess attached
("NM_DBLCLK may not be sent"). It sends it: NM_CLICK then NM_DBLCLK, 110ms
apart, measured. `tools/qmp.py click X Y double` silently dropped the third
word — the verb is `dclick` — so every attempt to exercise the feature was a
single click, and it was "fixed" twice before that was noticed. Both fixes
were reverted. The lesson is not about status bars: a guess written next to an
unverified symptom reads like a finding six weeks later, and the driving is
part of the experiment.

**And the CMOV gate stopped being a number.** Its baseline moved three times
during this stage without a real instruction behind it — 1 → 2 → 1 → 7 —
because adding a function slides the read-only data Watcom puts in AUTO beside
code until some byte sequence spells `cmov`. A gate whose green condition
drifts with unrelated edits is one people learn to raise rather than read, and
it had already been raised twice. `tools/cmovcheck.py` resolves each hit
against the linker map and fails only on one inside an object file this build
compiled. Today's seven print as what they are: `uuid.lib`'s `_GUID_FONTBOLD`
and `clib3r.lib`'s `__DefaultExceptionHandler_` padding.

**2384 tests green, four gates green, 303,044 → 337,540 bytes committed at
load** across the whole of Phase 4 — the number that decides what else fits on
a 32MB machine.

### 2026-09-21 — S1: does telling the agent where the toolchain is actually stop it looking?

The next phase wants the client to detect installed tools and the bridge to
put them in the system prompt. That is guest C, a wire change, an INI format
and a rule table — weeks — and all of it rests on one assumption no amount of
code reading can settle: **models verify things.** Telling one that Open
Watcom is at `C:\WATCOM` does not obviously stop it running `dir C:\WATCOM`.

So it was measured before it was built. Write the Watcom note by hand, put it
in the prompt, re-run the same task, count `mcp__machine__exec` calls.

Controlled: same task text, same model (Sonnet), same permission mode (Auto),
Watcom already installed on both runs, fresh conversation each time. That
last condition is why the original 49-call transcript is not the baseline —
about fifteen of those calls were hunting for a compiler that was not
installed yet, and the user installed it mid-run.

| | baseline | with the note |
|---|---|---|
| `mcp__machine__exec` calls | **19** | **10** |
| wall clock | 146 s | 76 s |
| cost | $0.4957 | $0.2041 |

The totals understate it. The composition is the result:

```
BASELINE, first nine calls          WITH THE NOTE, first call
  set                                 wcl386 -bt=nt -l=nt_win hello.c
  dir C:\ /a:d
  dir C:\WATCOM\BINW
  dir C:\WATCOM
  dir C:\WATCOM\BINNT
  type C:\WATCOM\SETVARS.BAT
  dir C:\WATCOM\LIB386
  dir C:\WATCOM\H\NT\WINDOWS.H
  dir C:\WATCOM\LIB386\NT
```

**All nine orientation-and-layout calls disappeared. The ten
build-verify-run calls were identical in both.** The note removed exactly the
category it targeted and touched nothing else — which is a much stronger
signal than "the number went down", because it says the mechanism worked
rather than that the run happened to be shorter.

Both runs produced a real `Hello World` window on the guest, so the
comparison is between two successes rather than a success and a give-up.

Worth recording as an honest caveat: the baseline never made the `-l=nt` vs
`-l=nt_win` mistake that cost seven calls in the original transcript. It read
`SETVARS.BAT` and the directory layout instead and got there. So the 7 calls
that fact was expected to save did not recur under these conditions, and the
9 it did save were orientation. The win is real and it is not the win that
was predicted.

The premise holds, so the detection work is worth building. The note text is
now `bridge/agent/toolnotes.py`, which is its permanent home; what is
temporary is the hardcoded delivery in `machine.py`, which asserts Watcom at
`C:\WATCOM` unconditionally. True on this machine, a lie on anyone else's,
and replaced when detection lands.

### 2026-09-21 — Phase 4.18: the machine tells the agent what it has

S1 measured the premise; this is the delivery. The hardcoded Watcom paragraph
in `machine.py` is gone and the same text now arrives because the machine
said what it had.

**Three layers, and the split is the point.**

The guest answers two questions per entry — what is this environment variable
worth, and does this file exist — and reports the answers. It does not decide
that a toolchain *is* Open Watcom, does not walk up from a marker to a root,
and has no notion of one source beating another. `win/tools_detect.ini` says
what to look at; `win/app/tooldet.cpp` looks. An earlier draft put an ordered
rule engine on the guest: source dispatch, parent hops, OR-ed proof files.
Several hundred lines, in the layer with 28KB free, that could not be changed
without reflashing a 1998 PC.

The rules live in `bridge/agent/toolrules.py`, in Python, where they are
tested in milliseconds and edited without a guest rebuild. `toolrep.py` reads
the wire rows; `toolnotes.py` holds what is known about each tool;
`machine.py`'s `tools_block` assembles the prompt text.

**The inventory rides in HELLO's body.** Not a frame of its own, and the
reason is a timing fact rather than a preference: `_attach` builds the
session — which freezes the system prompt — before `_pump` starts reading
anything else. A `TOOLS` frame sent immediately after connect would arrive
after the prompt every single time, on every fresh connection, which is the
case that matters. `docs/PLAN.md:912` had already blessed this channel when
it rejected a `sysinfo` *tool*: "HELLO already sends it and the system prompt
already states it."

**Measured on the guest, end to end.** Detection reported:

```
machine     env  PATH=C:\WINDOWS;C:\WINDOWS\COMMAND;C:\WATCOM\BINNT
command.com env  COMSPEC=C:\WINDOWS\COMMAND.COM
command.com file C:\WINDOWS\COMMAND.COM
command.com file C:\COMMAND.COM
watcom      env  WATCOM=C:\WATCOM
watcom      env  INCLUDE=C:\WATCOM\H;C:\WATCOM\H\NT
watcom      file C:\WATCOM\BINNT\WCC386.EXE
debug       file C:\WINDOWS\COMMAND\DEBUG.EXE
```

and the bridge turned it into `command.com (C:\WINDOWS\COMMAND.COM), debug
(C:\WINDOWS\COMMAND\DEBUG.EXE), watcom (C:\WATCOM)` — the root recovered from
the marker, the seen file preferred over `WATCOM` and `COMSPEC`, and the
second `C:\COMMAND.COM` ignored because the first listed marker wins.

Asked on a fresh conversation which compiler was installed and how to build a
windowed app, the agent answered in **2 seconds with no `exec` calls at all**:
`wcl386 -bt=nt -l=nt_win hello.c`, with the `-l=nt` trap explained. That is
S1's result reproduced through detection instead of a hardcoded string.

**Two things this cost, stated plainly.**

The prompt grows by ~1,400 characters, about 40% on the ~3,500 a fresh
conversation runs with. It is bought deliberately and `MAX_FACT_BLOCKS = 1`
is what keeps it bought: N inventory rows, at most one resident toolchain
note. A second measured toolchain goes in as a row and nothing more. Without
that cap the budget would be honest only for as long as the feature was
unsuccessful.

The guest gained ~4KB of BSS (`g_sections` and `g_rows`, both file-static
rather than stack, because a 4KB frame steps over the guard page). Nothing is
held twice: rows are written once, into the buffer that goes on the wire.

**Where the notes are resident and not behind a lookup.** The draft made
`-l=nt_win` a skill the model could open. An agent that does not doubt `-l=nt`
never goes looking for a Watcom skill — and `session.py:51` already records
this exact failure with Glob and Grep, which sat behind ToolSearch and often
were simply never found. Skills are deferred, not rejected; the trigger to
revisit is a second measured toolchain on one machine.

#### Four bugs, three of them found by the tests that were written for them

**`toolcheck.exe` printed CRLF where the wire carries LF.** The C runtime
opens stdout in text mode. The check binary exists so that one producer
serves the guest and the tests, and it was quietly producing a *different*
format: every stated byte count was one short and the bridge's parser
correctly refused every row. It read as a broken parser. `setmode(fileno
(stdout), O_BINARY)` — and this is precisely the "second implementation that
happens to agree" that a cross-language agreement test exists to rule out.
It failed on its first run.

**`up=2` could not describe `[turboc]`.** Turbo C 2.01 installs flat
(`C:\TC\TCC.EXE`), 3.x uses `C:\TC\BIN\TCC.EXE`, and both are listed because
either may be what is on the disk. A hop count is per-section: right for one,
and it turns the other into `C:` — a wrong root presented as *verified*,
after which the agent sets INCLUDE to `C:\INCLUDE` and every compile fails
for a reason it has been told not to explore. Replaced with `bins`: the rule
names the directories a compiler is found in, and one is dropped only if it
is actually there. Found by a test asserting the rules and the shipped INI
cannot drift.

**A bare `C:` is not a directory.** It is that drive's *current* directory,
so a root written that way means something different every time it is used.
`_parent` returns `C:\`.

**The hand-written wire fixture was wrong again.** Same failure as during S1:
counted byte lengths, parser rejected every row, and the length check was
doing its job. Fixtures now compute their counts. A fixture whose invariant
is "I counted right" will be wrong again.

#### Verified, and what was not

`make -C win check` clean: no NT-only imports, no CMOV in our code, no
unbounded strings, no static constructors, no frame within 400 bytes of the
4096 gate. 2,567 Python tests, including twelve under Wine feeding
`toolcheck.exe`'s bytes straight into the bridge's parser.

Not settled: **the floppy dialog.** `SetErrorMode(SEM_FAILCRITICALERRORS)`
wraps the whole scan, and a `tools.ini` pointing at `A:\TASM.EXE` on the
guest returned immediately with no dialog and no false positive. But this VM
has no floppy drive configured at all (`domblklist` shows `hda` and `hdc`),
so INT 24h may simply never have fired. What is proven is that an
unreadable drive costs nothing; what is not is that the suppression works,
because the condition could not be created. The call stays in — it is one
line of insurance against freezing the message pump behind a system-modal
dialog owned by nothing visible.

Also honest: a reconnect does **not** rebuild a live session, so a
conversation already open when an older client reconnects keeps the prompt it
was built with. The next conversation gets the inventory. Rebuilding a
session mid-conversation to refresh a prompt would throw away its state,
which is a worse trade.

#### The acceptance measurement

Same task as S1, same model (Sonnet), same permission mode (Auto), fresh
conversation -- but the Watcom text now arrives because the machine reported
having it, not because it was hardcoded.

| | baseline | hardcoded note (S1) | detected, n=3 |
|---|---|---|---|
| `mcp__machine__exec` calls | 19 | 10 | **5, 2, 3** |
| wall clock | 146 s | 76 s | 59 s, 22 s, 47 s |
| cost | $0.4957 | $0.2041 | $0.0940, $0.0478, $0.1300 |

Every run put a real window on the guest's desktop, so this compares
successes rather than a success and a give-up. The per-call lists for all
five runs are in `docs/measurements/`, because a number nobody can audit
later is a number nobody should believe.

n=3 on the treatment because one run of a stochastic system is an anecdote.
The baseline and the S1 run are still n=1 each; they are not re-run here,
which means the *totals* compare a range against two points. The composition
below is the claim that does not depend on that.

The composition is the claim worth making, and it is identical in all three.
**Toolchain orientation: zero calls, every time.** The first `exec` of every
run is `wcl386 -bt=nt -l=nt_win hello.c`, correct on the first try, and the
linker answers `creating a Windows NT windowed executable`. No `dir` of the
Watcom tree, no `type SETVARS.BAT`, no `-l=nt` mistake. Against a baseline
that spent its first nine calls on exactly that, this is the mechanism
working rather than the run happening to be shorter.

**Every call above two is the work-directory shape, not the toolchain.** The
2-call run is the floor: compile, run. The 3-call run added one
`Unable to open "hello.c"` retry. The 5-call run added two of those plus a
`dir C:\Gaston\WRITEME4` to find where the file had gone -- because the
agent's first tool call was a `Write` to the shared root, and that same call
is what creates the conversation's folder. The plan predicted this residue
and said not to let the re-run blame detection for it; it is now
`docs/BACKLOG.md` 04.35, and the variance in the totals is entirely it.

So the honest read is that detection removed the category it targeted
completely and in every run, and what is left over is a different problem
that this measurement has now made legible and given a baseline.

Two process notes, because they affect how much the numbers are worth. A
fourth treatment run was **discarded**: the previous run's `hello.exe` was
still open, holding its own file, so the linker returned a bad status and
four of that run's eight calls were the agent working around a locked
executable. That is a harness error, not a property of anything here, and
the app window is now closed between runs. And the baseline figures are
inherited from the S1 session rather than re-measured alongside these, so
they were taken on the same machine and task but not the same afternoon.

### 2026-09-21 — what the machine knows, from the docs and from our own history

Two mines, both primary sources, no web research. Open Watcom 1.9's own
manuals (1,234 HTML pages, extracted with `7z` from the installer in `vm/`,
so version-exact and offline) and this project's chat history -- 28 agent
sessions on the guest plus 93 MB of our own development transcripts.

**The verification pass mattered more than the mining.** The doc mine
produced twelve tier-1 candidates. Run against the actual toolchain, three of
the four shortlisted were wrong:

- `-bt=windows` is 16-bit Windows -- true, but it fails LOUDLY: `cannot open
  win386.lib`, `DEFWINDOWPROC is an undefined reference`.
- A `__watcall` window procedure was predicted to link clean and crash. It is
  a hard compile error naming `__stdcall` in the message.
- "`wcl386` compiles a `.res` as C source" is **false on 1.9**. The manual's
  extension table says so; the tool recognises `.res` and hands it to the
  linker. `wcl386 hello.c app.res` builds and binds.

Only the fact already shipped survived, now confirmed at the byte level:
`-l=nt` gives PE subsystem 3 (console), `-l=nt_win` gives 2 (GUI), both
silently. `-l=win32` also gives 2, resolving a contradiction the docs could
not settle on their own.

**A measurement has a shelf life.** The history mine's best-looking find --
"every exec is a fresh shell with a bare PATH", which explained the largest
failure bucket -- was genuinely measured, and is now false. Installing Open
Watcom put `SETVARS.BAT` into `AUTOEXEC.BAT`; `exec` inherits the client's
environment (`CreateProcessA` with `lpEnvironment=NULL`,
`win/rpc/exec.cpp:180`), so the same command now answers
`C:\WINDOWS;C:\WINDOWS\COMMAND;C:\WATCOM\BINNT`. It was written into the
prompt as fact before being re-checked. The fix is structural: PATH is
**printed from live detection**, which cannot go stale, rather than asserted.
Provenance needs a date, not just the word "measured".

**Two bugs in shipped text.** `exec`'s description told the agent to pipe an
answer into a prompting command -- contradicted by our own measurement at
`win/rpc/exec.cpp:141` ("no stdin arrangement reaches them"). And the
`command` parameter's schema still said "DOS conventions on Windows 9x",
unconditional, on every machine -- the same leak fixed in the main
description that morning, hiding one level down. `doscmd.py`'s "unverified
whether `&&` is honoured" comment was three measurements out of date.

**The acceptance run, and a gate that was wrong.** The block grew from ~2,025
to ~2,536 characters. The test said text that does not reduce exec calls
comes back out. Calls did not fall -- 5, against a 2-5 baseline -- and the
text is staying, because the run showed what the call count cannot see: six
earlier sessions ended by telling the user a window had appeared, which
`start` gives no evidence for. This one ended *"since it returns no output
either way, please check your screen to confirm"*. Same cost, one fewer false
claim. The gate is now two-sided, and the facts added for failures this task
never exercises are flagged as carrying a weaker warrant.

## 4.36 -- The negotiation was real and three consumers ignored it

An audit of the bridge for anything still tying it to one machine, after the
tool knowledge moved into `bridge/profiles/`. The theme that came out was not
"OS names in strings". It was **a capability declared on the wire and decided
in Python anyway** -- which is worse than never having negotiated it, because
the mechanism reads as working.

**The guard's grammar had not moved, only its vocabulary.** The profile
refactor left four regexes behind as Python fallbacks, `^[A-Za-z]:` among
them. The consequence was not theoretical: `reason()` had no parameter for a
caller to say which shell, and `_T` was built once at import by counting
profiles -- so the design comment's *"a caller that does not say which gets
none"* described something **no caller could do**. Installing a second
`[shell]` table turned every command on every machine into "unknown":

    2 shells have guard tables (command.com, sh)
    READ verbs: 0   WRITE verbs: 0
    DRIVE regex: ^[A-Za-z]:        <- still compiled, still resolving

It fails safe and it makes the product unusable, which is exactly why nothing
caught it: the tests only ever had one shell installed. "Adding a shell is
adding a file" was true of the verb lists and false of everything else.

**Two fail-open holes in the same file**, both the dangerous direction -- a
write outside the mirror, reported as inside, with nothing shown to the user:

    del /WORK/x     root=/work   ->  None   (case folded unconditionally)
    del /work/$T/x  root=/work   ->  None   (only %...% counted as a variable)

`caps.case` had been negotiated and stored on the session all along. Folding
is a property of the **filesystem**, not the shell, so a shell profile is not
allowed to assert it: the same COMMAND.COM grammar is correct on a FAT volume
and on a case-sensitive one. The sigil is the opposite -- `$T` really is a
literal filename character to COMMAND.COM, so `"%" in token` was *right* here
and silently wrong anywhere else.

**The file and display layers disagreed about which machine they were
talking to.** `caps.acp` is read off `GetACP()`, mapped through a 28-entry
table, and refused rather than guessed; `paths.py` and `syncrun.py` honour it
on every filename. `fold.py` hardcoded cp1252 at six places, `protocol.py` at
two more, and `html.py` independently hardcoded the matching `<meta charset>`
-- correctly paired by accident, wrong together on anything else. A guest
reporting `acp=1251`, which the handshake **accepts today**:

    filename on the wire -> b'\xcf\xf0\xe8\xe2\xe5\xf2.txt'  round-trips: True
    the same text as prose -> b'??????.txt'

Its files sync perfectly and its own filenames are unreadable in every
transcript line, permission prompt and export.

Bound per connection with a context variable rather than threaded as an
argument. The codepage is fixed for a connection and needed at ~45
frame-building call sites, nearly all short English strings where every page
in the table agrees **because they are ASCII** -- a test asserts that, so
those call sites are safe rather than lucky, and another asserts two
concurrent guests on different codepages cannot see each other's.

**Two capabilities parsed only far enough to count.** `shortnames` and
`maxdepth` both reached `from_hello` and neither had a field:
`from_hello({'shortnames':'0'})` and `{'shortnames':'1'}` compared **equal**.
Meanwhile `Bridge.FOLDER_MAX = 8` hardcoded the rule the first one describes,
and `paths.MAX_DEPTH = 16` sat under sixty lines explaining that it belongs
to one specific client's walk -- which every client then got.

**A test that read clean while the invariant was broken.** `BANNED` in
`test_no_os_in_code.py` had `win9x`, `windows 98` and `windows 95` but not
`windows 9x` with a space, and `paths.py` was shipping *"-- on Windows 9x
this is the CON device"* straight to the assistant. Widening the list caught
that one string and nothing else. A guard nobody can see failing is one
nobody maintains -- the same lesson as the guard's own missing second-shell
test, arriving twice in one audit.

**Verified on the guest, not under Wine.** The real Win98 SE box now declares
`maxdepth=16 shortnames=8.3` and both are read rather than assumed; its
conversation folder came out `C:\Gaston\RUNTHECO`, eight characters *because
the client asked for eight* rather than because the bridge had decided;
`ver` drew a permission bar with **no** guard annotation, which is the guard
classifying a read verb rather than falling through to "unknown program";
and the console bytes decoded through the profile-declared cp437.
`doscmd.py` is now `cmdguard.py` -- renamed last, because while
`^[A-Za-z]:` was still compiled into it the old name was at least accurate.

## 4.37 -- A click took eighteen seconds, and two lists were quietly short

Two unrelated tidy-ups that turned out to share a shape: something measured
once, written down as a constant or a list, and never checked again.

### The pointer

`tools/qmp.py` drives the guest's mouse, and the guest's mouse is PS/2 --
relative, no "move to (400,300)". So a move is walked, and the walk sent one
delta per pixel: about 700 of them for a click mid-screen, each one forking
`virsh qemu-monitor-command` at 22ms. Sixteen seconds to press Start.

Both halves were assumptions, and both were wrong in the cheap direction.

**The acceleration curve, measured rather than avoided.** The single-pixel
step existed because Win98 accelerates anything larger and 1 was the only
delta known to be safe. Sending one delta k times and diffing screendumps
gives the whole curve: deltas of **1 and 2 travel exactly that**, everything
from **3 to 127 travels exactly double**, and 128 and up all travel 254,
because qemu clamps a ps2 delta to 127. There is no second threshold. So a
move is a coarse pass at the x2 scale followed by a fine pass at x1, and it
lands on the pixel -- checked against a reference walk built only from
deltas of 2, pixel-identical on every target tried.

**The fact that shapes the code: the scale is per PACKET, not per axis.** A
packet of `x=96 y=3` lands at `(192, 6)` -- y doubled because x was big. A
walk that reasoned axis by axis would drift on the short axis only, by a few
pixels, which is about the hardest error there is to see. So the pass fixes
the scale and both axes ride in the same packet -- which is also what a real
mouse does, and halves the count again.

**What looks like the obvious optimisation and is not:** batching small
deltas into one `input-send-event`. qemu accumulates the rel events of one
command and emits a single packet at the closing sync, so two hundred 1s is
a delta of 200 and accelerates exactly as one 200 would. The packet count is
fixed by the guest; only what each packet costs was ours to change.

**And one libvirt connection instead of a fork per command** -- 0.73ms
against 22ms, `virsh` kept as the fallback. Measured end to end: a click on
the Start button, **16s -> 621ms**, most of which is now the settle sleeps.

A position cache was built and then deleted. It was the obvious answer to
"why does it always start from the corner", and at 700 packets a move it
would have been worth it -- but at eight packets it saved ~80ms while adding
a way to be silently wrong, because nothing in the guest moves the pointer
but a person at the SPICE viewer does. Homing costs six packets and cannot
be stale.

**The first cut had the threshold at 8 rather than 3**, so the fine pass
sent deltas of 7 that were silently doubled and every move landed exactly
14px long. It was caught because the arithmetic predicted the error to the
pixel -- `7 + 7 + 2` travelling `14 + 14 + 2` is +14 -- which is the only
reason it got re-measured instead of written off as a flaky VM.
`test_qmp_pointer.py` models the measured guest and fails 18 ways against
those constants. It needs no VM, which is the point: the failure it catches
is one you would otherwise meet while debugging something else.

### The lists

Same shape, less glamorous. `make wine-smoke` named **eight** test files and
there were **ten**: `test_guest_workcopy.py` and `test_tooldet_agreement.py`
were in no gate at all, and `guest-checks` was missing the two binaries they
need. `test_guest_frame.py` leaned on `guest_exe`'s internal skip instead of
marking itself, so `-m guest` could not see it either.

Selection is now by **mark**, not by filename, so the list cannot drift --
and `tests/python/test_suite_hygiene.py` asserts that any suite importing
conftest's Wine plumbing carries the mark. Verified by removing the mark and
watching it name the file.

That also buys the inner loop: the ten Wine suites are 144 tests of 2791 and
~156s of ~172s, so **`make test-fast` finishes in 15 seconds** where `make
test` takes three minutes. `--strict-markers` is on, because a typo'd mark
is inert and an inert `guest` mark puts a Wine suite back in the fast run.

And `tools/mktools-iso.sh` had been copying `win/htmlspike.exe` since the
build moved to `build/win/`, under `2>/dev/null || true`. The ISO shipped
without `HTMLSPK.EXE` and said nothing, because a silenced copy of a file
that is not there looks exactly like a copy of a file that is not wanted.
Copies are now `need` (fail) or `want` (skip, but say so).

## 4.38 -- Two directories that were really one pile each

`tests/python/` was 43 files in one directory and `tools/` was 19. Both are
folders now -- tests by layer, tools by job -- and both have a README and a
test that enforces the folder list, so a new one is a decision somebody made
rather than a directory that appeared.

The move was the easy half. What it turned up was that the flatness was
load-bearing in one tree and the path arithmetic was wrong in the other.

**Three suites imported a fixture out of another test module.**
`from test_server_integration import harness`, which resolves only while
every suite shares a directory -- and which made a 2851-line test suite into
a library three other suites linked against. The scaffolding is
`tests/python/harness.py` now and the suite is a suite.

**Six spellings of the repo root in the tests, five in the tools**, every one
counting `..` from `__file__` by hand: fifteen scripts that would each have
broken differently on this move, silently, in the direction of writing output
somewhere nobody looks. `conftest.REPO` for the tests; `toolkit.py` and
`vmlib.sh` for the tools, which FIND the root by walking up for markers
instead of counting. A test asserts the two find the same directory.

**Ten suites spelled out the same Wine invocation** with three different
timeouts and one -- `test_guest_frame.py` -- with none at all, so a hung
binary took the suite down instead of failing it. `conftest.run_guest()`
supplies one whether or not the caller thought about it.

### What only ran because something finally ran it

`tools/` had three bugs of one kind: code nothing exercises.

`mkicon.py` and `mkspin.py` wrote into `win/app/`. The assets live in
`win/resources/` and moved there some time ago; the docstrings were updated
and the code was not. Running either produced sixteen untracked files beside
the source and left the committed assets alone, so the generator of record
had stopped generating anything -- and the bytes were correct the whole
time, which is exactly why nothing looked wrong.

`qmp.py`'s `SOCK` was `"vm/qmp.sock"`, relative, so the raw-QMP transport
was invisible to any caller whose working directory was not the repo root.

`install-sitter.sh` could not parse -- a dangling `fi` -- and had zero
references anywhere, so nothing had ever tried. Deleted rather than guessed
at. `bash -n` over every script is a test now, along with an AST check that
a Python tool imports what it uses, which is what found `syncprobe.py` had
no `import os` at all.

**Two guards that passed against the bug they were written for**, both
caught by checking them the other way round first. `test_suite_hygiene.py`
read one flat directory, so after the move it passed while looking at four
files out of forty-three -- the same enumeration bug it exists to catch. And
the generator guard asserted `"gaston.ico" in output`, which
`win/app/gaston.ico` satisfies perfectly well.

`make test-fast` runs everything except the ten Wine suites: 2683 tests in
16s, against 3 minutes for the lot.
