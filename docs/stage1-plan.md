# Stage 1 — fruit, the decisions stage 3 needs, and the harness

## Context

`docs/ROADMAP.md` (committed `fd21924`) splits the rest of the project into six
stages. This is stage 1: the small unbuilt things, the decisions that gate the
markup refactor in stage 3, and one safety loose end. The P200 bring-up is
**out** by your call, so nothing here waits on hardware.

Three sweeps of the code turned up corrections that change what stage 1 is. Two
of them contradict the roadmap I wrote an hour ago, and one contradicts
`BACKLOG.md`:

1. **`ExecResult::viaShell` is not "sent nowhere".** It goes out on gremote's job
   lane (`win/remote/jobops.cpp:143-151`, read at `gremote/ops.py:112,181`), and
   the agent lane has since grown an exit-code *wrapper* that supersedes it
   (`bridge/services/execwrap.py`, `bridge/profiles/command.com.toml`
   `[shell.exit_code]`). The agent is already told "Non-zero exit status: this
   failed." So BACKLOG 17's bullet and `ROADMAP.md:55-58` are both stale, and the
   real gap is wrapper-shaped, not `viaShell`-shaped.
2. **Gate 0.15d has been re-run with the generation word** —
   `docs/measurements/vm-20260926.txt:90-101`, `distinct pids 2 (neither of them
   us)`, `-> INJECTION WORKS`. Not after a reboot (the generation reads ~9.4
   hours of uptime), on the VM rather than hardware, and in the same session that
   ended with the 0.15h wedge. `UIAUTOMATION.md:656-662,751`, `PROGRESS.md:74` and
   `ROADMAP.md:78-80` all say it has never been run.
3. **The autostart `.REG` exists, ships, and pre-logon answering is proven** —
   generated at `tools/gen/mktools-iso.sh:136-142`, applied by `SETUP.BAT`
   (`:237,:246`), and `docs/measurements/gremote-vm-20260927.txt:169-181` records
   the box answering 2 s after a cold boot with nobody logged in. BACKLOG 1b and
   `ROADMAP.md:70-72` carry it as outstanding; `docs/gremote-plan.md:335-341`
   already lists it closed.

Also: the `*-p200-*.txt` measurement files are mostly **Athlon XP** runs
(`inifile-absent-key-p200-20261001.txt:4` calls the Athlon "the P200 checklist
box"). The real Pentium 200 appears only in `p200-run1/2-20260926.txt`. That
mis-naming is how a hardware answer gets credited to the wrong machine.

**Outcome of this stage:** two new tools, a smaller DOC frame, an honest exec
result, four decisions recorded where the work will look for them, a VM harness
that makes an injection run safe to repeat, and a documentation set that does not
lie about what has been measured.

---

## The four decisions, as answered

Recorded in commit 1, before any code, because stage 3 reads them.

| Decision | Answer |
|---|---|
| **R7** | **In**, the larger reading: several guest machines against one bridge. It becomes its own stage between 3 and 4 in `ROADMAP.md`. `BACKLOG.md` 7's implementation list stands; `client_id` as an unauthenticated assertion is accepted in writing. Note while recording it: **two guests already exist** (the VM and the Athlon, `machine=TIBI`), so `rebind` is verifiable without new hardware — BACKLOG 7 says it needs a second machine and it has one. |
| **Export** | **The guest renders it.** One renderer, no drift, and `win/app/cache.cpp` already holds rendered pages, so export becomes "assemble what is cached and write it". `bridge/view/html.py:652-656` argues the exported copy must match the screen; this makes that true by construction. Replaces the three-option list in BACKLOG 13 step 2 and 31 step 5. |
| **Vocabulary capability** | One `HELLO` key, a small integer version, bridge refuses an unknown value rather than guessing — per **client rules are declared at handshake**. Covers both 13's class names and 31's block kinds. The client has 14 free HELLO slots (`win/net/net.cpp:236` `RC_HELLO_MAX 48`, 34 in use) and `tests/python/meta/test_hello_headers.py` asserts two remain after. |
| **Cache format** | The cache keeps holding **rendered markup**, not blocks, so a reconnect costs no re-render on a 1998 CPU; the format key gets a bump so pages written by an older renderer are discarded rather than shown. |
| **0.15d scope** | The reboot run plus BACKLOG 15's harness. Foreign-process GDI capture (the probe's own D4, `win/checks/probe.c:2874-2883`) stays deferred **with its reason restated**, not silently. |

