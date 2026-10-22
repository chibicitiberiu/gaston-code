# Stage 2 — size, before the thing that spends it

## STATUS, 2026-10-11: COMPLETE

Eleven commits, `6b1bc5d..`, each gated with `make -C win all`, `make -C win
check`, `make test-fast`, `pytest tests/python/remote`, `make lint` and `make
wine-smoke`. Outcome: **gaston 582,697 -> 478,249** and **gremote 365,497 ->
259,497** committed at load, the caps lowered to match (the first reductions in
either ladder), 30 KB less resident on the 32MB guest, and `BACKLOG.md` 2's
flag half, 19 and 29 closed. Verified under Wine, on the QEMU guest -- including
a full agent turn through the client -- and on the Athlon XP.

Two things in the plan below were NOT done, both deliberately and both recorded
where the work is: the `ui/uirec.cpp` split (attempted, declined, `BACKLOG.md`
34) and lowering `check-autocheck`'s cap (left loose on the argument that raised
it the day before). One thing was added: `[ui] msaa=0` had never been
implemented, found while answering BACKLOG 29's second bullet.

`ROADMAP.md` stage 2. Planned 2026-10-11 and reviewed twice before any code: a
fact-check of every figure and citation against the link maps and git, and an
adversarial pass looking for what would break. Between them they corrected three
byte counts, the three committed totals, eight line citations, the `comctl32`
proof, the GUID naming (which would not have compiled), the cmovcheck claim in
trap 1, the float call sites in commit 5, and the whole shape of commit 3's
guard. Those corrections are folded in below rather than listed, except where
the wrong version is worth keeping as a warning.

## Context

`docs/ROADMAP.md` stage 2 exists to pay for stage 3. Stage 3 moves the markup renderer
onto the guest, and `BACKLOG.md` 31 step 2 already predicts that will want the cap raise
§19 defers. The exit condition is "a measured before/after, and room in the caps for
stage 3".

**The real slack is 3,031 bytes.** Recomputed with the gate's own formula
(`win/Makefile:944-954`) over the exes at `01b5254`: gaston **582,697** of 585,728,
gremote **365,497** of 372,000, autocheck **212,328** of 220,000, fscheck 112,628 of
200,000, gupdate 52,496 of 64,000. `PROGRESS.md:4172-4174` already had the first three
within 512 bytes; `ROADMAP.md:159`, `BACKLOG.md:2771`, `REMOTE.md:868-869,887` and
`win/Makefile:837` still quote the pre-clipboard numbers, which is where an earlier draft
of this plan got 580,649 and a phantom 5,079 bytes of room.

The stage as written names three jobs: the flag decision (`BACKLOG.md` 2), the gremote
trim (`BACKLOG.md` 29), and R9 step C5+ (`REMOTE.md:1113`). **Reading the tree found that
most of what those entries propose is already done or was never the lever**, and that a
measurement nobody had taken finds about **69 KB per exe that nothing in the program
needs**:

| | gaston.exe | gremote.exe | why it is there |
|---|---|---|---|
| `uuid.lib`'s GUID table | 31,520 | 31,520 | one module, so one reference pulls 1,970 GUIDs; 1,957 are unreferenced and the 13 that are not cost 208 bytes |
| miniz's ZIP archive APIs | 31,424 | 31,381 | one translation unit; no `mz_zip_*` reference anywhere in `win/` |
| `math387r.lib` float formatting | 6,241 | 6,277 | four `(double)` casts computing a bytes-per-second figure |
| **total** | **69,185** | **69,178** | |

Which lands gaston near **513,500** and gremote near **296,300**, before the flag question
is even asked.

What the three entries got wrong, each with the citation:

1. **BACKLOG 2's two named levers are largely spent.** "The whole client is built with
   `-otexan`" was true on 2026-09-25 and is not now: six override rules at
   `win/Makefile:667,676,685,693,698,706` put `app/`, all of `fs/` but `reader.o`, most of
   `sync/`, `rpc/fsrpc.o`, `automation/steps.o`+`input.o`, `ui/uiengine.o`+`uirpc.o`, all
   of `remote/` and `core/thread.o` at `-os` already. And its largest BSS item,
   "`rc_net_send`'s frame build buffer at 66,560 bytes in `win/net.c`", **moved to the
   heap** when libwire was extracted — `win/wire/connection.cpp:925-933`: *"It was a
   function static -- 66,560 bytes committed at load, the largest static in the image …
   Per connection it is heap."* `gaston.map` now reports `_BSS` at **13,816** bytes with
   no symbol in it above 2,000. The dead figure is repeated in four more places commit 1
   takes with it: `win/Makefile:944`, `docs/mem-audit/README.md:96`,
   `docs/mem-audit/01-measurements.md:133`, `docs/audit/01-guest-c.md:832`.
