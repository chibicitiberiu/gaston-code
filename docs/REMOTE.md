# Remote control of the guest: `gremote.exe` and the static libraries

**Status:** planned 2026-09-25, revised the same day after an architecture
review and a fact-check against the code. Referenced from `PLAN.md` as R9.

## Why

The real-hardware gate (`BACKLOG.md` 1b) needs a way to drive the P200 MMX box
without a person at it. On QEMU we drive the VM out of band through QMP
(`tools/vm/qmp.py`); a physical PC has no QMP. We want something that feels like
ssh (run a command, get its output, move files, see and kill processes) plus UI
control (screenshot, window and control tree, click, type), scripted from
Python on Linux.

We evaluated and rejected:

- **VNC** (TightVNC 1.3.10, UltraVNC 1.0.2, RealVNC 3.3.7, the last 9x builds).
  Screen and input only, no exec, flaky file transfer on 98, and a polling
  server competing for the 32MB we are trying to measure.
- **Telnet/rexec daemons** (PC Telnetd, GoodTech, REXECD/95). command.com only,
  no UI, and `win/exec/execjob.cpp` (once `win/rpc/exec.cpp`) already solves output capture better.
- **retro-agent as-is.** Covers most of the surface, but its protocol and code
  are not ours; we already took ideas from it (`FORKED.md`), and its `input.c`
  is still the planned source for input code.
- **PiKVM.** The only option with zero guest footprint, and the only one that
  can see a blue screen or power-cycle. Kept as a possible later addition for
  crash recovery, not a replacement: it cannot exec.

The decision is our own server exe, built from static libraries that the client
also links. That gets us a test tool that works out of band from the agent
client, the Phase 6 UI primitives proven on real hardware before they become
agent tools, and a pattern for untangling `win/app/main.cpp` later.

## Shape

```
  Linux                                   Win98 (server this time)
  ------------------------------          -----------------------------------
  test script / REPL                      gremote.exe  (win/remote/)
      |                                       composition root + handlers
  gremote  (Python package)                     |       |      |         |
      blocking socket client  <--- TCP --->  libwire libexec libfs libautomation
      frame codec (shared                     \_____|_______|_____|____________/
      with bridge/wire)                                    libcore
```

`gremote.exe` listens and the Linux side connects: the reverse of the client.
One live client at a time. A 30-second heartbeat bounds how long a dead client
can hold the box (see Protocol).

### The libraries

| Lib | Dir | Contents | Depends on |
|---|---|---|---|
| `libcore` | `win/core/` | `rc_cpp.h` (Buf, RAII guards), log, str, path, **new** generic INI reader, **new** `App` / `Service` lifecycle, thread helper over `_beginthreadex` | Win32 only |
| `libwire` | `win/wire/` | frame codec (split out of `frameproto`), deflate, `Connection` (socket, reader/sender threads, bounded out queue), `Listener`, lane dispatcher. The op-to-lane table is supplied by the exe | libcore, miniz |
| `libexec` | `win/exec/` | `ExecJob` (command execution, from `exec.cpp`, per-job), `Processes` (list/kill/launch via Toolhelp32, resolved with `GetProcAddress`: present on 98, absent on NT4), `readSystemInfo`, `requestReboot`. `docs/libexec-plan.md` | libcore |
| `libfs` | `win/fs/` | list/stat/read/write/remove/mkdir/move, chunked transfer (the sink extracted from `export.cpp`, per `BACKLOG.md` 6), drives and free space | libcore |
| `libautomation` | `win/automation/` | screenshot, window enumeration, control tree, MSAA metadata, mouse and keyboard input | libcore |

`libautomation` is deliberately **not** called `libui` (renamed 2026-09-25). It
drives *other* programs' windows -- screenshot, window list, control tree, MSAA,
synthetic input -- and has nothing to do with the chat client's own interface,
which stays in `win/app/` and is not a library. `libui` read as "the client's UI"
to everyone who met it cold, and it is also the name of an unrelated third-party
toolkit already listed in `FEASIBILITY.md`'s survey. The directory follows the
library name, so it is `win/automation/`.

Rules for the libraries:

- **No lib knows the wire except libwire, and libwire knows no ops.** Today
  `frameproto.cpp:42-46` carries the client's lane table (`EXEC`, `EXECKILL`,
  `ATTGET`, `SYNC*`); that table stays with the client and each exe passes its
  own to the dispatcher. libexec, libfs and libautomation expose plain C++ APIs with
  sinks. Frames to calls and results to frames is the exe's job, which is what
  lets the client gate `exec` behind the permission bar while the server gates
  everything behind the secret.
- **No lib creates windows or owns a message loop.** Where a thread needs a
  message queue (UI input, MSAA), the lib documents it and the exe provides the
  thread.
- **The guest transforms nothing** still holds. Screenshots leave as native-bpp
  DIB rows plus palette, trees and listings as flat `key=value` records. PNG,
  JSON and tables happen in Python.
- **Nothing NT-only, ever**, including in libs that only `gremote.exe` links
  today. Toolhelp32, `oleacc.dll`, `GetDiskFreeSpaceExA`,
  `RegisterServiceProcess` are all `GetProcAddress`. Input stays on
  `keybd_event`/`mouse_event`: `SendInput` exists on 98 but not on 95 or
  NT4 before SP3.
