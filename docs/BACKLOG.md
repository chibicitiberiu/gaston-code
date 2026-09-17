# Backlog

Work deliberately deferred, with the reason and what it would take to close.
**Everything here is late-phase**: it is picked up towards the end, not folded
into whatever stage is in flight.

`PLAN.md` is the design and `PROGRESS.md` is the rolling log. This file exists
so that "we decided not to do that yet" does not decay into "nobody
remembered", which is what happens when a deferral lives only in a commit
message.

---

## 1. The throttled-link gate — `tc netem rate 40kbit`

**Status:** deferred by decision, 2026-09-17. Was a Phase 3 gate.

The standing rule in `PLAN.md` is that every phase demo runs once over a
simulated dialup link, because it is the cheapest way to stop LAN-speed
assumptions leaking into the design. It has never been run: `tc` needs root and
this environment has no passwordless sudo.

**Why it matters more now than it did.** The guest gained a sender thread and a
byte-bounded outbound queue in Stage B, and that code is *precisely* what a slow
link exercises. It has only ever run at LAN speed, where the queue never fills
and the backpressure path never executes. Three known weaknesses live behind
that gate and are unreachable without it:

- **The queue's byte cap is soft.** A writer that has waited its limit (50 ×
  100 ms) enqueues anyway rather than dropping the frame, because losing a
  frame on a stream with no resync is worse than overshooting. So the real
  ceiling is `RC_OUTQ_MAX_BYTES` plus one frame per blocked writer.
  (`win/net.h`, `rc_net_send` in `win/net.c`.)
- **The UI thread can block up to 5 s in `rc_net_send`** under that
  backpressure — from `do_send`, `do_stop`, `chats_switch` and the permission
  bar. The whole point of the sender thread was that no caller blocks on the
  wire; this is the residue.
- **`n->sock` is read and written without the lock.** `rc_net_stop` closes it
  while the sender may have captured it, and Winsock recycles handle values.

**To close it:** `sudo tc qdisc add dev lo root netem rate 40kbit`, run the
Phase 3 demos, then `tc qdisc del`. The bridge's `--link-bps` is *not* a
substitute — it throttles the bridge's own sending logic, not the wire, so it
exercises the adaptive-codec thresholds but never fills the guest's queue.

---

## 2. Memory optimisation

**Status:** deferred by decision, 2026-09-17.

Measured at the end of Phase 3, against the Phase 2 baseline:

| point | baseline | now |
|---|---|---|
| window created | 2420 KB free | 2312 KB |
| chat list loaded | 1060 KB free | 960 KB |

No megabyte-scale regression, which was the gate. About 100 KB lower at
comparable points, accounted for by a ~6 KB larger binary, 7.8 KB more BSS and
a chat list holding more conversations. Not a like-for-like comparison, so read
it as "nothing ran away" rather than a precise delta.

**What to look at when this comes up:**

- **86 KB of BSS is committed at load**, and the single largest item is
  `rc_net_send`'s frame build buffer at 66,560 bytes (`win/net.c`). Now that
  the sender thread copies each frame to a heap block anyway, that static
  buffer may be reducible to the largest frame actually built rather than
  `RC_MAX_FRAME + 1024`.
- `exec.c`'s 8 KB chunk buffer is already the streaming design's whole cost and
  is probably not worth touching.
- `make check` reports committed-at-load size per section, so a regression is
  visible without guessing. Use it.

**One correction worth keeping.** The "28 KB free after a turn" figure quoted
throughout the Phase 3 planning is `dwAvailPhys`, which **excludes the disk
cache Win98 shrinks under pressure**. Allocations were never actually failing.
It was cited as a hard wall when it is not one, and several design decisions
were justified by it more heavily than the number deserved.

---

## 3. Also deferred — smaller, from the Stage B review

Recorded so they are not rediscovered. None are reachable in normal use today.

- **`from=` on the guest.** The guest never sends it, so the bridge's replay
  buffer is dead code in production; `PERMSTATE` covers the case that mattered.
  Implementing it properly needs per-frame seq tracking *and* a decision about
  what a re-delivered `EXEC` means. `EXEC` and `EXECKILL` are already sent with
  `stamp=False` so that day cannot arm a command re-run.
- **Concurrent turns.** `_handle` spawns a task per `INPUT` with no guard, so
  two `INPUT`s produce two turns on one client — and one prompt bar.
  Pre-existing; Stage A is just where it first has a visible failure mode.
- **`bytes=` in `EXECRESULT` counts discarded bytes**, so it is not comparable
  with the sum of `EXECOUT` payloads and cannot serve as an integrity check.
  Either redefine it as "bytes delivered" and assert on it bridge-side, or drop
  it from the frame.
- **`cwd` travels as a header**, so it is silently truncated at 255 bytes — a
  milder version of the argument that put the command in the body. Unlikely on
  DOS paths, but unguarded.
- **`RC_EXEC_TOO_LONG` is reported as `spawn_failed`**, so the model is told
  "could not be started" when the truth is "your command exceeded 1024 bytes".
- **`_exec_out` grows its buffer without a bridge-side cap.** The guest caps at
  32 KB and the bridge trusts it.
- **`rc_rpc_submit` can race shutdown** and leak one frame without sending an
  `EXECRESULT`. Narrow, and only at exit.

## 4. Deferred from the sync layer

Recorded after Phase 3.3 was verified on the real machine. None blocks normal
use; each is a known limit rather than something forgotten.

- **No non-Windows client has ever connected.** The capability negotiation
  (`bridge/caps.py`, `win/caps.h`) is exercised by tests and by the Win98 guest
  declaring its own rules, but `C.POSIX` has never been on the other end of a
  socket. Until one is, "the bridge survives being pointed at MS-DOS" is a
  design claim, not a measurement.
- **`sync_resolve` implements `release` and `forget` only.** The plan named
  `accept-class`, `accept-revert` and `rebind` as well. Each corresponds to a
  halt that cannot currently happen: a class change is now resolved as an
  ordinary content change, and the two tree-identity halts need a second guest
  or a restored backup to reach.
- **Restore-from-backup is still the one unresolved silent-loss path.** A guest
  whose tree is older than its own INI is indistinguishable, per path, from a
  deliberate mass revert. The plan's mitigation is a persisted generation
  counter plus an aggregate heuristic; neither is built. `.gaston/base/` is
  retained as history, so a wrong mass pull is recoverable by hand.
- **`eol=cr` is declarable and not implemented.** `_EOL` has the entry and
  `to_guest` would use it, but no classifier treats a lone CR as the native
  ending, so a client declaring it would fail the class cross-check on every
  text file. Loud, not silent -- but it should be refused at handshake rather
  than failing per file.
- **Deletions do not propagate.** By decision, not omission: a rename is a
  create plus a delete, and the delete does not travel, so `git mv` leaves the
  old path on the guest. It is reported and never silently resurrected or
  removed. Closing it needs `SYNCDEL` and a decision about what a re-delivered
  delete means.
- **The `_snprintf` truncation sweep.** Every call in the guest uses
  `sizeof(x) - 1` plus an explicit NUL, which prevents the overrun and still
  truncates silently -- how `del c:\temp\*.tmp` became `del c:\temp\*`. Only
  `win/exec.c` checks the return. A silently shortened PATH in the sync layer
  writes to the wrong file.

Phase 4's own list (model per-client, the effort fork leaving the chat list on
the pre-fork session id, the real Server Host dialog, Export as HTML, the
permission rule table, icons, multi-client, the command menu) lives in
`PLAN.md` and is not duplicated here.