2. **BACKLOG 29's first bullet was fixed before the entry was written.** It says
   "`win/remote/`'s own sources are not all on" the size list; `win/Makefile:698` is a
   pattern rule covering all of `remote/`, and `git log -S` on that line returns exactly
   one commit — `a3b227b` (2026-09-27), the one that created gremote. What is genuinely
   off the size lists in gremote is `ui/uirec.o` at 2,432 bytes **and `miniz.o` at
   61,141**, the largest module in the exe, with no override rule of its own.
3. **R9 C5+ is done and the doc has not caught up.** `win/app/main.cpp` has **zero
   file-scope static data** (its only file-scope `static` is the function `exeDir()` at
   `:36`, and the map shows no `app/main.o` symbol in `_DATA`, `_BSS` or `c_common`), and
   both callbacks the step names were deleted: `rc_refresh_fn` in `bdd3c43`,
   `rc_turn_busy_fn` in `59a1e65`. The remaining module statics belong to the
   libwire/libexec extraction (`REMOTE.md:1116-1117`), not here.

So the shape is: **measure, delete what the measurement finds, then answer the recorded
decisions with evidence instead of with the premise they were written on.**

**Decided with the user, 2026-10-11:** trim first and answer BACKLOG 2 on the measurement;
lower the caps so the gate keeps biting and stage 3 takes a deliberate raise; verify under
Wine, on the VM, and on the Athlon; the on-demand DLL idea is deferred with its design
written down (commit 8).

---

## Commits

### 1. Docs: what reading the tree found

No code. `docs/stage2-plan.md` written from this file, then:

- **`BACKLOG.md` 2** — the `-otexan` sentence rewritten against the six override rules;
  the 66,560-byte static struck with `connection.cpp:925-933` and the `_BSS` 13,816
  figure, and the same claim corrected at `win/Makefile:944`, `docs/mem-audit/README.md:96`,
  `docs/mem-audit/01-measurements.md:133`, `docs/audit/01-guest-c.md:832`; the 32 KB `-os`
  figure kept but dated, since five of the six rules did not exist when it was measured.
- **`BACKLOG.md` 29** — bullet 1 struck with `win/Makefile:698` and `a3b227b`; bullets 2
  and 3 answered in commit 7; the two items it missed named with their numbers.
- **`ROADMAP.md` stage 2** and **`BACKLOG.md:2771`** and **`REMOTE.md:868-869,887`** — the
  stale committed/cap figures, which is what stage 3's own planning will read.
- **`REMOTE.md:1113`** — C5+ marked done with the two commit hashes, and the leftover
  module statics pointed at steps 2-3 where they belong.
- **The two decisions this stage overturns, named rather than quietly reversed**
  (see commit 10): `win/Makefile:88-101`'s "the page is bought once" policy, and the
  deferred `COMMITTED_CAP` +4,096 at `BACKLOG.md:1953-1954` and `ROADMAP.md:228` — *"Deferred
  to you explicitly."* BACKLOG 19's alternative was "free 126 bytes of CONST or DATA
  anywhere in `gaston.exe`"; the trims free 31,520, so that entry closes on the second
  option rather than the first.
- **`PROGRESS.md:27-30`** still says to use `virsh snapshot-create-as` on this domain.
  `tools/vm/vmsnap.sh:9-16` explains why that cannot work. Point it at `vmsnap.sh`.
- **`make/ow.mk:47,57-58`** is a dead duplicate of the real flags: only the root `Makefile`
  includes it (`:2`) and nothing reads `OW_CFLAGS`. **Annotate, do not delete** — `ow.mk:23-24`
  exports `WATCOM`/`WLINK_LNK` (harmless only because `win/Makefile:26-27` repeats both),
  and it is a recorded Phase 0.2 deliverable with live references at `PROGRESS.md:55,238`,
  `PLAN.md:642,702,2034` and `README.md:44`.

### 2. The instrument, and the artefacts the "after" will need

**Keep the "before" first, because no later commit can.** `build/` is gitignored and
commit 3 rebuilds `gaston.exe` in place, while `research/probes/wsprobe.py:54-55` needs
`--old <path>` to exist. So this commit's first act is to copy `gaston.exe`, `gremote.exe`
and their maps out of the tree, named by hash. `build/win/` is also partially stale right
now (`uicheck.map` is missing although `win/Makefile:434` emits one), so the before-figures
come from a clean `make -C win clean all`, not from what is lying there.

**`tools/check/mapsize.py`**, new: reads a wlink `.map`, attributes each symbol's bytes to
its `Module:` and prints a ranked table. This is what found everything above, and it
belongs in the tree so the next trim starts from a measurement. Two caveats go in its
docstring because both bit while exploring:

- **Attribution is sound in code and unsound in data.** Watcom emits string literals with
  no public symbol, so an unnamed literal run is charged to whichever named symbol precedes
  it. In gaston `CONST` has **no named symbols at all** (51,374 bytes genuinely
  unattributable) and `CONST2` has 16; the largest single gap in the image is **8,764 bytes
  at `0x47eb40` inside CONST2**, charged to a `uiengine.o` literal whose successor belongs
  to `steps.cpp` in the same segment. The tool therefore reports **`_TEXT` only** and says
  so: `ui/uiengine.o`'s 9,169 is code, with roughly 8.8 KB of literals set aside.
- **Clamp each delta to the segment table, not to a page.** The first cut dropped any gap
  over 4,096 bytes as "a segment jump" and so threw away every symbol larger than a page:
  it attributed 374,033 of gaston's 406,591-byte `_TEXT` and lost 32 KB silently. A total
  that does not reconcile with the segment sizes is a bug, and the tool should say that
  rather than print a ranking.

The two 31 KB findings rest on none of this: both are bounded by explicit first and last
symbol addresses with no foreign symbol between, which is why they are the ones being acted
on. Lint applies — `make lint` runs ruff over `tools/` (`Makefile:125`).

Measurement into `research/measurements/imagebytes-host-20261011.txt` for all five capped exes,
as the "before" half of the exit condition. (`-host-` because the directory's convention is
`<topic>-<machine>-<date>` and this one has no guest in it.)

### 3. The GUID tables stop travelling (-31,520 per exe)

`uuid.lib` is one object module, so a single reference pulls all 1,970 GUIDs: 31,520 bytes
in gaston, gremote, uicheck, autocheck, and — by the same mechanism, though neither link
line passes `map=` so it cannot be read off a map — netcheck and rpccheck. gremote and the
check exes pull it for **`IID_IUnknown` alone**, at `win/automation/calldeadline.cpp:92`.

The codebase already has the fix, written down twice and applied twice:
`win/automation/msaa.cpp:25-26` — *"Spelled out rather than linked from uuid.lib, so a
toolchain whose import library lacks oleacc's IID cannot fail the link"* — and
`research/guest/probe.c:566`. `calldeadline.cpp:13-15` spells out `RC_IID_IMessageFilter`
seventy-nine lines above the line that does not.

- **The locals must be renamed, and this is not cosmetic.** The stock names are already
  declared at file scope by the headers: `EXTERN_C const IID __cdecl IID_IOleObject;`
  (`/usr/bin/watcom/h/nt/oleidl.h:1110`), the same for `IID_IWebBrowser2` and
  `CLSID_WebBrowser` (`exdisp.h:1564,1567`) and `IID_IHTMLElement2`/`IID_IHTMLDocument2`
  (`mshtml.h:27152,27466`). A `static const GUID` of the same name after an `extern "C"
  const` declaration is a redeclaration with different linkage — a hard error. So
  `RC_IID_*` / `RC_CLSID_*`, matching `msaa.cpp`'s prefix rather than `probe.c`'s trailing
  underscore, and the plan says which.
- `RC_IID_IUnknown` in `calldeadline.cpp`, beside its neighbour. That clears gremote and
  all four check exes.
- The thirteen OLE and MSHTML GUIDs in `win/app/htmlview.cpp` (`IID_IUnknown`,
  `IID_IOleClientSite`, `IID_IOleObject`, `IID_IOleWindow`, `IID_IOleInPlaceUIWindow`,
  `IID_IOleInPlaceFrame`, `IID_IOleInPlaceObject`, `IID_IOleInPlaceSite`,
  `IID_IWebBrowser2`, `CLSID_WebBrowser`, `IID_IHTMLElement`, `IID_IHTMLElement2`,
  `IID_IHTMLDocument2`). That clears gaston.
- **The call-site form needs nothing.** `guiddef.h:81-91` keys `REFIID`/`REFCLSID` on
  `__cplusplus` alone and not on `CINTERFACE`, so despite `htmlview.cpp:13` defining
  `CINTERFACE` they are still `const IID &`, and `IsEqualGUID` is the inline at
  `guiddef.h:107` taking two references. A local `static const GUID` binds with no `&` and
  no cast at `htmlview.cpp:363,598,599,627,645,680,1074,1131,1153` and at
  `calldeadline.cpp:92`. Checked, so nobody re-derives it.
- **`$(LIBS)` is left alone.** An unreferenced import library costs nothing: `gremote.exe`
  names `comctl32.lib` at `win/Makefile:142` like every other link and does not import
  `COMCTL32.DLL`. (An earlier draft cited the map's "Libraries Used" section for this; do
  not — the two reviews disagree about what it contains, and the import table settles it
  either way.) `research/guest/htmlspike.c:61-67,213,214,227,253` keeps referencing ten of
  these and keeps linking `uuid.lib`; it is a spike and is untouched.