- Each lib has a check exe under `win/checks/` that runs under Wine
  (`make wine-smoke`), in the style of `rpccheck`/`netcheck`.

## C++ structure

The dialect stays what `rc_cpp.h` says: C++98 as Open Watcom implements it, no
exceptions, no RTTI, no STL, trivial templates only, **no file-scope objects
with non-trivial constructors**.

### Naming and layout

The client is not consistent, so new code does not copy it. New and moved C++
code uses:

- **PascalCase** for classes, structs, enums and typedefs: `App`, `Service`,
  `Connection`, `ServerConfig`. No `rc_` prefix on C++ types; `rc_cpp.h`
  (`Buf`, `ScopedLock`, `Unique`) already works this way. Checked against the
  Open Watcom SDK headers: none of the planned names collide.
- **camelCase** for methods, functions, locals and parameters: `initService`,
  `exeDir`. Members keep the `m_` prefix: `m_listener`.
- **UPPER_SNAKE** for macros and constants, as today.
- **Allman braces**, 4-space indent, no tabs.
- One class per header/source pair where the class is public. File names stay
  short and lowercase (`connection.cpp`). Includes are always qualified by
  directory (`"wire/conn.h"`), the rule `rpc/rpc.h` taught us.
- C translation units (`str.c`, `path.c`) and their `extern "C"` APIs keep
  `rc_snake_case`: that is C's only namespace.
- Existing client code is renamed when it moves into a lib or is refactored,
  not in a sweep. A mass rename would bury every real change in the diff.

### Services and lifecycle

*Revised 2026-09-25: App was first a registry (`addService`, a fixed array,
message filters). It is now a lifecycle a derived class fills, which reads
better for a dozen services.*

```cpp
/* core/app.h */
class Service                  /* the shape every service shares */
{
public:
    virtual const char *name() const = 0;
    virtual bool init() = 0;              /* acquire; false aborts startup */
    virtual bool start() { return true; } /* threads, listening */
    virtual void signalStop() {}          /* never blocks; idempotent */
    virtual bool join(DWORD timeoutMs);   /* false: threads still running */
};

class App
{
public:
    int run(const char *cmdLine);         /* the fixed sequence below */
    void quit(int exitCode);              /* any thread */
    HWND window() const;

protected:
    virtual bool initialize();            /* log, single instance, config */
    virtual bool parseArgs(const char *cmdLine);
    virtual bool initializeServices() = 0;
    virtual bool startServices();
    virtual bool preTranslate(MSG &msg);  /* accelerators, IsDialogMessage */
    virtual int main();                   /* the message loop */
    virtual void stopServices();
    virtual void cleanup();
    bool exitWith(int exitCode);          /* stop early, not as a failure */
};

class ClientApp : public App              /* app/main.cpp */
{
    ClientConfig m_config;                /* what services are handed */
    UniqueObj<OleService> m_ole;          /* services, in dependency order */
    UniqueObj<MainWindow> m_window;
    UniqueObj<ConnectionService> m_connection;
    ...
};
```

- **The derived app is the composition root.** It holds its services as
  `UniqueObj<>` members and creates them in `initializeServices()`, in
  dependency order, handing each its dependencies through its constructor.
  By then the config is loaded, so they are real values, not references to
  be filled in later. `stopServices()` and `cleanup()` take them down by
  hand, in reverse. No registry and no runtime lookup.
- **Two-phase construction.** Constructors only store dependencies and cannot
  fail. Anything that can fail happens in `init()`/`start()`.
  `initService(slot, new X(...))` creates, inits and logs a failure by name.
- **Shutdown is two passes**: `signalStop()` on every service, then `join()`
  on each, so threads wind down in parallel. `joinOrLeak()` releases a service
  whose threads did not stop instead of letting `cleanup()` delete it under
  them: the rule `net.cpp:1085-1200` arrived at the hard way.
- **A failed step still shuts down.** `main()` is skipped, `stopServices()`
  and `cleanup()` run, and both cope with a partial start: an empty
  `UniqueObj`, a service whose `init()` failed.
- **`App` owns a hidden top-level window** (a `Window`, `core/window.h`), not
  a bare thread queue: `PostThreadMessage` to a thread without a queue fails
  silently on 9x, and the window receives `WM_ENDSESSION`, on which App stops
  the services and cleans up before Windows ends the process.
- **The app object lives on the heap**, made by `WinMain`: a file-scope object
  would construct before `WinMain` and the log, and a local would put every
  member in one stack frame.
- **Interfaces only at real seams**: the frame sink, the socket (so libwire
  tests run without Winsock), and the op handler. Everything else is concrete.
- **Callbacks take a context** (`sink(void *ctx, ...)`) or are
  one-method interfaces. No context-free function pointers, the pattern in
  `app/reconnect.h` that forces globals.
- **No per-module globals in the libs.** State lives in objects the exe owns.
  `exec.cpp`'s `g_cancel`/`g_child` became fields of an `ExecJob` (done).
- Non-copyable by private undefined copy constructor and assignment.

### Configuration

`gremote.ini` sits next to the exe (8.3 names: `GREMOTE.EXE`, `GREMOTE.INI`).

