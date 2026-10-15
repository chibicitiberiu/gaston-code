# Stage 5 — the P200, and the optimisation it justifies

## Context

Every performance number this project has is from an emulator or from a 2002 Athlon.
`docs/ROADMAP.md:46` states the exit condition in one line: **the hardware questions are answered
from hardware.** Stage 5 is not a feature stage. It is where a physical Pentium 200 answers what
nothing else may answer, and where one optimisation is decided by the machine it exists for.

The box: Pentium 200 MMX, Win98 SE 4.10.2222 A, 32,284 KB, 800x600 at 24bpp, AutoPatcher applied
(`docs/measurements/p200-run1-20260926.txt:1-2`). It is **not** the Athlon XP 2000+ at 10.0.0.125
that has been driven over gremote since 2026-10-01; `docs/REMOTE.md:1145-1154` and
`docs/BACKLOG.md:83-88` both exist because most `docs/measurements/*-p200-*.txt` files are that
other machine, and reading a filename as a machine is how this gate came to look half-closed when
nothing had run on it.

**Two facts the user settled before this plan, and they change its shape:**

1. **The box already has a NIC with TCP/IP configured.** `docs/gremote-plan.md:254-255` is the clean
   statement of what was unknown — *"Two preconditions nothing in this tree records: whether that
   box has a NIC and a configured TCP/IP stack"* — and `BACKLOG.md:77-81` keeps it as the one thing
   standing. So bring-up is one floppy for gremote, and after that the box is driven from Linux over
   port 7401 exactly as the Athlon is.
2. **The box stays powered and accessible.** A campaign, not an expedition: builds can be shipped
   and re-shipped, and a measurement can be re-run.

What that buys is that most of the stage becomes a Linux-side script. What it does not buy is the
five things needing eyes or a hand — the tray icon, the standby timer, a Start-menu shutdown, the
no-gremote memory baseline, and the injection battery — grouped into two sittings below.

Stage 6 is what this unblocks: a from-scratch install by somebody who is not us.

---

## What review killed

Two passes ran against the tree. The first falsified nine claims, the second twenty-two more, and
**every one was then opened and read before being accepted** — a plan built on an agent's summary
is a plan built on a summary. These are the ones that changed what gets built; the line-number
slips are folded in silently.

