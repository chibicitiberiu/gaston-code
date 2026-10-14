# Roadmap — the order the rest of this gets done in

`PLAN.md` is the design. `PROGRESS.md` is what was built and why. `BACKLOG.md`
is what was deferred, with the reason and what closing it would take. This file
is the fourth question those three do not answer: **in what order, and what has
to be decided before what.**

It is a living file. A stage that finishes gets its outcome recorded in
`PROGRESS.md` and its line here struck, not deleted -- the order something was
done in is part of why it looks the way it does.

**Where the project stands, 2026-10-10.** Every `REMOTE.md` build step is
complete (1 through 6). `PLAN.md` phases 0 through 5 are done. Phase 6 is the
only part-built phase, and nothing in the plan sits at zero. So what remains is
a tail: small unbuilt tools, one architectural move, a pile of recorded
decisions, and a machine nobody has run this on.

---

## The shape, and the one rule behind the order

Six stages, and two that split off a stage already under way -- **3b** out of
3 when pagination turned out to be the client's business, and **4b** out of 4
when the driving indicator turned out not to fit in the client at all. Both
splits were made by review rather than by size, and both are recorded where
they happened.

They are the five that were asked for, with one change: the
**compiler-flag decision was pulled out of the optimisation stage and put before
the refactor**, because it pays for it rather than cleaning up after it.

The rule behind every other move here: **a decision that gates work goes in the
stage before that work, not in the stage that discusses decisions.** Three of
the decisions sitting in `BACKLOG.md` are inputs to stage 3 and they are listed
in stage 1 for that reason.

| # | Stage | Exit condition |
|---|---|---|
| 1 | Fruit, the decisions that gate stage 3, and the injection harness — **DONE 2026-10-11** | the fruit is picked, an injection run is repeatable without risking the guest, and nothing in stage 3 is waiting on an answer |
| 2 | Size: the flag decision and the gremote trim — **DONE 2026-10-11** | a measured before/after, and room in the caps for stage 3 |
| 3 | The markup moves to the client — **DONE 2026-10-12** | a second client could be written without touching the bridge |
| 3b | Pagination: the bridge stops dividing the transcript — **DONE 2026-10-13** | the guest asks for blocks by range, and `pages.py` is an append-only transcript |
| 3c | R7: several guest machines against one bridge — **DONE 2026-10-13** | two guests hold two mirrors and two baselines at once |
| 4 | The recorded decisions, the UI inconsistencies, the small repairs — **DONE 2026-10-14** | `BACKLOG.md` holds no entry whose only blocker is an unanswered question |
| 4b | The "agent is driving" indicator — **DONE 2026-10-14** | somebody at the keyboard can see that the desktop is not theirs, and stop it, while gremote or the client is driving |
| 5 | The P200: measurements, and the optimisation they justify | the hardware questions are answered from hardware |
| 6 | A clean install, and publish | somebody who is not us installs it from the kit and it works |

---

## Stage 1 — fruit, the decisions stage 3 needs, and the harness

Four unrelated kinds of thing, grouped because each is small and each unblocks
something later.

**The fruit.**

- `get_clipboard` / `set_clipboard` (`PLAN.md`, Phase 6 tool table). Two tools
  and about thirty lines of `OpenClipboard`/`CF_TEXT`.
- Exit codes through `COMMAND.COM` (`BACKLOG.md` 17). The exit-code wrapper
  already closes this where it applies, and `ExecResult::viaShell` already
  travels on gremote's job lane -- both of which this file got wrong in its first
  draft. What is left is the two cases where the wrapper could not speak and the
  agent is not told: it was skipped (no fs lane, no `temp` header, a failed
  `put`), or no verdict came back.
- The `STYLESHEET` comment strip (`BACKLOG.md` 13, "free and separable") --
  **DONE in stage 1**; `WIRE_CSS = _without_comments(STYLESHEET)`
  (`bridge/view/html.py:631`) and `document()` defaults to it. **5,574 of 9,375
  bytes** are CSS comments, 59% of the stylesheet; the strip removes 5,595,
  because the blank lines the comments leave go too, taking the DOC body from
  9,557 to 3,962 (measured 2026-10-12) -- 58% of the frame. Two denominators,
  which is why both
  percentages appear and neither is wrong. `doc()` carries no `enc` header, so
  those bytes went out verbatim once per connect and again on every reconnect.
- The stale-doc sweep: `PROGRESS.md`'s "Current goal" still says Phase 5; R5 and
  R6 are labelled planned above text saying they shipped; `PLAN.md:1660` says the
  in-process collector has not landed and it has; `BACKLOG.md` 697 asserts Phase
  6 has not landed; `LAUNCH` and `MKDIR`/`DELETE` sit in "Explicitly cut" while
  shipping as tools. Phase 6 was never written up as a phase, which is how most
  of these went stale at once.

**The P200 bring-up is out of this stage**, by decision, and its measurements
were always stage 5's. Two of the preconditions this file listed turned out to be
closed already: the autostart `.REG` is generated (`tools/gen/mktools-iso.sh`),
shipped, and proven answering before anyone logs on
(`docs/measurements/gremote-vm-20260927.txt`), and first delivery was answered by
use on the Athlon. What is genuinely open is the Pentium 200 itself -- a
different machine from the Athlon, whatever the `*-p200-*.txt` filenames say.

One piece of preparation stays here because it is cheap and the box will want it:
`tools/gen/mkw98kit.sh` stages 356 KB onto a 1.44 MB floppy with no size check at
all, so `GREMOTE.EXE`, `GREMOTE.INI` and `GREMOTE.REG` join it along with the
budget the script lacks.

