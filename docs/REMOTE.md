# Remote control of the guest: `gremote.exe` and the static libraries

**Status:** planned 2026-09-25. Referenced from `PLAN.md` as R9.

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
  no UI, and `win/rpc/exec.cpp` already solves output capture better.
- **retro-agent as-is.** Covers most of the surface, but its protocol and code
  are not ours; we already took ideas from it (`FORKED.md`), and its `input.c`
  is still the planned source for input code.
- **PiKVM.** The only option with zero guest footprint, and the only one that
  can see a blue screen or power-cycle. Kept as a possible later addition for
  crash recovery, not a replacement: it cannot exec.

The decision is our own server exe, built from static libraries that the client
also links. That gets us three things at once: a test tool that works out of
band from the agent client, the Phase 6 UI primitives written and proven on real
hardware before they become agent tools, and a reason and a pattern for
untangling `win/app/main.cpp` (1976 lines of file statics).

## Shape

```
  Linux                                   Win98 (server this time)
  ------------------------------          -----------------------------------
  test script / REPL                      gremote.exe  (win/remote/)
      |                                       composition root + handlers
  gremote  (Python package)                   |     |      |      |
      blocking socket client  <--- TCP --->  libwire libexec libfs libui
      frame codec (shared                     \______|______|______/
      with bridge/wire)                                libcore
```

Direction is reversed relative to the client: `gremote.exe` listens and the
Linux side connects. One client at a time.

### The libraries

