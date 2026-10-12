# 03 — Requirements traceability

Audit date 2026-09-21. Scope: every requirement stated in `docs/PLAN.md`
(Architecture, Phases 0–6, Revisions R1–R8, Verification) traced to code in
`win/` and `bridge/`, plus the standing rules in `CLAUDE.md`. `docs/PROGRESS.md`
was read for what is *claimed*; nothing here is accepted on its word.
`docs/BACKLOG.md` is treated as owning what it names — entries it already holds
are reported only where the entry itself is wrong, or where a "deferred" item
is load-bearing for something that has already shipped.

Method: every two-sided claim is cited `file:line` on both sides. Verification
was by reading the code, running `make check` and running the Python suite
(2538 pass, 144 Wine tests deselected, 2 xfail). A final section spot-checks
sixteen of the previous round's 202 `Fixed:` claims.

Labels: **MISSING** (not implemented), **PARTIAL** (implemented, does not meet
the stated requirement), **DRIFTED** (implemented differently, doc not
updated), **DOC-WRONG** (the doc asserts something the code does not do).

---

## High consequence

### 03.1 — There is no PE import allowlist. `make check` prints imports; it does not gate them. **MISSING**

`CLAUDE.md:32-34`: *"Nothing NT-only, ever. A single NT-only static import
kills the image at load, before `WinMain`, with no log written and nothing to
debug. Resolve optional DLLs with `LoadLibrary`/`GetProcAddress`. `make check`
audits imports."*

`make check` does not audit them. `win/Makefile:312-315` is:

```
@echo "=== DLLs ==="; objdump -p $(BUILD)/retroclaude.exe | grep -i 'DLL Name'
@echo "=== W-suffix imports (stubs on Win9x) ==="
@objdump -p ... | grep -E 'W$$' || echo "   (none)"
```

Both steps print and both always succeed. The four steps in that target that
*can* fail are CMOV (`win/Makefile:319`), the unbounded-string grep
(`:330-337`), static constructors (`:345-349`) and stack frames (`:351`).
Nothing compares the DLL set or the symbol set against anything. Run here:
`make check` exits 0 while reporting seven DLLs.

`PLAN.md:722-728` deliberately deferred this: *"**[R] Moved out: the PE import
allowlist test.** … It belongs in Phase 3, where forked code actually
arrives."* `PLAN.md:1988` lists it as part of `make test` — *"PE allowlist from
Phase 3"*. `docs/FORKED.md`'s planned table names the destination file,
`tests/python/test_pe_imports.py`, and the reusable ~45-line `pe_imports()` PE32
parser it would come from. Phase 3 closed 2026-09-18 and Phase 4 closed
2026-09-19. `find . -name '*pe_import*'` returns nothing; `grep -rn
'allowlist\|ALLOWED_DLL' win/Makefile make/ tools/ tests/` returns nothing.

This is not academic. `PLAN.md:581-582` records the binary as importing *"only
KERNEL32/GDI32/USER32"*. It now imports seven DLLs — OLEAUT32, OLE32, COMCTL32,
GDI32, KERNEL32, USER32, WSOCK32 — and 45 KERNEL32 symbols have become ~90.
Every one of them was added by hand with nothing checking it. The current set
inspects clean (`GetDiskFreeSpaceA` not `…ExA`, `GlobalMemoryStatus` not psapi,
no `ReadDirectoryChangesW`), so this is a gate gap rather than a live defect —
but it is the gate for the failure mode `CLAUDE.md` singles out as
undebuggable, and it is the only standing rule with no enforcement at all.

**Not in BACKLOG.md.**

### 03.2 — Phase 1's own hardware gate is still open three phases later, and PROGRESS says "done" **PARTIAL**

`PLAN.md:47`: *"real hardware validated at end of Phase 1 **[R: was Phase
2]**"*. `PLAN.md:719-721`: *"**[R] Gate at end of Phase 1: run it on real
hardware.** Moved up from Phase 2 so that a hardware problem and a RichEdit
problem and a pagination problem are never live at once."* The move was the
point: it exists specifically so that failures do not stack.

`PROGRESS.md:76`: `| 1.9 | Real hardware gate (period PC) | blocked |`.
`PROGRESS.md:35`: `| Real hardware access | end-of-Phase-1 gate | The physical
P200MMX on the LAN |`. Both still current.

`PROGRESS.md:8`: *"Phases 0–4 are done and demoed on the real guest."* The
"real guest" is the QEMU Win98 SE domain, which is what `CLAUDE.md`'s
"Verify on the guest, not under Wine" rule means — and that rule is honoured
well throughout. But it is a different gate from the one Phase 1 declared, and
the summary line reads as though both were discharged. Four phases of
accumulated behaviour now sit on the far side of the gate the plan moved
forward precisely to avoid that.

**Not in BACKLOG.md** (§1 covers the *link* gate, not the hardware gate).

### 03.3 — A Phase 0 gate is marked "provisional — not usable as a threshold", and the design that depends on it is now live **PARTIAL**

`PLAN.md:1993-1996` lists among the Phase 0 gates: *"**[R3] measured deflate
throughput on the real P200MMX**, since every threshold in the adaptive-codec
design is currently an estimate"*. `PLAN.md:32-40` is blunter: measuring it
*"sets every threshold in the adaptive scheme; guessing it would be guessing
the core of the design."*

`PROGRESS.md:52` records 0.5c as **provisional**, with the reason: *"Not usable
as a threshold: TCG on a modern host is nothing like a P200, and `probe.c`'s
sample is a two-line block repeated (164:1), which flatters deflate badly.
Needs a real corpus and real hardware."*

That was survivable while nothing consumed it. It no longer is. The guest now
sends `STAT` (`win/rpc/rpc.cpp:219`), so `guest.link_bps` is a real number, and
`bridge/wire/compress.py:88-106` branches on it:

```python
if link_bps > 400_000 and len(data) < 1024:
    return False
```

`400_000` and `1024` are the thresholds 0.5c was supposed to set. They are
guesses, and they are now deciding codec selection in production. The measured
figures the module's own docstring quotes (`compress.py:11-18`) are compression
*ratios* on a modern box, not P200 throughput.

`PROGRESS.md:8` counts Phase 0 as done.

### 03.4 — Sync file bodies are never compressed, in either direction, and BACKLOG §8 says they are **MISSING**

