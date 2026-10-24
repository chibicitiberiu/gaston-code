# gremote: remote control of a Windows 9x machine

`gremote.exe` is a server for Windows 95/98/Me, driven from Python on a modern
host. It gives you something that feels like ssh -- run a command, read its
output, move files, list and kill processes -- plus desktop control:
screenshots, the window and control tree, clicks and keystrokes.

It exists because a physical Windows 98 PC has no QMP. On QEMU the VM is
driven out of band (`tools/vm/qmp.py`); on real hardware there is no such
channel, and a person standing at the machine does not scale.

**It opens a port that hands whoever presents the secret the ability to run
programs and type at the desktop, with the secret sent in clear.** That is the
whole security model: a trusted network segment, an `allow` line, and a secret
you generated. See `## Configuration`.

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
| `libexec` | `win/exec/` | `ExecJob` (command execution, from `exec.cpp`, per-job), `Processes` (list/kill/launch via Toolhelp32, resolved with `GetProcAddress`: present on 98, absent on NT4), `readSystemInfo`, `requestReboot`. `notes/libexec-plan.md` | libcore |
| `libfs` | `win/fs/` | list/stat/read/write/remove/mkdir/move, chunked transfer (the sink extracted from `export.cpp`), drives and free space | libcore |
| `libautomation` | `win/automation/` | screenshot, window enumeration, control tree, MSAA metadata, mouse and keyboard input | libcore |

The **CRLF/binary classifier and its dual CRC stay in `win/sync/`**, not in
`libfs`. They are bridge agreement rather than filesystem behaviour: they
mirror `bridge/files/paths.py` and honour `.gitattributes`, which libfs has
never heard of, and `gremote`'s `GET` is byte-exact. libfs gives them a
one-pass read hook (`fs::ByteSink`) so nothing reads a file twice.

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
- Each lib has a check exe under `tests/guest/` that runs under Wine
  (`make wine-smoke`), in the style of `rpccheck`/`netcheck`.

### Services and lifecycle

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
; 1 raises the "Gaston Remote is controlling the desktop" strip while a step
; list runs, with a Stop button and Ctrl+Alt+Shift+F11. 0 for a box nobody
; watches -- and for a test harness, whose desktop belongs to somebody
indicator=1
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

  **One honest limit:** whether Windows 98 asks an application's permission
  when its *own* standby timer expires is unverified. Both veto handlers are
  implemented (`WM_POWERBROADCAST` to `BROADCAST_QUERY_DENY`, `WM_POWER` to
  `PWR_FAIL`) and `SetThreadExecutionState` is present on 98 SE, so the nudge
  is the layer known to work.
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
  (possible TIME_WAIT after a restart) before refusing to start. In practice
  TIME_WAIT has never been observed to arise -- repeated `UPDATE`s inside four
  minutes each logged `listen: on 0.0.0.0:7401` straight away -- so the 240s is
  a ceiling, not a cost.
- **Deflate is armed from the handshake, not at construction.** The Python
  declares `deflate=1` on `AUTH`, gremote calls `Connection::allowDeflate()`
  before `HELLO`, and the Python arms its own deflater strictly on *reading*
  `HELLO deflate=1`. A compressing peer talking to a non-compressing one drops
  the link rather than degrading, and the only repair path is `ship()`, which
  needs the link -- so arming on send would brick a box reachable only by CD.
  `shouldCompress` is `linkBps < deflateBps / 2`, so it declines on a LAN and
  takes effect on a slow link.

### Requests, lanes and cancellation

- **Requests carry `id`.** Responses are `OK id=` or `ERR id= code= msg=`.
  Streaming ops send `DATA id=` chunks then `END id=` with a summary.
- **Every request is tagged with the connection generation.** Output from an
  older generation is dropped, so a job still finishing for a dead client never
  lands in the new client's stream (Python restarts ids at 1).
- **`CANCEL` follows 9P's `Tflush`**: its ack means the
  request will never be answered, and the id cannot be reused before the ack.
  It compares the id under the lock, so a cancel cannot kill the wrong job.
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