**The injection-safety loose end.** `UIAUTOMATION.md:645` says section D should
not be scheduled before gate 0.15d is re-run; section D shipped anyway. The probe
was fixed and **has** been re-run -- `docs/measurements/vm-20260926.txt`, with the
generation word, `distinct pids 2`, injection confirmed -- which three documents
still deny. Two caveats survive that run: it was 9.4 hours into an uptime rather
than from cold, and it was the session that ended with the 0.15h wedge. Foreign-
process GDI capture remains untested by the probe's own design. `BACKLOG.md` 15 is
the other half: an hour of the guest at 100% of a vCPU after those gates,
causation unestablished, standing rule attended-only.

This is shipped code that runs inside other processes' message dispatch, resting
on a measurement that does not support it. It needs a reboot, a health check at
the end of the run, and a known-good rollback. It is not fruit; it is here
because everything after it is safer once it is answered.

**The first end-to-end agent turn through the UI tier**
(`UIAUTOMATION.md:674`). The tier is built and measured on hardware, but only
probes and gremote have driven it. Ask the model to read a dialog and press a
named button, through the bridge. Cheap, and it is the first time the thing is
used the way it is meant to be used.

**The decisions that gate stage 3** -- the reason they are here:

1. **What export does.** `export_document` (`bridge/view/html.py:668`, called
   from `bridge/services/session.py:159`) builds a standalone file from
   `pages.all_text()`. Once the markup belongs to the client the bridge cannot
   build it at all. Three options, in `BACKLOG.md` 13 and 31: the guest renders
   its own export, the export links a `.css` written beside it (there is
   precedent -- IE5 has no `data:` URIs, so inline images are already real files
   beside `view.htm`), or the bridge keeps a deliberate second renderer and the
   two are allowed to drift. **Decided once, for both entries.**
2. **The vocabulary capability at the handshake.** 13 needs it for the class
   names and 31 for the block kinds. One `HELLO` key, a version integer, bridge
   refuses an unknown value. Decide the shape once; whichever entry lands first
   builds it.
3. **The transcript cache format** (`win/app/cache.cpp`). Blocks or markup, and
   the format bump either way, so pages written by an older renderer cannot come
   back looking wrong.

**And one decision that changes what stage 6 contains: is R7 in or out?**
Multiple clients is the largest unbuilt thing in the project
(`BACKLOG.md` 7, `PLAN.md:1959`). It blocks per-client model selection and the
`client_id`/`conv_id` split, it needs a second guest machine for `rebind`, and it
needs two answers of its own: which reading is meant (several guests, or several
windows on one guest), and an acceptance in writing that `client_id` is an
unauthenticated assertion. If publish means one person with one retro box, the
recommendation is to **cut it explicitly** and cut the two waiting items with it.
Either way it is decided here, because an answer of "in" is a stage of its own
between 3 and 4.

---

**Answered 2026-10-10**, and `docs/stage1-plan.md` is the detailed plan:

- **R7 is IN**, the larger reading: several guest machines against one bridge. It
  becomes its own stage between 3 and 4, and `client_id` as an unauthenticated
  assertion is accepted. Note while implementing: two guests already exist (the
  VM and the Athlon, `machine=TIBI`), so `rebind` needs no new hardware.
- **Export: the guest renders it.** One renderer, no drift. *Built in stage 3
  C3, and the second half of that sentence -- "`win/app/cache.cpp` already
  holds rendered pages" -- turned out to be the wrong mechanism: ranges are
  served on demand and the cache holds only what arrived, so an export
  assembled from it silently omits whatever the reader never paged to. It
  streams from the bridge instead. See `BACKLOG.md` 13.*
- **The vocabulary capability** is one `HELLO` key, a small integer version, with
  the bridge refusing an unknown value. *Built in C1, and it is four keys
  rather than one: `markup`, `blocks`, `toolmax`, `maxblock`. Packing them
  would have cost the ability to refuse one value and accept another.*
- **The cache keeps rendered markup**, not blocks, with a format bump so pages
  written by an older renderer are discarded. *Built in C2, and it keeps
  RECORDS rather than markup -- which is better, since a cached page
  re-renders to the current window width -- with the version in the FILENAME
  rather than a FORMAT file. Discarding the directory would have deleted the
  cached conversation on the first launch after an upgrade with no bridge
  reachable, before the notice saying why.*
- **0.15d: the reboot run plus the harness.** Foreign-process GDI capture stays
  deferred with its reason restated.
- **The P200 bring-up is out of this stage** by decision; the floppy kit gains
  `GREMOTE.EXE` anyway, since the kit has room and the box will want it.

Three corrections the stage-1 sweeps found, which this file got wrong above:
`ExecResult::viaShell` IS sent (on gremote's job lane) and the agent lane has an
exit-code wrapper that supersedes it; gate 0.15d HAS been re-run with the
generation word on the VM, though not from cold; and the autostart `.REG` exists,
ships and is proven pre-logon. `docs/stage1-plan.md` carries the citations.

## Stage 2 — size, before the thing that spends it

Measured 2026-10-11 at `01b5254`: `gaston.exe` **582,697 of 585,728** -- 3,031
bytes -- and `gremote.exe` 365,497 of 372,000. (This file said 580,649 and
363,449, figures taken before the clipboard landed.) **Done: gaston ends at
478,249 of 482,345 and gremote at 259,497 of 263,593.** Stage 3 adds a renderer
translation unit to the guest, and a new TU costs ~2,144 bytes of `-xs`
exception machinery before any code in it.

**What reading the tree changed about this stage.** Two of the three jobs below
turned out to be largely done, and the room stage 3 needs was somewhere nobody
had looked. `docs/stage2-plan.md` is the detailed plan and carries the
citations; the short version:

- **About 69 KB per exe is linked and unreachable**, found by attributing the
  link map per module (`tools/check/mapsize.py`): `uuid.lib`'s GUID table
  (31,520 bytes, 1,957 of 1,970 symbols unreferenced, pulled by one
  `IID_IUnknown` in gremote and thirteen GUIDs in the client), miniz's ZIP
  archive APIs (~31,400, with no `mz_zip_*` reference anywhere in `win/`), and
  `math387r.lib`'s float formatter (~6,250, reached by four `(double)` casts
  computing a bytes-per-second figure). Deleting those pays for stage 3 without
  trading any speed anywhere.
