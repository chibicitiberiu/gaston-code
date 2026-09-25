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
assumptions leaking into the design. It has never been run: `tc` does not
require root (unprivileged user namespaces work here, as recorded in §5:
`pasta --config-net` keeps internet while shaping `lo`).

**Why it matters more now than it did.** The guest gained a sender thread and a
byte-bounded outbound queue in Stage B, and that code is *precisely* what a slow
link exercises. It has only ever run at LAN speed, where the queue never fills
and the backpressure path never executes. Two known weaknesses live behind
that gate and are unreachable without it (a third, soft queue cap, was closed
at `win/net/net.cpp:719-726` with a hard drop and `-1` return):

- **The UI thread can block up to 5 s in `rc_net_send`** under that
  backpressure — from `do_send`, `do_stop`, `chats_switch` and the permission
  bar. The whole point of the sender thread was that no caller blocks on the
  wire; this is the residue.
- **`n->sock` is read and written without the lock.** `rc_net_stop` closes it
  while the sender may have captured it, and Winsock recycles handle values.

**To close it:** Inside a user namespace, run `tc qdisc add dev lo root netem rate 40kbit`
(see §5), run the Phase 3 demos, then `tc qdisc del`. The bridge's `--link-bps` is *not* a
substitute — it throttles the bridge's own sending logic, not the wire, so it
exercises the adaptive-codec thresholds but never fills the guest's queue.

---

## 1b. Real hardware gate (period PC)

**Status:** open since Phase 1.9; deferred while developing on QEMU Win98 SE domain.

`PLAN.md:719-721` moved the physical hardware gate up from Phase 2 to Phase 1 so
hardware quirks, RichEdit issues, and pagination bugs wouldn't stack. Four phases of
software have accumulated on QEMU without running on the LAN's physical Pentium 200 MMX
box (`PROGRESS.md:35`). The physical PC is now available and ready to run the validation
gate.

