# Gaston Code — native Windows 98 client for Claude Code

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

Research: `docs/FEASIBILITY.md` (707 lines).

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
    UI thread                            +-- in-process MCP server (mcp__machine__* tools)
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
| Compression | **both** | **[R3]** first deliberate exception — see below |
| **CRLF normalization / file classification** | **guest** | **[R6]** second deliberate exception — single disk pass on 1998 platters; pinned by agreement tests |

**[R3] Compression and sync classification are the two exceptions to "transforms nothing".**
Decompressing bridge→guest text is near-free and always worth it. Compressing guest→bridge
uploads costs real CPU, so it is *adaptive*: cheap RLE on a fast link, RLE+deflate on a
slow one, chosen from measured throughput. Sync classification (`win/sync/sync.cpp`)
classifies text vs binary and CRLF-normalises in a single disk pass on 1998 IDE platters to
avoid double reading, pinned against the bridge by cross-language agreement tests.
Everything else on this list stays on the bridge unconditionally.

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

**Progress UI.** A status-bar pane showing direction, bytes done / total and transfer
rate (`rx 12k @ 45k/s`) updated at 1 Hz, enough to show the link is moving. Progress
bar, ETA and named transfer were explicitly cut to keep the UI simple; `CANCEL` is
deferred to Phase 5 upload (`BACKLOG.md` §6).

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

### Tool naming: nothing is called `win98_*` **[2026-09-17]**

The guest happens to be Windows 98 today. The bridge, the protocol and the tool
surface should survive a DOS box, a Windows 3.1 box or an NT 4 box on the other
end -- **only the guest binary is OS-specific**, and the project may well be
pointed at another machine.

So the in-process MCP server is **`machine`** and its tools are bare verbs:

    mcp__machine__exec            Phase 3, SHIPPED
    mcp__machine__sync_status     } Phase 3.3, SHIPPED -- added by R6; not
    mcp__machine__sync_resolve    } in this list's first draft, see below
    mcp__machine__list_dir        } Phase 3+, see "the exec test" -- not yet
    mcp__machine__list_processes  } built
    mcp__machine__kill_process    }
    mcp__machine__list_windows    } Phase 6, the UI tier -- not yet built
    mcp__machine__screenshot      }
    mcp__machine__ocr             }
    mcp__machine__click           }
    mcp__machine__send_keys       }
    mcp__machine__send_action_sequence
    mcp__machine__launch          }
    mcp__machine__get_clipboard   }
    mcp__machine__set_clipboard   }

**[R -- 2026-09-18]** `read_file` and `write_file`, listed here in the first
draft as "Phase 3, file sync", were dropped when R6 replaced the Samba share
with the sync layer: Claude edits the bridge's own working copy with its
native `Edit`/`Write` tools (R3), and the sync layer relays the result, so a
dedicated read/write tool pair never had a job. `sync_status` and
`sync_resolve` are the sync layer's actual MCP surface, and they shipped
before several items above them in this list that have not. See the exec-test
table under 3.1 for what each does and why it exists.

This matches vocabulary the codebase already uses -- `bridge/machine.py`
describes the connected machine, and HELLO reports its `os`, `cpu`, `mem_kb` and
`screen`. It is also unambiguous about direction in a way `host_*` or `remote_*`
would not be: the bridge is not the machine; the thing on the far end of the
socket is.

**The prefix comes from the dict key in `mcp_servers`, not from the `name=`
argument** (`subprocess_cli.py:664-670` keys the JSON by the dict key). They must
match.

Wire frame types were already OS-neutral verbs (`EXEC`, `INPUT`, `SWITCH`) and
stay that way. **Where the OS actually matters it is a runtime branch, not a
name**: `probe_exec` (`probe.c:339`) already does `GetVersion() & 0x80000000` to
pick `command.com` over `cmd.exe`. That branch is the only place the guest's
vintage should appear.

`docs/FEASIBILITY.md:206-215` sketches a `win98_*` surface. That naming is
**superseded**; the file is kept as the historical research document.

### The bridge owns markdown

**[R -- 2026-09-18: rewritten to match what shipped; the "pre-styled runs" design below was
never how the shipping code works.]** The original plan for this had the wire carry
**pre-styled runs** -- `TEXT\nstyle=code\n\n<raw bytes>` -- with Win98 looking `style` up
in a static `CHARFORMAT2` table via `EM_SETCHARFORMAT(SCF_SELECTION)` + `EM_REPLACESEL`.
That was the RichEdit design. It went with RichEdit when RichEdit was reversed for MSHTML
(see **Renderer: MSHTML** above): the bridge now sends **finished HTML**, and the `TEXT`
frame carries `mode=append|live` instead of `style=` -- `append` adds a finished block to
the transcript, `live` replaces the block still being written, wholesale, as it grows. The
`style=` constructor and the `CHARFORMAT2` table it implies were dead code with no caller
on either side and have been removed from `bridge/wire/protocol.py`; see the **Protocol
reference** under Phase 3.3 for the frame's actual headers. The rest of this paragraph
still holds: no markdown parser in C, and the bridge decides everything about how a block
looks before it ever reaches the wire.

**[R -- 2026-09-25: half of "how a block looks" is misfiled; this records where the
seam is.]** "The bridge decides everything about how a block looks" fuses two jobs.
*Structure* -- markdown to blocks, escaping, pagination -- is a transform: it needs
lookahead and a parser, and it belongs here for every reason this section gives.
*Decoration* -- `STYLESHEET` (`bridge/view/html.py:448`), inlined by `document()`
(`:622-630`) and sent once per connect from `server.py:341` -- is one client's
appearance, written to IE5's constraints, shipped from the far side of the wire. That
contradicts the standing rule that the bridge and the protocol should survive being
pointed at MS-DOS, Windows 3.1 or NT 4: a DOS client cannot use a byte of that
stylesheet, and a more capable client cannot restyle without the bridge's consent. The
class *vocabulary* is already a shared contract (`win/app/htmlview.h:61-76`, pinned by
`tests/python/meta/test_guest_css.py`) and the guest already composes markup against it
(`win/app/statusbar.cpp:146`), so what is shared and what is owned are currently the
wrong way round. Deferred rather than built -- `BACKLOG.md` §13, where export is the
awkward constraint.

Markdown needs lookahead, so the bridge buffers to a safe boundary (newline, or ~200 chars
of unambiguous text). Streaming granularity becomes one line — which is also what a P2
repaint budget wants.

**No JSON on the guest.** Grammar: `TYPE\n` + `key=value\n` + blank line + raw body. See
the **Protocol reference** under Phase 3.3 for the full grammar, the header limits and the
frame inventory as shipped.

**[R] Frame header keys present from Phase 1**, even where the guest ignores them, because
adding them later touches every frame and handler on both sides:

- **[R -- 2026-09-18: retired.]** `seq=` monotonic per session — what `resume
  ... from=` replays from. The guest never sent `from=`, so `replay.since()`
  was unreachable while `append()` still filled a 20,000-frame buffer and
  `_send` still stamped every outbound frame with it -- dead code the bridge's
  own comments admitted (server.py:704) and BACKLOG.md §5 tracked. Retired
  along with `from=`, `next_seq=` and the replay buffer itself
  (`bridge/wire/replay.py`, deleted); the never-replay invariant for
  EXEC/EXECKILL/SYNC* still holds, now as a property of there being no
  re-delivery mechanism left at all rather than of a `stamp=` flag. See
  BACKLOG.md §5 for the fuller account.