| was in a draft | why it is wrong |
|---|---|
| the `-ot` comparison is **three** builds, as `flags-vm-20261011.txt` ran it | **two.** That file's arms were `otexan` (*"as shipped: -otexan default, six -os overrides"*, `:16`), `all-os`, and `os-but-hot`. `win/Makefile:813-815` records that the default was **inverted on 2026-10-11** — so `os-but-hot` **is** HEAD, and `otexan` is a layout that was voted against and deleted. Re-creating it would re-ask a settled question. The stage's own question is whether `-ot` earns its bytes in the three hot modules, which is exactly HEAD against `all-os`, measured A/B/A. |
| three builds come from `BUILD=` alone | `BUILD ?= $(TOP)/build/win` (`:15`) gives separate output dirs, but `-os` is hardcoded in `CFLAGS` at `:51` and the hot-module override is unconditional at `:859-860`. One knob — `HOT_OPT ?= -otexan` — makes `all-os` a command-line switch. `OPT` is not needed, because the base default is already `-os`. |
| `gremote.ship.self_update()` installs each new build | **`GUPDATE.EXE` is not on the floppy.** `tools/gen/mkw98kit.sh:69-77` stages nine files and that is not one of them; `win/remote/control.cpp:27,357-361` refuses `UPDATE` with `no_updater`. Without it the first build on the box is the last until somebody walks over — and `flags-vm-20261011.txt:60` says each build was installed with `self_update()`. 48,128 bytes against ~800 KB of floppy still free. |
| the TIME_WAIT item is "kill gremote and restart it" | **there is no remote `QUIT`**, deliberately (`REMOTE.md:604-614`), and since `RunServices` runs at **boot**, a remote kill strands the box. `REBOOT` restarts the stack, so it cannot leave a TIME_WAIT. **`UPDATE` is the only instrument**, which makes it depend on the floppy fix. |
| the client is on the box already | **nothing puts it there.** The kit has no `GASTON.EXE`; the client's install path is the ISO (`tools/gen/mktools-iso.sh:230,264,268`) and the box has no CD. But `Remote.ship()` (`gremote/client.py:547-556`) exists precisely for this — it encodes the locked-exe and single-instance traps — so the client goes over the wire. **W1's `wsprobe`/`syncprobe`/`viewprobe` and S2 all need it, so it is its own commit.** |
| `focusprobe.exe` is shipped with `put` and started with `launch` | `launch` returns a pid and captures nothing (`client.py:306`), and `focusprobe.cpp:21-22` says *"Output goes to the console… a screenshot is the transport."* It needs `exec` with a redirect and a `get` — and two visible Notepads with one covering the other, or it bails. |
| `viewprobe --live` | **there is no `--live`.** The flags are `--hold`, `--replay-fence`, `--cadence` (`viewprobe.py:192-205`). §31's instrument is `--cadence` plus a long-page corpus, which has to be parameterised. |
| `cancelprobe.py --remote` is a flag | it is a rewrite. It is a **bridge stub** speaking the client's wire protocol — `PR.ui_tree`, `PR.ui_cancel`, `PR.ui_shot` off a listening socket (`cancelprobe.py:41,67,84-109`) — and gremote's lane is a different API on a different program. It also measures **gremote's** desktop worker rather than the client's. Same mechanism, same hardware, different process, and the file must say which. |
| `wsprobe.py` covers `REMOTE.md:1235` | it covers a third of it. Its docstring is *"what the client holds in physical memory: **one build against another**"*, `--old`/`--new` are both required, and it measures an **idle** client pointed at a dead port. The bullet asks for **gremote**, **under load**, with and without the client. The idle half is free; the under-load half needs its own driver. It also `get`s `C:\GASTON\TOOLS_DETECT.INI` unguarded at `:66`, so it throws on a box with no client. |
| gate 0.5c closes on the warm-pass rerun | the warm pass fixed the **ordering** bug only. `probe.c:477-483` indicts its own corpus — one two-line block repeated, ~164:1 — and asks for *"a realistic corpus (the project's own prose and code, which measured 2.16:1) read from a file next to the exe."* And the corpus is capped at **256 KB** by `static char src[256 * 1024]` (`probe.c:474`); growing it costs committed-at-load bytes on a 32 MB box, so the corpus is sized to the buffer. |
| `TEST.BAT` answers four of `UIAUTOMATION.md:387`'s five | two and a half. **`probe evlat` discards the system's timestamp on purpose**: its callback is `lat_proc(…, DWORD ms)` (`probe.c:2222-2225`) and `ms` *is* `dwmsEventTime`, voided in the next line. `PROGRESS.md:100` says the figures are pump latencies on a 55 ms tick — *"Do not budget against them."* The fix is one line, but both clocks are `GetTickCount`, so a one-tick quantum survives it. And **`probe imgfind` caps its scan at `sizeof(shot)/stride` rows and stops at the first hit** (`probe.c:3598-3601`), so a template that is *absent*, over the full height, has never been timed. |
| §26 closes by building `UICANCEL` for `UITREE`/`UISHOT` | **already built.** `win/ui/uiengine.cpp:182-197` polls `link.stopFlag()` per node, `win/ui/uirpc.cpp:129-150` starts a still-queued cancelled request already stopped, and `BACKLOG.md:3090-3091` says *"the flag reaches all three ops there."* The entry's opening is stale against its own body. The P200 decides only whether the cost was real. |
| gremote's memory is a `PS` reading | `Process` carries pid, ppid, threads, exe (`gremote/ops.py:19-30`) and nothing else. Every figure here is a `GlobalMemoryStatus` differential — so **the no-gremote baseline cannot be taken over the wire**, and is a keyboard reading. |
| the sleep toggle persists, so no restart is needed between runs 2 and 3 | **right conclusion, backwards mechanism.** `config.cpp:179-180` is explicit: *"The menu is deliberately NOT persisted… The menu is 'for now', the ini is 'from boot'."* The `prevent_sleep` write at `:192` is inside `if (!existed)` — the fresh-install path only. No restart is needed *because* `TrayService::setPreventSleep` (`tray.cpp:421-429`) flips an in-memory flag and writes nothing. |
| the staged INI is fine as shipped | `vm/tools-stage/GREMOTE.INI` is `bind=0.0.0.0`, `allow=` empty, `secret=vm-secret`, `automation=2`. On a LAN that is: anything that reaches 7401 can walk the desktop, type into it and reboot it — and it **boots**, because the refusal only fires on `change-me` (`config.cpp:16`). Permissive and bootable is the bad pair. |
| `C:\GASTON` vs `C:\GREMOTE` is "three places, two answers" | one divergence, one-sided. `REMOTE.md:731-733` is internally consistent — client in `C:\GASTON`, server in `C:\GREMOTE`. It is `mktools-iso.sh:208` sweeping `D:\*.EXE` into `C:\GASTON` and `:262-263` following with gremote's config that disagrees. E1 decides which file moves. |

