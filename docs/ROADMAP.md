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

Six stages. They are the five that were asked for, with one change: the
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
| 3b | Pagination: the bridge stops dividing the transcript | the guest asks for blocks by range, and `pages.py` is an append-only transcript |
| 3c | R7: several guest machines against one bridge | two guests hold two mirrors and two baselines at once |
| 4 | The recorded decisions, the UI inconsistencies, the small repairs | `BACKLOG.md` holds no entry whose only blocker is an unanswered question |
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

## Stage 3b — pagination: the bridge stops dividing the transcript

Everything `pages.py` does exists to cut a stream of HTML into MSHTML-sized
chunks at boundaries safe for MSHTML. After stage 3 every boundary is safe --
a record is indivisible -- and the client picks the size, so nearly all of it
is deletion: `Page`, `_seal`, `continued` (already always false), `MAX_BYTES`,
`MAX_LINES`, `HARD_*`, the three `over_*` predicates, `message_boundary`,
`limits_for`, `PAGESEALED`, the four page frames, `pagebar.cpp`'s arithmetic
and the anchors. `pages.py` is 272 lines today and should end around 90 as an
append-only `Transcript`.

What replaces it is the thing stage 3 made possible and deliberately did not
build: **the guest asks for blocks by range.** `offset` and `limit`, nothing
else -- the server has no opinion about what a page is, which is the whole
point of the client declaring its own caps.

Two things stage 3 was forced into that belong here:

- `link.py` keeps a chunk loop for the export and nothing else.
- The bridge's emitter (`bridge/view/htmlrender.py`) and the `HtmlStream`
  shim die here, with the allowlist entry in
  `test_no_markup_on_the_bridge.py` and the two tests that guard it. The
  equality proof becomes a checked-in fixture at the same moment, which is
  what keeps the guest's renderer pinned once the thing it was compared
  against is gone.

---

## Stage 4 — the recorded decisions, the inconsistencies, the small repairs

The stage where `BACKLOG.md` stops holding entries whose only blocker is a
question. Not a list to be worked top to bottom; a sitting with the file open.

The decisions, as they stand today:

- **`enc=rle8`** (`BACKLOG.md` 8, `REMOTE.md`'s one live open decision). The
  measurement is in -- 2.8x faster and 2x larger than deflate -- and Phase 6 has
  landed, so this is now a value in the chooser or a written-down no. If yes, it
  is about an hour; it could be pulled into stage 1.
- **The "agent is driving" indicator** (`BACKLOG.md` 19). Deferred as a taste
  call: a fourth status pane, a loud transient bar, or both. Turning the
  automation setting on and off from the menu is the same entry, and today it
  needs a restart because the INI is read once.
- ~~**`COMMITTED_CAP` +4,096** so `gaston.exe` can hold the guest half of the
  automation gate.~~ **Closed 2026-10-11 without being taken.** `BACKLOG.md` 19
  offered freeing 126 bytes of CONST or DATA as the alternative; stage 2 freed
  31,520 of CONST alone, and the cap came down rather than up.
- **What `KILL` means for a stranded console VM** (`BACKLOG.md` 17).
- **The `rsrc32.dll` question** (`REMOTE.md` P200 checklist, item 9): whether
  `SYSINFO` should report USER/GDI free resources at all, which needs Resource
  Meter installed on the box first.
- **Acting on the logoff bit** (`REMOTE.md` P200 checklist, item 12), which wants
  its measurement from stage 5 first.
- **Whether `ocr(rect)` is in scope.** It is unblocked and unbuilt, and it drags
  Tesseract onto the bridge as a dependency. Decide rather than assume.

The repairs, same stage: `BACKLOG.md` 30's six leftovers from the Options work,
the Mode menu offering modes the model cannot enter (18's residue, blocked
upstream), 11a's transcript cache that prunes nothing, and the ~97 `rc_strf`
sites that ignore a truncation return.

---

## Stage 5 — the P200, and the optimisation it justifies

**Resolved, and it stayed in stage 1:** the clipboard run on the Athlon ran
on 2026-10-11 once the box was powered on
(`docs/measurements/clipboard-athlon-20261011.txt`). Keeping it was right --
it found that `[Ctrl+A]` selects all in the text editor there and nothing at
all in the guest's, which the prose had been stating as a property of the
platform.

The bring-up happened in stage 1, so this is the measurements. Eight of the
twelve `REMOTE.md` P200-checklist bullets are unanswered:

`bind` after a restart in `TIME_WAIT`; the tray icon end to end through a real
logon cycle, which is the one part of `win/remote/tray.cpp` with no test behind
it; gremote's memory idle and under load, with and without the client; the
`rsrc32.dll` question; shutdown with gremote running, where the hang was seen on
this machine and only this machine can confirm the fix; shutdown with the client
running; whether gremote survives a LOGOFF as `RegisterServiceProcess` promises;
and prevent-sleep runs 2 and 3, which need APM the QEMU guest does not have.

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
