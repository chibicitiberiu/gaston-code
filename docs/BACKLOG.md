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
in `Connection::send` (`win/wire/connection.cpp`) with a hard drop and
`RC_SEND_FULL`):

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

**The tool exists (2026-09-27).** `gremote.exe` is built and proven on the
QEMU guest -- exec, processes, the whole fs lane and `ship()`, measured in
`docs/measurements/gremote-vm-20260927.txt`. What is still owed is the
hardware, and two preconditions nothing in this tree records: whether the
P200 has a NIC with a configured TCP/IP stack, and how `GREMOTE.EXE` reaches
it the first time -- its only proven channel is a 1.44MB floppy via
`tools/gen/mkw98kit.sh`, which is where it should be added. Autostart
through `RunServices` is the other missing piece for an unattended box; it
needs a shipped `.REG` rather than registry calls in C, because ADVAPI32 is
not on `tools/check/imports.py`'s allow-list.

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
lock-serialised -- `Connection::signalStop` (`win/wire/connection.cpp`) argues a lock would block shutdown behind an
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

**gremote implements the CONTRACT and closes none of the LIST (2026-09-27).**
`docs/REMOTE.md`'s protocol section says "`CANCEL id=` follows `BACKLOG.md` 6",
which reads like this entry can be ticked off once `gremote.exe` answers a
`CANCEL`. It cannot. gremote's lane worker is new code with its own id table;
it takes the `Tflush` rule above -- the ack means the request will never be
answered, and the id cannot be reused before the ack -- and shares nothing
with the four items below. Item (1) is arguably discharged by `win/fs/xfer.h`
(libfs, step 5a) rather than by a `win/xfer.c` that will now never exist;
item (2) is specifically the CLIENT's `rpc.cpp`; item (3) is bridge Python.

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
(`"GastonSingleInstance"`) and `FindWindowA("GastonWnd")`
forbidding a second window (`win/app/main.cpp:1822-1824`), and every
worker-side module being a hard singleton (`g_net` in `rpc.cpp:56`,
`g_cancel`/`g_child` in `exec.cpp` (gone since libexec: per-job now), `g_root`/`g_put` in
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
Phase 6 actually measures, and write down which.

**The measurement is in, and it is not a flat no** (2026-09-26, gate 0.10 and
the Phase 0 screenshot runs). RLE8 came out **2.8x faster and 2x larger** than
deflate on the same frame. That is a loss on a LAN, where bandwidth is free and
the guest's CPU is not, and a win on a modem, where it is the other way round --
which is exactly the trade the adaptive codec already chooses between for
`EXECOUT` and friends. So `rle8` is a third value for that chooser, not a
rejected idea, and the earlier reading of this section as "decided against" is
wrong. Note that general guest->bridge
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

**Cancellation is per-process, not per-job.** *Closed 2026-09-26 by
libexec (`docs/libexec-plan.md` steps 1-2).* 01.41 found one `g_cancel` and
one `g_child` in `exec.cpp`, and an `EXECKILL` that discarded its id, so a
kill for a command that had already finished killed whatever started
since. Now each EXEC runs in its own `ExecJob` (`win/exec/execjob.h`), and
`rpc.cpp`'s `cancel_job` compares the id under `g_lock`: a stale or id-less
`EXECKILL` is logged and ignored, and one for an EXEC still in the job slot
takes it out so it never runs. `rpccheck` proves all three.

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

**Status:** CLOSED 2026-10-04. Layers 1-3 closed in 4.0 and 4.1h; the
dialog itself shipped as Configure > Options... (docs/OPTIONS-PLAN.md). Layer 4
-- the settings-dispatch shape -- was overtaken rather than refactored: the
person's settings now arrive together in one PREFS frame (bridge/prefs.py,
`OptionsService.on_prefs`), validated against one registry and answered with
PREFSACK, so a new setting is a key, not a fourth hand-written SET* branch.
SETMODEL/SETEFFORT/SETMODE stay as the per-chat menu path; they were not
merged into PREFS, because they change the OPEN chat and PREFS sets defaults.
Previously: layers 1-3 CLOSED (4.0 and 4.1h); layer 4 open. Recorded
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
(`RC_MAX_INFLIGHT` and `ClientLink::onFrame` in `win/net/net.cpp`) -- a real `Sleep(10)` loop in the reader
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

**CLOSED 2026-10-04 by execution modes.** The CLI's Always is gone: the bar's
button is "This chat", a grant of the call's CATEGORY (bridge/agent/policy.py
`GRANTABLE`: changing files and running trusted programs in the work folder,
and one exact program built there), kept in the conversation's settings for
the machine that gave it, so it survives rebuilds and forks, and cleared when
the mode changes. The whole-tool objection below does not apply to a
category. No CLI `updated_permissions` is forwarded any more, except Plan
first's setMode on approval. The original entry follows.
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

## 12. Gaston as an external MCP server, and the two restart problems behind it

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

**The guest.** `GASTON.EXE` serves the RPC that would replace `GASTON.EXE`,
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
- Leave `GASTON.INI` out of the rollback: it is read from the exe's own
  directory and `win/core/ini.cpp` defaults anything missing.
- Still uncovered: a page fault leaves an orphaned child, because the running
  `ExecJob` is cancelled only on the graceful path (`rc_net_stop` ->
  `rc_rpc_stop`). Survivable on a dev box; worth knowing.

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

---

## 14. Stopping an exec leaked a process on Win98 -- fixed except for DOS programs

**Status:** found and fixed 2026-09-26/27, measured on the VM throughout
(`docs/measurements/execprobe-vm-*`, `execkill-vm-*`).

On 98 a COMMAND.COM job given std handles is, once it starts a program,
REDIR32.EXE (the pid CreateProcess returned) -> the DOS VM (WINOA386.MOD)
-> the program. The old stop called TerminateProcess on that pid: TRUE, and
nothing -- the command ran on and REDIR32 stayed, ~150KB until reboot.
Every exec timeout and EXECKILL leaked one.

