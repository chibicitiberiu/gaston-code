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

The **CRLF/binary classifier and its dual CRC stay in `win/sync/`**, not in
`libfs`. They are bridge agreement rather than filesystem behaviour: they
mirror `bridge/files/paths.py` and honour `.gitattributes`, which libfs has
never heard of, and `gremote`'s `GET` is byte-exact. libfs gives them a
one-pass read hook (`fs::ByteSink`) so nothing reads a file twice.

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
RTTI, trivial templates only, **no file-scope objects with non-trivial
constructors** (they run before `rc_log_open`, so a failure there has nowhere
to report). Exceptions are **on** (`-xs`) and the STL is allowed as of
2026-09-25 -- `std::vector`, `std::string`, `std::wstring`, `std::list` in
place of fixed `MAX_*` tables -- but not `<iostream>`/`<fstream>`, and nothing
uses exceptions for control flow. `Buf`/`Path` stay wherever a Win32 call fills
a fixed buffer. See CLAUDE.md, which is the current word on this.

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
- **There are two shutdown paths and only one of them may wait.**
  `joinOrLeak()` takes the `App` so it can tell them apart.
  - *The ordinary path* -- `quit()`, the loop ends, `run()` returns. The
    process goes on living, so an orphan thread is a real bug: join properly,
    and leak only what would not stop.
  - *The session-ending path* -- `WM_ENDSESSION`. **Nothing is joined at all.**
    Windows does not carry on with a 9x shutdown until that window procedure
    returns; a handler that takes its time is the "not responding / End Task"
    dialog, and by `WM_ENDSESSION` Explorer has gone, so there is no desktop
    left to put the dialog on. Worse, the thread Windows is waiting for can be
    one of ours: `REBOOT` runs `ExitWindowsEx` on the `Connection` reader
    thread, that call does not return, and the broadcast it drives
    `SendMessage`s App's own window -- so a join there waits on the accept
    thread, which waits on the reader, which waits on the handler. A machine
    hung exactly this way on 2026-10-01 (P200 checklist below). So:
    `signalStop()` everything, abandon each service *unjoined*, write and
    close the log, return. `runApp()` then leaks the `App` itself, because the
    abandoned threads hold pointers into its members.
  - *And on a session end THIS PROCESS asked for, it does not quit either.*
    `App` skips its `PostQuitMessage`, the message loop keeps running, and the
    thread inside `ExitWindowsEx` carries the shutdown to the end -- because on
    9x that thread is what DRIVES it. `ControlOps::reboot` sets
    `noteSessionEndAsked(true)` before the call. The derived app then owes the
    other half: a watch for the session end that failed to take, which
    `GremoteApp` discharges with a timer in `stopServices()` and
    `preTranslate()`. Both measured on the guest; see the P200 checklist.
  - `WM_QUERYENDSESSION` agrees and changes nothing: a shutdown another
    program vetoes is cancelled, and there is no un-signalling a service.
  - Pinned by `appcheck`'s `end-session-does-not-wait-for-a-stuck-service`
    (the handler returns in milliseconds against a service that never wakes)
    and `an-ordinary-quit-still-joins-and-waits` (the other half).