- **The guard is a check-exe case, not a generator.** An earlier draft proposed extracting
  the bytes from `uuid.lib` and comparing in `tests/python/meta`. Both halves were wrong:
  the library is a 16-byte-page OMF archive needing a bespoke PUBDEF/LEDATA walk verified
  by nothing, and `tests/python/meta/test_suite_hygiene.py:16` states that layer "reads the
  tree" and needs no toolchain, which `make test-fast` depends on. Instead: a case in an
  existing check exe (`cppcheck`, `appcheck` or `idcheck`) that links **both** `uuid.lib`'s
  symbol and the local constant and `memcmp`s them, in the established
  `say(name, ok, detail)` + `conftest.guest_verdicts` + exact `CASE_COUNT` shape. It
  compares against the real toolchain at build time, needs no parser, and
  `win/Makefile:800-808` already licenses a check exe carrying weight the shipping exes do
  not. The 31,520 bytes it costs there are the point, not a cost.
- `win/Makefile:780-781` names one of the seven standing cmovcheck hits as *"inside
  uuid.lib's `_GUID_FONTBOLD`"*. That hit goes; the comment moves with it. See trap 1 for
  the hits this commit may *create*.

### 4. miniz stops shipping a ZIP implementation (-~31,400 per exe)

`miniz.c` is one TU, so wlink takes all of it: gremote's largest module at 61,141 bytes.
The archive block is contiguous and has no external reference, and
`grep -rn "mz_zip\|tdefl_\|tinfl_" win/ --exclude-dir=third_party` is empty.

Add `-DMINIZ_NO_ARCHIVE_APIS` to `win/Makefile:53` beside the `-DTDEFL_LESS_MEMORY=1`
already there. The guard spans `miniz.c:3049-7833` (to the last line of the file);
`mz_crc32` is defined at `:95` — `:70` is the dead variant inside `#if 0` — and
`mz_deflateInit` `:192`, `mz_compress2` `:319`, `mz_inflateInit2` `:372`, `mz_uncompress`
`:593` are all far outside it, so the sync scanner's CRC pass and gremote's `deflate=1`
link are untouched. Nothing in lines 1-3048 reaches into the guarded part.

- **Drop `MINIZ_NO_STDIO`**, which an earlier draft scheduled a measurement for. Every one
  of its twelve guards in the `.c` is already inside `3049..7833`, so it is worth exactly
  zero there. The only thing it still does is stop `miniz.h:567` including `<stdio.h>` and
  `:171` including `<time.h>` for the six TUs that include the header — `sync/scanner.cpp`,
  `sync/manifest.cpp`, `rpc/fsrpc.cpp`, `remote/fsops.cpp`, `wire/connection.cpp`,
  `checks/probe.c`. None leans on either, and the two that use stdio include it themselves.
  Say that in the commit message so the next reader need not check.
- The PNG writer (`miniz.c:2144,2201`) is **outside** the guard and survives. It is
  unreferenced either way and is not part of the 31 KB; do not claim it.
- `$(MINIZ)` is an object, not a library, so `win/Makefile:373-376`'s "an unreferenced lib
  in `CHECK_OWNED` makes cmovcheck fail" does not apply here.

### 5. The float formatter, and the mechanism is known

`math387r.lib` contributes 6,241 bytes to gaston and 6,277 to gremote — `_EFG_Format_`,
`__cvtld_`, `strtod_`, the long-double conversion set — to format one bytes-per-second
figure. There are **four** call sites, not two:

- `win/wire/connection.cpp:349,360` — `(double)deflateIn * 1000000.0 / deflateUs`
- `win/rpc/rpc.cpp:245,256` — the same idiom, and `rpc/rpc.cpp` is in `CLIENT_SRCS`
  (`win/Makefile:219`), so it is in gaston, netcheck and rpccheck but not gremote

Rewriting only `connection.cpp` would leave gaston's `math387r` in full and return a false
negative. All four move to 64-bit integer arithmetic behind one helper.

