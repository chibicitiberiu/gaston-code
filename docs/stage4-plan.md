# Stage 4 — the recorded decisions, the inconsistencies, the small repairs

## Context

Stages 3b and 3c closed 2026-10-13 (`a83d8b0`). `ROADMAP.md:414` is next, and its exit condition
is unusual: **`BACKLOG.md` holds no entry whose only blocker is an unanswered question.** That
makes the stage's own text the first thing to check, and it does not survive the check.

**Two adversarial review rounds against the tree: 40 defects, then 37 more.** Twelve killed a
decision. Three of this plan's own claims were false and are corrected below rather than quietly
dropped. The driving indicator left the stage entirely as a result — see `stage4b`.

The user chose the strict reading of the exit condition: answer them all.

---

## What the reviews changed

**The count is 10, not 18 and not 12.** The roadmap names 7. My first sweep said 18, my second
said 12, and both were wrong in the same direction — by counting entries that already hold an
answer:

- **§3 holds no live question.** `bridge/wire/replay.py` does not exist. `protocol.py:115-118`:
  *"There is no replay buffer and no `stamp=` flag… The never-replay invariant for
  EXEC/EXECKILL/SYNC\* is enforced by never replaying anything."* Closed 2026-09-18. Only §4's
  `SYNCDEL` half is a real decision.
- **§23's flush question is answered in its own title** — "…and that is a choice" — with the
  condition on a future op already written.
- **§7, §9, §32** were already answered in writing (`BACKLOG.md:607`, `:880`, `:2870`).
- **§30's screen-input grant is a watch condition, not a ruling.** `policy.py:161-167` records it
  verbatim and `GRANTABLE` enforces it.

**Three claims of mine were false.**

| I said | actually |
|---|---|
| `ocr(rect)` has no `BACKLOG.md` entry | §28 `:2709` records it. It still does not block the exit condition — its blocker is work, not a question — but **E11 must update that bullet**, which says OCR "would be" built. |
| `rsrc32` is a checklist item, not a backlog entry | §11/04.23 `:1202` records it, and item 9's ruling **contradicts that sentence**. §11 joins E16's list. |
| `LIMIT.TXT` fixes the page-size orphans | It cannot be written or read. See E5. |

**Only the logoff bit (item 12) is genuinely checklist-only** — one unrelated `logoff` hit in the
whole of `BACKLOG.md`.

**E6 is gone.** gremote drives the desktop independently and `BACKLOG.md:2063` records that as
accepted, so the indicator belongs in the shared seam `rc_ui_run` (`win/ui/uiengine.cpp:795`,
compiled into `gaston`, `gremote` and `uicheck`), needs its own pump thread, breaks at
`clickAt`'s occlusion check rather than at `isOurs`, and must mean *stop* rather than "abort this
step list". That is a stage. §19's residue is then blocked on **scheduled work**, which still
satisfies this stage's exit condition.

---

## The decisions

**Taken by the user, 2026-10-13.**

| # | ruling |
|---|---|
| §35 | **The power button.** Cause 1 stands — no usable APM on that box as installed. `win/exec/system.cpp:92`'s lone `EWX_POWEROFF` is **innocent and does not change**. |
| §8 | **`enc=rle8`: no, ratified.** `UIAUTOMATION.md:285` already wrote it from a real four-band run; the "2.8x" has no measurement file and its likely source is a *discarded* 2.7x single-band number (`probe.c:1656`), sign flipped. |
| §17 | **`kill` kills the Win32 descendants** and lets a console program end itself. |
| §11b | **No to persisting a CLI permission rule** — restated below, because the first wording reversed the entry's own closure. |
| item 12 | **Measure the logoff bit in this stage**, not stage 5. |
| §11a | **The menu item, plus the orphan sweep that is actually possible.** No byte cap. |
| §19 | **A topmost window — in its own stage.** |
| §12 | **Accept the supervisor.** The agent may restart the bridge through a process that outlives it; nothing restarts a bridge it does not own. |
| §30 | **Models per mode: no to both readings** — neither an allow-list nor a preferred default. Mode says what may happen unasked; Model says who is doing it, and each menu shows what is in force. |
| — | **`ocr(rect)`: build it.** |
| — | **Raise the caps with a measured reason**, in the commits that need them. |