What the probes found (`execprobe --stops`, `--redir`, `--redir2`, `--gentle`):
- A normal end never leaks, whatever carries the output.
- REDIR32 comes with ANY std handle (a file handle as much as a pipe); a
  console stub in between (KB150956's pattern) only moves the zombie into
  the stub, at ~600KB a stop.
- The real cause: TerminateProcess on the DOS VM leaves it half-dead, and
  whatever waits on it -- REDIR32, or a stub -- can then never exit or be
  killed. Pipes closed, GenerateConsoleCtrlEvent (returns 0 on 98), Ctrl+C
  or WM_CLOSE to the VM's window: none ends it either.
- Killing only the Win32 programs below the VM lets the job END ITSELF in
  about 6s, and nothing is left. Twice in the probe; in the client, a
  Win32 program under the shell now stops in 0.3s (EXECKILL) and 3.4s
  (timeout) with no leak.

**The fix** (`Processes::stopJobTree`): the Win32 side of the tree first,
then an 8s grace for the job to end itself; only a job still running after
that -- a DOS program inside the VM -- gets the VM killed.

**Left:** that last case still costs one REDIR32 and the VM (a DOS program
that has to be stopped: a hung `choice`, an interactive tool), logged by the
client as `stopLeaked`. Nothing found from user mode ends a DOS program in
its VM cleanly; it would take VMM's own VM termination (a VxD). A DOS
program that ends by itself leaks nothing.

**Accepted as-is, by decision 2026-09-27.** Research found no existing
helper: Emacs gives up on killing DOS subprocesses on 95, Python's
`w9xpopen` is the console stub that only moves the zombie, and Tcl avoids
the problem by never running one against a live pipe. The only real
mechanism is VMM's `Close_VM`/`Nuke_VM`, callable from a VxD alone. If
forced DOS stops turn out common, the next step is a time-boxed dynamically
loaded VxD prototype (joncampbell123/doslib has Win9x VMM headers).

**Open risk, one sighting:** the guest wedged in ring 0 as a Toolhelp32
snapshot was taken (`EXECPRB --list`) on a box holding 3 of these zombies
(`docs/measurements/wedge-vm-20260927.txt`). Against it: the same `--list`
ran cleanly many times on boxes holding 3, 20 and 22 of them
(`execkill-vm-*`, `execprobe-vm-stops-*`). It matters because
`stopJobTree` snapshots too, on the client's worker, and its last resort is
what creates the zombies. If it recurs, the cheap guard is to skip the
snapshot while a leaked job is known to exist and fall back to the NT path.

## 15. The injection gates leave the guest in an unknown state

**What happened.** On 2026-09-26 a session ran gates 0.15d (system-wide
`WH_CBT` injection), 0.15f (thread-targeted injection, four hook types) and
0.15g, plus the full `autocheck` battery, which installs system-wide
`SetWinEventHook` hooks. Every gate returned and the battery passed 158 of
158. Some hours later the guest was found spinning at a full vCPU,
processing no keyboard input at all, and holding no connection. The screen
was untouched from the last command.

**What is NOT established.** That the gates caused it. Nothing was captured
between the last good interaction and the discovery, the hooks are all
removed on the paths that were taken, and a Win98 guest is capable of
wedging without help. The suspect is obvious and the evidence is absent,
which is the honest summary.

**Why it matters anyway.** These gates are the only thing in this project
that puts our code inside programs that did not ask for it, and the one
that has already killed Explorer once. A gate that cannot say whether the
machine survived it is not finished, however green its own output is.

**To close it:**

- A **health check after the battery**, not just during it: the kit's
  `TEST.BAT` should end by proving the guest still answers — a keystroke
  that changes the screen, a window that still takes focus, and a write
  that lands. A run that ends with 158 passes and a wedged machine should
  report a failure, and today it reports a pass.
- **Run the injection gates last and separately.** They are in the middle of
  `TEST.BAT` now, so everything after them is measured on a machine they may
  have damaged.
- **A known-good rollback in the loop.** There are libvirt snapshots, but a
  live internal snapshot is refused because the raw floppy `fda` cannot take
  one. Either detach the floppy for the duration or script a
  copy of `vm/win98.qcow2` before a run that injects.
- **Find out whether it reproduces.** Boot clean, run only 0.15d and 0.15f,
  and leave the machine alone for an hour. If it wedges, that is the answer
  and section D needs rethinking before D2 is built on it. If it does not,
  the battery is the place to look next.

Until this is closed, treat `inproc` and the injection gates as
**attended-only**: run them when somebody can watch the guest, and not as
part of an unattended sweep.

## 16. An agent can be told a control is occluded and have no way to fix it

**Status:** found 2026-09-27 doing the Phase 6 end-to-end check
(`docs/measurements/e2e-ui-vm-20260927.txt`); **fixed the same day**, measured
on Win98 SE (`docs/measurements/focus-vm-20260927.txt`). The residue is at the
bottom.

`Input::clickAt` refuses when `WindowFromPoint` at the target is not the
target (`win/automation/input.cpp:121`), and that refusal is right: clicking
anyway presses whatever is on top. The bridge tells the model
"occluded ... Bring the window forward first"
(`bridge/prompts/tools.toml`).

There is no tool that brings a window forward. `FOCUS` is in the op table at
`REMOTE.md:308` and was never implemented; the client's UI lane handles the
four `UI*` types only (`win/ui/uirpc.cpp`). So the advice names an action the
agent cannot take.

It is not a corner case. The client's own window covers the middle of an
800x600 screen, Windows centres many dialogs there, and the client comes to
the front every time somebody presses Allow on its permission bar. During the
check this made the target unclickable as a direct consequence of approving
the tool call that was going to click it. It was worked around by dragging the
dialog out from under the client, by hand, from the host.

What it would take:

- a `FOCUS` op that does `SetForegroundWindow` on a resolved target, with the
  `SPI_SETFOREGROUNDLOCKTIMEOUT` question on the P200 checklist
  (`REMOTE.md:664`) answered first -- a background process may not be able to
  do it at all on 98;
- or `click` growing a `raise=1` parameter that brings the target's root
  forward, re-checks occlusion, and only then clicks. This keeps it one round
  trip, which on a 200MHz machine behind a socket is the thing that matters;
- and either way, saying in the refusal which window is on top, so a model
  can tell "your own window" from "a dialog you have not noticed".

### What was done

`Input::clickAt` now raises the target and looks again before refusing, and
only refuses if the raise did not clear the point. The library already had
`Input::focus()` -- restore if iconic, `SetForegroundWindow`, an
`AttachThreadInput` fallback, verify -- so the gap was never that the client
could not raise a window. It was that `clickAt` never asked it to, and the
refusal advised the caller to do it instead.

Raising happens only when the point is covered. An uncovered window is
activated by the click itself, and a foreground change nobody needed is one
taken from whoever is at the keyboard.

`focusprobe` settled how, and the answer was not the obvious one:
`BringWindowToTop` and `SetWindowPos(HWND_TOP, SWP_NOACTIVATE)` are both
IGNORED across a process boundary on 9x. The cheap, focus-preserving route
does not work; activation is the only one that does.

The refusal that remains now names what is on top (`on_top=` on UIEND, from
`Input::blocker()`), because "something is on top" left a model unable to tell
our own window from a dialog it had not read.

### Residue

- There is still no standalone `FOCUS` op (`REMOTE.md:308`). Nothing needs one
  today: `click` raises what it must and `keys` already goes through
  `focus()`. Add it when a caller wants to raise a window WITHOUT clicking it.
- Measured on an emulator, not the P200. `REMOTE.md:664`'s
  `SPI_SETFOREGROUNDLOCKTIMEOUT` question stays open until the real machine
  agrees; if it disagrees, the fallback is already in `focus()`.

---

## 17. Gaps found testing exec on the guest

**Status:** recorded 2026-09-27 from the libexec work on the VM (probes,
`tools/probe/execkill.py`, and an end-to-end run with a Haiku agent,
`docs/measurements/e2e-vm-20260927.txt`). Each line says where it is owned.

- **No way to bring a window to the front.** *Fixed 2026-09-27, same day:*
  `click` now raises a covered window and re-checks before refusing, and the
  refusal names what is on top. Measured on Win98 SE. §16.
- **A DOS program that has to be stopped still leaks one REDIR32** (~150KB
  until reboot), and its stop costs the client's worker ~10s (the 8s grace,
  then the VM kill): a 3s timeout on `choice` answered after 13s. A Win32
  program under the shell stops cleanly in under 4s. §14.
- **Exit codes through COMMAND.COM are always 0.** The agent is now told so
  (the `command.com` profile note); the wire still carries `code=0`, and
  libexec's `ExecResult::viaShell` is not yet sent anywhere.
- **An interactive DOS prompt hangs to the timeout.** COMMAND.COM reads
  "Are you sure (Y/N)?" from CON, not stdin, so no input arrangement answers
  it (measured before libexec; the exec tool description says so). Unfixed.
- **A client that reports no shells runs every command bare, silently.**
  Seen when the client ran from a folder without `tools_detect.ini`: HELLO
  listed no shells, the bridge logged only a PATH-separator warning, and the
  agent's `ver` came back `spawn_failed` with nothing saying why. The bridge
  should tell the agent (and log) that no shell was reported; the client
  should log a missing `tools_detect.ini`.