---

## Commits

### 1. Docs: the four decisions, and what the sweeps found

No code. `PLAN.md` R7 and "The bridge owns markdown"; `BACKLOG.md` 7, 13, 31, 15,
17, 1b; `ROADMAP.md`'s three wrong lines; `UIAUTOMATION.md:645-653,751`;
`PROGRESS.md:74` and its stale "Current goal".

Corrections, each with the citation that settles it:

- BACKLOG 17's exit-code bullet rewritten around the wrapper. The residual gaps
  are named for commit 3.
- 0.15d recorded as measured on the VM, not from cold, with
  `vm-20260926.txt:90-101` quoted and the two caveats kept.
- BACKLOG 1b loses the `.REG` and the first-delivery preconditions, keeps the
  P200 itself. `gremote-vm-20260927.txt:291-293` contradicts its own `:169`;
  note which half is current.
- BACKLOG 13's figures refreshed: `STYLESHEET` is **9,375 bytes with 5,574 in 11
  comment blocks**, not 8,445/4,919, and its four line citations have drifted
  (`:448`→`:445`, `:622-630`→`:624-640`, `:633-657`→`:643-670`,
  `server.py:341`→`:407`).
- A line at the head of `REMOTE.md`'s P200 checklist naming which `*-p200-*.txt`
  files are Athlon runs. Renaming them would break citations across four docs;
  one note costs nothing and stops the next reader crediting the wrong machine.
- `REMOTE.md:884-889` still quotes gremote's cap as 264,000 (it is 372,000) and
  `:898-899` quotes 398,820 of 399,000 for the client (`COMMITTED_CAP` is
  585,728).
- The small ones: `PROGRESS.md`'s goal line, R5/R6 labels in `PLAN.md`,
  `PLAN.md:1660`'s "until that collector lands", `BACKLOG.md:697`'s "Phase 6 has
  not landed", `LAUNCH`/`MKDIR`/`DELETE` in "Explicitly cut", and
  `tools/gen/w98kit/TEST.BAT:27`'s `[1/18]` against 20 steps.

### 2. The DOC frame loses its comments

`bridge/view/html.py`. **Strip at import, do not edit the literal** — the
comments are the only explanation of why several of those rules exist (the
1,643-byte `overflow: auto` note among them), so they stay in the source and stop
reaching the wire.

A `_without_comments()` applied once to `STYLESHEET` at module scope.
`document()` and `export_document()` both pick it up, which is right: export
shares the stylesheet deliberately (`html.py:667`) and is compressed, so it loses
nothing by shrinking too.

Measured effect: the DOC body goes from **9,557 to 3,983 bytes**, 58% off a frame
`bridge/wire/protocol.py:171-173` sends with no `enc` header, once per attach
(`bridge/server.py:407`) and again on every reconnect.

Tests: a new case that no `/*` survives and that `H.document()` still contains
every `RC_CSS_*` class (`tests/python/meta/test_guest_css.py` already asserts the
second, so it is the regression guard). Then regenerate the **five** DOC goldens
with `UPDATE_GOLDENS=1 pytest tests/python/server/test_frame_goldens.py` and read
the diff — `tests/python/server/golden_utils.py:97-105` also writes silently when
a file is *missing*, so a deleted golden self-heals and passes. Regenerate
deliberately, never as a way of making a red test green.

### 3. When the shell cannot say, say so

`bridge/services/guest_exec.py`, one prose key in `bridge/prompts/tools.toml`.