**§11b, restated.** The first wording said "no" to approve-once for `exec` and the entry says the
opposite two paragraphs above its own closure: `BACKLOG.md:1089-1096` closed it on 2026-10-04,
and the bar's "This chat" button already grants a *category* — `GRANTABLE` is
`FILES_WORK`/`RUN_WORK`/`RUN_BUILT`, and the last two **are** `exec` classifications — kept in the
conversation's settings so it survives rebuilds and forks. So approve-once for `exec` exists. The
**no** applies only to persisting a CLI `updated_permissions` rule, which has no SDK call to
re-assert it and could therefore only re-apply after it was needed. E16 says which of the two it
refuses.

**Recommended rulings, for approval with this plan** — the remaining three:

| # | recommended answer |
|---|---|
| §4 | **A re-delivered `SYNCDEL` is answered from the recorded result, never re-executed.** `EXEC`'s half of this question no longer exists (there is nothing that can re-deliver one), so this is `SYNCDEL` alone. |
| §6 | **No third queue class — and close it on the entry's own condition rather than overriding it.** The entry says "decide when the first of the three callers is actually built"; Export *is* built (`BACKLOG.md:502`, 2026-09-19) and does not contend for the job slot. That is evidence, not an override. Fix the stale cite while there: `id_matches_family` is `rpc.cpp:914`, not `:595`. |
| item 9 | **No USER/GDI resources in `SYSINFO`, because of the lane rule** — `control.h:1-8`, kernel32/Winsock only, because a hung Win16 app holding Win16Mutex blocks USER/GDI from every 32-bit thread. The 16-bit-thunk claim is an *inference*, stated as one; `PLAN.md:650` marks the route "(unverified)" and `probe.c:75-93` already calls it successfully **off** that lane. So the op is refused, not the measurement, and §11's own sentence about installing Resource Meter is corrected rather than contradicted. `PLAN.md:653-654` has the alternative: our own GDI counts plus working set. |

**Confirmations, not rulings** (E16 records the outcome; it does not decide): §3, §7, §9, §23,
§32.

**Watch conditions, named:** §18's upstream half (no SDK `supportedPermissionModes`), §30's
screen-input grant (the guest must report the target before `screen_drive` can be granted), §30's
comctl32 4.00 bullet (no Windows 95 machine here — a stage 6 install-kit caveat), and §19 (stage
4b).

---

## Commits

**E0 — the plan, and `ROADMAP.md:414` rewritten.** No code. Records every ruling; corrects the
stage's own text where the reviews found it wrong — the `rc_strf` figure, the automation restart
claim, rle8's "about an hour", §18's residue, the count, and **§30 having seven bullets, not six**
(`ROADMAP.md:442`; the seventh, the SAFE cache at `:2829`, is already answered in the entry).
Renumbers the **ANCHOR** §32 (`:2845`), not the memory one — `stage1-plan.md:18` and
`PROGRESS.md:4225` both cite `:3038` as 32 and nothing cites ANCHOR, so that is the free one.
Also records that `ROADMAP.md:437` puts the logoff in stage 5 and this stage overrides it
deliberately, and that `ROADMAP.md:459`'s "eight of the twelve" P200 bullets is seven today and
six after E12.