- **No process tools for the agent.** *Fixed 2026-09-27:* machine tools
  `ps` and `kill` (PSLIST/PSKILL on the client's reader thread), verified
  end to end on the VM -- a live Notepad ended, two stuck orphans reported
  `pending` honestly (`docs/measurements/ps-kill-vm-20260927.txt`). Told
  "find it with ps", Haiku first ran `exec ps`; exec's description now
  names the tools. System info and reboot still have no tool (by choice).
- **Killing a console program strands its console VM** (below), and a
  guest carrying stranded VMs later **blue-screened in VMM** releasing a
  kernel object through a stale pointer while running `start notepad`
  (`docs/measurements/bsod-vm-20260927.txt`, one sighting; the same
  command on a clean box returned in 2s). *Decided and done 2026-09-27:*
  `kill` refuses a process with a DOS VM below it (a console program's
  console, or a DOS session) unless `force` -- status `dos_session`, its
  own sentence for the agent. Verified on the VM (execcheck 24/24).
- **No exec check runs on the guest.** *Fixed 2026-09-27:* `execcheck` now
  runs its own exe in child modes, directly or through COMSPEC, and has a
  `--detached-to FILE` mode for the guest (a console parent would hide
  REDIR32). First guest run on a clean box: 22/23, the grandchild stop with
  `leaked=0` and no REDIR32 afterwards; the one failure was the test's own.
- **Killing a console program on 98 leaves its console's DOS VM
  half-dead.** Found by that run: a Win32 console program's console is a
  WINOA386.MOD listed as its child, and TerminateProcess on the program
  strands it (1 thread, like REDIR32), degrading later runs. Matters for
  the KILL tool: kill the program's Win32 descendants and let a console
  program end, or say that the VM stays.
- **Stop read as a failure.** *Fixed 2026-09-27:* the bridge ends a stopped
  turn as `stopped` and the client shows "Stopped." instead of "did not
  complete... may not have run at all".
- **One guest wedge, cause unknown**, measured in ring-0 VMM code; the
  Toolhelp trigger first suspected was withdrawn. §14's open risk,
  `docs/measurements/wedge-vm-20260927.txt`.

---

## 18. A refused permission-mode change leaves the client showing the wrong mode

**Status:** found 2026-09-27 while setting up the BACKLOG 16 end-to-end check.
First two asks fixed 2026-10-01 in `bridge/services/options.py` `set_mode`.
The "related" item below is still open.

Picking **Auto** in the client's Mode menu with Haiku selected fails in the
bridge:

    ERROR bridge.options: permission mode change failed
    ...
    Exception: Cannot set permission mode to auto: auto mode unavailable
               for this model

`bridge/agent/session.py` `set_permission_mode` raises, `bridge/options.py`
logs it with a traceback, **and nothing reaches the guest**. The client goes on
showing `Auto` in the toolbar with a tick beside it in the menu, while the
session is still in `Manual` and asks for every single tool call.

So the machine says one thing and does another, and the only place the truth
is written is a host-side log the person at the Win98 box is not reading. I
spent a while assuming Auto was applied and that the permission bar appearing
was a second bug.

What it needed:

- the refusal sent to the client, so the menu snaps back to the mode that is
  actually in force and the transcript says why ("Haiku cannot run in
  Auto") -- **done**: `set_mode` now remembers the mode in force before the
  attempt, and on refusal puts `guest.perm_mode` back to it and resends the
  options menu, so the tick in the client lands on the mode that is really
  running. The transcript gets an `H.error` block naming the mode and the
  CLI's own reason.
- the bridge log downgraded from a traceback to one line, because this is a
  refusal, not a crash -- **done**: `log.exception` (ERROR, with traceback)
  became `log.warning` naming the model and the mode.
- ideally the menu not offering a mode the current model cannot enter, since
  the model is chosen right next to it in the same toolbar -- **not done,
  on purpose**: unlike effort (`supportedEffortLevels`, read straight off
  the CLI's model list), there is no SDK-reported list of which permission
  modes a given model accepts. Filtering the Mode menu up front would mean
  hardcoding a per-model table and keeping it current by hand, which is the
  exact shape of staleness `send_options`'s effort handling was written to
  avoid. Accepting the refusal and resyncing is the honest fix available
  without that table; revisit this item if the CLI ever exposes
  `supportedPermissionModes` (or similar) the way it does for effort.

Related: the mode and the model are separate dropdowns whose valid
combinations are not independent, and nothing in the client knows that.
Still open -- see the "not done" item just above.

---

## 19. UI automation should be opt-in, and say when it is driving -- CLOSED 2026-10-01

**2026-10-04:** the `automation` cap stays the hard ceiling (the client's
"Let it see and use this screen" box on Options > Permissions), and execution
modes add a policy on top: "Look at the screen" and "Click and type" are two
rows of every mode, Off leaving the tools out of the session entirely.

**Asked for by the user 2026-09-27 after §16 landed. Closed 2026-10-01**, with
two parts deliberately not done and recorded below rather than dropped.

Screen control is not like the other tools. It moves the mouse, types, and
since §16 it also **takes the foreground away from whoever is at the machine**
-- 9x offers no way to raise a window without activating it
(`docs/measurements/focus-vm-20260927.txt`). A capability like that should be
chosen, and visible while it acts.

### The standalone FOCUS op -- done 2026-09-27

`UIFOCUS` is in the lane table and handled beside the other four ops, and the
bridge exposes it as a fifth tool, `focus`.

A fifth tool rather than a parameter, and the exec test is why it passes:
`exec` cannot raise a window at all, and neither can any other tool without a
side effect -- `click` raises the window by PRESSING something in it, which is
exactly what a caller who wants to photograph a window does not want. A
`focus=` parameter on `shot` would have covered that one case and left "put
this in front of the person" unreachable.

Its description says it activates the target and takes the keyboard, because
on 9x there is no quiet way to raise a window across a process boundary.

### The capability, the setting and the gate -- done 2026-10-01

**Why, and it is not the token count.** Declaring five tools a client cannot
use is wrong whatever it costs. This entry used to say gating them "costs
nothing in context", which was false only in the sense that the capability did
not exist yet -- there was no way to know, so the tools went to everyone.

**What it does cost, stated precisely, because the first version of this
paragraph got it wrong.** Measured 2026-10-01 with
`ClaudeSDKClient.get_context_usage()` against haiku's 200K window: `ui`,
`click`, `keys`, `shot` and `focus` are **1,576 tokens** -- 46% of everything
the `machine` server contributes and 7.7% of the 20,360 a fresh session starts
with. Those schemas sit in the request's `tools` array, so they occupy context
window space **once for the session** (about 0.8% of a 200K window), and
prompt caching means they are billed once per cache write. It is **not** re-paid
per request, and nothing in the code or the commits says it is. Corroborated
offline the same day by serialising what `build()` hands the SDK: 6,054 of
14,735 characters of tool definition, 41.1% of the server, which at the ~3.8
characters per token that mixed JSON and prose runs to is the same 1,576.

**The header.** `automation`, HELLO header 29 of 32, a small integer enum with
room to grow (`win/net/caps.h`, `bridge/files/caps.py`):

- `RC_AUTOMATION_NONE` / `AUTOMATION_NONE` = 0 -- no screen; the five tools do
  not exist for this session.
- `RC_AUTOMATION_SCREEN` / `AUTOMATION_SCREEN` = 1 -- read the screen, click,
  type, photograph, raise a window.

An enum and not a flag for the reason `caps.py` gives for all of them: a client
that can photograph a screen but not type into it, or enumerate windows without
being able to capture them, is a third and a fourth value here and no new
header. A value the bridge does not recognise is **refused**, not masked to 0
or 1 -- both guesses are wrong for an unknown value, since NONE throws away a
capability the client says it has and SCREEN offers five tools shaped for a
screen it may not have.

It is also the second HELLO value that is not a compile-time literal (the
codepage is the first): it comes from the INI, through
`rc_net_set_automation`, which is called before the connection service exists
so the first HELLO carries it.

**The guest's setting, and the default is ON.** `[ui] automation` in
`gaston.ini`, through `ClientConfig` (`win/app/config.{h,cpp}`), `getBool`, so
`automation=maybe` **refuses startup naming the key** like every other invalid
value. The default for a missing key is **1**, and the argument is the upgrade
path: the five ops have shipped and been used on real hardware since §16, so
defaulting off would silently remove five tools from every install that
updates, and the symptom is an agent reporting it cannot see the screen with
no error anywhere to explain it. The INI is written with its defaults on first
run, so a fresh install gets a visible `automation=1` line to flip -- which is
what "chosen" means in a file edited with EDIT.COM. §19's argument for OFF was
"stops screen control arriving in an upgrade"; screen control arrived in §16
and is already there, so OFF would be *removing* it in an upgrade, which is
the change nobody asked for.

**The bridge's default for an absent header is SCREEN, and it is the one
default in `caps.py` that is not the restrictive value.** That rule exists for
guesses nothing downstream can detect -- `devices=0` on a DOS box wedges the
RPC worker, a wrong codepage mangles a filename and round-trips forever. This
one is detected and refused by the client itself, the only process that can
move somebody's mouse, so the permissive guess costs one round trip with a
sentence attached. What is left is `caps.py`'s other rule -- an absent field
means the deployed client -- and the deployed client drives a screen. It also
means the bridge half is safe to deploy ahead of the client half: a pre-§19
guest keeps all five tools and behaves exactly as before.

**The bridge gate.** `bridge/agent/tools.py`'s `build()`, and **nothing had to
be threaded in**: `guest` was already a parameter, and `guest.caps` is set at
attach (`bridge/handshake.py` -> `ConnectionService._attach`) before any
session is built. The five closures are still defined either way -- defining
one costs nothing, it is the schema reaching the model that costs -- and
`tools=[...]` splices in a `screen` list that is empty for a client declaring
NONE. A guest of `None` is a harness, not a computer, and gets no screen.

**The guest gates too, and that is the control.** `rc_uirpc_handle` refuses
every one of the five ops with `automation_off` when `net->automation` is NONE,
whatever the bridge believes, and reads the field live rather than capturing it
-- so turning the capability OFF takes effect on the next request instead of
the next connection, which is the asymmetry this entry asked for. The handshake
header is the optimisation.

**And the bridge refuses a stale conversation without a round trip.** Not
registering the tools only shapes a session at the moment it is built, and a
`GuestSession` outlives a reconnect -- so a conversation opened while the
setting was on still holds the five after the client comes back declaring none
of them. `_ui_ready()` reads the live declaration, so that call is answered on
the bridge rather than waking the machine's desktop worker to be told the same
thing. Neither side relies on the other for this.

**The system prompt needed no change, and that is worth a test rather than a
diff.** It already said "Only the tools actually offered to you exist: do not
assume you can see its screen or list its windows unless a tool for that is
present" -- written before the capability existed and honest either way. A
system prompt is one text for every client and is built before the tool list,
so a claim about a specific capability does not belong in it;
`test_the_prompt_is_honest_whether_or_not_the_screen_tools_exist` pins that no
sentence names one of the five tools or states the capability flatly. **No
prompt goldens were regenerated**, and `MAX_BLOCK_CHARS` did not move.

**`fakegui` declares `automation=1`.** It stands in for the Win9x guest and is
the only client the server suites run against, so declaring none of it would
mean the bridge builds every tested session without the five tools and every
shipped one with them. It does not implement the five ops, exactly as it does
not implement a mirrored tree:
`test_fakegui_hello_headers_match_guest_net_cpp` is about declaring the
capability, and answering the ops is `win/ui/uirpc.cpp`'s job.

**Tests.** `tests/python/files/test_caps.py` (round trip, unknown value
refused, the non-restrictive default made deliberate, `POSIX` has no screen,
the summary says which); `tests/python/agent/test_tools_surface.py` (all five
present when declared, none when not, exactly five and nothing else moves, a
client that never said keeps them, no new parameter on `build`, the stale
conversation refused); `tests/python/meta/test_hello_headers.py` (declared at
the handshake; the budget checks already cover 29 of 32 with 3 free);
`tests/python/agent/test_machine.py` (the prompt stays honest);
`tests/python/guest/test_guest_config.py` + `win/checks/inicheck.cpp` (the
setting round-trips, defaults on, and `automation=maybe` refuses startup).

`test_agent_tools.py`'s `_registered_names` had to learn that `tools=[...]` is
no longer one literal; it pattern-matched a single `ast.List` and reported
EVERY tool as unregistered the moment the screen list was spliced in, which is
the failure mode of a source-level check written against one spelling.

### Two things did not land

**1. The indicator, which is still an open decision.** `win/app/statusbar.h`'s
fourth pane with `set_override`, or `win/app/bar.h`'s loud transient bar, or
both. The argument for the bar is that during a click or a keystroke sequence
the agent is competing with the person for the keyboard and has just stolen the
foreground; the argument against a quiet pane is the foreground flap below. Not
taken here because it is a taste decision the user asked to keep.

Turning the setting on or off from a MENU is part of the same piece of work and
is also not done: the INI is read once at startup, so today a change needs a
restart. `net->automation` is read live by the refusal path specifically so a
menu item can make "off" immediate when somebody builds one.

**2. `gaston.exe` cannot hold the guest half at the current cap.** Measured
with `make -C win check-gaston`:

```
pristine a3c30e3      467,737   (2,263 spare of 470,000)
with the guest half   471,833   OVER BY 1,833
```

**The feature's own static cost is 404 bytes** -- `_TEXT` +272, `CONST` +132,
`_BSS` and `_DATA` unchanged to the byte. No new container, no `std::string`,
no new static buffer; the value is an `int` in `rc_net` and reaches the wire as
a compile-time literal through `RC_STR`, so there is no formatting code at all.

The other **4,096 bytes are one page of DGROUP alignment padding**, and they
are an accident of where HEAD happens to sit. From the linker map:

```
                        pristine        with the change
CONST                   0x8e8e          0x8f12      (+132)
data chain ends at      0x469ffe        0x46a07e
_BSS starts at          0x46a000        0x46b000    (+4096)
_BSS size               0x32e8          0x32e8      (unchanged)
```

`_BSS` is page-aligned, and at a3c30e3 the data chain ends **2 bytes** short of
`0x46a000`. The Makefile's figure is `sum(max(virtual, raw))` per PE section,
so DGROUP's virtual size carries that padding and the gate charges for it. Any
static data growth at all -- 3 bytes, let alone the 11 the HELLO key costs --
buys the whole page. There is no cheaper honest version: the wire shape is
fixed by CLAUDE.md (one header, a small integer enum, no bit packing), and the
unavoidable part alone is past the 2 bytes available.

The page is paid once. Past it there are 3,970 bytes of data growth already
bought. So this is a real decision with three answers, none of them taken here:

- raise `COMMITTED_CAP` by 4,096, which buys this and the next 3,970 bytes of
  static data in one step (the user deferred raising it, so this is theirs);
- free **126 bytes** of CONST or DATA anywhere in `gaston.exe`, which puts
  `_BSS` back on `0x46a000` and makes this feature's committed cost ~404 bytes;
- ship the bridge half alone. It is safe -- the absent-header default is
  SCREEN, so nothing changes for the current client -- but then no client ever
  declares NONE, the 1,576 tokens are never saved, and the capability exists on
  paper only.

### Known and accepted: manual approval makes the foreground flap

Observed in real use 2026-09-27, and accepted for now rather than treated as a
defect. With the permission mode on Manual, every approved screen op moves the
foreground twice:

1. the bar appears on the client, and pressing **Allow** raises the CLIENT,
   which is a click on the client's window like any other;
2. the op then runs and raises the TARGET, because since §16 that is what
   `click` does when the control is covered -- and after step 1 it usually is.

So the window under the pointer changes twice per approval, and anything the
person types in between lands wherever the foreground happens to be. It is
awkward rather than wrong, and it is worse for a sequence of ops than for one.

It bears on the indicator decision above rather than standing alone: a quiet
status pane is harder to justify when the screen is visibly flapping, and it
is an argument for the feature being off unless someone has chosen it -- which
is now something they can choose.

### Known and accepted: two programs can drive one desktop

**Recorded 2026-10-02 with gremote's ui lane (REMOTE.md step 6), accepted by
the user rather than guarded.** `gaston.exe` and `gremote.exe` hold different
single-instance mutexes (`GastonSingleInstance`, `GastonRemoteSingleInstance`),
so both run; and since gremote now answers `TREE`, `SHOT` and `RUN`, both can
drive the same screen at the same time.

Nothing stops them. `Input::isOurs` compares the target's **process** against
our own (`win/automation/input.cpp`) -- deliberately, because a child control
is a different HWND on the same thread -- so each program refuses only its own
windows. gremote will focus and type into the client's window, including its
permission bar; the client will do the same to gremote's tray window. And
`AttachThreadInput` from two foreign threads onto one target has no defined
interleaving.

**Why it is accepted.** It is a development box driven by one person who knows
which tool they are holding, and gremote can already `EXEC` anything on that
machine -- so clicking the client's own Allow button is not a capability it
gained here. The foreground flap above is the same class of problem with one
driver, and it was accepted on the same grounds.

**What would close it:** a named `GastonDesktopDriver` mutex held across any
focus change or step list, taken by both `win/ui/uiservice.cpp` and
`win/remote/uiops.cpp`, with a timeout and an abandoned-mutex path. Not built,
because it is new shared state between two programs bought against a collision
nobody has had.

**One nearby hazard that did get code.** `TrackPopupMenu` on gremote's tray
icon runs a modal loop that captures the input queue, so a step list running
while somebody opens that menu loses its keystrokes to the menu and
`Input::focus` reports `not_foreground` for a reason nothing would otherwise
log. The tray owns the loop, so it says so, and the ui lane refuses while the
menu is up.

## 20. `DRIVES` spins the floppy motor -- CLOSED 2026-10-01

**Found 2026-10-01**, on the first real-hardware run, by the person sitting
next to the machine: they heard it. The VM has a floppy image and no motor,
so nothing about this was visible on QEMU.

`enumerateDrives` (`win/fs/drives.cpp`) walked the drive letters and called
`GetVolumeInformationA`, then `freeBytes64`/`totalBytes64`
(`GetDiskFreeSpaceExA`/`GetDiskFreeSpaceA`), on every one -- unconditionally,
including a removable or a CD-ROM. Against a real `A:` that is what seeks:
`GetDriveTypeA` and `GetLogicalDrives` touch nothing, but the volume and
free-space calls reach the medium. It also cost: a `DRIVES` call on the XP
2000+ took **4.53 seconds** where every other op on that machine was
milliseconds, and the time was all in the floppy.

`SEM_FAILCRITICALERRORS` did NOT help here and was never meant to. It was
already set in `enumerateDrives` (the `QuietErrors` guard), and it
suppresses the "There is no disk in drive A:" dialog -- a real risk on an
unattended box, since that dialog is modal and nobody is there to dismiss
it. It never stopped the access that provokes it; closing this needed both,
and the dialog guard was already correct and needed no change.

**Why it was a defect and not a quirk.** `DRIVES` is a read-only listing. A
caller asking what letters exist had not asked to touch any of them, and a
listing with a physical side effect is one an agent trips over without
knowing -- an `ls` that wears out hardware. It also made the op's cost
unpredictable in a way nothing in the reply explained.

**What closed it.** `enumerateDrives` took a `probe` parameter (default
`false`). The letter and `GetDriveTypeA`'s type are read unconditionally, as
before. `GetVolumeInformationA` and the free/total space calls now run
unconditionally only for a drive that cannot seek (fixed, network, RAM
disk); for `DRIVE_REMOVABLE` and `DRIVE_CDROM` they run only when `probe` is
true. `DriveInfo.hasMedia` (a bool) became `DriveInfo.media`, a tri-state
`DriveMedia` (`MEDIA_UNKNOWN` / `MEDIA_ABSENT` / `MEDIA_PRESENT`) -- the third
state the note below asked for, so an unprobed removable drive is reported
as "nobody looked" rather than guessed empty.

Both callers of `enumerateDrives` were fixed, since both can reach a real
floppy:

- **gremote's `DRIVES`** (`win/remote/fsops.cpp`) reads an optional `probe`
  header on the request (`"1"` to probe, absent/anything else is quiet) and
  passes it through. The wire line's `media` field is now `"1"` / `"0"` /
  `"?"` instead of `0`/`1`. `gremote/ops.py` and `gremote/client.py` were
  updated to match: `Drive.has_media` is `bool | None`, `None` for `"?"`,
  and `Remote.drives(probe=True)` asks for the old behaviour. Covered by
  `tests/python/remote/test_fs.py::test_drives_does_not_probe_removable_media_unless_asked`
  (proves the code path -- quiet by default, same letters either way -- not
  the silence; Wine has no floppy motor to prove that on) and by
  `win/checks/fscheck.cpp`'s `caseDrives` (`drives.quiet_leaves_removable_unknown`,
  `drives.probe_asks_what_quiet_skipped`), which also only prove the
  branch is taken.
- **the agent's `drives` tool** (`bridge/agent/tools.py` ->
  `bridge/services/guest_fs.py` -> `win/rpc/fsrpc.cpp`'s `FSDRIVES`, used by
  `gaston.exe`, not gremote) is the path the person actually heard: `FSDRIVES`
  gained the same `probe` header and quiet default, `FSDRIVE`'s `media`
  header gained the third spelling `"unknown"`, and the `drives` tool gained
  an optional `probe` boolean argument (documented in its description) so
  the one caller that genuinely wants to know what is in `A:` still can.
  `bridge/services/guest_fs.py`'s `Drive.has_media` is now `bool | None`;
  the tool prints "not checked" for `None` rather than conflating it with
  "empty". Covered by `tests/python/agent/test_fs_tools.py`
  (`test_drives_says_not_checked_when_the_guest_never_probed`,
  `test_drives_forwards_probe_to_the_guest`).

**Measured:** `gremote.exe` committed-at-load went from 240713 to 242761
bytes (budget 250000) -- no new translation unit, all of it inside
`fsops.cpp`/`drives.cpp`/`drives.h`.

**What only the real machine confirms, and Wine cannot:** that a `DRIVES`
call against the floppy with `probe` unset makes no sound and takes no
noticeable time, the way the motor spin that found this bug did. Wine proves
the branch runs; it has no motor to prove silent.

## 21. `mine` was per-bridge, not per-guest-machine -- CLOSED

**[R] CLOSED 2026-10-01**, by decision, the same day it was found. The
client mints a random GUID on first run, keeps it beside the exe and
reports it in `HELLO`; the bridge files it beside `mine` and narrows the
chat list by it. What was built, and what was decided, is at the bottom
of this section. The problem, as found:

**Found 2026-10-01**, fixing the chat list losing conversations. That bug was
filtering after limiting -- `listing()` fetched the newest 50 sessions on the
whole host and `send_sessions` filtered them down to ours afterwards, so work
in any other checkout evicted the guest's chats (measured: 50 fetched, 33 of
ours in them, 54 of ours missing). Fetching wide and capping after the filter
closes that. It does not close what the filter is filtering ON.

