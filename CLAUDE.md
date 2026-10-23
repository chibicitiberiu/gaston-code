# Gaston Code

A native Windows 98 GUI client for Claude Code (`win/`, C89, Open Watcom) talking
to a Python bridge (`bridge/`) on Linux over one TCP socket.

`docs/PLAN.md` is the design. `docs/PROGRESS.md` is what is built and why
decisions were made. `docs/BACKLOG.md` is what was deliberately deferred and
what it would take to close -- check it before assuming something was
forgotten, and before starting late-phase work. `docs/ROADMAP.md` is the order
the rest of it gets done in, and which decisions gate which stage.

## Standing rules

**Tool names are OS-neutral.** The in-process MCP server is `machine` and its
tools are bare verbs -- `mcp__machine__exec`, not `win98_exec`. Only the guest
binary is OS-specific; the bridge, the protocol and the tool surface should
survive being pointed at MS-DOS, Windows 3.1 or NT 4. Where the OS genuinely
matters it is a runtime branch, not a name (the bridge picks `command.com` or
`cmd.exe` per call from the HELLO inventory, `bridge/agent/toolrules.py`
`shell_command()`). `docs/FEASIBILITY.md`'s `win98_*` sketch is
superseded.

**The client declares OS facts; the bridge holds none.** Protected system paths,
trusted and dangerous programs, and anything else that is true of one OS are
compiled into the client (or its INIs) and sent at handshake; the bridge
matches them as opaque patterns. The per-shell grammar in
`bridge/profiles/<shell>.toml` is the one accepted exception: shell data the
bridge already selects from the HELLO inventory. Do not add Windows paths,
8.3 rules or program names to bridge Python. `docs/OPTIONS-PLAN.md`.

**A tool earns its place only if `exec` does the job badly.** Every tool costs
context on every request. See "the exec test" in `docs/PLAN.md` §3.1.

**The guest transforms nothing.** Markdown, pagination, encoding folds, image
diffing, OCR and JSON all happen on the bridge. The guest makes syscalls and
moves bytes. Compression and the sync layer's single-pass text/binary CRLF
classification (to save disk passes on 1998 IDE platters; pinned by agreement
tests) are the two deliberate exceptions.

**Memory is the binding constraint.** Every allocation must be bounded. On a
32MB guest, physical memory and GDI/USER heaps are tight; keep buffers capped
and static allocations minimal (see `docs/mem-audit/`). Bounded does not mean
fixed at the maximum: a container sized to what is actually there beats a
`MAX_*` table held full at worst case, as long as anything the wire or the
disk can grow is still refused past an explicit cap.

**Nothing NT-only, ever.** A single NT-only static import kills the image at
load, before `WinMain`, with no log written and nothing to debug. Resolve
optional DLLs with `LoadLibrary`/`GetProcAddress`. `make check` audits imports.

**Verify on the guest, not under Wine.** Wine proves the code, never the
platform. Wine checks prove C algorithmic logic, data structures, and cross-language
agreements (~40% headless subset); FAT32 filesystem edge cases, MAX_PATH, and modal
dialogs require the real guest VM. `tools/vm/vmship.sh` ejects before inserting, because
`change-media --update` to the same path is a no-op and has served a stale
binary before.

**The person at the keyboard never loses a byte, and is never asked twice.**
On a conflict the guest's copy wins and the agent's is staged under
`.gaston/conflict/`; the agent halts and the halt bar appears. Pressing Continue
*is* the confirmation -- it lifts the refusal and the agent merges. Asking "have
you finished editing?" after Continue is a bug, not caution.

**Client rules are declared at handshake, never assumed.** `HELLO` carries one
header per capability (`acp`, `case`, `eol`, `illegal`, `maxpath`, ...), each a
small integer enum with room to grow. Do not pack them into bit fields to save
bytes; twenty bytes once per connection buys the ability to add an option
without a flag day. The bridge refuses an unknown value rather than guessing
(`bridge/caps.py`, `win/caps.h`).

