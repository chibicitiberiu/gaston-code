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

- **The replay buffer is still dead and still stamping.** The guest never sends
  `from=`, so `replay.since()` is unreachable; `append()` still fills a
  20,000-frame buffer and `_send` still stamps every frame. The bridge's own
  comment admits it (`server.py:704`). Retiring it is larger than it looks: the
  oversized-frame guard exists *because* replay is append-only, so its merit
  has to be restated rather than preserved, and the size probe stamps a fake
  `seq` into every frame's budget including the `stamp=False` ones -- after
  retirement that is a phantom tax refusing frames that would fit. Retiring
  `seq` also reverses a documented `[R]` in `PLAN.md:377`, which introduced it
  *"because adding them later touches every frame and handler on both sides"*;
  say so rather than quietly undoing it.
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
  to end: QEMU inside the namespace is unverified, and `tools/runvm.sh`
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

**Export as HTML** (04.15, `win/app/main.cpp:1706-1715`, still a
`MessageBoxA` -- closes the missing-`.rc`-infra-adjacent clause of 01.10 by
being the thing that actually needs the sink) needs the sink above (a
transcript is hundreds of KB and the guest has tens of KB free) and a real
END/status on `send_html`, which today has neither: it still returns without
sending anything when a LIVE block exceeds the frame budget
(`bridge/wire/link.py:102-103`), and the caller is told nothing either way.
An oversized APPEND is no longer dropped -- it is truncated with a marker the
reader can see (`link.py:113-127`), which is the half of this that did get
fixed.

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
`bridge/agent/registry.py` and the guest. None of this is reachable before
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
and `close()` (04.22, `server.py:2084` and `:308`); the registry's one
`~/.gaston-code/sessions.json` set (04.18, `bridge/agent/registry.py:33`)
becomes `{client_id: [session ids]}` in the same file -- not one file per
client, since the file is already written on every new session id and an
atomic replace of one small file is the right cost; `self.cwd` (04.19, read
at five sites in `server.py`) needs a per-client working directory once
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
mechanical check for this, `tools/stackframes.py`, is "not reachable from the
top-level `make check`, per 01.31/CLAUDE.md" is stale. 01.31 itself is marked
`Fixed:` in the same audit file: the top-level `Makefile:43` now runs
`$(MAKE) -C win check`, and `win/Makefile:264` runs `stackframes.py` inside
that same `check:` target (`win/Makefile:213`). Verified directly: `make
check` from the repo root exercises it today. The underlying advice --
heap-allocate Phase 6 job buffers -- stays good practice; the tooling that
would catch a violation already runs.

## 10. The Phase 4 settings dialog -- three layers, in a real order

**Status:** recorded 2026-09-18, from audits 01.34, 04.13, 04.17 and 02.31.
`IDM_HOST` is still a `MessageBoxA` telling the user to edit the INI by hand
(`win/app/main.cpp:1717-1726`); `IDM_EXPORT` is a sibling case handled in §6
above. These four findings describe the same object -- what a real
`Server Host...` dialog needs -- at three different layers, with a real
dependency order rather than a single combined cost.

**1. Resource-compile infrastructure (04.17), the true prerequisite.**
`WRC` is defined in both `make/ow.mk:13` and `win/Makefile:20` and invoked by
neither -- no `.rc` file exists anywhere in the tree, and `grep` for `$(WRC)`
across both makefiles returns nothing. Both R5's icon and a
`DialogBoxIndirectParam`-based dialog need this (closes the missing
`dialogs.c`/`.rc` clause of 01.10, which named the same gap from the
"Phase 4's dialog" angle). Linking an `.rc`-derived resource will also move
`make check`'s CMOV false-positive baseline (`CMOV_BASELINE = 1`,
`win/Makefile:208`) -- `objdump` disassembles the whole section linearly, and
a resource's binary data will decode as phantom instructions the same way
the existing false positive does. Expect to re-baseline with a comment, or a
future `make check` reads a resource-driven bump as a regression that never
was one.

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

**4. The settings-dispatch shape a dialog's new frames would multiply
(02.31).** `SETMODEL` and `SETEFFORT` are already two near-identical
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

**To close it, in order:** (1) wire `WRC` into `win/Makefile`, write the
`.rc`, re-baseline CMOV; (2) build the dialog -- `DialogBoxIndirectParam` is
cheaper than a `.rc` template given (1) is already required for the icon
(R5); (3) OK handler calls `rc_config_save` and resets the backoff state;
(4) exercise a live stream with the dialog open at least once -- for the
re-entrancy, which already happens behind the two confirmations above and has
simply never been looked at, rather than for the `RC_MAX_INFLIGHT` gate, which
a pumping modal does not reach; (5) if
R7's `client_id` is also live by then, one `rc_config_save` fix serves both.

## 11. Smaller Phase 4/5 gaps found ahead of their phases, not yet closed

**Status:** recorded 2026-09-18. Four unrelated findings, none large enough
for their own section, none blocking anything in flight.

**04.16 -- the permission rule table and the Phase 6 prompt-bar assumption
share one gap.** `PROGRESS.md`'s 2026-09-17 entry ("the auto-allow policy was
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

**04.23 -- no toolbar exists for Phase 5's attach control.**
`win/app/main.cpp` builds a hand-written menu bar and individual buttons, no
comctl32 toolbar (confirmed, ~`main.cpp:365-383`, `CreateMenu`/`AppendMenuA`
only). Adding one plus R5's icon bitmaps is the first real GDI spend the
project makes against the fixed 64KB USER heap -- the same constraint that
already forced the chat list to stay a `LISTBOX` (`main.cpp:1384`,
`LBS_OWNERDRAWFIXED`) rather than a `ListView`. Needs GDI-object counting
before and after, since `GetFreeSystemResources` is already ruled out (9x-only
16-bit export, no 32-bit equivalent worth resolving). Blocks Phase 5.1, not
before.

**04.24 -- `off_limits` is not wired to a path that does not exist yet.**
`off_limits` (`bridge/agent/permissions.py:220`) refuses path-tool calls
outside `cwd`, but nothing in `win/net/`, `win/rpc/` or `win/sync/` sends or
handles an `ATTACH` frame today -- grepped, zero hits. The finding is
accurate as a requirement and premature as a defect: there is no live path
where a guest could send `ATTACH path=/etc/shadow`, because there is no
`ATTACH`. Record it as a one-line requirement for Phase 5.2's first cut --
apply the same containment check to the attach path when it is built -- not
as something to fix now.

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