`bridge/agent/conversations.py` opens by arguing the case for the filter, and
the argument is about a MACHINE:

> **whether it is ours.** The chat list used to show everything
> `list_sessions()` returned, which on a working machine is mostly
> conversations from a desktop with a different set of tools, a different
> screen and a different idea of what a path looks like. Continuing one of
> those *from Windows 98* rarely makes sense. There is no settable tag in
> ClaudeAgentOptions, so the bridge remembers the ids it started.

"A different set of tools, a different screen, a different idea of what a
path looks like" is exactly as true of a SECOND GUEST as it is of the desktop.
But `mine` is written to `~/.gaston-code/conversations.json` on the bridge
host and says only that *this bridge* started the conversation. Point one
bridge at two machines -- the QEMU Win98 box and the real XP 2000+, or a
Win98 guest and an NT 4 one -- and each sees the other's conversations in its
list, with the other's drive letters, the other's compiler and the other's
`sync_root` in the folder paths. Resuming one of those from the wrong machine
is the same mistake the filter exists to prevent, one level down.

It has not bitten yet because there has been one guest at a time. It is not
hypothetical: §7 above already wants the sync baseline bound to a machine
rather than to a bridge, and §1b puts a second physical machine in the loop
deliberately.

**What was built.** A machine identity on the wire, in three pieces.