Two REMOTE.md items the drafts skipped and that cost one `exec` each: **`COMMAND.COM /c` exit codes**
(`:1172-1175`, *"The P200 only needs to confirm"*) and **socket handle inheritance into children**
(`:1176-1178`, *"Wine and NT differ"*). They are not among the six open bullets, but they are
confirmations the box can give for free and they go in W1.

---

## The design

Nothing to design but the order and the honesty of the instruments. Three principles.

1. **Nothing untested reaches the box.** Every probe change and every driver is rehearsed against
   `retroclaude-win98` first. The P200 is 20x slower and in another room; the September visit
   already lost step 18 that way.
2. **Each session end pays for more than one item.** Four in the whole stage, each named below.
3. **Every file is `docs/measurements/<name>-p200-<date>.txt` with the machine in its own header**,
   not in the filename. That is the lesson of `BACKLOG.md:83-88`, and it is why the one P200 figure
   this plan leans on — `PROGRESS.md:106`'s 11 ms naive / 1 ms filtered template scan — is treated
   as a summary rather than a measurement: **no file in `docs/measurements/` carries it.**

### What must be built before the box is touched

| what | where | why it cannot wait |
|---|---|---|
| `GUPDATE.EXE` and `FOCUSPROBE.EXE` on the floppy, plus the corpus | `tools/gen/mkw98kit.sh` | no updater means no second build and no TIME_WAIT instrument |
| the install path settled | `mktools-iso.sh:208,262-263` vs `REMOTE.md:733` | the box is where it gets expensive |
| a P200 INI | a real `secret`, `allow=` narrowed to the dev host | the shipped one is open on a LAN and boots anyway |
| `probe deflate` reads a corpus from disk, **≤256 KB** | `probe.c:472-483` asks for this itself; `:474` sets the ceiling | gate 0.5c cannot close otherwise |
| `probe evlat` uses the `ms` parameter it already receives | `probe.c:2222-2233`, naming which of the three hook sites it touches | otherwise one of the five questions has no instrument |
| `probe imgfind` times an **absent** template over the full height | `probe.c:3598-3601` | the present-template case is the only one anybody has |
| `HOT_OPT ?= -otexan` | `win/Makefile:51,859-860` | otherwise `all-os` is a hand edit, not a switch |
| `tools/probe/flagsprobe.py` | new | the round trip was done by hand |
| `tools/probe/memprobe.py` | new | the **under-load** half of `REMOTE.md:1235` has no instrument at all |
| `cancelprobe.py`, a gremote-lane mode | rewrite, not a flag | §26's number |
| `syncprobe.py`, a **scan-only** timed repeat-pass mode | its `run()` calls `dos()`, which is QEMU-only (`tools/vm/dos.py:22-45`) and cannot execute against the P200 | §2's timed scan, §4's re-read |
| `viewprobe.py`'s growth corpus parameterised for a long page | `viewprobe.py:192-205` | §31, which no draft scheduled |