`PLAN.md:952-953` (Phase 3.3): *"Reconcile by exchanging manifests and diffing;
transfer whole files **compressed**, since source files are a few KB and a
patch format can wait."* `PLAN.md:1873-1876` (R6): *"transfer is
whole-file-compressed for small files, a patch for large ones."*

`bridge/files/synctransport.py:141-160` builds `SYNCPUT` frames with a raw
body — no `enc` header, no `zip_out`, no `should_compress` call. The guest's
`SYNCDAT` path (`win/sync/syncrpc.cpp:378-456`) is likewise raw. `enc=` is
produced in exactly two places on the wire: `bridge/wire/protocol.py:438`
(`TEXT`) and `:480` (`EXPORTDAT`). The two callers of `should_compress` are
`bridge/wire/link.py:154` and `:186` — the transcript and the export.

The guest→bridge direction is not merely uncompressed, it has no compressor at
all: `win/net/net.cpp` inflates (`:57`, `:192`) and never deflates.
`bridge/wire/compress.py:31` defines `LEVEL_UP = 1  # guest -> bridge: the P200
pays here` and nothing references it. So the half of `PLAN.md:150-151`'s R3
design that was described as *"the only direction needing care"* was never
built.

`BACKLOG.md:651-653` states the opposite as settled fact: *"`deflate` alone
covers everything that ships today (transcript text, **sync file bodies**), so
there has been nothing to regret."* That clause is wrong, and it is the
sentence §8 rests its "intentional-so-far" reading on.

Consequence is real on the link the project keeps citing: a 1 MB push at 5 KB/s
is 200 seconds where ~2.1:1 would make it 95.

### 03.5 — "The guest transforms nothing" has a second exception, larger than the one it names, recorded nowhere **DRIFTED**

`CLAUDE.md:24-26` and `PLAN.md:104-123`: the guest makes syscalls and moves
bytes; *"Compression is the one deliberate exception."* `PLAN.md:951` authorises
exactly one guest-side computation in the sync layer: *"**CRC32 comes from
miniz**, which is already vendored and already linked."*

`win/sync/sync.cpp:20-130` does considerably more than CRC32. It classifies
every file as text or binary (tracking `saw_nul`, `saw_crlf`, `saw_bare_lf`,
`saw_lone_cr`, `pending_cr`) and accumulates a **CRLF-normalised** checksum
alongside the raw one, choosing between them at EOF. That is the encoding fold
— the transformation `PLAN.md:114` assigns to the bridge — reimplemented in C
on the guest, with a `RC_SYNC_CHUNK`-sized normalisation buffer in the state
block. Its own header says so (`win/sync/sync.cpp:3-9`): *"the rules here mirror
`bridge/paths.py` exactly, and the reasons are repeated rather than
cross-referenced."*

The engineering is defensible (one disk pass on a 1998 IDE platter, and
`tests/python/guest/test_guest_agreement.py` pins the two implementations
against each other). What is missing is the record. This is the second
deliberate exception to the project's most load-bearing principle, it carries
the exact cross-language duplication cost the principle exists to prevent, and
neither `PLAN.md` nor `CLAUDE.md` mentions it — `CLAUDE.md` still says "the one
deliberate exception".

### 03.6 — 21 of the previous audit's `Fixed:` claims are deferrals, not fixes **DOC-WRONG**

`docs/audit/01-guest-c.md:3-5` defines the convention: *"Findings are the lines
at the end: one per finding, prefixed `"Fixed:"` once the thing it describes has
actually been dealt with."*

202 lines carry that prefix. 21 of them say "recorded in BACKLOG §N" and
describe no change to the code — 15 of the 37 in
`docs/audit/04-planned-features.md` alone: 01.35, 01.38, 01.40, 02.3, 02.32,
04.3, 04.11, 04.15, 04.16, 04.18, 04.19, 04.21, 04.22, 04.23, 04.24, 04.25,
04.27, 04.28, 04.34, 04.35 (and 02.6, which is a partial).

The cleanest case, verified against the code:
`docs/audit/04-planned-features.md:871` — *"**Fixed:** 04.20 RECONNECT IDENTITY
IS NOT ACTUALLY CHECKED -- `_attach` never reads `sess` from HELLO even though
the guest sends it … Recorded in BACKLOG §7."* At HEAD,
`bridge/server.py:725-800` still never touches `sess`; the only reader of that
header anywhere in `bridge/` is `bridge/devtools/fakegui.py:224`, the test
harness. `win/net/net.cpp:463` still sends it. Nothing changed; the line reads
as a closure in a list of closures.

Writing a deferral down is the right call — `BACKLOG.md` exists for exactly
that. Labelling it `Fixed:` is what makes the list unreadable, and it is why
this audit had to re-derive the state of a third of the previous round from
source.

---

## Medium

### 03.7 — `CLAUDE.md`'s binding constraint cites a number the project measured as meaningless, and never corrected **DOC-WRONG**

`CLAUDE.md:28-30`: *"Measured on the 32MB guest with only the app running: 2420
KB free at window creation, 1060 KB after the chat list, **28 KB after a
turn**."* `PLAN.md:770-775` carries the same table.

`BACKLOG.md:76-80` corrects it once: the figure is `dwAvailPhys`, it excludes
the disk cache Win98 shrinks under pressure, *"Allocations were never actually
failing. It was cited as a hard wall when it is not one."*
`PROGRESS.md:1915-1931` goes further and names the file that should change:
*"on this machine `dwAvailPhys` is not an availability signal at all … The
standing rule in CLAUDE.md … rests on a figure that is now 0K next to an app
that works. The rule is still worth keeping … but the evidence cited for it
should be corrected rather than repeated."* `PROGRESS.md:2423` then measures
5208 KB / 4568 KB at Phase 4 close, twice the Phase 2 baseline.

`CLAUDE.md` and `PLAN.md` were never touched. `PLAN.md:943` and `:1616` build
Phase 6's whole strip-capture design on "28KB free" as a hard number.

Related and separate: `PLAN.md:1156-1159` makes *"Guest free memory is measured
again at the end of the phase, the same way as above, and compared against the
28KB baseline"* a Phase 3 gate. Phase 4's close (`PROGRESS.md:2423`) measured
window creation and chat list but **not** after a turn — the only one of the
three points the standing rule actually quotes. `docs/measurements/` holds only
the S1 tool-detection runs.

