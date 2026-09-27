# gremote plan (REMOTE.md step 4, and 5b with it)

## Context

`REMOTE.md` splits the client into five static libraries so a second program
can be built from the code the shipping client runs. All five now exist --
`libcore`, `libwire`, `libexec`, `libfs`, `libautomation` -- and each was
proven inside `gaston.exe` first, which was the point of the ordering.
`gremote.exe` is the program that consumes them, and it is what `BACKLOG.md`
1b has been waiting for: the physical Pentium 200 is reached today by burning
a floppy and reading COM1 (`tools/gen/mkw98kit.sh`), so there is no way to run
a command on it, read a file off it, or put a new build on it without walking
over.

gremote listens and Linux connects, the reverse of the client. One live client
at a time, a 30-second heartbeat, a shared secret and a CIDR allow-list.

## Scope decisions

- **Three lanes: control, job, fs.** REMOTE.md assigned fs to step 5, but 5a
  landed the library and 5b (`ship()`) is blocked only on this exe. Folding fs
  in is what makes the loop work: without `PUT` a new `gaston.exe` still
  travels by CD.
- **The ui lane waits for step 6.** `win/automation/` is already a library,
  but `ui/uiservice.h` is built on `rc_net` and gremote needs a `Connection`
  twin with its own COM and message-queue worker.
- **Deflate is off.** REMOTE.md's own open decision leans that way -- the only
  large payload is a screenshot, and screenshots are step 6. `Connection(sink,
  false)` also leaves out the 192KB of scratch buffers `wire/connection.h`
  documents, which is real on a 32MB box. So `HELLO` says `deflate=0` and the
  Python client never sets `enc=deflate`: a compressing peer talking to a
  non-compressing one drops the link rather than degrading, which
  `wirecheck`'s `refusal()` case proves. Turning it on later means importing
  `bridge/wire/compress.py` as well, which is stdlib-only and already has the
  persistent-stream discipline.
- **The gate for this step is the VM**, not real hardware. What the P200 still
  owes is listed at the bottom.

## What the reviews changed

Two adversarial passes ran against this plan before any code. The findings
that changed its shape, recorded so they are not re-derived:

- **`<portForward>` is refused by libvirt on our domain.** The RNG accepts it
  (`domaincommon.rng`), which is the trap; the C validation does not -- "the
  `<portForward>` element can only be used with the 'passt' backend of
  interface type='user' or type='vhostuser'", and the only implementation is
  `qemu_passt.c`. `retroclaude-win98` is plain SLIRP with `ne2k_isa` at
  `iobase=0x200 irq=0x3`, configured once by hand, and moving to passt swaps
  the backend and its DHCP range under a working guest. The host reaches the
  guest with QMP `hostfwd_add` instead: runtime, no restart, reversible with
  `hostfwd_remove`, and `tools/vm/qmp.py` already speaks
  `human-monitor-command`.
- **libwire needs two additions.** There is no reserved slot in the out queue,
  though REMOTE.md's protocol section calls for one: `RC_OUTQ_MAX_BYTES` is a
  single flat 64KB budget and `send` waits then returns `RC_SEND_FULL`, so a
  streaming `GET` can starve the `PONG` the reader owes. And `Listener` keeps
  its socket private with no accessor, so there is no way to make it
  non-inheritable -- which on NT and Wine, where the tests run, means an
  `EXEC` child would hold the port.
- **The 240s bind retry cannot live in a service's `init()`.** `App` enters
  its message loop only after `startServices()`, so a four-minute retry leaves
  a created top-level window not pumping, with `WM_ENDSESSION` dead
  throughout. It belongs on the accept thread, with the listener unbound until
  it succeeds.
- **Six threads, not the five REMOTE.md's budget names** -- main loop, accept,
  `Connection`'s reader and sender, job, fs -- and `make check` cannot see any
  of them. It sums PE sections; a thread stack is not in the image. Nothing in
  `docs/measurements/` records what a thread costs on this guest, so the stack
  reserves are chosen against runtime working set on the VM.
- **Autostart in C would fail the import gate.** ADVAPI32 is not in
  `tools/check/imports.py`'s `ALLOWED_DLLS`, so the `RunServices` key is
  written by a shipped `GREMOTE.REG` applied with `regedit /s` from
  `SETUP.BAT`. Only `RegisterServiceProcess` stays in the exe, on
  `GetProcAddress`, as REMOTE.md specifies.
- **`CANCEL` here closes none of `BACKLOG.md` 6's four items.** Item 2 is the
  *client's* `rpc.cpp`. REMOTE.md's "CANCEL follows BACKLOG 6" invites the
  wrong bookkeeping; BACKLOG 6 now says so.

## The unproven thing, and why it comes early

REMOTE.md: "first prove Wine can host a listening exe that pytest connects
to". That is still owed. `wirecheck.exe` proves `Listener`, `Connection::adopt`
and the idle timeout under Wine, but both ends are inside one Wine process,
and the only suite that crosses the boundary (`test_guest_net.py`) has Python
listening and Wine dialling out.

