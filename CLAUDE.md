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
moves bytes. Compression is the one deliberate exception.

**Memory is the binding constraint.** Measured on the 32MB guest with only the
app running: 2420 KB free at window creation, 1060 KB after the chat list,
**28 KB after a turn**. Every allocation must be bounded and small.

**Nothing NT-only, ever.** A single NT-only static import kills the image at
load, before `WinMain`, with no log written and nothing to debug. Resolve
optional DLLs with `LoadLibrary`/`GetProcAddress`. `make check` audits imports.

**Verify on the guest, not under Wine.** Wine proves the code, never the
platform. `tools/vm/vmship.sh` ejects before inserting, because
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

**Do not touch the `retrobridge` libvirt domain** in `qemu:///system` -- it
belongs to an unrelated project.