- **The flag decision** (`BACKLOG.md` 2) is answered on a measurement rather
  than on its own premise. "The whole client is built with `-otexan`" stopped
  being true across six override rules; what is left at `-otexan` is listed in
  that entry. The speed half is a timed run on the guest -- `probe deflate`, a
  timed `SCAN`/`MANIFEST` over a fixed corpus, and a `tree` call on a busy
  desktop -- and never under Wine.
- **The gremote trim** (`BACKLOG.md` 29) is three written answers and one
  measurement. Its first bullet was already fixed by `win/Makefile:698` before
  the entry was written; gremote links all ten `libautomation` members and
  `HELLO msaa=0` cannot change that (`msaa.o` is reached both by a by-value
  member and by the handshake itself) at a cost of 3,280 bytes; the `-xs` cost
  is ~16.9 KB paid once rather than per-TU. The working set is measured, as the
  entry asks.
- **R9 step C5+ was already done**, which is why it is no longer listed here:
  `win/app/main.cpp` holds no file-scope static data and both callbacks the step
  named were deleted in `bdd3c43` and `59a1e65`. Recorded at `REMOTE.md`'s
  C-step list. The module state that remains belongs to the libwire/libexec
  extraction, not to this stage.
- **The caps come down** rather than up, for the first time in the file's
  history -- which reverses `win/Makefile:88-101`'s "the page is bought once"
  policy deliberately, and makes the `COMMITTED_CAP` +4,096 deferred below
  unnecessary rather than overruled.

---

## Stage 3 — the markup moves to the client — DONE 2026-10-12

**`docs/stage3-plan.md` is the plan and `docs/PROGRESS.md` is the outcome.**
Twenty-three commits. The exit condition is a test now, not a sentence:
`tests/python/meta/test_no_markup_on_the_bridge.py` walks every file under
`bridge/` for a markup literal and allows exactly one, the oracle.

What it did, in one paragraph each.

**The wire carries records.** `<kind> [attr=value]*` then tagged payload
lines then `.`, in the frame BODY -- frame headers did not change. The bridge
decides what a block IS and the client decides what it looks like; a client
writing to a teletype consumes the vocabulary without discarding or parsing
anything, which is the test `kind=pre` fails and `kind=code` passes. Inline
runs are a stack of marks, not three bits of state, because `code` is
specified with the `font:` shorthand and so the nesting order is visible,
because nesting reaches depth three, and because `**a****b**` is two spans a
flat run list cannot tell from one.

**The appearance is three files beside the exe.** `view.css`, `export.css`
and `view.tpl`, CRLF-converted by the kit. Changing how a block looks is an
edit and a restart rather than a rebuild. A missing or malformed `view.tpl`
degrades to escaped paragraphs and says so in the status bar and the log --
never in the transcript, which in that state is indistinguishable from model
prose.

**Three MSHTML-shaped numbers moved.** The page cap and the block cap are the
client's, declared at the handshake; the wrap column is taken from the window
at render time and is not declared at all, because the bridge does not wrap
and a column sent once would be wrong after the first resize. The
tool-result cap stayed, because truncating on the client means the bytes
already crossed a 5 KB/s link -- bandwidth, not appearance.

**DOC is gone.** 9,557 bytes uncompressed, once per connect and again on
every reconnect, on a link measured in single digits of KB/s.

### The numbers

| | before | after |
|---|---|---|
| `gaston.exe` committed | 478,249 | 495,865 of 498,425 |
| live DOM write, Athlon | 1,064 µs | 986 µs |
| live render, Athlon | — | 142 µs (14.4% of the DOM write) |
| live DOM write, 32MB VM | 3,799 µs | 3,710 µs |
| live render, 32MB VM | — | 478 µs (12.9%) |
| DOC per connect | 9,557 bytes | 0 |

One page of image, measured and attributed in `win/Makefile`'s ladder. Half
of what the stage adds is not image at all: 9,375 bytes of stylesheet and
~5,400 of templates are files. `docs/measurements/live-before-20261012.txt`
and `live-after-20261012.txt` have the timings;
`template-roundtrip-20261012.txt` has the one platform risk that had to be
measured rather than reasoned about.

### What is worth knowing afterwards

- **The equality proof is the load-bearing test.** `rendercheck --render`
  against the bridge's emitter, byte for byte, ~75 blocks including the live
  path. It found two real bugs before it was even a test -- text-mode stdout
  turning every `\n` into `\r\n`, and a newline inside a payload line, which
  is a wire-format violation no amount of reading either side would show.
  The bridge's emitter stays until 3b for exactly this reason.
- **The screenshot caught what no test could.** Every test passed while the
  guest rendered the whole transcript as plain paragraphs, because nothing
  called `rc_tpl_load` -- both test paths pass `--template` and the client
  never did.
- **A bug the rewrite exposed and did not introduce**: a block that forced a
  page seal was never streamed to a reader following LIVE. Survivable only
  while a run could be split; 02.17 made splitting rare and so made the hole
  the common case.
- **One property is lost.** A CSS edit used to land on a RECONNECT, because
  `set_document` fingerprinted the incoming DOC. It lands on the next START
  now. The right trade, and a real regression against `PROGRESS.md:955`.
