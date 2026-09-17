# RetroClaude — native Windows 98 client for Claude Code

*Revised after adversarial review. Corrections to the first draft are marked **[R]**.*

## How to use this plan **[R5]**

**Phases 0–2 are executable as written. Phases 3–6 are a sketch, and deliberately so.**

Five assumptions in Phase 0 are unverified, and at least two reshape later phases if they
fail: if 16-bit `EXEC` capture cannot return real output, Phase 3's design changes; if the
OW binary's two W-suffix imports do not load on real Win98, everything changes. Planning
3–6 in detail now means planning them twice.

**Plan two phases deep, rolling.** Execute 0 and 1, re-plan at the Phase 2 boundary with
real answers in hand.

Scale, honestly: Phase 1 is ~800 lines of Python and ~1,200 lines of C — nothing here is
conceptually hard. What makes it long is the guest edit-compile-test loop (copy the exe in,
run it, read a log; no debugger, no console, no stack trace). **4–8 weekends to the first
real conversation; 4–6 months to the full thing through Phase 4.** The main risk is
attrition, not architecture, which is why every phase has a demo and why the first artifact
is a Win98 window showing text from Linux rather than a benchmark CSV.

---

## Context

Nobody has built a native Win98 GUI client for Claude Code. `claudewin9x` (MIT) proxies the
real CLI but is a console REPL. `retro-agent` (MIT, 25k lines of C) has a complete guest
tool surface and a hardened protocol but is headless and drives the Agent SDK, not Claude
Code. WinGPT proves a native GUI works on 16-bit Windows but has no agency.

The gap is the combination: comctl32 chrome, a streamed transcript, permission requests as
real modal dialogs, and one machine being both the terminal you sit at and the workspace
Claude operates on.

Research: `/home/tibi/Dev/RetroAI/FEASIBILITY.md` (707 lines).

### Hard constraint

**32MB RAM, ~8–12MB free after boot.** Win98 will swap on a period IDE disk, so working-set
size dominates performance more than CPU. Everything below is downstream of this.

### Decisions locked

- Scope: terminal **and** workspace (guest agent in scope, sequenced late)
- Target: QEMU dev loop, real hardware validated at end of Phase 1 **[R: was Phase 2]**
- OS range: Win98 + 2000/XP, `GetVersion` branch for shell selection only
- Toolchain: Open Watcom 2.0

### Supersedes prior research **[R]**

FEASIBILITY §6 and §10.1 conclude "the transcript view must be custom-drawn regardless"
and make that premise 1 of the raw-Win32 toolkit decision. **Pagination invalidates that
premise.** RichEdit 2.0 is now correct because a page is bounded. Raw Win32 (no wxWidgets
/ Win32++) remains correct for the *other* reasons in §10: every candidate toolkit is
frozen or dead, and the 64KB USER/GDI heaps reward thin layers.

---

## Architecture

```
Win98 guest                        Linux bridge
  retroclaude.exe      --dial out-->   one listener, one secret, one port
   (one process:                         +-- Agent SDK (ClaudeSDKClient, streaming)
    UI thread                            +-- in-process MCP server (win98_* tools)
    reader thread                        +-- markdown -> styled runs
    RPC worker thread)                   +-- pagination + replay
                                         +-- screenshot diffing, scaling, encoding
                                         +-- UTF-8 -> CP1252 fold
                                         +-- Samba share (the actual project files)
```

### One binary, one connection **[R2]**

The guest is **a single executable with one TCP connection**, replacing the earlier
two-exe / two-connection design. One config value (bridge host:port, in an INI), no
`role=` in HELLO, no stable LAN address, no inbound listener, no UDP discovery.

**Why one socket is safe here.** The two-process split existed to stop display backpressure
stalling RPC. That concern doesn't survive contact with the traffic pattern: display text
is bridge→guest (frequent, small) while the only large payloads — screenshots, file
downloads — are guest→bridge. TCP flow control is per-direction, so a slow upload cannot
stall the display direction. FEASIBILITY §5's channel-multiplexing design stays dropped,
now for this reason rather than the process split.

**The one thing that must be right:** the send mutex is **never held across a multi-frame
transfer**. Chunk into ≤64KB frames and release between chunks, or a keystroke queues
behind a 2MB screenshot.

**Three threads, and the split is mandatory.** UI thread (message loop, RichEdit, dialogs);
reader thread (blocking `recv`, demux by frame TYPE); **RPC worker thread**. An `EXEC` can
run 60 seconds — running a handler on the UI thread freezes the window, and running it on
the reader thread stops display frames being read.

**Cost of the merge, stated honestly:** a GUI crash now takes the tool surface with it,
where a separate agent process would have survived. Mitigated by the bridge failing
in-flight tool calls with a structured error on disconnect (already required by
`asyncio.wait_for`, Phase 3) rather than hanging. In exchange: one working set, one
statically-linked CRT, one INI, one build target, one shutdown path, one connection to
supervise — all of which matter on a 12MB budget.

### The guest transforms nothing **[R2]**

The guest makes syscalls and moves bytes. Every transformation happens on the bridge. This
is the single most load-bearing principle for fitting in 8–12MB:

| Work | Where | Note |
|---|---|---|
| Markdown parsing | bridge | already |
| Pagination, search, export | bridge | already |
| Base64, attachment reading | bridge | already — reads off the share |
| CP1252 folding | bridge | already |
| **Screenshot diffing / tiling / scaling** | **bridge** | **[R2]** guest captures one reduced-depth frame, nothing more |
| **JSON** | **bridge** | guest emits `key=value`, never builds JSON |
| Compression | **both** | **[R3] the one deliberate exception — see below** |

**[R3] Compression is the single exception to "transforms nothing", and it is earned.**
Decompressing bridge→guest text is near-free and always worth it. Compressing guest→bridge
uploads costs real CPU, so it is *adaptive*: cheap RLE on a fast link, RLE+deflate on a
slow one, chosen from measured throughput. Everything else on this list stays on the bridge
unconditionally.

The screenshot change alone deletes the worst memory item in the plan: `SCREENDIFF` peaked
around **4.4MB** on the guest (`g_prev_pixels` 1.37 + `curr_pixels` 1.37 live at once +
`resp_buf` ~1.6) purely to compute a diff. The guest now keeps one capture buffer, at
reduced colour depth, chunked out in ≤64KB frames.

### Bandwidth, compression and transfer progress **[R3]**

**The link is not one speed.** The reference target is 10Mbps, realistically **~750 KB/s**.
But some deployments are **dialup at ~5 KB/s**, which is 150× slower. A design tuned only
for LAN is unusable there; one tuned only for dialup wastes a 200MHz CPU on the LAN.
**Bandwidth is a first-class design input, not an afterthought.**

**The CPU is a Pentium MMX 200.** So compression is a genuine tradeoff, not free. It is
worth it only when `compress_time + transmit_compressed < transmit_uncompressed`.

#### The asymmetry that decides the design **[R3]**

Deflate **decompresses roughly 4–10× faster than it compresses**, and the traffic is
lopsided in exactly the helpful way:

- **bridge→guest is all text** (styled runs, page loads). The modern box compresses at any
  level it likes; the guest only inflates. Inflation on a P200 is fast enough to be
  irrelevant against even a 750 KB/s wire. **Always compress this direction, at a high
  level.** Near-zero guest cost, large dialup win.