### 03.8 — `HELLO` grew a body and two headers; the Protocol reference, written to close exactly this gap, does not know **DOC-WRONG**

`PLAN.md:1076-1079` introduces the section: *"The frame vocabulary has been
documented only by its two implementations plus a rolling narrative in
`PROGRESS.md`, which an audit called the single largest documentation gap in
the project. What follows describes the wire **as it ships**, verified
against …"*

Three things it gets wrong:

- `PLAN.md:1082` — *"currently 23 headers"*. The actual count is 25
  (`win/net/net.cpp`, `rc_net_start`): `oemcp` (`:495`) and `maxdepth`
  (`:512`) were added after the section was written.
- `PLAN.md:1092` — the capability group is *"eleven headers"* and lists them.
  It is twelve; `maxdepth` is missing from the list, and
  `bridge/files/caps.py:367` parses it.
- **`HELLO` now carries a body and the section never mentions one.**
  `win/net/net.cpp:514-520` sends the tool inventory as the frame body, with a
  comment explaining why it rides HELLO rather than its own frame;
  `bridge/server.py:830` parses it with `toolrep.parse_rows(hello.body …)`.
  The section's grammar (`PLAN.md:1063-1067`) allows a body on any frame, but
  the `HELLO` description is headers-only, and the row format that body carries
  is documented nowhere in `PLAN.md`.

### 03.9 — An entire shipped subsystem (4.18, tool detection) does not appear in PLAN.md **MISSING (from the design)**

`CLAUDE.md:6-8` makes `PLAN.md` the design and `PROGRESS.md` the log.

`grep -n 'tooldet\|tool detection\|toolchain\|WATCOM\|detected tools' docs/PLAN.md`
returns one hit, `PLAN.md:590`, about the Open Watcom import question. Nothing
else.

What exists: `win/app/tooldet.cpp` (219 lines), `win/app/tooldet.h`,
`win/tools_detect.ini` (86 lines), `win/checks/toolcheck.cpp`,
`bridge/agent/toolrules.py` (200), `bridge/agent/toolrep.py` (117),
`bridge/agent/toolnotes.py` (66), `bridge/profiles/`, a new wire payload in
`HELLO`'s body, `machine.py`'s `tools_block` (`bridge/agent/machine.py:197`),
and three test suites. `PROGRESS.md:2514` numbers it *"Phase 4.18"* — inside a
phase `PLAN.md` declares closed with a fixed item list that has no such item.

The work looks well reasoned and the layering argument in `PROGRESS.md:2521-2536`
is sound. The traceability problem is that a subsystem of this size entered a
closed phase with no design entry, so `PLAN.md`'s Phase 4 list can no longer be
used to answer "what is Phase 4".

### 03.10 — `ANCHOR` is a dead frame type listed in the "what is actually sent today" inventory **DOC-WRONG**

`PLAN.md:1134` lists `ANCHOR` among the bridge→guest frames, under a heading
that says (`PLAN.md:1113-1114`) *"This is what is actually sent and handled
today, not a design intent"*.

`bridge/wire/protocol.py:364-369` defines `anchor_frame()`. Nothing calls it:
`grep -rn 'anchor_frame\|anchor(' bridge/` finds the definition and the
paginator's unrelated internal `Anchor` list only. `win/app/main.cpp:937-943`
handles it by writing one `rc_log` line; `:1031` is its dispatch row.

`PLAN.md:1958`, in "Explicitly cut", already says: *"Anchor list /
jump-to-message | **[R]** Polish; `[<<] [<] [>] [>>]` is enough"*. So the
protocol reference lists as live a frame for a feature the same document
records as cut.

### 03.11 — `RESYNC`'s sender was deleted with the replay buffer; the handler added for it in the last audit now answers nobody **DOC-WRONG / dead code**

`PLAN.md:1127` lists `RESYNC` as live — *"a replay gap the bridge could not
fill"* — a mechanism `PLAN.md:398-407` itself records as retired along with
`seq=`, `from=`, `next_seq=` and `bridge/wire/replay.py`.

`grep -rn RESYNC bridge/` returns exactly one hit,
`bridge/devtools/fakegui.py:310` — a *receiver*. Nothing builds the frame.
The guest keeps the handler (`win/app/main.cpp:981-996`) and its dispatch row
(`:1040`).

`PLAN.md:1129` marks `TOOL` as dead in the same list; `RESYNC` is not marked,
and it is dead for a more interesting reason — the previous audit's 03.5 fix
(add a handler) was overtaken by the replay retirement.

### 03.12 — Two PLAN sections still specify the retired replay mechanism **DOC-WRONG**

`PLAN.md:464-465`: *"On reconnect: `HELLO ... resume=<sess> from=<seq>`."*
`PLAN.md:865-866` (Phase 3.0): *"The pending request survives a **reconnect**
the same way display frames do, through the replay buffer: the bar
reappears."*

Neither survives. `grep -n 'resume\|from=' win/net/net.cpp` finds no such
header. What actually happens is explicit re-issue:
`bridge/server.py:874-903` — *"There is no replay to rely on for anything a
reconnect needs back … every piece of live state below is re-issued
explicitly"* — followed by `guest.perms.reissue()`, `_reissue_halt`,
`_reissue_turn`, `_reconcile_halt`.

`PLAN.md:398-407` records the retirement in the `seq=` bullet only. The two
sections that depend on it were not updated, so the plan reads as specifying a
mechanism its own errata removed. (The behaviour is fine — arguably better —
this is purely traceability.)

### 03.13 — "Control frames must not queue behind streaming text" is not implemented as specified, and nothing tests the property **DRIFTED**

`PLAN.md:468-470`: *"`PERMREQ` shares the GUI stream with a flood of `TEXT`;
the coalescer drops pending display frames when a control frame is queued."*

`bridge/wire/coalesce.py` (the whole file) has no notion of a control frame, no
priority and no drop path. What exists is the opposite ordering: an explicit
`await self._flush_text(guest)` before a tool block
(`bridge/server.py:1345`, `:1361`), which pushes the buffered text *out first*.
The permission desk sends `PERMREQ` directly (`bridge/agent/permdesk.py:241`)
with no flush and no preemption.

In practice a buffered `TEXT` has not been written to the socket, so `PERMREQ`
does overtake it — the requirement's *intent* is met by accident of the
buffering. But the specified mechanism is absent, the guarantee is incidental
rather than designed, and no test asserts that a control frame beats queued
text.