It also needs scaffolding that does not exist. `conftest.run_guest` is a
blocking `subprocess.run` and cannot host a server, and a `system nt_win`
gremote writes nothing to stdout anyway. So `tests/python/remote/` gets a
fixture that starts gremote under Wine, waits for the port, yields and
terminates it -- there is no remote `QUIT`, deliberately -- choosing a free
port and writing it into a temp `gremote.ini`, and surfacing `gremote.log` on
failure. The fixture name goes into `test_suite_hygiene.py`'s `GUEST_API`, or
the suite stops being recognised as needing Wine and rejoins `make test-fast`.

## `win/remote/`

Not a library: these objects are shared with `remotecheck.exe` the way
`rpccheck` links the client's `rpc.cpp` directly.

| File | Holds |
|---|---|
| `main.cpp` | `WinMain`, `GremoteApp : App` |
| `config.{h,cpp}` | `ServerConfig`/`ExecConfig`/`FsConfig` over `IniFile` |
| `optable.{h,cpp}` | gremote's own op-to-lane table and protocol version |
| `server.{h,cpp}` | `ServerService`: `Listener`, the accept thread and its bind retry, the CIDR filter applied before AUTH, one live client, AUTH/HELLO, the generation counter, the `FrameSink` that routes by lane |
| `lane.{h,cpp}` | `LaneWorker`: one request in flight, `ERR busy`, the CANCEL id table, and the open-request-by-id table that `DATA`/`END` reach past the busy gate |
| `reply.{h,cpp}` | `Responder`: `OK`/`ERR`/`DATA`/`END`, dropping a dead generation |
| `control.cpp` | `AUTH PING CANCEL SYSINFO PS KILL REBOOT`, inline on the reader |
| `jobops.cpp` | `EXEC LAUNCH` |
| `fsops.cpp` | `LS STAT GET PUT RM MKDIR MV DRIVES` |

`system nt_win` with `App`'s hidden window. A console window on something
started before logon is wrong, and the hidden window is what receives
`WM_ENDSESSION`; `appcheck.exe` already shows `App` needs no GUI. The
directory compiles at `-os`: it is dispatch, refusals and config parsing, the
profile the `fs/` and `sync/` overrides were measured for.

Five fs ops have no handler anywhere today -- `LS`, `STAT`, `RM`, `MKDIR`,
`MV` -- and `listDirectory`, `removeFile` and `movePath` have no production
caller at all. The primitives exist and `fscheck` covers them: unwired, not
unwritten.

`rpc/fsrpc.cpp` is the shape to copy rather than code to move, since every
function there takes an `rc_net *`. What transfers is the judgement:
`WorkNames(".gaston-put", "", 11, false)`, the `GetLastError`-to-token
classifier, the `CommitStatus`-to-token map (which stays in the handler and
never moves into libfs), the GET status ladder, and `RawCrc`. A `PUT` is not
one long-running request: each `PUT`/`DATA`/`END` frame is its own short fs
request against a per-connection `ChunkSink` keyed by id, as the client
already does it.

## `gremote/` (Python)

`client.py` -- socket, handshake, request and reply, the `Tflush` cancel
contract, a background `PING` every 10s sharing the send lock -- plus
`ops.py`, `ship.py`, `errors.py`. It imports `bridge.wire.frame` and nothing
else from the bridge; both `__init__.py` on that path are empty, and
`test_frame_standalone.py` pins the codec's import graph. The cost, recorded:
`gremote/` cannot be copied off this repo without `bridge/` on the path. It is
a tool for driving our own hardware, not a package we publish.

## Order of work

Per commit: `make -C win all`; `make -C win check`, recording committed-at-load
for both exes; `make test-fast`; `pytest tests/python/remote -q`, which
`test-fast` deselects; `make lint`. From the direction proof on, `make
wine-smoke`.

| # | Commit |
|---|---|
| 0 | This file; REMOTE.md's step list; the BACKLOG 6 note. No code. |
| 1 | `core/thread.{h,cpp}` with an explicit stack reserve, and `appcheck` cases. Record which existing `_beginthreadex` sites migrate. |
| 2 | `Connection`'s two threads move onto `core/thread.h`. |
| 3 | libwire: the reserved slot on `Connection::send`, with `wirecheck` cases. The non-inheritable socket waits for the job lane, where a child exists to test it. |
| 4 | The `win/remote/` skeleton and the whole Makefile wiring. |
| 5 | The direction proof: the Python spine, the fixture, and a real socket into a Wine-hosted gremote. |
| 6 | Op table, lanes, `Responder`, generation tagging, `CANCEL`, `remotecheck.exe`. |
| 7 | Control lane. |
| 8 | Job lane, and the non-inheritable listener in use. |
| 9 | Python: the control and job ops. |
| 10 | fs read-only: `LS`, `STAT`, `DRIVES`. |
| 11 | `GET`. |
| 12 | `PUT`, `RM`, `MKDIR`, `MV`. |
| 13 | Python fs ops. |
| 14 | `ship()` and self-update -- REMOTE.md step 5b. |
| 15 | The agreement test: the op table and status vocabulary scraped from `win/remote/` and diffed against `gremote/`. |
| 16 | VM bring-up: `hostfwd` in `tools/vm/qmp.py`, the three `GREMOTE.*` files on the tools ISO, `SETUP.BAT`. |
| 17 | The guest run, the measurements, the budget tightened, and the docs. |