```ini
; Comments go on their own line: GetPrivateProfileString does not strip an
; inline "; ..." and IniFile rejects the value it leaves behind.
[server]
bind=0.0.0.0
; 7890/7897 are the client, 7900+ the test harness
port=7401
secret=change-me
; peers outside this are closed before AUTH
allow=192.168.1.0/24
; no frame from the client for this long drops it
heartbeat_timeout_s=30

[exec]
max_timeout_ms=120000
max_output_kb=1024

[ui]
; 0 skips oleacc.dll even if present
msaa=1
max_tree_nodes=2000
call_timeout_ms=2000

[log]
file=gremote.log
level=info
```

- `libcore` has `IniFile` (`core/inifile.h`): typed getters with defaults and
  bounds, strict whole-number parsing, `checkKeys()` so a typo like `prot=`
  is an error rather than a silent default, and refusal of a bare filename
  (the profile API would read it from `C:\WINDOWS`). The client's
  `core/ini.cpp` (`rc_config`) stays until the client's own refactor.
- **The composition root loads the INI once into per-service config structs**
  (`ServerConfig`, `ExecConfig`, `UiConfig`) and hands each service its struct.
  Services never read the INI.
- **Invalid values refuse startup**, the same rule as the handshake: never
  guess. `secret=change-me` counts as invalid. The refusal is logged, and shown
  in a message box only when started interactively: a modal box on an
  unattended box blocks forever.

## Protocol

Same frame grammar as the client (`uint32le len | TYPE\n k=v\n ... \n\n body`,
64KB max frame, percent-encoded values), so the codec is shared on both sides.
Separate op namespace and its own version number: this is not the bridge
protocol and must not drift into it.

### Connection and handshake

- **The heartbeat is the live Connection's reader; the listener has its own
  loop.** *Changed 2026-09-25 (step 2) from one reader `select()`ing on both.*
  `Connection` stays generic -- it is also the client's link to the bridge --
  so its reader `select()`s on its own socket only, with the idle timeout
  (`Connection::setIdleTimeout`). The listener is served by gremote's accept
  loop on its own thread: `Listener::accept(out, timeoutMs)` is a timed
  `select()` + `accept()`, so the loop polls its stop flag between waits and a
  second connection is always seen. Winsock 1.1 throughout; the cost is one
  more thread on the guest (to be measured with gremote, step 4).
- **Heartbeat: 30 seconds.** Any frame from the client resets the timer; with
  nothing else to send, the client sends `PING`. If no frame arrives within
  `heartbeat_timeout_s` (default 30), the server drops the connection
  (`shutdown(SD_SEND)`, then `closesocket`), cancels its in-flight requests, and
  goes back to accepting. A killed Python process or a pulled cable never sends
  FIN, and TCP keepalive defaults to about two hours; the heartbeat turns that
  lockout into at most 30 seconds.
- **The heartbeat runs on the reader thread**, from the same `select()`
  timeout. It never depends on a lane being free, so a long `EXEC` or a hung
  `TREE` cannot starve it and cannot keep a dead client alive either.
- **One client at a time.** While a client is live, a new connection is
  accepted, answered `ERR code=busy`, and closed gracefully. It never disturbs
  the live one. Once the live client is dropped, the next connection is served.
  A client that reconnects after its own crash may wait up to 30 seconds.