**And the hedge an earlier draft carried is unnecessary**: `build/win/gupdate.map` and
`fscheck.map` both contain `_vsnprintf_` and `clib3r.lib(prtf.c)` and **no `math387r`
module at all`, because Watcom reaches the formatter through the `___EFG_printf` hook. Float
formatting is pulled by float *use*, not by `printf`. So the 6.2 KB is expected to go, and
if it does not, that is a finding worth chasing rather than a shrug. The 64-bit divide pulls
`__U8DIV` from clib3r, a few hundred bytes, so net it off. `probe.c:522`'s `%.2f` is the one
float format left in the tree and keeps `math387r` in `probe.exe`, which has no cap.

**No test covers this arithmetic.** `deflateBps` reaches the bridge's compression chooser
through the `STAT` `deflate_bps` header, and `tests/python/wire/test_compress.py:87-97` feeds
that synthetic values — an overflow or a divide-by-zero would silently turn compression off
on a slow link. So this commit adds a `wirecheck` case for the helper: pure integer maths,
trivially testable, and `test_guest_wire.py:28` pins the count exactly.

### 6. The flag decision, answered (`BACKLOG.md` 2)

Now that the cap room does not depend on it, the question can be answered honestly.

**The size half.** Still `-otexan`: all of `core/` but `thread.o`, all of `net/`, all of
`rpc/` but `fsrpc.o`, `sync/scanner.o`, `fs/reader.o`, all of `wire/`, all of `exec/`, all
of `automation/` but `steps`/`input`, `ui/{uiservice,uirec,inject}.o`, every `checks/`
object, `inproc/`, and `miniz.c`. Build with `-os` substituted globally, record the delta
per exe with `mapsize.py`, and expect a much smaller number than BACKLOG 2's 32 KB.

**The speed half belongs on the guest and nowhere else.** `CLAUDE.md`: *"Verify on the
guest, not under Wine. Wine proves the code, never the platform."* A `-os`/`-ot` call taken
from a Wine timing is precisely what that rule exists to stop, so Wine's only job here is
"it runs and produces output". On the VM, for the three things that are genuinely loops:

- `probe deflate` for miniz — it exists (`research/guest/probe.c:472`), discards a warm pass,
  and documents why its corpus flatters the ratio.
- The sync scanner's CRC pass, timed from the bridge as `SCAN` sent to `MANIFEST` received
  over a fixed corpus, so no guest instrumentation is needed. Expect the platter to
  dominate; if it does, that is the answer.
- `automation/select.o`'s matcher, kept at `-otexan` because *"the matcher runs over every
  node of every scan"* (`win/Makefile:682-683`): a `tree` call on a busy desktop, timed
  from the host over gremote.

**Building "both ways" needs two object trees.** `miniz.o` is shared through `$(MINIZ)` by
gaston, gremote, probe, netcheck, rpccheck, wirecheck and synccheck, so one `build/` cannot
hold both variants. Use a second `BUILD=` tree, and keep the A and B exes side by side for
the timed runs rather than rebuilding between them.

The likely answer remains "`-os` by default, `-ot` where a timed run earns it", but it is a
measurement now. Record it in BACKLOG 2 and close the entry.

### 7. The gremote trim's written answers (`BACKLOG.md` 29)

Prose and citations, no code:

- **Bullet 1** was fixed before the entry was written (`win/Makefile:698`, `a3b227b`). What
  is left off the lists is `ui/uirec.o` (2,432) and `miniz.o` (61,141), plus library members
  shared with gaston: `wire/connection.cpp` 9,696, `automation/select.cpp` 4,688,
  `core/inifile.cpp` 4,224, `automation/tree.cpp` 3,823. Commit 6 measures them.
- **Bullet 2 — gremote links all ten libautomation members, and `HELLO msaa=0` cannot
  change that.** `msaa.o` is pulled twice over, both unconditionally: `UiContext` holds
  `MsaaSource` **by value** (`win/ui/uicontext.h:30`, constructed at
  `win/remote/uiops.cpp:280`), and the handshake calls `rc_msaa_present()` to fill the
  header (`win/remote/server.cpp:155`) on the reader thread, where no context exists. It is
  **3,280 bytes**, the smallest item on the entry's own list by an order of magnitude.
- **Bullet 3 — the `-xs` cost is not per-TU and not the lever it looks like.** ~16.9 KB of
  runtime paid once, plus ~2.75 KB of `std::exception` hierarchy landed in
  `obj/remote/main.o`. The 2,144 bytes per TU at `win/Makefile:820-822` is a measured
  before/after delta including page rounding, not a multiplier for 47 TUs. The seam worth
  recording: gremote's STL dependency is two `std::vector` out-params,
  `win/exec/process.h:77,123` (the second is the private `snapshot()`, and `process.cpp`
  has six more `vector` locals, so this describes the interface, not the usage). Dropping
  `-xs` for gremote would mean rewriting those, making `operator new` non-throwing and
  removing the `bad_alloc` catches — and the objects are shared with gaston, which needs
  the STL, so there is no per-exe flag. Record it; do not act.
- **Bullet 4** is the working-set number, commit 11.

Plus the two items the entry missed, closed by commits 3 and 4. **29 closes in commit 11**
with bullet 4, not here.

### 8. The DLL option, written down rather than built

Asked while planning: could the automation tier be a DLL loaded on demand? What it would
move is measured — about 49 KB of `_TEXT` per exe (`libautomation` 39,588 + `ui/uiengine.o`
9,169 in gaston; 40,024 + 9,300 in gremote), with each module's read-only data on top and
unattributable (commit 2's caveat). A `BACKLOG.md` entry, not a commit, and it leads with
what the idea does *not* buy:

- **On Win98 code is demand-paged from the image**, so code nobody touches is already
  nearly free. This project measured it: `research/measurements/workingset-vm-20261004.txt:30-32`
  — 66 KB of image cost 36 KB resident *"because the sheet's pages and the inspector are
  code that is not touched until Options is opened."* The gain is against `COMMITTED_CAP`,
  and `win/Makefile:103-106` already says that gate is a proxy for the working set.
- **The seam is not a C ABI.** `win/ui/uiengine.h` publishes C++ across the line:
  `rc_ui_tree` takes a `UiLink&` the caller implements and a `UiContext*` holding
  `MsaaSource` by value, and `TreeSink` heap-allocates its own batch buffer. Across a module
  boundary both sides carry their own `operator new`, which is the bug `core/heap.cpp`
  exists to prevent. Doing it properly means opaque handles, caller-supplied buffers and no
  exception crossing.
- **It fights gremote's update path.** `gupdate.exe` replaces one image and rolls back by
  renaming one file (`win/remote/gupdate.cpp:60-65,260-262,425`). A new `gremote.exe` beside
  a stale DLL loads, stays up and fails the first `ui` request — the failure `BACKLOG.md` 22
  records as *not* covered, on the box reachable only by CD.

**And the link-granularity version of the same instinct is probably net-negative, so it is
measured before it is done.** `ui/uirec.cpp`'s `rc_uirec_inproc` and `rc_uirec_item` are 928
bytes unreferenced in gremote, which refuses that tier (`win/remote/uiops.cpp:397-403`,
*"`inproc` is REFUSED, not ignored"*) — but they are referenced by `win/ui/uirpc.cpp:342,347,362,367`
and `tests/guest/uicheck.cpp:231,242`, and `uirec.cpp` is linked into gaston, uicheck,
netcheck, rpccheck *and* gremote. Splitting them adds a TU to five exes to save 928 bytes in
one, against the ~2,144 this same stage cites per TU. And `uirec.h:32`'s
`#include "inproc/shm.h"` cannot move with them: `RC_IN_DEPTH_MAX` is used at `uirec.h:63-64`
by `RcPathBuilder`, whose `rc_path_reset`/`rc_path_next` *are* referenced in gremote. So
only `RcInNode` would move, and the split is more invasive than it looks. Measure, and
expect to decline.

