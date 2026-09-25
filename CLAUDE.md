# Gaston Code

A native Windows 98 GUI client for Claude Code (`win/`, C89, Open Watcom) talking
to a Python bridge (`bridge/`) on Linux over one TCP socket.

`docs/PLAN.md` is the design. `docs/PROGRESS.md` is what is built and why
decisions were made. `docs/BACKLOG.md` is what was deliberately deferred and
what it would take to close -- check it before assuming something was
forgotten, and before starting late-phase work.

## Standing rules

**Tool names are OS-neutral.** The in-process MCP server is `machine` and its
tools are bare verbs -- `mcp__machine__exec`, not `win98_exec`. Only the guest
binary is OS-specific; the bridge, the protocol and the tool surface should
survive being pointed at MS-DOS, Windows 3.1 or NT 4. Where the OS genuinely
matters it is a runtime branch, not a name (`probe_exec` picks `command.com`
over `cmd.exe` from `GetVersion()`). `docs/FEASIBILITY.md`'s `win98_*` sketch is
superseded.

**A tool earns its place only if `exec` does the job badly.** Every tool costs
context on every request. See "the exec test" in `docs/PLAN.md` §3.1.

**The guest transforms nothing.** Markdown, pagination, encoding folds, image
diffing, OCR and JSON all happen on the bridge. The guest makes syscalls and
moves bytes. Compression and the sync layer's single-pass text/binary CRLF
classification (to save disk passes on 1998 IDE platters; pinned by agreement
tests) are the two deliberate exceptions.

**Memory is the binding constraint.** Every allocation must be bounded and
small. On a 32MB guest, physical memory and GDI/USER heaps are tight; keep
buffers capped and static allocations minimal (see `docs/mem-audit/`).

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

**New C++ goes through a container, not file statics.** Services derive from
`Service` (two-phase: trivial constructor, fallible `init`/`start`, idempotent
`stop`) and are owned by `App`. Dependencies are passed through constructors
from one composition root; no runtime lookup by name. Config comes from the
exe's INI, loaded once into typed per-service structs; an invalid value refuses
startup. Libraries (`libcore`, `libwire`, `libexec`, `libfs`, `libui`) know
nothing of each other's ops or of the wire, except `libwire`. See
`docs/REMOTE.md`.

**New C++ naming: PascalCase types, camelCase methods, Allman braces.** Members
keep `m_`; C files and `extern "C"` APIs keep `rc_snake_case`. The client's older
code is renamed as it is touched, not in a sweep (`docs/REMOTE.md`).

**Do not touch the `retrobridge` libvirt domain** in `qemu:///system` -- it
belongs to an unrelated project.