- **guest→bridge is where the P200 pays** — screenshots and file downloads. This is the
  only direction needing care. User input is tiny; never compress it.

#### Screenshots: crop, then reduce depth, then compress — and never JPEG **[R4]**

In priority order. The first lever is worth more than the other two combined.

**1. Crop. This is the whole game.** `WINLIST` already returns window rects, so a request
for a dialog sends the dialog, not the desktop. A 300×200 crop at 8bpp is ~60 KB raw and
~10 KB compressed, against 480 KB for a full 800×600 screen — **~50× better than any codec
choice**, and it cuts vision token cost from ~850 to ~80. Make region capture the default
and full-screen the explicit exception.

**2. Reduce bit depth, not resolution.**

- Win98's UI is flat fills from a small palette, so **24bpp → 8bpp is near-lossless for
  what matters**: text stays pixel-exact, only colour count drops. A wallpaper gradient may
  band; irrelevant.
- Win98 does not antialias UI text by default, so **4bpp stays legible** for pure dialogs —
  a further 2× when the link is slow.
- **Do not downscale.** Counterintuitive but important: Claude already scales images to
  ≤1568px on the longest edge, and 800×600 is *below* that. There is no free downscale to
  take, and going smaller destroys small text for zero token saving.
- **Quantize to a fixed palette with dithering OFF.** Dithering adds noise that hurts both
  compression ratio and legibility. Cheapest implementation: `BitBlt` into an 8bpp DIB
  section with a fixed palette and let GDI do the conversion in optimised code, rather than
  running median-cut over 480K pixels on a P200.

**3. Then compress, losslessly.**

- **RLE first.** Win98 UI at 8bpp is extremely run-friendly — flat fills, solid title bars —
  so RLE gets maybe 3–8:1 for a fraction of deflate's cost. **Check whether `GetDIBits`
  emits `BI_RLE8` directly** (Phase 0): if so, GDI does it and guest CPU cost is near zero.
  *Unverified — do not assume.*
- **Deflate on top only when the wire justifies it.** LAN: RLE alone. Dialup: both.

**Why not JPEG.** Its artifacts land precisely where the information is. DCT ringing and
8×8 blocking cluster on high-contrast edges, and a Win98 screenshot is almost nothing but
high-contrast edges — menu text, dialog labels, title bars — with chroma subsampling making
it worse. At quality levels that actually save bytes, small UI text degrades exactly as the
model tries to read it. This is the ordinary PNG-for-text / JPEG-for-photos rule, and it
bites harder when a vision model is the reader. (WebP/AVIF: encoder cost on a P200 is
prohibitive regardless of quality.)

**Lossy is not banned, it is content-dependent.** If a capture is genuinely photographic —
someone is viewing an image — JPEG is the right call for that region. That is a v2 refinement;
v1 crops and quantizes.

**[R4] Settle this empirically rather than by argument.** One capture of a busy Win98
desktop, encoded several ways — 8bpp+RLE, 4bpp+RLE, JPEG q50/q75/q90, a downscale — then
ask Claude to transcribe the visible text in each and score accuracy against ground truth.
Cheap, decisive, and it produces the actual quality/bytes curve for *this* content instead
of a general prior. Do it once, at the start of Phase 6; the numbers above are reasoning,
not measurement.

#### Text: keep the dictionary across frames — **[MEASURED 2026-09-16]**

One persistent deflate stream per direction with `Z_SYNC_FLUSH` per frame. Measured on
this project's own prose-plus-code documents with zlib, which is what the guest will see:

| Config | Ratio |
|---|---|
| Independent deflate per 512B frame | 1.47:1 |
| Persistent stream, 512B frames, L1 | 1.83:1 |
| Persistent stream, 2KB frames, L1 | 2.04:1 |
| **Persistent stream, 2KB frames, L6** | **2.16:1** |
| Persistent stream, 8KB frames, L6 | 2.11:1 |

Settled configuration:

- **Persistent stream, never per-frame.** Independent deflate per frame is 1.27× worse.
- **Coalesce to ≥2KB frames.** Frame size is the biggest single lever: 256B gives 1.68:1,
  2KB gives 2.04:1, and it flattens after that. **The 10fps coalescer therefore buys ~21%
  bandwidth on top of the latency win** — a second-order benefit worth knowing.
- **Level 6 bridge→guest** (free on a modern CPU, 6% better than L1), **level 1
  guest→bridge** (the P200 pays there).
- **32KB window.** Dropping to 8KB costs 6% ratio to save 24KB of guest RAM — a bad trade
  against 8–12MB.
- **No preset dictionary.** Measured 2.16:1 either way; 1.86→1.89 on a cold 4KB start.
  Not worth the complexity. *(The first draft guessed this was marginal; it is.)*

**[CORRECTION]** An earlier draft assumed ~3:1 on prose and ~4:1 on code. **Real is
~2.1:1.** Still worth doing — it nearly halves dialup time — but the tables below use the
measured figure.

#### Adaptive, not fixed

The guest already measures throughput for the progress UI. Feed it back in a periodic
`STAT` frame so **the sender picks the codec from the measured link speed**, per payload.
A frame header key `enc=raw|rle8|deflate|rle8+deflate` carries the choice; HELLO declares
what the guest supports. This is what makes one build work on both a 10Mbps LAN and a
modem.

#### Numbers

Guest→bridge, assuming ~4:1 on an 8bpp desktop capture:

| Payload | Raw | @750 KB/s raw | Compressed | @750 KB/s | @5 KB/s raw | @5 KB/s compressed |
|---|---|---|---|---|---|---|
| Screenshot 640×480×8bpp | 307 KB | 0.4 s | ~77 KB | ~0.1 s + CPU | 61 s | **15 s** |
| Screenshot 800×600×8bpp | 480 KB | 0.6 s | ~120 KB | ~0.2 s + CPU | 96 s | **24 s** |
| Screenshot 800×600×24bpp | 1.4 MB | 1.9 s | — | — | 280 s | — |
| `DOWNLOAD` 2MB log | 2 MB | 2.7 s | ~500 KB | 0.7 s | 400 s | 100 s |
| 10MB local attachment | 10 MB | 13 s | varies | — | 33 min | — |

Bridge→guest text at the **measured 2.16:1**, essentially free on the guest:

| Payload | Raw | @750 KB/s | Compressed | @5 KB/s raw | @5 KB/s compressed |
|---|---|---|---|---|---|
| 16KB page load | 16 KB | 20 ms | 7.4 KB | 3.2 s | **1.5 s** |
| A long streamed turn, 40KB | 40 KB | 53 ms | 18.5 KB | 8 s | **3.7 s** |

Screenshot ratios below remain **estimates** — they need a real Win98 capture to measure,
and flat 8bpp UI should compress far better than prose.

**Capture depth is therefore a wall-clock decision, not a taste one**: 8bpp+RLE at 800×600
is a fifth of 24bpp raw on the wire and a fraction of the memory.

**[R3] Compression runs on the RPC worker thread, never the UI thread.** At 200MHz a
level-1 deflate of a 480KB frame is a visible fraction of a second during which the box is
fully loaded, on a machine that is probably also swapping.