*The guest owns it.* `win/app/machineid.{h,cpp}`: `CoCreateGuid` once on
first run, written as one line of ASCII to `machine.id` beside the exe, read
back on every launch after that. `MachineId` is a plain member of
`ClientApp`, loaded in `initializeServices()` immediately after `OleService`
-- after OLE because `CoCreateGuid` lives in OLE32, before the connection
service because that is what eventually calls `rc_net_start`.

*Where it is stored, and why not `gaston.ini`.* The open question above
offered the volume serial or a key in the INI. Neither:

* the **volume serial** is derived state, and `tree_id` already uses it, so
  the two would be the same number meaning two things -- and reformatting a
  disk would silently retire a machine's whole chat list;
* **`gaston.ini`** can be written (`IniFile::setString`/`flush`, and
  `ConfigStore::save` already does) -- but it holds the host, the port and
  the secret, and by design a value in it that is present but wrong REFUSES
  STARTUP. Putting a write-once identity in the file that can refuse the
  client's launch buys nothing and risks the configuration. gaston.ini is
  also rewritten whenever a setting changes, so the id would be rewritten
  with it, for no reason.

A separate one-line file cannot corrupt the config if it is interrupted, is
fixable with EDIT.COM, and moves to a new install by copying one file. Its
name is 8.3 by construction, so nothing has to spell out a short alias.

*It is not allowed to stop the client.* Failure to generate (`CoCreateGuid`
refuses) or to persist (read-only or full media) leaves the id EMPTY, logged,
and the client starts normally. An empty id sends no header, and the bridge
then filters by `mine` alone -- exactly what every build before this did.
Deliberately NOT a per-boot id: one minted but not written would be a
different machine every launch, so the chat list would come up empty every
morning with nothing to say why. A `machine.id` that exists but does not
hold an id is replaced rather than refused, and says so in the log, because
nobody hand-authors that file.

*The wire.* One more `HELLO` header, `machine`. HELLO sent **27** headers
and now sends **28**, against `RC_HELLO_MAX` **32** in `win/net/net.cpp` and
`MAX_HELLO_HEADERS` **32** in `bridge/wire/frame.py`: four slots left. The
other cap is a red herring worth naming, because it is the one you find
first -- `RC_MAX_HEADERS` 16 in `win/wire/frame.h` bounds what the guest
PARSES, and the guest never receives a HELLO. That arithmetic was held
nowhere, so `tests/python/meta/test_hello_headers.py` now pins it: the
header list is parsed out of the `HDR()` calls, no key is sent twice, the
list fits the guest's array, the guest's array fits the bridge's cap, and at
least two slots stay free. Running out matters because the header list IS
the capability declaration -- CLAUDE.md's "client rules are declared at
handshake" -- so a full array means the next client rule has nowhere to go.

*The bridge.* `Conversations` gains a `machine` field beside `mine` and a
`belongs()` that asks both questions; `filter()` takes the connected guest's
id, which `GuestSession.machine` reads off `HELLO`. `SessionService.add` and
`TurnRunner` file every new conversation under it.

**ABSENT MEANS ANY, on both sides.** A row written before the field has no
machine and stays visible to every guest -- 89 such rows on this developer's
box the day it landed, and a reading of "no machine" rather than "any
machine" would have emptied every chat list on the first connection after
the upgrade. A client that sends no header gets the old, wider list rather
than an empty one. One rule, not two: `add()` with no machine writes no
machine field, so an unidentified conversation looks exactly like a legacy
one. Both directions are tested
(`tests/python/agent/test_conversations.py`,
`tests/python/services/test_session.py`).

*And the guest half is run, not read.* `win/checks/idcheck.cpp` drives the
shipping `app/machineid.cpp` under Wine --
`tests/python/guest/test_guest_machineid.py` reads its verdicts -- because
every way this can go wrong is invisible from outside the process: a client
that re-mints on each launch behaves perfectly until the morning the list is
empty. It asserts the mint, the file, the SAME id on the second launch, a
different id in a different directory, trailing CRLF trimmed, garbage
replaced and the replacement persisted, and no id at all when the directory
cannot hold the file.

