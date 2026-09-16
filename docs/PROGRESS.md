# Progress

Rolling log. Plan is `PLAN.md`; phases 0–2 are executable, 3–6 are a sketch
that gets re-planned once Phase 0 answers land.

**Current goal: complete phases 0–2.**

---

## Blockers

| Blocker | Blocks | Needs |
|---|---|---|
| **Win98 SE install media** | every guest-side experiment, all of Phase 0's gates, Phase 1 validation | An ISO or disk image from the user. Not something that can be sourced automatically. |
| Real hardware access | end-of-Phase-1 gate | The physical P200MMX on the LAN |

Everything Linux-side proceeds without these.

---

## Phase 0 — foundations + experiments

| # | Item | State | Notes |
|---|---|---|---|
| 0.1 | Repo, licence, attribution, layout | **done** | |
| 0.2 | `make/ow.mk` with `-bm`, `WLINK_LNK`, pinned `WINVER` | | |
| 0.3 | Re-run `make check` after `-bm` | | The recorded PE numbers predate the flag and must be re-verified |
| 0.4 | **Experiment C** — compile retro-agent sources under `wcc386` | | No guest needed |
| 0.5 | **Compression** — build miniz under OW; measure deflate on target | | Build check no guest; measurement needs guest |
| 0.6 | Win98 SE guest under QEMU + `hello98` socket/log artifact | **blocked** | Needs media |
| 0.7 | W-suffix import load test (`GetCommandLineW`/`GetModuleFileNameW`) | **blocked** | Last unverified toolchain assumption |
| 0.8 | Samba LFN over `vers=1.0` | partial | Server side can be set up now; guest half blocked |
| 0.9 | 16-bit `EXEC` capture — **correct non-empty output**, not "no hang" | **blocked** | May reshape Phase 3 |
| 0.10 | `GetDIBits` → `BI_RLE8`? | **blocked** | Free screenshot compression if yes |
| 0.11 | `_beginthreadex` vs `CreateThread` under `-bm`, with a message loop | **blocked** | Folds into the Phase 1 reader thread |

## Phase 1 — "I typed on Win98 and Claude answered"

Linux side first, so the guest develops against something real.

| # | Item | State |
|---|---|---|
| 1.1 | `bridge/protocol.py` — frame codec (new, not a lift) | |
| 1.2 | `bridge/server.py` — listener, HELLO, role rejection | |
| 1.3 | `bridge/session.py` — one `ClaudeSDKClient`, `allowed_tools=[]` | |
| 1.4 | `bridge/fold.py` — UTF-8 → CP1252 | |
| 1.5 | `bridge/replay.py` — seq numbering + resume | |
| 1.6 | `bridge/fakegui.py` — Linux CLI speaking the wire protocol | |
| 1.7 | `win/` — window, RichEdit, edit box, reader thread, log, status bar | |
| 1.8 | Stop button + `interrupt()` | |
| 1.9 | Real hardware gate | blocked |

## Phase 2 — four demos

| # | Item | State |
|---|---|---|
| 2a | Streaming raw text + `enc=deflate` bridge→guest | |
| 2b | Styled runs (markdown → `CHARFORMAT2`) | |
| 2c | Pagination | |
| 2d | Session list | |

---

## Decisions made while building

*(append as they happen, with the reason — this is what survives a fresh session)*