| Op | Request headers / body | Reply | Errors |
|---|---|---|---|
| `AUTH` | `ver`, `secret` | `HELLO` (above) | `bad_secret`, `bad_version`, `not_allowed`, `busy` (another client is live) |
| `PING` | none | `OK`; also serves as the heartbeat | none |
| `CANCEL` | `target` (the id to stop); `id` is its own | `OK id=` (target is dead, and will never be answered) | `no_such_id` |
| `SYSINFO` | none | `OK` with `mem_free_kb`, `uptime_ms`. No USER/GDI free-resource numbers: `GetFreeSystemResources` is 16-bit only (`ARCHITECTURE.md`, Toolchain facts) | none |
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
  behind it answered `busy`.
  **Since 2026-10-14 there is a third raiser and it is not on the wire at
  all**: the driving indicator's Stop button and chord, pressed at the
  keyboard (`win/automation/indicator.cpp`). The request still answers
  `cancelled` to whoever sent it, which is the point -- the caller is told its
  list stopped, by the same path a `STOP` would have used.
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

| Op | Request headers / body | Reply | Errors |
|---|---|---|---|
| `UITREE` | `scope=desktop\|foreground\|pid`, `pid`, `max_nodes`, `text`, `msaa`, `hidden`, `call_ms`, `timeout_ms`, optional `wait_for=` + `wait_ms`, optional `settle_ms`; or `acc=1` + `target=` for one control's MSAA items; or `inproc=1` + `target=` + `items=` for the in-process tier | `UIENT` records (see below) with `n=` per frame; `UIEND nodes=, walk=complete\|deadline\|maxnodes\|stopped, enriched=, dropped=, too_deep=, self=` | `bad_selector`, `not_found` (+`step=`), `ambiguous` (+`detail=` and up to three `cand1..3=`), `truncated`, `no_target`, `no_msaa`, `no_memory`, `busy`, `automation_off` |
| `UISHOT` | `x`, `y`, `w`, `h` (a width and a height, not a right and a bottom), `bpp` | `UIDAT` chunks of a native-bpp BMP; `UIEND w=, h=, bpp=, bytes=` | `no_memory`, `failed` (`detail=` says which) |
| `UIRUN` | `timeout_ms`, `step_ms`, optional `settle`; **body is the step list** -- one step per line, `verb` then TAB-separated `key=value`, every value percent-encoded | `UIEND status=, done=, step=, detail=, cand1..3=`, and a **body** of one `i= status= ms= hit= detail= part=` line per step that ran, plus `released=1` when the list left something held | `bad_steps` (+`detail=`), `cancelled`, `script_timeout`, `timeout`, `focus_lost`, `bad_key`, `bad_char`, `refused_self`, `no_window`, `not_foreground`, `occluded`, `not_visible`, `not_edit`, `no_item`, `ambiguous_item`, `no_text_match`, `ambiguous_text`, `prompted`, `still_open`, `refused_chord` (the driving indicator's own cancel chord, which a synthesised key would fire exactly as a real one does), plus every `UITREE` error for the resolution it does |
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
  (`research/measurements/execprobe-vm-gui-20260926.txt`): `COMMAND.COM /c` on 98
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
`research/measurements/focus-vm-20260927.txt`. `Input::focus` relaxes
`SPI_SETFOREGROUNDLOCKTIMEOUT` around that one call and puts it back, because
leaving it at 0 is a change to somebody's machine that outlives us.

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
- Tests live in `tests/python/remote/`.
- A `ship()` helper replaces the `vmship.sh` flow on real hardware: close the
  client, upload, relaunch. The locked-exe and single-instance traps become
  code, and the first is not optional: **a running exe is locked on Windows
  98**, so a plain `PUT` over a live image is refused and the kill-then-write
  is the only way the write happens at all. Wine does not hold the image, so
  this is a trap Wine cannot show you.
- `PUT` writes a temp file, checks the CRC from `END`, then deletes the target
  and renames (`MoveFileExA` is on the deny list). A truncated
  `gaston.exe` is a bricked client.

## Where things go on a target machine

    C:\GASTON\      BOTH programs: the client (gaston.exe, gaston.ini,
                     view.htm, spin.gif, tools_detect.ini, gaston.log,
                     gaston.prv) and the server beside it (gremote.exe,
                     gremote.ini, gupdate.exe, gremote.log, gremote.prv)
    C:\WORK\        the client's work root, and NOTHING else. Conversation
                     folders live under it; it must never contain either
                     program, or the client mirrors itself to the bridge
    C:\TEMP\GR\    scratch for a test session, and the only place a test
                     may write. Emptied at the end of the session that
                     made it

Three directories. Anything a test leaves outside them is a bug in the test.

Both programs share `C:\GASTON` rather than getting one each, because
`win/remote/control.cpp` refuses `UPDATE` unless `gupdate.exe` sits **beside
the running image**. The server's directory is already a two-file directory,
so a third place for one program buys nothing.

If the client declares no work root, nothing is invented: the agent is told
to ask, and told that a drive root is never the answer.

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
  - **Rollback covers a load failure, not a silent one.** `gupdate` can tell
    that a new image failed to load; it cannot tell that one loaded and then
    never answered. `GREMOTE.OLD` is one rename away at the keyboard.
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
- **Two desktop drivers is a documented hazard, not a guarded one.** The client
  and gremote can both be running and both be driving. `Input::isOurs` compares
  pid, so it refuses only its own process.

## Operations

**Read `GREMOTE.PRV`, not `GREMOTE.LOG`.** `.LOG` is the run that came up
after the reboot; `.PRV` is the one that went down. `rc_log_open` renames the
old file at startup, which is the only reason a crashed run leaves evidence.

The line that tells you a session end was handled correctly:

    app: the session-end handler is returning to Windows after N ms; this
    process stays up to drive the shutdown it asked for

`nothing below this line is ours` in its place means somebody else asked for
the shutdown, which is also correct. Two lines appear only when the shutdown
did not happen:

    gremote: the shell is still gone a minute after the session end, ...
    gremote: three minutes after the session end this machine is still here,
    so nothing is left to drive; exiting so a new copy can be started

On that path `GremoteApp` arms a 15 s timer in `stopServices()` and quits
when `Shell_TrayWnd` returns, or after three minutes regardless. The shell
signal alone is not enough: a failed poweroff lands on the logon prompt,
where Explorer is not running either.

**`RunServices` runs once at boot, not per session**, and
`RSP_SIMPLE_SERVICE` does *not* keep a process out of the logoff broadcast on
98 SE. `App::survivesLogoff()` is the arm; without it one logoff costs the
machine its remote access until a reboot.

**A wedged `ui` thread makes the box unrecoverable over the wire.** A
`REBOOT` asked of a box with abandoned ui-lane threads does not happen at
all: no session end is signalled, the three-minute watch never arms, the 30 s
idle timeout never fires because its reader is the blocked thread, and the
box holds its single session slot against a peer that is gone. Every recovery
path gremote has runs on the thread that is stuck. Power-cycling is the only
way back.

**`tray.cpp` logs the first line of each kind and then every eighth**, so a
reading that expects one line per event will under-count.

## Building

- Object files mirror the source tree: `win/app/input.cpp` to
  `$(BUILD)/obj/app/input.o`.
- Explicit source lists per target, not pattern discovery. The object list is
  derived from the source list.
- Libs are built with `wlib -q -b -n` into `$(BUILD)/lib/`, and a target
  lists a lib among its inputs like an object. `wlib` names each member by
  the full source path, so `a/x.o` and `b/x.o` coexist.
- `make check` is parameterised by EXE / MAP / CAP and runs for every
  shipped image. The live committed-at-load budgets are in `win/Makefile`,
  which carries each one's reason.

## Memory budget

`gremote.exe` runs next to the client it is testing, so its footprint shows up
in every measurement taken while it is running.

- Five threads (reader, sender, job, fs, ui) with explicit small stack
  reserves in `_beginthreadex`.
- One 64KB inbound frame buffer, one bounded out queue, a screenshot strip
  buffer, exec's 8KB chunk. No per-request heap growth; streaming everywhere.
  The output cap is a parameter (`ExecOptions::maxOutput`; the client passes
  32KB), since the remote shell streams rather than buffers.
- `TrayService` loads SHELL32 only while the icon is up, and lets it go again
  when there is no notification area -- which is the whole of the pre-logon
  life of a RunServices copy, and SHELL32 pulls four more DLLs behind it. It
  is resolved with `LoadLibrary`/`GetProcAddress`, so the import count stays
  at three DLLs.