**It composes with the fetch/cap order and does not disturb it.**
`send_sessions` still fetches `FETCH_LIMIT` (500) rows, filters, and caps at
`DISPLAY_LIMIT` (59) LAST. The machine filter is part of the filter step,
which is the only place it can go: applied after the cap it would send a
short list while the guest's own chats sat further down the fetch, which is
the §21-adjacent bug this file opens with.

**Still open: whether this id and §7's baseline identity are the same id.**
They should be, and this does not unify them -- deliberately. §7's `tree_id`
is derived from the volume serial and is checked as an identity by
`scan_guest`, which REFUSES a pass when it changes; that refusal is the
guard against pointing one baseline at a reformatted or swapped disk, and it
is correct for exactly the reason a chat list's id must NOT work that way (a
reformat should not retire your conversations). Unifying them is therefore
not a rename. It means deciding which of the two behaviours the single id
has, and then giving the other one back by a different route -- most likely
`tree_id` becoming `machine` + a volume-change check carried as its own
field, so "which machine" and "has the disk underneath it changed" stop
being one answer. That is a change to the sync layer's identity contract and
belongs with §7's mirror decision, not ahead of it. Nothing in what shipped
here forecloses it: the guest already has one persistent id and `HELLO`
already carries it, so §7 can adopt it when it is settled rather than mint a
second one.

---

## 22. Self-update rolls back from a load failure, not from a silent one

**Status:** built to here by decision, 2026-10-01. `gremote.exe` can now
replace itself (`UPDATE` + `win/remote/gupdate.cpp` + `gremote/ship.py`'s
`self_update`), and the automatic rollback covers one of the two ways a new
build can fail to serve.

**What is covered, and why it was worth building.** `gupdate` keeps the
previous image as `GREMOTE.OLD` -- the cheap, reliable half: a person always
has one rename between them and the build that worked. On top of that it
holds the `PROCESS_INFORMATION` handle of the image it just started and
waits `-verify` milliseconds on it. If the process is gone by then it puts
`GREMOTE.OLD` back and starts that instead. That is not a guess: on this
platform a single NT-only static import kills an image at LOAD, before
`WinMain`, with no log written -- the failure mode `CLAUDE.md` opens with --
and from the parent's side it is simply a handle that signals immediately.
The same catch fires for a build that refuses its own config. Proven under
Wine by shipping `capscheck.exe` at the server:
`tests/python/remote/test_update.py::test_a_replacement_that_does_not_stay_up_is_rolled_back`.

**What is NOT covered.** A replacement that loads, stays up, and never
answers. A build that starts and then fails to bind -- the port is in
`TIME_WAIT` from the connection the update itself just dropped, say, and the
bind retry gives up after four minutes -- or one that wedges before its
accept loop, is indistinguishable from a healthy one through a process
handle. `gupdate` would report success and the machine would be unreachable
until somebody walks over and renames `GREMOTE.OLD` back.

**To close it**, `gupdate` needs to probe the port rather than the process:

- read `gremote.ini` from beside the destination for `bind`/`port` (it
  already links `libcore`, so `IniFile` is free);
- link `wsock32` and connect to `127.0.0.1:<port>` inside the verify
  window, retrying while the socket is refused. A TCP connect is enough --
  it does not need the secret, and asking for a handshake would mean
  teaching the updater the protocol;
- treat "never accepted a connection inside the window" the same as "the
  process exited", and roll back.

The cost is real but small: Winsock startup, an INI read, and about 10KB of
image on a program currently measuring 50,688 committed at load against a
64,000 cap (`win/Makefile`, `check-gupdate`). It was left out because the
caller on the other end of the wire is ALREADY doing exactly that probe --
`self_update` reconnects and reports what answered -- so the gap is "the
guest cannot self-heal from a silent failure", not "nobody finds out". On a
machine in another room that distinction is the whole difference, which is
why this is written down rather than closed.

**Two smaller things deferred with it.**

- **`gupdate.exe` cannot update itself.** It runs from where it is
  installed, so it holds its own image. Replacing it needs a copy of it in
  `%TEMP%` to do the swap -- the same trick, one level up. Nothing needs it
  yet: `UPDATE` refuses when the updater is missing, so a stale updater is a
  loud failure rather than a quiet one.
- **The verify window is fixed per call, not adaptive.** `-verify 4000` is
  generous for Wine and for a P200 with a warm cache, and has never been
  measured against a cold start off a 1998 platter with the disk busy. If
  the real machine ever rolls back a build that was merely slow, the number
  is the first thing to look at -- and `GUPDATE.LOG` records how long the
  image actually survived, precisely so that question has an answer.

## 23. The machine hung on shutdown -- fixed and confirmed; three choices stand

**Status:** the hang is fixed, 2026-10-01, and CONFIRMED ON THE REAL MACHINE,
2026-10-02 -- see item 1, which this entry no longer lists as open. What is
left below is what the fix deliberately does *not* do, all three of them
decisions rather than gaps.

**What happened.** The physical Pentium 200 hung on the way down with
`gremote` running: no desktop, no reboot, two Ctrl+Alt+Dels to force it. The
reboot had been asked for over `gremote`'s own connection.

`App::onMessage` joined its services from inside the `WM_ENDSESSION` handler,
and Windows does not continue a 9x shutdown until that window procedure
returns. So every join budget was shutdown held up -- and in this case a
deadlock too: the handler waited on the accept thread (8 s), the accept thread
on `Connection::join` for the reader (3 s), and the reader was inside
`ExitWindowsEx`, which does not return and whose broadcast was `SendMessage`ing
the window whose handler was waiting. It unwedged only because every wait in
that ring happened to have a timeout. With Explorer already gone there is no
desktop for the "not responding" dialog, so what a person sees is a machine
that has stopped. The client was worse: `ConnectionService::join` hands the
job to `rc_net_stop` and ignores the App's budget, which is about 42 seconds.

The handler now signals every service, abandons each *unjoined*, writes and
closes the log, and returns; `runApp` leaks the App because those threads hold
pointers into its members. `docs/REMOTE.md` has the rule and the P200 entry.

**1. ~~Wine cannot confirm it~~ -- CLOSED 2026-10-02 on the Athlon XP.** Wine
still cannot: there is no 9x shutdown under it, no Win16Mutex, and no shell to
produce the End Task dialog, so `appcheck` proves only the necessary condition.
The real machine has now answered, twice and in both directions
(`docs/measurements/shutdown-real-hw-20261002.txt`):

  * a shutdown the USER asked for -- the client's handler returned in **12 ms**
    and gremote's in **9 ms**, with no End Task dialog;
  * a shutdown THIS PROCESS asked for, which is the case the fix was actually
    written for -- `REBOOT mode=reboot` over the wire, handler back in **6 ms**
    logging "this process stays up to drive the shutdown it asked for", and the
    box answering on its port again 91 seconds later.

The second one also closed two other things in passing: REBOOT over the wire on
real hardware, and autostart after a programmatic restart rather than a power
cut.

**2. Nothing is flushed on the way out beyond the log, and that is a choice.**
The session-ending path drops whatever the client's RPC worker was about to
answer and whatever `gremote`'s lanes had queued; the peer sees a dropped link.
Draining first was considered and rejected: a drain is a wait, a wait is the
bug, and the bridge already treats a lost link as a lost link. It costs nothing
a person typed -- the client saves settings when they change, not at exit --
but a future op whose reply matters more than the shutdown's promptness would
have to earn its wait, and say so here.

**3. `WM_QUERYENDSESSION` still does nothing but agree.** The tempting
optimisation is to start signalling there, since the gap before `WM_ENDSESSION`
is free time in which threads could actually wind down cleanly. It is not safe
as things stand: another program can veto the shutdown, `WM_ENDSESSION` then
arrives with `wParam` 0, and there is no un-signalling a service -- the result
would be a live process with a dead server on a machine that is staying up.
Closing this needs services that can be *un*-signalled, or a `reinit` the
lifecycle does not have, which is a bigger change than the hang warranted.