**Library: miniz 3.0.2, vendored at `win/third_party/miniz/`. [VERIFIED 2026-09-16]**
Compiles under Open Watcom with zero errors and zero warnings, and a round-trip test runs
correctly as a Win32 PE (subsystem 4.00, 0 CMOV, imports only KERNEL32/USER32). Adds ~95KB
to a linked binary. Win98's own `LZ32.DLL`/LZExpand is decompress-only and weak; ignored.

**Chunk size falls out of the same arithmetic.** A 64KB frame is ~85ms on the LAN wire, so
per-frame progress updates land at ~12/sec — smooth without flooding the UI thread. On
dialup the same frame is 13 seconds, which is exactly why progress and Cancel are required
rather than nice-to-have.

**Progress UI.** A status-bar pane showing direction, what, bytes done / total, rate and
ETA, plus a comctl32 progress bar and a **Cancel** button for anything over ~1s. The first
frame of a chunked transfer carries `bytes=<total>` so the guest can render a determinate
bar rather than a barber pole. Transfers are cancellable mid-stream via a `CANCEL\nid=`
frame; the sender checks between chunks.

**Attachments mostly avoid the wire entirely.** Since the bridge reads attachments off the
Samba share, a file already on `P:\` costs nothing — it never crosses the guest's socket.
Only a file on the guest's local `C:\` needs a chunked upload with progress. Make "attach
from P:\" the default path in the dropdown.

**[R3] The share is on the Linux box, which is the right way round.** Claude Code runs on
Linux and reads project files at local disk speed; only the *guest* pays LAN cost, and only
when it compiles or runs something. Worth stating because it inverts the obvious worry.

**Risk to watch:** SMB1 **round-trip latency**, not bandwidth, is what makes guest-side
builds slow — a compiler opening 50 small headers over `vers=1.0` pays per-file latency.
If Phase 3 builds crawl, the fix is copying the working set to local disk for the build,
not more bandwidth.

**[R] Threading rule, corrected.** The reader thread **never calls a window API that can
block or re-enter — `PostMessage` only** (the first draft said "never touches an HWND",
which is literally false since `PostMessage` takes one).

**[R] `PostMessage` backpressure — required, not optional.** `PostMessage` can fail; Win9x
queues are smaller than NT's. On failure the heap pointer leaks, on a 12MB machine,
precisely when it's already struggling. Rules: check the return value, **free it yourself
on failure**, and keep a UI-thread-acked in-flight counter that the reader blocks on past
a cap.

**[R] Use `_beginthreadex`, not `CreateThread`.** OW's CRT tracks per-thread data in a list
(`__AllocInitThreadData_` etc. in `clib3r.lib`); a raw `CreateThread` thread gets data
added lazily and **never released on exit**. `_beginthreadex` (`process.h:156`) also takes
a non-NULL `thrdaddr`, which is what Win9x requires anyway — `CreateThread(...,
lpThreadId=NULL)` returns ERROR_INVALID_PARAMETER on 95/98 (hardware-verified in
retro-agent's `docs/findings-dos-lane-2026-08-13.md`, single-source but credible).

### The bridge owns markdown

Wire carries **pre-styled runs**: `TEXT\nstyle=code\n\n<raw bytes>`. Win98 looks `style` up
in a static `CHARFORMAT2` table and does `EM_SETCHARFORMAT(SCF_SELECTION)` +
`EM_REPLACESEL`. No markdown parser in C.

Markdown needs lookahead, so the bridge buffers to a safe boundary (newline, or ~200 chars
of unambiguous text). Streaming granularity becomes one line — which is also what a P2
repaint budget wants.

**No JSON on the guest.** Grammar: `TYPE\n` + `key=value\n` + blank line + raw body.

**[R] Frame header keys present from Phase 1**, even where the guest ignores them, because
adding them later touches every frame and handler on both sides:

- `seq=` monotonic per session — what `resume ... from=` replays from
- `page=` — ignored in Phase 1, switches the control in Phase 2
- `sess=` — see multi-session below
- **[R3]** `enc=` — `raw` only in Phase 1; `deflate` / `rle8` / `rle8+deflate` later.
  Present from the start so adding a codec is a value change, not a protocol change.

**[R] Protocol error handling (missing entirely from the first draft).** Malformed header,
unknown `TYPE`, repeated key, truncated body → `ERROR\ncode=\nmsg=` frame and close.
**Cap frames at 64KB in our own `frameproto.h`.** retro-agent's `MAX_FRAME_SIZE` is 32MB —
the whole machine — and its own two implementations already disagree (32MB vs 16MB in
`agent-linux`), so assert the limit in HELLO alongside the version, not just the version.

**[R] Reconnect (missing entirely).** Bridge-down-at-startup shows a disabled input box and
a status-bar message, never a modal that blocks the message loop. Backoff 1/2/4/…/15s
driven by `WM_TIMER`, never a sleeping loop. On reconnect: `HELLO ... resume=<sess>
from=<seq>`. **Always `shutdown(SD_SEND)` + short grace before `closesocket`** — FEASIBILITY
treats the Win98 Winsock RST crash as real and field-verified.

**[R] Control frames must not queue behind streaming text.** `PERMREQ` shares the GUI
stream with a flood of `TEXT`; the coalescer drops pending display frames when a control
frame is queued.

**[R] Security, stated rather than inherited.** Plaintext shared secret on a trusted LAN
segment is an **accepted risk**; the listener binds only to the retro-segment interface.
**[R2]** With one binary the secret grants everything at once: authority to approve
permission prompts *and* the ability to feed fabricated tool results to Claude. A second
client presenting the secret while a session is live is **rejected**, not evicted.

### Pagination

The control holds exactly one page.

- Pages fill **forward from the oldest**, are sealed when full, and **never renumber**.
  Backward numbering would stale every anchor as content arrives.
- Bridge owns pagination. Cap on line count **and** byte count, preferring message
  boundaries. Never seal mid-run — **[R] except** past 2× the cap, where it breaks inside
  the run and emits `continued=1`. Without that override a single 200KB fenced code block
  is one unbounded page and the memory bound is fiction.
- **[R] LIVE-mode page turn is a designed event, not an accident.** Claude is mid-stream,
  page 500 fills, 501 opens. Chosen behaviour: **in LIVE mode, seal only at a message
  boundary**, so a turn is never split mid-sentence under the reader's eyes. Pages may
  therefore exceed the soft cap up to the 2× hard limit.
- Modes `LIVE` / `HISTORY`. Leaving LIVE doesn't stop the bridge buffering. The page count
  ticking up in the status bar is the new-content signal.
- Controls: `[Live] [<<] [<] "Page 500/500" [>] [>>]`.

Memory is bounded by page size, not transcript length: a 200-line page is ~16KB of text.

**[R] Deleted claim:** the first draft said pagination "deletes the append-while-scrolled-up
failure mode." False — within a LIVE page you can still be scrolled up. Autoscroll must
still track bottom-ness via `GetScrollInfo`.

Search and export become bridge operations over the full transcript.

### Renderer: RichEdit 2.0

`riched20.dll` ships with Win98. `CHARFORMAT2`/`PARAFORMAT2` cover bold, per-run monospace,
background tint, indents, bullets.

**[R] `#define _RICHEDIT_VER 0x0200` does nothing and `richcompat.h` as first specified was
worthless.** In OW's `h/nt/richedit.h`, *nothing* is gated on `0x0300` — the only gates are
`>= 0x0200` (class name, A/W typedefs) and `== 0x0100`. `EM_SETTEXTEX` (:116),
`EM_GETTEXTEX` (:113), `EM_AUTOURLDETECT` (:110) and `CHARFORMAT2` are all defined
unconditionally. Preprocessing at 0x0200 yields a byte-identical header.
**Replacement: a `make lint` grep for the 3.0-only message names**, listed explicitly, run
in CI. `RICHEDIT_CLASSA == "RichEdit20A"` is confirmed (:36) and ungated.

