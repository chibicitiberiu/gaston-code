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
- ~~**`bytes=` in `EXECRESULT` counts discarded bytes**~~ **Closed
  2026-09-18** by dropping it. It also counted chunks lost on the wire, so it
  needed two fixes rather than one, and the bridge never read it. `truncated=`
  already carries the only fact the model acts on.
- **`cwd` travels as a header**, so it is silently truncated at 255 bytes — a
  milder version of the argument that put the command in the body. Unlikely on
  DOS paths, but unguarded.
- ~~**`RC_EXEC_TOO_LONG` is reported as `spawn_failed`**~~ **Closed
  2026-09-18.** It has its own status, and the message states the limit --
  which is nearer 1009 than 1024, because the shell prefix counts.
- ~~**`_exec_out` grows its buffer without a bridge-side cap.**~~ **Closed
  2026-09-18.** Capped at 64 KB, and a cut here reports through the same
  `truncated` the guest uses.
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
- ~~**`eol=cr` is declarable and not implemented.**~~ **Closed 2026-09-18.**
  Refused at the handshake AND per path -- attributes are consulted per path,
  so `*.txt eol=cr` in a real `.gitattributes` would otherwise reach a client
  that declared itself perfectly legally. The diagnosis in this entry was also
  wrong: both classifiers already agree a lone-CR file is byte-exact, so the
  failure was a CRC mismatch on every text file, with the log pointing at
  corruption rather than at a capability.
- **Deletions do not propagate.** By decision, not omission: a rename is a
  create plus a delete, and the delete does not travel, so `git mv` leaves the
  old path on the guest. It is reported and never silently resurrected or
  removed. Closing it needs `SYNCDEL` and a decision about what a re-delivered
  delete means.
- **Bash can still reach `.gaston/`.** `Edit`, `Write` and `NotebookEdit` are
  refused there now, and the baseline is outside `--cwd` by a startup check --
  but `permissions.py` deliberately does not parse command lines, so `sed -i`
  on a staged conflict copy is not caught. The copies are recoverable from
  `.gaston/base/` history; the baseline is not reachable at all. Closing it
  needs a sandbox for Bash rather than another path rule.
- **`text=auto` is not what git means by it.** Git decides per blob and
  remembers; this treats it as "a candidate for conversion" and lets the
  content decide each pass, which is the same answer for every file that is
  not already ambiguous. A file that flips between passes would be reported
  rather than converted, so the difference is visible, not silent.
- **The guest re-reads its whole tree on every pass.** The bridge caches
  classification inputs against (size, mtime) and honours the settle window
  before storing one; the guest has no cache at all, because a per-path entry
  on a 32MB machine costs memory it does not have, and keying on a path hash
  to save space trades a bounded cost for an unbounded one -- a collision
  returns a confident wrong checksum. Measure the real cost on the box before
  building anything.

- ~~**The `_snprintf` truncation sweep.**~~ **Closed 2026-09-18, and corrected
  first -- from audit finding 01.16.** This entry's own claim was wrong: "every
  call in the guest uses `sizeof(x) - 1` plus an explicit NUL" was measured
  against the 86 `_snprintf` call sites that existed at the time and only 39
  of them did that. 15 used `sizeof(x) - 1` with **no** NUL and 32 used the
  full `sizeof(x)` with **no** NUL -- both idioms leave the buffer
  unterminated whenever the formatted text does not fit, which is a read past
  the end of the buffer on the next string operation, not merely a truncation.
  Two of those sites were wire-reachable: `syncrpc.c:224` (`g_put.rel`, from a
  wire length prefix) and `main.c:577`/`750` (`g_net.sess`, echoed back in
  HELLO).

  The guest no longer calls `_snprintf` anywhere -- verified: the only two
  hits left in `win/*.c`/`win/*.cpp` are in comments explaining the old bug,
  not live calls. Every write-family call now goes through
  `win/core/str.c`'s `rc_strf` / `rc_strcpy` / `rc_strncpy`, which always NUL-
  terminate regardless of whether the input fit (`rc_strf` passes the *full*
  capacity to `_vsnprintf` and terminates by hand afterwards, rather than
  reserving a byte nothing then writes) and report success or truncation
  through their return value. The unterminated-buffer defect class this entry
  was actually about is gone.

  **What is not gone, and is not this entry's original concern:** silent
  truncation itself. Of 105 call sites to `rc_strf`/`rc_strcpy`/`rc_strncpy`,
  only 8 check the return value (`win/sync/sync.cpp` x3, `win/rpc/exec.cpp`
  x2, `win/net/frameproto.cpp` x3) -- not just `win/exec.c` any more, but
  still a small minority. `del c:\temp\*.tmp` becoming `del c:\temp\*` is
  still possible at any of the other ~97 sites; it would now stop at a clean
  NUL rather than reading past one. Worth a future sweep of its own if a
  truncated value is ever found to matter silently, but it is a truncation
  question now, not a memory-safety one.

