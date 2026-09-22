# Audit 2 — index and executive summary

Audit date 2026-09-21, against `master` at `6299e26` plus an in-progress
working tree. Covers the 86 commits and ~37,500 changed lines since the
2026-09-18 round in `docs/audit/`.

## Reports

| | scope | state |
|---|---|---|
| [`00-audit-method.md`](00-audit-method.md) | how the previous round records itself, and what that hides | — |
| [`01-guest.md`](01-guest.md) | `win/` — the C++98 guest, 38 findings | **all closed**, plus 01.39 found on the guest |
| [`02-bridge.md`](02-bridge.md) | `bridge/` excluding sync and wire, 18 findings | **all closed** |
| [`03-requirements.md`](03-requirements.md) | PLAN/PROGRESS/BACKLOG against the code, 25 findings | **all closed** |
| [`04-verification.md`](04-verification.md) | what is proved vs. claimed, 16 findings | **all closed** |
| [`05-sync-layer.md`](05-sync-layer.md) | `bridge/files/` + `win/sync/`, 6 findings | 05.3 closed with 01.6; rest open |
| [`06-protocol.md`](06-protocol.md) | the wire protocol, both sides, 7 findings | open |

Findings are **live text**. Nothing is commented out, for the reason in
[`00-audit-method.md`](00-audit-method.md). A report that has been worked
through carries a `# Resolution` section at the end with one `Fixed:` line per
finding — the finding above it is left exactly as written, so the reasoning
that justified it stays readable and re-checkable against the fix.

## Two things to know before reading

**Another session was committing to this tree throughout the audit.** `HEAD`
moved from `6299e26` at the start to `ed7d74c` at the end — three commits
(`b6880fa`, `7a97451`, `ed7d74c`) completing the `tests/python` and `tools/`
reorganisations — and every remaining unstaged change has an mtime inside the
audit window (13:57–14:07). The content is coherent follow-through on that
reorg: path references updated across `CLAUDE.md`, `docs/PLAN.md`,
`docs/BACKLOG.md`, `bridge/files/manifest.py`, `win/Makefile` and five
`win/app` headers, plus a new `tools/toolkit.py`.

**Nothing in this audit modified the repository outside `docs/audit-2/`**, and
nothing was reverted. A full patch of the concurrent work was saved aside before
anything else was done. Two consequences are worth knowing:

- `00-audit-method.md` was written at 13:54 and **was swept into the other
  session's commit `7a97451`** ("Ten suites spelled out the same Wine
  invocation"), which is an unrelated change. The file is intact; the history is
  untidy. Left alone rather than rewritten, since the commit is not this
  session's to amend.
- A test-collection failure observed at ~13:55 and reported by one reviewer was
  fixed by that other session at 13:59. It is **not a defect**; the note in
  `03-requirements.md` has been corrected in place.

Findings were re-verified against the source after the tree settled. None of the
concurrent work touches the sync layer, the wire protocol, or the structure of
`win/Makefile`'s `check` target, so no finding here is affected by it.

**Severity is by consequence on the real target** — a 32MB Win98 box, possibly
on a 5 KB/s link, with the agent writing to a mirrored tree. Not by code smell.

---

## The seven that matter

Three are new and critical. I verified each of these myself against the source
rather than relaying them.

**1. `exec`'s truncation guard is inverted, so over-long commands are truncated
and executed.** (`01.3`, `win/rpc/exec.cpp:120`.) `rc_strf` returns **1 on
success and 0 on truncation**; the guard is `if (n < 0 || n >= (int)sizeof(cmd))`,
written for `_snprintf`'s old length-or-negative contract and never updated when
the 81-site `rc_strf` sweep landed. Neither value can satisfy it, so it never
fires and `RC_EXEC_TOO_LONG` is unreachable. A command over 1024 bytes is cut and
run. The file's own header, twelve lines from the bug, describes exactly this
defect in `probe.c` — *"a truncated command is still executed, so
`del c:\temp\*.tmp` cut short becomes `del c:\temp\*`. Rejected here instead"* —
and the rejection does not happen. This is the only site in `win/` that captures
`rc_strf`'s return into an `int` and compares it numerically; `sync.cpp:320` uses
it correctly.