- **Two of §8's three throttle thresholds are the same measurement with
  inconsistent limits**, which the after-measurement records. Nothing is
  throttled; the thresholds want rewriting before stage 5 uses them.

---

## Stage 3b — pagination: the bridge stops dividing the transcript — DONE 2026-10-13

Planned in `docs/stage3b-plan.md`, which was replanned mid-stage after an
adversarial review round found 27 defects in its first draft, three of which
killed a decision. Ten commits, `786a911..94e1835`.

**The exit condition, which is now what the code does:** the guest asks for
blocks by range, and `pages.py` is an append-only transcript.

| | before | after |
|---|---|---|
| `bridge/view/pages.py` | 272 lines | **71** |
| `bridge/view/htmlrender.py` | 410 lines | **gone** — the last markup on the bridge |
| page frames | `GETPAGE`, `PAGESTART`, `PAGEEND`, `PAGESEALED` | `GETBLOCKS`, `RANGE`, `RANGEEND`, `GREW` |
| `TEXT` headers | `mode`, `enc`, `page` (+ dead `total`/`off`) | `mode`, `enc`, `total` |
| HELLO keys | 39 | **39** — the bridge needed none |
| `gaston.exe` | 495,865 | 496,377 of 498,425 |
| `PROTO_VERSION` | 2 | 3 |

**What the bridge knows about a page now: nothing.** The client computes how
many records it can hold from its own memory and its own Small/Normal/Large
setting (`win/net/net.cpp` `rc_page_blocks`, which is `limits_for` and
`prefs.scaled` moved across), puts that number in every `GETBLOCKS` as
`limit`, and `offset=live` aligns to the grid that limit implies. A first
draft of the stage declared the number as a HELLO key so the bridge could
push a window after a conversation switch; removing it and making the guest
ask is what makes the exit condition literally true.

### What is worth knowing afterwards

- **A PREFS key can stop being read. It can never be removed.** `prefs.py`'s
  key chain ends in `raise PrefsError`, and on the HELLO path that is fatal.
  The client sends `view.page` unconditionally, so deleting the branch would
  have refused every connection from any guest not reflashed in lockstep --
  permanently, for a setting the bridge no longer uses. Found by review,
  before it shipped.
- **The guest's own echo of the user's message was a second source**, and
  paging back on the real machine is what showed it: the echo landed in the
  window being read, where the record does not belong and where the bridge
  would never put it. The bridge streams the user turn now, the guest draws
  nothing, and audit 01.29 -- the two escapers that disagreed about a
  trailing blank line -- closes with it.
- **The window delivery races block appends**, and the first lock was in the
  wrong place: reading the records and setting the live flag outside it left
  a gap where a block was absent from the window being built and streamed as
  live, and `RANGE` begins with a `clear()`.
- **`rendercheck` was never in `guest-checks`** for the whole of stage 3 --
  the stage whose renderer those 69 tests are the only proof of. It built on
  any machine that had run `make -C win all`, so nobody saw it.
- **The equality proof is a checked-in fixture now**
  (`tests/goldens/render/guest_render.json`, 46 cases, 71,943 bytes),
  generated from the oracle's last passing run. A fixture is weaker than a
  proof and the file says so: it pins the renderer to what it did, and there
  is no second renderer left to disagree with it.

---

## Stage 3c — several guest machines against one bridge — DONE 2026-10-13

R7. Planned in `docs/stage3c-plan.md`; `BACKLOG.md` §7 is the decision record
and §21 the question it answered. Six commits, `7c81fe5..342f4cf`.

**Exit condition, met on the real machines:** two guests hold two mirrors and
two baselines at once. The QEMU guest and the Athlon, attached to one bridge
at 01:35:56, in two conversations, with two mirrors and two baselines --
`docs/measurements/two-guests-20261013.txt`.

**What it was.** `ConnectionService.guest` was a single slot. A second
*simultaneous* client was refused process-wide, and a second *sequential* one
silently inherited the first machine's session -- `is_reconnect` asked whether
a GuestSession existed, never whose it was, so attach left the live
conversation, work folder, grants, permission desk and transcript belonging to
the previous machine. Both of this project's machines talk to one bridge, so
that was reachable rather than theoretical.

| what was shared | now |
|---|---|
| one `GuestSession` slot | a dict keyed by machine id |
| one baseline per folder path | one per (machine, folder) |
| one `SyncCoordinator` for every guest | one each; the first adopts the pre-attach queue |
| one codepage binding per task | bound per guest in both background loops |
| `recent_prompts`, cleared nowhere | cleared with the chat |

**Two decisions, both of which the backlog asked for in writing.** The machine
id and the sync baseline's identity stay separate -- §21's open question,
answered: `machine` says *who* and `tree_id` exists to refuse a swapped or
reformatted disk, and unifying them would mean deciding which of the two
behaviours the one id has. And `machine` as a reconnect identity is an
**unauthenticated assertion, accepted as one** (audit 04.21): there is one
shared secret per bridge, compared against a single `GASTON_SECRET`, so a
client claiming another's id inherits its transcript exactly as before.

### What is worth knowing afterwards

- **Nothing on the bridge resolves a guest implicitly any more.** Six
  production paths picked one out of process state when a caller passed none.
  `agent/tools.py` had described that failure since it was written: the
  command runs on whichever machine is attached rather than the one whose
  conversation asked, and the permission prompt appears on the right person's
  screen -- so it looks approved, because it WAS approved, for something else.
- **`connection.guest` survives as test scaffolding**, which is a narrowing of
  the plan said out loud: what makes two machines unsafe is production
  resolving a guest implicitly, not a test reading one.
- **Eviction is new behaviour on a path that deliberately had none.**
  `reap_idle` kept a session so a reconnect reattached it, which was free with
  one slot and is one retained SDK subprocess per machine id ever seen with a
  dict.