- `page=` — ignored in Phase 1, switches the control in Phase 2
- `sess=` — see multi-session below
- **[R3]** `enc=` — `raw` only in Phase 1; `deflate` was added for Phase 2a and is what
  ships. **[R -- 2026-09-18]** `rle8` and `rle8+deflate`, named here and again in the
  Phase 6 screenshot design, were **never built** -- `bridge/wire/compress.py` defines only
  `ENC_RAW` and `ENC_DEFLATE`, and the guest (`win/net/net.cpp`) drops the connection on
  any `enc=` value it does not recognise. Whether that gap is intentional (the screenshot
  codec is Phase 6 and hasn't landed, so there may be nothing to compress with `rle8` yet)
  or a forgotten scope cut is recorded as an open question in `BACKLOG.md` §8, not decided
  here.

**[R] Protocol error handling (missing entirely from the first draft).** The first draft
said: malformed header, unknown `TYPE`, repeated key, truncated body → `ERROR\ncode=\nmsg=`
frame and close. **What actually shipped only agrees on malformed framing** -- a bad
length prefix or an unparseable header genuinely closes the link on both sides. The other
two cases diverge, and the two sides diverge from *each other*, not just from this
paragraph:

- **Unknown `TYPE`. [R] Decided 2026-09-18: IGNORE AND LOG, NEVER CLOSE -- and what
  shipped was already right, so the paragraph above is what changes.**

  The original rule (reply `ERROR` and close) makes adding a frame type a FLAG DAY: the
  moment one side learns a new type, every older peer drops the link on first sight of it.
  That is the opposite of what the capability handshake is for -- `HELLO` exists so an
  option can be added without both ends shipping together, and a protocol that closes on
  anything unrecognised throws that away at the frame layer.

  So the receiving side logs the type and drops the frame, and the connection survives. The
  bridge ALSO replies `ERROR unknown_type`, which is kept deliberately: it costs one frame,
  it is how a developer finds out their new type is not wired up on the far side, and it
  does not close anything. The guest does not reply, and that asymmetry is fine -- it has no
  console, its log is the diagnostic, and a guest answering every stray frame would give a
  confused bridge a second thing to be confused about.

  Unchanged in the code. `bridge/server.py`'s dispatch `else` and `win/app/main.cpp`'s
  `render_frame` default already do exactly this; the specification was the part that was
  wrong, and it has been since before either side was written.
- **Repeated header key.** The bridge (`bridge/wire/protocol.py`, `FrameReader.read`)
  raises `ProtocolError` and the connection is torn down, which matches the original
  design. The guest (`win/net/frameproto.cpp`, `rc_frame_parse`) has no duplicate check at
  all: a second `k=v` line is parsed and stored as an ordinary header, and
  `rc_frame_get` returns whichever occurrence came **first** -- so a duplicate is silently
  accepted rather than refused, and the two occurrences don't even agree on which one wins
  in the two functions that read it (`rc_frame_get` always returns the first; nothing reads
  the rest).

This is left as an open question rather than resolved here: reconciling it means picking
one behaviour for two frame types on two codebases that currently disagree with the design
and with each other, and that choice has not been made. **Cap frames at 64KB in our own
`frameproto.h`** is the one part of this paragraph that did ship as designed. retro-agent's
`MAX_FRAME_SIZE` is 32MB — the whole machine — and its own two implementations already
disagree (32MB vs 16MB in `agent-linux`), so the limit is asserted in HELLO alongside the
version, not just the version.

**[R] Reconnect (missing entirely).** Bridge-down-at-startup shows a disabled input box and
a status-bar message, never a modal that blocks the message loop. Backoff 1/2/4/…/15s
driven by `WM_TIMER`, never a sleeping loop. On reconnect: guest sends fresh `HELLO`
with `sess=<sess>`; bridge explicitly re-issues live state (`perms.reissue()`,
`_reissue_halt`, `_reissue_turn`, etc. at `server.py:874-903`) rather than relying on a
replay buffer. **Always `shutdown(SD_SEND)` + short grace before `closesocket`** — FEASIBILITY
treats the Win98 Winsock RST crash as real and field-verified.

**[R] Control frames must not queue behind streaming text.** `PERMREQ` shares the GUI
stream with streaming `TEXT`. Rather than dropping transcript text (which users scroll back
to), control frames (`PERMREQ`) bypass coalescer buffering and are written directly to the
socket, while the bridge explicitly flushes pending text before starting a tool call.

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

### Renderer: MSHTML **[R -- 2026-09-18, reverses the RichEdit 2.0 design below]**

The shipping transcript is `win/app/htmlview.cpp` / `win/app/htmlview.h`, and it hosts
**MSHTML**, not RichEdit. The RichEdit design in the section immediately below was built
first and then reversed. It is kept rather than deleted, because the Open Watcom header
investigation in it is still correct and the reasoning for choosing RichEdit was sound
given what was known at the time -- the reversal is a fact that arrived later, not a
repudiation of the reasoning.

**What forced the reversal**, from `htmlview.h`'s own header comment: MSHTML measures at
about 4MB against a 32MB guest, which is real money on this budget, but it buys padding,
borders, background tints, real bulleted lists and tables -- none of which RichEdit 2.0
can do -- and the engine is already resident because Explorer hosts it for folder Web View,
so a second host pays only its own instance data rather than the DLL tax in full. The
"MSHTML rejected for v1" paragraph below weighed COM boilerplate against
`CreateWindowEx`, and that tradeoff didn't change; what changed is that RichEdit's
ceiling on what a transcript could look like turned out to matter more than the
boilerplate cost of the alternative.

**Where this was recorded, and where it was not.** Only in `htmlview.h`'s header comment
and in `PROGRESS.md`'s rolling log -- not here, until now. `make lint` (`Makefile:35`)
still greps `win/` for RichEdit-3.0-only message names, which is now a check against a
renderer that does not exist; closing that is a build-file change, so it is tracked in
`BACKLOG.md` rather than fixed in this design document.

See **Protocol reference**, under Phase 3.3, for how this reversal also changed what
travels on the wire: `style=`/`CHARFORMAT2` runs became finished HTML tagged
`mode=append|live`.

The RichEdit 2.0 design below is superseded in full by the above. It is kept because the
Open Watcom header findings in it (`_RICHEDIT_VER` gating, the `make lint` replacement for
`richcompat.h`) are still accurate facts about Open Watcom, independent of which renderer
ended up using them.

### Renderer: RichEdit 2.0 **[SUPERSEDED -- see MSHTML above]**

`riched20.dll` ships with Win98. `CHARFORMAT2`/`PARAFORMAT2` cover bold, per-run monospace,
background tint, indents, bullets.

**[R] `#define _RICHEDIT_VER 0x0200` does nothing and `richcompat.h` as first specified was
worthless.** In OW's `h/nt/richedit.h`, *nothing* is gated on `0x0300` — the only gates are
`>= 0x0200` (class name, A/W typedefs) and `== 0x0100`. `EM_SETTEXTEX` (:116),
`EM_GETTEXTEX` (:113), `EM_AUTOURLDETECT` (:110) and `CHARFORMAT2` are all defined
unconditionally. Preprocessing at 0x0200 yields a byte-identical header.
**Replacement: a `make lint` grep for the 3.0-only message names**, listed explicitly, run
in CI. `RICHEDIT_CLASSA == "RichEdit20A"` is confirmed (:36) and ungated.

**MSHTML rejected for v1. [R -- 2026-09-18: reversed; see "Renderer: MSHTML" above.]**
Direct hosting is Microsoft-documented and is what Outlook Express did — but OE rendered
a short fresh document per message, never mutating one long-lived document at 15Hz.
MSHTML's design centre is infrequent async whole-document loads. ~600–1200 lines of
plain-C COM boilerplate vs RichEdit's one `CreateWindowEx`, and its multi-MB DLL tax is
fixed per process so pagination doesn't reduce it. *Revisit only if sealed history pages
ever need tables / syntax highlighting / inline images — those are short static documents,
which is the pattern MSHTML is good at.*

The revisit happened sooner than "only if": tables and real lists turned out to matter for
the live transcript, not just sealed history, and the DLL-tax argument turned out to be
moot because Explorer already residents the engine. Kept for the COM-boilerplate-vs-
`CreateWindowEx` tradeoff, which is real and was correctly stated -- it was simply outweighed.

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
a significant portion of it. `dwAvailPhys` excludes the disk cache, which Win98 shrinks
under pressure (measured as low as 0 KB with an app that still functioned normally;
Phase 4 measured 5.2MB / 4.5MB free). While allocations were not failing, physical RAM
and 16-bit GDI/USER heaps are tight (see `docs/mem-audit/`). So **every allocation in
this phase has to be bounded and small**, and screen capture moved out of this phase
entirely into Phase 6.

#### 3.0 — Permission prompts *(prerequisite, not optional)*

Nothing else in this phase may land first. Handing Claude a tool that runs
programs on the user's computer without `can_use_tool` is `bypassPermissions`,
which is exactly what FEASIBILITY condemns in claudewin9x and says not to
inherit.

**What the SDK actually provides**, checked against the installed 0.2.153
rather than taken from this document:

```
can_use_tool(tool_name, input: dict, context: ToolPermissionContext)

ToolPermissionContext:
    tool_use_id      correlation id, already unique -- do not invent one
    suggestions      list[PermissionUpdate] the CLI itself proposes
    title            } human-readable, already written
    display_name     } -- the bridge does not need to summarise
    description      }
    blocked_path, decision_reason, agent_id, signal

-> PermissionResultAllow(updated_input=None, updated_permissions=list|None)
-> PermissionResultDeny(message=str, interrupt=bool)
```

Three consequences, all of which make this smaller than the original sketch:

- **`tool_use_id` is the correlation id.** No counter, no uuid.
- **`suggestions` is what "Always" applies.** The CLI proposes the rule it
  would add; returning it in `updated_permissions` is the whole of "remember
  this". **Do not invent a rule format** -- an earlier draft of this section
  proposed keying on the first token of a command line, which would have been
  a second, worse permission model sitting beside the real one.
- **`title` / `display_name` / `description` are the prompt text.** The bridge's
  `_summarise()` is for the tool-activity line, not for this.

**"Always" is session-scoped by default.** `PermissionUpdate.destination` can
write to `userSettings`, `projectSettings` or `localSettings`; a dialog on a
Win98 box quietly editing the developer's settings files is a surprise nobody
asked for. Default to the session, and leave anything more permanent to the
settings UI in Phase 4.

**Deny sends `PermissionResultDeny(message=..., interrupt=False)`.** A single
refusal is not a reason to tear down the turn -- the model should be told no
and allowed to carry on. v1 sends a fixed message; a typed reason is friction
on a 640x480 screen and can wait.

##### The prompt is modeless, and that is a deliberate deviation

This document has said "permission requests as real modal dialogs" since the
first draft. Build it modeless instead -- a prompt bar above the input box,
with Allow / Always / Deny, Send disabled while one is pending, Esc to deny.
Three reasons, in order of weight:

1. **A modal pumps messages.** This document already flagged it: `WM_RC_FRAME`
   dispatches *during* the modal, so the transcript is re-entered while Claude
   is mid-stream. Modeless has no nested message loop and the problem does not
   arise.
2. **Deferring frames costs memory we do not have.** The obvious fix for (1) is
   to queue frames for the duration -- but they are heap blocks, and the guest
   has **28 KB free**. A thinking-heavy turn could queue a lot.
3. **It is better behaviour anyway.** A modal freezes the window while the
   model waits; a bar lets the transcript keep streaming behind it, which is
   how you can actually see *why* the tool was requested.

Disabling Send while a prompt is pending keeps the one property the modal was
there for: the request cannot be ignored.

##### Sequencing and failure

- **One prompt at a time.** The model can request tools in parallel; a single
  guest can show one bar. The bridge queues them and sends the next when the
  previous resolves. No second UI state to design.
- **`can_use_tool` has no timeout and a pending prompt wedges the session
  forever.** Both mitigations are required: a bridge-side deadline (~10 min)
  that auto-denies with an explanatory message, and `fakegui.py` as the manual
  escape hatch for a wedged prompt.
- **A guest that disconnects with a prompt pending is denied immediately**, not
  after the deadline. Waiting ten minutes for a machine that has gone away is
  the failure mode the deadline exists to prevent, not one to reproduce.
- The pending request survives a **reconnect**: the bridge explicitly re-issues
  `PERMSTATE` during `_attach`, restoring the bar.

*Demo: ask for something that needs a tool. The bar appears with the CLI's own
description of what is being requested, the transcript keeps streaming behind
it, Deny is pressed, and Claude says it was refused and carries on.*

#### 3.1 — The tool surface

- `bridge/tools.py` with `create_sdk_mcp_server` and one tool,
  `mcp__machine__exec`. See **Tool naming** under Architecture: nothing is
  called `win98_*`.
- **Three timeouts, and they must be ordered.** An earlier draft claimed
  `MCP_TOOL_TIMEOUT` "documents stdio and WebSocket servers; ours is in-process
  and is not covered". **That is backwards** -- the `sdk` server config carries a
  `timeout` described as overriding `MCP_TOOL_TIMEOUT` *for this server*, and
  only the *idle* timeout is disabled for `sdk`. Worse, `types.py:653-662`
  records that on mcp 1.x an SDK server's tools are **not cancelled when Claude
  Code abandons a call -- they run to completion**. So:
  guest `run_capture` limit < bridge `asyncio.wait_for` < MCP server `timeout`
  (`mcp_servers={"machine": {**server, "timeout": ...}}`; `subprocess_cli.py`
  forwards every key except `instance`). Get it wrong and the CLI abandons the
  call while the Python body runs on, the guest keeps running the child, and the
  result arrives for a future nobody awaits.
- A guest that disconnects mid-call gets a **structured error, not a hang**.
  This is the cost of the single-binary merge and was accepted on the
  condition that it is handled here.
- `bridge/paths.py` translates Linux <-> DOS paths at the tool boundary, so
  neither side ever sees the other's spelling.
- `LAUNCH` is dropped **as a lift**, which is what keeps `util.c`'s JSON builder
  out of the guest entirely. A `launch` of our own comes back in Phase 6 -- see
  the exec test below.

**The exec test: a tool earns its place only if `exec` does the job badly.**
Every tool costs context on *every* request, so the long tail belongs to `exec`.
Candidates that pass, in rough order of when they are needed:

| tool | why `exec` is not enough |
|---|---|
| `list_dir` | parsing `dir` on Win98 is hostile: 8.3 versus long names, CP437, localised headers. `FindFirstFile` returns structure instead. With no path, enumerate drives (`GetLogicalDrives`) |
| `read_file` / `write_file` | needed for the sync layer anyway; binary-safe, chunked, encoding-explicit |
| `list_processes` / `kill_process` | **Win98 has no `tasklist` and no `taskkill`.** There is no command line for this at all -- `CreateToolhelp32Snapshot` (which is 9x-only, and absent on NT 4) is the sole route. The single strongest case on this list |
| `launch` | `exec` waits for exit. You cannot drive Notepad's UI with a tool that blocks until Notepad closes. `command.com /c start x` works but is fragile; `CreateProcess` without the wait is ~10 lines |
| `get_clipboard` / `set_clipboard` | the escape hatch for text no message will surrender: Ctrl+A, Ctrl+C, read it. ~30 lines |

Candidates that **fail** the test and stay out: `registry_read` / `registry_write`
(`regedit /e` and `/s` are ugly but they work), `ini_read` / `ini_write`
(`read_file` covers it), `find_files` (`dir /s /b`), and `sysinfo` (HELLO already
sends it and the system prompt already states it).

**[R -- 2026-09-18] What actually shipped is a different, shorter list**, and it
does not match the table above. `bridge/agent/tools.py` builds exactly three
tools: `exec` (as designed above), and two the table never named because R6
(the sync layer replacing Samba) had not been written yet when this table was:
`sync_status`, which reports which files failed to reach the guest and which
are conflict-halted, and `sync_resolve`, which lifts a halt after the user
confirms they are done with a file. `read_file` / `write_file` never shipped
**at all** -- once R6 made the bridge's own working copy the thing Claude
edits (native `Edit`/`Write`, relayed by the sync layer rather than by a
tool call, per R3), a dedicated read/write tool pair had no job left to do.
`list_dir`, `list_processes`, `kill_process`, `launch`, `get_clipboard` and
`set_clipboard` remain candidates only -- none are built. The `mcp__machine__*`
list under **Tool naming** above is corrected to match.

#### 3.2 — The RPC worker thread

The third thread, and the split is mandatory: an `EXEC` can run 60 seconds, so
running it on the UI thread freezes the window and running it on the reader
thread stops display frames being read.

- `_beginthreadex`, verified by 0.11 along with a message loop.
- `win/rpc/exec.cpp` derived from `run_capture()` in `probe.c`, rewritten rather
  than forked from retro-agent to eliminate six upstream defects (including
  unconditional `taskkill` and `HeapReAlloc` leaks; see `win/rpc/exec.cpp:8-22`).
- Output is capped at 32KB and streamed in frames beyond that. On a machine
  with tight memory (see `docs/mem-audit/`), an unbounded capture buffer is not a
  performance question.

*Demo: "what is the volume label on C:?" -> `vol c:` runs on the Win98 box,
the permission dialog appears, and the answer comes back.*

#### 3.3 — The file sync layer *(replaces the Samba share -- see R6)*

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
  `FindFirstChangeNotification` plus a rescan of a small tree -- **verified by
  experiment 0.14 on Win98 SE: all three symbols present, and create / modify /
  delete / create-in-subdirectory all signal with `bWatchSubtree=TRUE` on FAT.
  Resolved via `GetProcAddress` anyway, so a guest without it polls instead of
  failing to load**; FAT timestamps
  have **2-second granularity**, so mtime comparison needs a tolerance or
  everything looks modified; the filesystem is case-insensitive, has no
  symlinks, and 8.3 aliasing means one file can arrive spelled two ways.
- Line endings stay as decided: CRLF for files the guest consumes, stated in
  the system prompt rather than translated in a layer.

- **One mirror per CHAT, not per client, and not one shared by everything.
  [R] Decided 2026-09-18, and it is the decision R7 was blocked on.**

  The question was what the mirrored folder means once two machines are
  connected, and the three answers on the table were one mirror per client, one
  shared mirror with a per-guest dimension added to the baseline, or one mirror
  with a writer lease. Keying it to the CONVERSATION instead dissolves the
  problem rather than managing it: the number of mirrors is bounded by chats
  rather than by clients times chats, so the combinatorial case never arises.

  What that buys is the part worth protecting. One mirror, one baseline and one
  guest still means the reconcile stays three-way, so the state table that five
  adversarial review rounds went into is untouched -- no N-way merge, no lease
  to time out and steal, and the `tree_id` guard keeps working as written. Two
  people work at once by working on different conversations.

  The default work folder becomes `C:\Gaston\<shortened chat name>`, with a
  different folder selectable when a chat is started (the UI for that is a
  Phase 4 item -- see "The work folder, chosen when a chat is started").

  **The consequence to design against, because it is not free.** The guest's
  sync root is one string read once from the INI at startup, so per-chat
  folders mean the bridge has to tell the guest which folder this conversation
  uses rather than the guest declaring it in `HELLO` as it does today. And the
  path budget shrinks: `C:\WORK\` is 8 characters where `C:\Gaston\` plus a
  chat name plus a separator is 11 plus the name, all of it coming out of the
  259 that `RC_CAP_MAXPATH` allows, before the 11 bytes `RC_CAP_WORKRESERVE`
  already reserves for `.gaston-tmp`. **The shortened chat name therefore needs
  a hard cap, and `paths.check` has to count the real prefix** -- the same
  arithmetic that `03.13`'s static assert and the working-suffix reserve exist
  to protect. A generous chat name would otherwise quietly cost the user the
  ability to sync deep paths, which is exactly the class of silent limit this
  layer keeps being bitten by.

*Demo: Claude edits a file with its own Edit tool and the change appears on the
Win98 box a moment later.*

#### Protocol reference **[R -- 2026-09-18, new]**

This section did not exist before today. The frame vocabulary has been documented only
by its two implementations plus a rolling narrative in `PROGRESS.md`, which an audit
called the single largest documentation gap in the project. What follows describes the
wire **as it ships**, verified against `win/net/frameproto.h` / `frameproto.cpp`,
`bridge/wire/protocol.py`, `win/net/caps.h` and `bridge/files/caps.py`. It is a reference,
not a tutorial -- see "The bridge owns markdown" above for the design reasoning.

**Frame grammar.** One frame is a 4-byte little-endian length prefix followed by that many
bytes of payload:

    frame   := uint32le(len) payload            len <= RC_MAX_FRAME (64 KB)
    payload := TYPE "\n" *(key "=" value "\n") "\n" body

No JSON, no nesting: `TYPE` is the frame kind, the header block is flat `key=value` lines
terminated by a blank line, and everything after that blank line is the body, exactly
`body_len` bytes, never itself parsed as text unless the frame says so.

**Limits, and what happens when one is hit** (`RC_MAX_TYPE` etc. in `frameproto.h`):

| Limit | Value | Guest behaviour when exceeded |
|---|---|---|
| Frame | `RC_MAX_FRAME` = 64 KB | `rc_frame_build` refuses to build it (send-side); a length prefix over this on receive drops the link |
| Type | `RC_MAX_TYPE` = 32 bytes | An over-long type line drops the frame |
| Headers per frame | `RC_MAX_HEADERS` = 16 | Drops the frame. (Until 2026-09-18 the 17th and later header lines were parsed and **silently discarded** -- the one limit of the three that lost well-formed data without saying so. If the lost header was `id`, an ack was answered to nobody. The bridge's largest frame carries six headers, and `tests/python/test_guest_frame.py` counts them out of `protocol.py` by AST so that adding a seventeenth fails in a test rather than dropping a live link.) |
| Header key | `RC_MAX_KEY` = 32 bytes | Drops the frame |
| Header value | `RC_MAX_VAL` = 256 bytes | Drops the frame. (This used to be a silent truncation to 255 bytes -- fixed, because a truncated `cwd` header named a different, *existing* directory to `CreateProcessA` rather than failing loudly.) |

**Encoding.** Header values are percent-encoded for `%`, `\n` and `\r` only, so the guest's
parser is `strchr` and nothing more. The bridge (`protocol.py`'s `_quote`) escapes every
byte outside `0x20..0x7E` plus `%`, folding to CP1252 first. The guest's decoder
(`unquote` in `frameproto.cpp`) accepts any `%XX`, which is more permissive than what the
bridge ever sends.

**[R]** For the whole of Phases 1-3 this rule held in exactly one direction. `rc_frame_build`
copied header values raw while the bridge decoded everything it received, so a guest header
value of `50%41` arrived as `50A` -- silently, self-consistently, and wrong on every layer
below. A value containing `\n` was worse: it ended the header line, and what followed was
parsed as a further header, so a frame could arrive carrying an `id` nobody sent, and `id`
is what routes an ack. Reachable from ordinary data, not from an attack: these values come
from `.gaston.ini`, from `GetVersionExA` and from the filesystem. `append_quoted` in
`rc_frame_build` closes it. The rule is now checked ACROSS the two implementations rather
than within one -- `tests/python/test_guest_frame.py` feeds the shipping builder's bytes to
`bridge/wire/protocol.py`'s own reader, the same loop `bridge/files/manifest.py` was given,
because a format two halves of one file agree on is not a protocol.

**Malformed framing, an unknown `TYPE`, and a duplicate header key are three different
things, and only the first is handled the same way on both sides.** A bad length prefix or
an unparseable header genuinely drops the link everywhere. The other two do not match each
other:

- **Unknown `TYPE`. [R] Decided: ignore and log, never close.** Both sides already did
  this; the original reply-and-close rule was the thing that was wrong, because it makes
  adding a frame type a flag day and the `HELLO` handshake exists precisely so options can
  be added without one. The bridge additionally replies `ERROR unknown_type` as a developer
  diagnostic and keeps the link. The guest logs and drops. See "Protocol error handling"
  above for the full reasoning.
- **Duplicate header key.** **[R] Now the same on both sides.** Bridge: `FrameReader.read`
  raises `ProtocolError` and the connection is torn down, which is what this document always
  specified. Guest: `rc_frame_parse` had no duplicate check at all -- the second occurrence
  was stored as an ordinary header and `rc_frame_get`, a linear scan, returned the first. So
  a repeated key was accepted, kept, and never read. Silent first-wins is the worst of the
  three available answers: the sender believes it set a value, the receiver acts on a
  different one, and nothing disagrees out loud. The guest drops the frame now. Safe to be
  strict because the bridge builds headers from a dict and cannot emit a duplicate even by
  accident; `framecheck.exe` pins both the refusal and the case it must not catch (two
  distinct keys).

**Unknown `TYPE` is still genuinely unresolved** -- see "Protocol error handling" above. The
bridge answering `ERROR` and staying up is arguably better than this document's original
"reply and close", and the guest doing neither is arguably worse than both. Not decided
here, because picking one is a design call and this section is for recording what is true.

**Capability handshake.** The guest's first frame is always `HELLO`, currently 25 headers
(`win/net/net.cpp`, `rc_net_start`), in three groups:

- **Connection identity**, always present: `ver` (protocol version, refused if it does not
  match `RC_PROTO_VERSION`/`PROTO_VERSION`), `secret` (shared, plaintext, refused if wrong),
  `max_frame` (refused if it does not match `RC_MAX_FRAME`/`MAX_FRAME`).
- **What this machine is**, used for the system prompt and for sizing pages, sent when
  available rather than guaranteed: `sync_root`, `mem_kb`, `mem_free_kb`, `os`, `cpu`,
  `screen`, `disk_free_kb`, `sess`, `acp`, `oemcp`.
- **Filesystem and path capability declaration** (`win/net/caps.h`, mirrored in
  `bridge/files/caps.py`), twelve headers, each a small integer with a stated meaning
  rather than a bitfield -- `caps.h`'s own reasoning is that a bit can only ever answer
  yes/no, and an unrecognised value has to be distinguishable from a bit that is clear:
  `case`, `devices`, `trailing`, `illegal`, `eol`, `shortnames`, `dirreserve`,
  `workreserve`, `maxpath`, `maxcomp`, `manifest`, `maxdepth`.

**HELLO body.** `HELLO` carries an optional body containing the guest's tool inventory
(`win/net/net.cpp:603-609`, parsed at `bridge/server.py:830` via `toolrep.parse_rows`).
It rides `HELLO` rather than its own frame so the bridge has the inventory before
synthesizing the agent's initial system prompt (`PROGRESS.md:2536`). Format: newline-delimited
rows with tab-separated fields `tag\tname\tpath\tversion`.

The bridge's `caps.from_hello` refuses rather than guesses: an enum value outside the set
it knows, or a number outside a declared range, raises `CapsError`, which the handshake
turns into `ERROR bad_caps` and closes the connection -- deliberately, because an
unrecognised value means the client knows something the bridge does not, and falling back
would be the permissive guess in exactly the wrong direction. `eol=cr` is the one case
where this matters in practice: it is a legal declared value the bridge cannot honour, so
it is refused at the handshake rather than failing per file forever with a `crc_mismatch`
that points at corruption instead of at the capability.

Full handshake refusal vocabulary, all `ERROR` frames sent before a `GuestSession` exists:
`expected_hello`, `hello_timeout`, `bad_version`, `bad_frame_cap`, `bad_secret`, `busy`
(another client holds the session), `bad_caps`. `protocol` is the catch-all for a
`ProtocolError` anywhere in the connection, not just at handshake. **Every one of these
closes the link.**

**In-session `ERROR` codes are a separate vocabulary and none of them close the link:**
`unknown_type` (a frame type this bridge does not know -- a developer diagnostic, and the
reason the rule above is "ignore and log, never close"), `corrupt_frame` (an `enc=deflate`
body that would not inflate), `no_such_page` and `bad_page` (a `GETPAGE` for a page that
is not there), `bad_session` (a `SWITCH` to an id the registry does not hold),
`bad_attach` (an attachment path outside the mirrored folder), `turn_busy` (an `INPUT`
while a turn is already running) and `halted` (an `INPUT` while a sync conflict is
unresolved).

`turn_busy` is **not** `busy`. Both mean "not now", but the handshake's `busy` means
another client owns the session and is followed by a close, while `turn_busy` is answered
on a live link that stays up. One code for both would have left the guest unable to tell
a refused connection from a refused prompt.

**The guest currently treats all of them as fatal to the turn.** `win/app/main.cpp`'s
`on_error` ends in `g_turn->abort(); ui_set_busy(0);` whatever the code says, so a
`bad_page` while a turn is streaming clears the thinking indicator and re-enables Send
with the turn still running. The abort needs to key on the code; recorded here because
the fix is on the client and belongs with the client's own round.

**Frame inventory, by direction.** Types only; see the source files above for headers and
bodies. This is what is actually sent and handled today, not a design intent -- several
entries below differ from what earlier drafts of this document assumed.

*Guest -> bridge:* `HELLO` (handshake) · `INPUT` (typed text) · `INTERRUPT` (Stop) ·
`RESUME` (Continue on the halt bar) · `PERMREPLY` (Allow/Always/Deny) · `GETSESSIONS` ·
`SWITCH` · `GETPAGE` · `NEWSESSION` · `SETMODEL` · `SETEFFORT` · `SETMODE` (the
permission mode, live -- no fork, unlike effort) · `SYNCDIRTY` (the guest
noticed its tree moved; it reconciles nothing itself) · `SYNCENT` / `SYNCEND` (manifest
scan) · `SYNCPUTACK` / `SYNCMKDIRACK` (acks to the bridge's push/mkdir) · `SYNCDAT` /
`SYNCDATEND` (file data, answering the bridge's `SYNCGET`) · `EXECOUT` (streamed command
output) · `EXECRESULT` · `STAT` (periodic measured link speed, feeds the adaptive codec
choice) · `PING` (keepalive, sent only when there is no sync root to make `SYNCDIRTY`
serve the same purpose).

*Bridge -> guest:* `ERROR` · `READY` (post-handshake session state) · `RESYNC` (dead: sender
deleted with replay buffer; guest handler removed) · `DOC` (initial transcript skeleton, once
per process) · `TEXT` (a styled transcript block, `mode=append|live`) · `TOOL` (dead: guest
keeps a handler "for older bridges", nothing builds this frame any more) · `NOTICE` · `TURN`
(start/end/error) · `PERMREQ` / `PERMSTATE` / `PERMWAIT` / `PERMEND` (the permission
bar) · `HALT` / `HALTEND` (the sync-conflict halt bar) · `SESSIONS` / `SESSION` /
`SESSIONSEND` (chat list) · `SWITCHED` · `OPTION` / `OPTIONSEND` (model/effort menus) ·
`PAGESTART` / `PAGEEND` / `PAGESEALED` (pagination) · `ANCHOR` (sent every turn; guest logs
only, jump-to-message feature was cut) · `EXEC` / `EXECKILL` (routed straight to the RPC
worker thread, never the UI thread) · `SYNCSCAN` / `SYNCPUT` / `SYNCPUTEND` / `SYNCGET` /
`SYNCMKDIR` (routed the same way, matched as a `SYNC*` wildcard lane rather than five
separate table rows -- `win/net/frameproto.cpp`) · `PONG` (answers the guest's `PING`;
the guest only sends `PING` when it has no sync root configured, since `SYNCDIRTY` already
serves as the keepalive otherwise -- the guest branch that receives `PONG` just logs it,
nothing is shown).

A tool result is **not** its own frame type on the wire today: it arrives as an ordinary
`TEXT`/`DOC` block through the normal transcript path, the same as any other message
content. An earlier design sent it as a dedicated `TOOLRESULT` frame the guest never
implemented a handler for, so tool output never appeared on Win98 at all; the fix routed
it through the path that already worked rather than adding the missing handler.

#### Verification gates for the phase

- **0.12 and 0.13 are no longer Phase 3 gates** -- they moved to Phase 6 with
  the screen tier. Nothing in this phase is blocked on either.
- A denied permission produces a refusal, not a hang, and a prompt left
  unanswered auto-denies rather than wedging the session.
- `EXEC` of a 16-bit tool returns correct non-empty output *through the GUI
  binary on the RPC thread*, not only through `probe.exe`.
- Guest free memory is measured again at the end of the phase (see `docs/mem-audit/`).
  A phase that quietly spends another megabyte needs to know it did.
- Every demo run once over `tc netem rate 40kbit`, per the standing rule.
  **Deferred 2026-09-17 to `BACKLOG.md` §1** -- unprivileged netem setup documented in §5.
  It is the gate that would exercise the sender thread's backpressure path, which has only
  ever run at LAN speed.


### Phase 4 — settings and the rest of the surface *(revised 2026-09-17)*

**Permissions moved to 3.0** and are a prerequisite of Phase 3, not a successor
to it: the tool surface cannot ship without them.

Several items here shipped a first version early, during Phase 2 polish. They
are **still Phase 4 items** -- a first version is not a finished one, and each
has known work left.

- **Model selection.** *Shipped:* menu built from
  `get_server_info()["models"]`, with the `default` alias resolved away and a
  tick on the model actually in use.
  *Still to do:* per-client model once R7 lands.
  **[R -- 2026-09-19] The other three are closed.** `description` is surfaced:
  it rides as its own OPTION header and the menu shows "Haiku -- Haiku 4.5 .
  Fastest for quick answers" while the toolbar button keeps the short
  `displayName`, because the button has 640 pixels of window to live in.
  The list changing under a running session is handled at the one point where
  it can actually change: `get_server_info()` returns the init result the SDK
  cached when the CLIENT connected, so re-fetching mid-session re-reads the
  same answer. `_make_session` clears the cache; nothing else can.
  And the three `supports*` flags are ANSWERED, by measurement rather than by
  deciding: the installed SDK has **zero references** to fast mode or
  adaptive thinking -- no option, no control request, no method -- so
  `supportsFastMode` and `supportsAdaptiveThinking` name capabilities there is
  no way to drive from here. `supportsAutoMode` names a permission mode this
  client deliberately withholds (see `MODE_ORDER` in `bridge/server.py`, and
  4.1f). Re-open this only if the SDK grows a control for one of them.

- **Effort selection.** *Shipped:* levels filtered by the selected model's
  `supportedEffortLevels`, with a greyed entry for models that have none, and
  the fork dance -- `resume=<id>, fork_session=True` -- because `set_effort()`
  does not exist.
  *Still to do:* **[R -- 2026-09-18]** This used to say the chat list does not
  follow a fork until the next turn writes a file, leaving the old
  conversation listed and the new one briefly invisible. That symptom is
  fixed: the new session id is now picked up in `on_system` the moment the
  SDK's init `SystemMessage` reports it -- during the fork's first turn, not
  after some later turn happens to write a registry file -- and the list is
  pushed when it changes (commit `a805df3`). What is still open is the cause
  underneath the symptom: `guest.sess_id` is one field for two concepts, the
  client's connection identity and the conversation's identity, and splitting
  it into `client_id` plus a bridge-owned `conv_id` is R7's job
  (`BACKLOG.md` §7), not this stage's.

- **Settings.** *Shipped:* an INI read with `GetPrivateProfileString`, not the
  registry.
  *Still to do:* **Configure -> Server Host...** is a message box telling the
  user to edit the file. It wants a real dialog -- `wrc` is available for a
  resource, or a runtime `DialogBoxIndirectParam` template.

- **Opening a chat starts nothing. [R -- 2026-09-19] Done (4.1i).** `_switch`
  used to spawn a full CLI subprocess with `--resume` just to READ a
  conversation. It replays stored history and builds no client; the first
  message is what starts the process (`Bridge._resume_pending`). Four things
  that were quietly session-dependent had to stop being so: the option menus
  answer from a Bridge-level cache of the CLI's model and command lists,
  Model and Effort are recorded against the guest and applied when the client
  is finally built, the chat list marks the open conversation by
  `GuestSession.conv_id` rather than by a live client's id, and the
  deny-everything-pending barrier moved out of `_make_session` into
  `_abandon_pending`, which fires on a switch whether or not anything is
  rebuilt.
  The stored compaction summary is replayed as one line rather than as a
  multi-KB user bubble. Matched on its opening sentence, not on
  `isCompactSummary`: that flag is a top-level key on the JSONL record and
  `SessionMessage.message` carries only `{content, role}`, so the accessor
  drops it -- and matching text degrades to today's behaviour when the wording
  changes, where a JSONL reader would break the whole chat list.

- **The work folder. [R -- 2026-09-19, CORRECTED] `C:\Gaston\<name>`, named
  after the conversation, after its first message.**
  An earlier attempt asked for the name on New chat and put it under the
  existing `C:\WORK`. Both were wrong against what was actually asked for
  (transcript, "Lets default to `C:\Gaston\<chat shortened name>` as work
  folder"): a folder is named after the conversation, and the CLI names the
  conversation from its FIRST MESSAGE -- so asking up front was asking the
  user to invent something before there was anything to base it on, and then
  live with it. The toolbar shows `<unset>` until then.
  Unique by suffix, checked against both the disk and the folders already
  claimed: two conversations about listing files both reduce to `LISTFILE`,
  and the second taking the first's folder would put two conversations' work
  in one place with no way to tell them apart afterwards.
  The mapping is REMEMBERED (`conversations.Conversations`), not read back
  from the
  session: the folder is named after the session has already started and
  recorded the directory it started in, which is the root -- so asking the
  SDK gives the wrong answer and a reopened chat showed `<unset>` for a
  conversation whose folder was sitting on disk.
  "Change work directory..." sets THIS CONVERSATION'S directory, through the
  native folder browser (`SHBrowseForFolder`, resolved via LoadLibrary). An
  earlier version edited the mirrored ROOT in the INI and asked for a
  restart, which is a different setting wearing this one's name -- "we don't
  set the directory in which the work directory will sit in the future,
  we're setting the actual wk directory". No restart: the sync layer is
  re-pointed live (`app/workdir.cpp`), and the RPC watcher follows on its own
  thread, which is the only thread allowed to touch that handle.
  THE BRIDGE DECIDES AND THE GUEST APPLIES. Picking a folder sends `SETWORK`
  as a proposal; it comes back in the next OPTION batch and that is what
  makes it real on the guest. One direction of authority, so the two sides
  cannot disagree about which tree is mirrored -- the disagreement that ends
  with files in the wrong place. The guest's own spelling is stored
  (the same store) because `D:\Projects\Retro` cannot be derived from a
  folder name on the bridge.
  The INI default is `C:\Gaston`, and the guest CREATES the root if it is
  missing -- a work directory is a place the user named, not one they are
  asserting already exists, and leaving it missing left synchronisation dead
  with "guest scan returned no_root".

- **Nothing is mirrored until a conversation commits to a folder.
  [R -- 2026-09-20]**
  A chat that is only being talked to shares no files at all: the guest's
  sync root is empty, `HELLO` names no live tree, and the bridge runs no
  pass. **The commit happens at the FIRST TOOL CALL**, which is the first
  moment a folder is needed for anything, and it is a commitment -- the
  folder is created, sharing turns on, and the toolbar stops saying
  `<unset>`.
  This is not tidiness, it fixes a real defect. The live root used to be
  held on the GUEST and reported in `HELLO`, so it outlived a bridge
  restart: a freshly started bridge was told the machine was mirroring
  `C:\Gaston\OK`, picked that tree's baseline, and then ran its first pass
  against its own whole mirrored directory -- two sides pairing different
  trees before a conversation was even open. With nothing mirrored until
  the bridge names a tree, there is no tree to disagree about.
  So `HELLO`'s `sync_root` carries the machine's CONFIGURED root (the INI),
  which is a fact about the machine, and never the live one, which is
  bridge-directed and therefore already known there. A connection starts
  with sharing off (`try_connect` re-applies `""` before `rc_net_start`).
  The name is derived from the FIRST MESSAGE rather than waiting for the
  CLI's title: a tool can run in the first turn, before the conversation has
  been titled, and the alternative -- commit a placeholder and rename later
  -- renames a tree sync has already scanned, which is a new `tree_id`, a
  new baseline and a window where both sides must move together.

- **Changing the work directory when it already holds files.
  [R -- 2026-09-20]**
  Two things can be true at once: the folder being left has work in it, and
  the folder being chosen is an existing project with its own. Requiring an
  empty target would rule out the case that makes the feature worth having.
  So a dialog of our own, not the bare folder browser -- current path with a
  **Review...** button (`ShellExecuteA` on the old folder, so "review, then
  decide" is literal), a path field with **Browse...** (still
  `SHBrowseForFolder`), and a choice: bring a copy of the existing files
  into the new folder, or leave them where they are. It appears only when
  there is something to lose; an unset or empty current folder goes straight
  to the browser, because a radio group about zero files is noise.
  **A COPY, NEVER A MOVE.** The old folder is left on disk, unmirrored, and
  the notice names it. Deleting a directory as a side effect of changing a
  setting deserves its own confirmation rather than a radio button; and,
  which is what decides it, an interrupted copy is recoverable by
  construction because the source is still intact. A move that deletes as it
  goes leaves the tree split across two folders with no record of which
  files made it.
  **COLLISIONS ARE ASKED ABOUT, NOT STAGED BY RULE.** `.gaston\conflict`
  exists because a background sync conflict has nobody to ask; this one has
  a user standing in front of it who has just clicked OK. One question for
  the whole set with the list shown, not one per file -- point at a real
  project and forty names collide. Three answers, and cancel:
  **Keep the files already there** -- the incoming copies are simply not
  brought; nothing is staged, because they are still in the old folder.
  **Replace them with the ones I am bringing** -- the target's own copies go
  to `Gaston-Replaced\`, the one case where a byte would otherwise be
  destroyed with nowhere else to exist.
  **Let the assistant merge them** -- the folder keeps what it has, the
  incoming copies go to `Gaston-Incoming\`, and the agent reconciles them
  file by file. The only answer that does not make the user pick a side;
  the other two are for when they already know which side is right.
  **NOT `.gaston\conflict`,** which is what this first used and which was
  wrong for a reason only the guest could show: that directory is the sync
  layer's own and the bridge ignores it by default
  (`bridge/files/ignores.py`), so everything staged there was frozen out of
  the mirror and the agent could not read a byte of it -- the entire point
  of the merge answer. The first run on the guest reported
  `.gaston/conflict/hi.txt: ignored`. Two ordinary visible folders instead,
  and two names rather than one because they hold opposite things: INCOMING
  is what was brought and did not overwrite, REPLACED is what was
  overwritten and would otherwise be gone.
  **PRE-FLIGHT BEFORE A BYTE IS WRITTEN**, so a refusal never leaves a
  half-copied tree: every destination path against the guest's declared
  `maxpath`/`maxdir`/`maxfile` (names that fit under `C:\Gaston\OK` need not
  fit under a deeper root, and this is the failure most likely to bite);
  summed source bytes against free space on the target volume
  (`GetDiskFreeSpaceExA` through `GetProcAddress`, `GetDiskFreeSpaceA` as
  the fallback -- Win95 OSR2 predates the first); containment either way,
  since copying a tree into its own subdirectory never terminates; the
  target writable; and the collision set, which is what feeds the question.
  Failures during the copy are still possible -- something else fills the
  disk, a file is locked -- so each one offers **Retry / Skip / Cancel** with
  the reason spelled out.
  **THE FIRST PASS IS SHOWN FOR WHAT IT IS.** Pointing a chat at an existing
  project means the guest reads that whole tree and sends it up, which on
  this link is minutes. The dialog states the count and the total size
  before OK, rather than letting a settings change turn into an unexplained
  transfer.
  **ONE PROGRESS WINDOW COVERS BOTH PHASES** -- "Copying files 34/312", then
  "Sending to the assistant 34/312" -- because from where the user sits it
  is one wait, and two bars for one action invite the belief that the first
  one finishing means it is done. MODELESS, so the message loop keeps
  pumping: the second half is driven by the bridge over the socket and a
  modal dialog would stall the very frames it is reporting on.
  `ICC_PROGRESS_CLASS` is already registered in `WinMain` -- it was put
  there for Phase 5's uploads and this is its first user. Cancel stops the
  copy where it stands, which is safe for the reason above: the source is
  untouched.

- **[superseded] The work folder as a SUBFOLDER OF THE MIRROR.** §7 settled that
  a mirror belongs to a conversation; what it did not settle is whether that
  means moving the sync root. Moving it would mean a new baseline, a new
  `tree_id`, a watcher restart and a recovery pass on the new tree -- four
  chances to report deletions nobody made, in the one layer that can lose a
  person's work. Putting the conversation INSIDE the existing mirror gets the
  same property (every chat has its own folder; two chats cannot overwrite
  each other's files) with one mirror, one baseline and one watcher, which is
  what §7 says the decision requires. It also makes a stronger guarantee than
  the original shape: with the agent's cwd inside the mirror, everything it
  can write is mirrored, so `_edits_outside_the_mirror` has nothing to find by
  construction.
  The name is asked for on New chat and cannot be changed afterwards -- there
  is nothing to default it from (the CLI titles a conversation from its first
  message, which has not happened yet) and changing it later would mean moving
  files that are already on the other machine. Blank is a real answer and
  means the shared folder.
  A folder name with a separator, a colon or `..` in it is REFUSED, not
  repaired: it is not a typo, and rewriting it into something that works is
  how a guard becomes decoration. A restored conversation comes back in its
  own folder, read from the stored session's `cwd` and accepted only if it is
  inside the mirror.
  Two consequences worth knowing: the chat list is no longer filtered by
  directory (`registry.filter` was always the real filter, and a directory
  filter would drop a chat the moment it was given a folder), and the
  permission desk's containment root follows the conversation, or `off_limits`
  would refuse the chat's OWN files while allowing the previous chat's.

- **File -> Export as HTML. [R -- 2026-09-19] Done.** Three frames
  (`EXPORTSTART`/`EXPORTDAT`/`EXPORTEND`), `GuestLink.send_export`, and a
  streaming sink in `win/app/export.cpp`. The guest never holds the document
  -- each chunk goes from the socket to the file -- and writes to a temp file
  renamed on a clean END, so a transfer that dies halfway leaves nothing
  rather than a truncated file under the name the user chose.
  The Save dialog is opened BEFORE the request goes out, which is the one
  ordering decision worth knowing: a modal on this thread still pumps
  `WM_RC_FRAME`, so a dialog opened when the data starts arriving would be
  dispatching chunks with no file open. `GetSaveFileNameA` is resolved
  through `LoadLibrary` rather than imported, so a stripped install loses the
  dialog instead of refusing to load the program.
  Verified on the guest: 9,514 bytes exported and opened in IE5 from
  `C:\RETRO\EXPORT.HTM`, byte count matching what the bridge sent.

- **The permission rule table. [R -- 2026-09-19] Built, and NOT as specified.**
  Parsing `.claude/settings*.json` would have been actively misleading:
  `Session` runs with `setting_sources=[]` on purpose, so the operator's
  permission rules are not in effect for a Win98 client at all, and a table of
  them would answer "what can happen without asking?" with rules that cannot
  happen.
  What shipped is `Configure -> What can run without asking...`, answered by
  the bridge because the answer is about the session it is running: the mode
  in force and what it means, that anything touching the user's machine always
  asks, the tool list with a one-line note each, and -- this is 04.16's
  requirement, honoured -- a paragraph saying plainly that the list is
  incomplete, because Claude Code resolves some of its own calls locally and
  never tells the bridge. A view that listed only what reaches the prompt
  would read as "this is everything", which is measured false.

- **R5 (icons), R7 (multiple clients), R8 (Command menu)** land here unless
  pulled forward. **[R -- 2026-09-19]** R5 shipped as Phase 4.0 and R8 as
  4.1f part 3. R7 did NOT: one conversation now has one folder, but two
  guests still share one mirror and one baseline, so `BACKLOG.md` §7's
  implementation list is untouched.

- **Autoscroll is a toggle, not a heuristic. [R -- 2026-09-20] Done.** It was
  `autoscroll && was_at_bottom`, so following the conversation depended on a
  measurement of where the view happened to be -- and anything leaving it a
  few pixels short of the end turned autoscroll off silently and permanently
  until the user scrolled there by hand. Reported twice from the real
  machine, most recently for an error notice that never came into view.
  ON means the view follows, always; OFF means it never moves on its own.
  The control is an icon button on the page-control strip (a drawn
  page-down arrow -- comctl32 has no arrow that points down, only the
  back/forward chevrons, which read as navigation), because that is where it
  was asked for and because "follow the end" belongs next to "which page am
  I on".

- **`auto` is back on the Mode menu. [R -- 2026-09-20]** It was asked for by
  name and withheld for one release on the strength of a measurement. That
  was the wrong call to make silently: the measurement says what auto does
  NOT do, and that belongs in front of the person choosing it rather than in
  a decision made for them. `MODE_CAVEAT` puts it in the transcript when the
  mode is picked -- with `can_use_tool` installed the model's own danger
  classifier is not consulted, so Auto here still asks, it does not add a
  second opinion. `bypassPermissions` and `dontAsk` stay off for their own
  reasons, which are not measurements but documented behaviour.

- **Replayed tool calls look like live ones. [R -- 2026-09-19] Done.** The
  last survivor of "opening an old conversation makes it lose formatting":
  every other block type had been routed through the live renderer, and
  `tool_use` had not. A call rendered `tool -> Bash` on reopening and
  `Bash command: ls -la ...` while it was happening. Both paths ask
  `permissions.telling_argument` which one argument matters now, so they
  cannot drift again, and "summarised" still means summarised -- one
  argument, clipped at 160 characters, never the whole input dict.

- **Keyboard: Ctrl+A and Ctrl+M. [R -- 2026-09-19] Done.** Windows 98's EDIT
  control has no Ctrl+A -- select-all arrived with rich edit and NT's common
  controls, and on 9x it just beeps, which is twenty years of muscle memory
  arriving at a control that predates it. The input box handles it, and
  swallows the SOH that would otherwise beep. Ctrl+M advances the permission
  mode: the setting most worth changing mid-conversation and the one furthest
  from the keyboard. It does take Ctrl+M away from the edit control, where
  Win98 treats it as a newline; Shift+Enter is this app's documented way to do
  that, so the collision costs a behaviour nobody here was told about.

- **The status-bar double-click was never broken.** It was recorded as an
  unresolved defect ("NM_DBLCLK may not be sent") and it is not: measured on
  the guest, a Windows 98 status bar sends NM_CLICK and then NM_DBLCLK, 110ms
  apart, exactly as documented. What was broken was the testing --
  `tools/vm/qmp.py click X Y double` silently ignored the third word, because the
  CLI verb is `dclick`, so every attempt to exercise it was a SINGLE click. A
  working feature was written down as a defect and then "fixed" twice before
  that was noticed. The tool accepts both spellings now and `VirshQmp` borrows
  the constant `click` reads, which it had not.

- **Answered permission boxes collapse. [R -- 2026-09-19] Done.** Every
  approval used to stay a full amber argument table forever, and on a
  640x480 screen a handful of them is the whole page. The block keeps its
  place and its record; the table is hidden and the heading changes to
  "Answered".
  A DIFFERENT class, not `.perm.done`: IE5 and IE6 read a compound class
  selector as its last class only, so `.perm.done` would have styled every
  `.perm` -- a rule that looks broken and is actually working on everything.
  Two headings are written and the CSS shows whichever matches the state, so
  flipping one class does both jobs without the guest rewriting text through
  the DOM. Live view only: paging back to a sealed page re-renders what the
  paginator holds, which is the original block, and that is right -- what was
  asked and answered is a record.

- **`cwd` on `mcp__machine__exec` is genuinely optional now.** The SDK's
  `{name: Annotated[...]}` shorthand marks EVERY property required, so three
  arguments documented as optional were not. Measured on the guest: the model
  called exec with a command and no `cwd`, got back `Input validation error:
  'cwd' is a required property`, and every single exec cost two round trips
  with a red error on the user's screen in between. The tool now hands the
  SDK a full JSON Schema with `required: ["command"]`, and an absent `cwd`
  defaults to this conversation's own folder on the user's machine rather
  than to empty -- empty means the guest passes NULL to `CreateProcess` and
  the child inherits a directory the bridge has never been told.

- **A transcript that survives a disconnection. [R -- 2026-09-19] Done
  (4.1k).** The bridge owns the transcript, so a guest that started with the
  bridge down had nothing at all to show -- an empty white rectangle and an
  empty chat list. Each completed page and the chat list are now kept on
  DISK under `<exe dir>\CACHE\`, and a failed connect puts them back with a
  notice saying they are not live.
  On disk and not in memory, deliberately: `app/pagebar.h`'s "the control
  holds exactly ONE page ... the whole reason a 32MB box can run this" is
  unchanged, and so is the high-water mark. The chat list is streamed a row
  at a time in both directions -- an earlier version held it in a static
  array, which cost 12.8KB of BSS on a machine measured at 28 KB free after
  a turn, for data whose destination is a file written one line at a time.
  Only a page the bridge calls complete is kept, so a half-streamed answer is
  never restored as though it had finished.

- **Tool detection. [R -- 2026-09-21] Done (4.18).**
  The guest environment carries toolchains, compilers and interpreters (Open Watcom,
  DJGPP, Visual C++, Perl, etc.) that the model needs to know about to invoke the right
  tools.
  **Three layers, and the split is the point:**
  1. `win/tools_detect.ini` declares what to check (environment variables, marker files);
     `win/app/tooldet.cpp` probes them locally and reports raw observations without deciding
     identity.
  2. The inventory rides in `HELLO`'s body (`win/net/net.cpp:603-609`) so the bridge has it
     before initializing the agent session and building the system prompt (`PROGRESS.md:2536`).
  3. `bridge/agent/toolrules.py` evaluates detection rules in Python; `toolrep.py` parses wire rows;
     `toolnotes.py` provides toolchain usage notes; and `machine.py`'s `tools_block` formats
     the prompt block.

**One SDK question from the original text is now answered** rather than
guessed, checked against the installed 0.2.153:
`PermissionResultAllow.updated_permissions` is a **`list[PermissionUpdate]`**.
The rendering that called it a `dict` was wrong; the snippet passing a list was
right.

### Phase 5 — Attach *(1–2 weekends)*

- Toolbar `TBSTYLE_DROPDOWN`, with **"Attach from the mirrored folder" as the default
  item** -- those cost nothing on the wire (the bridge reads them from its own working
  copy, `cfg.sync_root`, the same tree the sync layer already keeps current). **[R --
  2026-09-18]** This used to say "Attach from P:\" / "the bridge reads them off the
  share directly" -- Samba-era wording R6 (the sync layer replacing Samba) left behind
  when it landed. No code changed here; the sentence was simply never revisited.
- **Attachments never transit guest memory.** The GUI sends a *path*; the bridge reads it
  and base64s it. A 10MB image can't be held on a 32MB box, let alone base64'd. Enforce
  10MB / 8000px bridge-side by re-encoding.
- A file on the guest's local `C:\` is the only case needing a chunked upload — progress
  bar and Cancel required, since 10MB is ~13s at 750 KB/s.
- Non-image files referenced by path (no arbitrary-file content block exists).
- **The cheap win: "attach a screenshot of my desktop."** *(shares the capture
  path with Phase 6; whichever lands first builds it.)* `GetDC(NULL)` + `GetDIBits` +
  `screen_palette_for_capture()` — ~60 lines, most of the value of the whole screen tier
  with no tile diffing, no coordinate space, no `input.c`. Capture at 8bpp (0.6s on the
  wire at 800×600, against 1.9s for 24bpp). The palette fix is non-obvious and
  hardware-verified: a fresh memory DC has only 20 static colours, so `GetDIBits` needs the
  live hardware palette selected in or you get colour-index noise.

### Phase 6 — the UI tier: seeing and driving the screen *(revised 2026-09-17)*

**Everything to do with the screen now lives here**, including screenshots, which
used to sit at 3.2 and were pulled out so that "look at the machine" is one
coherent stage rather than a capture primitive in Phase 3 and everything that
uses it three phases later. OCR joins it for the same reason.

The old text for this phase was a lift plan for retro-agent's `input.c` plus a
warning that the UX was unsolved. Both still apply. What is new is the design
principle below, which changes what gets built first.

#### The control tree is the primary sense; vision is the fallback

Modern computer-use agents lean on screenshots because modern UIs are
custom-rendered -- Electron, canvas, web. **Windows 98 is the opposite.** Almost
every pixel is a standard USER32 control whose text can be queried exactly. A
dialog's control tree is ~200 tokens and is *exact*; a screenshot of the same
dialog is ~850 tokens and is *inferred*.

That inverts the usual build order. The ladder, cheapest and most reliable first:

1. **The control tree.** `EnumWindows` / `EnumChildWindows`, `GetClassName`,
   `GetWindowRect`, `GetDlgCtrlID`, `GetWindowLong(GWL_STYLE)`. Always
   available, no dependency, works on custom apps too.
   Use **`SendMessage(WM_GETTEXT)`, not `GetWindowText`**: cross-process,
   `GetWindowText` returns only a caption, while `WM_GETTEXT` is marshalled by
   USER32 and returns a control's actual contents.
2. **MSAA enrichment** -- roles, values and `accDoDefaultAction` where the app
   supports it. Gated on 0.13 below.
3. **OCR, on the bridge** -- for what neither can see.
4. **A screenshot to the vision model** -- last, not first.

**Known limitation, recorded so it is not rediscovered.** `LB_GETTEXT`,
`LVM_GETITEMTEXT` and `TVM_GETITEM` are **not** marshalled cross-process, and
the NT route (`VirtualAllocEx` / `WriteProcessMemory`) does not exist on 9x.
Reading a list box's *items* from another process needs the
shared-memory-above-2GB trick. Treat list contents as an MSAA-or-OCR problem,
not a message problem.

**Updated 2026-09-26.** That conclusion holds only from OUTSIDE the process,
which is where it was written from. Inside the target address space there is
no buffer to place: the pointer already belongs to the process being asked,
so `LB_GETTEXT`, `LVM_GETITEMTEXT` and `TVM_GETITEM` simply work. This is the
one thing the in-process collector (`docs/UIAUTOMATION.md`, section D) can do
that no other tier can, and unlike the speed argument it depends on no
measurement at all. Until that collector lands the paragraph above is still
the operative answer.

**Identity.** HWNDs are stable within a session and meaningless across one.
Every node returns its HWND, its child-index path (`0/3/1`), its control id and
its class. `GetDlgCtrlID` is the most stable handle inside a dialog; the path
survives a repaint.

> **New gate -- 0.13: is `oleacc.dll` present on Win98 SE, and does
> `AccessibleObjectFromWindow` return a usable tree for a standard dialog?**
> MSAA 1.3 is documented as shipping with Win98 (Win95 needed the RDK), and
> UI Automation is Vista+ so it is not an option. `LoadLibraryA` +
> `GetProcAddress`, **never a static import** -- a missing DLL as a static
> import kills the image at load, before `WinMain`, with nothing to debug.
> Walk one common dialog: `get_accChildCount`, `AccessibleChildren`,
> `get_accName`, `get_accRole`, `get_accLocation`.
>
> This decides whether the semantic layer is MSAA-enriched or
> `EnumChildWindows`-only, and it is ~40 lines in `probe.c`, which is already
> built and already shipped to the guest. Answer it while that file is warm.

#### Screenshots, in strips *(moved here from 3.2)*

0.10 measured `GetDIBits` producing `BI_RLE8` directly: 640x480x8 went
307,200 -> 27,088 bytes in 14ms, **11.3:1, done by GDI**. The compression is
free. The problem is the buffer.

**A full-screen DIB is 307KB and physical memory and heaps are tight (see `docs/mem-audit/`).** So capture proceeds in
**strips** of N scanlines into a small fixed buffer, each strip compressed and
sent as it is taken. Region capture is the default -- the control tree already
returns window rects, so "read this dialog" sends ~10KB instead of ~120KB and
costs ~80 vision tokens instead of ~850. Full-screen is the explicit exception.

> **0.12 -- does `GetDIBits` produce valid `BI_RLE8` for a scanline RANGE?**
> RLE8 is row-oriented so it ought to, but "ought to" is what 0.9 and 0.10 were
> there to replace. If it does not, capture strips as `BI_RGB` 8bpp and RLE them
> on the bridge: guest CPU, but no extra memory.

`screen_palette_for_capture()` is the one piece of retro-agent's `screen.c`
worth keeping: on an 8bpp display a fresh memory DC has only the 20 static
colours, so `GetDIBits` needs the live hardware palette selected in or the
result is colour-index noise. Hardware-verified, non-obvious, easy to lose.

**`screen.c` is otherwise not lifted.** Under "the guest transforms nothing",
`SCREENDIFF`, the 64x64 tiling, `g_prev_pixels` and `resp_buf` all move to the
bridge, which has memory to spare. That deletes the worst memory item the plan
ever carried: a ~4.4MB guest peak to compute a diff the bridge computes free.

Run the encoding-vs-legibility experiment (see Architecture) before fixing the
default depth.

#### OCR, on the bridge, as a fallback

Tesseract on the Linux side, consistent with "the guest transforms nothing" --
the guest never ships an OCR engine and never could.

OCR earns its keep exactly where the control tree cannot see: DOS full-screen
boxes, custom-drawn installers, setup progress, games. For a standard control
`WM_GETTEXT` is exact and free and beats OCR every time, so **OCR is third in
the ladder, never first**. A tool that OCRs a dialog whose text was already
retrievable has spent CPU and tokens to produce a worse answer.

Practical notes: feed it the 8bpp capture before any scaling; Win98 does not
antialias UI text by default, which is unusually good input for Tesseract;
`--psm 6` (uniform block) suits dialog text better than the default.

#### click: one execution path, two ways to resolve a point

`click(target)` takes **either** `{x, y}` **or** a control reference. A control
reference resolves to its rect centre and then performs the *same* real
`mouse_event` click.

Resolving and clicking are separate concerns. Branching the implementation would
mean two input paths with different behaviour, and a synthesised `BM_CLICK`
bypasses the real input stack in ways some apps notice. Raw XY stays as an
escape hatch, but it is the degraded mode: a coordinate from a control rect is
exact, a coordinate from a vision model on a 640x480 screenshot is a guess.

**Occlusion check falls out for free.** After resolving, `WindowFromPoint` the
result and refuse if it is not the intended control. Clicking the wrong thing
confidently is the failure mode that makes an agent dangerous.

#### send_keys: focus first, and the real hazards are not the famous one

`send_keys(target, keys)` focuses, then synthesises with `keybd_event`.

- **Focus is two calls, not one.** `SetForegroundWindow` works on 9x (there is
  no NT5 foreground lock; **[R -- 2026-09-25] wrong for Win98**, which introduced
  the lock alongside 2000: see `docs/REMOTE.md`, UI details), but cross-thread `SetFocus` needs
  `AttachThreadInput` first or focus silently never arrives. **[R --
  2026-09-18]** And it needs detaching afterwards: the attach is
  worker-thread-to-target, so leaving it in place makes the RPC worker share
  the target's input-queue state for the rest of the session.
- **Never send keys to our own window.** **[R -- 2026-09-18]** This said
  "compare the target's thread against ours", which does not work from where
  the tool is designed to run. Every tool dispatches on the RPC worker, so the
  target's thread and the calling thread already differ even when the target
  *is* Gaston's input box, and the refusal never fires. Compare
  `GetWindowThreadProcessId(target)`'s **process** against our own and refuse
  the whole process -- not the target `HWND` against `g_main` either, since a
  child control shares the UI thread and is a different HWND. The agent typing
  into Gaston's own input box is a self-feeding loop, and it is a far likelier
  accident than any hotkey.
- **Block Ctrl+Alt+Del** and the power/sleep keys. On 9x Ctrl+Alt+Del is handled
  by the VMM at ring 0 so `keybd_event` probably cannot synthesise it anyway --
  but it is one line and the cost of being wrong is a reboot.

`input.c` is lifted mostly intact for the mechanics: `UICLICK` / `UIDRAG` /
`UIKEY` are pure syscalls with no buffers, and the E0-extended-scancode table
and the `VK_SNAPSHOT`-returns-0 fallback to scancode `0x37` are both worth
keeping.

#### send_action_sequence: closed-loop, never open-loop

Batching is worth real money here. A round trip to a 200MHz machine over a slow
link is expensive, and filling one dialog -- focus, type, Tab, type, click OK --
is five of them plus five permission prompts.

But the reason computer-use tools are usually step-by-step is that **each step's
outcome is uncertain and a blind sequence compounds errors**. If step 2 raises an
unexpected dialog, steps 3-5 land somewhere nobody predicted -- and with
`send_keys` in the list, that means typing into the wrong window. A naive batch
trades round trips for exactly the failure this tier has to avoid.

**So: per-step preconditions and fail-fast.**

- Each step may carry an expectation -- the foreground window's class or title,
  or that a named control exists. The sequence **aborts at the first mismatch**
  and returns how far it got, which step failed, and the state at the abort
  point, so the model re-plans rather than guesses.
- **Waits are conditional, not fixed sleeps.** `wait_for(class=...)` and
  **`WaitForInputIdle`** -- literally "the app has finished starting and is ready
  for input" -- beat `sleep(500)`. On a 200MHz box under TCG a fixed sleep is
  either too short or pure waste.
- Cap the step count. Always return the per-step outcome log.

**It is better for permissions, not worse.** One prompt describing the whole
sequence beats five prompts the user starts rubber-stamping by the third; prompt
fatigue is a genuine security failure mode, not a UX complaint. The cost is that
the bar must render a multi-step summary on a 640x480 screen, so the sequence
needs a compact human rendering (`type "hello" -> click [OK] -> wait "Notepad"`),
not a JSON dump.

The workflow this points the model at -- **enumerate the control tree, then batch
against what you found** -- belongs in `bridge/machine.py`'s appended system
prompt. Closed-loop batching only works when the batch is built from a fresh
observation.

#### The tools this stage adds

| tool | why it is not just `exec` |
|---|---|
| `list_windows(scope)` | the semantic tree; the primary sense |
| `screenshot(rect)` | region by default, strips underneath |
| `ocr(rect)` | bridge-side, for what the tree cannot see |
| `click(target)` | control-resolved or raw XY, one real input path |
| `send_keys(target, keys)` | focus + synthesise, with the guards above |
| `send_action_sequence(steps)` | closed-loop batching |
| `launch(path, args)` | **`exec` waits for exit.** You cannot drive Notepad's UI with a tool that blocks until Notepad closes. `command.com /c start x` works but is fragile; `CreateProcess` without the wait is ~10 lines. *(The original plan dropped `LAUNCH` because retro-agent's version dragged in its JSON builder -- that reason does not apply to our own.)* |
| `get_clipboard` / `set_clipboard` | the escape hatch for text no message will give up: Ctrl+A, Ctrl+C, read it. `OpenClipboard` / `GetClipboardData(CF_TEXT)`, ~30 lines |

**[R -- 2026-09-18] `list_windows` takes a scope and a node cap from its first
version, not its second.** The token argument above -- a tree at ~200 against a
screenshot at ~850 -- is measured on one dialog and inverts on a busy desktop,
where an unscoped enumeration returns every control on screen and costs more
than the screenshot it was meant to replace. Scope is one of: the foreground
window, a window by class, or everything owned by a pid. The cap is a node
count, and a truncated tree says so rather than ending silently. Both are
cheaper to decide now than to retrofit once the model has learned to call it
unscoped.

**[R -- 2026-09-18] An inline screenshot is a file on disk, never a `data:`
URI.** The transcript is MSHTML under IE5 and `rc_html_set_document` navigates
`file:///` at a `view.htm` the guest writes (`win/app/htmlview.cpp`); `data:`
URIs arrived in IE8. So a screenshot shown in the transcript has to be written
as a real file beside `view.htm` and referenced by relative path -- through the
same chunked sink Export as HTML needs, which is why `BACKLOG.md` §6 treats the
two as one primitive with two callers rather than two features.

#### Permissions: this is where whole-tool "Always" is worst

Every tool here goes through the 3.0 prompt bar. The rule from 3.0 that
`allows=0` for all `mcp__machine__*` matters more in this stage than anywhere
else: MCP permission rules cannot carry specifiers, so a single "Always" on
`send_keys` or `click` grants unattended keyboard and mouse control of the
machine for the rest of the session.

**Gate honestly, as the original text did:** the UI spec never asked for this,
and Claude driving the mouse while the user types into Gaston on the same desktop
is an unsolved UX problem. A "the agent is driving, hands off" modal state is
probably the answer, and it is not designed yet.

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

### R5 — icons  *(the application icon is done; the toolbar is not)*

Windows 98 is a colourful system and this app is a grey rectangle. At minimum
an application icon, so the title bar and taskbar are not generic; ideally a
small toolbar. Open Watcom ships `wrc`, so a `.rc` with an `.ico` is the
mechanism. Cheap, visible, and currently the largest gap between "works" and
"looks like it belongs on the desktop".

**[R -- 2026-09-18, updated 2026-09-20]** The application icon shipped as Phase 4.0
(`win/app/gaston.rc`, `tools/gen/mkicon.py`). The comctl32 toolbar shipped as
`win/app/toolbar.cpp` (373 lines) with dropdown arrows and prompt controls,
resolving R5 and unblocking Phase 5.1's attach UI (`BACKLOG.md` §11).

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

### R8 — a Command menu  *(shipped)*

A menu of the slash commands that make sense here (`bridge/server.py:4260`
`COMMAND_MENU = ["/compact", "/rename", "/init"]`), filtered at connect against what the
local CLI provides. `/context` is deliberately excluded because the status bar already
displays the token gauge, and commands requiring unsupported UI affordances are omitted
(`bridge/server.py:3868-3887`).

### R9 — `gremote.exe` and the static libraries  *(planned 2026-09-25)*

A standalone Win98 server for driving the real P200 out of band from the agent
client: ssh-like exec, files and processes, plus screenshot, window/control tree
with MSAA, and input, scripted from a Python package. Built from static libs
(`libcore`, `libwire`, `libexec`, `libfs`, `libautomation`) that the client
also links,
with an `App` container and INI config. It is also the first step of
refactoring the client and the source of the Phase 6 primitives. Full plan:
`docs/REMOTE.md`.

## Repo layout — one repo

```
RetroAI/
  README.md  FEASIBILITY.md  NOTICE  LICENSE
  Makefile                    # exe test lint wine qemu
  make/ow.mk                  # -bm, WLINK_LNK, WINVER=0x0400, verified rules
  win/                        # [R2] one binary, one target -- SOURCE ONLY
    app/                      # GUI, window, HTML view, toolbar, dialogs
    core/                     # INI, log, string utils
    net/                      # Winsock, frameproto, capability headers
    rpc/                      # RPC dispatch, command exec, process spawn
    sync/                     # File scanner, CRC32, sync RPC
    checks/                   # Static and Wine-based contract verification
    third_party/miniz/
  build/                      # ALL generated output, gitignored
    win/                      # .o .exe .map .err, and guest runtime log
  tmp/                        # scratch from VM scripts, gitignored
  bridge/
    agent/                    # Session, tools, permissions, conversations
    files/                    # Sync engine, manifest, path normalization
    view/                     # HTML generation, pagination, styling
    wire/                     # Protocol framing, link socket, compression
    devtools/                 # fakegui conformance harness
  tests/python/               # Subdivided: agent/ files/ wire/ view/ server/ guest/ meta/ tools/
  tools/                      # vm/ gen/ check/ probe/
  vm/                         # disk images and run scripts (gitignored)
  docs/                       # PLAN.md PROGRESS.md BACKLOG.md audit/
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
| `UPLOAD`/`MKDIR`/`DELETE` (via Samba), `LAUNCH` | Samba superseded by sync layer (R6); `SYNCMKDIR`/`SYNCPUT` exist; `LAUNCH` drags in JSON builder |
| Channel multiplexing over one socket | Process split already separates them |
| Scrollback trimming, custom-drawn view | Pagination supersedes (MSHTML is the shipping renderer) |
| Warm multi-session client pool | One live client, switch = close + resume |
| Integrated QMP auto-recovery | `tools/vm/qmp.py` run by hand |
| Progress bar, ETA, transfer name | Simpler status-bar rate/counter indicator ("rx @ rate") is sufficient |

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