**MSHTML rejected for v1.** Direct hosting is Microsoft-documented and is what Outlook
Express did — but OE rendered a short fresh document per message, never mutating one
long-lived document at 15Hz. MSHTML's design centre is infrequent async whole-document
loads. ~600–1200 lines of plain-C COM boilerplate vs RichEdit's one `CreateWindowEx`, and
its multi-MB DLL tax is fixed per process so pagination doesn't reduce it. *Revisit only if
sealed history pages ever need tables / syntax highlighting / inline images — those are
short static documents, which is the pattern MSHTML is good at.*

### Multi-session **[R] — the biggest hole in the first draft**

**One live `ClaudeSDKClient` at a time.** Switching chats closes it and re-opens with
`resume=<id>`; the list is read-only history; background sessions do not stream. This is a
third of the work of a warm-client pool and nobody will notice on a single-user box.

**[R]** `SDKSessionInfo` already carries `summary`, `custom_title` **and `first_prompt`**,
and `rename_session()` exists — so the first draft's "derive a title from the first user
message" workaround is unnecessary. Use the fields.

---

## Toolchain facts

Verified on this machine unless marked otherwise.

- Open Watcom 2.0 beta (2026-09-14), `/usr/bin/watcom`, host binaries in `binl64`
- Builds a Win98-valid PE: 22KB, "MS Windows 4.00 (GUI)", imports only
  KERNEL32/GDI32/USER32, no MSVCRT.DLL, 0 CMOV
- **[R] These numbers predate `-bm`.** `-bm` links a different (multithreaded) CRT.
  **Re-run `make check` after the flag change before trusting any of them again.**
- **[R] NOT verified, and the first draft wrongly promoted it to fact:** the OW CRT startup
  imports `GetCommandLineW` and `GetModuleFileNameW`. Both *should* be exported as stubs by
  Win98's kernel32, so the EXE should load — but this has never been run on Win98. **It is
  a load-time hard failure if wrong, and it is the last unverified assumption in the whole
  toolchain.** The import allowlist must whitelist these two on day one.
- **[R] `-bg` is redundant, not required.** `wlink system nt_win` already selects windowed
  startup; the spike builds and passes `wine-smoke` without `-bg`. Harmless to add.
- **[R] `-bm` is required, but the first draft's `errno` example was wrong** — `errno.h:32`
  uses `__get_errno_ptr()` unconditionally. Per-thread-ness comes from the library module
  `-bm` selects, not from headers.
- **[R] `wlink` finds `wlink.lnk` via PATH — and not via its own directory either**
  (verified: `wlink.lnk` lives *in* `binl64` and an absolute-path invocation still failed
  with "undefined system name: nt_win"). **Set `WLINK_LNK` instead of mutating PATH** —
  strictly more robust, and immune to another Watcom on PATH.
- **[R] Pin `-DWINVER=0x0400 -D_WIN32_WINNT=0x0400` in `ow.mk`.** OW defaults to `0x0400`,
  which is why `sizeof(NONCLIENTMETRICS)` is Win98-correct *by luck*; `winuser.h:5101`
  gates `iPaddedBorderWidth` on `WINVER >= 0x0600`. Same bug class as `_RICHEDIT_VER` —
  the first draft caught one instance and generalised to none.
- **[R] Leave `_WIN32_IE` at its default (0x0400, from `sdkddkver.h:131`).** `TBSTYLE_*` is
  gated `>= 0x0300`, `BTNS_*` on `>= 0x0500`, so use `TBSTYLE_DROPDOWN`. Lowering
  `_WIN32_IE` "to be safe" silently removes `TBSTYLE_FLAT`, `TBSTYLE_LIST`, `HDPA`.
  (The QEMU target is Win98 **SE**, which ships comctl32 5.80, not FE's 4.72.)
- **[R] `GetFreeSystemResources` is a 16-bit `USER.EXE` export** — it appears only in
  `h/win/win16.h`, never in `h/nt/`. `GetProcAddress` against user32 returns NULL. The
  32-bit route is `rsrc32.dll` + `_MyGetFreeSystemResources32@4` (*unverified*), and it
  returns a **percentage**, not a quantity. Log GDI object counts we create ourselves plus
  working set instead; neither needs a 9x-only dynamic resolve.
- Wine tier works via Lutris `wine-ge-8-26-x86_64`; `make wine-smoke` passes
- QEMU 10.2.2 with pc-i440fx, pcnet, cirrus-vga, sb16

---

## Phases

### Phase 0 — Make the guest real *(2 weekends)*

**[R] Hard external dependency, unlisted in the first draft: a Win98 SE install source.**
Nothing downstream starts without it. Resolve this first.

**The one thing to do first — one weekend, one artifact:** a Win98 SE guest under QEMU,
with `hello98.exe` modified to open a TCP socket to a 30-line Python echo server on the
host, exchange one length-prefixed frame, and write a log file beside the exe.

It resolves the ISO dependency, **closes the `GetCommandLineW`/`GetModuleFileNameW`
question** (which Wine cannot answer, by our own argument), establishes the copy-in /
read-the-log inner loop that every later phase depends on and that the first draft never
mentioned building, and proves Winsock plus the dial-out topology in the smallest form.
The artifact is a Win98 window showing text from Linux, which matters for morale in a
6–9 month evenings project in a way a CSV does not.

Then, cheap now that the loop exists:

- **Experiment C (~30 min, best item in the plan):** `wcc386` each of `exec.c protocol.c
  util.c log.c handlers.c` from the retro-agent clone and count errors. Turns a Phase 3
  assumption into a number. *(Skip `files.c` — only `DOWNLOAD` survives the cut.)*
- **[R] Samba LFN test (~20 min), moved up from Phase 3.** Mount the share, create a long
  filename from Linux, confirm the guest sees it un-mangled over `vers=1.0`. **This is a
  genuine kill shot** — if LFN breaks, the whole file-access architecture changes. It was
  sitting at the top of a phase months away.
- **[R] 16-bit EXEC capture test (~1 hour) — new, and the highest-value experiment here.**
  See the Phase 3 risk below: this may invalidate the entire EXEC design.
- **~20-line thread test**, folded into the Phase 1 reader thread rather than standing
  alone: `_beginthreadex` vs `CreateThread` under `-bm`, **with a message loop** (the
  `retro_chat.c` precedent is a console app with no message loop, so it doesn't prove the
  GUI half).
- **[R3] Compression feasibility (~1 hour), three questions in one sitting**, because the
  whole adaptive-codec design rests on them and all the throughput figures above are
  order-of-magnitude estimates, not measurements:
  1. Does `miniz` (or zlib) build clean under Open Watcom? Single `.c`, should be trivial.
  2. **Measure** deflate level-1 compress and inflate throughput on the *actual* P200MMX,
     on a real 8bpp desktop capture and on a chunk of chat text. This sets every threshold
     in the adaptive scheme; guessing it would be guessing the core of the design.
  3. Does `GetDIBits` emit `BI_RLE8` directly on Win98? If yes, screenshot compression is
     nearly free and GDI does it. If no, hand-roll RLE8 (~40 lines).
- `git init`, repo layout, `make/ow.mk` with `-bm`, `WLINK_LNK`, pinned `WINVER`

**[R] Cut: Experiment A, the RichEdit benchmark (~300 lines + CSV + plot).** Its own framing
conceded pagination already bounded the problem and the output merely "sets the page size".
An experiment whose failure mode is "adjust a constant" is a tuning exercise wearing a
kill-shot costume. Instead: **set the page at 200 lines / 16KB by fiat**, put ten lines of
`QueryPerformanceCounter` logging in the real Phase 1 append path, and read numbers off
actual use. Halve the constant if it's slow. Also drop the "assert `EM_EXLIMITTEXT` accepts
>64K" free-rider — pointless at a 16KB page. (`cchTextLimitDefault` is 32767,
`richedit.h:32`.)