The wrapper closes the COMMAND.COM exit-code hole where it applies
(`guest_exec.py:331-344`). Three cases where it does not, and today the agent
cannot tell them from success:

- **The wrapper was skipped** — no fs lane, no `temp` HELLO header, or the `put`
  failed. `guest_exec.py:145-151` logs and carries on, and the agent gets
  `code=0`, which on this shell proves nothing.
- **The command is a builtin** (`execwrap.applies()` against the profile's
  `builtins` list). `dir NOSUCHFILE` is success-shaped silence.
- **No verdict came back** — the output cap bit, or the shell died, so
  `execwrap.split()` returns `""` and nothing is claimed.

All three become one sentence from `tools.toml`, said only when the shell's
profile has `lost = true`: the status of this command is unknown, not zero. Exact
exit values stay out of scope by the decision already recorded at
`execwrap.py:22-26`.

`tests/python/services/test_guest_exec.py` has **no coverage of the wrapped
verdict path at all** today, and none of the `Exit code {code}.` branch. Three
cases here, one per skip reason.

### 4. `get_clipboard` / `set_clipboard` — the guest half

PLAN's own framing: the escape hatch for text no message will surrender —
`Ctrl+A`, `Ctrl+C`, read it (`PLAN.md:942`). So it belongs beside `input`, and
**no new translation unit is created anywhere**, which matters: `-xs` costs
~2,144 bytes per TU before any code, `gaston.exe` has 5,079 bytes of slack and
`gremote.exe` 8,551.

- **Primitive into `win/automation/input.cpp`.** `OpenClipboard`/
  `GetClipboardData(CF_TEXT)`/`GlobalLock` to read;
  `GlobalAlloc(GMEM_MOVEABLE)` + `EmptyClipboard` + `SetClipboardData` to write,
  with ownership passing to the system on success. All USER32/KERNEL32, all on
  `tools/check/imports.py`'s allow-list, and **no W-suffix import** — assertion 3
  of that gate fails on any W symbol outside `{GetCommandLineW,
  GetModuleFileNameW}`, so `CF_TEXT` in the machine's ANSI codepage is the path,
  as the rest of the codebase already does with `fold`.
- **Handler shared in `win/ui/uiengine.cpp` behind `UiLink`**, the seam the ui
  lane already established. The text rides `report()`, never `end()` —
  `win/ui/uilink.h:66-76` is explicit that `end()` is headers only because a
  non-bulk frame reserves no out-queue slot and would eat the heartbeat's
  reserve.
- **Gated by level**, mirroring TREE/RUN: `automationLevel() >= 1` to read,
  `>= 2` to write, both refused at `RC_AUTOMATION_NONE` by the lane's existing
  gate (`win/ui/uirpc.cpp:564-572`). No new capability, no `caps.py` bump —
  `RC_CAP_OPS` stays 2 against `bridge/files/caps.py:465`'s ceiling of 2.
- **One frame type, not two.** `UICLIP` with `op=get|set`, body carrying the text
  on a set, reply `UIDAT` + `UIEND`. `UIDAT` is already in
  `rc_frame_compressible` (`win/net/frameproto.cpp:64-89`), fixed last week, so a
  large clipboard travels compressed for free. One lane-table row, one dispatch
  branch in `win/ui/uirpc.cpp:597-620`, one `G_UI` row `CLIP` in
  `win/remote/optable.cpp` with its branch in `win/remote/uiops.cpp:395-409`.
- Cap the reply at `bodyCap()` and set `truncated=1` rather than refusing;
  refuse an oversized `set` outright, since a clipped selector-shaped string is a
  different string.
- `uicheck.exe` gains cases through the existing `FakeUiLink`: round-trip,
  refusal at each level, an empty clipboard, a body at the cap.

### 5. The clipboard's bridge half

- `bridge/wire/protocol.py` — one constructor beside `ui_tree`/`ui_shot`.
- `bridge/services/ui.py` — a method on the existing `u<n>` id table, which
  already serialises on `guest.ui_lock`; there is one desktop worker and that is
  the right queue.