Phase 4's own list (model per-client, the effort fork leaving the chat list on
the pre-fork session id, the real Server Host dialog, Export as HTML, the
permission rule table, icons, multi-client, the command menu) lives in
`PLAN.md` and is not duplicated here.

## 5. Found by the 3.4 review rounds, not yet closed

Three adversarial rounds ran against the hardening plan before any code. Nine
defects were fixed (see `PROGRESS.md`, 2026-09-18); these were found by the
same rounds and deliberately left.

- **`cwd` is still a header, still clipped at 255.** §3 records it; the review
  added that it goes straight to `CreateProcessA` with no validation, so the
  command runs in a *different existing directory* rather than failing. The fix
  is a bridge-side length check before sending, not a move to the body — that
  would be a lockstep protocol change wanting a declared capability, for a
  value that is a short DOS path in every real case.
- **A pass-level abort is invisible.** `SyncError` is caught by a bare
  `except Exception` and becomes a `log.warning` — it never calls `note()`,
  never latches, never reaches `sync_status_text`. So the two live
  tree-identity refusals, and any future mass-revert refusal, stop the whole
  pass with nothing said to either the user or the agent. Making it visible is
  not just deleting the `log.warning`: a `tree_id` mismatch aborts *every*
  pass, so it needs a persisted fingerprint written inside the handler, keyed
  on the reason. Narrow the bare `except` while there — today a crash is
  disguised as a deliberate refusal, which is how the `IsADirectoryError`
  total-stop stayed hidden.
- **Concurrent turns are still unguarded**, and the reachable route is the
  *designed* conflict flow rather than a race: the Continue button is not
  gated on `g_turn_active`, and a halt is raised by a sync pass while the agent
  works. Two `_run_turn`s then run on a stream that can only have one consumer,
  so one turn can consume the other's `ResultMessage` and the transcript is
  corrupted. Both call sites are bare `create_task` — `:837` and `:863` — so
  neither is even in the strong-reference set. Note `_release_all_latches` now
  takes the lock, so the specific loss that made this urgent is closed; what
  remains is the turn guard itself, and it must not defer the turn that carries
  the conflict preamble past the latch release.
- **The guest never re-requests page content on reconnect.** `READY` carries
  `page` and `total`, and the guest updates both — but `DOC` is guarded by
  `if (!g_doc_sent)`, set once per *process*, so the skeleton is not reset
  either and the stale transcript simply persists under a new page number.
  `page_goto("live")` already exists; this is one call in the `READY` handler.
  It matters more if the replay buffer is ever retired.
- **The replay buffer is still dead and still stamping.** The guest never sends
  `from=`, so `replay.since()` is unreachable; `append()` still fills a
  20,000-frame buffer. Retiring it is larger than it looks: the oversized-frame
  guard exists *because* replay is append-only, so its merit has to be
  restated rather than preserved, and the size probe stamps a fake `seq` into
  every frame's budget including `stamp=False` ones.
- **Directories deleted on either side are silently resurrected.** `diff` never
  consults the ancestor for a directory, so guest-only becomes `ADOPT` plus
  `os.makedirs` and bridge-only becomes `MKDIR`. §4's "reported and never
  silently resurrected" is true for files and false for directories.
- **`reject_sync_busy` answers every full-queue sync frame with
  `SYNCPUTACK`.** MKDIR survives it — the bridge handles both acks in one
  id-keyed branch — but SCAN and GET get a 2-tuple where they unpack six or
  four, which surfaces as "sync pass aborted: not enough values to unpack".
  A blocker for any new sync frame.
- **`n->sock` is unserialised across four threads**, 14 accesses, none under a
  lock — §1 names one instance; the review found `recv_exact`'s two and the
  TOCTOU guard in `rc_net_send` are the same class. And the shutdown drain's
  acks are *all* discarded, because `rc_net_stop` clears `running` before
  calling `rc_rpc_stop` and `rc_net_send` bails on `!running`: the comment
  promising queued frames are "answered, not silently dropped" is false on the
  only path that reaches it.
- **The netem gate is no longer blocked on sudo.** Verified: unprivileged user
  namespaces work here, `tc qdisc add dev lo root netem rate 40kbit` succeeds
  inside one, and `pasta --config-net` keeps outbound internet while leaving
  `lo` shapeable. The live domain uses `-netdev user` with no `hostfwd`, so the
  guest's connection is re-originated on host loopback — exactly what netem on
  `lo` would shape. Not yet run end to end: QEMU inside the namespace is
  unverified, and `tools/runvm.sh` specifies `pcnet` where the live domain is
  `ne2k_isa`, so a run built on it would come up with no network and look like
  a bridge bug.

## 6. CANCEL, and the chunked-transfer primitive it belongs to

**Status:** deferred by decision, 2026-09-18, from audit finding 04.4.

`PLAN.md` mandates a Cancel on any transfer with a progress bar. There is no
`CANCEL` frame anywhere, on either side.

