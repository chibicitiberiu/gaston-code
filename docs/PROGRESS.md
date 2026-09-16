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
| 0.2 | `make/ow.mk` with `-bm`, `WLINK_LNK`, pinned `WINVER` | **done** | Uses `WLINK_LNK` rather than mutating PATH |
| 0.3 | Re-run `make check` after `-bm` | **done** | **Unchanged**: same 45 imports, 3 DLLs, 0 CMOV, 22016 bytes. `-bm` pulls nothing extra until thread functions are actually called — re-check once `_beginthreadex` is used |
| 0.4 | **Experiment C** — compile retro-agent sources under `wcc386` | **done** | **All five clean, zero errors, zero warnings, unchanged** |
| 0.5a | miniz builds + runs under OW | **done** | Vendored at `win/third_party/miniz/`; round-trip verified as a Win32 PE |
| 0.5b | Compression ratios measured | **done** | **~2.16:1**, not the ~3:1 assumed. Plan corrected |
| 0.5c | Deflate throughput on the real P200MMX | **blocked** | Sets the adaptive-codec thresholds |
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

### 2026-09-16 — `-bm` does not change the PE
Re-ran `make check` after adding `-bm`. Byte-identical result: 45 imports across
KERNEL32/GDI32/USER32, 0 CMOV, 22016 bytes. Open Watcom only pulls the extra CRT
machinery once thread functions are referenced. **Re-verify after the first
`_beginthreadex` call**, not before.

### 2026-09-16 — Experiment C: the fork risk was overstated
`protocol.c`, `util.c`, `log.c`, `exec.c`, `handlers.c` compile under Open Watcom
**unchanged, zero errors, zero warnings**. The plan assumed "nearly unchanged, build
system only"; the sources needed no edits at all. Phase 3's main remaining work is
reshaping the response path onto our frame grammar, not portability.

### 2026-09-16 — compression: measured, and the plan was optimistic
Real ratio on prose-plus-code is **~2.16:1**, against the ~3:1 the plan assumed.
Corrected in PLAN.md. Still clearly worth doing (dialup 8s → 3.7s for a 40KB turn).

Three parameters settled by measurement rather than argument:

- **Persistent deflate stream, never per-frame.** Independent deflate per frame is
  1.27× worse.
- **Coalesce to ≥2KB frames.** Frame size is the biggest lever: 256B → 1.68:1,
  2KB → 2.04:1, flattening after. The 10fps coalescer therefore buys ~21% bandwidth
  on top of its latency purpose — it was designed for one reason and pays for two.
- **No preset dictionary.** 2.16:1 either way; 1.86→1.89 on a cold 4KB start. The plan
  guessed this was marginal; it is, so the complexity is dropped.

Window stays 32KB (dropping to 8KB costs 6% ratio to save 24KB — bad trade against
8–12MB). Level 6 bridge→guest, level 1 guest→bridge.

### 2026-09-16 — vendored miniz rather than zlib
Two files, MIT, no build system, and it compiled clean under OW first try. Vendored
rather than submoduled so the guest build is reproducible offline.