- `bridge/server.py:234-276` — the inbound dispatch row.
- `bridge/agent/tools.py` — two `@tool` blocks, full JSON Schema (never the
  `Annotated` shorthand, `:283-301`); the read joins the `screen` list, the write
  the `steps_ok` list (`:1702-1716`).
- `bridge/prompts/tools.toml` — `description`, `args`, `view`, and one `refused`
  key per guest status word. Prose cannot live in `tools.py`:
  `tests/python/meta/test_prose_lives_in_toml.py` gives that file a budget of
  zero.
- `bridge/agent/categories.py:668-787` — `classify` branches: the read is
  `screen`, the write `screen_drive`. **Without a branch a machine verb falls
  through to `return v` at `:787` and lands on ASK forever**, silently.
- `bridge/agent/policy.py:251-270` `TOOL_CATS` and
  `bridge/agent/permissions.py:245-258` `_PLAIN`.
- `screen_drive` stays out of `GRANTABLE` (`policy.py:167`), so "Allow for this
  chat" never lifts a clipboard write. That is the same reasoning BACKLOG 30
  already records for typing.

### 6. The harness, and then the run

`tools/`, `tools/gen/w98kit/TEST.BAT`, `win/checks/probe.c`.

- **Move the injection steps last.** They are 9-11 of 20
  (`TEST.BAT:48-53`), with nine screen/MSAA steps and the whole `AUTOCHK.EXE`
  battery behind them, so a wedge there costs the rest of the run. Behind a
  prompt, and the `[1/18]` label fixed.
- **`PROBE.EXE` with no argument runs every section including `inject`**
  (`win/checks/probe.c:3798`). Exclude the three injection sections from the
  no-arg sweep and require an explicit name: the no-arg path is what an operator
  types.
- **A `health` section** as the run's epilogue, asserting the three things
  BACKLOG 15 asks for and nothing in the tree does: a keystroke changes a window's
  text, a window still takes focus (`win/checks/focusprobe.cpp` already measures
  exactly this and is not wired into `TEST.BAT`), and a write lands.
- **A rollback script.** `virsh destroy retroclaude-win98`, copy
  `vm/win98.qcow2` to a timestamped known-good, start, run, restore on failure.
  There is no snapshot tooling in the tree today and the eight `vm/*.bak-*` files
  were all made by hand. `tools/vm/qmp.py:493-498` advertises `save`/`load`,
  which the raw `fda` floppy silently blocks — add that note where it is
  advertised. `tools/vm/vmdefine.sh:28-31` **refuses the current
  `vm/win98.xml`** because the floppy was added after the guard; fix or scope the
  refusal.
- **Then the run**, cold boot, injection last, health check after, and
  `PROBE.LOG` pulled off over gremote's fs lane rather than mailed by hand.
  Measurement into `docs/measurements/`, and the three docs that say 0.15d was
  never run corrected against it.

### 7. The first agent turn through the UI tier, and the kit

- **The turn.** Client and bridge on the VM, and the model asked to read a dialog
  and press a named button — then to read a list box's contents, which is
  precisely the case `PLAN.md:942` says the clipboard exists for and which
  `WM_GETTEXT` cannot answer cross-process. Only probes and gremote have ever
  driven this tier. Transcript into `docs/measurements/`.
- **The kit, optional and cheap.** `tools/gen/mkw98kit.sh` stages 355,666 bytes
  onto a 1,474,560-byte floppy and has **no size check at all**. Add
  `GREMOTE.EXE` (356,352), `GREMOTE.INI` and `GREMOTE.REG`, and the budget check
  the script lacks: still under half the disk. The P200 is out of this stage, but
  when it arrives it is then one floppy away rather than a build.

---

## Traps