**E1 — §35 closes on cause 1.** `core/app.h:87` gains *confirmed on the Athlon XP, 2026-10-13*.
An **addition, not a correction**: that comment is already scoped ("on a machine with no power
management") and already attributed ("also measured, on the same guest"). The flag does not
change. The failed-poweroff watcher is `stopServices()`/`preTranslate()` at
`win/remote/main.cpp:172,196,299,333` — **not** `stayRunningAfterLogoff()` at `:91`, which is
E12's subject — and is now proven necessary on real hardware.

**E2 — §8 closes as a no.** Docs only, and first because it is the one ruling that *removes*
work. The 2.8x claim is struck with `probe.c:1656` named as its likely provenance;
`ROADMAP.md:421` and `REMOTE.md:1363` stop carrying it live.

**E3 — the `rc_strf` convention.** `win/core/str.h` gains the rule: a return is checked, **or**
guarded by an explicit length precondition, **or** logged and carried on where no refusal is
available, **or** the destination is display-only — enforced for path and id destinations only.
The fourth form is not padding: at `uirpc.cpp:139` and `:595` the refusal that would replace the
copy **would itself have to carry the over-long id**, so those two can only log. Template:
`win/rpc/rpc.cpp:467-473`. **Not a sweep**, and the replacement figure has to be honest about its
denominator: **472** of **527** production sites across `rc_strf`, `rc_strcpy` and
`rc_strncpy` discard the return — which is what `BACKLOG.md:300`'s superseded 105 counted. That
bullet also names `win/rpc/exec.cpp`, which no longer exists (it is libexec now), and its "only 8
check" is stale by about five times. Fix the function set, the file list and both numbers.
**E3 does not move the image** — a header comment and backlog prose.

**E4 — the eight id sites.** `win/sync/syncrpc.cpp:224`, `win/rpc/fsrpc.cpp:166` (one line below a
path that *is* checked — the convention half-applied), `win/ui/uirpc.cpp:139`, `:595`, `:361`,
`win/remote/uiops.cpp:357`, `:374`, `:395`. **Eight, and the commit message says eight** — the
previous draft called them six while listing eight. `RC_MAX_VAL` is 256 against 48- and 64-byte
buffers, so a buggy bridge reaches them; real ids are `f12`/`u7`, so this is robustness and the
message says so. **`win/ui/uirec.cpp:97` is left alone for the right reason:** the measured tree
is 4 to 6 deep (`tree.cpp:26`), not because of the depth cap and not because
`win/checks/uicheck.cpp:320` pins it — that check never truncates in the case it exercises (33 of
64 bytes), so it would stay green either way, and at three-digit siblings 17 components is 67
bytes, over budget by the same arithmetic that clears `tree.cpp:208`. **The check goes in
`win/checks/rpccheck.c`**, which links `fsrpc.cpp`, `syncrpc.cpp` and `uirpc.cpp`
(`win/Makefile:650`) and is driven by `test_guest_rpc.py`, which pins named verdicts rather than a
count; `uicheck` links none of these sites. `uiops.cpp`'s three are gremote's, covered by
`tests/python/remote/`.

**E5 — the cache sweep and the menu item.** `LIMIT.TXT` is dead, for three independent reasons:
`rc_cache` has no access to the block limit at all (`cache.h:50-64` — no `rc_net`, no config, and
`open_session` takes only an id); the limit is not *known* on the cold-start path it existed for,
because `page_blocks` is assigned at `net.cpp:433` **after** the connect fails at `:380`, so
`rc_net_page_blocks` falls back to 90; and when `per` changes **every** boundary changes, so "sweep
the stale pages" means the whole conversation — on the disconnected path, before
`mainwindow.cpp:675`'s notice, because `:667` returns on `pages <= 0` first. That is the FORMAT
file's loss under another name. What is left, and is clean:
- **The sweep** for `P###.TMP` (the ordinary dropped-link case, `cache.cpp:159`) and pre-stage-3
  `P###.HTM`, which `cache.cpp:84` hands to §11a by name. Both identifiable from the filename
  alone. Placement after `dir`/`sess` are set (`cache.cpp:102,107`), with the early return when
  the session is already open accounted for, across all four callers
  (`mainwindow.cpp:446,542,615,665`).
- **"Clear cached history"** beside **`IDM_OPTIONS`** (`mainwindow.cpp:210`) — *not* "Configure >
  Server Host", which no longer exists (`resource.h:70`, "was Server Host… same id"), and
  `BACKLOG.md:1154` is corrected with it. Reuses `fsDeleteTree` (`win/fs/tree.h:75`, already in
  the image via `rpc/fsrpc.cpp:616`), passing `permanent=false` — for a deletion the person asked
  for, the Recycle Bin is the feature.
- **Three live strings still tell the user to use a menu that is gone**:
  `mainwindow.cpp:1112` ("Use Configure -> Server Host... to set one"), `:1388`, and
  `statusbar.h:4`. A real small repair, found by the review, and this is the commit that is in
  those files.
- The stale disk bound is in **two** places: `cache.h:26` and `BACKLOG.md:1141`. Both corrected.
  (`cache.cpp:76` also carries a stale cite of its own, `mainwindow.cpp:620-631`, now `:665-667`.)
- **Not claimed:** the contiguous-scan staleness. `mainwindow.cpp:669-674` already records it in
  as many words and the notice already says "a page", not the last page.

**E6 — the automation menu item.** One checked item in the Configure popup beside `IDM_PERMS`
(`mainwindow.cpp:211`), a `WM_COMMAND` case beside `IDM_OPTIONS` (`:1756`), routed through
`optionsApply` (`:2063`), which already handles saving, the PREFS resend, the reconnect decision
and the mid-turn prompt, and already lists `automation` among its five reconnect triggers
(`:2070`). **The reconnect stays in both directions**, and the commit records why "off is
instant" was not taken: nothing writes `net->automation` outside `beforeStart()`, which
`reconnect.h:34-38` declares the only moment a HELLO-carried setting may change "without a thread
reading them mid-write"; and the bridge's `guest.caps` only move at handshake, so a skipped
reconnect would leave the agent offered tools the client has stopped honouring. **The tick must
not lie:** `optionsApply` writes `*m_cfg` at `:2077` and saves at `:2084` *before* the mid-turn
prompt at `:2101`, so declining leaves the setting saved while `net->automation` keeps the old
value — the tick reads the live value or shows pending. Making "off" instant is its own recorded
piece of work. The gate `BACKLOG.md:2002` refers to is the client's, `uirpc.cpp:564` — **not**
`uiops.cpp:200`, which is gremote's `UiConfig`, changed by gremote's own tray and unreachable
from `optionsApply`.

**E7 — §18's three stale comments.** `optmenu.h:70` (the vocabulary changed entirely, not just
`auto`: `options.py:34` records that "the CLI's permission-mode names are no longer on the wire"),
`optmenu.h:80` (`COMMAND_MENU` → `services/options.py:40`), `optmenu.cpp:23` (`MODE_ORDER` →
`:37`). §18's "Related" paragraph **narrows rather than closing** — `options.py:305-311` still
calls the CLI and still handles a refusal at the Plan boundary — and `BACKLOG.md:1799`'s cite of
`bridge/options.py`, a file that does not exist, is fixed in the entry being closed.