**4. `ConnectionService::signalStop()` does not exist, so the client's fast
path sends no FIN.** The `Service` contract is two passes -- `signalStop()`
never blocks, `join()` waits -- but `ConnectionService` declares only `join()`
and hands the whole thing to `rc_net_stop`, which signals, drains and joins in
one call. On the ordinary path that is merely unusual; on the session-ending
path it means the connection is abandoned with *nothing* having been signalled,
so the bridge learns the link is gone when the process dies rather than from a
FIN. Harmless -- the bridge treats a dropped link as a dropped link -- and not
fixed here on purpose: splitting `rc_net_stop` into a signal half and a join
half means reordering the three phases whose order `net.cpp` documents at
length as the thing that was wrong before (clearing `running` first silently
threw away every ack the RPC worker's drain composed). Worth doing when
somebody is in that file anyway, with `wirecheck` watching. `gremote` does not
have the problem: `ServerService::signalStop` is a flag and nothing else.

**Not a gap, recorded so it is not re-derived:** `shutdown(sock, SD_SEND)` does
not wake a blocking `recv` on the local socket -- it half-closes the *sending*
direction, and the reader returns only when the peer reciprocates, or after
`heartbeat_timeout_s` (default 30) in the `select()` in front of every `recv`.
`Connection::signalStop`'s own comment says as much. So `Connection::join`'s
3 s budget times out and leaks whenever the peer does not close, which during
a machine shutdown is the ordinary case. That is correct behaviour for the leak
rule; it is only a problem when somebody waits for it, which is what §23 was
about.

## 24. The log rotation is consumed before the single-instance check

**CLOSED 2026-10-04**, the lazy shape below: the client opens its log
appending at the first statement (`rc_log_open_early`) and rotates `.log` to
`.prv` only after the single-instance check passes (`rc_log_begin_run`).
Verified on the VM: a bounced second copy left GASTON.PRV byte-identical.

`rc_log_open` renames `<program>.log` to `<program>.prv` as its first act, and
`App::initialize()` reaches the single-instance mutex check only afterwards. So a
second copy that bounces off the mutex has **already spent the rotation**: it
writes its own handful of lines, the previous run's log is now `.prv`, and the
run somebody actually wanted to read has been pushed out of both files.

Found 2026-10-01 while measuring the shutdown fix, by losing a log to it: a
failed `mode=poweroff` left the box on the logon prompt with the mutex held, the
next launch bounced, and the evidence of the failure was gone. That is the same
class of bug `.prv` was written to fix (a93df53), arriving one step earlier.

Not fixed on purpose, because the obvious move trades one diagnostic for
another. Rotating *after* the single-instance check puts it after the point where
"no log file was created at all" is the only signal for an image that died at
load -- an NT-only static import kills the process before `WinMain` with nothing
written, and that absence is how you tell it apart from a program that started
and failed. Closing it properly needs the rotation to know whether this copy is
the one that will go on to run: either the mutex check moves ahead of logging
(and then logs nothing about why it exited), or the rotation becomes lazy and
happens on the first line written after the instance check passes, which is the
better shape and is a change to `log.cpp`'s contract rather than a tweak.

Until then: on a box where a second copy may have bounced, `.prv` is not
necessarily the run you want, and `gupdate.log` is the independent record of what
happened to the binary.

## 25. Detection refuses to find a toolchain newer than its note

`win/tools_detect.ini`'s `[jdk]` section deliberately lists only 1.1-era install
paths, and `bridge/profiles/msvc.toml` deliberately declines a
`[detect.version].default`, for the same reason in both cases: a note is prose
about one release, and stating 1.1's absences in front of a JDK 1.3 -- or
stamping "6.0" on a 16-bit `C:\MSVC` -- is confidently wrong in the direction
that wastes the most time. Refusing to detect it at all was the only defence
available.

That defence is now unnecessary. `for_version` and `[[note.variant]]` landed
2026-10-01 (af59ebb, 10409d8): `toolnote` resolves the version in play to the
nearest release anybody has measured, and an inexact match names both numbers
above the body and says which way it is wrong. A JDK 1.3 can now be detected and
described honestly as "measured on 1.1, and 1.3 came after it".

What closing this takes:

- `win/tools_detect.ini` gains the 1.2/1.3/1.4-era `[jdk]` markers it currently
  omits, and whatever paths Sun's installers of that era actually wrote. Guest
  side, so it needs a reflash.
- `bridge/profiles/jdk.toml` gains the matching `[detect.version]` rows, and the
  comment that currently justifies listing only 1.1 layouts is rewritten -- it is
  load-bearing for the `default = "1.1"` and stops being true.
- `bridge/profiles/msvc.toml` can then map `DevStudio\VC` to "5.0" and `MSDEV` to
  "4.0" instead of leaving them unversioned, because a 5.0 install asking a 6.0
  note now produces a labelled answer rather than a silent one.

One case stays open even then, and it is worth stating: an install detected with
**no version at all** cannot be told it mismatches, because there is nothing to
compare. Only giving those markers versions fixes that, which is this item.

## 26. A cancelled `ui` request still gets done

Noticed in a live session, 2026-10-01, as one DEBUG line:

    bridge.ui: ui frame UIEND for u3, which nobody awaits

The user interrupted a `ui` call mid-flight. The bridge dropped the pending
entry, the guest's desktop worker finished the enumeration anyway, and the reply
arrived for a future nobody held.

**Nothing is broken.** The frame is logged and discarded, the id is already
gone, and the next call allocates a fresh one. It is recorded because of what it
costs on a slow machine rather than what it corrupts: the desktop worker is
single-threaded, so a cancelled enumeration is work the NEXT `ui` call queues
behind -- on the Athlon box a desktop tree is tens of milliseconds and on the
Pentium 200 it is not.

**What closing it would take.** There is no cancel on the ui lane. `EXECKILL`
exists for the job lane and is `RC_LANE_IMMEDIATE` so it is answered on the
reader rather than behind the work it is cancelling; a `UICANCEL` would be the
same shape, plus a generation check in `win/ui/uiservice.cpp` so a worker
part-way through an enumeration stops rather than finishing and sending.

**Why it is deferred rather than done.** Every reply the guest can send to the
ui lane is bounded and small, and the only way to observe the cost is to cancel
a `ui` call and immediately make another on a machine slow enough to notice.
That measurement has not been taken, and a cancel path built against a cost
nobody has measured is a third lane's worth of generation bookkeeping bought on
a guess. Take the measurement first.

**Partly closed, and the measurement now has an instrument (2026-10-02).**
`UICANCEL` landed with the `input` step list and stops a running list between
steps and inside any wait -- but only a list. `UITREE` and `UISHOT` still run
to completion, which is what this entry is actually about.

gremote's ui lane (REMOTE.md step 6) needed a stop for its step lists anyway,
so the bookkeeping is paid; the marginal cost of stopping a walk turned out to
be two polls on functions that already return `bool` (`NodeSink::onNode`, with
`WALK_STOPPED` already in `TreeWalker`, and `ByteSink::write`, whose own
docstring lists "the request was cancelled" as a reason). So the flag reaches
all three ops there, and **the client's `UICANCEL` now raises it for all three
too** rather than for step lists alone.

**The VM's number, 2026-10-03** (`docs/measurements/ui-cancel-vm-20261003.txt`,
`tools/probe/cancelprobe.py`): a full MSAA-enriched desktop walk is **0.16 s
at 72 nodes and 0.58-0.71 s at 109**, well under the 8 s default deadline. So
on an emulator a cancelled enumeration costs the next request well under a
second -- the cheap end this entry already guessed at. A cancelled capture is
worth more: 190,464 of a full screen's 481,078 bytes arrive before the strip
loop reads the flag and stops.

**And measuring it found a second bug, which is the better reason this was
worth doing.** With the flag wired in, eight attempts to cancel a walk lost
the race and the log said `UICANCEL ... matches no running request` every
time. `UICANCEL` is `RC_LANE_IMMEDIATE` so the reader answers it at once --
but `g_run_id` was only written when the desktop WORKER picked the frame up,
so a cancel for a request still in the queue was logged and dropped. That
window has existed since UICANCEL was built; it was invisible while only step
lists could be cancelled, because a step list is long enough that the cancel
almost always arrives after the worker has started. A 0.16 s walk is not.
`rc_uirpc_cancel` now remembers an id it could not stop and the worker starts
that request already stopped: cancelled on attempt 1, in 0.00 s.

**The Athlon's number, 2026-10-04**
(`docs/measurements/ui-lane-real-hw-20261004.txt`): a full MSAA-enriched walk
of a freshly booted desktop, hidden windows included, is **36-37 ms**, and the
request after a cancelled one is answered in 44 ms. This entry guessed "on the
Athlon box a desktop tree is tens of milliseconds and on the Pentium 200 it is
not" -- tens of milliseconds, confirmed.

So on that machine **the problem does not arise**: eight threaded attempts to
race a STOP against the walk, from the moment the id existed, caught none. The
walk finishes before the round trip asking it to stop can land. The mechanism
is built and proved (the VM caught it on attempt 1, and STOP on a step list
works on the Athlon), and what is left is the motivation -- a slower box, or
this one with a working desktop rather than sixteen windows on a fresh boot.

**This entry can be closed when the P200 says so, and not before.** It is the
machine the entry was written about.

## 27. Prevent sleep works in practice, and three of its four mechanisms are untested