1. **Five exact-set tests will fail on a new tool**, each on purpose:
   `tests/python/agent/test_tools_surface.py`'s `SCREEN_TOOLS = ("ui","input",
   "shot")` is asserted as the *exact* difference the automation gate makes;
   `tests/python/agent/test_input_steps.py:288-299` requires every offered
   machine tool to be in `TOOL_CATS`; `test_policy.py:374-381` the same from the
   other side; `tests/python/agent/test_ops_tools.py:78-81` pins what an
   `ops=0` client is denied; `tests/python/meta/test_gremote_contract.py:149-161`
   asserts `continuing == {"DATA","END"}` — check whether a `CLIP` row sets
   `continues` before assuming it is unaffected.
2. **`CASE_COUNT` is asserted exactly** because a case that stops running prints
   nothing: `test_uirec_agreement.py:61` is **52** and drives `uicheck.exe`;
   `test_guest_automation.py:47` is **180** for `autocheck.exe`.
3. **`checkKeys("ui", UI_KEYS, 6)`** (`win/remote/config.cpp:129`) counts with a
   literal that is separate from the array's own length. Nothing here adds an INI
   key, but the gremote ui section is where one would go.
4. **`check-sources`** (`win/Makefile:916-934`) fails the build on `strcpy`,
   `sprintf` and seven more across all of `win/`. The clipboard's text handling is
   exactly where `sprintf` gets reached for; `rc_strf`/`rc_strcpy` from
   `win/core/str.h`.
5. **No file-scope object with a constructor** (`win/Makefile:936-941`), and
   **no stack frame near a page** (`stackframes.py`) — a clipboard buffer is heap
   or it is a `Buf`.
6. **Both caps are charged.** The gate sums `max(virtual, raw)` per PE section, so
   it bills a whole 4,096-byte page: a few hundred bytes of clipboard code can
   cost 4 KB of cap in each exe. Measure `check-gaston` and `check-gremote` before
   and after, and if a page tips, that is the moment to reach for BACKLOG 19's
   deferred `COMMITTED_CAP` raise rather than inventing a different number.
7. **A clipboard op cannot go on the control or immediate lane.**
   `win/net/frameproto.h:50-62` and `win/remote/optable.h:21-24`: never USER or
   GDI there, because a hung 16-bit app holding Win16Mutex blocks those calls for
   every 32-bit thread, and that thread carries the heartbeat.
8. **`UPDATE_GOLDENS=1` writes a missing golden silently.** Regenerate on
   purpose and review the diff; never to clear a red test.
9. **Do not rebuild under a running suite.** `make wine-smoke` runs the built exes
   for about five and a half minutes, and a rebuild mid-run mixes them.
10. **Start guest runs from a clean boot.** Killing a console program strands its
    DOS VM, and a guest carrying stranded VMs produced a measured BSOD in VMM.

---

## Verification

**Per commit**, the project's standing gate: `make -C win all`; `make -C win
check` (six targets, each running identity, `imports.py`, `cmovcheck.py`, the
static-constructor audit, `stackframes.py` and the committed-at-load sum);
`make test-fast`; `pytest tests/python/remote -q`; `make lint`; `make wine-smoke`.

**The clipboard, in three places, because Wine proves the code and not the
platform:**

1. Under Wine: `uicheck.exe`'s new cases and the refusal ladder at each
   automation level, reachable with no desktop.
2. On the VM, with a real clipboard: copy text in Notepad by hand and read it
   through the tool; write through the tool and paste it by hand; a list box's
   contents via `Ctrl+A`/`Ctrl+C` then a read, which is the case the tool exists
   for; `automation=0` refusing both on the next request, then `automation=1`
   allowing the read and refusing the write.
3. On the Athlon over gremote (`tools/probe/`, `Remote("10.0.0.125", <secret>)`),
   because that is the box where the ui lane's last four bugs were found.

**The exec change**: three new cases in `tests/python/services/test_guest_exec.py`,
then one real wrapped failure and one builtin failure on the VM through the
client, with the result text read as the agent would see it.

**The DOC strip**: the five regenerated goldens reviewed by diff, then a client
attach on the VM confirming the transcript still renders — the stylesheet is the
one thing no test can judge by eye.

**The harness**: the run itself is the verification, and it must be repeatable —
restore the known-good image and run it twice.