**E8 — `kill` walks the Win32 descendants: one edit.** The `dos_session` refusal is implemented
**once**, in libexec at `win/exec/process.cpp:177` (`win/exec/process.h:54,87`);
`procrpc.cpp:170` and `control.cpp:169` are switch rows mapping that enum to a status string, and
gremote's lane answers the `gremote` Python client, not the bridge. So `Processes::kill` reaches
both programs for free and the two rows become vestigial. **Moves libexec**, so `gaston`,
`gremote` and `execcheck` all move. The check is `execcheck.cpp:503-505`
(`kill-refuses-a-dos-session`); `tests/python/guest/test_guest_execjob.py:46`'s `CASE_COUNT = 29`
moves only if a case is added. `bridge/prompts/tools.toml:424` carries the `dos_session` sentence
the agent reads and is in scope.

**E9 — §17's other two residues.** A client reporting no shells runs every command bare and
silently: the bridge tells the agent and logs, the client logs a missing `tools_detect.ini`. And
the exit-code wrapper's two silent-skip cases get a verdict — skipped (no fs lane, no `temp`
header, a failed `put`) and no verdict returned (output cap, shell died). `execwrap.py`,
`guest_exec.py`, `win/remote/jobops.cpp` (**moves gremote's image**), `gremote/ops.py`. Tests
that pin today's shape: `test_execwrap.py:97-124`, `test_guest_exec.py:162-214`. The
interactive-DOS-prompt hang stays unfixed and stays recorded.

**E10 — `ocr(rect)`.** A tool beside `shot` calling the same `ui.shot`; Tesseract shelled out as a
system binary (`pyproject.toml` has no `[project]` table at all, and `tools/vm/watchvm.sh:19`
already does `command -v tesseract`); the degrade copied from `uiview.py:127-131`, whose docstring
at `:124-125` states it as policy — *"None rather than an exception when Pillow is missing: a
bridge without it should say so and carry on, not fail the tool."* Plus `categories.py:718` under
`SCREEN_SEE` and prompt text in `prompts/tools.toml`. `PLAN.md:1748` and `:1752` already fix the
ladder position and the parameters, so nothing there needs correcting except naming MSAA as the
second rung — and `PLAN.md:1840`'s table has **no status column**, so no row gets "flipped to
built". **Updates §28's bullet** (`BACKLOG.md:2709`), which says OCR "would be" on the bridge.
**Reds `tests/python/agent/test_tools_surface.py:230`** — `on - off == set(SCREEN_TOOLS)` is
asserted exactly, with `SCREEN_TOOLS` at `:183`; it moves in this commit.

**E11 — the logoff measurement, and whatever it decides.** **Setup the plan previously missed:**
gremote's `RunServices` key was removed from the QEMU guest after the 2026-09-27 run
(`docs/measurements/gremote-vm-20260927.txt:175-199`), so the run needs
`REGEDIT /S C:\GASTON\GREMOTE.REG` reinstated or gremote started by hand after logon. Then: log
off at the console, see whether 7401 answers, read `GREMOTE.PRV` — `endSessionKind()` is in
libcore (`app.cpp:127`) so both programs print the bit. **If the port survives, the commit is
that finding** and no code is written. If it stops, the decision is **a libcore `App` change**,
not a gremote one: `HiddenWindow` is a private nested class with a non-virtual handler
(`app.h:230-242`), `WM_ENDSESSION` is *sent* so `preTranslate` never sees it, and `app.cpp:256`
quits on a foreign session end **by design** (`app.h:81`). So the question is whether `App` grows
a hook letting a service process decline a foreign session end — taken after the measurement.

**E12 — §23 item 4: `ConnectionService::signalStop()`.** The entry says "worth doing when somebody
is in that file anyway", and names the cost (`BACKLOG.md:2468`): splitting `rc_net_stop` into
signal and join halves means reordering the three phases `net.cpp` documents at length as the
thing that was wrong before — clearing `running` first silently threw away every ack the RPC
worker's drain composed. **With `wirecheck` watching**, as the entry asks.

**E13 — the Options leftover that is plain work:** the Tools page's Add becoming one form with a
Browse for the marker file. **The command-log item is not in this commit and not a repair:** the
`.gaston-rc` sentinel exists only at `execwrap.py:74`, the command log is written on the guest
chunk by chunk with nothing buffered (`cmdlog.h:1-13`), so stripping it guest-side would make the
client learn a bridge-side wrapper convention — against "the guest transforms nothing" — and
could not work anyway across a chunk boundary. It becomes a bridge-side design question in E16:
keep the verdict off the command's stdout.

**E14 — two measurements.** Large Fonts: the Options sheet's 230x170 DLU was computed, never seen;
reboot the VM at large fonts and look. And §11's 04.35, the first tool call being the write that
commits the folder, measured against the 4.18 acceptance run as the baseline.

**E15 — the rulings land in `BACKLOG.md`.** The three rulings, the five confirmations, the four
watch conditions, §11b's narrowing, "off is instant" and the command-log fix as recorded work,
and the stale cites the reviews found: §7's `main.cpp:1822` (the file is 326 lines; the mutex and
`FindWindowA` are at `:139-141`), §9's `:140/:78/:88/:1391`, §10's `:228/:230/:651`, §11 and §28's
bullets. Two sweep hazards to clear or the exit condition fails its own check: **§7's
"someone has to decide what a mirror MEANS"**, its three options and its "To close it: pick one"
are all **un-struck** below the 2026-10-10 answer, and **§5's "Three remain:"** stands above three
struck-through bullets. And **§9 is only one-third confirmed** — `scope` and `max_nodes` are
shipped schema properties (`tools.py:474,478`), but the entry names three items; the second ("should
happen before the *second* Phase 6 tool lands") has been passed by five shipped tools and gains a
sixth in E10, and the third ("no bar state for the agent is driving") is stage 4b's, which E15
must say. Then the exit condition is **verified by a sweep, not asserted**.

**E16 — docs: stage 4 closes**, and `stage4b` gets its one-paragraph stub in `ROADMAP.md` with
what the reviews established. `PROGRESS.md`'s current goal moves on.

---

## Verification

**Gates:** `make -C win check`, `make lint`, `make test-fast`, `make wine-smoke`,
`pytest tests/python/remote -q`. Build only what changed.

**Two caps, not one.** `COMMITTED_CAP` 498,425 against 496,377 (`win/Makefile:186`) — 2,048 bytes,
but only **204** before a 512-byte code step and **1,456** before `_BSS` slides a 4,096-byte page.
And `check-gremote` 263,593 against 259,497 (`win/Makefile:962`) — exactly one page. The
image-moving commits are **E4, E5, E6, E8 (libexec → three exes), E9 (gremote), E12 and E13** —
E3 moves nothing. With the indicator out, a raise is no longer certain, but `check-gremote` after
E8 and `check-gaston` after E5 are the two readings that decide it; the raise goes in the commit
that needs it, with the measurement in the message.

**Tests that move with the commit that breaks them:** `test_tools_surface.py:230` with E10;
`test_execwrap.py` and `test_guest_exec.py` with E9; `execcheck.cpp:503` with E8;
`test_guest_rpc.py`'s named verdicts with E4. `test_mnemonics.py:104-116` parses `AppendMenuA`
and `bar.cpp` only, so E5's and E6's new menu items are unguarded either way — worth knowing
rather than relying on.

**On the guest, not under Wine.** The automation menu item toggling, with the tick honest when the
mid-turn prompt is declined. "Clear cached history" against a real `CACHE` tree, and the orphan
sweep against a conversation holding a `P###.TMP`. `ocr` against a real screenshot **and** against
a bridge with no Tesseract. `kill` on a console program, confirming the VM is reclaimed.

**Three runs that are the commit, not a check on it:** the logoff (E11, after reinstating
`GREMOTE.REG`), Large Fonts and §11's folder measurement (E14).

**The Athlon is not needed.** Ratifying §8 removed the only item that wanted it. It stays off.

---

## Risks

1. **E15 is what the stage is judged by and the easiest to do shallowly.** Writing "decided" into
   ten entries is not the sweep that verifies it — and two of the three claims this plan had to
   retract were claims about what `BACKLOG.md` says. Read the entries, do not grep them.
2. **E5 is the only commit that deletes anything.** The sweep removes only files whose names say
   what they are, and nothing keyed on a conversation's staleness survived review. "Clear cached
   history" keeps `FOF_ALLOWUNDO`. The backup rule applies to a real `CACHE` directory first.
3. **E8 moves libexec, so one edit moves three images** including gremote's, which has one page of
   slack. Measure `check-gremote` before assuming it fits.
4. **E11 can answer with nothing to build**, and that is a success. The temptation is to write the
   `App` hook anyway; §35 is what that looks like when it ships unverified.
5. **Sixteen commits, three of them measurements.** Cutting any of E15's rulings means the exit
   condition does not hold, which is the one thing this stage exists for.