**[R] Cut: the iron contradiction.** The first draft gated the project on a number it
declared non-authoritative, measured on a platform it declared optimistic, against a
threshold not evaluable until two phases later. Gone with Experiment A.

### Phase 1 — "I typed on Win98 and Claude answered" *(5–6 weekends)*

No tools, no streaming, no formatting, no pagination UI.

**Linux first**, so the Win98 side develops against something real:

- `bridge/protocol.py` — frame codec. **[R] This is not a "lift" of
  `client/retro_protocol.py`**: that file is AUTH + status-byte + an `asyncio.Lock`
  serialising strict request/response, structurally unlike our full-duplex push protocol.
  About 15 lines of length-prefixed read loop survive. Budget it as new.
- `bridge/server.py` — asyncio listener, HELLO auth, role dispatch, role-conflict rejection
- `bridge/session.py` — one `ClaudeSDKClient`, complete blocks only
  (`include_partial_messages` is Phase 2a)
- **[R] `allowed_tools=[]` explicitly.** Unstated in the first draft, which meant Phase 1
  Claude would run Bash unrestricted **on the Linux bridge host**.
- `bridge/fold.py` — UTF-8 → CP1252, ~80 lines. Build now or the first demo is full of `â€™`
- `bridge/replay.py` — **[R] ~40 lines, not a ring buffer.** The retrofit brutality is in
  the `seq` numbering, not the storage; a `list` is fine and eviction isn't needed yet.
- `bridge/fakegui.py` — a ~150-line Linux CLI speaking the GUI protocol. Highest-value item
  in the plan: the whole bridge gets built and tested with no Win98 in the loop, and it
  stays as the conformance harness and the break-glass tool for a wedged permission prompt.
  **[R] Dropped the "first-class, permanently maintained artifact" framing** — that's how a
  150-line script becomes a framework.

**Then Win98** — `win/gui/main.c`, `net.c`, `transcript.c`. Window, RichEdit
(`LoadLibraryA("riched20.dll")` *before* `CreateWindowExA` or it returns NULL), edit box,
Send, reader thread, framing, `PostMessage` ownership, CP1252 display, first-run INI.
**[R] This is not one evening.**

- **[R] `win/common/log.c` from day one**, writing `retroclaude.log` from the first
  statement of `WinMain`. There is no console on this platform; "no log file created" is
  the primary signal that `main()` never ran. Plus a Help→Diagnostics box: connection
  state, last Winsock error, frames in/out, bytes. ~80 lines, and it's the difference
  between debugging by reading and debugging by bisect.
- **[R] Stop button + `interrupt()`** — one frame type, one toolbar button. A client that
  can't stop a runaway Claude isn't "working", and the first draft budgeted an anchor
  dropdown before it.
- **[R3] Status bar with a transfer pane from day one.** Even in Phase 1 it shows
  connection state and bytes in/out; the progress bar and Cancel land in Phase 3 when the
  first large transfers appear. Building the pane now means `net.c` reports byte counts
  from the start rather than being retrofitted.
- Font: `SystemParametersInfo(SPI_GETNONCLIENTMETRICS)` → `lfMessageFont`. No verification
  needed given the pinned `WINVER`.

**[R] Gate at end of Phase 1: run it on real hardware.** Moved up from Phase 2 so that a
hardware problem and a RichEdit problem and a pagination problem are never live at once.

**[R] Moved out: the PE import allowlist test.** `make check` already exists and prints DLL
names, every symbol, W-suffix imports, CMOV count and size. The regression class barely
exists in Phase 1 (six hand-chosen DLLs, statically linked CRT). It belongs in Phase 3,
where forked code actually arrives. FEASIBILITY also notes the export-allowlist design is
strictly better than retro-agent's ban-list; both need a Win98 install, which Phase 0 now
provides.

### Phase 2 — split into four demoable steps *(6–8 weekends)*

**[R] The first draft bundled five hard problems behind one demo. That is where this
project stalls.**

- **2a — streaming raw text.** `include_partial_messages=True`, `StreamEvent` →
  `content_block_delta`/`text_delta`, 10fps coalescer. **[R3]** Plus `enc=deflate` on the
  bridge→guest direction with a persistent stream and `Z_SYNC_FLUSH` per frame — it is
  nearly free for the guest and it is what makes a dialup link usable, so it belongs with
  the first streaming work rather than as a later optimisation. *Demo: text types out; and
  it types out at a sane rate over a throttled link.*
- **2b — styled runs.** `bridge/markdown.py` with the safe-boundary flusher, `CHARFORMAT2`
  run table, batched append inside `WM_SETREDRAW`/`EM_SETEVENTMASK(0)`/`EM_SETUNDOLIMIT(0)`.
  Autoscroll only when already at bottom, via `GetScrollInfo`. *Demo: formatted output.*
- **2c — pagination.** `bridge/pages.py`, page controls, the LIVE-mode message-boundary
  seal, the 2× `continued=1` override. *Demo: page back and forth through a long chat.*
- **2d — session list.** ListView over `list_sessions()` / `get_session_messages()`, one
  live client, switch = close + `resume`. *Demo: two conversations.*

**[R] Deferred to later polish: the anchor list** (per-message `(page, offset, first 40
chars)` + dropdown + `EM_EXSETSEL`/`EM_SCROLLCARET`). `[<<] [<] [>] [>>]` is enough.

### Phase 3 — the guest becomes a workspace *(detailed 2026-09-17)*

Phase 0 answered the two questions this phase was gated on, and both answers
were favourable, so the shape below is firmer than the original sketch.

**What 0.9 settled.** The original text warned that `exec.c`'s capture might
return nothing and that the fallback was a rewrite. Measured on the guest:
`command.com /c ver` returned 36 bytes, `dir c:\ /w` 352 bytes, and
`echo hello | find "hello"` returned `hello` -- the known-bad 16-bit tool,
through the 16-bit shell. **Capture works, the design holds, and the rewrite
is not needed.**