- **A service with no threads takes `signalStop()` and no `join` at all**, so
  no `joinOrLeak()` either -- there is nothing to abandon, and a call that
  logged "abandoning X unjoined" on every session end would say something
  untrue. `TrayService` is the first of these: `signalStop()` takes the icon
  out of the notification area, which is work that must happen on *both*
  paths and cannot block on either (`win/remote/main.cpp`'s `stopServices`).
- **A failed step still shuts down.** `main()` is skipped, `stopServices()`
  and `cleanup()` run, and both cope with a partial start: an empty
  `UniqueObj`, a service whose `init()` failed.
- **`App` owns a hidden top-level window** (a `Window`, `core/window.h`), not
  a bare thread queue: `PostThreadMessage` to a thread without a queue fails
  silently on 9x, and the window receives `WM_ENDSESSION`, on which App
  signals the services and gets out of the way before Windows ends the
  process. `appcheck` confirms `EnumWindows` finds it -- that set is what USER
  broadcasts the session messages to, so being in it is what makes the whole
  path reachable.
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

[machine]
; 1 refuses this box's attempts to put itself to sleep, which otherwise takes
; the network with it. 0 (the default) leaves the machine alone.
prevent_sleep=0

[ui]
; 2 the full lane (TREE, SHOT, RUN); 1 read-only (TREE and SHOT, no input);
; 0 off -- every ui op refused with automation_off, on the NEXT request
automation=2
; 0 skips oleacc.dll even if present
msaa=1
max_tree_nodes=2000
call_timeout_ms=2000
; a whole step list, and one step that names no timeout of its own
script_ms=120000
step_ms=30000

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
- **`[ui] automation` is the control, not an optimisation.** It is read on
  every request rather than captured at connect, so turning it off takes
  effect on the next request instead of the next connection -- and the tray
  menu does exactly that, with **three items and not a tick**, because the
  middle value is the interesting one and a cycling item would make somebody
  click twice to find out where they were. Like **Prevent sleep**, the menu
  is NOT written back to the ini: the menu is "for now", the file is "from
  boot", and the log records the change and where it came from. That menu is
  the only control a person at this keyboard has over whether the box can be
  driven. The default is **2**, because there is nobody
  at this keyboard to surprise and a box you cannot see is the problem the
  lane exists to fix -- but the section is written into the ISO's
  `GREMOTE.INI` with every key, so a fresh install has a line on its own disk
  saying the screen can be driven, and the effective level is logged at
  startup. It is also an enum and not a bool because the middle value is
  real: a box that may be read and photographed but not typed into.
- **`[machine]` is the box, not the socket.** `prevent_sleep` is the only key
  so far. It is **off by default**: it changes how the hardware behaves, so an
  install that predates the option has to come up exactly as it did before. The
  tray menu's checked **Prevent sleep** item is the same switch at the keyboard,
  and the tooltip carries `[awake]` while it is on. `win/remote/tray.h` has the
  four mechanisms, what is documented for 9x about each, and why
  `SystemParametersInfo`'s power flags are not among them -- they mutate a
  persistent system setting that no restore can be guaranteed to put back.
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
- **`CANCEL` follows `BACKLOG.md` 6** (9P `Tflush`): its ack means the
  request will never be answered, and the id cannot be reused before the ack.
  It compares the id under the lock, fixing the id-blind EXECKILL (`BACKLOG.md` 9).
  *Corrected 2026-09-27 while building it:* it carries TWO ids, as `Tflush`
  does. The first draft of the table gave it only the target's, and the ack
  then comes back under the victim's id and lands in whatever is waiting for
  the victim's own reply -- indistinguishable from the answer this op
  promises will never come.
- **Lanes.** One worker per lane, one request in flight per lane, `ERR busy
  op= age_ms=` otherwise:

| Lane | Thread | Ops | May block on |
|---|---|---|---|
| control | reader, inline | `AUTH`, `PING`, `CANCEL`, `STOP`, `SYSINFO`, `PS`, `KILL`, `REBOOT`, `UPDATE` | nothing: kernel32 and Winsock only |
| job | job worker | `EXEC`, `LAUNCH` | the child |
| fs | fs worker | `LS`, `STAT`, `GET`, `PUT`, `RM`, `MKDIR`, `RMDIR`, `MV`, `DRIVES` | the disk |
| ui | ui worker (message queue, `CoInitialize`d) | `SHOT`, `TREE`, `RUN` (a step list: `StepRunner`, win/automation/steps.h, as the client's `UIRUN`) | USER/GDI, other apps |

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
| `CANCEL` | `target` (the id to stop); `id` is its own | `OK id=` (target is dead, and will never be answered) | `no_such_id` |
| `SYSINFO` | none | `OK` with `mem_free_kb`, `uptime_ms`. No USER/GDI free-resource numbers: `GetFreeSystemResources` is 16-bit only (`PLAN.md` Toolchain facts) | none |
| `PS` | none | `DATA` records `pid` (unsigned: 9x pids are obfuscated pointers with the high bit set), `ppid`, `threads`, `exe`; `END count=` | `no_toolhelp` |
| `KILL` | `pid`, optional `force=1` | `OK` | `pending` (TerminateProcess said yes and the process is still there; on 98 that is not the same thing), `denied` (deny list below), `no_such_pid`, `failed` |
| `REBOOT` | `confirm=yes`, optional `mode=logoff\|reboot\|poweroff` | `OK`, then `ExitWindowsEx` | `unconfirmed` |
| `EXEC` | `timeout_ms`, `cwd`, optional `shell`; body is the command line | `DATA` output chunks (OEM bytes, untouched), `END exit=, truncated=, timed_out=` | `busy`, `spawn_failed` |
| `LAUNCH` | `cwd`, `show`; body is the command line | `OK pid=` | `spawn_failed` |

The fs lane. The client answers these as `FS*` frames (`win/rpc/fsrpc.cpp`,
step 5a) and `gremote` as the shorter names in the lane table above. They are
TWO sets of handlers sharing libfs, not one set: what is shared is libfs's
calls -- and, since 2026-10-02, the four mutations' reasons
(`win/fs/ops.h`), so RM/MKDIR/RMDIR/MV and the client's FSRM/FSMKDIR/FSRMDIR/
FSMV classify Windows 98's errors once. Every error token here is one `win/sync/syncrpc.cpp` already
produces, which is a constraint and not a coincidence -- see the note after the
table.

| Op | Request headers / body | Reply | Errors |
|---|---|---|---|
| `LS` | `path`, optional `max_entries` | `DATA` records `name`, `type`, `size`, `mtime`, `attrs`; `END count=, truncated=` | `bad_path`, `missing`, `too_many` |
| `STAT` | `path` | `OK type=, size=, mtime=, attrs=` | `bad_path`, `too_long`, `missing`, `locked` |
| `GET` | `path`, optional `max_bytes` | `DATA` chunks (`total`, `off`), then `END status=, crc=, bytes=, truncated=` | `bad_path`, `missing`, `is_directory`, `locked` |
| `PUT` | `path` on the first frame and `off` on every one; the body is raw bytes and nothing else. `END crc=` closes it | `OK bytes=, crc=` | `bad_path`, `too_long`, `no_parent`, `no_space`, `locked`, `readonly`, `out_of_order`, `crc_mismatch`, `busy` |
| `RM` | `path` | `OK` | `bad_path`, `missing`, `is_directory`, `readonly`, `locked` |
| `MKDIR` | `path` | `OK` | `bad_path`, `too_long`, `no_parent`, `exists`, `is_file` |
| `RMDIR` | `path` (an EMPTY directory) | `OK` | `bad_path`, `drive_root`, `missing`, `is_file`, `not_empty`, `denied`, `is_cwd`, `locked` |
| `MV` | `from`, `to` | `OK` | `bad_path`, `too_long`, `missing`, `exists`, `no_parent`, `cross_volume` (a FOLDER to another drive -- a file moves, measured on the Athlon), `denied`, `locked` |
| `DRIVES` | none | `DATA` records `letter`, `type`, `label`, `serial`, `total`, `free`; `END count=` | none |

- **The path travels in a header, not in the body.** `SYNCPUT` puts it in
  the body, length-prefixed, and needs a parser with four refusals to get it
  back out -- which exists because that format predates the header being an
  option. There is no reason to repeat it: a Windows path cannot contain a
  newline, so a header holds one, and the body stays raw bytes.
- **`max_bytes` is honoured by the guest, not the caller.** A bridge-side cap
  still has the guest read and send the whole file; pointed at a swap file that
  is a socket held open for as long as it takes. `truncated=1` says it stopped.
- **`PUT` outside a sync root does not move the old file aside.** The aside
  exists so no instant has no file, and it is safe only because
  `rc_sync_recover` runs over the sync root at startup. Nothing will ever run a
  recovery pass over `C:\WINDOWS`, so a failure there must leave the original
  in place or leave nothing at all -- never a `.gaston-old` with no file at the
  real path.
- **The error vocabulary is closed by a test.**
  `tests/python/guest/test_guest_agreement.py` diffs `bridge/status.py`'s
  `SyncStatus` against the literals it scrapes out of `win/sync/syncrpc.cpp`,
  in both directions. A token the fs handlers can send that `syncrpc.cpp`
  cannot makes that test fail. Adding one means widening the extraction in the
  same commit.
- **`DRIVES` sets `SEM_FAILCRITICALERRORS`** for the length of the
  enumeration. `GetVolumeInformationA` on an empty floppy otherwise trips the
  9x critical-error handler, which is a modal dialog on the user's screen
  raised by a read-only call, blocking the worker until someone dismisses it.

The **ui lane, as gremote answers it** (step 6, written 2026-10-02 at the start
of the step as this section's preamble requires). Three ops plus a stop, over
the same handlers the client runs: `win/ui/uiengine.cpp` is shared and knows
nothing of either wire, and each exe supplies a `UiLink` that turns the
engine's calls into its own frames (`win/ui/uilink.h`).

| Op | Request headers / body | Reply | Errors |
|---|---|---|---|
| `TREE` | as `UITREE`, minus `inproc` | `DATA` records, then `OK nodes=, walk=, enriched=, dropped=, too_deep=, self=` | as `UITREE`, plus `inproc_unsupported` |
| `SHOT` | as `UISHOT` | `DATA` chunks of a BMP, then `OK w=, h=, bpp=, bytes=` | `no_memory`, `failed` |
| `RUN` | as `UIRUN`; **body is the step list** | `DATA` lines, one per step that ran, then `OK done=` or `ERR code= step= done= detail=` | as `UIRUN` |
| `STOP` | `target=` the id to stop | `OK` -- and the target still answers, with `cancelled` | `no_such_id` |

- **`STOP` is not `CANCEL`, and both exist.** `CANCEL` is 9P's `Tflush`: its
  ack promises the request will never be answered. A step list needs the
  opposite -- a list that failed at step 5 still ran four steps the caller has
  to hear about -- so `STOP` acks and the target answers `cancelled` with its
  partial report. **Both raise the same stop flag**; they differ only in
  whether the reply is suppressed. If `STOP` alone raised it, a cancelled walk
  would run to its full deadline with its replies dropped and every ui request
  behind it answered `busy`, which is `BACKLOG.md` 26 reproduced here.
- **`STOP` is the Session's, not the control lane's**, for the reason `CANCEL`
  already is: it is about the lanes and the Session owns them, and it needs an
  ungated `Responder` or the ack is suppressed along with the target. The id is
  compared against `LaneWorker`'s `m_inFlightId` under the lane's lock, which
  also covers a request that is queued but not yet started.
- **The stop flag reaches all three ops**, not just the step list.
  `NodeSink::onNode` and `ByteSink::write` already return `bool` and
  `TreeWalker` already has `WALK_STOPPED`, so a stopped walk or capture ends at
  the next node or strip instead of finishing for nobody.
- **One request in flight, and `ERR busy` otherwise**, as the other lanes. The
  ui worker is a `LaneWorker` with `onThreadStart`/`onThreadStop` hooks: it
  forces its message queue into existence, `CoInitialize`s, installs the call
  deadline, and builds its `UiContext` on that thread, because every member of
  it is apartment- or thread-bound. There is no pump mode -- `idle()` drains
  the queue with `PeekMessage` on every pass of the loop the lane already runs.
- **The event feed starts lazily and stops when the lane goes quiet.** A
  system-wide `SetWinEventHook` left installed would wake gremote on every
  window event on the machine, forever, with no request pending -- which is the
  opposite of `win/remote/tray.h`'s "nothing polls" rule for a program that
  runs beside the thing being measured.
- **`inproc` is refused rather than ignored.** gremote does not carry the
  in-process tier: it needs `HOOKDLL.DLL` beside the exe and a deny list with
  no force escape, and a caller who asked for that tier and silently got the
  out-of-process one would read an empty list box as empty.


The ui lane, **as built in the client** (`win/ui/`). These are the frame
types the shipping client answers today; `gremote` reuses the same handlers
behind the shorter names in the lane table above.

*Rewritten 2026-10-02.* `UICLICK`, `UIKEYS` and `UIFOCUS` were retired for one
`UIRUN` step list -- a list of actions and waits run in order, so "focus, type,
Ctrl+S, wait for Save As, type a name, Enter, wait for it to go" is one round
trip to a 200MHz machine instead of seven. `UICANCEL` arrived with it.

| Op | Request headers / body | Reply | Errors |
|---|---|---|---|
| `UITREE` | `scope=desktop\|foreground\|pid`, `pid`, `max_nodes`, `text`, `msaa`, `hidden`, `call_ms`, `timeout_ms`, optional `wait_for=` + `wait_ms`, optional `settle_ms`; or `acc=1` + `target=` for one control's MSAA items; or `inproc=1` + `target=` + `items=` for the in-process tier | `UIENT` records (see below) with `n=` per frame; `UIEND nodes=, walk=complete\|deadline\|maxnodes\|stopped, enriched=, dropped=, too_deep=, self=` | `bad_selector`, `not_found` (+`step=`), `ambiguous` (+`detail=` and up to three `cand1..3=`), `truncated`, `no_target`, `no_msaa`, `no_memory`, `busy`, `automation_off` |
| `UISHOT` | `x`, `y`, `w`, `h` (a width and a height, not a right and a bottom), `bpp` | `UIDAT` chunks of a native-bpp BMP; `UIEND w=, h=, bpp=, bytes=` | `no_memory`, `failed` (`detail=` says which) |
| `UIRUN` | `timeout_ms`, `step_ms`, optional `settle`; **body is the step list** -- one step per line, `verb` then TAB-separated `key=value`, every value percent-encoded | `UIEND status=, done=, step=, detail=, cand1..3=`, and a **body** of one `i= status= ms= hit= detail= part=` line per step that ran, plus `released=1` when the list left something held | `bad_steps` (+`detail=`), `cancelled`, `script_timeout`, `timeout`, `focus_lost`, `bad_key`, `bad_char`, `refused_self`, `no_window`, `not_foreground`, `occluded`, `not_visible`, `not_edit`, `no_item`, `ambiguous_item`, `no_text_match`, `ambiguous_text`, `prompted`, `still_open`, plus every `UITREE` error for the resolution it does |
| `UICANCEL` | `id=` of the running list | none of its own -- the run's `UIEND` carries `cancelled` | ignored and logged when `id` is not the one running |

**`UICANCEL` is `RC_LANE_IMMEDIATE`, not `RC_LANE_DESKTOP`**, because the
desktop worker is the thread running the list it is meant to stop: queued
behind it, the cancel would arrive when the list had ended. It raises a flag
the runner reads between steps and inside every wait.

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
- **The waits are headers and steps, not ops.** `wait_for`/`settle_ms` on
  `UITREE`, `settle` on `UIRUN`, and `waitfor`/`waitgone`/`waittext`/`idle` as
  verbs inside a step list. The loop an agent runs is "do a thing, then look",
  and folding the wait in turns three round trips into one -- behind a socket
  to a 200MHz machine the round trip is the expensive part. A failed wait IS
  the guard: a list has no branching, and the agent is the control flow
  between lists.
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
  something that kills it, which is deliberate friction. *Refined
  2026-10-01, when self-update made the tension real: what the friction
  protects is the machine's reachability -- a caller must not be able to
  take the box away from itself with one frame and have nothing to show for
  it. `REBOOT` already stops this process, harder, and is allowed because
  the box comes back. `UPDATE` is allowed on the same test, and is built so
  it cannot be degraded into a bare quit: it stops only after a validated
  replacement is staged and an updater process is already running to
  install it, and it can point at nothing but the running image. See
  "Safety of the test box" and `win/remote/control.cpp`'s `update()`.*
- **The tray icon's Exit is not that `QUIT` either, and is not an exception
  to it.** The bullet above is about a caller on the far end of the socket:
  what the friction protects is the machine's reachability from the other
  side. The person at the keyboard is standing in front of the box and can
  already pull the power, so making them hunt the process out of a task list
  protects nothing -- and the icon exists precisely because, with no window,
  there was no other way to tell a crashed `gremote` from a working one.
  `win/remote/tray.cpp` makes that argument at the `AppendMenu` call, so
  nobody later "fixes" it by adding a remote `QUIT` to match.

A step list's `focus` verb raises the window containing `target` and does
nothing else. `click` and the typing verbs already raise what they need, so
`focus` exists for the case where pressing something would be wrong -- a
`UISHOT` of a window that is behind another. It cannot be quiet: on 9x a window
cannot be raised across a process boundary without being activated, measured in
`docs/measurements/focus-vm-20260927.txt`. `Input::focus` relaxes
`SPI_SETFOREGROUNDLOCKTIMEOUT` around that one call and puts it back, because
leaving it at 0 is a change to somebody's machine that outlives us.

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
    r.put("build/win/gaston.exe", r"C:\Gaston\gaston.exe")
    for p in r.ps(): ...
    r.kill(pid)
    tree = r.tree()                        # the control tree, with selectors
    [ok] = tree.find("OK")                 # -> 'Button[text="OK"]'
    run = r.run([{"click": ok}, "hello", {"wait_text": "Notepad Edit",
                                          "text": "hello"}])
    if not run.ok:
        print(run.status, [(s.index, s.status, s.detail) for s in run.steps])
    open("shot.bmp", "wb").write(r.shot())  # or r.png(), with Pillow here
```

- **The desktop ops mirror the client's**, which is not tidiness: the
  handlers are the same code (`win/ui/uiengine.cpp` behind `UiLink`), and the
  Python reuses the bridge's own record parser, selector builder, step
  compiler and key table. A step list that works through the `input` tool
  works here, and a malformed one is refused on THIS side before anything is
  sent.
- **`run()` does not raise on a failing list**, and that is the one place
  this package departs from "an ERR is an exception". A list that failed at
  step 5 still ran four steps the caller has to hear about, and the report of
  them arrives in the `DATA` frames an exception would discard. `StepRun.ok`
  is the thing to branch on. `bridge/services/ui.py` made the same exception
  for the same reason.
- **`stop()` is not `cancel()`.** `cancel` is 9P's `Tflush` and its ack means
  the request will never be answered; `stop` promises only that the work
  stops, and the target comes back `cancelled` with whatever it had.
- **`shot()`'s ceiling is enforced as the frames arrive**, on the reader
  thread. By the time the caller sees them the memory is already spent.

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
  `gaston.exe` is a bricked client.

## Where things go on a target machine

Decided 2026-10-01, on the first real-hardware run, because the QEMU guest's
C:\ had become a midden -- `C:\RETRO` and `C:\GASTON` side by side, a
dozen smoke-test leftovers called `BIG`, `WRITEME3`, `PINEAPPL` loose in the
install directory, and scratch in three places. None of it was wrong when it
was written; it accumulated because nobody said where things went.

    C:\GASTON\      the client: gaston.exe, gaston.ini, view.htm,
                     spin.gif, tools_detect.ini, gaston.log, gaston.prv
    C:\GREMOTE\     the server: gremote.exe, gremote.ini, gremote.log,
                     gremote.prv
    C:\WORK\        the client's work root, and NOTHING else. Conversation
                     folders live under it; it must never contain either
                     program, or the client mirrors itself to the bridge
    C:\TEMP\GR\    scratch for a test session, and the only place a test
                     may write. Emptied at the end of the session that
                     made it

Four directories. Anything a test leaves outside them is a bug in the test,
and `ls` of each of these should be explicable in one sentence.

**`C:\WORK\<CONVERSATION>` is where the agent works, and it does not depend on
the bridge mirroring anything.** *Corrected 2026-10-01, the same day it bit:
the folder was created only when the bridge had a `--sync-dir`, so against a
bridge without one the agent had no destination it had been given and wrote a
file into the root of `C:` on the user's own machine.* The split:

- **The work root is the client's**, named in its own `gaston.ini` and created
  by it at startup (`win/app/main.cpp`). The bridge never chooses the parent;
  it reads `sync_root` from HELLO.
- **The folder inside it is named by the bridge**, from the conversation's
  title, because that is where the title and the client's declared naming
  rules (`illegal`, `maxcomp`, `shortnames`, `case`, `devices` -- `bridge/caps.py`,
  `win/caps.h`) are both in hand. `WorkspaceService.choose_name`.
- **The folder is created by the client**, from the `OPTION work` row of the
  options batch it already receives: `rc_workdir_apply` makes it parents and
  all and makes it that side's live work root (`win/app/workdir.cpp`). No wire
  op was added for this, because one would have had to beat what already
  happens (docs/PLAN.md §3.1, the exec test).
- **Both happen at the FIRST TOOL CALL**, not at the handshake: the name comes
  from the title and there is no title until the user has spoken. A
  conversation that is only talked to still creates nothing.
- **Mirroring is a separate question.** `--sync-dir` decides whether there is
  a tree on the bridge paired with that folder, with a baseline and deletion
  propagation. Without it the folder on the machine is still the agent's
  working directory there; files reach it one at a time through `put_file`.
- **If the client declares no work root at all, nothing is invented.** There is
  no agreed place on that machine, and a path chosen by the bridge would be a
  directory on somebody's real disk that nobody asked for. The agent is told to
  ask, and told that a drive root is never the answer.

The two programs get their OWN directories rather than sharing one. They are
separate products with separate configs and separate logs, one of them is
deployed by the other, and `ship()` ends every process running from the
directory it writes into -- which is a reason not to have them cohabit.

## Safety of the test box

- **Self-update keeps a way back.** *Built 2026-10-01, and the shape changed
  on the way: the sketch had `gremote` copy ITSELF to `%TEMP%`, and what
  shipped is a separate 47KB exe, `gupdate.exe`, installed beside the
  server.* A caller `PUT`s the new build to a staging path and sends
  `UPDATE`; the server validates it, starts `gupdate` and stops. `gupdate`
  waits for the old process to let go, renames `GREMOTE.EXE` to
  `GREMOTE.OLD`, puts the new one in place and launches it. If the new
  image does not stay up it restores `GREMOTE.OLD` and launches that.
  `gremote/ship.py`'s `self_update` then reconnects and reports what
  answered, because the machine stops being able to speak for itself in the
  middle of this.
  - **Why a second exe rather than a copy of the server.** The copy would
    carry libwire, libexec, libfs and Winsock into a program whose whole
    job is to still be working when the server is not. `gupdate` links
    `libcore` alone.
  - **How it knows the old process has gone: two polls, not one.** A
    running `.exe` is LOCKED on Windows 98, so "can I open the destination
    for writing" is the question asked directly rather than through a pid
    and `OpenProcess` -- which needs the pid, survives pid reuse by luck,
    and answers a different question anyway. But the relaunch needs a
    second fact: gremote is single-instance, and a copy started while the
    old one lives exits silently, which would read as a successful update
    that changed nothing. So it also waits for the named mutex
    `GastonRemoteSingleInstance` to disappear. Wine does not lock a running
    image, so under Wine only the mutex test says anything and on the guest
    both do -- which is exactly why there are two.
  - **The wait is bounded and its timeout is not silent.** 60 seconds by
    default. When it expires nothing is written at all, and the destination
    is launched anyway: either the old process is alive and the launch
    hits the mutex, or it died holding the image and the launch is the
    recovery. Every failure path in `gupdate` ends with something started
    and a line in `GUPDATE.LOG`; a machine with no server and no
    explanation is the one outcome it is written to avoid.
  - **Rollback covers a load failure, not a silent one.** See
    `docs/BACKLOG.md` §22 for what a port probe would add and what it would
    cost.
- **There is still no remote QUIT, and `UPDATE` is not one.** The rule below
  forbids casually taking the machine away from itself. `UPDATE` stops the
  server only after it has found the updater beside the exe, resolved its
  own image path from the loader, been given a source that exists, differs
  from that image, and reads as a 32-bit PE, and successfully STARTED the
  updater -- any of which failing is an `ERR` with the server still running.
  The destination is never a header; it is always the running image. So
  there is no spelling of `UPDATE` that means "stop", and `EXEC` of
  something that kills it remains the general way to stop it, at the same
  price it always cost.
- **`REBOOT`** exists for a wedged client. `ExitWindowsEx` needs no privileges
  on 9x. The `OK` goes out before the call, because on success the call does
  not return -- so the honest signal that a reboot did NOT happen is that the
  machine is still up and this server is still answering on the same socket,
  with `control: Windows refused the shutdown; this machine is staying up and
  the server is still listening` in the log. What the op must never do is end
  this process while that call is in flight: see the P200 checklist below.
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
  `gaston.exe` and `gremote.exe`. Today every step hardcodes
  `gaston.exe`.
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
- `make check` gives `gremote.exe` its own committed-at-load cap: 242,761
  before the tray icon and 250,441 after it, against 264,000 (2026-10-01).
  The icon's +7,680 is about 6KB of real image and the rest PE section
  rounding, and +2,144 of the 6KB is the `-xs` exception machinery a new
  translation unit brings before any of its own code. The `win/Makefile`
  comment at `check-gremote` carries the whole series and the reason the cap
  was raised.
- `TrayService` loads SHELL32 only while the icon is up, and lets it go again
  when there is no notification area -- which is the whole of the pre-logon
  life of a RunServices copy, and SHELL32 pulls four more DLLs behind it. It
  is resolved with `LoadLibrary`/`GetProcAddress`, so the import count stays
  at three DLLs.
- The first P200 session measures `gremote` idle and under load, so later
  client measurements can subtract it.

## Build order

Each step leaves `gaston.exe` building and behaving the same, and ends
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
   *Scoped 2026-09-27 (`docs/gremote-plan.md`):* **the fs lane is built here,
   not in step 5.** libfs and its handlers landed in 5a, and 5b (`ship()`)
   waits on nothing but this exe -- so without `PUT` a new `gaston.exe`
   would still travel by CD, which is the thing this step exists to stop. The
   ui lane still waits for step 6. **Deflate is off in v0**: the only large
   payload is a screenshot and screenshots are step 6, so `HELLO` says
   `deflate=0` and `Connection` is constructed without its 192KB of scratch.
   The VM is reached with QMP `hostfwd_add` rather than a libvirt
   `<portForward>`, which libvirt accepts only on the `passt` backend --
   ours is slirp with a hand-configured `ne2k_isa`.
   **Step 4 is complete (2026-09-27), and 5b with it**, verified on the real
   guest and not only under Wine --
   `docs/measurements/gremote-vm-20260927.txt`. gremote answers the control,
   job and fs lanes; the Python package drives all three; `ship()` works.
   The measurement that earns the step: **a running exe is locked on Windows
   98**, so a plain PUT over a live image is refused and ship()'s
   kill-then-write is the only way the write happens at all. Wine does not
   hold the image and lets the PUT through, which is the class of thing
   `make wine-smoke` is documented as unable to settle.
   Left for the ui lane at step 6, and recorded in `docs/gremote-plan.md`:
   `CANCEL` (the contract is specified, nothing cancellable enough to test it
   against yet), autostart through `RunServices`, and a non-inheritable
   listening socket.
5. **libfs**, the fs lane, `PUT` with CRC and temp-rename, `ship()`,
   self-update. Split in two: **5a** is the library plus the client moved onto
   it, plus the fs lane in the client and the agent's `get_file`/`put_file`/
   `drives` tools (`docs/libfs-plan.md`). **5b** is `ship()` and self-update,
   which need `GREMOTE.EXE` from step 4 and cannot start before it.
   **Step 5a is complete (2026-09-27)**, verified on the real guest and not
   only under Wine -- `docs/measurements/libfs-vm-20260927.txt`.
   **5b is built inside step 4** (`docs/gremote-plan.md`), because `ship()`
   is the first thing anyone wants from gremote and it needs the fs lane that
   step now carries.
6. **libautomation.** Screenshot, window list, focus and input, control tree,
   MSAA, in that order. Each verified on the P200, not just Wine: window trees,
   focus and input are exactly where Wine lies.
   **Step 6 is complete (2026-10-04), on the VM and on the Athlon XP** --
   `docs/measurements/ui-lane-real-hw-20261004.txt` is 12 of 12 on real
   hardware and `ui-lane-real-hw-20261004.png` is a 1280x720 capture of that
   machine's desktop, the first non-VM image in that directory. It answered
   the two questions the P200 checklist had carried since the start (a
   process with no window CAN take the foreground; retail 98 does carry
   oleacc and returns real roles and names), gave BACKLOG 26 its number
   (37 ms, below the cost of asking for a stop), and **reversed** this
   file's own prediction about compression: `shouldCompress` declines on the
   emulator and fires on the hardware, where a 922KB screenshot goes twice
   as fast compressed. The VM run -- `docs/measurements/ui-lane-verify-vm-20261003.txt` is eleven of
   eleven on the guest, `ui-lane-vm-20261003.png` is the screenshot it took
   while the client sat beside it unable to reach its bridge, and
   `tools/probe/uilaneprobe.py` re-runs the list. What the Athlon alone can
   settle is listed at the bottom of PROGRESS.md's entry.
   *Scoped 2026-10-02.* The library landed in step 5a's wake and was proven in
   the client first, as every lib here is, so this step is the **lane**, not
   the library: `TREE`, `SHOT`, `RUN` and `STOP`, per the op table above.
   **What it is for, and it is not tidiness.** A desktop measurement on this
   hardware is currently a *build*: `keys-named-p200-20261002.txt` was taken by
   shipping `autocheck.exe` to the box and parsing its stdout over the job
   lane. The host-side probes do reach `UITREE`, but each is a bridge stub that
   waits for the *client* to dial out -- so every one needs a working
   `gaston.exe`. The case that has no answer at all is the client being the
   thing that is wrong: the wedge, the GPF and the shutdown hang were each
   diagnosed over QMP, and the Athlon has none. gremote is the only process
   still answering then, and until this step it could not look at the screen.
   **The engine is shared, not copied.** `win/ui/uirpc.cpp`'s coupling to
   `rc_net` was six places in 919 lines; the handlers moved to
   `win/ui/uiengine.cpp` behind `UiLink` (`win/ui/uilink.h`) and each exe
   supplies the wire. `uisteps.cpp` was absorbed rather than renamed beside it,
   so the client's translation-unit count did not change and the ~2,144 bytes
   of `-xs` machinery a new TU costs was not paid twice. `SHOT` is the one
   handler deliberately duplicated: 55 lines of header clamping over a sink
   that has to be per-exe anyway.
   **Deflate turns on here**, which step 4 deferred because the only large
   payload is a screenshot. It is armed from the handshake, not at
   construction: the Python declares `deflate=1` on `AUTH`, gremote calls
   `Connection::allowDeflate()` before `HELLO`, and the Python arms its own
   deflater strictly on *reading* `HELLO deflate=1`. A compressing peer talking
   to a non-compressing one drops the link rather than degrading, and the only
   repair path is `ship()`, which needs the link -- so arming on send would
   brick a box reachable by CD.
   *Measured 2026-10-03* (`docs/measurements/deflate-gremote-vm-20261003.txt`):
   the new Python against the OLD gremote kept a raw link and `self_update()`
   travelled over it, so the compatibility case is the path the upgrade itself
   took. A 220KB round trip is byte-exact both ways; the host-to-guest half is
   2.7x faster compressed, and the guest-to-host half barely moves because
   `shouldCompress` is `linkBps < deflateBps / 2` and declines on a LAN --
   correctly, since 220KB raw at LAN speed beats compressing it first. The
   client is untouched: `Connection` still starts both halves unless a caller
   defers, and only gremote does.
   **Two desktop drivers is a documented hazard, not a guarded one.** The
   client and gremote can both run and both drive; `Input::isOurs` compares PID
   and so refuses only its own process. It is a dev box driven by one person
   who knows which tool they are holding, and gremote can already `EXEC`
   anything, so this grants no power it did not have. A named
   `GastonDesktopDriver` mutex is the fix if it ever bites. See
   `BACKLOG.md` 19.

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

- `oleacc.dll` present, and its version. **ANSWERED 2026-10-04: present on
  the Athlon XP's retail 98**, and it answers properly rather than merely
  loading -- the walk comes back with roles (list, pagetablist, pushbutton,
  text, toolbar) and names ("Start"), where Wine's returns the generic client
  object for every class. `HELLO msaa=` says so on every connection.
- Foreground lock: **ANSWERED 2026-10-04 on the Athlon XP, yes**
  (`docs/measurements/ui-lane-real-hw-20261004.txt`). With Progman in front,
  a `focus` step asked for by gremote -- a process with no window of its own
  on the screen -- put Notepad in the foreground, and `Input::focus`'s
  AttachThreadInput fallback was not needed. The P200 itself is still a
  different machine. *Narrowed 2026-10-02:* the lock is real on 98 and
  measured at 15000 ms, and `Input::focus` already relaxes it around the one
  call that needs it and puts it back (`win/automation/input.cpp`). What is
  untested is whether that works from a process with **no window on screen,
  started before anyone logged on** -- which is exactly what gremote is, and
  what its ui lane now asks on every `focus` step.
- `COMMAND.COM /c` exit codes: is the child's errorlevel returned. *Answered
  on the VM (2026-09-26, `docs/measurements/execprobe-vm-gui-20260926.txt`):
  no, always 0, even for a program that does not exist; a direct spawn
  returns the real code. The P200 only needs to confirm.*
- Handle inheritance of sockets into children on 98. *Answered on the VM
  (same file): a listening socket is not kept alive by an inheriting child;
  closing it in the parent frees the port. Wine and NT differ.*
- `bind` after a restart: TIME_WAIT behaviour.
- **The tray icon, end to end**, which Wine cannot answer. Boot with no one
  logged on (`gremote` up from RunServices, `EXPLORER` not running) and check
  `GREMOTE.LOG` says "no notification area yet"; then log on and check the
  icon APPEARS -- that is the `TaskbarCreated` broadcast reaching a window
  created before the shell existed, and it is the one part of
  `win/remote/tray.cpp` with no test behind it. Then kill and restart
  `EXPLORER` and check the icon comes back. Also: that the stock
  `IDI_APPLICATION` at `SM_CXSMICON` looks like anything at 16x16 on a real
  98 desktop, that the menu dismisses on a click outside it (the
  `SetForegroundWindow`/`PostMessage` pair), that Exit leaves no ghost icon
  behind -- and neither does a shutdown, which takes the same
  `signalStop()` path with nothing joined.
- **Prevent sleep, and it is the one item here with an open question behind
  it.** `win/remote/tray.h` argues the four mechanisms; what no document
  settles, and what Wine and the QEMU guest cannot settle either (that guest has
  **no APM at all**, which is why `mode=poweroff` there degrades to a logoff),
  is whether Windows 98 asks an application's permission when its OWN standby
  timer expires. Three runs answer it, in this order:

  1. ~~`probe power`~~ **-- DONE 2026-10-02 on the Athlon XP**
     (`docs/measurements/sleep-mechanisms-p200-20261002.txt`).
     `SetThreadExecutionState` is **PRESENT** on Windows 98 SE and returned
     `80000000`, so the nudge is available and the input-faking fallback is not
     needed. Confirmed on **two** machines, because the Athlon has an
     autopatcher that could have added it: the stock retail QEMU guest reports
     the same presence and the same value. Both veto handlers answer correctly: `WM_POWERBROADCAST` ->
     `BROADCAST_QUERY_DENY`, `WM_POWER` -> `PWR_FAIL`. The probe SYNTHESISES
     both messages, so this proves the handlers and not that Windows ever sends
     them -- runs 2 and 3 below are still the ones that matter.
  2. **The clean measurement.** `prevent_sleep=0`, Power Management set to its
     shortest system standby, and leave the box alone past it. `gremote` logs a
     suspend query **even when it is going to say yes**, exactly so this
     measurement does not change the machine's behaviour. A
     `the machine asks to suspend (...)` line means an idle standby does ask,
     and which message it names settles `WM_POWERBROADCAST` versus the obsolete
     `WM_POWER`. **Silence means no veto can ever help**, and the nudge is the
     only layer that can.
  3. `prevent_sleep=1`, same wait. `REFUSED` then
     `the suspend was abandoned` is the veto working.
     `the machine is suspending ANYWAY` is it being asked and ignored. Neither
     line, and the box still up, is the nudge doing it -- and
     `the first nudge went out` near the top of the log is what says the nudge
     was running at all. That line exists because the nudge is the one mechanism
     of the four with **no automated coverage anywhere**: it is used only when
     `SetThreadExecutionState` is absent, and under Wine, where every `gremote`
     test runs, that call is present.

  Also at the keyboard: that **Prevent sleep** is checked on the menu and
  `[awake]` is in the tooltip; that pressing Standby from Start > Shut Down with
  it on gets 9x's "some application or device has rejected the suspend"
  complaint (Intel's 1997 APM paper says it will, and that is the option
  working); and that **tray > Exit writes the `Prevent sleep OFF` line with its
  count** -- the Wine suite cannot see that one, because `GuestServer` stops the
  guest by terminating the process and no teardown runs
  (`tests/python/remote/test_sleep.py` says so).
- `gremote` memory idle and under load, with and without the client running.
- Whether `rsrc32.dll`'s free-resources export works, if we want USER/GDI numbers in `SYSINFO`.
- **Shutdown with `gremote` installed and running.** *Observed on the real
  P200, 2026-10-01: the machine hung on the way down -- "stuck with no
  desktop", no reboot -- and took two Ctrl+Alt+Dels to force. The reboot had
  been asked for over `gremote`'s own connection.*

  The cause was `App`'s `WM_ENDSESSION` handler joining its services from
  inside the window procedure. Windows waits for that procedure before it
  carries on, so every join budget is a 9x shutdown held up, and in this case
  a deadlock as well: the handler waited on the accept thread (8 s), the accept
  thread on `Connection::join` for the reader (3 s), and the reader was inside
  `ExitWindowsEx`, which does not return and whose broadcast was
  `SendMessage`ing the very window whose handler was waiting. Broken only by
  the timeouts firing, which is seconds of frozen USER with no desktop to
  report anything on. The same shape was worse in the client, where
  `ConnectionService::join` hands the job to `rc_net_stop` and ignores the
  App's budget entirely: about three quarters of a minute.

  Fixed by never joining on that path (above). **Only the real box can
  confirm the fix**: Wine has no 9x shutdown and no Win16Mutex, so
  `appcheck` can prove the handler returns promptly and nothing more.

  **That was half of it. The other half was leaving at all.** Not joining made
  the handler return in 40-70 ms and the machine still hung, four runs out of
  four on a clean Windows 98 guest against two builds
  (`docs/measurements/gremote-shutdown-vm-20261001.txt`). On 9x the process
  that calls `ExitWindowsEx` DRIVES the shutdown: the call does not return, the
  broadcast runs out of it, and the 16-bit USER code it is executing holds
  Win16Mutex between the sends. Returning promptly and then `ExitProcess`ing a
  second later killed that driver mid-broadcast, after Explorer had gone and
  before Systray was dealt with -- bare teal desktop, one unanswering task, an
  idle CPU parked at `EIP=00006a2a`, and no `End Task` releasing it. A
  `rundll32` gremote launched to make the same call rebooted the same guest in
  three seconds, because it stays parked inside `ExitWindowsEx`.

  So **on a session end this process asked for, `App` does not
  `PostQuitMessage`**: it tears down exactly as above, returns, and the message
  loop keeps running until Windows ends the process itself.
  `ControlOps::reboot` says so with `noteSessionEndAsked(true)` before the
  call, and clears it if the call comes back refused. A session end somebody
  ELSE asked for still quits, because there the driver is the shell's thread.
  Eight fixed runs rebooted and two unfixed runs hung, interleaved, same boot
  pattern (`docs/measurements/gremote-shutdown-fix-vm-20261001.txt`).

  **Staying brings an obligation: a session end can fail to take.**
  `mode=poweroff` on a machine with no power management runs the whole
  teardown, cannot switch the machine off, and returns to the logon prompt --
  measured. Staying then leaves gremote with every service abandoned, nothing
  listening and `GastonRemoteSingleInstance` still held, so no replacement can
  start: a box that answers nothing and cannot be told to, which is worse than
  the hang. `GremoteApp` therefore arms a 15 s timer in `stopServices()` on
  that path and quits when `Shell_TrayWnd` comes back, or after three minutes
  regardless. The shell signal alone was measured insufficient, because a
  failed poweroff lands on the logon prompt where Explorer is not running
  either. The watch lives in gremote and not in `App` because in libcore it
  cost `gaston.exe` 4,608 bytes of committed-at-load for a path the client can
  never take.

  **Read `GREMOTE.PRV`, not `GREMOTE.LOG`.** Forcing the restart is how the
  hang is recovered, and the restarted `gremote` truncated its own log -- so
  the evidence was destroyed by the recovery, twice, before `rc_log_open`
  learned to rename the old file first (`win/core/log.cpp`). `.LOG` is the run
  that came up after the reboot; `.PRV` is the one that went down.

  What to look for in it:

  - `app: Windows asks whether the session may end (a shutdown or restart);
    yes`
  - `app: Windows is ending the session (a shutdown or restart); signalling
    every service and leaving this handler WITHOUT joining any of them`
  - `app: the session is ending; abandoning server unjoined`
  - `app: every service is signalled and abandoned; cleaning up`
  - `=== gremote exiting ===`
  - `app: the session-end handler is returning to Windows after N ms; this
    process stays up to drive the shutdown it asked for`

  All of them, with a small N, and the machine reaching the reboot. **That last
  clause is the one to read.** It is `nothing below this line is ours` when the
  session end was somebody else's -- a Start-menu restart, a logoff -- and
  `this process stays up to drive the shutdown it asked for` when it was
  `REBOOT`'s. A `REBOOT` whose log says the former is a build without the
  stay-alive fix, and that machine is going to wedge.

  Two further lines appear only when the shutdown DID NOT HAPPEN, and seeing
  either on the P200 is a finding:

  - `gremote: the shell is still gone a minute after the session end, ...`
  - `gremote: three minutes after the session end this machine is still here,
    so nothing is left to drive; exiting so a new copy can be started`

  Each line that is MISSING names a different failure, which is the point of
  having six rather than two: no first line at all means the broadcast never
  reached us;
  the first without the second means `WM_ENDSESSION` never arrived; the second
  without `cleaning up` means it faulted while signalling, and the
  `abandoning` lines say how far it got; `cleaning up` without the banner
  means it faulted in `cleanup()`; and the last line -- written after the log
  was closed, by reopening it -- is the only thing that distinguishes a clean
  return to Windows from a fault on the way out of the handler.
- **Shutdown with the client running**, same question, same log lines in
  `GASTON.PRV` (`abandoning desktop` / `connection` / `main window` / `ole`
  `unjoined`). The client had the longer wait of the two, so if a desktop ever
  hung on the way down with `gaston.exe` up, this was why.
- **Does `gremote` survive a LOGOFF, as `RegisterServiceProcess` promises?**
  Found while fixing the shutdown hang, and not answered by anything here.
  *A full shutdown does reach it: the VM run of 2026-09-27
  (`docs/measurements/gremote-vm-20260927.txt` §8) recorded its hidden window
  taking `WM_ENDSESSION` while registered as a service process, so
  registration buys no exemption there.* A logoff is the untested half. 9x
  broadcasts the same two messages at logoff, and `RSP_SIMPLE_SERVICE` is
  supposed to keep a service process out of that broadcast -- but if it does
  not, `App` treats the message the only way it can and quits, and "it will
  outlive a logoff" in `GREMOTE.LOG` is a claim the program does not keep.
  §9 of that same file proves only that the process runs *before* a logon, not
  that it survives a logoff.

  To test: connect, log off at the console, and see whether the port still
  answers. If it does not, `GREMOTE.PRV`'s last lines will say which message
  arrived, and both handlers now print `(a logoff)` or `(a shutdown or
  restart)` from `WM_ENDSESSION`'s `lParam` -- so the measurement is a matter
  of reading the file. ACTING on the bit, by ignoring a logoff, is a
  judgement about what it means on 98 and still wants that measurement
  first. Note this is
  unchanged by the 2026-10-01 fix: that changed only whether the handler
  waits, never which messages lead to a quit.

## Open decisions

- ~~**Deflate on by default.**~~ **Settled 2026-09-27: off.** The only large
  payload is a screenshot and screenshots are step 6, so v0 constructs
  `Connection` without compression -- which also leaves out the 192KB of
  persistent scratch, real on a 32MB box -- and `HELLO` reports `deflate=0`.
  A compressing peer talking to a non-compressing one drops the link rather
  than degrading (`wirecheck`'s `refusal()` case), so the Python client must
  never set `enc=deflate`. Turning it on means importing
  `bridge/wire/compress.py`, which is stdlib-only and already keeps one
  persistent stream per direction.
- **`BI_RLE8`** for screenshots (`BACKLOG.md` 8).