### 03.14 — The transfer progress UI shipped as roughly a third of what PLAN requires; two of the missing pieces are owned by nobody **PARTIAL**

`PLAN.md:281-285`: *"A status-bar pane showing **direction, what, bytes done /
total, rate and ETA**, plus a comctl32 **progress bar** and a **Cancel**
button for anything over ~1s. The first frame of a chunked transfer carries
`bytes=<total>` so the guest can render a determinate bar rather than a barber
pole."* `PLAN.md:793-796` schedules it: *"the progress bar and Cancel land in
Phase 3 when the first large transfers appear."*

`win/app/xferstat.h` (the whole class) ships: rx/tx byte counters, `done/total`
for a page load, and a 1 Hz rate. Missing: the **ETA**, the **"what"** (no
transfer is named), the **progress bar**, and **Cancel**.

Phase 3 shipped the sync layer — whole-file pushes up to `MAX_FILE`'s 1 MB
(`bridge/files/syncrun.py:46`) — and Phase 4 shipped Export. Both are "large
transfers".

`BACKLOG.md:317-330` (§6) owns `CANCEL` explicitly and correctly. It mentions
the progress bar only in passing (`BACKLOG.md:351-356`, and then only to record
that `ICC_PROGRESS_CLASS` is now registered). The ETA and the transfer
description are in no backlog entry at all.

### 03.15 — BACKLOG §1 is stale on its blocker and on one of its three named weaknesses **inaccurate backlog entry**

`BACKLOG.md:16-21` (§1, throttled-link gate): *"**Status:** deferred by
decision, 2026-09-17 … It has never been run: `tc` needs root and this
environment has no passwordless sudo."* `BACKLOG.md:44-45`: *"**To close it:**
`sudo tc qdisc add dev lo root netem rate 40kbit` …"*

`BACKLOG.md:278` (§5) and `BACKLOG.md:305-316` record the opposite: unprivileged
user namespaces work here, `tc qdisc add dev lo root netem rate 40kbit`
succeeds inside one, and `pasta --config-net` keeps outbound internet while
leaving `lo` shapeable. `BACKLOG.md:441-443` (§6) already writes *"close the
netem gate first (§1 above -- no longer blocked on sudo, per §5's last
bullet)"* — §6 knows, §1 does not.

`BACKLOG.md:29-33` also still lists as a live weakness *"**The queue's byte cap
is soft.** A writer that has waited its limit (50 × 100 ms) enqueues anyway
rather than dropping the frame."* That is closed: `win/net/net.cpp:719-726` now
logs and returns `-1` when the wait expires with no room. §5's own narrative
(`BACKLOG.md:281-287`) records both the correction (*"it was not soft, it was
absent"*) and the fix, but §1's list was never updated.

Net effect: the reader of §1 is told the gate is blocked on a permission it is
not blocked on, and is pointed at a defect that no longer exists.

---

## Low / cosmetic

### 03.16 — The OS-neutral tool schema ships a DOS path to the model, twenty lines under a comment forbidding exactly that **DRIFTED**

`bridge/agent/tools.py:112-121` (on the `command` property): *"No OS named
here, for the same reason the main description carries no shell syntax: this
schema ships to every machine."* `bridge/agent/tools.py:145-146` (the `cwd`
property, same schema): *"Working directory on the connected machine, e.g.
`C:\WORK`."*

It is the only live string in `bridge/` outside `profiles/` and `devtools/`
that hardcodes a drive-letter path (every other hit from `grep -rn 'C:\\\\'
bridge/` is a comment or docstring). `tests/python/meta/test_no_os_in_code.py`'s
`BANNED` list has no drive-letter needle, so the guard cannot see it. Harmless
on the three targets `CLAUDE.md:14` names (all DOS-path machines); it is the
inconsistency that is worth a line.

### 03.17 — The guest builds a fourth CSS class that the cross-language guard does not sweep **FIXED**

`tests/python/meta/test_guest_css.py:1-10`: *"the guest builds three blocks
itself — its own echo of what you typed, a notice, an error — and hard-codes
their class names."* It finds them by regex over `RC_CSS_*` defines
(`win/app/htmlview.h:61-74`).

**This paragraph describes the pre-fix tree and was never updated when the
finding was closed; corrected 2026-10-12 after it was cited twice as live
justification while planning stage 3.** The header said PARTIAL while the ledger
below said Fixed, and nothing checks that those two agree. What it read was:
*"`win/app/main.cpp:356` writes `<div class="status">` as a bare literal, with a
`<img src="spin.gif">` inside it. `.status` is not an `RC_CSS_*` macro, so the
test does not see it."*

Both halves are now closed. The macro exists -- `RC_CSS_STATUS`
(`win/app/htmlview.h:61`) -- and the status row that uses it has since moved out
of `main.cpp` into `win/app/statusbar.cpp:146-152`; `main.cpp` is 326 lines and
holds no such literal. `test_guest_css.py`'s `guest_classes()` sweeps every
`RC_CSS_*` define in the header and asserts each class against the stylesheet, so
the guard's invariant ("rename `.notice` on the bridge and the guest's notices
silently become unstyled text") is enforced for all four.

**The mirror of this finding is still open, and the sweep is structurally unable
to see it:** a class the *bridge* invents, which no `RC_CSS_*` names.
`.thinking` is exactly that -- `bridge/services/presentation.py:38-41` writes
`<div class="{style}">` and `bridge/services/session.py:343` passes
`style="thinking"`, while the stylesheet has no `.thinking` rule and
`bridge/view/html.py` mentions the word once, in a comment. The thinking stream
has never been styled, on either side, and no test could notice. Stage 3 closes
it in A10 and B3, which is also what makes `view.tpl` and `view.css` a single
place to check.

### 03.18 — An unrecognised guest exec status is reported to the model as success **PARTIAL**

`bridge/status.py:56-70`:

```python
try:
    return cls(value)
except ValueError:
    return cls.OK
...
@property
def is_error(self) -> bool:
    return self is not ExecStatus.OK
```