**New C++ runs under App's lifecycle, not file statics.** An exe derives
from `App` (`core/app.h`) and fills its hooks: `initialize`, `parseArgs`,
`initializeServices`, `startServices`, `preTranslate`, `stopServices`,
`cleanup`. Services derive from `Service` (trivial constructor, fallible
`init`/`start`, `signalStop` then `join`) and are `UniqueObj<>` members of the
derived app, created in `initializeServices()` in dependency order with their
dependencies passed to the constructor; no registry, no runtime lookup.
Config comes from the exe's INI via `IniFile`, loaded once into typed
structs; an invalid value refuses startup. Windows derive from `Window` or
`SubclassedWindow` (`core/window.h`), never a static proc with a global
instance; a window someone else creates and destroys (a property sheet
page) derives from `DialogPage` (`core/dialogpage.h`), which holds its HWND
without owning it. Libraries (`libcore`, `libwire`, `libexec`, `libfs`, `libautomation`) know
nothing of each other's ops or of the wire, except `libwire`. See
`docs/REMOTE.md`.

**Open Watcom's STL where it makes sense** (exceptions on, `-xs`, since
2026-09-25): `std::vector`, `std::string`, `std::wstring`, `std::list` in
place of fixed `MAX_*` tables and hand-built string code. Keep `Buf`/`Path`
where a Win32 call fills a fixed buffer (the OS limit is real there). No
`<iostream>`/`<fstream>`: static constructors and size. `operator new`
throws `std::bad_alloc` (`core/heap.cpp`); only `App::run` and thread entry
points catch it, and nothing else uses exceptions for control flow.

**Keep `<string>` and `<vector>` out of widely included headers.** Each
function that copies a class with a `std::string` member grows exception
tables: one `std::vector<std::string>` in `app/config.h` cost 25 KB of
image. Use `TextList` (`core/textlist.h`) or plain char buffers there, and
forward-declare a class that wraps the string (`PrefsBody`).

**Watcom is C++98: `new T()` does not zero a class.** A class with any
private member is a non-POD, and C++98 gives a non-POD with no constructor
default-initialisation -- heap garbage, `()` or not. Every class that is
heap-allocated gets a constructor that sets every member. Moving a file
static (zero because static) onto the heap is exactly how this bites: the
Codec in `wire/connection.cpp` jumped through a garbage `zfree`.

**New C++ naming: PascalCase types, camelCase methods, Allman braces.** Members
keep `m_`; C files and `extern "C"` APIs keep `rc_snake_case`. The client's older
code is renamed as it is touched, not in a sweep (`docs/REMOTE.md`).

**Every resource lives in a guard from `win/core/rc_cpp.h`.** No raw `HWND`,
`HANDLE`, `HMODULE`, GDI object or `new`/`new[]` held across a return: use
`WindowGuard`, `Handle`, `FindHandle`, `Dll`, `GdiObj`, `MemoryDC`, `File`,
`MenuGuard`, `UniqueObj<>`, `UniqueArr<>`, `HeapPtr<>`, `WindowDC`,
`Selected`, `ScopedLock`. Check that file before writing a paired
acquire/release. If no guard fits, add one there (a `Unique<>` policy is
three statics), not a local cleanup path in the caller.

**Do not touch the `retrobridge` libvirt domain** in `qemu:///system` -- it
belongs to an unrelated project.

**Before acting on a real machine, ask whether a bad outcome leaves you able
to reach it.** gremote is single-session, has no remote `QUIT` by design, and
starts only at boot. Those three together mean anything that disturbs
gremote, the shell, or a session end is potentially one-way, and recovery
costs somebody a walk to another room. If the answer is no, it is a keyboard
step or it waits -- whatever the API technically exposes. Written after a
session that locked itself out twice in one evening, each time with a warning
already in front of it:

- **A flag that exists is not permission.** `win/exec/process.cpp` refuses to
  kill `EXPLORER.EXE` and logs `refused without force`. `kill(force=True)`
  gets past it, takes gremote down with the shell, and leaves the box
  reachable by ping and by nothing else (BACKLOG 40). A guard with a note
  attached is an argument, not a gate.
- **Read the guest's log for damage, not just for results.** The first
  hidden-window MSAA walk logged `thread: the ui lane did not stop within
  5000ms` -- the guest saying it had abandoned a thread it could not stop.
  Four more were run. By then the box had three wedged threads on it, would
  not honour a reboot, and held its session slot for half an hour.
- **A step the plan calls a keyboard step is one for a reason.** Overriding
  that is overriding the plan, not just the guard.

**Nothing untested reaches the box.** This is `docs/stage5-plan.md`'s first
design principle and it keeps paying: `trayprobe.py` was correct by
inspection and had two defects in its only interesting path, both found by
running it against the QEMU guest and pulling the connection out from under
it. Rehearse the driver, not only the thing it drives.
