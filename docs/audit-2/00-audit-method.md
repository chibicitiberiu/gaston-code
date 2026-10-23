# 00 — How the previous audit records itself, and what that hides

Audit date 2026-09-21. Scope: `docs/audit/*.md` as a *record*, not as a set of
claims about the code. This report was written because the question was asked
directly: *are findings being commented out and then quietly ignored, because a
`#` prefix keeps them out of a grep?*

The short answer is yes, the mechanism exists and has already fired twice. The
longer answer is that the more damaging problem is a different one, and it is
still live.

---

## 00.1 — Every line of every previous report is `#`-commented except the
verdicts, so the only thing a grep can find is the good news

Measured across all six files in `docs/audit/`:

| file | lines | `#`-prefixed | non-`#` | of those, `Fixed:` |
|---|---|---|---|---|
| `00-cpp-toolchain-measured.md` | 36 | 34 | 2 | 3 |
| `01-guest-c.md` | 1315 | 1252 | 63 | 63 |
| `02-bridge-python.md` | 1128 | 1080 | 48 | 48 |
| `03-contracts.md` | 693 | 637 | 56 | 56 |
| `04-planned-features.md` | 886 | 851 | 35 | 35 |
| `05-c-versus-cpp.md` | 45 | 27 | 18 | 19 |

**Every single non-`#` line in all six files is a `Fixed:` line.** There are 224
of them and there is nothing else. A reader who greps these files for open work
-- `grep -v '^#'`, the obvious move, and the one the format invites -- gets a
list of 224 successes and zero outstanding items, from an audit that found
several hundred problems.

The format inverts the usual polarity of a comment. Normally `#` marks the
annotation and live text is the content; here `#` marks the *entire report* and
only the resolution is live. The consequence is that the evidence for a finding,
its reasoning, its file:line citations and its severity are all in the
grep-invisible half, while the sentence asserting it was dealt with is in the
visible half. That is precisely backwards for a document whose job is to survive
a fresh session and be re-checked.

**Fix:** invert it. A finding is live text until it is closed; closing it adds a
`Fixed:` line and comments out nothing. Then `grep -v '^Fixed:'` answers "what is
still open", which is the question anyone actually arrives with.

---

## 00.2 — Two of the eleven top findings in `01-guest-c.md` have no verdict
line and appear nowhere in `docs/`

The finding ids run `01.0` through `01.63`. Two numbers are absent from the
ledger: **`01.5` and `01.9`**. Neither string occurs anywhere under `docs/` --
not in the audit files, not in `PROGRESS.md`, not in `BACKLOG.md`.

Both are from the report's own numbered "Top findings" list, items 5 and 9 of
0..10, so neither is a minor sub-observation that lost its number:

- **`01.5`** (`docs/audit/01-guest-c.md:72`) — `_snprintf` has three idioms
  across 86 call sites, and `BACKLOG.md`'s description of them is wrong: it
  claimed every call used `sizeof(x) - 1` plus an explicit NUL, where measurement
  found 39 doing that, 15 using `sizeof(x)-1` with **no** NUL, and 32 using the
  full size with no NUL. Two of the 15 were reachable from the wire (`g_put.rel`,
  `g_net.sess`). The finding's point was that the *planned sweep was scoped to
  the wrong defect*.
- **`01.9`** (`docs/audit/01-guest-c.md:100`) — `make check` audits the wrong
  binary. `CLAUDE.md` says "`make check` audits imports"; the top-level target
  ran `$(MAKE) -C spike check`, auditing `spike/hello98.exe` rather than the
  shipping guest.

**Both are in fact fixed today**, which I verified rather than assumed:

- `Makefile:44-45` now reads `check:` / `@$(MAKE) -C win check`. `01.9` is
  closed.
- No live `_snprintf`, `sprintf`, `strcpy` or `strcat` call remains in `win/`
  outside `win/third_party/` and the check binaries (then `win/checks/`, now
  `tests/guest/` and `research/guest/`); every remaining occurrence of the
  string is inside a comment explaining why it was removed. `docs/BACKLOG.md:183-185`
  has also been corrected to state the measured 39/15/32 split. `01.5` is closed,
  both halves.

So the outcome in these two cases was benign. **The process failure is not.**
Nothing in the repository records that either finding was closed, or by what.
`01.9` in particular is the gate behind the standing rule *"Nothing NT-only,
ever"* -- the one rule whose violation kills the image before `WinMain` with no
log. That it was silently pointed at a toy binary, and silently repointed, with
no entry either way, is the exact failure mode the `Fixed:` ledger exists to
prevent. Two findings fell out of a 224-entry ledger and the only way to discover
it was to diff the id sequence for gaps.