| Lib | Dir | Contents | Depends on |
|---|---|---|---|
| `libcore` | `win/core/` | `rc_cpp.h` (Buf, RAII guards), log, str, path, **new** generic INI reader, **new** `App` / `Service` lifecycle, thread helper over `_beginthreadex` | Win32 only |
| `libwire` | `win/wire/` | frame grammar (`frameproto` moves here), deflate, `Connection` (socket + reader/sender threads + bounded out queue), `Listener`, lane dispatcher | libcore, miniz |
| `libexec` | `win/exec/` | `exec.cpp` (moved, unchanged API), **new** process list/kill via Toolhelp32 (resolved with `GetProcAddress`: present on 98, absent on NT4), **new** `launch` (detached, visible) | libcore |
| `libfs` | `win/fs/` | list/stat/read/write/remove/mkdir/move, chunked read and write, drive and free-space info | libcore |
| `libui` | `win/ui/` | screenshot (from `app/screenshot.cpp`, plus retro-agent's 8bpp palette fix), window enumeration, control tree, MSAA metadata, mouse and keyboard input | libcore |

Rules for the libraries:

- **No lib knows the wire except libwire, and libwire knows no ops.** libexec,
  libfs and libui expose plain C++ APIs that take a sink for output. Parsing a
  frame into a call and a result into frames is the exe's job. That is what lets
  the client and the server share them with different trust models: the client
  gates `exec` behind the permission bar, the server gates everything behind
  the shared secret.
- **No lib creates windows or owns a message loop.** Where a thread needs a
  message queue (UI input, MSAA), the lib documents it and the exe provides the
  thread.
- **The guest transforms nothing** still holds. Screenshots leave as DIB rows,
  trees as flat `key=value` records, directory listings as records. PNG, JSON,
  pretty tables all happen in Python.
- Each lib has a check exe under `win/checks/` that runs under Wine
  (`make wine-smoke`), in the style of `rpccheck`/`netcheck`.
- Each lib is built with `wlib -q -b -n`; the check exes link the libs instead
  of repeating `log.o str.o path.o miniz.o` object lists.

## C++ structure

The dialect stays what `rc_cpp.h` says: C++98 as Open Watcom implements it, no
exceptions, no RTTI, no STL, trivial templates only, **no file-scope objects
with non-trivial constructors**. Within that:

### Naming and layout

The client is not consistent, so new code does not copy it. New and moved C++
code uses:

- **PascalCase** for classes, structs, enums and typedefs: `App`, `Service`,
  `Connection`, `ServerConfig`. No `rc_` prefix on C++ types; `rc_cpp.h`
  (`Buf`, `ScopedLock`, `Unique`) already works this way.
- **camelCase** for methods, functions, locals and parameters: `addService`,
  `exeDir`. Members keep the `m_` prefix: `m_listener`.
- **UPPER_SNAKE** for macros and constants, as today.
- **Allman braces**, 4-space indent, no tabs.
- One class per header/source pair where the class is public; file names stay
  short and lowercase (`connection.cpp`), since they have to survive 8.3 tools.
- C translation units (`str.c`, `path.c`) and their `extern "C"` APIs keep
  `rc_snake_case`: that is C's only namespace.
- Existing client code is renamed when it moves into a lib or is refactored
  (build step 7), not in a sweep. A mass rename would bury every real change
  in the diff.

### Services and lifecycle

```cpp
/* core/app.h */
class Service
{
public:
    virtual ~Service() {}
    virtual const char *name() const = 0;
    virtual bool init() = 0;               /* acquire resources; false aborts startup */
    virtual bool start() { return true; }  /* start threads, begin listening */
    virtual void stop() {}                 /* signal and join; idempotent */
};

class App
{
public:
    static App *create(const char *exeDir);
    ~App();                                /* deletes services in reverse order */

    bool addService(Service *service);     /* fixed capacity; registration order = dependency order */
    int run();                             /* init all, start all, message loop, stop in reverse */
    void quit(int exitCode);               /* posts WM_QUIT; safe from any thread */

private:
    App(const App &);
    App &operator=(const App &);
    ...
};
```

- **Two-phase construction.** Constructors only store dependencies and cannot
  fail. Anything that can fail (heap, sockets, threads, DLLs) happens in
  `init()`/`start()`, which return `bool`. This matches the existing
  `static create()` returning null on OOM (`app/reconnect.cpp:29`).
- **`App` is the container: it owns the services and their lifecycle.**
  If `init` fails partway, only the services that were initialised are stopped
  and deleted, in reverse order. `stop` is idempotent so a partial start is safe.
- **Dependency injection through constructors, from one composition root.**
  `win/remote/main.cpp` has a single `buildApp()` function that creates every
  service, passes each its dependencies by reference or pointer, and registers
  them in order. No service looks another up by name at runtime. Without RTTI
  and templates, a type-keyed registry would be a service locator with casts;
  the composition root is the honest form of DI here, and it keeps the wiring
  readable in one screen.
- **Interfaces only at real seams.** Abstract classes where there is a second
  implementation or a test fake: the frame sink, the socket (so libwire tests
  run without Winsock), and the handler interface. Everything else is a
  concrete class. Every virtual call costs a vtable and nothing is gained by
  making `FileSystem` swappable.
- **Callbacks take a context.** `sink(void *ctx, ...)` as `exec.h` already does,
  or a one-method interface. No context-free function pointers (the pattern in
  `app/reconnect.h` that forces globals).
- Non-copyable by private undefined copy constructor and assignment, as today.

### Configuration

`gremote.ini` sits next to the exe (8.3 names: `GREMOTE.EXE`, `GREMOTE.INI`).

```ini
[server]
bind=0.0.0.0
port=7900
secret=change-me
allow=192.168.1.0/24     ; peers outside this are closed before AUTH

[exec]
max_timeout_ms=120000

[ui]
msaa=1                   ; 0 skips oleacc.dll even if present
max_tree_nodes=2000

[log]
file=gremote.log
level=info
```

- `libcore` gets a generic reader over `GetPrivateProfileStringA`/`IntA` with
  typed getters, defaults and bounds, and truncation reported through `Buf`.
  `win/core/ini.cpp`'s `rc_config` stays for the client until the client's own
  refactor.
- **The composition root loads the INI once into per-service config structs**
  (`ServerConfig`, `ExecConfig`, `UiConfig`) and hands each service its
  struct. Services never read the INI themselves.
- **Invalid values refuse startup** with a log line and a message box, the
  same rule as the handshake: never guess. `secret=change-me` counts as invalid.

## Protocol

Same frame grammar as the client (`uint32le len | TYPE\n k=v\n ... \n\n body`,
64KB max frame, percent-encoded values), so the codec is shared on both sides.
Separate op namespace and its own version number: this is not the bridge
protocol and must not drift into it.

- **Handshake.** Client connects and sends `AUTH ver=1 secret=...`. Server
  replies `HELLO` with `ver`, `os`, `cpu`, `mem_kb`, `mem_free_kb`, `screen`,
  `bpp`, and one header per capability (`msaa`, `toolhelp`, `deflate`), each a
  small integer enum as the client does. Wrong secret or version: `ERR`, then
  `shutdown(SD_SEND)` before `closesocket` (an abrupt RST is reported to crash
  Win98's Winsock). A second connection while one is live gets `ERR busy` and
  the same graceful close.
- **Requests carry `id`.** Responses are `OK id=` or `ERR id= code= msg=`.
  Streaming ops send `DATA id=` chunks then `END id=`. `CANCEL id=` cancels one
  request (fixing the client's id-ignoring EXECKILL, `BACKLOG.md` 9).
- **Lanes**, one worker thread each, one request in flight per lane, `ERR busy`
  otherwise:

| Lane | Thread | Ops |
|---|---|---|
| control | reader, inline | `PING`, `CANCEL`, `SYSINFO`, `QUIT` |
| job | job worker | `EXEC`, `LAUNCH`, `PS`, `KILL` |
| fs | fs worker | `LS`, `STAT`, `GET`, `PUT`, `RM`, `MKDIR`, `MV`, `DRIVES` |
| ui | ui worker (has a message queue, `CoInitialize`d) | `SHOT`, `WINDOWS`, `TREE`, `CLICK`, `DRAG`, `MOVE`, `KEYS`, `TYPE`, `FOCUS`, `CLOSEWIN` |

So a long `EXEC` never blocks a screenshot, and `CANCEL` always gets through.

### UI details decided now

- **MSAA is loaded, never imported.** `LoadLibrary("oleacc.dll")` +
  `GetProcAddress("AccessibleObjectFromWindow")`. Win98 is believed to ship
  it (MSAA 1.x); Win95 and NT4 did not. Verify on the P200. When absent,
  `TREE` falls back to the plain window tree and `HELLO` says `msaa=0`.
- **Every cross-process call has a timeout.** Window text through
  `SendMessageTimeout`, and a per-node deadline on the MSAA walk, because a hung
  target app otherwise hangs the ui lane. Node cap from the INI; the walk
  streams records instead of building the tree in memory.
- **Tree record fields:** hwnd, parent, class, text, rect, style, visible,
  enabled, and with MSAA: role, name, value, state, default action, location.
- **Input** from retro-agent's `input.c` (`FORKED.md`: keep the E0 extended
  scancode table and the `VK_SNAPSHOT` fallback). Click checks the target point
  is not covered by another window before sending (`PLAN.md` Phase 6).
- **Screenshot** as DIB strips in `DATA` frames, deflated when negotiated.
  Whether to add GDI `BI_RLE8` is the open question from `BACKLOG.md` 8; start
  without it.

## Python client

A plain blocking-socket package, `gremote/` at the repo root. No asyncio.

```python
from gremote import Remote

with Remote("192.168.1.50", secret="...") as r:
    res = r.exec(r"C:\WINDOWS\COMMAND.COM /c mem", timeout=20)
    print(res.exit_code, res.output)
    r.put("build/win/retroclaude.exe", r"C:\Gaston\retroclaude.exe")
    for p in r.ps(): ...
    r.kill(pid)
    img = r.screenshot()                  # PIL.Image
    for w in r.windows(): ...
    node = r.tree(hwnd).find(role="push button", name="OK")
    r.click(node)                         # or r.click(x, y)
    r.type("hello"); r.keys("alt+f4")
```

- The frame codec moves out of `bridge/wire/protocol.py` into a dependency-free
  module that both `bridge` and `gremote` import. Today `protocol.py` pulls in
  `fold` and `files.caps`; `link.py` pulls in the view layer.
- Tests live in `tests/python/remote/`.
- A `ship()` helper covers the flow `vmship.sh` does for the VM: close the
  client, upload, relaunch. Your notes on the locked-exe and single-instance
  traps become code, not memory.

## Memory budget

`gremote.exe` runs next to the client it is testing, so its footprint shows up
in every measurement taken while it is running.

- Five threads (reader, sender, job, fs, ui) with explicit small stack
  reserves in `_beginthreadex`.
- One 64KB inbound frame buffer, one bounded out queue, the 38KB screenshot
  strip buffer, exec's 8KB chunk. No per-request heap growth; streaming
  everywhere.
- `make check` runs its import audit, CMOV scan, static-constructor check and
  committed-at-load cap against `gremote.exe` too, with its own cap.
- The first P200 session measures `gremote.exe` idle and under load, so later
  client measurements can subtract it.

## Build order

Each step leaves `retroclaude.exe` building and behaving the same. Each ends
with `make check`, the Wine tests, and a guest run.

1. **libcore.** Move log/str/path/`rc_cpp.h` into a lib, add the INI reader and
   `App`/`Service`. Relink the client and the check exes against it. No
   behaviour change.
2. **libwire (server side first).** Move `frameproto`, add `Connection`,
   `Listener`, the lane dispatcher. The client keeps `net.cpp` for now and
   only its `frameproto` include moves. Extract the Python frame codec.
3. **libexec.** Move `exec.cpp`; add process list, kill, launch.
4. **`gremote.exe` v0 + Python client.** App, config, listener, AUTH/HELLO,
   control and job lanes. Integration test: pytest starts `gremote.exe` under
   Wine on localhost and drives it with `gremote`. Ship to the VM, then the
   P200. **This is the first useful milestone: ssh-equivalent on the real box.**
5. **libfs** and the fs lane, plus `ship()`.
6. **libui.** Screenshot, window list, then input, then the control tree, then
   MSAA. Each verified on the P200, not just Wine: window trees and input are
   exactly where Wine lies.
7. **Client refactor.** `retroclaude.exe` gets an `App` composition root.
   `main.cpp`'s file statics become services; `net.cpp` moves onto `Connection`;
   rpc onto the lane dispatcher; screenshot attach onto libui. The Phase 6 agent
   tools then need only bridge-side work plus handlers. This is its own phase
   and gets its own plan when we reach it.

## Open decisions

- **Updating `gremote.exe` itself.** A running exe cannot be overwritten. Options:
  `WININIT.INI [rename]` plus a reboot (native to 9x, slow), or a copy of itself
  run from `%TEMP%` that waits for the old PID to exit, replaces the file and
  relaunches. Leaning to the second.
- **Autostart.** A StartUp folder shortcut vs the `Run` registry key.
- **Auth strength.** A plain shared secret on an isolated LAN matches the
  client. A challenge-response would keep the secret off the wire for little
  code; decide before step 4.
- **Deflate on by default.** Probably off on LAN; screenshots are the only large
  payload.
- **`BI_RLE8`** for screenshots (`BACKLOG.md` 8).
