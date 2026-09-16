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
| 1.0 | venv + `claude-agent-sdk` 0.2.153 installed | **done** |
| 1.1 | `bridge/protocol.py` — frame codec (new, not a lift) | **done** — 29 tests |
| 1.2 | `bridge/server.py` — listener, HELLO, dup-client rejection | **done** |
| 1.3 | `bridge/session.py` — one `ClaudeSDKClient`, `allowed_tools=[]` | **done** |
| 1.4 | `bridge/fold.py` — UTF-8 → CP1252 | **done** — 31 tests |
| 1.5 | `bridge/replay.py` — seq numbering + resume | **done** — 10 tests |
| 1.6 | `bridge/fakegui.py` — Linux CLI speaking the wire protocol | **done** |
| 1.7 | `win/` — window, RichEdit, edit box, reader thread, log, status bar | **done** — 42KB, builds + runs |
| 1.8 | Stop button + `interrupt()` | **done** |
| 1.9 | Real hardware gate | blocked |

## Phase 2 — four demos

| # | Item | State |
|---|---|---|
| 2a | Streaming raw text + `enc=deflate` bridge→guest | **done** — verified both link regimes |
| 2b | Styled runs (markdown → `CHARFORMAT2`) | **done** — 66 tests, verified over the wire |
| 2c | Pagination | |
| 2d | Session list | |

---

### Phase 1 verification status

- `fakegui.py` -> bridge -> real Claude: **passing**, monotonic seqs
- `netcheck.exe` (real guest C: net.c + frameproto.c) under Wine -> bridge ->
  real Claude: **passing**, `inflight=0` so no frame leaked
- `retroclaude.exe` under Wine: connects, handshakes, receives READY, renders.
  Picked up **Tahoma** via `SPI_GETNONCLIENTMETRICS` -- the font question the
  plan said not to bother verifying.
- Still unverified on real Win98: everything. Wine proves the code, not the
  platform.

### Phase 2a measured (2026-09-16)

Same prompt, same code, two link regimes, via `--link-bps`:

| link | frames | deflate | bytes on wire | ratio |
|---|---|---|---|---|
| fast (default) | 33 | 1 | 2437 | — |
| 5 KB/s (dialup) | **6** | **6** | **594** | **6.27:1** |

All output intact both times, `inflight=0` both times. (6.27:1 is flattered by
deliberately repetitive test text; real prose measured 2.16:1.)

## Decisions made while building

### 2026-09-16 — coalesce first, THEN parse markdown
Ordering was an open question. Coalescing first is right: the parser receives
~2KB of whole lines instead of single tokens, which lets it merge adjacent
same-style runs. Each run costs an `EM_SETCHARFORMAT` + `EM_REPLACESEL` pair on
the guest, and that pair is RichEdit's expensive path.

Found by a test: `_merge` only coalesces within one `feed()`, so char-by-char
input produced more runs with identical content. Harmless, but it made the
ordering decision obvious.

### 2026-09-16 — no italic style
RichEdit could do it, but italic bitmap text at 8pt on a 640x480 CRT is hard to
read. `*emphasis*` degrades to plain with the markers stripped.

### 2026-09-16 — the coalescer's interval, not its target, sets frame size
First implementation targeted 2KB frames and **never compressed anything**: at
realistic token rates a 100ms window accumulates only ~35 bytes. The target was
unreachable by construction.

Fix is to trade latency for ratio as the link slows: 100ms on a LAN, 600ms below
~2 Mbit/s. Buffering longer is *free* when the wire is the bottleneck -- at
5 KB/s a frame already takes seconds to send, so 600ms more is invisible and
buys a much better ratio. At LAN speed the opposite holds and latency wins.

This is also why `MIN_WORTH_COMPRESSING` is only 64 bytes: because the stream is
persistent, even small frames ride the warm dictionary.

### 2026-09-16 — streaming duplicates text unless you suppress completed blocks
With `include_partial_messages=True` the SDK emits **both** token deltas and the
completed `AssistantMessage` blocks. Rendering both prints every word twice.
`Session` skips the completed blocks while streaming.

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

### 2026-09-16 — netcheck.exe: test the C, not the GUI
Driving a Win32 GUI from a test harness is fragile. `netcheck.exe` links the
real `net.c` and `frameproto.c` and does the whole round trip headless, so the
networking and framing are covered without GUI automation. It is also the
thing to run first on the real guest, since it needs no display.

### 2026-09-16 — my own RichEdit lint list was wrong
The first `make lint` list included `EM_SETUNDOLIMIT`, `EM_SETPALETTE` and
`EM_AUTOURLDETECT` as "3.0-only". All three are **2.0**, and `EM_SETUNDOLIMIT`
is one we actively rely on to keep the undo queue from growing. Corrected to
the five genuinely 3.0-only messages.

### 2026-09-16 — SDK introspected; every uncertain API claim resolved
`claude-agent-sdk` 0.2.153. The reviewers flagged four things to verify before
Phase 4 rather than trust the plan on. All four came back favourable:

- `PermissionResultAllow.updated_permissions` is `list[PermissionUpdate]` — the
  plan's list comprehension is right (docs rendered it as `dict` in one place).
- **`ClaudeSDKClient.set_model()` exists**, so the model dropdown changes live.
- **`effort` is an option, not a setter** (`Literal['low','medium','high','xhigh','max']`),
  so changing effort *does* need `fork_session=True`. The plan guessed both correctly.
- `SDKSessionInfo` carries `summary`, `first_prompt`, `custom_title` — no title
  derivation needed.

Bonus, not in the plan: `get_context_usage()` and `stop_task()` exist.
`get_context_usage()` is a free context-fill indicator for the status bar.

### 2026-09-16 — header values are percent-encoded, not backslash-escaped
Three characters only (`%`, `\n`, `\r`). claudewin9x needed a section of its
system prompt teaching the model to quadruple backslashes across four
interpreter layers; percent-encoding removes that class of bug, and the guest
can still parse headers with `strchr`.

### 2026-09-16 — fold(): explicit map must precede the CP1252 passthrough
First version checked "can CP1252 represent this?" first, which silently skipped
every deliberate override. Curly quotes, the ellipsis, the bullet and nbsp *are*
in CP1252 but are overridden anyway, because they render poorly in a fixed-pitch
bitmap font at 640x480. Caught by tests, which is the argument for writing them.

### 2026-09-16 — vendored miniz rather than zlib
Two files, MIT, no build system, and it compiled clean under OW first try. Vendored
rather than submoduled so the guest build is reproducible offline.