**Why it is deferred rather than built.** It has no caller yet. The transfers
that exist today -- a sync push, a sync get, `EXECOUT` -- are each bounded and
already interruptible by other means (`EXECKILL`, and a sync pass that commits
per path and is safe to cut short). The transfer that genuinely needs Cancel is
Phase 5's attachment upload from the guest's own disk, which does not exist.

Building the frame now would mean guessing its shape against no user, and the
audit is explicit that there are already **four ad-hoc chunked transfers**
(`_send_html`, `Pass.push`, `do_get`, `EXECOUT`) with three more coming. Cancel
belongs to whatever primitive unifies those, not beside them as a fifth
special case.

**To close it:** design the chunked-transfer primitive (audit 04.3) and give it
Cancel as a property. `do_put` is already a chunked sink and `do_get` a chunked
source, so the shape exists twice in the guest and wants naming rather than
inventing.

**Done now, because it is one word and it is a trap:** `ICC_PROGRESS_CLASS` is
registered alongside `ICC_BAR_CLASSES`. It was not, so the first
`CreateWindow(PROGRESS_CLASS, ...)` would have failed on a machine with no
debugger, presenting as "the progress bar does not appear".

## 7. The sync baseline is bound to one machine, and R7 needs a decision

**Status:** deferred by decision, 2026-09-18, from audit finding 04.2. This is
a DESIGN question, not a defect, and it is recorded rather than guessed at.

`scan_guest` refuses when `tree_id` or `guest_root` changes, and `tree_id` is
derived from the volume serial. That is correct for one machine and it means
two guests sharing one `--sync-state` do not degrade gracefully -- they
deadlock. Each refuses the other's identity, every pass, forever.

**The rate-limiting half is fixed** (`Baseline.announced`): the two reasons
alternate, so the previous once-only guard compared against the last reason
and never matched, and both users were told "synchronisation has stopped"
twice a minute. Each distinct reason is now announced once until a pass
succeeds.

**The design question is not.** Before R7 can give the bridge a dict of
guests, someone has to decide what a mirror MEANS with more than one machine:

* **One mirror per client.** Each guest gets its own `--sync-state` and its
  own subtree. Simple, and the baseline logic needs no change at all -- but
  two people cannot collaborate on one tree, which may be the whole point of
  having two clients.
* **One shared mirror, many guests.** The baseline becomes per-path-per-guest
  rather than per-path, `tree_id` stops being an identity check and becomes a
  key, and every row grows a dimension. The three-way diff becomes an N-way
  diff, and "the guest's copy wins" needs an answer to "which guest".
* **One mirror, one writer.** A shared tree with a lease, so only one machine
  syncs at a time and the others are read-only until it is released.

The first is a weekend. The second is a redesign of `sync.py`'s state table,
which five review rounds went into. The third is somewhere between.

**To close it:** pick one, and write down why, before `self._guest` becomes
`self._guests`. Doing it in the other order means discovering the answer from
whichever tests break.

## 8. `rle8` / `rle8+deflate`, named in PLAN.md, never built

**Status:** open question, recorded 2026-09-18, from audit finding 03.35. Not a defect --
nothing that exists today needs either codec -- but it is a gap PLAN.md promised and never
delivered, and nothing anywhere said whether that was on purpose.

`PLAN.md`'s wire-format design named `enc=raw|rle8|deflate|rle8+deflate` as the adaptive
codec set, chosen per payload from a measured `STAT` link speed. What shipped
(`bridge/wire/compress.py`) defines only `ENC_RAW` and `ENC_DEFLATE`; the guest
(`win/net/net.cpp`) drops the connection on any `enc=` value it does not recognise, so
`rle8` is not a silently-ignored option, it is a link-killing one if anything ever sent it.
`rle8` is named a second time in the Phase 6 screenshot design (`PLAN.md`, "Screenshots:
crop, then reduce depth, then compress"), where `GetDIBits` producing `BI_RLE8` directly
was measured and works -- but that is GDI's RLE inside a DIB, a different thing from an
`enc=rle8` wire codec applied to an arbitrary frame body, and Phase 6 has not landed.

**Two readings, neither confirmed:**

- **Intentional-so-far.** The only traffic that would want RLE over deflate is the
  screenshot path, which is Phase 6 and does not exist yet. `deflate` alone covers
  everything that ships today (transcript text, sync file bodies), so there has been
  nothing to regret.
- **A forgotten scope cut.** The wire-format section named `enc=` as a value that should be
  free to extend "so adding a codec is a value change, not a protocol change" -- and then
  the codec set was never extended when the measured numbers (11.3:1 on `BI_RLE8`,
  near-zero guest cost) came in during Phase 0's screenshot experiments. Nothing recorded a
  decision either way.

**To close it:** when Phase 6's screenshot capture lands, decide whether the wire codec
gets an `rle8` value of its own (cheap: GDI already produces `BI_RLE8` bytes, so the guest
side may be close to free) or whether `deflate` alone is enough for the compressed sizes
Phase 6 actually measures, and write down which. Until then this is not blocking anything.
