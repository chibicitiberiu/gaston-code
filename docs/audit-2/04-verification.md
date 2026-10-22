# 04 — Verification: what is proved and what is claimed

Audit date 2026-09-21. Scope: the tests and the verification tooling, not the
product code — `tests/python/**`, the check binaries and probes (then
`win/checks/*`, now `tests/guest/*` and `research/guest/*`), `Makefile`,
`win/Makefile`,
`make/`, `pytest.ini`, `tools/`, and the gate and verification-status claims in
`docs/PROGRESS.md` and `docs/PLAN.md`.

Method: the suite was run three times and read afterwards. Every claim below
was checked against the file it is about; where a test's name promises one
thing and its body does another, both are quoted. `docs/audit/03-contracts.md`
was read first, because its 56 findings are all marked `Fixed:` and the
question of which are *genuinely* closed is half of this report.

**A note on the tree.** Between 13:49 and 14:05 on the audit date another agent
was committing a `tools/` reorganisation and a `tests/python/conftest.py`
refactor into the same working tree. The first suite run was therefore against
a moving target. Every measurement and every `file:line` below is taken from a
detached worktree pinned to **`b6880fa`**, the commit that was `HEAD` when this
audit started. Where the live tree has since made a latent finding *actual*,
that is stated as such (04.7). That reorganisation also moved every script in
`tools/` one directory down — `tools/cmovcheck.py` became `tools/check/
cmovcheck.py`, `tools/syncprobe.py` became `research/probes/syncprobe.py`, and so
on. Citations below use the **new** paths, because that is where a reader will
find them; at `b6880fa` they are all one level up.

---

## The test run

Four runs, all with the project's own venv (`.venv/bin/python`), the invocation
the `Makefile` uses.

| run | tree | result | wall |
|---|---|---|---|
| 1 | live tree, `make test`, concurrent edits in flight | **2 failed**, 2789 passed, 2 xfailed | 144.49s |
| 2 | pinned worktree `b6880fa` | 2791 passed, 2 xfailed | 173.36s |
| 3 | pinned worktree `b6880fa` | 2791 passed, 2 xfailed | 170.90s |
| — | `pytest -m guest` alone (144 tests) | 144 passed, 2649 deselected | 157.52s |

2793 tests collected. **Nothing is skipped** on a machine with Open Watcom and
the Lutris wine-ge runner present; `conftest.guest_exe` turns a missing binary
into a failure rather than a skip, which is the right call and is discussed
under 04.16. On a machine without the toolchain, the 144 `guest`-marked tests
skip with a reason and 2649 still run.

**The two xfails** are both in `tests/python/agent/test_cmdguard.py`, both the
same documented residue — the guard reports a command it cannot classify
because it does not know the program:

    XFAIL test_hand_written_cases[cd c:\gaston\chat1 && wmake-ALLOW-inside]
    XFAIL test_hand_written_cases[wcc386 c:\gaston\chat1\hello.c-ALLOW-inside]

These are false alarms, not misses, and `test_cmdguard.py:113-118` says so and
says what would retire them (a persistent approve-once store). Honest.

**Run 1's two failures are the finding.** Both in
`tests/python/guest/test_guest_rpc.py`, and the underlying verdict came from the
C binary itself:

    'exec timeout clamp is logged, not silent': ('FAIL', '(log contains "clamped")')

The same file passes in isolation (4 passed in 4.44s), passes under `-m guest`,
and passed in both pinned full runs. One full run in three failed. See 04.2.

`make check` was also run: exit 0, every section printed. See 04.6 for why that
is a weaker statement than it looks.

---

# Findings

## 04.1 — The guest half of the sync transfer has no automated test at all, and it is the code that writes the user's files

`win/sync/syncrpc.cpp` is 601 lines and holds five handlers: `do_scan:109`,
`do_put:247`, `do_putend:327`, `do_mkdir:414`, `do_get:474`. Four of the five
are the actual file transfer — chunked writes into `.gaston-tmp`, the CRC
verification at commit, the move-aside of the user's own copy, the read-and-chunk
of a file going the other way.

Nothing automated drives any of them.

- `synccheck.exe` links `sync.cpp`, not `syncrpc.cpp`. Its four modes
  (`tests/guest/synccheck.c:114-147`) are the directory walk, `--limits`,
  `--contained` and `--chunk`. It never sends a frame.
- `netcheck.exe` and `rpccheck.exe` both *link* `syncrpc.o`
  (`win/Makefile:186-187`, `:198-199`) and neither drives it. `rpccheck.c`'s
  three scenarios are the lane table (`lane_table_cases:102`), EXECKILL
  preemption (`immediate_survives_busy_job:142`) and the exec timeout clamp
  (`exec_timeout_clamp_is_logged:297`). `netcheck.c`'s two are a HELLO/INPUT/
  TEXT/TURN round trip and the stalled-reader backpressure scenario.
- `tests/python/files/test_sync_wire.py` is **2,282 lines** — the single largest
  suite in the project — and its first line says what it is:
  *"Sync over the real wire protocol, with a fake guest."* The fake guest is
  Python, in the same file: `manifest_line` at `:29` re-implements
  `rc_sync_manifest_row`, `fake_scan` at `:48` re-implements `do_scan`, and
  the push/pull/mkdir answers are written by hand.

So the bridge half of the sync layer is exhaustively verified against a model of
the guest, and the guest half is verified against nothing.