### 9. `[ui] msaa=0` does nothing, and the INI says it does

Found while answering bullet 2: a documented contract with no implementation.
`win/remote/config.h:86-87`:

> `/* 0 skips oleacc.dll even when it is there: the walk falls back to the window tree and HELLO says msaa=0. */`

`UiConfig::msaa` is defaulted (`config.cpp:32`), parsed (`:137`), written back (`:190`),
keyed in `UI_KEYS` (`:44`) and logged (`main.cpp:261,265`) — and read by nothing that acts
on it. `MsaaSource`'s constructor loads `oleacc.dll` unconditionally (`msaa.cpp:58-60`) and
`server.cpp:155` answers the header from "is the DLL on disk".

- The header half is a one-liner: `m_uiCfg` is already in scope at `server.cpp:158`.
- The walk half must **not** be a constructor argument. `UiContext` declares `UiContext()`
  with no parameters (`uicontext.h:27`) and holds `MsaaSource` by value (`:30`), so an added
  ctor arg ripples into gaston, uicheck, netcheck and rpccheck. A setter or a lazy load
  keeps it local.
- The alternative is deleting the key and the comment. Either is honest; leaving it after
  reading it is not.

### 10. The caps come down

Each cap goes to **the measured number plus a stated cushion**, re-measured from the clean
build rather than subtracted from this file's figures:

- `COMMITTED_CAP` (`win/Makefile:137`), gaston, from 585,728, with the ladder comment's
  "measured 580,649" refreshed.