- **The real machines found the last stage's bug too**: the VM was still
  running a pre-3b client and was refused with `bad_version (bridge speaks 3,
  guest speaks 2)` -- stage 3b's version bump doing its job rather than
  letting a guest connect and silently fail to page.

---

## Stage 4 — the recorded decisions, the inconsistencies, the small repairs — DONE 2026-10-14

Planned in `docs/stage4-plan.md` through two adversarial review rounds (40
defects, then 37). Eighteen commits, `92f49cb..`.

**Exit condition, met and VERIFIED BY SWEEPING rather than asserted:**
`BACKLOG.md` holds no entry whose only blocker is an unanswered question.
Nine rulings, six confirmations, four named watch conditions.

**The stage's own text was the first thing it had to correct.** It claimed
seven blocked entries; a sweep found more, and five of the seven were
described wrongly. The `rc_strf` "~97 sites" was counted before the tree
quadrupled (472 of 527 discard the return, and eight of them can bite). The
automation setting had not needed a restart since the Options sheet landed.
§18's residue was three stale comments. `enc=rle8` rested on a figure with
no measurement file behind it -- the nearest real number is a *discarded*
one with the sign flipped -- while two other documents had already closed
it as a no.

**What running it found that reading it did not.** Four real defects, none
of them visible to lint, test-fast, wine-smoke or either review round:

- The cache sweep shipped with `"%s\%s"` -- one backslash, an invalid
  escape, so the glob had no path separator and matched nothing. Found by
  planting an orphan on the guest. `check-sources` now rejects it.
- The Add-a-tool dialog clipped its own OK and Cancel: 128 DLU tall with
  buttons ending at 133, wrong at every font size. Found by the Large Fonts
  measurement BACKLOG 30 had been asking for.
- `SyncStatus.BAD_ID` -- a new guest status broke the cross-language
  vocabulary agreement. Found by wine-smoke.
- `policy.TOOL_CATS` was missing `ocr`, so a mode with the screen Off could
  not have hidden it. Found by a test whose docstring says exactly why it
  exists.

**Three measurements, and two overturned something.**

- **The logoff bit** (`REMOTE.md` item 12), pulled out of stage 5 because it
  needed one logoff on any 98 box. `RegisterServiceProcess` does NOT exempt
  a process from the logoff broadcast, so the arm was needed rather than
  dead code -- and `RunServices` runs at BOOT, not per session, so gremote
  did not come back at the next logon either. One logoff cost the machine
  its remote access until a reboot, and `BACKLOG.md` 35 says the opposite in
  passing. gremote outlives a logoff now, and picks up the next session's
  taskbar as the same process.
- **04.35's folder commit.** The mechanism survives and the harm does not:
  `permdesk` refuses a write outside the working copy, so the agent re-aimed
  in three seconds at zero exec cost. The cost MOVED -- four of seven execs
  were toolchain orientation, against a baseline of zero.
- **Large Fonts.** The 230x170 DLU figure stands.

**And `ocr(rect)` is built**, third in the ladder and saying so in its own
description: on the guest it read "Let ityse this scien" where `ui` returns
"Let it use this screen" exactly.

**The image cap went up one page**, 498,425 -> 506,617, and the Makefile
says honestly that 1,400 bytes of content bought 4,096 of padding because
`_DATA` ended 192 bytes short of a boundary.

### What is worth knowing afterwards

- **A stale backlog is worse than a short one.** Six entries read as open
  while carrying their own answers -- a title still saying "needs a
  decision" a month after the work shipped, a "Three remain:" above three
  struck bullets, a `from=` bullet describing a replay buffer deleted in
  September. Most of this stage was reading, not writing.
- **Three of the four code defects were in code written during this stage**,
  and all three were caught by running the thing on the real machine rather
  than by reviewing it. The fourth was caught by a test that existed because
  somebody had been bitten before.
- **Writing C through a script collapses escapes.** `"\\"` in a generator
  is `"\"` in the file, and one level too many is invisible in a diff
  because the line still reads as a path join.
- **The last guest check took five wrong runs, and every one was the
  harness.** `force-kill-reclaims-the-dos-vm` finally passed with `vm=1`,
  `kill=0`, `vm-gone=1`, `vms-on-box=0` on a freshly booted box -- the
  reclaim works. What stood in the way: running execcheck from a DOS prompt
  (where its own parent is a VM and a console it opens reuses that one, so
  nothing is ever a child -- `--detached-to` exists for this and said so),
  and then launching COMMAND.COM to force the issue (a 16-bit program,
  denied by a different rule, with a pid `launch` does not usefully
  return). The sibling case had been passing VACUOUSLY on the guest since
  it was written, for the first reason.
  `docs/measurements/forcekill-vm-20261014.txt`.
- **Every bullet of the plan's Verification section was executed in the
  end, and the last one took the longest to stage.** The automation tick's
  honesty when the mid-turn prompt is declined needs a toggle to land while
  a turn is streaming -- Haiku finishes a long reply faster than a menu
  click, so the only reliable hold is a permission bar in Ask-first mode,
  raised by a command that CHANGES something. Held that way it passes:
  "Saved. They take effect at the next connection", the turn still
  streaming, and no tick on a setting `m_cfg` has already stored.
  `docs/measurements/tick-honest-vm-20261014.txt`.
- **The on-guest work found a live defect that predates the stage.** The
  client reconnects about once a second and clears file sharing each time,
  which makes it useless to the agent until restarted -- `BACKLOG.md` 38,
  reproduced and localised to `Reconnector::tryConnect` running on a healthy
  link. Stage 4's menu item provokes it reliably but does NOT cause it: a
  freshly started client that was never toggled loops identically. Not
  fixed here; the diagnosis is written down and the fix wants its own
  change.