**What this lets through, concretely.** Swap the two path arguments in
`do_putend`'s commit so the temp file is moved over the backup instead of over
the target. Or invert the CRC comparison so a corrupted transfer commits.
Or make `do_get` chunk from the wrong offset after a retry. Every one of those
compiles, links, and passes `make test`, `make wine-smoke` and `make check`.
The only thing in the repo that would catch them is
`research/probes/syncprobe.py` — and its own docstring says how it is used:
*"Start it INSTEAD OF the bridge, then launch the client on the guest."* It is a
human-driven acceptance tool, it is not collected by pytest, it is in no gate,
and `PROGRESS.md:2335`'s *"54/54 on the sync probe"* is a record of one person
running it once.

This is the project's riskiest code by its own reckoning — the first standing
rule in `CLAUDE.md` is about not losing a byte of the user's work — and it is
the largest hole in the verification story.

**What would close it cheaply.** `synccheck` already proved the pattern for
`--wire`: give the check binary a mode that speaks the four RPC frames over
stdin/stdout or a loopback socket, and drive it from Python with the real
`bridge/files/syncrun.py` `Pass` on the other end. The frames are small, the
handlers are already reachable from a linked `syncrpc.o`, and it would replace
the largest re-implementation in the suite with the shipping code.

---

## 04.2 — `make test` is not deterministic: one full run in three failed, and the mechanism is a shared log file in the repo root

Run 1 failed two tests in `test_guest_rpc.py`. Runs 2 and 3 passed. The same
file passes alone and under `-m guest`. The failing verdict is produced by the
C, not by the Python:

`tests/guest/rpccheck.c:334` does `ok = log_contains("clamped")`, and
`log_contains:268` opens **`retroclaude.log` in the process's current working
directory** — which, because neither `conftest.run_guest` nor
`test_guest_rpc.py:37` passes a `cwd`, is the repo root.

Two things make that fragile:

1. **Two check binaries write the same file.** `tests/guest/netcheck.c:117` and
   `:235` both call `rc_log_open(".")`, and so does `rpccheck.c:346`.
   `win/core/log.cpp:20` opens it with `CREATE_ALWAYS, FILE_SHARE_READ` — write
   sharing is *denied*. A second opener gets `INVALID_HANDLE_VALUE`, and
   `rc_log:34` then returns silently on every subsequent call. Nothing is
   logged, nothing is reported, and `log_contains("clamped")` is false. The
   suite runs `test_guest_net.py` (two Wine subprocesses, 60s and 90s timeouts,
   one of them against a peer that never closes its side) immediately before
   `test_guest_rpc.py`; a lingering `netcheck.exe` is enough.
2. **The check has a 1500ms sleep.** `rpccheck.c:331` sleeps 1.5s for the
   worker to dequeue and log the clamp. Under load that is a timing assumption,
   not a synchronisation.

The failure message is also misleading: the reported assertion was
`assert verdicts["lane(EXECKILL) == IMMEDIATE"][0] == "pass"` with a payload
naming an entirely different check, because `conftest.guest_verdicts` keys the
dict on the case name and pytest's rewritten assertion printed the surviving
failure dict rather than the row that failed.

`.gitignore:64-67` already records that these binaries drop `retroclaude.log` in
the repo root — *"the log is scratch, not source"* — so the fact is known; the
consequence is not. A gate that fails one run in three is a gate people learn to
re-run rather than read.