**2. No file containing `//` can ever be pushed to the guest.** (`05.1` / `01.2`,
`win/sync/syncrpc.cpp:285`.) `do_put` hands `rc_sync_contained` the raw `rel`
pointer, but the bridge sends `"<len> <path>" + filedata` with **no separator**
(`bridge/files/synctransport.py:142`), so the check scans the path *and the first
8KB of the file's contents* as one string. `//` is an empty path component, so it
is rejected. Measured, running the guest's own logic: a `.c` file whose first
chunk holds `// a comment` is refused `bad_path`, as is `http://example.com` in a
text file — while the actual path is valid in every case. `push` then does not
advance the baseline (`syncrun.py:318`), so it retries every 30 seconds forever,
the user is told once that the *path* is bad, and the edit never lands. Found
independently by three reviewers. It has survived because the only tree this
project syncs is its own, and **0 of 66 sources under `win/` use a `//`
comment**.

**3. The permission bar takes focus on every push, so the next space bar answers
it.** (`01.1`, `win/app/bar.cpp:221`.) `show()` calls `SetFocus(allow)`
unconditionally, driven straight off `WM_RC_FRAME`. A HALT raised while someone
is typing moves focus from the edit control to **Continue**, and the next space
in their sentence sends `RESUME` — the agent merges over the file being edited.
That is `CLAUDE.md`'s "pressing Continue *is* the confirmation" inverted into
"any space bar is the confirmation". Compounding it, `01.13`: the bar's STATIC
lacks `SS_NOPREFIX`, so `rm -rf build && make` is displayed as
`rm -rf build & make` — the command shown is not the command approved.

**4. `make check` prints the import audit instead of enforcing it.**
(`04.6` / `03.1`, `win/Makefile:309-315`.) Five of nine steps only echo: identity,
the version gate (*"want 4.0/4.0"* is inside an `echo`; nothing compares), the
DLL list (**no allowlist exists anywhere in the repo**), the W-suffix imports,
and static allocation. Four can fail. So `CLAUDE.md`'s *"`make check` audits
imports"* — guarding the one failure mode it calls undebuggable, an NT-only
import that kills the image before `WinMain` with no log — is a human reading
scrollback. The DLL set has grown from 3 to 7 and committed-at-load from 303,044
to 363,580 bytes with nothing checking either, on a project whose stated binding
constraint is memory. This is also the sequel to a finding the last round raised
and lost (see below): the fix repointed the target and never added the assertion.
Related, `01.29`: the grep looks for `W`-suffixed imports, so adding the
`MoveFileExA` that `sync.cpp:585` explicitly wishes for would pass the gate and
kill the image.

**5. The bridge has no lock discipline around replacing the live agent.**
(`02.1`, `bridge/server.py:3677`, `:3612`, `:3554`.) `turn_lock` is taken only by
`_run_turn`/`_resume_turn`; `_switch`, `_new_session` and `_set_effort` close and
replace `guest.claude` without it. Switching chats mid-turn lets the in-flight
turn's callbacks write into the just-replayed conversation, and `server.py:2187`
dereferences `guest.claude` unguarded eleven lines above a site that guards the
same object — so the turn task dies and no `TURN end` is ever sent, leaving the
guest busy forever. `_handle` has no exception guard either, which is why it is
silent, and why `02.2` (`SETMODEL`, the one control path with no `try`) drops the
connection outright.