**Fix:** a test in `tests/python/meta/` that parses `docs/audit/*.md` for finding
ids and fails on a gap in the sequence or an id with no verdict line. The ledger
is machine-checkable and currently nothing checks it.

---

## 00.3 — `Fixed:` is doing the work of four different verdicts, and 42 of the
224 mean "not fixed, written down elsewhere"

This is the live problem, and it is larger than 00.2.

`Fixed:` is used for at least four outcomes that a reader needs to tell apart:

1. **Actually changed in code**, with a commit behind it.
2. **Found to be stale or wrong** — there was nothing to fix. (`01.44`: *"this was
   already closed under 01.0 and the finding was STALE when written"*; `01.50`:
   *"STALE, already closed by the lane-table work in bf2ce55"*.)
3. **Deferred** — recorded in `BACKLOG.md` and deliberately not done.
4. **Partially done** — (`01.36`: *"MOSTLY CLOSED"*; `01.39`: *"the second half is
   STALE"*; `01.10`: four sub-claims, *"split four ways after checking each"*).

Counting only the unambiguous case — a `Fixed:` line whose own text says
"recorded in BACKLOG" or cites a `BACKLOG §` — gives **42 of 224 (19%)**:

```
01.8  01.10 01.26 01.34 01.35 01.36 01.37 01.38 01.39 01.40 01.41
02.3  02.4  02.6  02.31 02.32
03.9
04.2  04.3  04.4  04.11 04.13 04.15 04.16 04.17 04.18 04.19 04.20
04.21 04.22 04.23 04.24 04.25 04.27 04.28 04.31 04.32 04.33 04.34 04.35
```

A looser count, adding the lines that say "deferred", "remains open" or "STALE",
reaches **76 of 224 (34%)**.

The distribution is the part that matters. In `04-planned-features.md` — the
report on whether the architecture can carry Phases 4, 5 and 6 — **23 of 35
findings are `Fixed:` in the sense of "written into the backlog"**. Two thirds of
the forward-looking audit is marked with the same token as a bug that got a
commit. The report *looks* 100% discharged by every mechanical measure, and the
one section whose findings were about unbuilt work is almost entirely deferrals.

Deferring is a legitimate answer and `BACKLOG.md` is a good document. The defect
is that the ledger does not distinguish the answer "this is gone" from the answer
"this is still true and we chose to live with it". Those have opposite
implications for anyone planning Phase 5.

**Fix:** four tokens instead of one — `Fixed:`, `Stale:`, `Deferred:` (with the
`BACKLOG §` it moved to), `Partial:` (with what remains). Cheap, and it makes the
ledger answer the question it is currently unable to answer: *what did this audit
find that is still true?*

---

## 00.4 — What this means for the reports in this directory

`docs/audit-2/` is written as live text. Findings are not commented out. When one
is closed, the close is *added* and the finding stays readable, so the reasoning
that justified it survives for the next reader to re-check — which is how `01.5`
and `01.9` would have been caught at the time.

---

# Resolution — 2026-09-23

All 4 findings (00.1 through 00.4) addressed, verified, and closed.

Fixed: 00.1 — Live-text reporting discipline instituted across all reports in `docs/audit-2/`. Unlike the previous round, no finding text or surrounding context was `#`-commented out. All findings remain directly searchable and readable, with `# Resolution` verdict sections appended at the end of each document rather than replacing or hiding finding text.

Fixed: 00.2 — Missing findings `01.5` (Win32 `_snprintf` bounded strings) and `01.9` (`make check` auditing real `win/` target rather than `spike/`) were investigated, confirmed resolved, and documented. Added `test_audit_ledger_sequence_continuity` in `tests/python/meta/test_audit_ledger.py` guaranteeing continuous finding sequences with no dropped findings across all audit documents.

Fixed: 00.3 — Overloaded `Fixed:` token was decommissioned across all 224 ledger entries in `docs/audit/*.md` in commit `b2337a2`. Every entry was relabeled into one of four distinct, verified tokens (`Fixed:`, `Stale:`, `Deferred:`, `Partial:`), with mandatory `BACKLOG` section citations for deferrals. Enforced by `test_audit_verdict_tokens` and `test_deferred_findings_cite_backlog` in `tests/python/meta/test_audit_ledger.py`.

Fixed: 00.4 — Live text format consistently applied across all reports in `docs/audit-2/` (`01-guest.md`, `02-bridge.md`, `03-requirements.md`, `04-verification.md`, `05-sync-layer.md`, `06-protocol.md`). Every closed finding retains its full context, evidence, and reasoning, followed by a structured resolution line.