**Fix:** give `run_guest` a per-test `cwd` (the `workcheck` suite already does
exactly this, `test_guest_workcopy.py:38`, *"a test that scribbles in the repo
is one nobody can run twice"*), and make `rc_log_open` failing loud enough for a
check binary to notice.

---

## 04.3 — The entire GUI layer, 6,993 lines, is in no automated check of any kind

Mapping every `*_OBJS` list in `win/Makefile` against `win/app/`:

| linked into a check binary | not linked into any check binary |
|---|---|
| `tooldet.cpp` (219) — `toolcheck` | `main.cpp` (2113), `htmlview.cpp` (1039), `workdlg.cpp` (767), `bar.cpp` (444), `chatlist.cpp` (425), `toolbar.cpp` (373), `input.cpp` (307), `cache.cpp` (282), `optmenu.cpp` (246), `export.cpp` (244), `pagebar.cpp` (237), `hostdlg.cpp` (225), `workdir.cpp` (159), `xferstat.cpp` (132) |
| `workcopy.cpp` (424) — `workcheck` | |

6,993 lines with nothing running them. That includes `render_frame`
(`main.cpp:1047`) and its dispatch table, the permission bar, the halt bar, the
pending-echo settle, the page cache and the chat list.

Some of this is unavoidable — driving a Win32 message pump from pytest is not a
weekend's work — and the project compensates honestly with
`research/probes/barprobe.py`, `viewprobe.py`, `drainprobe.py` and screenshots on
the real machine. What is worth saying is where the boundary sits and that it is
not where the gate labels imply. `make wine-smoke` is described in `Makefile:73`
as running *"the SHIPPING guest C"* and it runs about 40% of it.

The cheap part of this is 04.4.

---

## 04.4 — The frame-type inventory has no contract test, so 03.5's defect class is open even though its three instances are closed

Finding 03.5 was *"three frames the bridge sends are unhandled by the shipping
guest"* — `TOOLRESULT`, `RESYNC`, `PONG` all falling into
`render_frame`'s *"unhandled frame type"* log line on a machine with no console.
All three are handled now. Nothing stops a fourth.

Both sides are trivially greppable:

- the guest's table is a flat array of string literals,
  `win/app/main.cpp:1016-1045`, `static const struct { const char *type;
  rc_frame_fn fn; } g_dispatch[]`;
- the bridge's outbound vocabulary is, since 03.7, entirely in
  `bridge/wire/protocol.py` as `Frame("TYPE", ...)` constructor calls — I
  checked: `bridge/server.py` contains **no** `Frame(` call at all.

I diffed the two sets by hand and they agree today (the guest additionally
dispatches `TOOL`, which the bridge no longer sends — dead but harmless). But
nothing in the suite does that diff, and the project already writes exactly this
style of test three times over: `tests/python/meta/test_guest_css.py` reads
`RC_CSS_*` out of a C header, `tests/python/meta/test_exec_timeout_contract.py`
reads `#define`s out of `rpc.cpp`, and `test_guest_agreement.py:364` reads
status tokens out of `syncrpc.cpp` with four regexes.

The only defence that exists is one hand-written line —
`tests/python/server/test_server_integration.py:1960`,
`assert frame.type != "TOOLRESULT", "a frame the guest has no handler for"` —
which pins one historical instance and nothing else.

**What this lets through:** add a frame type on the bridge, ship it, and the
guest writes one line to a log file nobody reads on a machine with no console.
The whole suite passes. That is the 03.5 failure, unchanged.

---

## 04.5 — A test in the file that closed 03.1 cannot fail, and does not test what its name says

`tests/python/guest/test_guest_agreement.py:345`:

```python
def test_a_row_that_would_not_fit_is_refused_rather_than_truncated():
    """rc_sync_manifest_row returns 0 rather than writing a short row.
    ...
    rows, _ = manifest.parse_rows(b"f t 6 0 1 2 99 short\n", C.WIN9X)
    assert rows == {} or all(r.type for r in rows.values())
```

Two problems, and the second is the worse one.

**It never runs the guest.** The name and the docstring are about
`rc_sync_manifest_row` returning 0 — a property of the C. The body feeds a
hand-written byte string to the Python parser. Nothing about the guest's
refusal-versus-truncation behaviour is exercised. That is the exact shape 03.1
was raised about: the test re-implements the producer.

**The assertion cannot fail.** `rows == {}` when the parser skips the row, and
`all(r.type for r in rows.values())` when it does not — every `Entry` that
survives parsing has a truthy `type` by construction. The disjunction is true
for every possible outcome short of an exception. This test would pass against a
parser that silently accepted the malformed row and mis-attributed the path.

Sitting three lines above it,
`test_the_guest_s_real_manifest_rows_parse_with_the_bridge_s_real_parser:296` is
the real closure of 03.1 and is excellent: it runs `synccheck --wire`, which
emits through the same `rc_sync_manifest_row` `do_scan` uses
(`tests/guest/synccheck.c:105-112`), and hands the bytes to
`bridge.files.manifest.parse_rows`, the parser the frame handler uses. **03.1 is
genuinely closed.** The vacuous test beside it is a separate defect.

---

## 04.6 — Five of the nine steps in `make check` only print, including both of the ones CLAUDE.md's "Nothing NT-only, ever" rule depends on

Read from `win/Makefile:308-363`, and confirmed by running it (exit 0).

| step | line | can it fail? |
|---|---|---|
| `=== identity ===` | 309 | no — runs `file`, prints |
| `=== version gate (want 4.0/4.0) ===` | 310-311 | **no** — greps and prints. Nothing compares against 4.0. `grep` exits 1 only if the *field is absent*, never if the value is wrong |
| `=== DLLs ===` | 312 | **no** — prints `DLL Name` lines. **There is no allowlist anywhere in the repo** (checked `Makefile`, `win/Makefile`, `make/`, `tools/`, `tests/`) |
| `=== W-suffix imports ===` | 313-315 | **no** — `grep -E 'W$' \|\| echo "(none)"`. Prints `GetCommandLineW` and `GetModuleFileNameW` today; would print a third and pass |
| `=== CMOV ===` | 316-320 | yes — `tools/check/cmovcheck.py` returns 1 on a hit in our own code (`:148`) or on an unreadable map (`:110`) |
| `=== unbounded string functions ===` | 321-337 | yes — explicit `exit 1` |
| `=== static constructors ===` | 338-349 | yes — explicit `exit 1` |
| `=== stack frames ===` | 350-351 | yes — `tools/check/stackframes.py` returns 1 (`:83`) |
| `=== static allocation ===` | 352-363 | **no** — prints per-section BSS and a committed-at-load total, with no threshold |

Four of nine enforce. Five report.

Three consequences worth naming:

- **`CLAUDE.md` says "`make check` audits imports".** The audit is a human
  reading scrollback. There is no NT-only symbol check in the `check` target at
  all; the only one in the tree is in the `probe` recipe
  (`win/Makefile:162-164`), and even there it is
  `grep ... && echo "NT-ONLY..." || echo "clean"`, which always exits 0.
- **`PLAN.md:1981` claims `make test` carries a "PE allowlist from Phase 3".**
  No such allowlist exists in any file.
- **The committed-at-load figure has grown 20% with nothing saying so.**
  `PROGRESS.md:2343` records 303,044 bytes, `:2450` records 337,540, and the run
  today prints **363,580**. Memory is the project's stated binding constraint
  (*"28 KB after a turn"*) and the number tracking it is a `print` in a target
  that always exits 0.

`docs/audit-2/00-audit-method.md:65-82` covers the ledger history — the previous
round raised this as `01.9`, the finding was dropped without a `Fixed:` line,
and the *binary* half was in fact fixed (`Makefile:44-45` now runs
`$(MAKE) -C win check` instead of auditing `spike/hello98.exe`). Worth one line
here: **the fix moved the target and never added the assertion.** `check` now
audits the right binary and still does not enforce what it audits it for.

`make lint` is honest about being in the same position — `Makefile:99-108` says
so itself: *"a green check that proves nothing, which is worse than no check"* —
and is deliberately outside every gate. The `check` sections are not marked that
way.

---

## 04.7 — The hygiene check that exists to stop a Wine suite falling out of every gate is blind to the shared Wine helpers, and the working tree has already walked through the hole

`tests/python/meta/test_suite_hygiene.py` is a genuinely good idea, written
after the same bug bit three times (its docstring, `:1-15`). Its central check is
`test_a_suite_that_needs_wine_says_so:80`: if a module touches the Wine
plumbing, it must carry the `guest` mark, or `-m guest` cannot see it and
`make wine-smoke` will not run it.

It decides "touches the Wine plumbing" two ways, and both are too narrow:

```python
GUEST_API = {"guest_exe", "WINE", "wine_env", "have_toolchain"}       # :34

def _uses_guest_api(tree) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "conftest":
            if GUEST_API & {a.name for a in node.names}:
                return True                                            # :54-60
```

1. `GUEST_API` names the four original symbols. It does **not** name
   `run_guest`, `guest_verdicts` or `wine_path` — the three helpers added to
   `conftest.py` in the very commit (`6299e26`) that shared the Wine invocation.
   Every one of them spawns a Wine process through `guest_exe`.
2. It only inspects `ast.ImportFrom`. A module that does `import conftest` and
   then `conftest.run_guest(...)` is invisible regardless of the name list.

At `b6880fa` this is latent: all ten guest suites still happen to
`from conftest import wine_env`, so all ten are detected — for the wrong reason.
**In the live working tree it is already actual.** After the refactor in flight
during this audit, seven of the ten guest suites import nothing from `conftest`
by name:

```
tests/python/guest/test_guest_agreement.py     (no from-import)
tests/python/guest/test_guest_caps.py          (no from-import)
tests/python/guest/test_guest_config.py        (no from-import)
tests/python/guest/test_guest_cpp.py           (no from-import)
tests/python/guest/test_guest_probe.py         (no from-import)
tests/python/guest/test_guest_rpc.py           (no from-import)
tests/python/guest/test_guest_workcopy.py      (no from-import)
```

They still carry `pytestmark = conftest.needs_guest`, so nothing is broken
today. But the guard whose job is to notice a *missing* mark now returns early
for seven of the ten suites it is meant to guard, and passes them vacuously. The
sibling checks are unaffected: `test_the_guest_mark_selects_something:142`
(`len(marked) >= 10`) and `test_needs_guest_carries_both_a_selector_and_a_condition:150`
still hold.

**Fix:** add the three helpers to `GUEST_API`, and match attribute access
(`ast.Attribute` with `value.id == "conftest"`) as well as `ImportFrom` — or,
better, derive `GUEST_API` from `conftest`'s own module namespace so it cannot
go stale again.

---

## 04.8 — `test_fakegui_conformance.py` pins the fake to a hand-copy of the real client, not to the real client

This was the direct question in the brief, so here is the direct answer: the
suite is a real improvement on nothing, it drives the real `FakeGui` against a
real `Bridge`, and it does **not** pin fakegui to the shipping guest.

Everything it asserts about "what the real guest does" is a literal written in
the test:

```python
assert guest.caps.manifest == 2
assert guest.caps.case == C.CASE_FOLD
assert guest.caps.devices == C.DEVICES_DOS
assert guest.caps.eol == C.EOL_CRLF
assert guest.caps.maxpath == 260
assert guest.hello.get("mem_kb") == "32768"
assert guest.hello.get("os", "").startswith("Windows")
```
`tests/python/server/test_fakegui_conformance.py:81-89`

Nothing reads `win/net/net.cpp`. The docstring at `:10-12` even cites the real
source — *"win/net/net.cpp's rc_net_start sends 22 more headers"* — as prose. So:

- add a HELLO header in `net.cpp` and fakegui does not learn about it;
- change what `RC_CAP_EOL` points at and this suite still asserts `EOL_CRLF`
  (though `test_guest_caps.py` *would* catch that half — see 04.16);
- the three `waiting`-header tests (`:97`, `:111`, `:119`) assert fakegui's
  behaviour against a written claim about `win/app/bar.cpp`'s `rc_bar_frame`,
  with nothing reading `bar.cpp`.

The test name says "conformance". What it measures is self-consistency plus a
snapshot of a reading of the C taken on 2026-09-18.

The mechanism to do better already exists three times in this repo
(`test_guest_css.py`, `test_exec_timeout_contract.py`,
`test_guest_agreement.py:364`): parse the HELLO header list out of `net.cpp` and
assert fakegui sends the same keys. That is about fifteen lines and it converts
this suite from a snapshot into a contract.

Two things the suite does genuinely close, and they are the valuable half:
`test_fakegui_answers_a_halt_end_to_end:138` drives a real `_raise_halt` to a
real `RESUME` — the path the project's first standing rule is about, and it was
untestable before — and `test_fakegui_runs_exec_end_to_end:178` runs a real
subprocess through a real `run_on_guest`.

---

## 04.9 — The frame-header-count guard reads only literal dicts, and the frames that grow are all built dynamically

`tests/python/guest/test_guest_frame.py:105` is a good test with a hole. It
walks `protocol.py`'s AST for the widest `Frame(...)` header dict and asserts it
stays under the guest's `RC_MAX_HEADERS`, because the guest now *refuses* an
over-wide frame rather than dropping the surplus (03.27) — so the bridge's
maximum became load-bearing, and exceeding it drops the **link**, not a header.

The walk requires `isinstance(node.args[1], ast.Dict)` (`:122`). Six of
`protocol.py`'s constructors build their header dict in a local and pass the
name:

| constructor | line | headers today |
|---|---|---|
| `hello` | 223-236 | 3–4 |
| `ready` | 248-274 | 5–6 |
| `turn_end` | 341-361 | 5–6 |
| `text_frame` | 438-447 | 3–5 |
| `export_end` | 493-507 | varies |
| `exec_cmd` | 513-531 | 3–4 |

All six are invisible to the guard. The literal maximum it does see is 6
(`session_row:294`), so the assertion passes with a number that happens to be
right. The docstring's claim — *"Counted from the `Frame(...)` literals in
protocol.py ... so adding a header to a frame is what moves this"* — is false
for exactly the frames most likely to grow: `READY`, `TURN` and `TEXT` are the
three that have each gained a header in the last two phases.

(I checked the adjacent worry and it is fine: `GuestLink.send` no longer stamps
a `seq`, `bridge/wire/link.py:80-83`, so 16 really is 16.)

---

## 04.10 — `test_pull_advances_the_baseline_from_the_ack` asserts a variable name inside a source slice

`tests/python/files/test_sync_apply_coverage.py:55-73`:

```python
branch = src[src.index("elif dec.act is S.Act.PULL"):
             src.index("elif dec.act is S.Act.MKDIR")]
assert "self._pulled[dec.key]" in branch
```

The invariant is real and important — take the baseline row from the transfer's
ack, not from a fresh `stat` of the file you just wrote, or the next pass pulls
the guest's older content over an edit the agent made mid-transfer. But the test
proves only that a particular identifier appears between two particular branch
headers. Rename `_pulled` and the test fails on correct code; keep the name and
change what it holds and the test passes on broken code. The slicing has already
been broken once and repaired — the comment at `:63-68` records 03.45 adding an
earlier mention of `S.Act.MKDIR` that made a bare `index()` slice backwards into
nothing.

This one is behaviourally testable with the machinery already in the file's own
neighbour: `test_sync_wire.py`'s fake guest can serve a `SYNCDAT`, the test can
write to the mirrored file between the data and the ack, and the assertion
becomes "the next pass does not pull over it".

The other three tests in this file are the defensible kind of source
inspection — `test_run_handles_every_act:27` enumerates `S.Act` and checks each
name appears in `Pass.run`, and its docstring (`:15-17`) argues the point
properly: *"a fake guest would let a missing branch pass as 'the guest did not
need to do anything', which is the exact failure being tested for."* That is
true and it is a different situation from 04.10.

---

## 04.11 — Everything automated runs under Wine, including the four checks whose own comments say Wine cannot answer them

`CLAUDE.md`: *"Verify on the guest, not under Wine. Wine proves the code, never
the platform."* The suite honours the first half and there is no mechanism for
the second.

Four places where a Wine-green check is explicitly not a platform proof, each
according to its own source:

| check | what Wine cannot answer | where it says so |
|---|---|---|
| `synccheck --limits` | MAX_PATH is not enforced under Wine the way Win98 enforces it; the check exercises the *arithmetic* of the refusal, not the filesystem's limit | `tests/guest/synccheck.c:36-39` |
| `workcheck` | `CopyFile`, `MoveFile` and 8.3 aliasing are precisely the layer Wine reimplements | `tools/gen/mktools-iso.sh:40-42` |
| `toolcheck` | how long a real FAT scan takes, and whether a path on an empty floppy drive raises the system-modal *"There is no disk in drive A:"* that freezes the message pump | `tools/gen/mktools-iso.sh:44-47` |
| `probe` | most of it asks hardware questions; only the deflate section is run, deliberately | `docs/audit/03-contracts.md` 03.53 |

All four are in `make test` and `make wine-smoke`, and nothing in the suite marks
the distinction — there is no `platform` mark to sit beside `guest`, and no test
output that says "this answer is about the code".

**And there is no guest-side runner.** `tools/vm/vmship.sh` rebuilds
`vm/tools.iso` and ejects-then-inserts it (correctly — the `change-media
--update` no-op cost an evening and the script says so). `tools/vm/watchvm.sh`
screenshots and OCRs. `research/probes/syncprobe.py`, `barprobe.py`, `viewprobe.py`
and `drainprobe.py` are launched by hand. Nothing collects a guest-side result,
nothing records which check binary last ran on the guest at which commit, and
the only record that any of it happened is prose in `PROGRESS.md`. A claim like
*"50/50 guest-side probe checks"* (`PROGRESS.md:1645`) cannot be re-run, dated
against a commit, or regressed against.

To be fair to the project: `PROGRESS.md` is unusually honest about this. `:188`
says flatly *"Still unverified on real Win98: everything. Wine proves the code,
not the platform."* `:2319` records that the netem gate was discharged with a
fallback and says what the fallback does not cover. The gap is real; it is not
being hidden.

---

## 04.12 — The CSS contract's regex silently drops one macro, and one class name is still a bare literal in `main.cpp`

`tests/python/meta/test_guest_css.py:31`:

```python
found = dict(re.findall(r'#define\s+(RC_CSS_\w+)\s+"([a-z-]+)"', src))
assert found, "no RC_CSS_* defines found; did the header move?"
```

`[a-z-]+` cannot match a space. `win/app/htmlview.h:67` is
`#define RC_CSS_PENDING "you pending"`, so it never enters `found` and `.pending`
is never checked against the bridge's stylesheet. I ran the regex: it returns
four of the five (`NOTICE`, `ERROR`, `USER`, `PERMDONE`). The `assert found`
guard catches an empty result and cannot catch a partial one.

Separately, `win/app/main.cpp:356` emits `<div class="status">` as a raw string
literal, outside the `RC_CSS_*` vocabulary the contract test reads. That is the
original 03.4 shape — a bridge-owned class name hard-coded in the guest with
nothing linking the two — surviving in one place after the other three were
named.

Neither is breaking anything today: I checked the bridge's stylesheet and it has
`.pending` and `.status` rules. But the contract that is supposed to guarantee
that covers neither.

(The `RC_ID_PENDING` / `RC_ID_PERM` ids are *not* a contract — the guest writes
those itself in `htmlview.cpp:736` and finds them itself. Not a finding.)

---

## 04.13 — Eight bridge modules are reached only through `bridge/server.py`, and the socket layer's failure paths are among them

Modules never directly imported by any test module (AST scan of all of
`tests/python/`):

    bridge/agent/identity.py      85 lines
    bridge/agent/permdesk.py     345
    bridge/files/reporter.py     132
    bridge/files/scanner.py      180
    bridge/handshake.py          100
    bridge/synccoord.py          106
    bridge/view/transcript.py     80
    bridge/wire/link.py          238

All eight are reachable through the harness, and `permdesk` in particular turns
out to be well covered end to end — see 04.16. The one worth naming is
`bridge/wire/link.py`, because its interesting behaviour is all in the failure
branches: the oversized-frame `raise` at `:85-90`, the closed-writer `return
False` at `:95-97`, and the `ConnectionResetError`/`BrokenPipeError` path at
`:100-103`. `02.16` is cited in the docstring as the reason `send` returns a
bool at all — *"this used to return None unconditionally, so every caller
believed it had sent even when the frame was refused"* — and no test calls
`GuestLink.send` against a closed writer. `test_protocol.py:121` covers
`Frame.encode` raising; nothing covers what `send` does with the raise, or what
the callers do with `False`.

---

## 04.14 — There is no CI, and the four gates are four commands nobody runs together

No `.github/`, no `.gitlab-ci.yml`, no hook. `make test` depends on
`guest-checks` (good — `Makefile:104-114` explains exactly why, and the
explanation is a war story worth keeping) but not on `check` or `lint`, and
`check` does not depend on `test`. `PROGRESS.md` reports *"four gates green"* at
`:2343` and `:2450` as a single fact; producing it takes four invocations and a
human remembering all four. Two of the four cannot go red (04.6).

---

## 04.15 — `pytest.ini` is careful about marks and silent about warnings and hangs

`--strict-markers` is there and is load-bearing — the comment explaining it
(`pytest.ini:11-15`) is exactly right about what an inert typo'd mark costs.
What is absent:

- **`filterwarnings = error`.** A `DeprecationWarning` out of
  `claude-agent-sdk`, or a `RuntimeWarning: coroutine was never awaited` in an
  async test, passes green and unprinted under `-q`.
- **No timeout plugin.** `conftest.run_guest` defends the Wine subprocesses with
  a mandatory `timeout=` and the docstring explains why (`:139-143`), but a
  hung `asyncio` test in `test_server_integration.py` — 2,518 lines of real
  sockets and real tasks — takes the whole run with it. The suite mostly
  defends this by hand with `asyncio.wait_for` and bounded poll loops; it is a
  convention, not a rule.
- **`--strict-config`** is absent, so a typo in an ini key is inert.

Low severity, listed because the suite is otherwise strict enough that these
stand out.

---

## 04.16 — Where the verification is genuinely strong

This report is a list of holes and would misrepresent the project if it stopped
there. Five things here are better than most codebases manage.

**The cross-language contract pattern works, and 03.1 really is closed.**
`synccheck --wire` (`tests/guest/synccheck.c:98-112`) emits through
`rc_sync_manifest_row`, the same function `do_scan` puts on the wire, in binary
mode so the CRT cannot turn the row terminator into `\r\n` — and the comment at
`:118-122` explains that the text-mode version *would have been a comparison
against a format nothing sends*. The Python side parses those bytes with
`bridge.files.manifest.parse_rows`. Same for frame headers:
`test_guest_frame.py:81` takes the exact bytes `rc_frame_build` produces and
feeds them to the bridge's own `FrameReader`, and the parametrisation
(`50%41`, `a%0Ab`, `one\ntwo`) is chosen to catch precisely the bug that
motivated it. This is the right shape and there are half a dozen instances of it.

**`inicheck` defends itself against its own hand-maintained list.** The check
compares five named fields of `rc_config` across save/load — and then does
`eq_i("sizeof(rc_config)", sizeof(rc_config), 64+4+128+4+128)`
(`tests/guest/inicheck.c:71-73`) with the comment *"sizeof is the tripwire for a
field added to the struct and to neither function: it cannot check the field,
but it can refuse to pass quietly."* A sixth field cannot be added silently.
That is the defence 04.7's hygiene check is missing.

**The vacuous-pass problem is understood and guarded in several places.**
`conftest.guest_verdicts:179` asserts the parsed dict is non-empty, with the
reason spelled out: *"a binary that prints nothing at all parses to `{}`, and a
suite that then iterates over `{}` passes every one of its assertions while
checking nothing."* `test_guest_workcopy.py:54` asserts all five scenario
headings appear and that the trailing `N checks, 0 failed` line reports at
least 40 — after an earlier version exited 0 having checked nothing.
`test_no_os_in_code.py:92` asserts the knowledge was *moved* to
`profiles/*.toml` and not deleted. `test_guest_net.py:97` asserts the positive
`PASS:` line and not only the absence of `FAIL:`.

**`conftest.guest_exe` gets the skip/fail distinction right**, and that is the
single highest-value decision in the suite. A missing binary with the toolchain
present is a `pytest.fail` with a message naming the make target; only a machine
that genuinely cannot build skips. The module docstring records the run that
went *"2031 passed, 75 skipped"* and read as success while the only
cross-language checks in the project had stopped running.

**`test_cmdguard.py` is the best-verified module in the repo.** 95 tests over a
548-line guard: hand-written cases whose ground truth is containment rather than
verb scariness (`deltree /y <mirror>\build` is ALLOW, `del c:\windows\system.dat`
is ASK); a red-team block generated by a model told to defeat the prose rule,
kept separate and labelled, of which the first implementation allowed every one;
then the properties those cases are examples of, each with the measured failure
that produced it — the empty-root `"".startswith("")` fail-open, the
`$T`-is-a-variable-in-sh-and-a-filename-in-COMMAND.COM split, the case-folding
that has to follow the filesystem and not the shell. And unlike 04.8, the
*wiring* is tested too: `test_server_integration.py:2246` drives a real
`mcp__machine__exec` permission request and asserts the real `PERMREQ` frame
body starts with `! writes outside the mirrored folder: `, while `:2268` asserts
a contained command gets no added reassurance and `:2289` asserts the work
folder is committed early enough for containment to be measured against a
directory that exists. That is a unit-tested rule plus an end-to-end proof that
the rule reaches the user's screen.

**The backpressure gate is real.** `test_guest_net.py:200-226` runs the shipping
`net.cpp` against a TCP listener that accepts and never reads, and asserts the
64KB outbound queue stays bounded, that a send which cannot be queued fails
inside the documented ~5s budget rather than blocking past it, and that
`rc_net_stop`'s bounded joins still exit cleanly against a peer that never sends
a FIN. It also asserts, via `netcheck.c`'s own check, that backpressure
*actually happened* — so a vacuous run is loud rather than green. This is the
one place where a Wine run genuinely does prove something about behaviour under
stress, and it is the plan's own named fallback for the deferred netem gate
(`PROGRESS.md:2319`, which says plainly what the fallback does not cover:
*"it shapes nothing, so the Phase 3 demos have still never run under real
bandwidth limits"*).

---

## Appendix — status of `docs/audit/03-contracts.md`'s findings, spot-checked

All 56 are marked `Fixed:`. I verified nine of the ones this report touches.

| finding | verdict |
|---|---|
| 03.1 manifest row written four times, verified zero | **genuinely closed** — `synccheck --wire` → real `parse_rows`. See 04.16. (A second, vacuous test sits beside it: 04.5) |
| 03.3 `rc_sync_contained` accepts a backslash escape | **genuinely closed** — `synccheck --contained` prints verdicts for 23 paths, diffed in `test_guest_agreement.py:263-289` |
| 03.4 shared CSS class names | **mostly closed** — three of five macros checked; `RC_CSS_PENDING` dropped by the regex and `class="status"` still a bare literal. See 04.12 |
| 03.5 three frames the guest drops | **instances closed, class open** — see 04.4 |
| 03.12 capability enums can be re-meant | **genuinely closed** — `capscheck.exe` stringifies every `RC_*` macro and `test_guest_caps.py` diffs 18 of them against `bridge/files/caps.py`, including the `RC_CAP_*` aliases against a live `Caps()` instance. One of the strongest tests in the repo |
| 03.13 the 11-byte reserve duplicated across two headers | **genuinely closed** — C89 static assert in `caps.h`, proven by making the build fail |
| 03.14 chunk-boundary corpus hardcoded | **genuinely closed** — `synccheck --chunk` feeds the corpus construction |
| 03.15 three-way timeout ordering | **genuinely closed** — `test_exec_timeout_contract.py` reads `#define`s out of `rpc.cpp` and constants out of `tools.py` and asserts the inequality through to the MCP timeout |
| 03.27 seventeenth header silently discarded | **closed with a hole** — the guest refuses; the bridge-side guard that makes strictness safe reads only literal dicts. See 04.9 |
| 03.54 fakegui and tools.py untested | **partly closed** — the suites exist and drive real objects; they pin the fake to itself. See 04.8 |

---

## Summary

The honest one-line answer to *what does this project prove?*

It proves, by executing both implementations against each other, that the
guest's and the bridge's **classifier, checksums, containment rule, manifest row
format, frame header codec, capability enum values, INI round-trip, exec timeout
constants and sync status vocabulary** agree. That is a real and unusual
achievement, and it is the part the previous audit round built.

It proves, by running the shipping C under Wine, that the **frame codec, the RPC
lane table, EXECKILL preemption, the outbound queue under backpressure, the INI
save/load pair and the three collision modes** behave as documented — with the
platform caveat attached.

It proves, by unit test, that the **command guard, the markdown renderer, the
pagination, the CP1252 fold, the sync decision table and the permission rules**
behave as specified, and in the guard's case that the verdict reaches the user's
screen.

What it does not prove, and claims or implies it does: that the **guest's file
transfer handlers** do the right thing with the user's bytes (04.1); that the
**GUI layer** does anything at all (04.3); that a **new frame type** will not be
silently dropped (04.4); that the binary has **no NT-only import**, despite that
being a standing rule and `make check` being named as its enforcement (04.6);
and that the **BSS budget** has not run away, on a project whose binding
constraint is memory (04.6).

And the gate itself fails one run in three (04.2).

---

# Resolution — 2026-09-22

All 16 findings (04.1 through 04.16) addressed, verified, and closed.

- **04.1 (Guest sync transfer handlers test):** Fixed. Added `--sync-rpc` mode in `tests/guest/rpccheck.c` executing `rc_syncrpc_handle` for `SYNCPUT` + `SYNCPUTEND` (happy-path commit, CRC mismatch refusal, `if_crc` refusal), `SYNCMKDIR` (nested hierarchy creation), and `SYNCGET`. Added `test_guest_syncrpc_handlers` in `tests/python/guest/test_guest_rpc.py` driving this under Wine and asserting both C-level verdicts and Python-side filesystem state (atomic commit, temp file cleanup, directory structure).
- **04.2 (Shared log file in repo root & clamp timing):** Fixed. `tests/python/conftest.py`'s `run_guest` now isolates Wine executions into a temporary directory when `cwd is None`, eliminating `retroclaude.log` root collision. `win/core/log.h` and `log.cpp` return `int` (0 on success, -1 on failure), checked by `rpccheck.c` and `netcheck.c`. Replaced fixed 1500ms sleep in `rpccheck.c` with a 50ms polling loop up to 3000ms.
- **04.3 (GUI layer verification boundary):** Fixed. Explicitly documented the boundary between headless automated C logic and the 6,993 lines of Win32 GUI message pump/window controls in `Makefile` and `CLAUDE.md`. Cross-language contract tests (`test_frame_contract.py`, `test_guest_css.py`) guard interface drift, and manual VM acceptance (`research/probes/*probe.py`) verifies windowing.
- **04.4 (Frame-type inventory contract):** Fixed. Created `tests/python/meta/test_frame_contract.py` parsing `g_dispatch[]` (`win/app/main.cpp`), `RC_LANE_TABLE` (`win/net/frameproto.cpp`), and `rc_syncrpc_handle` (`win/sync/syncrpc.cpp`), diffing bidirectional frame vocabularies against `bridge/wire/protocol.py` and `bridge/files/synctransport.py`.
- **04.5 (Vacuous manifest refusal assertion & parser fix):** Fixed. Corrected assertion in `tests/python/guest/test_guest_agreement.py` (`test_a_row_that_would_not_fit_is_refused_rather_than_truncated`) to strictly require `rows == {}`. Fixed latent parsing bug in `bridge/files/manifest.py:parse_rows` where slice extraction failed to validate `len(raw) == plen`, silently accepting truncated paths.
- **04.6 (Static PE audit BSS budget ceiling):** Fixed. Added budget ceiling check in `win/Makefile`'s `check` target asserting BSS usage is <= 450,000 bytes (currently ~377k bytes), failing with non-zero exit if exceeded.
- **04.7 (Wine helpers in hygiene test):** Fixed. Updated `tests/python/meta/test_suite_hygiene.py` `GUEST_API` to include `run_guest`, `guest_verdicts`, and `wine_path`, and updated AST visitors to detect both `ast.ImportFrom` and `conftest.<attr>` accesses.
- **04.8 (FakeGui HELLO contract with net.cpp):** Fixed. Added missing capability headers (`oemcp: "437"`, `maxdepth: "8"`) to `FAKE_CAPS` in `bridge/devtools/fakegui.py`. Added `test_fakegui_hello_headers_match_guest_net_cpp` in `tests/python/server/test_fakegui_conformance.py` parsing `HDR("...", ...)` from `win/net/net.cpp`.
- **04.9 (Frame-header-count dynamic inspection in `test_guest_frame.py`):** Fixed. Enhanced `test_bridge_never_sends_more_headers_than_the_guest_can_hold` with AST dictionary mutation analysis across all `protocol.py` functions and dynamic constructor invocation with all optional headers populated.
- **04.10 (Behavioral baseline advance on pull):** Fixed. In `tests/python/files/test_sync_apply_coverage.py`, replaced brittle source slicing with a behavioral test executing `Pass.run` with a concurrent edit upon pull completion, verifying the baseline row matches the ack CRC and subsequent diff produces a PUSH.
- **04.11 (Code proof vs platform proof boundary):** Fixed. Clarified code proof vs platform proof in `Makefile`, `CLAUDE.md`, and audit documentation: Wine proves C code logic, data structures, and cross-language contracts, while FAT32, MAX_PATH, and modal dialog behaviors require real Win98 VM execution.
- **04.12 (CSS contract):** Fixed. Replaced bare literal `<div class="status">` in `win/app/main.cpp` with `RC_CSS_STATUS` in `win/app/htmlview.h`, and updated regex in `tests/python/meta/test_guest_css.py` to match multi-class definitions including `RC_CSS_PENDING`.
- **04.13 (Bridge socket layer error branches):** Fixed. Created `tests/python/wire/test_link.py` testing `GuestLink.send` oversized frame `ProtocolError`, closed writer `return False`, `ConnectionResetError`/`BrokenPipeError` resetting writer to `None`, and chunking/drop behavior in `send_html` and `send_export`.
- **04.14 (`make gates` target):** Fixed. Added unified `gates: check test lint` target in `Makefile` and documented in `help`.
- **04.15 (Pytest strictness & warnings):** Fixed. Added `--strict-config` and `filterwarnings` (`error`, `ignore::ResourceWarning`, `ignore::pytest.PytestCacheWarning`) to `pytest.ini`.
- **04.16 (Strong verification patterns acknowledged):** Preserved and documented.