`flagsprobe.py` reproduces a specific method, not a general idea (`flags-vm-20261011.txt:55-72`): a
put and a get of **377,990 bytes** of this project's prose and code, 2.59:1 compressible, **led by a
NUL so the fs lane classifies it binary and the EOL fold does not rewrite it**; five of each;
median; and the first arm measured again at the end, because one pass cannot tell a regression from
a drift. The shape to reproduce in rehearsal is the compressing direction, the only one that moved:
`os-but-hot` get 0.182 against `all-os` 0.219, with the baseline at 0.176/0.178 either side.

### The bring-up

One floppy, carried once, after `make -C win probe hookdll autocheck gremote gupdate focusprobe` —
the kit is ~556 KB once rebuilt (the `build/w98kit/` staging on disk is a stale 739 KB), and with
`GUPDATE.EXE`, `FOCUSPROBE.EXE` and a 256 KB corpus it lands near 900 KB of 1,474,560. The script's
own size check prints the real number (`mkw98kit.sh:111-117` sums the staged files, which is what
goes on the disk — the zip is transport only), and its CRLF check matters because `COMMAND.COM` will
not run a `.BAT` with bare LF and that failure lands on whoever is at the Windows machine hours
later.

At the box: `MD C:\GASTON`, copy, `REGEDIT /S GREMOTE.REG`, and the secret and `allow=` typed in by
hand — the one secret-bearing step, and it does not travel on a floppy that will sit on a desk.

Then, before anything else:

1. `PROBE.EXE platform` — **free memory with nothing of ours running**, the one number that cannot
   be taken over the wire.
2. `TEST.BAT` parts one and two, steps 1-17. **Not part three**: `tools/gen/w98kit/TEST.BAT:90-100`
   says those three *"have killed Explorer once, and one session in September ended with this
   machine spinning at full speed"*, so *"they run LAST and on their own."* The only prior baseline
   is four months old and missing step 18, because `AUTOCHK.EXE` crashed on a 64 KB stack local
   over the guard page (fixed in `2e46555`) — so this is the first complete one, run by hand because
   the comparison against `p200-run1-20260926.txt` is worth more than the convenience.
3. Reboot. Before anybody logs on, `GREMOTE.LOG` must say
   `tray: no notification area yet (no shell is running)` (`tray.cpp:235`) **and** Linux must
   already reach `sysinfo()`. One reboot, two answers.
4. Log on; the icon appears — `TaskbarCreated` reaching a window created before the shell existed.
5. The **HELLO inventory** verbatim (`win/caps.h`, `bridge/caps.py`): the box's own
   self-description, which 0.13 was asserted from on two other machines.

Everything after this is driven from Linux.

---

## The run order

**K1 — first contact, at the keyboard.** The bring-up. Answers: the no-gremote memory baseline; the
first complete `TEST.BAT`; gate 0.5c; the walk in probe form; the tray's pre-logon half. Boot #1.
`TEST.BAT` does not run `msaacost` or `msaafast` — the two that produced `p200-run2-20260926.txt`,
where `AccessibleChildren` beat `IEnumVARIANT` 2078 against 2555 µs/node — so those go in by hand
or over `exec` later.

**C1 — the client, over the wire.** `ship()` `gaston.exe`, and `put` `gaston.ini` repointed at the
dev host, `view.htm`, `spin.gif` and `tools_detect.ini` (`win/tools_detect.ini`, which has no 8.3
spelling and so must be written out in full). Without this, W1's three stub-based probes and S2 have
nothing to measure. `ship()` is the right tool because it already encodes that a running exe is
locked and that a second copy raises the first and exits.

**W1 — the wire session, from Linux, nothing restarts.** Internal order matters, because `wsprobe`
stops and relaunches the client against `C:\GASTON\GASTON.INI` and would pull it out from under a
probe stub:

1. gremote-only work: `r.tree()` with `acc=1` and `acc=0`, timed host-side, against the Athlon's
   36-37 ms (`ui-lane-real-hw-20261004.txt:57,62`); `memprobe` sampling `sysinfo()` while a walk, a
   full-screen `shot()` and an `exec` run, which is the **under-load** half of `REMOTE.md:1235`;
   `cancelprobe`'s gremote mode for §26; `focusprobe` via `exec` with a redirect and a `get`, with
   two Notepads staged first; one full-screen `shot()` — 800x600x24 is 1,440,000 bytes raw, and this
   is the first time that payload crosses a real NIC from a 200 MHz compressor; and the two free
   confirmations, exit codes through `COMMAND.COM` and socket inheritance.
2. then `wsprobe`, which owns the client for its run.
3. then the stub probes, one at a time: `syncprobe`'s scan-only timed passes over a populated
   `C:\WORK` (§2, §4 — a timed no-change pass over an empty tree measures nothing), and `viewprobe`
   with a long page (§31). Each needs `GASTON.INI` pointed at the stub rather than the bridge.

**K2 — the keyboard sitting, no reboot.** The 16x16 `IDI_APPLICATION` at `SM_CXSMICON`, which is a
judgement and wants a photo; the menu dismissing on a click outside; `EXPLORER` killed and
restarted, for `tray: the taskbar appeared; adding the icon` (`tray.cpp:605`); **prevent-sleep run
2** with `prevent_sleep=0` and the shortest standby, reading for
`tray: the machine asks to suspend` (`:489`) — silence means no veto can ever help; then the tray
toggle, which arms run 3 *and* is the checkmark-and-`[awake]` check, with **no restart needed
because the menu is deliberately not persisted** (`config.cpp:179-180`, `tray.cpp:421-429`);
**run 3**, where `REFUSED` (`:507`) then `the suspend was abandoned` (`:521`) is the veto working —
and note `:504-508` logs **the first of each kind and then every eighth**, so a reading that expects
one line per ask will under-count; Start > Shut Down > Standby for 9x's rejection complaint; tray
**Exit** for `tray: Prevent sleep OFF (…)` (`:462`), the line the Wine suite structurally cannot
see, plus no ghost icon; and a second `PROBE.EXE platform` with gremote stopped, so the idle figure
is a delta. gremote restarted by hand before leaving. **No logoff** — `RunServices` runs at boot
(`REMOTE.md:1350-1354`), so a logoff costs the box its reachability.