**What changed underneath it.** Samba is gone (R6): the file layer is a sync
layer now. And the memory position is far tighter than this document assumed.

#### The budget, measured

With ONLY the app running on the 32MB guest -- no DOS box, no probe:

| | free of 32288 KB |
|---|---|
| window created | 2420 KB |
| chat list loaded | 1060 KB |
| after one turn | **28 KB** |

The original Phase 6 text worried about a 4.4MB `SCREENDIFF` peak on a machine
with "8-12MB free". There is no longer 8-12MB free; the HTML transcript spends
almost all of it. `dwAvailPhys` excludes the disk cache, which Win98 shrinks
under pressure, so this is the cache being squeezed rather than allocation
failing -- the app stays responsive. But **every allocation in this phase has
to be bounded and small**, and the one place the old plan still assumed
otherwise is the screenshot buffer.

#### 3.0 — Permission prompts *(prerequisite, not optional)*

Nothing else in this phase may land first. Handing Claude a tool that runs
programs on the user's computer without `can_use_tool` is `bypassPermissions`,
which is exactly what FEASIBILITY condemns in claudewin9x and says not to
inherit.

- `can_use_tool` -> `PERMREQ` frame -> native modal -> `PERMRESP`.
- Allow / Always allow / Deny, with a reason field on deny.
- **`can_use_tool` has no timeout and a pending prompt wedges the session
  forever.** Both mitigations are required: a bridge-side deadline (~10 min)
  that auto-denies with an explanatory message, and `fakegui.py` as the manual
  escape hatch for a wedged prompt.
- **Modal dialogs pump messages**, so `WM_RC_FRAME` will dispatch *during* the
  prompt and re-enter the transcript. `rc_html_append` already has a queue for
  frames that arrive before the document is ready; reuse it. A flag set around
  the modal defers appends into that queue rather than making the DOM calls
  reentrant.
- "Always allow" decisions are remembered **on the bridge**, which is the side
  that enforces them, persisted like `bridge/registry.py`. Not in the guest's
  INI: a rule that lives on the machine being protected is the wrong way round.

*Demo: ask for something that needs a tool, get a real Win98 dialog, deny it,
and see Claude say so.*

#### 3.1 — The tool surface

- `bridge/tools.py` with `create_sdk_mcp_server` and one tool, `win98_exec`.
- **Wrap the tool body in `asyncio.wait_for`.** `MCP_TOOL_TIMEOUT` documents
  stdio and WebSocket servers; ours is in-process and is not covered.
- A guest that disconnects mid-call gets a **structured error, not a hang**.
  This is the cost of the single-binary merge and was accepted on the
  condition that it is handled here.
- `bridge/paths.py` translates Linux <-> DOS paths at the tool boundary, so
  neither side ever sees the other's spelling.
- `LAUNCH` is dropped, which is what keeps `util.c`'s JSON builder out of the
  guest entirely.

#### 3.2 — The RPC worker thread

The third thread, and the split is mandatory: an `EXEC` can run 60 seconds, so
running it on the UI thread freezes the window and running it on the reader
thread stops display frames being read.

- `_beginthreadex`, verified by 0.11 along with a message loop.
- `exec.c` forked with the `send_response(SOCKET, ...)` signature kept and only
  its body replaced -- that is what makes "lifted verbatim" nearly true.
- Two bugs to fix while forking, both already identified: `taskkill` is shelled
  unconditionally on timeout and does not exist on 9x (use `TerminateProcess`,
  which `probe.c` already does), and `HeapReAlloc` failure leaks the original
  block at two sites.
- Output is capped at 32KB and streamed in frames beyond that. On a machine
  with 28KB free, an unbounded capture buffer is not a performance question.

*Demo: "what is the volume label on C:?" -> `vol c:` runs on the Win98 box,
the permission dialog appears, and the answer comes back.*

#### 3.3 — Screenshots, in strips

0.10 measured `GetDIBits` producing `BI_RLE8` directly: 640x480x8 went
307,200 -> 27,088 bytes in 14ms, **11.3:1, done by GDI**. So the compression is
free. The problem is the buffer.

**A full-screen DIB is 307KB and there are 28KB free.** The old plan's
"capture one frame into one buffer" no longer fits, so capture proceeds in
**strips** of N scanlines into a small fixed buffer, each strip compressed and
sent as it is taken. Region capture stays the default -- `WINLIST` already
returns window rects, so "read this dialog" sends ~10KB instead of ~120KB and
costs ~80 vision tokens instead of ~850.

**New Phase 0-style gate, because this is an assumption and not a measurement:**

> **0.12 — does `GetDIBits` produce valid `BI_RLE8` for a scanline RANGE?**
> RLE8 is row-oriented so it ought to, but "ought to" is what 0.9 and 0.10
> were there to replace. If it does not, capture strips as `BI_RGB` 8bpp and
> RLE them on the bridge, which costs guest CPU but no extra memory.

`screen_palette_for_capture()` is still the one piece of `screen.c` worth
keeping: on an 8bpp display a fresh memory DC has only the 20 static colours,
so `GetDIBits` needs the live hardware palette selected in or the result is
colour-index noise. Hardware-verified, non-obvious, easy to lose.

#### 3.4 — The file sync layer *(replaces the Samba share -- see R6)*

- A manifest per side: `path -> (size, mtime, crc32)`. **CRC32 comes from
  miniz**, which is already vendored and already linked.
- Reconcile by exchanging manifests and diffing; transfer whole files
  compressed, since source files are a few KB and a patch format can wait.
- **The client wins on conflict.** The guest is the machine being worked on;
  the bridge holds a working copy.
- The manifest must **stream**, not be assembled whole in guest memory. A few
  hundred entries at ~50 bytes is fine; a whole-tree structure is not.
- Win9x specifics, recorded so they are not rediscovered: `ReadDirectoryChangesW`
  is NT-only and would kill the image at load, so watching is
  `FindFirstChangeNotification` plus a rescan of a small tree; FAT timestamps
  have **2-second granularity**, so mtime comparison needs a tolerance or
  everything looks modified; the filesystem is case-insensitive, has no
  symlinks, and 8.3 aliasing means one file can arrive spelled two ways.
- Line endings stay as decided: CRLF for files the guest consumes, stated in
  the system prompt rather than translated in a layer.

*Demo: Claude edits a file with its own Edit tool and the change appears on the
Win98 box a moment later.*

#### Verification gates for the phase

- **0.12** answered before 3.3 is built.
- A denied permission produces a refusal, not a hang, and a prompt left
  unanswered auto-denies rather than wedging the session.
- `EXEC` of a 16-bit tool returns correct non-empty output *through the GUI
  binary on the RPC thread*, not only through `probe.exe`.
- Guest free memory is measured again at the end of the phase, the same way as
  above, and compared against the 28KB baseline. A phase that quietly spends
  another megabyte needs to know it did.
- Every demo run once over `tc netem rate 40kbit`, per the standing rule.


### Phase 4 — settings and the rest of the surface *(revised 2026-09-17)*

**Permissions moved to 3.0** and are a prerequisite of Phase 3, not a successor
to it: the tool surface cannot ship without them. What is left here is smaller
than the original text, and some of it is already done.

**Done, ahead of schedule:**

