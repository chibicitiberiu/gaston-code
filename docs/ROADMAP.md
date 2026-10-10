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
| 1 | Fruit, bring-up and the decisions that gate stage 3 | the P200 answers on the LAN; nothing in stage 3 is waiting on an answer |
| 2 | Size: the flag decision and the gremote trim | a measured before/after, and room in the caps for stage 3 |
| 3 | The markup moves to the client | a second client could be written without touching the bridge |
| 4 | The recorded decisions, the UI inconsistencies, the small repairs | `BACKLOG.md` holds no entry whose only blocker is an unanswered question |
| 5 | The P200: measurements, and the optimisation they justify | the hardware questions are answered from hardware |
| 6 | A clean install, and publish | somebody who is not us installs it from the kit and it works |

---

## Stage 1 — fruit, bring-up, and the decisions stage 3 needs

Four unrelated kinds of thing, grouped because each is small and each unblocks
something later.

**The fruit.**

- `get_clipboard` / `set_clipboard` (`PLAN.md`, Phase 6 tool table). Two tools
  and about thirty lines of `OpenClipboard`/`CF_TEXT`.
- Exit codes through `COMMAND.COM` (`BACKLOG.md` 17). `ExecResult::viaShell` is
  computed and sent nowhere, so the agent is told a wrapped command succeeded.
  The smallest real fix in the backlog.
- The `STYLESHEET` comment strip (`BACKLOG.md` 13, "free and separable"). 4,919
  of 8,445 bytes are CSS comments, `doc()` carries no `enc` header so they are
  never compressed, and they go out once per connect and again on every
  reconnect. Needs the DOC goldens regenerated and depends on nothing.
- The stale-doc sweep: `PROGRESS.md`'s "Current goal" still says Phase 5; R5 and
  R6 are labelled planned above text saying they shipped; `PLAN.md:1660` says the
  in-process collector has not landed and it has; `BACKLOG.md` 697 asserts Phase
  6 has not landed; `LAUNCH` and `MKDIR`/`DELETE` sit in "Explicitly cut" while
  shipping as tools. Phase 6 was never written up as a phase, which is how most
  of these went stale at once.

**The P200 bring-up.** Not the measurements -- stage 5 is those. Just: the box
is on the LAN and `gremote` answers it. Two preconditions nobody has recorded
(`BACKLOG.md` 1b): whether it has a NIC with a configured TCP/IP stack, and how
`GREMOTE.EXE` reaches a cold machine the first time. The only proven channel is a
1.44MB floppy via `tools/gen/mkw98kit.sh`, where gremote is not yet included.
Autostart wants a shipped `.REG` for `RunServices` rather than registry calls in
C, because ADVAPI32 is not on `tools/check/imports.py`'s allow-list.

It is here, and not in stage 5, because four-plus phases of software have
accumulated on an emulator and the Athlon has already reversed one prediction
taken from the VM (`shouldCompress`, `PROGRESS.md`). Every assumption carried
past this point is carried further.

**The injection-safety loose end.** Gate 0.15d was never re-run, and
`UIAUTOMATION.md:645` says section D should not be scheduled before it. Section D
shipped anyway. Neither half of the original evidence holds: the GDI patch was
applied to the probe's own pid, so nothing has ever been captured from a foreign
process, and the injection verdict read a cumulative counter nothing zeroed. The
probe is fixed and has never been run. `BACKLOG.md` 15 is the other half -- an
hour of the guest at 100% of a vCPU after those gates, causation unestablished,
standing rule attended-only.

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

1. **What export does.** `export_document` (`bridge/view/html.py:643`, called
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

## Stage 2 — size, before the thing that spends it

`gaston.exe` is at 580,649 of a 585,728 cap; `gremote.exe` at 363,449 of 372,000.
Stage 3 adds a renderer translation unit to the guest, and a new TU costs ~2,144
bytes of `-xs` exception machinery before any code in it.

- **The flag decision** (`BACKLOG.md` 2). `-os` measured 32 KB smaller than
  `-otexan` across the image. The likely shape is "`-os` by default, `-ot` kept
  for measured hot modules" -- miniz, the sync CRC pass, the frame codec. Needs a
  speed/size call, and a before/after on the guest: a timed sync scan and a long
  transcript page. Recompiling the world smaller before adding translation units
  pays for stage 3; doing it afterwards is another cap raise.
- **The gremote trim** (`BACKLOG.md` 29). Already decided when the cap was raised
  for the ui lane: `-os` over `win/remote/`'s own sources, whether gremote needs
  every object in `libautomation` (including the MSAA path when `HELLO msaa=0`),
  and the per-TU `-xs` cost. Measure the **working set**, not just the image.
- **R9 step C5+** (`REMOTE.md:1109`) belongs here rather than in stage 3: the last
  C-step with no "done" marker, `main.cpp`'s statics becoming objects and the
  context-free callbacks gaining a context. It is in the same files stage 3 will
  touch, so it goes first or it goes much later.

---

## Stage 3 — the markup moves to the client

`BACKLOG.md` 31, with 13 first because 13 is smaller and builds the handshake
capability and the export answer that 31 also needs.

What it is: the bridge keeps the markdown parse (lookahead, fence state across
feeds, escaping, the encoding fold) and stops emitting markup. It sends blocks
and inline runs; each client renders them. The guest's renderer emits the same
HTML MSHTML gets today. "No markdown parser in C" holds, and so does **the guest
transforms nothing** -- by 13's own distinction this is decoration.

Why it matters beyond tidiness: three bridge-side numbers are calibrated to
MSHTML rather than to text -- the page's block cap (`bridge/view/pages.py:39`, 90,
measured: a 200-element page took the 32MB guest to 48 KB free), the tool-output
wrap column (`bridge/view/html.py:152`) and the tool-result truncation (`:114`).
A terminal client inherits all three and can use none of them.

Three things in here are larger than they look, and each is named in 31:

- **`live` mode, not `append`.** A partial block is re-sent whole as it grows, so
  with rendering client-side the guest re-renders the open block on every update.
  Same bytes, but the work moves onto a 1998 CPU. That makes it a repaint-budget
  question, and therefore a question stage 5 has a bearing on.
- **Export**, decided in stage 1 and built here.
- **The goldens are the thing being rewritten**, so they cannot be the oracle.
  `tests/python/server/test_frame_goldens.py` pins `TEXT` and `DOC` and gets
  regenerated wholesale. Capture a before screenshot of the transcript on the
  guest, and an agreement test on the vocabulary in the shape
  `test_guest_css.py` and `test_uirec_agreement.py` already use: both sides'
  tables compared, with a count asserted so a dropped case fails loudly.

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
- **`COMMITTED_CAP` +4,096** so `gaston.exe` can hold the guest half of the
  automation gate. The feature's own cost is 404 bytes; the rest is one page of
  DGROUP alignment. Deferred to you explicitly.
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