- **And `BACKLOG.md` 17's "degrading later runs" got demonstrated by
  accident.** The run before the reboot carried three VMs leaked by those
  experiments; five unrelated cases failed with them present and all thirty
  passed once the box was clean. That is the claim measured rather than
  asserted, and the argument for the reclaim in one line.

---

The stage where `BACKLOG.md` stops holding entries whose only blocker is a
question. Not a list to be worked top to bottom; a sitting with the file open.
Planned in `docs/stage4-plan.md`, through two adversarial review rounds.

**This section was wrong about its own stage, and the plan says how.** The
count, the sizes and the ownership were all taken on trust from figures a year
of commits had moved. Corrected here because the stage's exit condition is a
claim about this file's accuracy:

- **Ten entries hold a live question, not seven.** §4's `SYNCDEL`, §6, §8,
  §11a, §11b, §12, §17, §19, §30, §35. Five more read as open and are not:
  §3 (there is no replay buffer at all -- `protocol.py:115` enforces
  never-replay by never replaying), §7, §9, §23 (answered in its own title),
  §32.
- **The `rc_strf` sweep is not a sweep.** "~97 sites" was counted 2026-09-18;
  re-measured, 472 of 527 sites across `rc_strf`, `rc_strcpy` and
  `rc_strncpy` discard the return, which is the function set the superseded
  figure counted. Eight of them can bite. The rest stay, and a convention
  in `str.h` says why.
- **The automation setting does not need a restart.** `MainWindow::optionsApply`
  already re-applies it and already lists it among its reconnect triggers. Only
  the menu item is missing.
- **§18's residue is nearly closed** by the 2026-10-04 policy rework: `auto` is
  the bridge's own table and asks the CLI for nothing. What is left is three
  stale comments.
- **§30 has seven leftovers, not six.**

The decisions, as they were settled 2026-10-13:

- **`enc=rle8`: no, ratified.** `UIAUTOMATION.md:285` had already written the
  conclusion from a real four-band run -- deflate beat GDI's own RLE at every
  depth where both exist. The "2.8x faster and 2x larger" this section carried
  has no measurement file behind it, and its likely source is a *discarded*
  2.7x single-band number at `win/checks/probe.c:1658`, sign flipped. Two of
  four repo documents already said no; now all four do.
- ~~**`COMMITTED_CAP` +4,096**~~ **Closed 2026-10-11 without being taken.**
  Stage 2 freed 31,520 bytes of CONST against the 126 needed.
- **`KILL` and a stranded console VM** (`BACKLOG.md` 17): kill the Win32
  descendants and let a console program end itself, extending the `dos_session`
  refusal already beside it. One edit in libexec, which reaches both programs.
- **The `rsrc32.dll` question** (`REMOTE.md` P200 item 9): **no**, and not for
  the hardware reason. `SYSINFO` runs inline on gremote's control lane, whose
  one rule is kernel32 and Winsock only, because a hung 16-bit app holding
  Win16Mutex blocks USER and GDI from every 32-bit thread. No install clears
  that. `PLAN.md:653` has the alternative.
- **Acting on the logoff bit** (item 12): **measured in this stage**, not
  deferred to stage 5. One logoff on the QEMU guest answers it, and
  `GREMOTE.PRV` already prints which kind of session end arrived.
- **`ocr(rect)`: in scope**, built in this stage. Tesseract is a system binary
  rather than a dependency, degrading the way a missing Pillow already does.
- **The "agent is driving" indicator** (`BACKLOG.md` 19) **left this stage.**
  gremote drives the desktop independently -- `BACKLOG.md:2488` records "two
  programs can drive one desktop" as accepted -- so the indicator belongs in
  the shared seam both programs enter, needs its own pump thread, breaks at
  `clickAt`'s occlusion check rather than at `isOurs`, and has to mean *stop*
  rather than "abort this step list". That is a stage: **4b**, below.

The repairs, same stage: `BACKLOG.md` 30's seven leftovers from the Options
work, 18's three stale comments, 11a's transcript cache that prunes nothing,
the eight `rc_strf` id sites, §23's missing `ConnectionService::signalStop()`,
and three strings still telling the user to use a menu item that was deleted.

---

## Stage 4b — the "agent is driving" indicator — DONE 2026-10-14

Split out of stage 4 on 2026-10-13, by review rather than by size: the design
in that stage would not have worked. **Planned and built 2026-10-14**
(`docs/stage4b-plan.md`), after three more review rounds against the tree --
one on the sketch, two on the plan -- which between them falsified six claims
and then eleven more.

**What shipped.** A topmost strip, bottom right above the taskbar, saying
"Gaston is controlling the desktop" (gremote says "Gaston Remote") with two
ways to stop it: a Stop button and a global chord, Ctrl+Alt+Shift+F12 in the
client and F11 in gremote. It is raised by `rc_ui_run` around the step runner
and nowhere else, steps aside for a click underneath it, hides itself out of
every screenshot, and refuses its own chord to anything the agent synthesises.
Stop cancels the run and does what that program's own Stop does -- an
INTERRUPT frame in the client, the tray's "automation off" in gremote --
because a cancel alone is undone by the next request a second later.