**Status, 2026-10-02:** accepted by the user as working -- "from my pov, it
seems to work so far and i'm happy, and will report if it ever sleeps while
option is on". This entry exists so that does not get re-remembered later as
"fully tested", because it is not.

**What is actually established.** `SetThreadExecutionState` is PRESENT and
answers on two independent machines -- the autopatched Athlon XP and the STOCK
retail QEMU guest, which is the control that matters because the autopatcher
could have added it (`docs/measurements/sleep-mechanisms-p200-20261002.txt`).
It is called on the hidden window's message-loop thread, which lives for the
process, so the per-thread state cannot lapse. Both veto handlers answer
correctly when asked: `BROADCAST_QUERY_DENY` and `PWR_FAIL`. The nudge
(mechanism 4) is never armed, because it only arms when
`SetThreadExecutionState` fails and it does not.

**What is not.** THE VETO HAS NEVER FIRED IN ANGER. The only messages those
handlers have ever seen are ones `probe power` synthesised and sent at the
window itself. With a 1-minute system standby and Prevent sleep on, the Athlon
sat 26 minutes and gremote logged NO suspend query -- and it logs one even when
it intends to say yes, so that silence is real. By elimination the thing
holding the box awake is the nudge-free `SetThreadExecutionState`, not the
veto. So the belt-and-braces argued in `win/remote/tray.h` is, in practice, one
brace, and the backup has no evidence behind it.

There is also no baseline: nobody has watched this machine sleep. The user's
own earlier report -- it kept sleeping while they drove it over gremote, and
stopped once the option went on -- is a real before/after and the best evidence
there is, but it was not recorded under controlled conditions.

**Three ways to close it, cheapest first.**

1. **`probe suspend` -- ask for a suspend instead of waiting for one.**
   `SetSystemPowerState(TRUE, FALSE)` is documented on 95/98 and runs the real
   sequence: it asks every application and, if one denies, sends
   `PBT_APMQUERYSUSPENDFAILED` and gives up. `tray.h` already cites that page
   as the basis for mechanism 1 and NOTHING IN THIS TREE HAS EVER CALLED IT.
   With Prevent sleep on, a denial proves the veto end to end in seconds, with
   no idle wait and no settings change. With it off, the machine suspends --
   which is the missing baseline, at the cost of somebody pressing a key.

2. **QEMU with `acpi=off`.** The guest has no APM at all, which is why
   `mode=poweroff` there degrades to a logoff and why it can never raise a
   query. `tools/vm/runvm.sh` passes `-machine pc-i440fx-9.2,acpi=on`, and
   Win98 with ACPI present will not generally fall back to APM. SeaBIOS does
   implement APM, so turning ACPI off may give the guest a standby timer -- and
   with it, the veto path in CI rather than only on somebody's desk. One flag
   and one boot answers it; Device Manager says immediately.

3. **86Box or PCem.** The faithful option: a real Award or AMI BIOS with APM
   1.2, which is what QEMU lacks. Also another Win98 install, ROM images and a
   slow emulator. Worth it only if 2 fails and the veto matters enough to pay
   for.

**What would change the priority:** anything that makes
`SetThreadExecutionState` stop working -- a different KERNEL32, a machine where
the export is absent, or the call turning out to be a stub that returns a
plausible value. Today it is the entire feature and the other three mechanisms
are a hypothesis.

## 28. What the `input` work deliberately left out

**Status, 2026-10-02.** The gap survey that started the `input` step list
(PROGRESS, "One input tool") found library calls no tool reached. Most are
now reached. These were decided against, or deferred, on purpose:

- **`requestReboot` is not a tool.** A reboot ends the session that asked for
  it and the person's work with it; gremote's REBOOT is for a test box driven
  from Linux, not for an agent. If it ever is one, it needs its own bar
  wording and no Always.
- **The `UiWatch` event feed** ("what changed since") stays internal.
  `wait_for`, `wait_gone`, `wait_text` and `idle` cover what an agent asks of
  it. Low priority.
- **`text` targets in RichEdit.** RichEdit 1.0's `EM_POSFROMCHAR` takes a
  `POINTL*` in wParam -- a pointer into our process, meaningless in the
  target's. Needs the in-process reader (`win/inproc/`) or a shared-memory
  POINTL. Plain Edit is done.
- **Text that exists only as pixels** -- owner-drawn controls, canvases. Would
  be OCR on the bridge (the guest transforms nothing) followed by a `screen`
  click at the found point. Until then: `at` offsets read off a `shot`.
- **Recursive delete and folders across drives -- DONE the same day**, at
  the user's call ("go above and beyond what win98 lets us do"): win/fs/tree.h
  copies, moves and deletes whole trees, deletes go to the Recycle Bin unless
  `permanent`, nothing is overwritten unless `overwrite`, the bar shows what a
  call touches, and in Auto a reviewer model decides or asks the person. What
  is still out: trees past 5,000 entries or 200MB in one call (the caller
  splits), and a Recycle Bin on removable drives (98 has none).
- **gremote's UI lane** (REMOTE.md step 6) should be a thin adapter over
  `StepRunner` (win/automation/steps.h), exactly as `win/ui/uisteps.cpp` is
  for the client -- the runner knows nothing of either wire.
  **Taken up 2026-10-02**, and it turned out to be more than `StepRunner`:
  `uisteps.cpp`'s own 393 lines were three `rc_net` references away from
  shareable, so the step codec moved too, into `win/ui/uiengine.cpp` behind
  `UiLink`. See REMOTE.md step 6.

## 29. gremote's image grew by a third for the ui lane, and nobody has trimmed it

**Opened 2026-10-02, by decision, while raising the cap.** The user's words:
*"just raise it, we'll have a separate round of optimizations"*. This entry is
that round, written at the moment of the raise so the number is not later
mistaken for an endorsement of it.

`check-gremote`'s cap had been held tight on a stated principle --
`win/Makefile`: *"It stays tight rather than being dropped because gremote runs
BESIDE the client being measured, so its footprint is inside every number this
project has ever taken of the client on real hardware."* Linking
`libautomation` plus the shared `win/ui/` objects is the largest single
addition the program has had.

**What to look at when the round comes**, in the order the evidence points:

- `-os` versus `-otexan` on the new objects. The client's `ui/uirpc.o`,
  `ui/uiengine.o`, `automation/steps.o` and `automation/input.o` are already
  compiled for size by an override list in `win/Makefile`; gremote compiles
  the same objects through the same rule, but `win/remote/`'s own sources are
  not all on it.
- Whether gremote needs every object in `libautomation`. It links the library,
  and wlink pulls what is referenced -- but `uiengine.cpp` references nearly
  all of it, including the MSAA path on a box where `HELLO msaa=` may say 0.
- The `-xs` exception machinery, ~2,144 bytes per translation unit before any
  of its own code. The ui lane added two to gremote.
- The runtime number rather than the image. `COMMITTED_CAP`'s own comment says
  that once the gate has been hit repeatedly the working set is what to
  measure, and `workingset-vm-20261002.txt` has the method -- free physical
  memory read through gremote `SYSINFO`.

**What this entry is not.** It is not a claim that the image is too large for
the box; a 32MB guest pages code from disk and the resident cost is a fraction
of the PE sections. It is a claim that the number was taken on trust once and
should be earned back.

## 30. What the execution modes and the Options dialog left out

Recorded 2026-10-04 with the work (docs/OPTIONS-PLAN.md has the design).

- **Allowed or preferred models per mode.** Asked for while planning because
  Haiku refused the CLI's Auto; our own Auto removed that reason (it never
  asks the CLI for a mode), so this waits for a reason of its own. The likely
  shape is "preferred model for this mode", not an allow-list.
- **"Allow for this chat" for screen input.** The plan keyed a typing grant
  to the target window's process; the bridge does not know which window a
  step list reaches until the guest resolves it, and a grant over every
  window is the blanket licence the red team warned about. So `screen_drive`
  is not in `policy.GRANTABLE` until the guest reports the target.
- **The checker's SAFE cache was not built.** With the red team's exclusions
  (never for run_*, purge, files_sys, input, launch) and invalidation on any
  write, it could never hit. Latency is the warm client's job (review.py).
- **Large Fonts.** The pages are 230x170 DLU so that the sheet fits 640x480
  at Large Fonts, which was computed, not seen: the VM runs 800x600 small
  fonts, and switching needs a reboot of the shared VM.
- **The command log shows the exit-code wrapper's `.gaston-rc` line** at the
  end of a wrapped command's output: the log records output raw, and the
  bridge strips that line only from what the agent sees.
- **A Windows 95 comctl32 4.00 run.** The sheet uses the V1 structures for
  it (and found the array-stride bug that way on 98); no 95 machine was run.
- **The Tools page's Add asks four one-line questions** rather than one form,
  and has no Browse for the marker file.