**Plan (2026-09-25):** drive the box with `gremote.exe`, `docs/REMOTE.md` step 4.

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
- **The compiler flags are the biggest lever found so far** (measured
  2026-09-25). The whole client is built with `-otexan` (favour speed, inline
  expansion, functions padded to 16 bytes). The same client built with `-os`
  commits **366,552** bytes at load against **398,808**: 32 KB less, more than
  the whole "free after a turn" figure. For libcore's `App` + `IniFile`, `-os`
  cut their cost from +6,656 to +4,608. Not applied, because it trades speed
  on a P200. Most of our code is event-driven UI (rendering is MSHTML's), so
  the likely shape is `-os` by default with `-ot` kept for measured hot
  modules -- miniz, the sync scanner's CRC pass, the frame codec -- and a
  timed sync scan and long transcript page on the guest before and after.

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
- ~~**`sync_resolve` implements `release` and `forget` only.**~~ **Partly
  closed 2026-09-18.** `accept-revert` is built (02.46) -- it answers the halt
  the stale-editor-buffer guard raises, which is the one of the three that
  happens routinely. `accept-tree` was added alongside it, path-less, to answer
  the whole-pass refusal below. `accept-class` stays deferred for the reason
  already recorded: the halt it would answer does not exist, because a class
  change resolves as an ordinary content change. `rebind` stays deferred and is
  a genuinely different job from `accept-tree` -- it re-anchors tree IDENTITY
  after a machine change, and needs a second guest to reach.
- ~~**Restore-from-backup is still the one unresolved silent-loss path.**~~
  **Closed 2026-09-18** (02.47), and NOT the way the plan specified, which is
  worth recording because the plan was written before the fix existed.

  The plan wanted a persisted generation counter on the wire plus an aggregate
  heuristic. **The heuristic was built; the generation counter was not, and on
  inspection it would not have detected the case it was for.** The INI lives in
  `C:\RETRO`, outside the synced tree, so restoring `C:\WORK` from a backup
  leaves the stored generation untouched: the guest would report the CURRENT
  generation over a tree that is five passes old, and the comparison would
  pass. Its real value was ruling out "a different machine" as the alternative
  explanation, and `tree_id` plus `guest_root` already refuse that case with
  their own message. So it bought a better sentence in one report at the price
  of a protocol change on both sides, a new INI field, and a flag day. Not
  built, deliberately.

  What is built refuses the whole pass when too many paths go backwards at
  once, above both a floor and a share of the tree, and ships `accept-tree` as
  the way to say it was intended. `.gaston/base/` is still retained as history.
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
defects were fixed at the time (see `PROGRESS.md`, 2026-09-18); these were
found by the same rounds and deliberately left.

**Re-verified against the code on 2026-09-18**, because the list had drifted:
six of the nine entries here had been closed by the audit sweep and were still
written up as open, which makes a backlog worse than no backlog. What closed,
and where to read it: `cwd` is length-checked bridge-side before the frame is
sent (`server.py:1464`); a pass-level abort goes through `_report_abort`, which
persists a fingerprint and pushes a notice to both the user and the agent;
concurrent turns are serialised on `GuestSession.turn_lock` with both call
sites through `self._spawn`, and latch release moved into the turn that carries
the preamble; the guest re-requests page content on every `READY` via
`goto_live()`; `reject_sync_busy` now answers with the ack type each sync frame
expects; and the shutdown drain's acks leave before `running` is cleared,
measured on hardware at 0 before and 24 after. `n->sock` is deliberately NOT
lock-serialised -- `net.cpp:740` argues a lock would block shutdown behind an
idle blocking `recv` -- the race is removed by joining the reader and sender
before `closesocket`, and leaking the socket if a thread will not exit.

Three remain:

- ~~**The replay buffer is still dead and still stamping.**~~ **Closed
  2026-09-18.** `bridge/wire/replay.py`, the unreachable `from=` branch, `seq`
  stamping, `Frame.seq`, the `stamp=` kwarg and `protocol.resync()` are gone,
  and `PLAN.md`'s `seq=` bullet carries a dated note reversing the original
  decision rather than quietly undoing it.

  **The two guards were kept and their remedies CHANGED**, which was the part
  that was larger than it looked. Both had justified a silent drop by replay
  being append-only -- one poisoned frame corrupts every future replay -- and
  with replay gone that reasoning left a silent loss behind a log line that
  was no longer true. `GuestLink.send` now raises on a frame that exceeds
  `MAX_FRAME` instead of returning a bool nobody checked, and
  `permissions.MAX_BODY` still degrades to a short fallback (never silent to
  the user, who sees "arguments too large to display") but now logs, which it
  never did. The phantom `seq=9999999999` in the size probe died with the
  stamping, so the budget check finally measures the bytes that actually go on
  the wire.

  **"EXEC is never replayed" was re-expressed, not deleted**, since it goes
  vacuous the moment nothing replays: the surviving property is that nothing
  re-sends anything except the explicit reconnect reissue path
  (`_reissue_halt`, `_reissue_turn`, `PermissionDesk.reissue`), and that path
  must never carry EXEC, EXECKILL or any SYNC frame. Both that test and the
  sync equivalent now assert it across a real reconnect.

  One thing left deliberately: `win/app/main.cpp` still has an `on_resync`
  handler that is now unreachable. Harmless, and removing it is a guest change
  that would want its own build and hardware check for no behavioural gain.
- ~~**Directories deleted on either side are silently resurrected.**~~
  **Closed 2026-09-18** (02.45). `diff`'s directory branch read "directories
  carry no content" as "directory existence has no history" and never looked at
  the ancestor, so every one-sided absence was a creation: a directory deleted
  on the machine went straight back on the next pass, and again thirty seconds
  later. Both sides now answer `REPORT`, which is what the file rows have
  always answered for the same shape, so §4's "reported and never silently
  resurrected" is true for directories too.
- ~~**The netem gate is no longer blocked on sudo, and still has not been
  run.**~~ **Discharged 2026-09-18** (01.62), with the plan's own named
  fallback rather than netem: a deliberate slow reader
  (`win/checks/netcheck.c --slowsend` driven from
  `tests/python/test_guest_net.py`). It fills the guest's outbound queue
  directly, which is what the named weaknesses actually need, and it lives in
  the suite instead of being run by hand.

  **It found the first weakness to be worse than recorded.** The byte cap was
  not soft, it was absent: after the 5s wait `rc_net_send` enqueued anyway and
  returned success, measured at 65558 -> 98337 -> 131116 bytes across three
  calls. The second weakness (the UI thread blocking up to 5s) is real and
  deliberately left -- the duration is the design's stated cost, and only what
  happened when it expired was wrong.

  **What the fallback does NOT cover, stated rather than implied:** it shapes
  nothing, so nothing here measures the guest under real bandwidth limits, and
  the Phase 3 demos have still never been run throttled. The netem route below
  is kept for whenever that is wanted. Also unreached by hand: the shutdown
  drain with work still queued -- on loopback the guest answers a full
  24-frame burst in 762ms, faster than a click can land, so a manual close
  always finds an empty queue. `rc_net_stop` under a stalled peer IS covered
  by the slow-reader test, which measured it returning in ~12s rather than
  hanging.

  The original note, kept because the route is still open:
  Verified: unprivileged user namespaces work here, `tc qdisc add dev lo root
  netem rate 40kbit` succeeds inside one, and `pasta --config-net` keeps
  outbound internet while leaving `lo` shapeable. The live domain uses
  `-netdev user` with no `hostfwd`, so the guest's connection is re-originated
  on host loopback -- exactly what netem on `lo` would shape. Not yet run end
  to end: QEMU inside the namespace is unverified, and `tools/vm/runvm.sh`
  specifies `pcnet` where the live domain is `ne2k_isa`, so a run built on it
  would come up with no network and look like a bridge bug. Take the argv from
  `/proc/<pid>/cmdline` of the running domain instead. Stand the harness up as
  its own gate with a measured throughput check before any demo runs through
  it -- an unshaped link that looks shaped would discharge the gate while
  testing nothing.

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

**Steal 9P's `Tflush` contract when the time comes, rather than re-deriving
it.** Raised 2026-09-18 while comparing this design against WSL's 9P layer.
The hard half of Cancel is not the message, it is the race: did the cancel
beat the reply, and when may the id be reused? 9P specifies that away, and the
rule is worth copying verbatim -- the server answers `Rflush` only once the
flushed request is definitely never going to be answered; a `Tflush` for
something already answered still gets an `Rflush`; and the client may not reuse
the tag until it arrives. Our ids are the tags. Write this into the primitive
while it is being designed, not into four transfer loops afterwards.

**To close it:** design the chunked-transfer primitive (audit 04.3) and give it
Cancel as a property. `do_put` is already a chunked sink and `do_get` a chunked
source, so the shape exists twice in the guest and wants naming rather than
inventing.

**Done now, because it is one word and it is a trap:** `ICC_PROGRESS_CLASS` is
registered alongside `ICC_BAR_CLASSES`. It was not, so the first
`CreateWindow(PROGRESS_CLASS, ...)` would have failed on a machine with no
debugger, presenting as "the progress bar does not appear". (Audit 01.10 found
the same gap independently; closes that clause of it too.)

**Progress UI scope cut (audit-2 03.14):** `PLAN.md:281-285` originally required
an ETA, transfer description, and determinate progress bar. These were explicitly
cut by decision: `win/app/xferstat.h`'s 1 Hz rate and transferred byte counters
(`rx 12k @ 45k/s`) in the status bar are sufficient to see the link is moving
without unnecessary UI complexity. `CANCEL` remains owned by this section for Phase 5.

**The primitive itself, now named.** Audit 04.3 counted four ad-hoc chunked
transfers already shipping with no shared shape: `send_html`
(`bridge/wire/link.py:84` since the `GuestLink` extraction, and `_send_html`
on `Bridge` is a two-line delegate to it now -- bridge to guest, display
text, no ack, no END frame, no Cancel), `Pass.push`
(`bridge/files/syncrun.py`, bridge to guest, `SYNCPUTEND`/`SYNCPUTACK`, an
`if_crc` precondition, no Cancel), `do_get` (`win/sync/syncrpc.cpp:378-456`,
guest to bridge, `SYNCDATEND`, no Cancel) and `send_out` (`win/rpc/rpc.cpp:253`,
guest to bridge, `EXECRESULT`, no framing at all, no Cancel). Phase 4's Export
would be a fifth, Phase 5's upload a sixth, Phase 6's screenshot strips a
seventh -- and 01.10 raised the guest-outbound half of this same gap
independently, from the "no guest-initiated chunked upload exists" angle.

`do_put` (`win/sync/syncrpc.cpp:188`) is already a chunked sink -- opens on
`off=0`, writes each chunk to disk, commits on END -- and `do_get` is already a
chunked source. Both matter more than the other two because neither ever
holds the whole payload in guest memory, which Export, upload and screenshots
all need. The shape to extract, per the audit's own worked design
(`docs/audit/04-planned-features.md`, its §C3): a **sink**
(`begin(dest) / write(chunk) / commit(crc) / abort(why)`) that `do_put`
becomes a caller of and that Export and an inline-image write would also use,
and a **source** (`open(reader_fn, total) / pump()`) that `do_get` becomes a
caller of and that the Phase 5 upload and Phase 6 strips would also use, both
in a new `win/xfer.c`, both carrying `id`/`total`/`off` and an END frame with
a status and a CRC -- what all three existing implementations converged on
independently -- plus `CANCEL\nid=`, added once during extraction rather than
retrofitted into four separate loops later. Deliberately NOT unifying
`_send_html`: it is a no-ack streaming hot path by design, and giving it a
round trip per block would cost it the thing it exists for.

**The guest-side routing half of this is mostly already solved.** Audit 01.36
worried that a third frame family would not fit `rpc.c`'s two-prefix model.
It now does: `rc_rpc_route` (`win/rpc/rpc.cpp:94`) dispatches on
`rc_frame_lane()` against `RC_LANE_TABLE` (`win/net/frameproto.cpp:42-46`,
four lanes -- UI/JOB/STREAM/IMMEDIATE), with `frameproto.h:70` inviting "add a
Phase 6 tool by adding a row to `RC_LANE_TABLE`, not by [editing the
router]." That landed closing 04.5. What is NOT generalised:
`id_matches_family`/`rc_rpc_submit` (`rpc.cpp:595-614`) still fight for one
job slot keyed on a single id prefix, so a third family wanting its own queue
depth -- a Phase 5 upload running independently of a live `exec`, say --
still needs a new lane and a new branch there. Narrower work than 01.36
described, since the table it asked for already exists, but not nothing.

**Export as HTML -- BUILT 2026-09-19 (Phase 4.2), and it did NOT wait for the
general primitive.** `EXPORTSTART`/`EXPORTDAT`/`EXPORTEND`, `send_export` on
`GuestLink` and a streaming sink in `win/app/export.cpp`. It has the three
things this entry said the shape needed -- an END frame with a status, a
`total`/`off` on every chunk, and a guest that never holds the document --
without `win/xfer.c` existing, because it is one caller and the primitive is
meant to be extracted from two or three.

What that decision costs, stated rather than discovered later: **the sink in
`export.cpp` is now the third implementation of the same shape** after
`do_put` (`win/sync/syncrpc.cpp:188`) and the guest side of `EXECOUT`. It is
also the cleanest of the three to extract from, because it was written
knowing the others exist: `begin`/`frame`/`cleanup` with a temp file renamed
on a clean END, an offset check that refuses a chunk which does not continue
where the last one stopped, and no `CANCEL` -- deliberately, since an export
is seconds on a LAN and the Cancel design belongs to the primitive (§6 above,
and 9P's `Tflush` contract). **Phase 5's upload should extract the sink from
`export.cpp` rather than write a fourth.**

Still true and still not done: `send_html` has no END and no status. It
returns without sending anything when a LIVE block exceeds the frame budget
(`bridge/wire/link.py`), and the caller is told nothing either way. An
oversized APPEND is truncated with a marker the reader can see rather than
dropped, which is the half of this that got fixed earlier. `send_export`
does NOT share that weakness -- it never truncates, because a shortened
export is not a smaller file, it is a broken one.

**Correction, verified 2026-09-18:** this entry previously cited that drop at
`bridge/server.py:847-848` and said `wire_yield` was skipped by
`_send_html`'s loop and `_send_page`'s run loop. Both statements were written
against the pre-`GuestLink` tree and are wrong now. The line range cited is
`_reissue_turn`, an unrelated function. And 04.12 is closed for these paths,
not open: `send_html` awaits `wire_yield()` between chunks
(`bridge/wire/link.py:143-146`) and `_send_page` awaits it between runs
(`bridge/server.py:2329`), each with a comment naming 04.12 as the reason.
`wire_yield` itself (`link.py:148`) records the measurement that earns it --
an uncontended lock with a `drain()` below the high-water mark has no await
that actually suspends, so 128 chunks of a one-megabyte push ran to
completion before any other task was scheduled at all. Nothing here needs
doing; the extraction above inherits a yield that already works.

**The Phase 5 upload** (04.25) hits a second, independent problem once the
primitive exists: the guest's outbound queue is one 64KB budget
(`RC_OUTQ_MAX_BYTES`, `win/net/net.h`) backing `rc_net_send`, which already
blocks its UI-thread callers up to 5s when full (`do_send`, `do_stop`,
`chats_switch`, the permission bar -- §1 above). A 10MB attachment is ten
times `MAX_FILE`'s 1MB cap (`bridge/files/syncrun.py:46`) and 33 minutes on a
modem, so Stop and the permission bar go unresponsive in bursts for the whole
transfer, on a path that has never run because the netem gate never has.
Fix order, per 04.25: close the netem gate first (§1 above -- no longer
blocked on sudo, per §5's last bullet) before trusting any number here, then
build the source primitive with Cancel and a determinate progress bar, then
cap the upload on both sides and report the cap rather than truncate silently.

**To close it, in order:** (1) `win/xfer.c` with the sink/source pair above,
maybe 150-250 lines given `do_put`/`do_get` are already ~130 combined and
mostly reusable; (2) `id_matches_family`/`rc_rpc_submit` grow a third queue
class if Export or upload need to run independently of `exec` -- decide this
when the first of the three callers is actually built, not before; (3)
`_send_html` gains an END frame and a status; (4) each caller (Export,
upload, screenshot strips) becomes a thin wrapper. None of the three callers
is built yet, so there is no size estimate for the whole thing worth stating
beyond "the primitive is smaller than any one caller and should come first."

## 7. The sync baseline is bound to one machine, and R7 needs a decision

**[R] ANSWERED 2026-09-18: one mirror per CHAT.** The three options below were
one per client, one shared with a per-guest baseline dimension, or one with a
writer lease. The answer keys the mirror to the CONVERSATION instead, which
bounds the number of mirrors by chats rather than by clients times chats -- the
combinatorial case the options were all trying to manage simply does not arise.

One mirror, one baseline and one guest still holds, so the reconcile stays
three-way and the state table five review rounds went into is untouched. No
N-way merge. No lease to time out or steal. `tree_id` keeps working as written.
Two people work at once by working on different conversations.

Default work folder: `C:\Gaston\<shortened chat name>`, with a different
folder selectable when the chat is started. The UI is a Phase 4 item, recorded
in `PLAN.md` under "The work folder, chosen when a chat is started".

**[R -- 2026-09-19] BUILT, with one deliberate departure: the conversation
moves INSIDE the mirror rather than the mirror moving.** `C:\WORK\<name>`,
not `C:\Gaston\<name>`. The two "things this decision creates" below were
the reason:

- *The sync root moving from something the guest declares to something it is
  told* does not happen. It is still one string read once from the INI and
  declared in HELLO. No new frame, no watcher restart, no second baseline, no
  `tree_id` change, no recovery pass on a fresh tree -- four chances to report
  deletions nobody made, avoided rather than handled.
- *The path budget* shrinks by the subfolder name only, which is capped at 8
  characters and 8.3-safe by construction (`Bridge.folder_name`), so
  `paths.check`'s existing arithmetic is unchanged.

What it does NOT give, stated plainly: two guests still share one mirror and
one baseline, so this is not R7 and does not make multiple clients work. It
gives one conversation one folder, which is what the Phase 4 item asked for.
Verified end to end on the guest: a chat created with the folder NOTES ran the
agent in `~/gaston-work/NOTES`, the write reached `C:\WORK\NOTES\HELLO.TXT`
on the next pass, and reopening the chat resumed in the same folder.

Two things this decision creates, neither of them free:

- The guest's sync root is one string read once from the INI at startup and
  declared in `HELLO`. Per-chat folders invert that: the BRIDGE has to name the
  folder for the conversation, so the root moves from something the guest
  declares to something it is told, probably on `SWITCHED`/`READY`.
- The path budget shrinks by the length of the prefix. `C:\WORK\` is 8
  characters; `C:\Gaston\` plus a chat name plus a separator is 11 plus the
  name, out of `RC_CAP_MAXPATH`'s 259 and before `RC_CAP_WORKRESERVE`'s 11
  bytes for `.gaston-tmp`. The shortened name needs a hard cap and
  `paths.check` must count the real prefix, or a long chat title silently costs
  the user the ability to sync deep paths.

The R7 items below are no longer blocked on a decision. They are ordinary
implementation work now, and `self.sync_dir` becoming per-conversation
(`04.19`) is the first of them.

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

**Implementation blockers, once the question above is answered.** Recorded
2026-09-18 from audits 02.3, 04.18, 04.19, 04.20, 04.21, 04.22, 04.35 and
01.35 -- eight findings, all the same object seen from `bridge/server.py`,
`bridge/agent/conversations.py` and the guest. None of this is reachable
before
the mirror decision above; it is what changes in code once it is made.

*Bridge-side per-guest state.* 02.3 counted eight attributes living on
`Bridge` that need to move to per-`GuestSession` (or into
`self._guests: dict[client_id, GuestSession]`, PLAN.md's R7 shape). One is
already fixed: commit `326a62a` closed the worst of them -- `run_on_guest`,
`tools.build()` and `_baseline_lock` now all take an explicit `guest`
parameter instead of resolving `self._guest` inside a closure. The audit
called it "the hardest R7 blocker" for good reason: a wrong resolution there
runs a command on the wrong machine while the permission prompt asking for it
appears on the right person's screen. `run_sync` (`server.py:1359`) still
hardcodes `guest = self._guest` and was missed by that fix. Five of the
remaining seven are still singular exactly as found: `self._agent_notices`,
`self.guest_sync_root`, `self._last_sync_reports`, `self.registry`,
`self.sync_dir`/`self.sync_state`. `self._early` (the sixth named in 02.3)
was closed the same day by a different commit (`a805df3`) for an unrelated
reason -- it held frames pipelined behind HELLO, per-connection state that a
second client's HELLO was overwriting before the first client's frames were
read, corrupting a live connection with a doomed one's data. Worth noting
because it is exactly the failure shape 02.3 warns about, not because R7
needed it fixed.

*Mechanical dict-migration, once `self._guests` exists.* Four findings are
each "this loops over one guest and needs to loop over the dict": `_reap_idle`
and `close()` (04.22, `server.py:2084` and `:308`); the conversation store's
one process-wide `~/.gaston-code/conversations.json` (04.18,
`bridge/agent/conversations.py` -- it was `sessions.json` plus two files
beside it until those were merged) needs its `mine` flag to become a client
id rather than a boolean, still one file, since it is already written on
every new session id and an atomic replace of one small file is the right
cost; `self.cwd` (04.19, read at five sites in `server.py`) needs a
per-client working directory once
clients get separate mirrors, gated on the mirror-per-client design above,
not to be decided independently of it; and the duplicate-connection
"reject, don't evict" check (04.21, `server.py:~460-462`) narrows from "a
guest is already connected" to "this `client_id` is already connected," which
wants a test and a written-down acceptance: `client_id` is an unauthenticated
assertion (one shared secret via `secrets.compare_digest`, `server.py:451`),
so a client claiming another's id inherits its transcript and pending
permission prompts. Accept that risk in writing rather than patch it
silently with per-client secrets later.

*Identity, on both sides.* 04.20 and 01.35's first point are the same gap
seen from bridge and guest: `_attach` (`server.py:432`) never reads `sess`
from HELLO even though the guest already sends it, so reconnect today works
only because there is exactly one `GuestSession` to attach to. The guest
side of that fix is `rc_config_save` never being called when a setting
changes (`win/core/ini.cpp:42`, called only from `rc_config_load`'s
first-run branch and from `inicheck.c`'s self-test) -- a minted `client_id`
would regenerate every launch without it, the same trap `tree_id` avoided by
deriving from the volume serial instead of storing one. This is the
identical gap §10 below records for the Phase 4 settings dialog; one fix
closes both call sites' need for it. 04.35 records the other half of guest
identity: `guest.sess_id` is one field for the client's connection identity
and the conversation's identity, which is why an effort fork used to leave
the chat list on the pre-fork name. That symptom is fixed (`on_system` now
picks up the real id during the fork's first turn, commit `a805df3`); the
field is still one name for two concepts, and splitting it into `client_id`
plus a bridge-owned `conv_id` is R7's job specifically, not a fix to make
ahead of it.

*A scope question 01.35 raises that nothing answers.* Its second and third
points -- the guest's single-instance mutex
(`"RetroClaudeSingleInstance"`) and `FindWindowA("RetroClaudeWnd")`
forbidding a second window (`win/app/main.cpp:1822-1824`), and every
worker-side module being a hard singleton (`g_net` in `rpc.cpp:56`,
`g_cancel`/`g_child` in `exec.cpp:47-49`, `g_root`/`g_put` in
`syncrpc.cpp:18,153`) -- only matter if R7 means multiple *windows on one
guest*. PLAN.md's R7 text says "the bridge should take multiple **clients**"
and describes `self._guests: dict[client_id, GuestSession]`, which reads as
multiple *guests*, each still one window. If that reading holds, 01.35's
points 2 and 3 need nothing done to them, ever. Say which reading is meant
when the mirror decision above is made -- it is the same shape of question,
and answering one without the other leaves half of 01.35 either wrongly
deferred or wrongly ignored.

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
  screenshot path, which is Phase 6 and does not exist yet. `deflate` covers
  transcript text today, and the Phase 4 compression round (audit-2 03.4) brings
  deflate to sync file bodies as well.
- **A forgotten scope cut.** The wire-format section named `enc=` as a value that should be
  free to extend "so adding a codec is a value change, not a protocol change" -- and then
  the codec set was never extended when the measured numbers (11.3:1 on `BI_RLE8`,
  near-zero guest cost) came in during Phase 0's screenshot experiments. Nothing recorded a
  decision either way.

**To close it:** when Phase 6's screenshot capture lands, decide whether the wire codec
gets an `rle8` value of its own (cheap: GDI already produces `BI_RLE8` bytes, so the guest
side may be close to free) or whether `deflate` alone is enough for the compressed sizes
Phase 6 actually measures, and write down which. Note that general guest->bridge
deflate compression (audit-2 03.3, 03.4, 06.7) was implemented in win/net/net.cpp for
bulk frames (EXECOUT, SYNCDAT, SYNCENT) with adaptive throughput measurement; wire codec
decisions (deflate vs rle8) for screenshots specifically remain deferred to Phase 6. Until
then this is not blocking anything.

## 9. Phase 6 input/window tools -- readiness gaps found ahead of the phase

**Status:** recorded 2026-09-18, from audits 01.38, 04.31, 01.41, 04.27,
04.28, 02.32, 04.11, 01.37, 01.10 and 01.39. Phase 6 has not landed; nothing
here blocks anything in flight. Recorded now because the audit found several
separate objects that all land the moment `list_windows`, `click` or
`send_keys` get their first line of code, and finding them once ahead of time
is cheaper than finding them once per tool.

**Thread affinity has no structural guard.** 01.38: `EnumWindows` and
`SendMessage(WM_GETTEXT)` are safe from the RPC worker thread, but
`keybd_event`/`mouse_event` are process-global, and PLAN.md's own rules --
`AttachThreadInput` before a cross-thread `SetFocus`, never send keys to
Gaston's own window -- have nothing in the router enforcing them; whoever
writes the first input tool has to remember both by hand. 04.31 is the same
finding at implementation depth, not a second risk: (1) the
`AttachThreadInput` call is worker-to-target and must be detached afterward,
or the worker thread permanently inherits the target's input-queue state;
(2) "never send keys to our own window" has to compare
`GetWindowThreadProcessId(target)` against our own **process** id and refuse
the whole process, not compare the target HWND against `g_main` -- a child
control shares the UI thread but is a different HWND, so the HWND comparison
would miss it. Verified: no cross-thread input code exists anywhere in
`win/` today (`win/rpc/rpc.cpp:85-93` already has a comment anticipating
where `list_windows` would dispatch; nothing further is built). **`PLAN.md`'s
`send_keys` rules are corrected to match**, because as written they did not
work from where the tool will run: "compare the target's thread against ours"
never fires on the RPC worker, whose thread differs from the UI thread even
when the target is Gaston's own input box. The detach-after-attach
requirement was missing there too.

**Cancellation is per-process, not per-job.** 01.41: `rc_exec_cancel` sets
one `g_cancel` and terminates one `g_child` (`win/rpc/exec.cpp:47-83`), so a
screenshot job and an `exec` could never be cancelled independently once
Phase 6 adds jobs that are not `exec`. Separately, `EXECKILL` on the reader
thread (`win/rpc/rpc.cpp:106-113`) logs the kill id and then discards it,
calling `rc_exec_cancel()` unconditionally -- an `EXECKILL` for a command
that already finished kills whatever started since. `rc_exec_arm()` narrows
the race but does not close it. Not urgent while `exec` is the only job;
becomes urgent the day a second job type exists.

**`list_windows` has no scope argument or node cap planned.** 04.27:
PLAN.md's own argument -- a control tree costs ~200 tokens against a
screenshot's ~850 -- holds for one dialog and inverts on a busy desktop. A
scope argument (foreground window / by class / by pid) and a node cap need
deciding before the first call, not after the first call returns every
control on screen. **`PLAN.md`'s Phase 6 tool table now carries both as a
requirement** rather than leaving the tool a bare name, since this is a case
of the design being underspecified rather than the code being absent.

**Inline screenshots need the chunked-transfer primitive, not a new one.**
04.28: the transcript renders under MSHTML/IE5 (`win/app/htmlview.cpp`), and
IE did not support `data:` URIs until IE8 -- confirmed, `htmlview.cpp:426,452`
only ever build `file:///%s\view.htm`. Inline images have to be written as
real files beside `view.htm` via the sink primitive §6 above already
describes for Export. Cross-reference, not a separate cost: this is one more
caller of `win/xfer.c`'s sink, not a fourth design. `PLAN.md`'s Phase 6
section now states the constraint, which it did not: the audit's complaint
was partly that a one-line feature description hid a renderer limit that
decides how the feature has to be built.

**No reusable RPC primitive on the bridge either.** 02.32 (with 04.11 folded
in -- they name the same extraction from two different files): `run_on_guest`
(`bridge/server.py:1249`) and the four `Pass` transport methods --
`scan_guest`/`push`/`pull`/`mkdir` (`bridge/files/syncrun.py:126,353,420,509`)
-- are five hand-rolled copies of allocate-id/send/await-future/handle-reply/
kill. `exec_lock` (`server.py:167`) encodes "one worker thread on the guest"
as a field specific to `exec` rather than a property a shared channel
enforces for everything. `_summarise` and the `EXEC_OUT_MAX` 64KB body cap
(`server.py:2362-2367`, `:1919`) are both tuned for text tools; neither has a
branch for an image result. Shape of the fix, per audit: `GuestRpc.call(verb,
headers, body, *, timeout, serialise=True) -> Result` on the Bridge, with
`run_on_guest` and the `Pass` methods becoming thin callers. This does not
block Phase 6's *first* tool -- one more hand-rolled copy costs nothing new --
but should happen before the *second* Phase 6 tool lands, or the duplication
this finding is about doubles again.

**The bar has no state for "the agent is driving."** 01.37: PLAN.md itself
calls this "probably the answer, and it is not designed yet" -- a fourth
`g_bar_mode` value after `BAR_NONE`/`BAR_PERM`/`BAR_HALT`
(`win/app/main.cpp:140-143`, unchanged), and the first one that likely needs
to block input to Gaston's own window rather than merely disable Send.
Related, and not yet a problem because nothing renders it: 01.10 flagged that
`g_perm_text` is a single-line `SS_ENDELLIPSIS` static at fixed height 26,
about 45 characters at 640x480 (`win/app/main.cpp:78,88,1391-1394`), while
PLAN.md's `send_action_sequence` design wants the bar to render a compact
multi-step summary (`type "hello" -> click [OK] -> wait "Notepad"`). Same
bar, same gap, two audits.

**Closed, not open: the stack-frame guard IS reachable from top-level `make
check`.** 01.39 restated a real memory-safety fact -- `win/sync/sync.cpp:100-116`
still carries the postmortem for an actual invalid-page-fault where an 8KB
stack local under a 62KB caller landed below the guard page, so every Phase 6
job needing a buffer must heap-allocate -- but its claim that the only
mechanical check for this, `tools/check/stackframes.py`, is "not reachable from the
top-level `make check`, per 01.31/CLAUDE.md" is stale. 01.31 itself is marked
`Fixed:` in the same audit file: the top-level `Makefile:43` now runs
`$(MAKE) -C win check`, and `win/Makefile:264` runs `stackframes.py` inside
that same `check:` target (`win/Makefile:213`). Verified directly: `make
check` from the repo root exercises it today. The underlying advice --
heap-allocate Phase 6 job buffers -- stays good practice; the tooling that
would catch a violation already runs.

## 10. The Phase 4 settings dialog -- three layers, in a real order

**Status:** layers 1-3 CLOSED (4.0 and 4.1h); layer 4 open. Recorded
2026-09-18, from audits 01.34, 04.13, 04.17 and 02.31. `IDM_HOST` WAS a
`MessageBoxA` telling the user to edit the INI by hand; it is
`rc_hostdlg_run` now (`win/app/hostdlg.cpp`). `IDM_EXPORT` is a sibling case
handled in §6 above and is still open. These four findings describe the same object -- what a real
`Server Host...` dialog needs -- at three different layers, with a real
dependency order rather than a single combined cost.

**1. Resource-compile infrastructure (04.17), the true prerequisite.**
**Closed 2026-09-18 as Phase 4.0** -- `win/app/gaston.rc` + `app/resource.h`
compiled by `wrc` and bound with the linker's `option resource=`, carrying
the R5 icon and a VERSIONINFO block. See `PROGRESS.md` "4.0 -- the resource
pipeline" for the Win98 icon-format facts, which are the part worth keeping.
This also closes the missing `dialogs.c`/`.rc` clause of 01.10, which named
the same gap from the "Phase 4's dialog" angle.

**One prediction in this entry was wrong, and is kept here rather than
edited away.** It said linking a resource would move `make check`'s CMOV
false-positive baseline, because objdump disassembles a section linearly and
a resource's binary data would decode as phantom instructions. Measured when
`gaston.res` landed: the count did not move. `objdump -d` disassembles only
sections flagged as code and emits exactly one `Disassembly of section
AUTO:` for this image; `.rsrc` is READONLY DATA and is never walked. The
existing false positive is inside AUTO, which is where Watcom puts read-only
data as well as code. `CMOV_BASELINE` stays at 1.

**[R -- 2026-09-19] The baseline is gone.** It moved three more times without
a real instruction behind it -- 1 -> 2 -> 1 -> 7 -- because adding a function
slides the read-only data that follows it and a different byte sequence lands
where objdump's linear walk reads a CMOV. `tools/check/cmovcheck.py` locates each
hit against the linker map instead and fails only on one inside an object file
the build compiled. The current seven are one in `uuid.lib`'s
`_GUID_FONTBOLD` and six in `clib3r.lib`'s `__DefaultExceptionHandler_`
padding, and they are printed by name on every run rather than summed into a
number nobody can check.

**2. `rc_config_save` is a one-line fix with no caller yet (04.13, and half
of 01.34).** It runs from exactly two places today: `rc_config_load`'s
first-run branch (`win/core/ini.cpp:39`) and `inicheck.c`'s round-trip
self-test (`:58`) -- never from anywhere a setting actually changes. An OK
handler calling it unconditionally would be its first-ever call against an
existing INI file. This is genuinely small (`rc_config_save();
try_connect();`) but it has nothing to attach to until either this dialog
exists or R7 mints a `client_id` that needs persisting (§7 above) --
whichever lands first should write it; building it speculatively before
either does just leaves an unreachable function.
**Closed 2026-09-19 by Phase 4.1h** (commit `e292b87`): the dialog landed and
its OK handler is `rc_config_save`'s first caller against an existing file.
Layer 3's backoff reset landed with it.

**3. What the dialog itself still has to get right (01.34).** The naive OK
handler -- `rc_config_save(); try_connect();` -- does not reset
`g_backoff_ms`/`g_retry_noted` (`win/app/main.cpp:228,230`, reset only on a
successful connect, `:651-652`), so fixing a typo'd host and hitting OK
still rides out up to the existing ~15s backoff before the first real
attempt. And a modal dialog re-enters the message pump while the reader
thread keeps posting `WM_RC_FRAME`.

**One correction to 01.34 on that last point, verified 2026-09-18.** The
re-entrancy half is not waiting for Phase 4 -- it is a designed path that
runs today. `MessageBoxA` pumps the thread queue exactly as
`DialogBoxIndirectParam` would, so `WM_RC_FRAME` is dispatched and
`render_frame` re-enters while a box is up, and **two of the existing boxes
are gated on `g_turn_active`** so they appear only while a turn is streaming:
the switch-conversation confirmation (`win/app/main.cpp:417-421`) and the
new-session confirmation (`:1692-1696`). A settings dialog lengthens the
window rather than opening a new class of failure. What genuinely has not run
is the other half, the `RC_MAX_INFLIGHT` backpressure gate
(`win/net/net.cpp:22,206-207`) -- a real `Sleep(10)` loop in the reader
thread, which fires only if the UI thread stops draining frames, and a
pumping modal does not stop draining them. Worth keeping the two apart: the
first is worth testing because it already happens, the second is reachable
only from a nested loop that does not dispatch, and nothing here has one.

Also lands here: Export as HTML, the permission rule
table, R8's Command menu and per-model description text all currently point
at `main.cpp` too, each another 60-150 lines into a file already at 1923
lines (it was 1783 when 01.34 was written).
**R8's Command menu landed 2026-09-19 (4.1f part 3) and did NOT go into
`main.cpp`** -- it is a fourth bridge-driven menu in `app/optmenu.cpp` plus a
button in `app/toolbar.cpp`, which is the shape the rest of this paragraph
was warning about. The prediction was right about the pressure and wrong
about where it would land.

**4. The settings-dispatch shape a dialog's new frames would multiply
(02.31). STILL OPEN, and now three rather than two:** `SETMODE` landed
2026-09-19 (4.1f part 2) as a third near-identical branch, delegating to
`_set_mode` the way `SETEFFORT` delegates to `_set_effort`. The dispatch
itself is a table now (`Bridge._DISPATCH`), so adding a type is one row
rather than an if/elif arm, but there is still no `_apply_setting(name,
value)` and each setting still writes its own notice, its own `_send_options`
call and its own failure path. `SETMODEL` and `SETMODE` differ in whether
they validate the value at all.

Original text: `SETMODEL` and `SETEFFORT` are already two near-identical
dispatch branches -- one inline (`server.py:1124-1129`), one delegated to
`_set_effort` (`:1133-1134`, `_set_effort` itself at `:2200`) -- and a
settings dialog's new `SET*` frames would add a third and fourth shape
without a unified `_apply_setting(name, value)`. One correction to make
when recording this: 02.31 as audited also claimed `guest.sess_id` stays on
the pre-fork value "until the next turn fixes it up," framing it as open.
That symptom is fixed (commit `a805df3`, see §7 above) -- `on_system` now
picks up the real id during the fork's first turn. Cite 04.35's framing, not
02.31's, for that part; the dispatch-unification point stands on its own
regardless.

**To close it, in order:** (1) ~~wire `WRC` into `win/Makefile`, write the
`.rc`, re-baseline CMOV~~ **done, 4.0 -- no re-baseline was needed**;
(2) build the dialog -- `DialogBoxIndirectParam` is
cheaper than a `.rc` template given (1) is already required for the icon
(R5); (3) OK handler calls `rc_config_save` and resets the backoff state;
(4) exercise a live stream with the dialog open at least once -- for the
re-entrancy, which already happens behind the two confirmations above and has
simply never been looked at, rather than for the `RC_MAX_INFLIGHT` gate, which
a pumping modal does not reach; (5) if
R7's `client_id` is also live by then, one `rc_config_save` fix serves both.

## 11c. One baseline per guest root -- built, and what it does not do

**Status:** built 2026-09-19. Recorded because it touches the sync layer and
because the shape is easy to misread as more than it is.

A baseline is bound to a `tree_id` and a `guest_root`, and `scan_guest`
refuses when either changes -- correctly, since treating a different directory
as the same one would conflict the whole tree. That refusal is exactly what
"Change work directory..." triggers, which made the feature brick
synchronisation with a message the user has to decode.

So a root gets its own baseline file: the legacy `base.json` keeps belonging
to whichever root it already records, and any other root gets
`base-<sha1(root)[:10]>.json` beside it. Nothing is migrated, nothing is
deleted, and switching back to a previous root finds its history exactly as it
left it. `Pass` now takes the FILE rather than re-deriving `base.json` from
the directory -- it did, which silently defeated the whole thing for one
build.

**What it is not.** This is not R7. Two guests still share one mirror and one
baseline per root, so the multi-client question in §7 is untouched. It makes
one machine able to change where it works without losing what it knows.

## 11b. "Always" still does not survive a session rebuild -- and would not
help the case it was written for

**Status:** open, recorded 2026-09-19. Listed in the Phase 4.1j plan as "make
Always survive a session rebuild ... Approve `wmake` once, never again."
Working through it found that the second half of that sentence cannot be true.

**What exists.** Pressing Always forwards the rule the CLI itself proposed,
forced to `destination="session"` (`permissions.session_scoped`). It holds
until the Session object is replaced -- which a switch, a new conversation and
the effort fork all do -- and then it is gone.

**Why persisting it would not deliver the example.** `allows_always` refuses
the Always button for `mcp__machine__*` outright, because MCP rule names
cannot carry specifiers: the only expressible rule is THE WHOLE TOOL, and one
press on "run vol c:" would be a standing grant of arbitrary command execution
on the user's machine. `wmake` runs through `mcp__machine__exec`. So the two
nags the eval found cannot be retired by persisting Always no matter how the
storage is built -- the button is not offered for them in the first place.

What persistence WOULD cover is Bash/Read/Write/Edit on the bridge, which
`off_limits` already confines to the working directory.

**The other half of the problem is mechanical.** There is no control request
for setting permission rules -- `ClaudeSDKClient` has `set_permission_mode`,
`set_model`, `interrupt` and a dozen others, and nothing for rules. The only
channel is `PermissionResultAllow.updated_permissions`, which needs a prompt
to attach to. So a restored rule could only be re-applied on the FIRST allow
of a new session, and until then the user is asked anyway.

**To close it, decide first, then build:** whether "approve once" should mean
something for `mcp__machine__exec`, which is the tool it is actually wanted
for. That is the stop-asking-mode decision the 4.1j plan deferred by name --
"the guard is built and exercised first; what gates on it is a separate
decision once you have lived with it" -- and `bridge/agent/cmdguard.py` is the
thing that would have to gate it. Building the storage before that decision
would produce a feature that works only where it is not needed.

## 11a. The transcript cache prunes nothing

**Status:** recorded 2026-09-19, when the cache was built (Phase 4.3,
`win/app/cache.cpp`). A deliberate omission, not an oversight.

Each page of each conversation is kept as a file under `<exe dir>\CACHE\`,
capped at the guest's own page budget -- 16KB on a 32MB box -- so a
hundred-page conversation is under two megabytes and the Win98 test machine
has 1.7GB free. Nothing deletes any of it, ever.

**Why it was not built.** Pruning needs a policy for "which conversation is
stale", and the only source for that ranking is the chat list, which this
same cache is holding a copy of -- so the rule would be deciding what to
throw away based on data it may itself be the last copy of. Getting it wrong
deletes the one thing that survives a disconnection, which is the whole
reason the cache exists. A wrong prune is silent and unrecoverable; unbounded
growth is visible in a `DIR` and costs megabytes.

**To close it:** either a total-bytes cap with oldest-directory-first
eviction keyed on the directory's own mtime (no chat list involved, which is
the point), or a "Clear cached history" item next to Configure > Server Host.
The second is smaller and answers the real complaint, which is "this machine
is keeping something I did not ask it to keep".

**Also not built, and the same call:** paging BACKWARDS through a cached
conversation while disconnected. The pages are all there and `page_count()`
already reports how many, but the page bar's buttons send `GETPAGE` over a
socket, and making them silently mean something else when the socket is down
is a control that lies about what it does. The restored view shows the last
page and says plainly that it is not live.

## 11. Smaller Phase 4/5 gaps found ahead of their phases, not yet closed

**Status:** recorded 2026-09-18. Four unrelated findings, none large enough
for their own section, none blocking anything in flight.

**04.16 -- HALF CLOSED 2026-09-19.** The rules view shipped (Phase 4.2,
`permissions.rules_view`) and it says in as many words that it is incomplete,
which is what this finding asked for. It also does not parse
`.claude/settings*.json` at all -- working through this finding is what made
it clear that those rules are not in effect for this client, so showing them
would have been worse than showing nothing. **The Phase 6 half is still
open**: nothing has verified that every `mcp__machine__*` call reaches the 3.0
prompt bar, and that wants its own spike before Phase 6 relies on it.

Original text: **the permission rule table and the Phase 6 prompt-bar
assumption share one gap.** `PROGRESS.md`'s 2026-09-17 entry ("the auto-allow policy was
deleted, because Claude Code already does it") measured that Claude Code
resolves `Read`/`Glob`/`Grep` inside `cwd` and sandboxed `Bash` locally,
never reaching `can_use_tool` at all. A rules table read from
`.claude/settings*.json` (the planned 4.5) cannot show those local
resolutions, so it has to say plainly that it is incomplete, or it reads as
"these are all the things that can happen without asking," which is already
measured false. The same measurement undercuts Phase 6's assumption that
every `mcp__machine__*` call reaches the 3.0 prompt bar (PLAN.md's
Permissions note under Phase 6) -- that wants its own spike to verify before
Phase 6 relies on it, since this exact class of assumption was wrong once
already. Nothing to build yet; something to verify before either Phase 4.5
or Phase 6 ships.

**04.23 -- CLOSED 2026-09-19 by Phase 4.1f.** Two comctl32 toolbars now
exist (`win/app/toolbar.cpp`): the settings bar under the input box and a
one-button New chat bar right-aligned on the page-bar row. Phase 5's attach
control has somewhere to go.

**The measurement this entry asked for could not be taken, and that is the
part worth keeping.** It said `GetFreeSystemResources` was "already ruled out
(9x-only 16-bit export, no 32-bit equivalent worth resolving)", which is half
wrong: `win/checks/probe.c:75-93` already resolves it through `rsrc32.dll`
and reports system/GDI/USER separately. But on this Win98 SE install the
answer is `rsrc32.dll not present`, measured 2026-09-19 -- the Resource Meter
is not installed, so there is no USER/GDI percentage to read without
installing it first. What WAS measured, with only the app running (launched
from Start > Run, no DOS box, no Explorer window): **5208 KB free at window
creation, 4568 KB after the chat list, 4580 KB after a page**, against the
2420/1060 marks recorded when the chat list landed. The toolbars are not a
memory problem. Anyone who wants the USER split specifically has to install
the Resource Meter on the guest first.

**04.24 -- `off_limits` is not wired to a path that does not exist yet.**
`off_limits` (`bridge/agent/permissions.py:220`) refuses path-tool calls
outside `cwd`. In Phase 5.1, `_validate_attach_target` applied the strict
containment check to mirrored attachments (`attach` parameter), forbidding
any path escapes or symlink traversals. In Phase 5.2/5.3, out-of-band streaming
via `ATTGET` directly streams external files and memory-safe 16-scanline strip
desktop screenshots over the wire using `SYNCDAT`/`SYNCDATEND` chunks without
workspace pollution or wire-exposed traversal holes. Closed in Phase 5.3.

**04.34 -- no shared frame-dispatch table, only a closed instance of the bug
it enables.** The specific failure -- `TOOLRESULT` going unrendered for all
of Phase 3 because a frame type was added on one side and missed on the
other -- is closed (04.7, marked `Fixed: (in part)`): every frame the bridge
sends now has a guest branch, and the guest's own routing table
(`RC_LANE_TABLE`, `win/net/frameproto.cpp:42-46`, closing 04.5) means the
transport itself is no longer a dispatch site on that side. What is not
built is the audit's own proposed fix for the general case
(`docs/audit/04-planned-features.md`, its §C5): a single `FRAMES` table in
`bridge/wire/protocol.py` naming every type, its direction and its
guest-side lane, plus two coverage tests -- one asserting `Bridge._handle`
(`server.py`, currently a plain if/elif chain of about 19 `f.type ==`
branches, no table) has a branch for every guest-to-bridge type, one
asserting the guest's dispatch covers every bridge-to-guest type. Phase 4
adds `EXPORT`/`EXPORTDAT`/`EXPORTEND` and a settings frame; Phase 5 adds
attach and upload; Phase 6 adds the UI-tier request/reply pairs -- each one
is a chance to repeat the exact bug that motivated this. Belongs with
whichever of those lands first, not before.

**04.35 -- the first tool call is usually the write, and it is the call that
commits the folder.** A conversation gets its folder on the first tool call
(`_ensure_work_dir`), and the prompt's no-folder branch (`machine.py:60`)
tells the agent its tools operate on `{workdir}` -- the shared root -- and to
"write in the folder the notice names, once it names one". The agent cannot
get a notice without making a tool call, and the natural first tool call is
the write. So it writes to the root, that same call creates
`C:\Gaston\<Name>`, and the file it just wrote is outside the mirror that was
created for it.

Measured on the Phase 4.18 acceptance run, which is what makes this worth
recording rather than reasoning about: 5 `exec` calls total, of which **three
were this** -- two `wcl386 ... hello.c` returning `Unable to open "hello.c"`,
then a `dir C:\Gaston\WRITEME4` to find where the file had actually gone. The
remaining two were the real work (compile, run). Zero were toolchain
orientation.

The agent recovered on its own in one `Bash` (`mkdir -p ... && cp ...`) after
reading `sync_status`, which already says "EDITED HERE BUT OUTSIDE THE
MIRRORED FOLDER". So the machinery reports the problem correctly; the cost is
the retries before it thinks to look.

Not a one-line prompt fix, which is why it is here and not done. The
candidates all have a cost: telling the agent to make a throwaway tool call
first is a wasted round trip on a 200MHz machine; moving root-written files
into the folder at commit time means the sync layer relocating a file the
agent believes it knows the path of; naming the folder in the prompt before
it exists is exactly what `_sync_paragraph`'s docstring says sent an agent
off to diagnose a sync fault. Wants its own measurement, against this
transcript as the baseline.

---

## 12. RetroAI as an external MCP server, and the two restart problems behind it

**Status:** deferred by decision, 2026-09-23. Designed and reviewed against the
source; nothing built.

The idea: expose the bridge as an external MCP server so an agent running on
the modern host (Claude Code, Antigravity, Cursor) drives the retro machine
directly, editing files with host tooling while the sync layer carries them
across. Gaston Code keeps working; this is a second front end, not a
replacement.

It is recorded here rather than started because the design review turned up a
bootstrap problem that is worth solving deliberately, and because the one piece
that is unambiguously worth building is separable from the rest.

### What the review settled, so it is not re-derived

- **The endpoint has to live inside `bridge.server`.** The bridge owns the TCP
  socket to the guest, the `SyncCoordinator`, the `GuestSession` and
  `exec_lock`; a separate process has none of them, and reaching them means a
  second RPC protocol beside the one the project already has. The `mcp` package
  is already in `.venv`.
- **`permdesk` is not only a dialog.** The path is `can_use_tool` ->
  `PermissionDesk` -> `cmdguard.reason()` (`bridge/agent/cmdguard.py:570`),
  which parses the command under the named shell's grammar to find writes
  outside the mirror, device names and unresolvable paths -- *"None means
  'nothing found', NOT 'safe'. Nothing here is ever a grant."* An external path
  must **keep `cmdguard` and skip only the bar**, returning the finding in the
  tool result so the host agent's own prompt shows it. Bypassing the desk
  wholesale loses the analysis that makes the host's prompt worth answering.
- **A `sync_init` that takes a guest directory cannot work.** `win/core/ini.cpp:35`
  defaults `sync.root` to `C:\Gaston` and `rc_sync_contained` refuses anything
  outside it, so the obvious default of `C:\WORK` is refused by the guest.
  It must take `host_dir` only and derive the guest side under the existing
  mirror, which also keeps §7's one-mirror-one-baseline invariant.
- **Tools are named bare** (`exec`, not `mcp__machine__exec`). The prefix is
  synthesised by the client from its own config key --
  `bridge/agent/tools.py:3-7` states it.
- **`sync_status` and `sync_resolve` already exist** (`tools.py:204`, `:218`).
  Without the second, an external agent can reach a conflict it cannot clear.
- **`read_file` fails the exec test** -- `type` does the job. If it is ever
  wanted, build it on the existing `SYNCGET` path rather than a new RPC.
- **No shell may be named in the schema.** Since 02.5 the machine reports what
  it has; `"command.com"` and `"cmd.exe"` are both in
  `tests/python/meta/test_no_os_in_code.py`'s `BANNED` list.

### The bootstrap problem, which is the real reason this is deferred

Anything that is both the agent's hands and the thing being edited cannot
restart itself. It happens twice, one layer apart.

**The bridge.** The MCP endpoint lives in the process being edited, so applying
a bridge change kills the agent's own connection. The workable answer is to
invert the lifecycle: MCP clients *spawn* stdio servers, so `python -m
bridge.mcp` becomes a supervisor that owns the bridge daemon as a child,
proxies machine tools to it, and carries control tools (`bridge_status`,
`bridge_restart`, `bridge_logs`) that still work while the bridge is down. The
agent's session never drops because the supervisor never dies. It also removes
the `pkill -f "python -m bridge.server"` trap, which matches the wrapping shell
of the command running it. The recursion does not vanish -- editing the
supervisor still needs a human -- so it stays small and boring.

**The guest.** `RETROCL.EXE` serves the RPC that would replace `RETROCL.EXE`,
and Win98 maps the running image: `tools/vm/dos.py:151` records that the
resulting `COPY` failure *"prints as one line in a console whose screendumps
are frequently stale, so it reads as success. Twice a test ran against the
previous binary and its result was taken at face value."* Today's workaround is
to install under a fresh name each run. **This is why `tools/vm/` cannot be
deleted** -- the out-of-band channel is structurally required, and any plan
claiming external MCP removes it is wrong.

### The guest-side updater, if this is picked up

A small, rarely-changing `RETROUPD.EXE` gets the update in-band without
splitting the client into a service and a GUI -- the `[R2]` one-binary decision
(`PLAN.md:1981`) stands. The agent syncs the new exe into the mirror (286KB
against `MAX_FILE`'s 1MB), execs the stub, the stub waits for the client to
exit, swaps, relaunches, and the client reconnects on its own backoff.

- **Do not invent the swap.** `rc_sync_commit` (`win/sync/sync.cpp:612`) already
  does it on 9x: CRC-verify first, move the old aside rather than deleting it,
  move the old back if the second move fails, with `rc_sync_recover` knowing the
  naming. `MoveFileExA(REPLACE_EXISTING)` is NT-only, so it cannot be atomic and
  the aside is what makes every intermediate state recoverable.
- **Roll back on failure to start**, which is what makes an in-band updater
  responsible rather than a way to brick the guest in-band.
- **Liveness is not the test.** A binary can start perfectly and never connect
  -- the `PROTO_VERSION` 1->2 bump produced exactly that (`[bad_version] bridge
  speaks 2, guest speaks 1`), and it is the failure most likely while iterating
  on the wire.
- **Invert the marker.** Wait for the absence of a *failure* marker, not the
  presence of a success one, or starting the client while the bridge is off
  rolls back a good binary. The guest already distinguishes a refusal (an
  `ERROR` frame it received and renders) from an unreachable bridge (a socket
  error and a backoff).
- Leave `RETROCL.INI` out of the rollback: it is read from the exe's own
  directory and `win/core/ini.cpp` defaults anything missing.
- Still uncovered: a page fault leaves an orphaned child, because
  `rc_exec_cancel()` runs only on the graceful path (`rc_net_stop` ->
  `rc_rpc_stop`, `win/rpc/rpc.cpp:808`). Survivable on a dev box; worth knowing.

### What already works, which makes this cheaper than it looks

- `run_on_guest(..., guest=None)` (`bridge/server.py:2655`) already binds a
  guest rather than resolving `bridge._guest` at call time -- audit 04.1. The
  multi-machine question is already answered.
- The guest reconnects by itself, 1/2/4/8->15s on `WM_TIMER`. A bridge restart
  needs nothing done on the retro side.
- No orphaned children on a clean link loss (`win/rpc/rpc.cpp:808`), and
  `_fail_execs` (`server.py:3823`) resolves the bridge's pending futures.
- Baselines persist per root as `base-<slug>.json` under `--sync-state`, forced
  outside `--sync-dir` (`server.py:4556-4559`), so they survive a restart.
- `asyncio.start_server` sets `SO_REUSEADDR`, so rebinding is not the hazard; an
  old process still holding the port is.

### The one piece worth doing anyway

**Rootless execution has no MCP dependency and fixes litter in the product as it
stands.** `run_on_guest` calls `_ensure_work_dir` unconditionally
(`server.py:2682`), whose docstring states the rule -- *"THE FIRST TOOL CALL IS
THE COMMITMENT"* (`:1247`) -- so asking "what is the volume label on C:" creates
a mirrored folder. Guarding that on sync actually being initialised is a small,
testable change. `ensure_sync_flush` needs no guard added; it already returns
early without a mirror (`:2849-2852`).

**To close it:** decide whether the agent restarts the bridge itself (the
supervisor) or a human does; decide what happens when two MCP clients each want
to own one bridge daemon (pidfile-and-attach, or refuse the second and say who
holds it); then build rootless execution, `init_sync_root`, the embedded
endpoint, and the stdio supervisor, in that order. The updater stub is
independent of all of it and can land whenever the guest reflash loop becomes
the bottleneck.

---

## 13. Decoration ships from the bridge, so only one client can ever look right

**Status:** deferred by decision, 2026-09-25. The seam is named; nothing moved.

This is not a challenge to **the guest transforms nothing** (`CLAUDE.md`). That rule
is right and it stays. The problem is that "how a block looks" turns out to be two
jobs that were never separated, and only one of them is a transform.

- **Structure** -- markdown to blocks, escaping, pagination, the encoding fold. Needs
  lookahead and a real parser (`PLAN.md`, "The bridge owns markdown"). Writing it in
  C++ would cost image bytes and heap on the one resource the project is tightest on.
  It belongs on the bridge and is not in question.
- **Decoration** -- `STYLESHEET` (`bridge/view/html.py:448`), inlined into the skeleton
  by `document()` (`:622-630`) and sent once per connect from `server.py:341`. This is
  one client's appearance, written to IE5's constraints, and it ships from the far side
  of the wire.

**Why it matters.** The standing rule is that the bridge, the protocol and the tool
surface should survive being pointed at MS-DOS, Windows 3.1 or NT 4. A DOS client with
no MSHTML cannot use a single byte of that stylesheet. A more capable client -- one
with a real layout engine, or a different screen -- cannot restyle its own transcript
without a bridge change. The bridge is currently the authority on something it has no
business knowing.

**The seam already exists, and the guest already declared its half.**
`win/app/htmlview.h:53-76` defines `RC_SLOT_DONE`/`LIVE`/`STATUS` and `RC_CSS_STATUS`,
`NOTICE`, `ERROR`, `USER`, `PENDING`, `PERMDONE`. Those *names* are a cross-language
contract, pinned by `tests/python/meta/test_guest_css.py`, and the guest already
composes its own markup against them -- `win/app/statusbar.cpp:146` writes
`<div class="status">` itself, on the guest, today. So the vocabulary is shared and the
appearance is owned by the bridge, which is exactly backwards.

`htmlview.h:55-58` makes the argument by accident. The status slot is optional *because*
"the bridge's DOC is authoritative and may be an older build than this guest" -- an
appearance that ships from the bridge means a guest cannot fix how the guest looks by
rebuilding the guest.

### What it would take to close

1. **Move `STYLESHEET` to the guest as a resource**, and let the bridge send the
   skeleton with no `<style>` block. The class vocabulary becomes a declared contract
   rather than an implicit one -- per **client rules are declared at handshake, never
   assumed**, that means a `HELLO` capability saying which vocabulary version this
   client understands, and the bridge refusing an unknown value rather than guessing.
2. **Decide what export does.** This is the awkward one.
   `bridge/view/html.py:633-657` builds a standalone file with `STYLESHEET + EXPORT_CSS`
   inline, on the bridge (`bridge/services/session.py:116`), and its docstring gives a
   real reason: an export that looks different from what was on screen invites the
   question of which one is right. If the bridge no longer holds the stylesheet it
   cannot inline it. The options are that the guest renders its own export, that the
   export links a `.css` file written beside it (there is precedent -- IE5 has no
   `data:` URIs, so inline images are already real files beside `view.htm`; see the
   MSHTML constraint in `PLAN.md`), or that export keeps a deliberate bridge-side copy
   and accepts the drift.
3. **Regenerate the frame goldens.** `tests/python/server/test_frame_goldens.py` pins
   the DOC frame, which carries the stylesheet.
4. **The class-name guard is already fit for this.**
   `tests/python/meta/test_guest_css.py` matches
   `#define\s+(RC_CSS_\w+)\s+"([^"]+)"` and asserts it found as many as the header
   defines, so a class with a space in it (`RC_CSS_PENDING "you pending"`) is caught
   and a partial match fails loudly. It is the right shape to carry a real contract.

**Free and separable:** the shipped `STYLESHEET` is 8445 bytes, of which **4919 are
CSS comments**. Stripping them leaves every rule byte-identical. `doc()`
(`bridge/wire/protocol.py:171`) builds a plain `Frame` with no `enc` header, so unlike
`TEXT` and export data this one is **never compressed** -- those 4919 bytes go on the
wire verbatim, once per connect and again on every reconnect. On a 5 KB/s link that is
close to a full second of the time between plugging in and seeing a transcript. It
needs the DOC goldens regenerated and it depends on none of the above.