**S1, S2 — the self-asked shutdowns, from Linux.** Pull the logs off the box **first**, because the
recovery is what destroyed the evidence twice. `r.reboot("reboot")` with only gremote up (boot #2),
then with the client too (boot #3). Read `GREMOTE.PRV` — not `.LOG` — for the six lines at
`REMOTE.md:1302-1310`, and `GASTON.PRV` for the client's four. Each missing line names a different
failure, which is the point of having six (`:1326-1334`, which enumerates five discriminations for
the six lines). The last clause is the one to read: `this process stays up to drive the shutdown it
asked for` for a `REBOOT`. The two "it did not happen" lines at `:1322-1324` are findings.

**S3 — the somebody-else-asked shutdown.** Start > Shut Down at the keyboard, both up. Boot #4. The
PRVs must end `nothing below this line is ours` instead, and a `REBOOT` whose log says *that* is a
build without the fix.

**U1 — the update and flags session, from Linux.** Two arms, measured A/B/A: HEAD (`os-but-hot`) and
`make HOT_OPT=-os` (`all-os`), into separate `BUILD=` dirs so no object is shared. Three
`self_update`s, each also one TIME_WAIT reading and one `-verify` reading, free. Then the client
half: `ship()` both builds and re-run W1's sync and view timings.

**X1 — the injection battery, last, at the keyboard.** `TEST.BAT` part three and
`AUTOCHK.EXE --health` behind it, then a reboot whatever it says.

Four session ends, two sittings, one floppy. **`mode=poweroff` is never used** and neither is a
logoff: `BACKLOG.md:3895-3898` measured a failed poweroff landing on the logon prompt with gremote
gone — a box that answers nothing and cannot be told to.

---

## What a result can change

| result | the change it licenses |
|---|---|
| `-ot` earns nothing in the three hot modules | delete `win/Makefile:859-860`. `flags-vm-20261011.txt:25-27` priced whole-program `-os` at **gaston -26,624, gremote -19,456** on the VM; the P200 says whether the 23% is real here |
| it earns its bytes | nothing changes, and the `:813-858` ladder comment gains P200 numbers beside a TCG measurement. The likelier outcome, and the stage is still worth it |
| a cancelled walk visibly delays the next call | §26 closes as **justified**; the live follow-on is whether `max_tree_nodes=4000` suits a slow box |
| it does not | §26 closes as **the problem does not arise**, on the machine it was written about (`BACKLOG.md:3131-3132`) |
| a cold start approaches 4,000 ms, or the bind retry costs real seconds | `DEFAULT_VERIFY_MS` (`gupdate.cpp:132`) or `BIND_RETRY_SLICE_MS` rather than the four-minute policy (`server.cpp:27-29`). `gupdate.cpp:71-74` already writes down that a build which loads and never answers is indistinguishable from a healthy one, and this stage provokes exactly that |
| the sync re-read costs real seconds | §4's cache becomes buildable **with its memory argument intact**: bounded, capped, measured hit rate, and not a path hash, which the entry rules out because a collision returns a confident wrong checksum |
| it does not | strike the "measure the real cost" clause and close the bullet as not worth building |
| the foreground lock behaves unlike the Athlon | the `AttachThreadInput` fallback already in `focus()` (`win/automation/input.cpp:102,150,173`) stops being the untaken path. `BACKLOG.md:1987` pre-commits: *"if it disagrees, the fallback is already in `focus()`"* |
| the shutdowns pass | nothing — they confirm a shipped fix. If one hangs, the stage stops and the hang is the work |

---

## Commits

| | what | tests / artefacts |
|---|---|---|
| **E0** | docs: this plan as `docs/stage5-plan.md`; `ROADMAP.md`'s stage 5 rewritten; the record corrections below | — |
| **E1** | the floppy and the install path: `GUPDATE.EXE`, `FOCUSPROBE.EXE`, a ≤256 KB corpus; `C:\GASTON` vs `C:\GREMOTE` settled in the generator; a P200 INI | the script's CRLF and size checks print the new total; a rehearsal install from the kit on the guest |
| **E2** | `probe.c`: `deflate` reads the corpus and **refuses** rather than falling back; `evlat` uses its `ms` parameter; `imgfind` times an absent template over the full height | `make -C win probe`, `check-sources`, a guest run showing each print something the old one could not |
| **E3** | `HOT_OPT` in `win/Makefile`; `tools/probe/flagsprobe.py` | `make -C win check` unchanged at the defaults — the point of the knob is that the shipped build does not move; a guest run reproducing the 0.182-against-0.219 shape |
| **E4** | `memprobe.py`; `cancelprobe.py`'s gremote-lane mode; `syncprobe.py`'s scan-only timed passes; `viewprobe.py`'s long-page corpus | guest runs of each; cancelprobe against `ui-cancel-vm-20261003.txt` |
| **E5** | **K1**: bring-up, baseline, boot #1, first contact | `bringup-p200-<date>.txt`, `testbat-p200-<date>.txt`. Closes gate 0.5c |
| **E6** | **C1**: the client installed over the wire | `ship()`/`put` transcript in the W1 file; the client answers the bridge from the box |
| **E7** | **W1**: the wire session, in its three-part internal order | `ui-p200-<date>.txt`, `memory-p200-<date>.txt`, `sync-p200-<date>.txt`, `view-p200-<date>.txt`, one screenshot |
| **E8** | **K2**: the keyboard sitting | `tray-p200-<date>.txt`, `sleep-p200-<date>.txt`, a photo of the 16x16 icon |
| **E9** | **S1-S3**: the three session ends | `shutdown-p200-<date>.txt`, three PRV transcripts side by side. The commit that confirms the 2026-10-01 fix on the machine it was written for |
| **E10** | **U1**: the two-arm comparison A/B/A, with the TIME_WAIT and `-verify` readings that come free | `flags-p200-<date>.txt`, `update-p200-<date>.txt` |
| **E11** | the code changes the numbers demand. **Nothing here that a number did not ask for** | `make -C win check`: an `-os` outcome moves `COMMITTED_CAP` and `check-gremote` **down**, ladder comment extended |
| **E12** | **X1**: the injection battery and the health check, reboot after | `inject-p200-<date>.txt`, the `--health` verdict pass or fail |
| **E13** | docs: stage 5 closes — `ROADMAP.md`; `PROGRESS.md` (0.5c at `:80`, gate 1.9 at `:123`, the "Real hardware access" row at `:63`, the current-goal paragraph); `BACKLOG.md` §1b, §2, §4, §9, §22, §26, §31; `REMOTE.md`'s checklist; `UIAUTOMATION.md:387` | — |

## Record corrections this stage carries

Each opened and read:

- **`REMOTE.md:1236`** still writes item 9 (`rsrc32.dll`) as live; `ROADMAP.md:779-781` closed it as
  a no. Six open bullets, not seven.
- **`ROADMAP.md:779`** cites `logoff-vm-20261014.txt`; the file is `logoff-vm-20261013.txt`, which
  `REMOTE.md:1341` spells correctly.
- **`ROADMAP.md:783-784`** says "the six measurements `UIAUTOMATION.md` says only the P200 can
  answer"; `:387-398` lists **five**.
- **`ROADMAP.md:45`**'s table row says 4b is DONE 2026-10-14 while `:593` says 2026-10-15 —
  introduced by `77ae4ee`, and mine to fix.
- **`BACKLOG.md:3900-3902`** says gremote "exits with the session and autostarts on logon". Both
  halves are now wrong: it survives a logoff, and `RunServices` runs at boot
  (`REMOTE.md:1350-1354`).
- **`BACKLOG.md:3060-3062`** says `UITREE` and `UISHOT` "still run to completion", which `:3090-3091`
  and `uiengine.cpp:182-197` contradict. Stale against its own entry.
- **`BACKLOG.md:80`** says the kit stages 356 KB; it is 567 KB rebuilt and will be ~900 KB after E1.
- **`BACKLOG.md:77-81`** and **`gremote-plan.md:254-255`** record the NIC as unknown. It exists.
- **`mktools-iso.sh:208,262-263`** sweeps gremote into the client's directory against
  `REMOTE.md:733`.
- **`PROGRESS.md:106`**'s P200 template figures (11 ms naive, 1 ms filtered) have **no measurement
  file behind them** -- no `imgfind`, `naive` or `filtered` appears in either `p200-run1` or
  `p200-run2`. Either a file appears in this stage or the claim is marked as a summary. The walk
  figures at `:95-97` were suspected of the same and are clean: 0.13e, 0.13f and 0.13g are all
  sections of `p200-run2-20260926.txt`.

---

## Verification

`make -C win check` (six caps) · `make lint` · `make test-fast` · `make wine-smoke` ·
`pytest tests/python/remote -q`. Build only what changed — E2 is `make -C win probe` plus
`check-sources`; E11 is the full six.

**Rehearsed on the QEMU guest, with a pass written down:** the kit installs from a fresh copy and
gremote answers; `probe deflate` reads the corpus and refuses without it; `evlat` prints a figure
that differs from the pump number — **and if it cannot, that is the finding**, because both clocks
are `GetTickCount` and a 55 ms quantum survives the fix; `imgfind` times a miss; `flagsprobe`
reproduces the VM's shape; `cancelprobe` reproduces `ui-cancel-vm-20261003.txt`; the client install
of E6 runs end to end on the guest before it runs on the box.

**On the P200, with a pass written down for each:** gremote answers from Linux after one floppy and
one reboot; `TEST.BAT` 1-17 completes and comes back; the client connects to the bridge from the
box; the icon appears at logon and survives an Explorer restart; an idle standby either asks or does
not, and the log says which; four session ends and four correct last lines; three updates and three
bind readings; two arms and three medians.

**What a green run will not prove:** the box is 800x600, so nothing here answers 640x480; it is one
AutoPatcher-patched install, so anything that works here and not on the stock guest is a two-machine
question before it is a hardware question — the shape `sleep-mechanisms-p200-20261002.txt` used to
clear `SetThreadExecutionState`; and if the injection wedge recurs, causation still will not be
established, only better observed.

**One cheap extra, since the box is there.** Stage 4b's verification says the developer's box is not
a 640x480 P200. One `shot()` with the indicator up during a real step list costs a single request and
closes that caveat at 800x600.

## Recorded, not fixed

- **`tools/gen/w98kit2/` and `w98kit3/` are referenced by nothing** — `mkw98kit.sh:15` reads
  `w98kit` only. The September round-two and second-machine kits, superseded by the box having a
  network. Left as history, noted so nobody edits one expecting it to ship.
- **9x offers no per-process memory this codebase trusts.** Every figure is a differential, which
  attributes a delta to a program by construction rather than by measurement.
- **The corpus choice is a judgement.** 2.16:1 prose and code represents the sync lane, not a 24bpp
  screenshot, which is the only large payload the design actually sends. 0.5c closes on the prose
  number; the screenshot number is read separately from W1's full-screen capture.
- **`win/sync/syncrpc.cpp` logs a scan's entry count, not its duration.** Timing stays host-side
  here; adding a duration is a guest change made to serve a measurement, and belongs to whoever
  next touches the sync layer.
- **`syncprobe.run()` cannot run against the P200 at all** — `dos()` is QEMU-only. Only the
  scan-only mode travels.

## Risks

1. **The shutdown item hung this exact box, twice, and needed two Ctrl+Alt+Dels.** The logs come off
   before S1, because the recovery destroyed the evidence twice before `rc_log_open` learned to
   rename; S1 runs with only gremote up, so a hang has one suspect; and a hang is a **result**, not
   a lost session. Schedule S1-S3 for a day somebody can reach the machine.
2. **`self_update` can take the link away.** It is the mechanism for three installs in U1, and
   `gupdate`'s rollback covers a build that fails to **load**, not one that loads and never answers
   — the case this stage provokes. `GREMOTE.OLD` is always one rename away at the keyboard, and
   `GUPDATE.EXE` is on the floppy from E1. `gupdate.cpp:78-81` notes that `ship.py`'s `self_update`
   does reconnect and report the version, which is the probe `gupdate` deliberately does not
   duplicate — so the host side does notice. Still: not on a day nobody can reach the box.
3. **The NIC is configured but nothing has ever talked to it.** K1 step 3 is the first time. If it
   fails, the stage reverts to the floppy shape every document assumes, and W1, C1 and U1 become
   `TEST.BAT` variants.
4. **E2 can stall the stage**: three changes to a C file only the guest can verify, and 0.5c cannot
   close without the first. The corpus is bounded by `probe.c:474`'s 256 KB rather than by the
   floppy, and 128 KB still clears a 55 ms tick by two orders of magnitude on a P200.
5. **The latency question may not be answerable.** Fixing the clock source removes pump latency and
   leaves a one-tick quantum. If the answer is still 0-or-55, the honest outcome is to strike the
   question rather than ship a number nobody can budget against — which is what 0.15c did last time.
6. **32 MB, and a 1998 platter.** The Athlon and the QEMU guest are both 32 MB, so working-set
   figures should transfer; the disk will not. Expect the first symptom to be the view, not an
   allocation failure — and `BACKLOG.md:172-176` already corrects the identical scare: `dwAvailPhys`
   excludes the cache Win98 shrinks under pressure, and allocations were never actually failing.
   W1's gremote-only part runs before the client arrives, so a problem has a side.
7. **E11 is the only commit that changes shipping code, and it is allowed to be empty.** A stage
   that measures everything and changes one Makefile line is a success. A stage that ships a change
   the numbers did not demand is what "not changed on a guess" exists to prevent.