## Traps

1. `GREMOTE.INI` is not `gremote.ini`. The loader joins the exe's directory
   with a literal long name; on FAT an 8.3 file written from DOS is a
   different file, every key reads back empty, and the first symptom points at
   the one thing that is not wrong. `SETUP.BAT` copies to the long name, as it
   already does for `gaston.ini`.
2. A refusal must not raise a modal on an unattended box. `secret=change-me`
   refuses startup: log and exit, with a message box only behind an explicit
   interactive flag.
3. The control lane never touches USER or GDI. A hung 16-bit app holding
   Win16Mutex blocks both from every 32-bit thread, and the reader carries the
   heartbeat.
4. `Connection` is single-use, and `release(othersGone)` needs the lane
   workers joined first or it frees a buffer a worker is still sending from.
5. A dead client's job must die with it: every request carries the connection
   generation, and a stale `Responder` returns false from `onOutput`, which
   `ExecJob` turns into `EXEC_SINK_GONE`.
6. The sender thread does not `select()` for writability, though REMOTE.md's
   protocol section says it should, so a client that stops reading parks it
   in `send()`. **Checked rather than assumed, and it is already covered:**
   `test_guest_net.py`'s slow-reader scenario points `netcheck --slowsend` at
   a listener that accepts and never reads, with `SO_RCVBUF` squeezed to 2048
   so the sender genuinely blocks, and asserts the run reaches its own PASS
   line and exits 0. `signalStop`'s `shutdown(SD_SEND)` and the bounded joins
   do break it out. No writability select is added.
7. No file-scope object with a non-trivial constructor: they run before
   `rc_log_open` and `make check` fails the build on one. `fsrpc.cpp`'s lazy
   placement-new is the precedent.
8. A stack frame of 4096 bytes or more fails the gate, and Watcom pools an
   inlined callee's locals into the caller -- which is how `fscheck`'s `main`
   reached 13,268 bytes.
9. Two budgets now, and `core/thread.cpp` entering libcore moves the client's
   as well as gremote's.
10. `tests/python/meta/test_suite_hygiene.py` fails the moment
    `tests/python/remote/` exists: `LAYERS` is a hand-maintained frozen set,
    deliberately.
11. `make lint` cannot see a new root package: ruff is invoked over `bridge/
    tools/ tests/` and mypy has `files = ["bridge"]`.
12. `CHECK_OWNED` must list only what the exe links -- `cmovcheck.py` fails
    loudly on an owned module absent from the map -- and the link rule must
    emit `map=`, which most check exes do not. `check-remotecheck` is not
    needed: `gremote.exe` links every `win/remote/` object.
13. gremote's status vocabulary is its own namespace, so `too_many`, `exists`
    and `is_file` are legal here. What is not: mapping `CommitStatus` to wire
    tokens inside libfs, which would make them invisible to the extraction
    `test_guest_agreement.py` protects.
14. Start guest runs from a clean boot. Killing a console program strands its
    DOS VM, and a guest carrying stranded VMs produced a measured BSOD in VMM
    (`docs/measurements/bsod-vm-20260927.txt`).
15. Prove the self-update rollback on the VM. Shipping a deliberately broken
    binary is the only way to test it, and on the VM a failure costs a CD
    swap.

## Verification

On the guest, `hostfwd_add`'d and from a clean boot:

1. Handshake; a wrong secret, with the 1s delay and a FIN rather than an RST;
   a peer outside `allow`; a second client answered `busy` without disturbing
   the first.
2. A client killed mid-session -- the box is free again within 30 seconds.
3. A long `EXEC`, cancelled; `PS` and `KILL` answered while it is in the slot.
4. `GET` of a binary, byte-exact; `GET` of a swap file with `max_bytes`,
   answering `truncated=1` rather than streaming for ten minutes.
5. `PUT` outside any sync root, killed mid-transfer, leaving nothing behind.
6. A streaming `GET` in flight while a `PING` is answered: the reserved slot.
7. `ship()` replacing `gaston.exe` with the client running, and a
   self-update rolled back from a deliberately broken binary.
8. Autostart: reboot, and gremote answers before anyone logs on.
9. Runtime working set idle and under load -- the number the stack reserves
   are chosen against, since `make check` cannot see a thread stack.

## Still open: the P200

Out of scope for this step by decision. What only the real box can answer:
`bind` after a restart (TIME_WAIT), gremote's footprint with and without the
client, socket inheritance into children, and `COMMAND.COM /c` exit codes
confirmed against the VM's measurement. Two preconditions nothing in this tree
records: whether that box has a NIC and a configured TCP/IP stack, and how
`GREMOTE.EXE` gets there the first time -- its only proven channel is a 1.44MB
floppy via `tools/gen/mkw98kit.sh`, which is where it should be added.