- Model and effort selection, from the menu bar. The list comes from
  `get_server_info()["models"]` rather than a hardcoded table, so it carries
  `resolvedModel` and `supportedEffortLevels` with it.
- The SDK questions this section said to verify are answered: `set_model()`
  **exists**, `set_effort()` **does not**, so effort is session-start-only and
  changing it does the fork dance -- `resume=<id>, fork_session=True` -- with
  the transcript saying so. Model changes are live.
- Settings in an INI via `GetPrivateProfileString`, not the registry.
- The model list is **not** hardcoded after all, and there is no "Default"
  entry: default is an alias, not a model, so it is resolved and the model it
  points at is the one ticked.

**Still outstanding:**

- `PermissionResultAllow.updated_permissions` is documented inconsistently
  (one rendering says `dict`, an older snippet passes a list). **Check the
  installed SDK when 3.0 is written**, not this document.
- No API reads the effective permission rule table, so parse
  `.claude/settings*.json` on the bridge and push a read-only view.
- **Configure -> Server Host...** is a message box today and wants a real
  dialog. `wrc` is available for a proper resource, or a runtime
  `DialogBoxIndirectParam` template.
- **File -> Export as HTML** is wired and unimplemented. It needs every page
  from the bridge as one document, which is a chunked transfer.
- R5 (icons), R7 (multiple clients) and R8 (the Command menu) land here unless
  they are pulled forward.


### Phase 5 — Attach *(1–2 weekends)*

- Toolbar `TBSTYLE_DROPDOWN`, with **"Attach from P:\" as the default item** — those cost
  nothing on the wire (the bridge reads them off the share directly).
- **Attachments never transit guest memory.** The GUI sends a *path*; the bridge reads it
  and base64s it. A 10MB image can't be held on a 32MB box, let alone base64'd. Enforce
  10MB / 8000px bridge-side by re-encoding.
- A file on the guest's local `C:\` is the only case needing a chunked upload — progress
  bar and Cancel required, since 10MB is ~13s at 750 KB/s.
- Non-image files referenced by path (no arbitrary-file content block exists).
- **The cheap win: "attach a screenshot of my desktop."** `GetDC(NULL)` + `GetDIBits` +
  `screen_palette_for_capture()` — ~60 lines, most of the value of the whole screen tier
  with no tile diffing, no coordinate space, no `input.c`. Capture at 8bpp (0.6s on the
  wire at 800×600, against 1.9s for 24bpp). The palette fix is non-obvious and
  hardware-verified: a fresh memory DC has only 20 static colours, so `GetDIBits` needs the
  live hardware palette selected in or you get colour-index noise.

### Phase 6 — Computer-use tier *(3–4 weekends, defer)*

`input.c` lifted mostly intact — `UICLICK`/`UIDRAG`/`UIKEY`/`WINLIST` are pure syscalls
with no buffers, including the E0-extended-scancode table and the `VK_SNAPSHOT`-returns-0
fallback to scancode 0x37.

**[R2] `screen.c` is not lifted; it is replaced by ~60 lines.** Under "the guest transforms
nothing", the guest does `GetDC(NULL)` + `GetDIBits` into one buffer at reduced depth and
chunks it out. `SCREENDIFF`, the 64×64 tiling, `g_prev_pixels` and `resp_buf` all move to
the bridge, which has memory to spare and can diff against the previous frame there. The
only part of `screen.c` worth keeping is `screen_palette_for_capture()`: on an 8bpp display
a fresh memory DC carries only 20 static colours, so `GetDIBits` needs the live hardware
palette selected in or you get colour-index noise. Non-obvious and hardware-verified.

**[R4] The capture tool takes a rect, and region capture is the default.** `WINLIST`
already returns window rects, so "read this dialog" sends ~10 KB rather than ~120 KB and
costs ~80 vision tokens rather than ~850. Full-screen is the explicit exception, not the
default. Run the encoding-vs-legibility experiment (see Architecture) before fixing the
default depth.

**[R2] This deletes the worst memory item in the plan.** The first draft's `SCREENDIFF`
peaked around 4.4MB on the guest (`g_prev_pixels` 1.37 + `curr_pixels` 1.37 live at once +
`resp_buf` ~1.6) on a box with 8–12MB free — all of it to compute a diff the bridge can
compute for free. (The earlier claim that `g_prev_pixels` was "resident permanently" was
also wrong: it is lazily allocated and freed by `screendiff_reset()`.)

Gate honestly: the UI spec never asked for this, and Claude driving the mouse while you
type into `retroclaude.exe` on the same desktop is an unsolved UX problem.

---

## Revisions — 2026-09-17

Eight changes, in rough order of how much they disturb what is already built.
Four are done; four are design work that lands in Phase 3 or later. The two
architectural ones (**file sync** and **multiple clients**) each overturn a
decision made earlier in this document, and the reasoning is recorded because
"the plan said Samba" should not outlive the reason it said Samba.

### R1 — the chat list shows only sessions this client started  *(done)*

`list_sessions()` returns everything under the working directory, so the Win98
list was full of conversations from a desktop with a completely different mode
of working. Continuing one of those *from Win98* makes little sense: the
machine has different tools, a different screen and a different idea of what a
file path looks like.

There is no settable tag in `ClaudeAgentOptions`, so the bridge keeps its own
registry of the session ids it created and filters the listing through it.
Crude but honest, and it survives restarts.

### R2 — New session  *(done)*

There was no way to start a fresh conversation at all, which is faintly absurd.
File -> New Session.

### R3 — a system prompt that says where the agent actually is  *(done)*

The model has been answering as if it were on the machine it is running on. It
is not: it is on a proxy acting for a 1998 PC, and nothing told it so.

`SystemPromptPreset` takes an `append`, so this **extends** Claude Code's own
prompt rather than replacing it -- replacing it would throw away everything
that makes Claude Code work. The appended text covers:

  * what the proxied machine is, from real values the guest sends in HELLO:
    Windows version, CPU, total and free RAM, screen mode, free disk;
  * that edit tools operate on the bridge's working copy and are relayed to
    the guest, so they can be used normally;
  * that the guest is the thing being worked *on*, reached only through the
    MCP tools, never assumed;
  * and that it is a 200MHz machine with ~10MB free, so an answer that says
    "run this 400MB install" is a wrong answer however correct it is in
    general.

The last point is the one worth having: the model cannot make good decisions
about which tool to reach for if it thinks it has a modern machine.

### R4 — confirm before switching chats mid-turn  *(done)*

Switching closes the live client, which silently kills a running request. Now
it asks first.

Background sessions were considered and rejected for the same reason the plan
rejected a warm client pool: one live `ClaudeSDKClient` is a third of the work,
and a machine with ~10MB free should not be running several at once.

### R5 — icons  *(planned)*

Windows 98 is a colourful system and this app is a grey rectangle. At minimum
an application icon, so the title bar and taskbar are not generic; ideally a
small toolbar. Open Watcom ships `wrc`, so a `.rc` with an `.ico` is the
mechanism. Cheap, visible, and currently the largest gap between "works" and
"looks like it belongs on the desktop".

### R6 — **the file layer is a sync layer, not Samba**  *(planned, replaces Phase 3's share)*