- **Handshake.** Client sends `AUTH ver=1 secret=...`. Server replies `HELLO`
  with `ver`, `os`, `cpu`, `mem_kb`, `mem_free_kb`, `acp`, `oemcp` (exec output is
  OEM, filenames and window text are ANSI; Python decodes, the guest does not),
  and one header per capability (`msaa`, `toolhelp`, `deflate`), each a small
  integer enum as the client does. Screen size and bpp are cached at startup,
  never queried by the reader (see lanes). Wrong secret or version: `ERR`, a 1s
  delay, then `shutdown(SD_SEND)` before `closesocket` (an abrupt RST is
  reported to crash Win98's Winsock).
- **Auth is a plain shared secret plus the `allow` range**, on an isolated LAN,
  matching the client. Challenge-response needs a hash the guest does not have;
  revisit only if the box ever leaves the LAN.
- **Never `SO_REUSEADDR`.** On Windows it lets a second copy share the port and
  steal connections. If `bind` fails with `WSAEADDRINUSE`, retry for up to 240s
  (possible TIME_WAIT after a restart) before refusing to start.

### Requests, lanes and cancellation

- **Requests carry `id`.** Responses are `OK id=` or `ERR id= code= msg=`.
  Streaming ops send `DATA id=` chunks then `END id=` with a summary.
- **Every request is tagged with the connection generation.** Output from an
  older generation is dropped, so a job still finishing for a dead client never
  lands in the new client's stream (Python restarts ids at 1).
- **`CANCEL id=` follows `BACKLOG.md` 6** (9P `Tflush`): its ack means the
  request will never be answered, and the id cannot be reused before the ack.
  It compares the id under the lock, fixing the id-blind EXECKILL (`BACKLOG.md` 9).
- **Lanes.** One worker per lane, one request in flight per lane, `ERR busy
  op= age_ms=` otherwise:

| Lane | Thread | Ops | May block on |
|---|---|---|---|
| control | reader, inline | `AUTH`, `PING`, `CANCEL`, `SYSINFO`, `PS`, `KILL`, `REBOOT` | nothing: kernel32 and Winsock only |
| job | job worker | `EXEC`, `LAUNCH` | the child |
| fs | fs worker | `LS`, `STAT`, `GET`, `PUT`, `RM`, `MKDIR`, `MV`, `DRIVES` | the disk |
| ui | ui worker (message queue, `CoInitialize`d) | `SHOT`, `WINDOWS`, `TREE`, `CLICK`, `KEYS`, `TYPE`, `FOCUS`, `CLOSEWIN` | USER/GDI, other apps |

- **The control lane never touches USER or GDI.** On 9x a hung 16-bit app
  holding Win16Mutex blocks USER/GDI calls from every 32-bit thread; the reader
  must stay responsive through that. `PS`/`KILL` are here, not on the job lane,
  because behind a stuck `EXEC` is exactly when you need to kill something.
- **The reader never waits on the out queue.** Control replies have a reserved
  slot. Bulk `DATA` blocks only the worker that produced it, and a sink whose
  connection generation has died fails fast so the job cancels itself instead
  of hanging in `ExecJob::run` while the sink blocks: an `OutputSink` that
  returns false ends the job with `EXEC_SINK_GONE` (libexec, step 3).
- **The sender `select()`s for writability with a timeout** before `send`.
  `SO_SNDTIMEO` is Winsock 2 and not trusted through wsock32 on 98.
- **Multi-frame requests** (`PUT`): the first frame opens the request; the
  following `DATA id=` frames route to it by id, not through the lane's busy
  check; `END id= crc=` closes it.

### Op table

Written per op before its step starts. Step 4 needs the control and job lanes
settled; fs and ui tables are written at the start of steps 5 and 6.

| Op | Request headers / body | Reply | Errors |
|---|---|---|---|
| `AUTH` | `ver`, `secret` | `HELLO` (above) | `bad_secret`, `bad_version`, `not_allowed`, `busy` (another client is live) |
| `PING` | none | `OK`; also serves as the heartbeat | none |
| `CANCEL` | `id` of target | `OK id=` (target is dead) | `no_such_id` |
| `SYSINFO` | none | `OK` with `mem_free_kb`, `uptime_ms`. No USER/GDI free-resource numbers: `GetFreeSystemResources` is 16-bit only (`PLAN.md` Toolchain facts) | none |
| `PS` | none | `DATA` records `pid` (unsigned: 9x pids are obfuscated pointers with the high bit set), `ppid`, `threads`, `exe`; `END count=` | `no_toolhelp` |
| `KILL` | `pid`, optional `force=1` | `OK` | `pending` (TerminateProcess said yes and the process is still there; on 98 that is not the same thing), `denied` (deny list below), `no_such_pid`, `failed` |
| `REBOOT` | `confirm=yes`, optional `mode=logoff\|reboot\|poweroff` | `OK`, then `ExitWindowsEx` | `unconfirmed` |
| `EXEC` | `timeout_ms`, `cwd`, optional `shell`; body is the command line | `DATA` output chunks (OEM bytes, untouched), `END exit=, truncated=, timed_out=` | `busy`, `spawn_failed` |
| `LAUNCH` | `cwd`, `show`; body is the command line | `OK pid=` | `spawn_failed` |

The ui lane, **as built in the client** (`win/ui/`). These are the frame
types the shipping client answers today; `gremote` will reuse the same
handlers behind the shorter names in the lane table above.

| Op | Request headers / body | Reply | Errors |
|---|---|---|---|
| `UITREE` | `scope=desktop\|foreground\|pid`, `pid`, `max_nodes`, `text`, `msaa`, `hidden`, `call_ms`, `timeout_ms`, optional `wait_for=` + `wait_ms`, optional `settle_ms` | `UIENT` records (see below) with `n=` per frame; `UIEND nodes=, walk=complete\|deadline\|maxnodes\|stopped, enriched=, dropped=, too_deep=` | `bad_selector`, `not_found` (a `wait_for` that never came true), `no_memory`, `busy` |
| `UICLICK` | `target=` selector, optional `double=1`, optional `settle=` | `UIEND` | `no_target`, `bad_selector`, `ambiguous` (with `detail=` carrying the **count**), `not_found`, `truncated`, `refused_self`, `no_window`, `not_foreground`, `occluded`, `failed` |
| `UIKEYS` | optional `target=` selector, optional `settle=`; **body is the text** | `UIEND` | `no_text`, plus every `UICLICK` error |
| `UISHOT` | `x`, `y`, `w`, `h` (a width and a height, not a right and a bottom), `bpp` | `UIDAT` chunks of a native-bpp DIB; `UIEND w=, h=, bpp=, bytes=` | `no_memory`, `failed` (`detail=` says which) |
| `UIFOCUS` | `target=` selector, optional `settle=` | `UIEND` | every `UICLICK` error except `occluded` |

- **Every request ends in exactly one `UIEND`**, including the ones refused
  before they run and the ones still queued when the worker stops. Silence
  costs the bridge its whole timeout and tells it nothing, which is the same
  duty `reject_busy` has on the job lane.
- **`UIENT` records are length-prefixed, not `key=value` lines.** A window's
  text comes back through `WM_GETTEXT` and can contain anything somebody
  typed into an Edit control, newlines included; a line format yields three
  plausible records where there was one control. The encoding is in
  `win/ui/uirec.h` and the parser in `bridge/wire/uirec.py`, pinned against
  each other by `tests/python/guest/test_uirec_agreement.py`.
- **No handle crosses the wire.** A record carries `src`, `path`, `parent`,
  `ctrl` and `child` — never an `hwnd`. One is meaningless across a session
  and 9x reuses them, so a handle sent back later can click whatever
  inherited it. The bridge computes a `sel=` selector per record and the
  agent addresses controls with that; a stale selector is a clean
  `not_found`.
- **`ambiguous` is an answer, not a failure to try.** It carries the count,
  because the count is what tells the caller whether the `:nth(n)` it is
  about to write exists. Clicking the first of four buttons called OK is the
  outcome this lane exists to refuse.
- **The waits are headers, not ops.** `wait_for`/`settle_ms` on `UITREE` and
  `settle` on `UICLICK`/`UIKEYS`. The loop an agent runs is "do a thing, then
  look", and folding the wait into both ends turns three round trips into
  one — behind a socket to a 200MHz machine the round trip is the expensive
  part.
- **One worker, one request.** The desktop worker services its queue
  serially, so "one in flight per lane" holds without a separate gate; a
  caller more than four requests ahead gets `busy`.

- **`exit` is only meaningful without a shell.** Measured on the VM
  (`docs/measurements/execprobe-vm-gui-20260926.txt`): `COMMAND.COM /c` on 98
  exits 0 whatever happens, even for a program that does not exist; a program
  spawned directly returns its real code. `ExecResult::viaShell` records which,
  and the Python client documents it.
- **`EXEC` does not inherit handles it should not.** `CreateProcess(..., TRUE, ...)`
  hands every inheritable handle to the child. Measured: on 98 a socket is
  **not** kept alive that way (the listener dies on the parent's
  `closesocket` while an inheriting child runs), but on NT and Wine it is,
  and a child outliving `gremote` would hold the port. So gremote makes its
  listening socket non-inheritable with `DuplicateHandle(..., FALSE, ...)`,
  which works on a SOCKET on 98 too; `LAUNCH` passes `FALSE`.
- **`KILL` deny list** (`Processes::kill`): `MSGSRV32.EXE`, `KERNEL32.DLL`,
  `MPREXE.EXE`, `MMTASK.TSK`, `REDIR32.EXE` and our own pid always;
  `EXPLORER.EXE` and any 16-bit (NE) process only with `force=1`. Killing the
  first four can hang 9x; `REDIR32` cannot be killed at all, only turned into
  a zombie; Win16 kill safety is untested.
- **There is no remote `QUIT`.** Stopping `gremote` remotely is `EXEC` of
  something that kills it, which is deliberate friction.

`UIFOCUS` raises the window containing `target` and does nothing else.
`UICLICK` and `UIKEYS` already raise what they need, so it exists for the case
where pressing something would be wrong -- a `UISHOT` of a window that is
behind another. It cannot be quiet: on 9x a window cannot be raised across a
process boundary without being activated, measured in
`docs/measurements/focus-vm-20260927.txt`.

### UI details decided now

- **Focus on Win98 is not free.** `PLAN.md` Phase 6 said 9x has no foreground
  lock; that holds for 95 and NT4, not 98, which introduced it alongside 2000.
  A background `gremote` calling `SetForegroundWindow` makes the taskbar button
  flash instead. At startup, try `SPI_SETFOREGROUNDLOCKTIMEOUT` = 0; otherwise
  attach to the foreground thread, set focus, detach. After `FOCUS`, check
  `GetForegroundWindow() == target` and return `ERR focus` if not, so `TYPE` never
  types into the wrong window. On the P200 checklist.
- **MSAA is loaded, never imported.** `LoadLibrary("oleacc.dll")` +
  `GetProcAddress("AccessibleObjectFromWindow")`. Win98 is believed to ship it
  (MSAA 1.x); verify on the P200. When absent, `TREE` falls back to the window
  tree and `HELLO` says `msaa=0`.
- **Hung targets.** Before touching a window's process, probe it with
  `SendMessageTimeout(WM_NULL, SMTO_ABORTIFHUNG)`; window text also goes through
  `SendMessageTimeout`. A per-node deadline alone cannot interrupt a COM call
  already blocked in an STA, so the ui thread registers an `IMessageFilter`
  whose `MessagePending` returns `PENDINGMSG_CANCELCALL` past the deadline, with
  a `SetTimer` so it gets called. Never `IsHungAppWindow` (2000+). The walk
  streams records under `max_tree_nodes` instead of building a tree.
- **Tree record fields:** hwnd, parent, class, text, rect, style, visible,
  enabled, and with MSAA: role, name, value, state, default action, location.
- **Input** from retro-agent's `input.c` goes to `win/automation/`, not the
  `win/app/input.cpp` that `FORKED.md` once named: that file is the compose box.
  Keep the E0 extended scancode table and the `VK_SNAPSHOT` fallback. Click
  checks with `WindowFromPoint` that the target is not covered (`PLAN.md`
  Phase 6). `DRAG` and `MOVE` are deferred until a test needs them.
- **Screenshot** as native-bpp DIB rows plus the palette in `DATA` frames: on an
  8bpp display that is a third of 24bpp and no GDI conversion. Needs retro-agent's
  palette fix (a fresh memory DC has only the 20 static colours). The current
  `app/screenshot.cpp` writes to a file handle, so it is rewritten onto a sink,
  not just moved. `BI_RLE8` stays the open question of `BACKLOG.md` 8.

## Python client

A plain blocking-socket package, `gremote/` at the repo root. No asyncio.

```python
from gremote import Remote

with Remote("192.168.1.50", secret="...") as r:
    res = r.exec(r"C:\WINDOWS\COMMAND.COM /c mem", timeout=20)
    print(res.output_text)                 # decoded with HELLO's oemcp
    r.put("build/win/retroclaude.exe", r"C:\Gaston\retroclaude.exe")
    for p in r.ps(): ...
    r.kill(pid)
    img = r.screenshot()                   # PIL.Image
    for w in r.windows(): ...
    node = r.tree(hwnd).find(role="push button", name="OK")
    r.click(node)                          # or r.click(x, y)
    r.type("hello"); r.keys("alt+f4")
```

- **Heartbeat on the Python side:** a background thread sends `PING` every
  10 seconds when the socket has been quiet, sharing the send lock with
  requests. It keeps running while the main thread blocks in a long `exec`, so
  a slow command never looks like a dead client.
- The frame codec moves out of `bridge/wire/protocol.py` into a dependency-free
  module both `bridge` and `gremote` import. Today `protocol.py` pulls in
  `fold` and `files.caps`, and `link.py` pulls in the view layer.
- Tests live in `tests/python/remote/`.
- A `ship()` helper replaces the `vmship.sh` flow on real hardware: close the
  client, upload, relaunch. The locked-exe and single-instance traps (a running
  exe locks its file; a second copy raises the first and exits) become code.
- `PUT` writes a temp file, checks the CRC from `END`, then deletes the target
  and renames (`MoveFileExA` is on the deny list). A truncated
  `retroclaude.exe` is a bricked client.

## Safety of the test box

- **Self-update keeps a way back.** `gremote` copies itself to `%TEMP%`, the copy
  waits for the old pid to exit, renames `GREMOTE.EXE` to `GREMOTE.OLD`, puts the
  new one in place, launches it, and waits for it to answer on localhost. If it
  does not answer (an NT-only import dies at load with no log), the copy
  restores `GREMOTE.OLD` and launches that.
- **`REBOOT`** exists for a wedged client. `ExitWindowsEx` needs no privileges
  on 9x.
- **Autostart** through the `RunServices` registry key plus
  `RegisterServiceProcess` (resolved dynamically), so `gremote` runs before
  logon and survives logoff. Installing it is a one-time manual step.
- **Out of band, nothing yet.** An in-guest server dies with the guest; a smart
  plug or PiKVM is the answer to a blue screen, not this plan.

## Build changes

- **Object files mirror the source tree.** `win/app/input.cpp` compiles to
  `$(BUILD)/obj/app/input.o`, `win/automation/input.cpp` to
  `$(BUILD)/obj/automation/input.o`. This applies to all code, the client's
  included, not just the new libs. Today
  every object shares one `$(BUILD)/%.o` under a flat `VPATH`, which silently
  takes the first match: `app/input.cpp` against libautomation input, `screenshot.o`
  mid-move, `exec.o`. With mirrored paths two files with the same name in
  different directories simply cannot collide, and `VPATH` goes away.
- **Explicit source lists per target**, not pattern discovery. Each lib and exe
  names its sources by path; the object list is derived from it.
- **Libs are built with `wlib -q -b -n`** into `$(BUILD)/lib/`, and a target
  lists a lib among its inputs like an object. No duplicate-name check is
  needed: `wlib` names each member by the full source path the compiler
  recorded, not the object's basename, so `a/x.o` and `b/x.o` coexist
  (verified; the review assumed otherwise).
- **Header dependencies from the compiler**, not a wildcard. `HDRS` today is
  every header under a hardcoded directory list, so a new directory's headers
  never trigger rebuilds. `wcc386`/`wpp386 -ad=<file>.d -adt=<obj> -adfs` write
  a make-style dependency file next to each object, and the Makefile
  `-include`s them. Watcom writes the source's basename as the first
  dependency and `-add=` truncates a `/` path the same way, so the recipe
  rewrites that one token.
- **The gates key on object paths, not basenames**, now that basenames can
  repeat (`cmovcheck.py` matches basenames against `$(OBJS)` today).
- **`make check` is parameterised by EXE / MAP / CAP** and runs for both
  `retroclaude.exe` and `gremote.exe`. Today every step hardcodes
  `retroclaude.exe`.
- **The gates must keep seeing code that moves into a lib.**
  `tools/check/stackframes.py:36` exempts every `.lib:` module and
  `cmovcheck.py:57-59` only counts basenames in `$(OBJS)`, so as written both
  would stop checking our own code the moment it moves. They learn that our
  `lib*.lib(...)` modules are owned, and a check fails if lib objects drop out
  of coverage.
- **The client has 180 bytes of headroom**: 398,820 committed at load against a
  399,000 budget (measured 2026-09-25). Linking from libs must not grow it;
  step 1 compares the number before and after.

## Memory budget

`gremote.exe` runs next to the client it is testing, so its footprint shows up
in every measurement taken while it is running.

- Five threads (reader, sender, job, fs, ui) with explicit small stack
  reserves in `_beginthreadex`.
- One 64KB inbound frame buffer, one bounded out queue, a screenshot strip
  buffer, exec's 8KB chunk. No per-request heap growth; streaming everywhere.
  The output cap is a parameter (`ExecOptions::maxOutput`; the client passes
  32KB), since the remote shell streams rather than buffers.
- `make check` gives `gremote.exe` its own committed-at-load cap.
- The first P200 session measures `gremote` idle and under load, so later
  client measurements can subtract it.

## Build order

Each step leaves `retroclaude.exe` building and behaving the same, and ends
with `make check`, `make test-fast`, `make wine-smoke` and a guest run.

1. **The build first, then libcore.**
   - *1a, done (`c9d70fa`):* mirrored object tree, no `VPATH`, explicit source
     lists, compiler header dependencies. All 12 exes disassemble identically
     to the build before it.
   - *1b, done (`310553d`, `21a112e`, `497aa67`):* gates own our libs and fail
     when blind; `libcore.lib` built with `wlib` and linked by every exe; `make
     check` per exe. Each gate proven to fire on a violation planted in
     libcore. Committed at load 398,808.
   - *1c, done (`5e408b8`, `72d1116`, `4d4f69b`):* `IniFile` and
     `App`/`Service` in libcore, with `inifilecheck` (28 cases) and `appcheck`
     (9 lifecycle traces) run under Wine by `tests/python/guest/`. The client
     links none of it yet; committed at load still 398,808.
   - **Step 1 is complete.**
2. *2a done 2026-09-25 (`4ea30a1`, `961cfd8`):* the frame codec is libwire
   (`win/wire/frame.{h,cpp}`); `net/frameproto.*` keeps the client's version
   and lane table. On the bridge side `bridge/wire/frame.py` is the codec,
   stdlib only, with header text as a pluggable `TextCodec`;
   `protocol.Frame`/`FrameReader` plug in the CP1252 folding.
   **libwire, extracted from `net.cpp`, not written fresh.** Its shutdown order
   (FIN before join, leak on join timeout, no descriptor reuse) cost many fixes.
   Split `frameproto` into the codec (libwire) and the client's lane table
   (stays in `net/`). Add `Listener` and the server-side reader. `net.cpp` is
   frozen until the client moves onto libwire, so nothing is fixed twice.
   Extract the Python frame codec.
   *2b done 2026-09-25 (`docs/wire-connection-plan.md` has the steps and
   hashes):* `Connection` (`win/wire/connection.{h,cpp}`) is the transport,
   and `net.cpp` is the client's adapter over it. `Listener`
   (`win/wire/listener.{h,cpp}`), `Connection::adopt` and the idle timeout
   are the server half, proved by `wirecheck` over loopback under Wine
   (`tests/python/guest/test_guest_wire.py`). Verified on the guest with the
   client. On the way, a pre-existing one-byte heap overrun in the sync
   scanner was found and fixed; it crashed every build under a SYNCGET flood.
   **Step 2 is complete.**
3. **libexec.** Move exec onto `ExecJob` (per-job cancel, id-checked), output
   cap as a parameter, handle inheritance fixed; the client's rpc adapts.
   Add process list, kill, launch.
   *Done in code 2026-09-26 (`docs/libexec-plan.md` has the steps and
   hashes):* `ExecJob`, `Processes`, `readSystemInfo`/`requestReboot` in
   `win/exec/`, the client on them, EXECKILL id-checked. A probe on the VM
   first (`win/checks/execprobe.cpp`) found that a stop never worked on 98 --
   the pid exec held was `REDIR32.EXE`, which TerminateProcess cannot end
   (`BACKLOG.md` 14) -- so a stop now ends the whole tree. Verified on the
   VM: the client passes the stub-bridge run (`tools/probe/execkill.py`),
   and a stop now kills only the Win32 side and lets the job end itself,
   which leaves nothing; only a DOS program that must be stopped still
   costs one REDIR32 (~150KB), logged (`BACKLOG.md` 14).
   Also landed in this step: the agent's `ps`/`kill` tools on libexec
   (`kill` refuses a DOS session without `force`), and `execcheck` runs
   24/24 on the real 98 guest. Gaps left are decided in `BACKLOG.md` 17.
   **Step 3 is complete (2026-09-27).**
4. **`gremote.exe` v0 + Python client.** App, config, listener, AUTH/HELLO,
   control and job lanes, per the op table. **First prove Wine can host a
   listening exe that pytest connects to:** every existing Wine test runs the
   exe as a client, so the reverse direction has no precedent here. Then ship
   to the VM, then the P200. **First useful milestone: ssh-equivalent on the
   real box.**
5. **libfs**, the fs lane, `PUT` with CRC and temp-rename, `ship()`, self-update.
6. **libautomation.** Screenshot, window list, focus and input, control tree,
   MSAA, in that order. Each verified on the P200, not just Wine: window trees,
   focus and input are exactly where Wine lies.

### The client is the first consumer (decided 2026-09-25)

The libraries exist to be shared, so the client moves onto them **before**
gremote is written, not after: every lib is proven in the shipping program
first. Each C-step is a small commit that leaves the client working.

- **C0. Budget.** Linking `App` and `IniFile` costs about 6 KB committed at
  load (measured: `App` +2,560, `IniFile` +3,584, both +6,144 over 398,808).
  `COMMITTED_CAP` goes to 405,000 up front, by decision; trimming later if the
  P200 shows pressure.
- **C1. `App` message filters** *(superseded by `preTranslate` when App
  became a lifecycle)*. The client's loop runs accelerators,
  `IsDialogMessage` and keyboard shortcuts (PgUp/PgDn, Ctrl+Home/End, Esc,
  Enter on the chat list) before dispatch. `App::run` gains an ordered list
  of `MessageFilter`s, each able to consume a message.
- **C2. `IniFile` writes.** The client saves its INI (`ID_FOLLOW`, the host
  dialog), so `IniFile` gains typed setters.
- **C3. `ClientConfig` replaces `rc_config`.** Loaded through `IniFile` with
  `checkKeys` and bounds; an invalid value refuses startup with a message box
  (the client is always interactive). `inicheck`'s save/load agreement moves
  with it.
- **C4. `WinMain` becomes a composition root.** Log and the single-instance
  check stay ahead of `App`. Services, in registration order: OLE, the main
  window (class, window, widgets, filters), the connection (`rc_net`; `join`
  is `rc_net_stop`). **Closing changes shape:** `WM_CLOSE` asks `App` to quit
  instead of destroying the window, so shutdown stops the connection while
  the window it posts to still exists, then destroys the window. Today the
  network is stopped from inside `WM_DESTROY`, on the UI thread, for up to
  about 42 s.
- *C0-C4 done 2026-09-25* (`97624c2`/`2a99058` budget, `28ef321` filters,
  `9a57456` writes, `67fc263` ClientConfig, `4914752` composition root). C4
  verified on the Win98 guest: start and reconnect, Esc denying a permission
  through the filter, File > Close while connected.
- *C5a-C5d done 2026-09-25:* `WinMain` split into `app/main.cpp` from the
  window (`app/mainwindow.cpp`); `OleService`/`ConnectionService` in their
  own files; `Reconnector` and `Turn` report to listener interfaces;
  `FrameSender` replaces five copies of the `ui_send` typedef. Verified on
  the guest: reconnect, busy state, Allow and a full turn, switching chats.
  Budget 412,000 as headroom for the rest of C5.
- *C5e done 2026-09-25:* App became a lifecycle (initialize ... cleanup)
  with UniqueObj service members; `Window`/`SubclassedWindow` in libcore;
  every module callback became a listener interface MainWindow implements:
  `ChatList` (listbox a `SubclassedWindow`), `ComposeBox` (edit a
  `SubclassedWindow`), `PermissionBar`, `AttachmentBar` (a `Window`; the sync
  worker reaches it through a context), `OptionMenus`, `WorkDirProposer`.
  Verified on the guest: click-to-open, Enter-to-send, Allow, a screenshot
  attachment end to end, a Commands entry, Ctrl+A.
- *C5f-C5g done 2026-09-25:* `MainWindow` is a `Window` (`wnd_proc` became
  `handle()`), and all of the main window's file statics and helpers are its
  members; `g_window`/`g_main` are gone. `mainwindow.o` builds with `-os`
  (the `-ot` output for `handle()` was 9 KB larger). Committed at load
  405,976. A stale `main.o` (lost `.d`) crashed the first Wine run; the
  build now rebuilds any object whose `.d` is missing. Verified on the
  guest: start, reconnect, a full turn with Allow.
- **C5+. `main.cpp`'s statics become objects**, one module per commit:
  context-free callbacks (`rc_refresh_fn`, `rc_turn_busy_fn`, ...) gain a
  context, and the state they close over moves into the window service.
- Then **libwire** and **libexec** are extracted from `net.cpp`/`exec.cpp`
  with the client as their first consumer (steps 2-3), and gremote follows.

The Phase 6 agent tools then need only handlers and bridge work.

## P200 checklist

Things only the real box can answer, collected so the first session covers them:

- `oleacc.dll` present, and its version.
- Foreground lock: does `SPI_SETFOREGROUNDLOCKTIMEOUT` = 0 work from a background process.
- `COMMAND.COM /c` exit codes: is the child's errorlevel returned. *Answered
  on the VM (2026-09-26, `docs/measurements/execprobe-vm-gui-20260926.txt`):
  no, always 0, even for a program that does not exist; a direct spawn
  returns the real code. The P200 only needs to confirm.*
- Handle inheritance of sockets into children on 98. *Answered on the VM
  (same file): a listening socket is not kept alive by an inheriting child;
  closing it in the parent frees the port. Wine and NT differ.*
- `bind` after a restart: TIME_WAIT behaviour.
- `gremote` memory idle and under load, with and without the client running.
- Whether `rsrc32.dll`'s free-resources export works, if we want USER/GDI numbers in `SYSINFO`.

## Open decisions

- **Deflate on by default.** Probably off on LAN; screenshots are the only large
  payload.
- **`BI_RLE8`** for screenshots (`BACKLOG.md` 8).