**6. The header-value cap is a protocol invariant enforced at one producer out of
about forty, and that one measures the wrong length.** (`06.1`, `06.2`.) A header
value over 255 bytes makes the guest drop the frame, and `net.cpp:184` turns that
into a dropped link. `Frame.encode()` does not check it — measured, a 304-byte
value encodes without complaint. The single call site that does check
(`server.py:2331`, for `exec`'s `cwd`) measures **UTF-8** bytes while the wire
carries percent-encoded CP1252, which is up to 3× longer. `error()` is the
sharpest case: it puts an unbounded `str(exc)` in a header, so a frame that
desyncs on a flaky link produces a 3,027-byte `msg=` — the frame explaining the
problem is the frame that kills the link.

**7. The guest half of the sync transfer has no automated test at all.**
(`04.1`.) `do_put`, `do_putend`, `do_mkdir` and `do_get` are linked into the
check binaries and never driven; `test_sync_wire.py`'s 2,282 lines run against a
Python fake. Swap the commit's temp/target arguments or invert its CRC check and
every gate stays green. This is why finding 2 above survived: `fakegui.py:421`
answers every `SYNCPUT` with a canned `readonly` **without parsing the body**,
`synccheck.c:93` tests `rc_sync_contained` correctly on bare paths, and
`rpccheck.c:107` only asserts which lane the frame takes. Every component is
covered; the composition is not.

---

## Four cross-cutting patterns

**The "one place" discipline protects functions, not call sites.** Findings 1
and 2 are both a *correct* function misused at a single site — `rc_strf`'s return
read under the old contract, `rc_sync_contained` handed the wrong string. So are
`01.6` (three discarded `rc_sync_join` returns) and `05.3` (`rc_sync_commit`
joining without checking, safe today only because two buffers happen to be the
same size). The project has been rigorous about giving each rule one
implementation and has no comparable mechanism for checking that callers use it
as documented. `win/core/str.h` even names the two wire-reachable sites; one of
them is finding 2.

**A comment asserting a fix is not a fix, and here four of them are inert.**
This codebase's outstanding quality is that almost every non-obvious decision
carries the incident that produced it — which is exactly why a stale one is
dangerous, because it is trusted. `exec.cpp:12` describes the truncation bug it
still has. `BACKLOG.md:651` justifies deferring compression on the claim that
deflate *"covers … sync file bodies"*, which are never compressed in either
direction (`03.4`). `CLAUDE.md`'s "28 KB after a turn" is a number `PROGRESS.md`
itself says should be corrected rather than repeated (`03.7`). `frameproto.h:6`
says header values are percent-encoded, true in one direction only (`06.3`).

**Gates that print rather than fail.** Finding 4 is the important instance, but
`04.5` (a test whose assertion `rows == {} or all(...)` is true for every
outcome), `04.7` (the Wine-suite detector blind to the shared helpers, now
vacuous for 7 of 10 suites) and `04.9` (the `RC_MAX_HEADERS` guard invisible to
the six constructors that build their dict dynamically) are the same shape: a
check that cannot go red is indistinguishable from one that passes.

**The forward-looking audit was answered by writing it down.** `00.3`: 42 of the
previous round's 224 `Fixed:` lines record a deferral to `BACKLOG.md` rather than
a change, and in `04-planned-features.md` — the report on whether the
architecture can carry Phases 4–6 — that is 23 of 35. The ledger uses one token
for "gone" and "still true, accepted".

## And the record lost two findings

`00.2`: ids `01.5` and `01.9` were assigned in the last round and appear nowhere
in `docs/` — no verdict line, no mention. Both were top-11 findings. Both turn
out to have been fixed incidentally by other work, which I verified; but nothing
records that, and the only way to notice they had gone was to diff the id
sequence for gaps. `01.9` was the `make check` finding that finding 4 above is
the sequel to.

---

## What is genuinely strong

Worth stating plainly, because the density of findings above is a function of how
much was built, not of how badly.

`diff()` in `bridge/files/sync.py` is an exhaustive, ordered state table over
every `{absent, file, dir, unreadable}` × baseline combination, and its two
invariants — the baseline advances last from the receiver's ack, and direction
comes from divergence rather than time — are the right two and are actually held
to. The capability handshake refuses unknown values instead of guessing the
permissive one, and the parsed-but-never-sent gap it had was genuinely closed.
The framing has no resync path and both sides say so. The send mutex is never
held across a multi-frame transfer, verified on both sides, with a measurement
behind the yield. `test_cmdguard.py` is 95 tests with a red-team block, wired
end-to-end through a real `PERMREQ` body. `test_exec_timeout_contract.py` pins
three ordered timeouts across both languages. `inicheck`'s `sizeof(rc_config)`
tripwire defeats its own hand-maintained field list — the defence `04.7` is
missing. The guest has no NT-only static imports, no raw `strcpy`/`sprintf`
anywhere, correct COM ownership across ~20 sites, correct four-thread shutdown
ordering, and a worst-case stack of ~21KB against a 64KB commit. And
`PROGRESS.md` is honest where it costs something to be: `:188` still reads
"Still unverified on real Win98: everything".

## Suggested order of work

1. `01.3` (exec guard) — one line, and it is a silent command-truncation.
2. `05.1`/`01.2` (push containment) — two lines swapped, plus a wire test.
3. `01.1`/`01.13` (bar focus and `SS_NOPREFIX`) — both break the approval rule.
4. `04.6`/`03.1` (make an allowlist, make the gate exit non-zero).
5. `02.1`/`02.2` (take `turn_lock` in the three rebuild paths; guard `_handle`).
6. `06.1` (enforce the header cap in `Frame.encode`, delete the wrong check).
7. `00.1`/`00.3` (invert the report format; split `Fixed:` into four verdicts).