- `check-gremote` (`:857-858`), from 372,000, same for its "measured 359,609".
- **`check-autocheck` is left alone.** It was raised 212,000 → 220,000 on **2026-10-10**
  with a stated reason (`win/Makefile:876-882`: *"a cap with 184 bytes behind it is a
  tripwire for whoever writes the next line rather than a budget … the thing it is watching
  for is a jump, not a kilobyte"*). Lowering it one day later contradicts that argument
  rather than answering it. It loses 31,520 to commit 3 and the extra slack is the tripwire
  margin that reasoning asked for. Same for `check-fscheck` (112,628 of 200,000) and
  `check-gupdate` (52,496 of 64,000), which the trims do not touch at all: 87 KB and 11 KB
  of slack, noted rather than silently inconsistent.

**The cushion is not "one page", and the earlier draft's arithmetic was wrong about why.**
The gate sums `max(VirtualSize, SizeOfRawData)` per section: for gaston that is AUTO 408,064
and `.reloc` 26,624 (512-aligned raw sizes), `.idata` 7,680, `.rsrc` 7,680, and DGROUP
132,649 byte-exact. The only 4,096 effect is `_BSS`'s page alignment *inside* DGROUP
(`gaston.map:47`: `_BSS` at `0x484000`, the data chain ending at `0x48360e`), where 2,546
bytes of static data already fit free. A code-only trim moves AUTO and `.reloc` in 512-byte
steps and does not touch DGROUP. So the cushion is chosen and stated — 4,096 is a reasonable
number for it — not derived from a page the gate does not charge here.

**This reverses a recorded policy and answers a deferred question, which commit 1 says out
loud rather than leaving to be discovered.** `win/Makefile:88-101` argues the opposite case
("the page is bought once… the feature after it lands on the same boundary and somebody goes
hunting again"), and `BACKLOG.md:1953-1954`/`ROADMAP.md:228` carry a `COMMITTED_CAP` +4,096
the user explicitly deferred. The trims make that raise unnecessary — BACKLOG 19's own second
option was "free 126 bytes of CONST or DATA anywhere", and this frees 31,520 — so the entry
closes rather than being overruled.

Four prose sites quote the old caps and move with them: `ROADMAP.md:159`, `BACKLOG.md:2771`,
`REMOTE.md:868-869`, `REMOTE.md:887`.

### 11. The runtime number, and the close

- **The client, on the VM:** `research/probes/wsprobe.py --old <the exe commit 2 kept> --new
  build/win/gaston.exe` — the 2026-10-02 method as a tool: each build fresh from its own
  folder, idle, pointed at a dead port, five samples 2 s apart, median, alternated twice. An
  image 69 KB smaller should show as tens of KB less held; the two previous runs tracked the
  image closely.
- **gremote, on the Athlon**, the machine BACKLOG 29 is about. `wsprobe`'s shape does not
  transfer, because there the instrument is the subject: it kills and relaunches the program
  it measures through, and `control.cpp:33`'s `GastonRemoteSingleInstance` rules out a second
  copy. So: `mem_free_kb` off the **HELLO frame** (`server.cpp:141`) at a fixed point after a
  cold boot, one build per boot, old and new alternated.
- `research/measurements/imagebytes-host-20261011.txt` gains its "after" half; a second file for
  the flag timings.
- `ROADMAP.md` stage 2 → **DONE**, `BACKLOG.md` 2 and 29 closed, 19 closed on its second
  option, the DLL entry opened, `PROGRESS.md` written up with the current goal moved to
  stage 3.

---

## Traps

1. **cmovcheck can fail, and not only on the flag change.** An earlier draft said the gate
   "prints hits rather than counting them, so nothing fails". That is true only for
   *non-owned* modules: `tools/check/cmovcheck.py:156-170` puts an owned-module hit that does
   not match the pointer-table pattern into `real` and returns 1. `win/Makefile:770-776`
   explains the mechanism — *"Adding a function slides the data after it and the number
   changes"* — and commits 3, 4, 5 and 6 all slide the image, moving every later literal pool
   including those inside our own objects. **Read the cmovcheck block after each of them.** A
   new benign hit is a comment edit; a new owned hit means widening the `pointer_table`
   heuristic, which is work.
2. **`stackframes.py` hard-fails if an owned `.lib` contributes no symbols** (`:75-78`,
   "owned lib never appears in the map"). A trim that makes a library unreferenced in some exe
   fails loudly rather than silently.
3. **`check-%` is a pattern rule with no cap outside the five.** `make check-netcheck`
   expands the inline python to `tot > ` and raises `SyntaxError`, which is not a pass. It also
   means netcheck and rpccheck have no map, so commit 3's saving there is inferred from the
   object lists and cannot be confirmed; say "inferred", not "confirmed".
4. **`COMMITTED_CAP` is `?=`**, so an environment variable masks a regression. There is no CI
   here, so this is a note to the operator rather than a defect.
5. **A `static const GUID` is a POD** and stays out of the static-constructor gate
   (`win/Makefile:927-938`). Anything with a constructor would fail it.
6. **A flag change moves inlining, which moves stack frames.** `steps.o` at `-otexan` was
   22.5 KB with its helpers inlined into one 6.7 KB `run` (`win/Makefile:681-682`) — the shape
   that merges frames toward `stackframes.py`'s 4,096-byte limit.
7. **The check exes are all `-otexan` for their own objects and share library members** with
   the shipping exes, so a library-level flag change moves `check-autocheck` (7,672 bytes of
   slack) and `check-fscheck` too.
8. **miniz must stay referenced by gremote** (`win/Makefile:373-376`): `Connection`'s Codec
   references it even with compression off. `MINIZ_NO_ARCHIVE_APIS` keeps `mz_compress2` and
   `mz_crc32`, so that holds — verify rather than assume.
9. **Do not rebuild under a running suite.** `make wine-smoke` runs the built exes for about
   five and a half minutes.
10. **The guest run starts from a clean boot and from a `vmsnap.sh` known-good**: killing a
    console program strands its DOS VM, and a guest carrying stranded VMs produced a measured
    BSOD in VMM. `tools/vm/vmship.sh` ejects before inserting, because `change-media --update`
    to the same path is a no-op and has served a stale binary before.
11. **`make/ow.mk` is dead but not removable** (commit 1). `win/Makefile:51` is the live
    definition.
12. **The gaston GUID change can break the product rather than the build**, and three things
    here have no automated cover at all:
    - **The twelve MSHTML GUIDs.** `win/Makefile:60-66` states it: *"The Win32 GUI layer
      (main.cpp, htmlview.cpp, …, ~60%) has no headless runner."* `uicheck` and `autocheck`
      never touch `htmlview.cpp`. A wrong byte in `CLSID_WebBrowser` means `CoCreateInstance`
      fails and there is no transcript at all. Commit 3's `memcmp` case against `uuid.lib` is
      the guard; a real client attach on the VM is the confirmation.
    - **`CallDeadline::QueryInterface`.** `grep -rn CallDeadline tests/` is empty; the class is
      reached only when COM calls back into an installed message filter. A wrong
      `RC_IID_IUnknown` makes the filter stop answering and MSAA calls stop being
      interruptible — on the Athlon, not under Wine.
    - **The bps arithmetic**, which commit 5 adds a `wirecheck` case for precisely because of
      this.

---

## Verification

**Per commit**, the standing gate: `make -C win all`; `make -C win check` (`check-sources`
plus five per-exe gates, each running identity, `imports.py`, `cmovcheck.py`, the
static-constructor audit, `stackframes.py` and the committed-at-load sum); `make test-fast`;
`pytest tests/python/remote -q`; `make lint`; `make wine-smoke`. Per *build only what
changed*, commits 1, 7 and 8 need the Python gates only.

**Under Wine**, which proves the code and never the platform. `make wine-smoke` builds 21
binaries and runs `pytest -m guest`; the named guard for commit 4 exists today —
`tests/python/guest/test_guest_probe.py::test_probe_deflate_round_trips_the_shipping_miniz`
runs `probe deflate` against the shipping miniz. Beside it: `wirecheck` with
`test_guest_wire.py`'s `CASE_COUNT = 27` for the codec and the new bps case, `framecheck`,
`synccheck` for the CRC pass, `uicheck` and `autocheck` — which lose the GUID table and must
still walk and drive, with `test_uirec_agreement.py`'s 57 cases and
`test_guest_automation.py`'s 180 asserted exactly, so a case that stops running fails loudly.
Nothing about speed is decided here.

**On the VM:**

1. A full client attach with a real turn, a sync scan and a screenshot attachment — the MSHTML
   instantiation path commit 3 touches, end to end, which no test can reach.
2. `wsprobe.py` old against new, the number the exit condition asks for.
3. The flag timings from two build trees: `probe deflate`, a timed `SCAN`→`MANIFEST` over a
   fixed corpus, and a `tree` call on a busy desktop.

**On the Athlon over gremote**, the machine `BACKLOG.md` 29 was written about: a
`self_update()` to the trimmed build (its own rollback is the safety net), the ui lane through
`research/probes/uilaneprobe.py` and `clipprobe.py` — which is also the only way
`CallDeadline::QueryInterface` and the MSAA path get exercised with a real `oleacc.dll` — and
the HELLO `mem_free_kb` pairs for gremote's working set. `[ui] msaa=0` is tested here if
commit 9 implements it, since this is the box where MSAA actually answers.

**Every figure in this file was re-derived from `build/win/*.map` and the PE headers at
`01b5254`, and two review passes corrected it**: three byte counts, the three committed
totals, eight line citations, the `comctl32` proof, the GUID naming (which would not have
compiled), the cmovcheck claim in trap 1, the float sites in commit 5, and the generator in
commit 3. Commit 2 re-measures from a clean build anyway, because the before/after is the
deliverable and this file is not the measurement.
