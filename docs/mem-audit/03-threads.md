# Threads, and what they share

## The model

Four threads, not three. `win/Makefile:4` still says *"three threads (UI /
reader / RPC worker)"*; `win/net/net.h:3` says *"FOUR threads now: UI, reader,
sender, and the RPC worker in rpc.c."* The header is right. Worth fixing the
Makefile, since it is the first thing a new reader meets.

| thread | created | entry | owns |
|---|---|---|---|
| UI | `WinMain` | `win/app/main.cpp` | every window, the ten module objects, start/stop |
| reader | `net.cpp:608` | `reader_thread` | `recv`, frame parse, inflate, routing |
| sender | `net.cpp:594` | `sender_thread` | every byte written to the socket |
| RPC worker | `rpc.cpp:612` | `rpc_thread` | `exec`, all `SYNC*`, the directory watcher |

All created with `_beginthreadex`, never `CreateThread` — required twice
over: Watcom's multithreaded CRT allocates its per-thread block there, and
`CreateThread(..., lpThreadId=NULL)` is rejected outright on Win95/98.
`win/checks/probe.c:151-193` measured both.

## Locks

| lock | guards | init | deleted |
|---|---|---|---|
| `n->send_lock` (`net.h:79`) | the outbound queue only, never the wire | `net.cpp:404` | `:992`, only if all three threads joined |
| `g_lock` (`rpc.cpp:58`) | `g_sync_root`, `g_syncq[]`, `g_queued` | `rpc.cpp:596` | never, deliberately |
| `g_child_lock` (`exec.cpp:49`) | `g_child` | `exec.cpp:55` | never, deliberately |
| `g_lock` (`log.cpp:9`) | the log file handle | `log.cpp:17` | `:74`, unconditionally — see F6 |

Three of the four are deliberately leaked at process exit rather than
destroyed under a thread that might still enter them. That is the right
trade, it is argued in place, and it is the pattern `log.cpp` does not
follow.

**`ScopedLock` is used once**, at `net.cpp:753`. Every other
`EnterCriticalSection`/`LeaveCriticalSection` in the guest is hand-paired —
`rpc.cpp` and `exec.cpp` do not include `rc_cpp.h` at all. I traced every
pair: **none has an early return between the two that skips the `Leave`**,
and there is no nested locking anywhere. The hand-written version is
currently correct everywhere.

## Cross-thread state that no lock covers

This is the interesting part, and mostly it is deliberate.

| variable | written by | read by | what makes it safe |
|---|---|---|---|
| `n->running` (`volatile LONG`) | UI, reader, sender | all four | `volatile` + aligned word write; the two meanings it used to carry were split by *sequencing* instead (`net.cpp:872-895`) |
| `n->inflight` (`volatile LONG`) | reader (`InterlockedIncrement`), UI (`InterlockedDecrement`) | reader | properly interlocked |
| `n->state` | UI, reader | UI | the UI's real view arrives as a value copy in `WM_RC_STATE`'s `wParam`, not by reading the field |
| `n->sock` | UI (close), reader, sender | four actors | **ordering, not a lock** — see below |
| `g_running` (`volatile LONG`) | UI | worker, reader | `volatile` word |
| `g_cancel` (`volatile LONG`) | reader, UI | worker | `volatile` word, with documented arm/check ordering |
| `n->rx_bytes` / `n->tx_bytes` | reader / sender | UI, for the status bar | nothing; torn reads of a counter |
| `g_home` (`syncrpc.cpp:69`) | UI | net | **nothing — see F7** |

The socket deserves its own note, because it is the best piece of reasoning
in the codebase. `n->sock` is touched by four threads with no lock, and
`net.cpp:936-941` explains why a lock is the *wrong* fix: the reader holds
the socket across a blocking `recv`, so any mutex covering it would be held
for the length of an idle connection and `rc_net_stop` would wait behind it.
The race is removed by ordering instead — `shutdown(SD_SEND)` first to unblock
the reader, then join, then close, and *don't* close if the join failed.
That is a better answer than a lock, and it is the reasoning the rest of this
document measures other sites against.

## Where the model is not held to

Three places, all in `02-findings.md`:

- **F3** — `rc_net_stop` applies the join-gate to the socket, the critical
  section and both events, then calls `inflate_finish()` and `WSACleanup()`
  outside it. Same function, four lines apart.
- **F6** — `log.cpp` does the three things `net.cpp` argues against: holds a
  lock across a blocking call, touches a handle outside the lock that
  protects it, and deletes a critical section without establishing that
  nobody is inside it.
- **F7** — `g_root` is hardened and `g_home`, seventeen lines later, is not.

The pattern is consistent: **the transport layer learned each lesson and the
layers around it did not.** Every rule `net.cpp` and `rpc.cpp` state in
comments is correct and followed there. `log.cpp` and `syncrpc.cpp` break the
same rules, in the same ways, and their comments do not mention them.

## Cross-thread messaging

Only `PostMessage`, never `SendMessage` — the rule at `net.h:6-8` and it is
honoured. Two messages:

- `WM_RC_FRAME` (`net.cpp:291`) carries an `rc_frame *` in `LPARAM`. The
  ownership rule is three-way and correctly implemented; see F4 for the one
  trap next to it.
- `WM_RC_STATE` (`net.cpp:182`) carries an enum by value. Nothing to own.

## What a scope guard does and does not buy here

`ScopedLock` cannot express two of the four patterns in use:

- **The conditional lock.** `if (g_ready) Enter...; ...; if (g_ready) Leave...`
  appears in `log.cpp:58/63` and `rpc.cpp:249/251, 291/293`. A guard that
  takes the lock only when a flag is set is a five-line class, and it would
  also close F6's window by making the `g_ready` test happen once instead of
  twice.
- **The deliberate temporary unlock.** `net.cpp:799-802` leaves and re-takes
  the section inside the `ScopedLock`'s own scope, so a full-queue writer
  does not block the sender that has to drain it. The comment at `:772-781`
  explains this is intentional: *"ScopedLock has no 'pause' — it is a
  destructor, not a semaphore."* That is correct and should stay as it is.

And a guard buys **nothing at all** for the thing this layer actually gets
right the hard way: the join-gated teardown. A destructor fires
unconditionally. `rc_net_stop`'s whole design is that cleanup is *conditional*
on what the joins reported — leak the socket rather than close it under a
live thread. Wrapping those in `Handle` would convert a carefully chosen leak
into a use-after-free. See R7.