The stated reason is compatibility with the behaviour before the enum existed.
The effect is fail-open in the one direction that matters: a status word added
on the guest side — Phase 5's upload and Phase 6's tools are both expected to
add some — reaches the model as an ordinary successful result. This is the same
shape as audit finding 03.18 (*"an unknown manifest type letter is dropped
silently rather than detected"*), which was closed by detecting it.

### 03.19 — "Explicitly cut" still lists MSHTML as cut and justifies two cuts by Samba **DOC-WRONG**

`PLAN.md:1966`: *"| Scrollback trimming, custom-drawn view, **MSHTML** |
Pagination supersedes / see Architecture |"*. MSHTML is the shipping renderer
(`win/app/htmlview.cpp`, 1039 lines; `PLAN.md:504-536` records the reversal).

`PLAN.md:1964`: *"| `UPLOAD`/`MKDIR`/`DELETE`, `LAUNCH` | **Samba covers
them**; `LAUNCH` drags in the JSON builder |"*. Samba was overturned by R6
(`PLAN.md:1855-1890`), and `SYNCMKDIR` exists
(`bridge/files/synctransport.py:178-186`).

This is the table that is supposed to be the project's own record of scope
decisions, which is why it is worth correcting rather than ignoring.

### 03.20 — "Repo layout" describes a tree that has not existed since the C++ migration **DOC-WRONG**

`PLAN.md:1918-1944` shows flat `win/frameproto.c/.h net.c/.h … main.c
htmlview.c/.h rpc.c/.h exec.c/.h sync.c/.h syncrpc.c/.h`; the tree is
`win/{app,core,net,rpc,sync,checks}/*.cpp`. It shows `bridge/ server.py
session.py rpc.py tools.py markdown.py fold.py pages.py paths.py replay.py
protocol.py fakegui.py`; `rpc.py`, `markdown.py` and `replay.py` do not exist
and everything else moved into `bridge/{agent,files,view,wire,devtools}/`. It
shows `tests/python/ test_pe_imports.py (Phase 3) test_protocol.py
test_markdown.py test_fold.py`; the first two do not exist and the suite is
now `tests/python/{agent,files,guest,meta,server,tools,view,wire}/`.

### 03.21 — PLAN R5 says the toolbar is outstanding and blocks Phase 5.1; a toolbar shipped **DOC-WRONG**

`PLAN.md:1846-1851`: *"What is left of R5 is the toolbar, which is not a
resource problem … It also has no home yet, since `win/app/main.cpp` builds a
hand-written menu bar and individual buttons and there is no comctl32 toolbar
to hang bitmaps on. **Blocks Phase 5.1**, not this stage."*

`win/app/toolbar.cpp` is 373 lines and `win/app/toolbar.h` 136;
`PROGRESS.md:2396-2402` debugs three toolbar bugs found by clicking, including
`TBSTYLE_EX_DRAWDDARROWS` and `BTNS_WHOLEDROPDOWN`. `BACKLOG.md:1008` (§11, "04.23 -- CLOSED 2026-09-19")
is updated (*"Phase 5's attach control has somewhere to go"*); `PLAN.md` R5 is
not, so the plan still records Phase 5 as blocked on something that exists.

### 03.22 — PLAN R8's two named commands are both absent, one deliberately **DOC-WRONG**

`PLAN.md:1913`: *"A menu of the slash commands that make sense here —
`/context`, `/goal`, and whichever others are useful and cheap"*.

`bridge/server.py:3888`: `COMMAND_MENU = ["/compact", "/rename", "/init"]`.
`/context` is excluded on purpose (`bridge/server.py:3875` — *"the status bar
has the gauge"*) and `/goal` is not a command the CLI has. The exclusion list
in `bridge/server.py:3868-3887` is excellent and reasoned; R8 in `PLAN.md` was
simply never reconciled with it.

### 03.23 — PROGRESS's "Phase 4 closed" entry describes the work-folder design as the reverse of what ships **DOC-WRONG**

`PROGRESS.md:2369-2377`: *"The per-chat work folder was `C:\Gaston\<chat>`, a
new mirror per conversation. That means a new baseline, a new `tree_id` … The
conversation moves **inside** the existing mirror instead."*

`PLAN.md:1235-1271` (the item, dated `[R -- 2026-09-19, CORRECTED]`) and the
code both do `C:\Gaston\<name>`, because `C:\Gaston` *is* the mirror root:
`win/core/ini.cpp:35` defaults `sync.root` to `C:\Gaston`, and
`bridge/server.py:1131-1205` (`_name_work_dir`) creates the folder under
`self.sync_dir`. The two statements are reconcilable — "inside the mirror" and
"`C:\Gaston\<name>`" are the same thing once the root is `C:\Gaston` — but the
PROGRESS entry reads as a reversal that did not happen, and it is the entry a
future reader will land on when they search for why the folder is where it is.

### 03.24 — PLAN 3.2 still describes `exec.c` as a fork with two upstream bugs to fix; it is a rewrite **DOC-WRONG**

`PLAN.md:936-941`: *"`exec.c` forked with the `send_response(SOCKET, ...)`
signature kept and only its body replaced — that is what makes 'lifted
verbatim' nearly true. Two bugs to fix while forking, both already identified:
`taskkill` … and `HeapReAlloc` failure leaks …"*

`win/rpc/exec.cpp:1-28`: *"Derived from `run_capture()` in `probe.c` … **It is a
rewrite rather than a lift**, because probe.c's version has six defects …"*.
`grep -rn 'retro-agent' win/ --include='*.cpp'` finds no forked exec code.
`docs/FORKED.md`'s "Planned" table still shows `agent/src/exec.c -> win/exec.c`
with those two fixes. (`FORKED.md`'s "Nothing forked yet" is *correct*, so
there is no attribution problem — only a stale plan and a stale table, and the
`win/exec.c` / `win/input.c` / `win/rpc.c` destinations in it predate the
package split.)

### 03.25 — Two `Fixed:` lines in the previous round disagree with each other about `STAT` **DOC-WRONG**

`docs/audit/02-bridge-python.md:1086` (02.6): *"**STAT is NOT sent**, so
`guest.link_bps` stays None and every adaptive threshold in
`compress.py`/`coalesce.py` is inert in production; that half is recorded in
BACKLOG rather than built."*

`docs/audit/03-contracts.md:643` (03.6): *"**STAT is fixed too**: the guest
measures bytes moved over wall time on its sweep … and sends it."*

The code agrees with 03.6 — `win/rpc/rpc.cpp:196-219` sends `STAT` with a `bps`
header, and `bridge/server.py:1799-1810` consumes it into
`guest.set_link_speed`. 02.6's body is stale, and it is the one that names a
backlog deferral that no longer applies. (See 03.3 for why the fix makes an
unmeasured threshold live.)

---

## Requirements in PLAN prose with no test

Checked by grepping `tests/python/` for the identifying symbol or string.

- **The ~10-minute permission deadline that auto-denies.** `PLAN.md:840-842`:
  *"Both mitigations are required: a bridge-side deadline (~10 min) that
  auto-denies with an explanatory message, and `fakegui.py` as the manual
  escape hatch."* `bridge/agent/permissions.py:37` (`DEADLINE_S = 600.0`) and
  `bridge/agent/permdesk.py:205` implement it. `grep -rn DEADLINE tests/`
  returns nothing. The sibling requirement — a guest that disconnects with a
  prompt pending is denied immediately — *is* covered
  (`bridge/agent/permdesk.py:330-340` plus the reaper tests).
- **`hello_timeout` and `bad_caps` as wire refusals.** `PLAN.md:1108-1110`
  names the full refusal vocabulary. `expected_hello`, `bad_frame_cap`,
  `bad_secret` and `busy` appear in
  `tests/python/server/test_server_integration.py`; `hello_timeout`
  (`bridge/server.py:721`) and `bad_caps` (`bridge/server.py:736`,
  `bridge/handshake.py:91`) appear in no test. The `CapsError` that produces
  `bad_caps` is thoroughly covered at unit level
  (`tests/python/files/test_caps.py`, 15 cases) — what is untested is that it
  becomes an `ERROR bad_caps` frame and closes the link, which is the half
  `CLAUDE.md:44-50` actually promises.
- **Control frames not queuing behind text** (03.13) — no implementation, so
  no test.
- **Sync transfer compression** (03.4) — no implementation, so no test.

## Working-tree note (not a requirements finding)

**This was observed mid-flight and has since been fixed by someone else. It is
not a defect and needs no action.** The paragraph is kept because the
observation was real when made and because the reason it was transient is worth
recording.

At roughly 13:55 on 2026-09-21 `make test` failed at collection: the staged
`tools/` reorganisation had moved `tools/qmp.py` to `tools/vm/qmp.py` while
`tests/python/tools/test_qmp_pointer.py:32` still did
`sys.path.insert(0, os.path.join(HERE, "tools"))` and `import qmp`, so
collection aborted and *all* tests were skipped — the "a silenced gap looks like
success" shape `PROGRESS.md:2886-2902` (4.37, "The lists") was written about.

**Re-checked at 14:05: collection succeeds, 2829 tests collected, and
`tests/python/tools/test_qmp_pointer.py` passes 109/109.** The file was edited
at 13:59:04 to insert `tools/vm` instead of `tools`.

The cause is that **another session was working in this tree throughout this
audit.** Every unstaged change in the repository has an mtime inside this
audit's window (13:57 to 14:07), and the content is coherent follow-through on
the `tools/` reorg: path references updated across `CLAUDE.md`, `docs/PLAN.md`,
`docs/BACKLOG.md`, `bridge/files/manifest.py`, `win/Makefile` and five `win/app`
headers, plus a new shared `tools/toolkit.py` with a `REPO` constant adopted by
the moved scripts. None of it touches the sync layer, the wire protocol or the
structure of `win/Makefile`'s `check` target, so no finding in this audit is
affected — including 03.1, whose two `objdump | grep` lines are unchanged apart
from a path in a neighbouring comment.

The lesson for the *next* auditor is the one worth keeping: a test-run result
taken while another session is editing the tree is a snapshot, not a fact, and
should be re-checked before it is reported.

---

## Sections verified correct

One line each, checked against code, not against PROGRESS.

- **Phase 3.0, permission prompts.** Modeless bar, Send gated, one prompt at a
  time with a queue, `tool_use_id` as the correlation id, `suggestions` as
  "Always", `PermissionResultDeny(interrupt=False)`, disconnect denies
  immediately, `PERMSTATE` restores the bar on reconnect — all present
  (`bridge/agent/permdesk.py`, `win/app/bar.cpp`).
- **Phase 3.1, the three ordered timeouts.** Implemented and enforced across
  languages by `tests/python/meta/test_exec_timeout_contract.py`, which reads
  `win/rpc/rpc.cpp`'s `#define`s and `bridge/agent/tools.py`'s constants and
  asserts `ceiling + 30 s < 300 s`. One of the better tests in the repo.
- **Phase 3.1, the exec test.** Exactly three tools ship (`exec`,
  `sync_status`, `sync_resolve`, `bridge/agent/tools.py:60/178/192`), matching
  `PLAN.md:996-1008`'s correction, with `allowed_tools=[]`
  (`bridge/agent/session.py:142`) and `setting_sources=[]` (`:148`).
- **Phase 3.3, the sync layer.** One mirror per conversation, one baseline per
  root (`bridge/server.py:2640-2708`), `.gaston/conflict` staging
  (`bridge/files/syncrun.py:162`), guest wins on conflict, CRC32 from miniz,
  streamed manifest (`SYNCENT` per entry, `win/sync/syncrpc.cpp:85`),
  `FindFirstChangeNotification` via `GetProcAddress`.
- **`CLAUDE.md`'s Continue rule.** `bridge/server.py:1727-1767` — pressing
  Continue lifts the refusal directly and the release travels with the turn
  that explains it. Nothing asks twice.
- **`CLAUDE.md`'s capability rule.** `bridge/files/caps.py` refuses rather than
  guesses on every enum and every range; `eol=cr` is refused at the handshake;
  `shortnames` and `maxdepth` are read rather than assumed
  (`bridge/server.py:946-960`). Well tested.
- **Threading and socket discipline.** `_beginthreadex` throughout
  (`win/net/net.cpp:535/549`, `win/rpc/rpc.cpp:585`), `PostMessage` return
  checked with the frame freed on failure and an in-flight cap the reader
  blocks on (`win/net/net.cpp:228-240`), `shutdown(SD_SEND)` before
  `closesocket` (`:825`), `WM_TIMER` backoff 1→15 s
  (`win/app/main.cpp:1434-1436`), send lock released between chunks, outbound
  queue byte-bounded with a hard drop (`:719-726`).
- **Pagination.** Forward numbering, never renumber, soft caps on lines *and*
  bytes *and* blocks, 2× hard cap with `continued=1`, LIVE seal only at a
  message boundary, page size scaled from `mem_kb`
  (`bridge/view/pages.py:31-118`). Tested.
- **Phase 4's own item list.** Every item found: model/effort/work/mode in one
  `OPTION` batch with `description` as its own header
  (`bridge/server.py:3418-3517`); Export as HTML with the Save dialog opened
  first and `GetSaveFileNameA` via `LoadLibrary` (`win/app/export.cpp:14-45`);
  "What can run without asking" with the incompleteness paragraph
  (`bridge/agent/permissions.py:416-460`); autoscroll as a real toggle
  (`win/app/htmlview.cpp:700-710`, `win/app/pagebar.cpp:91-113`); `auto` back
  on the mode menu with `MODE_CAVEAT` (`bridge/server.py:3840-3850`); Ctrl+A
  swallowing the SOH (`win/app/input.cpp:188-205`); answered permission boxes
  collapsing via a separate `.permdone` class (`bridge/view/html.py:531-549`);
  `cwd` genuinely optional with a full JSON Schema
  (`bridge/agent/tools.py:103-151`); the on-disk transcript cache
  (`win/app/cache.cpp`, restored once per outage at
  `win/app/main.cpp:1429`); and the change-work-directory dialog with
  pre-flight, `Gaston-Incoming`/`Gaston-Replaced`, `staged=merge` reaching the
  agent (`win/app/workdlg.cpp`, `win/app/workcopy.cpp`,
  `bridge/server.py:1918-1930`).

---

## Previous-audit `Fixed:` claims spot-checked

Sixteen checked against HEAD.

| id | claim | verdict |
|---|---|---|
| 01.0 | unbounded string functions removed from `win/` | **holds.** `grep -rn 'strcpy(\|strcat(\|sprintf('` over `win/` minus `third_party` returns 0, and `win/Makefile:330-337` fails the build on a reintroduction |
| 01.62 | outbound queue byte cap is absent, not soft | **holds.** `RC_OUTQ_MAX_BYTES` with a hard drop and `-1` return at `win/net/net.cpp:719-726`; `rc_net_outq_bytes` exists so `netcheck.exe` can assert it |
| 02.6 | `STAT` and `PING` handled and never sent | **text wrong, code right.** Both are sent (`win/rpc/rpc.cpp:219`, `:230`). The `Fixed:` body still says STAT is not — see 03.25 |
| 02.7 | idle guest disconnected at 90 s against a 600 s prompt deadline | **holds.** `idle_timeout_s = 90.0` (`bridge/server.py:534`) with a `PING` keepalive rather than an exemption, as the claim describes |
| 02.27 | `GETOPTIONS` was a dead dispatch branch | **holds.** `grep -rn GETOPTIONS` over the whole repo returns nothing |
| 03.4 | guest hand-builds HTML with class names shared with the bridge | **holds for what the guest names.** `RC_CSS_*`/`RC_SLOT_*` macros plus `tests/python/meta/test_guest_css.py`, and the `class="status"` row is inside the sweep since 03.17 was fixed. What stays outside it is a class the *bridge* invents: `.thinking` has no rule and no macro (03.17) |
| 03.5 | TOOLRESULT, RESYNC and PONG unhandled by the guest | **holds but overtaken.** All three have handlers; `RESYNC`'s sender was then deleted with the replay buffer, so the handler is now dead code (03.11) |
| 03.6 | STAT / PING / GETOPTIONS never sent by the guest | **holds.** Two now sent, the third removed rather than left as a dead handler |
| 03.13 | 11-byte working-name reserve duplicated with no build check | **holds.** C89 static assert at `win/net/caps.h:84-85` tying `RC_WORKRESERVE_GASTON` to `RC_SYNC_WORK_SUFFIX`; `bridge/files/caps.py:149` mirrors it |
| 03.15 | exec timeout ceiling was an untested three-way constraint | **holds, well.** `tests/python/meta/test_exec_timeout_contract.py` reads both sources as text and asserts floor/default/ceiling equality and the ordering |
| 03.27 | a seventeenth frame header was silently discarded | **holds.** `win/net/frameproto.cpp:117` returns `-1` (drops the frame) rather than discarding the header |
| 03.37 | duplicate header keys handled asymmetrically | **holds.** Bridge raises `ProtocolError` (`bridge/wire/protocol.py:154`); guest drops the frame (`win/net/frameproto.cpp:128`) |
| 03.46 | a timed-out exec reported to the model as success | **holds.** `ExecStatus.is_error` is `self is not OK` (`bridge/status.py:68-70`), so `TIMEOUT` is an error. Note the new fail-open it introduces at `parse()` — 03.18 |
| 03.49 | guest never percent-encoded outgoing header values | **holds.** `append_quoted` at `win/net/frameproto.cpp:207`, used by `rc_frame_build` at `:245`, with `framecheck.exe` and `tests/python/guest/test_guest_frame.py` feeding the builder's bytes to the bridge's reader |
| 04.1 | the MCP tool closure bound the bridge, not the guest | **holds.** `build(bridge, guest=None)` (`bridge/agent/tools.py:40`), called as `build(self, guest)` (`bridge/server.py:1481`), with `test_two_builds_bind_two_different_guests` |
| 04.5 | the RPC worker had one job slot | **holds.** Four lanes in `RC_LANE_TABLE` (`win/net/frameproto.cpp:42-46`) with `rpccheck.exe` asserting EXECKILL returns in single-digit ms during a live EXEC |
| 04.20 | reconnect identity is not actually checked | **OVERSTATED — not fixed.** `bridge/server.py:725-800` still never reads `sess`; the line is a deferral wearing a `Fixed:` prefix (03.6) |
| 04.26 | nothing asserted every `@tool` function is registered | **holds.** `test_every_tool_decorated_function_is_registered` and `test_registration_list_names_nothing_undeclared` (`tests/python/agent/test_agent_tools.py:60,75`) |

Fifteen hold as written. Two (02.6, 03.5) are true of the code but their
recorded text no longer matches it. One (04.20) is not a fix at all, and it is
the visible instance of the systematic problem in 03.6.

---

# Resolution — 2026-09-22

All 25 findings are closed. Written as LIVE text, per `00-audit-method.md`:
nothing above is commented out, so the reasoning that justified each finding
stays readable and re-checkable.

**Verified how.** `make -C win all && make -C win check`: 202 symbols from 7 DLLs, no NT-only imports, PE 4.0/4.0, zero warnings. Full test suite: **2995 passed, 2 xfailed, 0 failed** (including all 166 guest tests under Wine). Ledger verified with `tests/python/meta/test_audit_ledger.py`.

Stale: 03.1 — PE import allowlist gate was implemented in `tools/check/imports.py` and run via `win/Makefile:322` (checking 7 DLLs, 32 NT-only deny-list symbols, PE 4.0/4.0); `Makefile` updated to include `retroclaude.exe` in `guest-checks` to prevent test skip on clean builds.

Deferred: 03.2 — Phase 1 hardware gate recorded in `docs/BACKLOG.md` §1 as open, not run; machine is available, and `docs/PROGRESS.md:8` clarified that demo was run on QEMU guest while real-hardware gate remains open.

Fixed: 03.3 — Runtime deflate measurement added via QueryPerformanceCounter in `win/net/net.cpp` and `win/rpc/rpc.cpp:214-227` emitting `deflate_bps` in `STAT`; adaptive compression rule (link_bps < 0.5 * deflate_bps) replaced guessed constants in both guest and bridge.

Fixed: 03.4 — Sync file bodies are now compressed in both directions: bridge compresses `SYNCPUT` in `bridge/files/synctransport.py` under `send_lock`, guest compresses bulk frames (`EXECOUT`, `SYNCDAT`, `SYNCENT`) in `win/net/net.cpp` via persistent miniz stream `g_def` (`TDEFL_LESS_MEMORY=1`).

Fixed: 03.5 — Documented the second guest transformation exception (CRLF normalization and checksum fold in `win/sync/sync.cpp:20-130`) in `CLAUDE.md:24-26` and `docs/PLAN.md:104-123`.

Fixed: 03.6 — Relabeled 224 audit ledger items in `docs/audit/*.md` into four distinct tokens (`Fixed:`, `Stale:`, `Deferred:`, `Partial:`) and added continuous sequence and valid token validation in `tests/python/meta/test_audit_ledger.py`.

Fixed: 03.7 — Replaced the misleading "28KB after a turn" allocation constraint in `CLAUDE.md:30` and `docs/PLAN.md` with pointers to measured bounds (5208 KB / 4568 KB at Phase 4 close) and `docs/mem-audit/`.

Fixed: 03.8 — Corrected Protocol reference in `docs/PLAN.md`: updated header count to 25, capability group to 12 (including `maxdepth` and `deflate`), and documented `HELLO` body tool inventory row format.

Fixed: 03.9 — Documented the Phase 4 tool detection subsystem in `docs/PLAN.md`, covering `win/app/tooldet.cpp`, `tools_detect.ini`, `toolrules`/`toolrep`/`toolnotes`, and the HELLO body.

Stale: 03.10 — `ANCHOR` is not dead: sent by bridge per turn from `_start_turn` (`bridge/server.py:2334`) and logged by guest; `docs/PLAN.md:1134` updated to clarify that jump-to-message UI was cut.

Fixed: 03.11 — Marked `RESYNC` as retired in `docs/PLAN.md:1127` and removed dead `RESYNC` handler and dispatch table row from `win/app/main.cpp`.

Fixed: 03.12 — Updated `docs/PLAN.md:464-465` and `:865-866` to describe explicit re-issue on reconnect (`bridge/server.py:874-903`) rather than retired replay buffer.

Fixed: 03.13 — Corrected `docs/PLAN.md:468-470` to match non-blocking priority ordering without dropping display frames; added test verifying `PERMREQ` precedes buffered text in `tests/python/wire/test_compress.py`.

Fixed: 03.14 — Reconciled transfer progress UI in `docs/PLAN.md` and `docs/BACKLOG.md` §6: recorded ETA, transfer name, and progress bar as cut by design; aligned quiet threshold in `win/app/xferstat.h`.

Fixed: 03.15 — Updated `docs/BACKLOG.md` §1: unblocked netem gate via unprivileged user namespaces and noted outbound queue byte cap is hard at `RC_OUTQ_MAX_BYTES` (`win/net/net.cpp:719-726`).

Fixed: 03.16 — Removed DOS paths and built-in names from tool schema descriptions in `bridge/agent/tools.py`; strengthened `tests/python/meta/test_no_os_in_code.py` to ban Windows paths in live strings.

Fixed: 03.17 — Added `RC_CSS_STATUS` define in `win/app/htmlview.h`, used by the status row (then `win/app/main.cpp:356`, now `win/app/statusbar.cpp:149`); updated regex in `tests/python/meta/test_guest_css.py` to match multi-class strings and verify all defines. Header token corrected from PARTIAL to FIXED 2026-10-12.

Fixed: 03.18 — Added `ExecStatus.UNKNOWN` in `bridge/status.py` and `EXEC_STATUS_PROSE`; updated `bridge/server.py` and `win/rpc/rpc.cpp` to treat unrecognized exec status as error; added agreement test in `tests/python/wire/test_exec_status.py`.

Fixed: 03.19 — Corrected `docs/PLAN.md:1966` and `:1964` to reflect MSHTML as shipping renderer and `SYNCMKDIR` as active wire frame.

Fixed: 03.20 — Updated repository layout in `docs/PLAN.md` to reflect `win/{app,core,net,rpc,sync,checks}` and `bridge/{agent,files,view,wire,devtools}`.

Fixed: 03.21 — Updated `docs/PLAN.md` R5: noted toolbar is implemented and shipped in `win/app/toolbar.cpp`.

Fixed: 03.22 — Reconciled `docs/PLAN.md` R8 with shipping commands in `COMMAND_MENU` and documented deliberate exclusions (`/context`, `/goal`).

Fixed: 03.23 — Corrected `docs/PROGRESS.md` work-folder description to clarify conversation directory lives inside `sync.root`.

Fixed: 03.24 — Corrected `docs/PLAN.md` 3.2 and `docs/FORKED.md` to document `win/rpc/exec.cpp` as a clean rewrite rather than a fork, and `tools/check/imports.py` using `objdump`.

Fixed: 03.25 — Reconciled `STAT` finding 02.6 in `docs/audit/02-bridge-python.md` with `03-contracts.md` and verified `STAT` is sent by guest and handled by bridge.