Measured on the guest: `docs/measurements/stripprobe-vm-20261014.txt` (what 98
SE does with somebody else's topmost window) and
`docs/measurements/indicator-vm-20261014.txt` (the thing itself, both
programs, both cancel routes). Cost: `COMMITTED_CAP` 506,617 -> 513,273 and
`check-gremote` 263,593 -> 270,249, with no `_BSS` page slide on either.
Gates at the close: `make -C win check` six caps, `make lint` clean,
`make test-fast` 4,309, `make wine-smoke` 464, `pytest tests/python/remote`
122.

What the review established before any of it was written, so none of it is
re-derived:

- **It cannot live in the client.** `RUN` goes through `rc_ui_run`
  (`win/ui/uiengine.cpp:795`), compiled into `gaston`, `gremote` and `uicheck`.
  An indicator in the client is absent when gremote drives, and worse in the
  mixed case: gremote's `isOurs` compares its own pid and its tree walk excludes
  its own pid, so the client's window becomes a node in gremote's `TREE` and a
  click-blocker it cannot hide.
- **It cannot be a bar row.** `PermissionBar::push`/`pop` are private, the only
  public raise is `frame()` over six wire types, and a drive runs on the desktop
  worker while the bar runs on the window thread.
- **It needs its own thread and pump.** A posted cross-thread `ShowWindow` is
  not synchronous; a `SendMessage` blocks against `joinOrLeak(m_ui, *this,
  5000)`, which waits without pumping. The worker does not pump during a step,
  so a button or a hotkey hosted there would be dead for up to `stepMs`.
- **`isOurs` is not the chokepoint.** Five of its eight sites guard against
  driving our own UI on handles the indicator can never hold. The break is
  `clickAt`'s occlusion check: `WindowFromPoint` returns the strip, the settle
  loop polls for it to clear, and a window raised by `SetForegroundWindow` never
  rises above a topmost one -- so every click under it stalls with
  `INPUT_OCCLUDED`.
- **`HTTRANSPARENT` is not the answer**, which is the correction to the first
  version of this list. The `WM_NCHITTEST` walk is documented thread-scoped --
  "the underlying windows in the same thread" -- and every window the strip
  covers is in another process. So the strip **steps aside** for the duration of
  a step instead, one bracket per action step in `StepRunner`, and
  `HTTRANSPARENT` is at most an optimisation the guest probe looks for.
- **Not `WS_EX_LAYERED` or `WS_EX_NOACTIVATE`.** Style bits are a silent no-op
  on 9x and a compile error at `WINVER=0x0400`; the load-time kill is
  `SetLayeredWindowAttributes`, the export. `SW_SHOWNOACTIVATE` and
  `MA_NOACTIVATE` are the mechanism -- and **never activating is load-bearing**,
  not politeness: `steps.cpp:464` fails every typing step with `refused_self`
  when the foreground is ours, so a strip that can take the foreground breaks
  the feature it belongs to. That, with no child control to hold focus, is the
  answer to "transparent to the keyboard".
- **Injected keys fire `RegisterHotKey` hotkeys**, so the cancel chord needs a
  denylist where keys are synthesised. The live hole is `tap key=f12 mods=7`:
  `tapKey` discards `keyDown`'s return (`input.cpp:806-820`), so a guard in
  `keyDown` alone would report `ok` having typed nothing. `typeText` and
  `pressKey` have no production caller at all.
- **Stop must mean stop**, and what that means was decided by the user on
  2026-10-14: cancel the run and do what the UI's Stop button does. In the
  client that is an `INTERRUPT` frame, which ends the turn. It is **not enough
  on its own**: `bridge/services/coordinator.py` kills every pending `exec` on
  INTERRUPT and nothing pending on the ui lane, and
  `test_server_integration.py:2190-2197` records why that loop exists -- the SDK
  does not cancel an in-flight tool call, measured on the guest. A second
  `ui(run=...)` batched into the same message would drive a second after the
  Stop, so the bridge gains the same loop for the ui lane. gremote has no turn
  and no Stop button; there the equivalent gesture is the tray's own, so Stop
  also sets the automation level to 0.
- **It is owned by the App in both programs, not by `UiContext` and not by
  `UiOps`.** `UiContext`'s constructor runs on the main thread in the client,
  and `UiOps` is a member of `Session`, made fresh per TCP connection -- which
  would mean a new thread, a repeat class registration and a fresh hotkey on
  every reconnect, and a leaked session holding the chord for the life of the
  process. Everything in `libautomation` reaches it through an abstract
  `DriveOverlay`, so `indicator.o` stays out of `uicheck`, `autocheck` and the
  probes; and the indicator never names the wire, because `libautomation` may
  not.
- **Two caps move**, `COMMITTED_CAP` and `check-gremote`'s, and three sections
  are charged, not one: gaston has 150 bytes before `AUTO`'s next 512-byte step
  and 146 in `.idata`, gremote 330 and 129 -- and gremote needs about thirteen
  imports it does not have today. A single `_BSS` page slide exceeds both caps'
  slack on its own, so the first commit after the plan is a shape-final trial
  link that can end the stage on cost.

### What is worth knowing afterwards

- **The documentation was right and the guest confirmed it, which is the
  cheapest outcome a probe can have.** `HTTRANSPARENT` buys nothing across a
  process boundary on 98 SE -- `WindowFromPoint` returns the strip through it
  even when the strip is on another thread of the SAME process, the case the
  documentation says is covered. The plan budgeted the step-aside as the
  design rather than as a fallback, so the measurement cost three lines of
  deletion instead of a redesign.
- **`SW_SHOWNOACTIVATE` holds on 98 SE**, measured: zero activations, never
  foreground, after being clicked, while topmost. That is load-bearing rather
  than polite -- `keyboardTargetOk` fails EVERY typing step with
  `refused_self` while one of our own windows is in front, so a strip that
  could be activated would break the automation it exists to announce.
- **The bug the guest found was in the first drive, and only the first.**
  `driveStarting` starts the indicator's thread and posts it a wake, and on
  the first drive that wake goes to an HWND that does not exist yet -- the
  thread creating the window is 440ms behind on a 32MB machine. The clock
  never started and the strip stayed hidden through the whole run. Every test
  that drove twice passed, including the Wine one written for this feature.
- **Stop needed a bridge-side commit nobody had planned.** `on_interrupt`
  killed every pending `exec` and never touched the ui lane, and the SDK does
  not cancel an in-flight tool call (`test_stop_kills_a_running_command`
  measured that on the guest in September). So a step list kept driving after
  Stop, and a second `ui(run=...)` batched into the same assistant message
  would have taken the lane and driven on anyway.
- **A denylist for a chord belongs where the chord can be TYPED**, which is
  `tapKey` and not `keyDown`: `tap key=f12 mods=7` is one step with composed
  modifiers, and `tapKey` discards `keyDown`'s return -- a guard there alone
  would have refused the keystroke and reported `ok`.
- **An agreement test written for one status found a gap in another.** Every
  status `inputStatus` can send must have a sentence in `tools.toml`;
  `failed` had none, and `failed` is where `[Ctrl+Alt+Delete]`'s refusal
  lands, so an agent that tried it was told nothing at all.
- **Three capture paths, not one.** `Screenshot` is constructed per call in
  both programs, and the client has a second desktop capture nobody had
  counted -- Attach > Screenshot -- whose image goes to the agent exactly as
  a `shot` does, and which a person reaches for BECAUSE something is
  happening on screen.
- **BACKLOG 38 is still there and is now easy to reproduce**: it ran
  underneath the client's own guest test, a full HELLO-and-attach once per
  second, and turned a cancelled run into "the machine disconnected".
- **The strip scales with the DPI and not with the screen, measured in all
  four corners: 352x25 at 96 dpi, 426x28 at 120, and the same 426 at 120 on
  a 640x480 screen with 207 pixels to spare.** Display Properties itself
  does not fit in that last corner -- its own buttons are below the bottom
  edge -- which is the measure of how tight it is. The width is taken with
  `GetTextExtentPoint32A` rather than laid out, which is the whole reason it
  survives a font size nobody designed it against. And there is a cheaper way through a 9x font
  change than stage 4 recorded: answer NO to the restart offer and shut down
  from the Start menu, because a domain destroyed at "Windows is shutting
  down" boots normally where one destroyed at "Windows is now restarting"
  comes back in Safe mode.
- **Finishing the verification found a defect the stage had called a known
  gap.** Two programs driving put both strips in the same corner and the
  wider one covered the other, so the screen named one program while two
  drove -- and the Stop a person could see belonged to the other. "Two
  strips can be up" had been written down as accepted; what was actually
  happening was the strip lying about who was driving. Fixed by stacking.
  The lesson is the one stage 4 kept learning: a gap recorded from a reading
  is not the same as a gap seen.

---

## Stage 5 — the P200, and the optimisation it justifies

**Resolved, and it stayed in stage 1:** the clipboard run on the Athlon ran
on 2026-10-11 once the box was powered on
(`docs/measurements/clipboard-athlon-20261011.txt`). Keeping it was right --
it found that `[Ctrl+A]` selects all in the text editor there and nothing at
all in the guest's, which the prose had been stating as a property of the
platform.

The bring-up happened in stage 1, so this is the measurements. ~~Eight~~
**Six** of the twelve `REMOTE.md` P200-checklist bullets are unanswered --
stage 4 took two of them, and the count was out by one before that:

`bind` after a restart in `TIME_WAIT`; the tray icon end to end through a real
logon cycle, which is the one part of `win/remote/tray.cpp` with no test behind
it; gremote's memory idle and under load, with and without the client;
shutdown with gremote running, where the hang was seen on
this machine and only this machine can confirm the fix; shutdown with the client
running; and prevent-sleep runs 2 and 3, which need APM the QEMU guest does not
have.

**Taken in stage 4, both because neither actually needed the P200:**
item 12, whether gremote survives a LOGOFF, which needed one logoff on any
real 98 box -- it did not survive, and `RunServices` runs at boot rather
than per session, so it did not come back at the next logon either
(`docs/measurements/logoff-vm-20261014.txt`); and item 9, the `rsrc32.dll`
question, settled as a no on the control lane's kernel32-and-Winsock rule,
which no install can clear.

Plus gate 0.5c (deflate throughput, still provisional), the six measurements
`UIAUTOMATION.md` says only the P200 can answer, `BACKLOG.md` 26 -- which closes
when the P200 says so and not before -- and 22's `-verify 4000` window, never
measured against a cold start off a 1998 platter.

The optimisation that belongs here rather than in stage 2 is the part that needs
the slow machine to judge: whether `-ot` earns its bytes in the modules it was
kept for, and whether the guest re-reading its whole tree every sync pass costs
anything real (`BACKLOG.md` 4, "measure the real cost on the box before building
anything").

---

## Stage 6 — a clean install, and publish

**The gate: a from-scratch install on a clean Windows 98 install, not the dev
VM.** Nobody has ever installed this as a new user. The ISO and the floppy kit
are the real install path, `SETUP.BAT` and `GREMOTE.INI`'s fresh-install write
have only ever run on a machine that already had a copy, and `GREMOTE.INI` versus
`gremote.ini` on FAT is the kind of thing that only bites a stranger.

Everything else here is small, and it is small because the project has been
written down as it went:

- A README that addresses somebody who has not read `PLAN.md`.
- `LICENSE` as a file. MIT is already asserted in the VERSIONINFO block and
  nowhere else.
- The security posture stated rather than inherited: a plaintext shared secret on
  a trusted LAN segment, the listener bound to one interface, and what the secret
  grants when presented (approving permission prompts and feeding fabricated tool
  results to the agent, both at once).
- Whatever stage 1's sweep did not reach, and the Phase 6 write-up that never
  happened.