This overturns the Samba decision, and the reasons are not only aesthetic:

  * **It was never verified.** 0.8 (long filenames over `vers=1.0`) is still
    blocked, so the whole file architecture rests on an untested assumption.
  * **The plan already named the real risk**: "SMB1 **round-trip latency**,
    not bandwidth, is what makes guest-side builds slow -- a compiler opening
    50 small headers over `vers=1.0` pays per-file latency."
  * **SMB1 is disappearing.** It needs `server min protocol = NT1` and
    `ntlm auth = ntlmv1-permitted` on a modern distro, and that is getting
    harder to enable each year, not easier.
  * And the decisive one: **Claude's native edit tools are fast and local on
    the bridge.** Editing over a mounted SMB1 share means every read and write
    crosses the wire; editing a local working copy and syncing the result does
    not.

**Shape.** One watched directory each side. Each keeps a manifest of
`path -> (size, mtime, hash)`. Reconciliation is a manifest exchange and a
diff; transfer is whole-file-compressed for small files, a patch for large
ones. Source files are a few KB, so whole-file is the common case and a patch
format can wait.

**The client is the source of truth on conflict**, as specified. That is the
right way round: the guest is the machine being worked on, and the bridge's
copy is a working copy.

**Win9x specifics that will bite, recorded now.** `ReadDirectoryChangesW` is
NT-only and will not load; `FindFirstChangeNotification` works but only says
"something under here changed", so the guest rescans a small tree rather than
receiving per-file events. FAT timestamps have **2-second granularity**, so
mtime comparison needs a tolerance or every file looks modified. The filesystem
is case-insensitive, has no symlinks, and 8.3 aliasing means a path can arrive
spelled two ways. Line endings are already a decided policy: CRLF for files the
guest consumes.

### R7 — the bridge should take multiple clients  *(planned)*

Today `self._guest` is singular and a second client is rejected. That rejection
was right for one guest and is wrong as a permanent design.

**Shape.** `self._guests: dict[client_id, GuestSession]`, with `client_id` sent
in HELLO and persisted in the guest's INI so a reconnect is recognised as the
same client rather than a new one. Everything already per-guest stays that way:
paginator, renderer, compressor, coalescers, model and effort. One live
`ClaudeSDKClient` **per client**, not one globally -- the bridge is a modern
machine and can afford several; the 32MB guest is the constrained side, and it
only ever has one.

The "second client is rejected, not evicted" rule stays, but narrows to its
actual intent: **two connections claiming the same `client_id`** is the
dangerous case, because one may be mid-turn. Two different clients are fine.

R1's registry becomes per-client, which is also what makes R1 correct on a
bridge serving more than one machine.

### R8 — a Command menu  *(planned)*

A menu of the slash commands that make sense here -- `/context`, `/goal`, and
whichever others are useful and cheap -- inserted into the input box rather
than invented as new protocol. Explicitly not all of them: anything needing a
UI the guest does not have is not worth faking.

## Repo layout — one repo

```
RetroAI/
  README.md  FEASIBILITY.md  NOTICE  LICENSE
  Makefile                    # exe test lint wine qemu
  make/ow.mk                  # -bm, WLINK_LNK, WINVER=0x0400, verified rules
  win/                        # [R2] one binary, one target
    frameproto.h  net.c  ini.c  log.c  w9x.c
    main.c  transcript.c  dialogs.c
    rpc.c  exec.c  input.c   # Phase 3 / Phase 6, same exe
    FORKED.md
  bridge/     server.py session.py rpc.py tools.py markdown.py fold.py
              pages.py paths.py replay.py protocol.py fakegui.py
  tests/python/  test_pe_imports.py (Phase 3) test_protocol.py
                 test_markdown.py test_fold.py
  tools/      qmp.py mkvm.sh
  vm/         run scripts (gitignore *.qcow2)
```

`FORKED.md` records which retro-agent files at which commit with what changes — MIT
attribution and upstream-diff tool in one.

## Explicitly cut

| Cut | Why |
|---|---|
| Experiment A (RichEdit benchmark) | **[R]** Tuning experiment, not a kill shot; set the constant by fiat and instrument the real path |
| `richcompat.h` as a `#define` | **[R]** No-op; replaced by `make lint` for 3.0-only message names |
| `-bg` as "required" | **[R]** `system nt_win` already does it |
| PE import test in Phase 1 | **[R]** `make check` covers it; real regression risk arrives in Phase 3 |
| Anchor list / jump-to-message | **[R]** Polish; `[<<] [<] [>] [>>]` is enough |
| **Second guest binary (`retroclaude_agent.exe`)** | **[R2]** One exe, one connection; `main.c` never forked, ~1,000 lines saved |
| **Guest-side `SCREENDIFF` / tiling / scaling / compression** | **[R2]** Bridge has the memory; deletes a 4.4MB peak on a 12MB box |
| `GetVersion` threaded/select divergence | MinGW artifact; OW has no emutls, so the concern evaporates |
| Agent as listener | Dial out: ~60 lines written to delete ~700 |
| UDP discovery, `wmake`, `registry.c` | Fleet feature / GNU make works / INI instead |
| `UPLOAD`/`MKDIR`/`DELETE`, `LAUNCH` | Samba covers them; `LAUNCH` drags in the JSON builder |
| Channel multiplexing over one socket | Process split already separates them |
| Scrollback trimming, custom-drawn view, MSHTML | Pagination supersedes / see Architecture |
| Warm multi-session client pool | One live client, switch = close + resume |
| Integrated QMP auto-recovery | `tools/qmp.py` run by hand |

## Effort

Assuming ~10 h/week. **Phase 0 through Phase 4: roughly 19–27 weekends, 4–6 months
part-time.** **[R]** The first draft carried no estimates at all and was shaped like
FEASIBILITY's 3–4 month figure while scoped like its 9-month one. **[R2]** The single-binary
merge took ~3 weekends out of Phase 3 (no `main.c` fork, no second build/shutdown path) and
~2 out of Phase 6 (no guest-side diffing).

Most likely to stall: **Phase 2**, which is why it's now four demos instead of one.
Second: **Phase 0 attrition** — weekends with no product and a CSV as the reward is how
part-time projects quietly die, which is why the benchmark is cut and the first artifact is
a Win98 window showing text from Linux.

## Verification

- `make check` — PE version gate, imports, W-suffix list, CMOV, size. **Re-run after adding
  `-bm`**; the recorded numbers predate it.
- `make lint` — grep for RichEdit 3.0-only messages
- `make test` — protocol round-trip, markdown→runs, CP1252 fold; PE allowlist from Phase 3
- `make wine-smoke` — code sanity only, never a Win98 proof
- `bridge/fakegui.py` — full end-to-end chat with no Win98 involved
- Per-phase demos as listed; Phase 2 has four
- Phase 0 gates: guest boots and the OW binary **loads** (settles the W-suffix question);
  long filename survives `vers=1.0`; **EXEC of a 16-bit tool returns correct non-empty
  output**, not merely "doesn't hang"; **[R3] measured deflate throughput on the real
  P200MMX**, since every threshold in the adaptive-codec design is currently an estimate
- **[R3] Test over a throttled link, not just the LAN.** `tc netem rate 40kbit` on the
  bridge side simulates dialup without a modem. Every phase demo should be run once
  throttled — it is the cheapest way to stop LAN-speed assumptions leaking into the design
