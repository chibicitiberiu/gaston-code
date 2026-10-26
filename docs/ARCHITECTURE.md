# Gaston Code — architecture

How the system is put together: the memory budget everything is
downstream of, the process and thread topology, the wire protocol as
shipped, the permission mechanism, the sync layer and the UI tier.

## Context

Nobody has built a native Win98 GUI client for Claude Code. `claudewin9x` (MIT) proxies the
real CLI but is a console REPL. `retro-agent` (MIT, 25k lines of C) has a complete guest
tool surface and a hardened protocol but is headless and drives the Agent SDK, not Claude
Code. WinGPT proves a native GUI works on 16-bit Windows but has no agency.

The gap is the combination: comctl32 chrome, a streamed transcript, permission requests as
real modal dialogs, and one machine being both the terminal you sit at and the workspace
Claude operates on.

### Hard constraint

**32MB RAM, ~8–12MB free after boot.** Win98 will swap on a period IDE disk, so working-set
size dominates performance more than CPU. Everything below is downstream of this.

Measured with only the app running on the 32MB guest — no DOS box, no probe:

| | free of 32288 KB |
|---|---|
| window created | 2420 KB |
| chat list loaded | 1060 KB |
| after one turn | **28 KB** |

The 16-bit GDI and USER heaps are tight too. So **every allocation has to be
bounded and small** — that is the single rule most of this document exists to
serve. (`dwAvailPhys` excludes the disk cache Win98 shrinks under pressure, so
28 KB is not 28 KB of headroom; allocations were never actually failing. It is
still the number to design against.)

**Raw Win32, no toolkit.** Every candidate is frozen or dead, and the 64KB
USER/GDI heaps reward thin layers.

### Decisions locked

- Scope: terminal **and** workspace (guest agent in scope, sequenced late)
- Target: QEMU dev loop, validated on real hardware
- OS range: Win98 + 2000/XP, `GetVersion` branch for shell selection only
- Toolchain: Open Watcom 2.0

---

## Architecture

```
Win98 guest                        Linux bridge
  gaston.exe           --dial out-->   one listener, one secret, one port
   (one process:                         +-- Agent SDK (ClaudeSDKClient, streaming)
    UI thread                            +-- in-process MCP server (mcp__machine__* tools)
    reader thread                        +-- markdown -> finished HTML
    RPC worker thread)                   +-- pagination
                                         +-- screenshot diffing, scaling, encoding
                                         +-- UTF-8 -> CP1252 fold
                                         +-- the file sync layer
```

### One binary, one connection

The guest is **a single executable with one TCP connection**, replacing the earlier
two-exe / two-connection design. One config value (bridge host:port, in an INI), no
`role=` in HELLO, no stable LAN address, no inbound listener, no UDP discovery.

**Why one socket is safe here.** The two-process split existed to stop display backpressure
stalling RPC. That concern doesn't survive contact with the traffic pattern: display text
is bridge→guest (frequent, small) while the only large payloads — screenshots, file
downloads — are guest→bridge. TCP flow control is per-direction, so a slow upload cannot

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

### The guest transforms nothing

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

### Bandwidth, compression and transfer progress

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

Settled configuration:

- **Persistent stream, never per-frame.** Independent deflate per frame is 1.27× worse.
- **Coalesce to ≥2KB frames.** Frame size is the biggest single lever: 256B gives 1.68:1,
  2KB gives 2.04:1, and it flattens after that. **The 10fps coalescer therefore buys ~21%
  bandwidth on top of the latency win** — a second-order benefit worth knowing.
- **Level 6 bridge→guest** (free on a modern CPU, 6% better than L1), **level 1
  guest→bridge** (the P200 pays there).
- **32KB window.** Dropping to 8KB costs 6% ratio to save 24KB of guest RAM — a bad trade
  against 8–12MB.

#### Adaptive, not fixed

The guest already measures throughput for the progress UI. Feed it back in a periodic
`STAT` frame so **the sender picks the codec from the measured link speed**, per payload.
A frame header key `enc=raw|rle8|deflate|rle8+deflate` carries the choice; HELLO declares
what the guest supports. This is what makes one build work on both a 10Mbps LAN and a
modem.

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
deferred to Phase 5 upload.

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

### Tool naming: nothing is called `win98_*`

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

### The bridge owns markdown

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
wrong way round. Deferred rather than built, where export is the
awkward constraint.

**[R -- 2026-10-10: and "the bridge decides everything about how a block looks" is the
sentence that is wrong.]** The seam above names two jobs; there is a third between them,
which is *emitting the markup*. `HtmlStream` (`bridge/view/html.py:228`) returns finished
HTML written to IE5's constraints, and three bridge-side numbers are calibrated to MSHTML
rather than to text: the page's block cap (`view/pages.py:39`, measured -- a 200-element
page took the 32MB guest to 48 KB free), the tool-output wrap column and the tool-result
truncation (`view/html.py:152,114`). A terminal client wants runs and a width, a DOS
client wants lines, and neither can use a byte of the markup. The *parse* stays here --
"no markdown parser in C" holds -- but what reaches the wire should be blocks and inline
runs, with each client rendering them: the pre-styled-runs design at the top of this
section, returning with a vocabulary rich enough for what the parser now produces. Wanted
by the user, not built.

Two things §31 needed decided are decided (2026-10-10). **Export is rendered by
the guest** -- once the markup is the client's, the bridge has nothing to write,
so the choice was a renderer on the guest or a second one on the bridge, and one
renderer is the point; `win/app/cache.cpp` already holds the rendered pages
export would assemble. And **the vocabulary is declared at the handshake** as one
key with a small integer version, the bridge refusing an unknown value, which is
the same key §13 needs for the class names.

Markdown needs lookahead, so the bridge buffers to a safe boundary (newline, or ~200 chars
of unambiguous text). Streaming granularity becomes one line — which is also what a P2
repaint budget wants.

**No JSON on the guest.** Grammar: `TYPE\n` + `key=value\n` + blank line + raw body. See
the **Protocol reference** under Phase 3.3 for the full grammar, the header limits and the
frame inventory as shipped.

**[R] Frame header keys present from Phase 1**, even where the guest ignores them, because
adding them later touches every frame and handler on both sides:
- `page=` — ignored in Phase 1, switches the control in Phase 2
- `sess=` — see multi-session below

**[R] Reconnect (missing entirely).** Bridge-down-at-startup shows a disabled input box and
a status-bar message, never a modal that blocks the message loop. Backoff 1/2/4/…/15s
driven by `WM_TIMER`, never a sleeping loop. On reconnect: guest sends fresh `HELLO`
with `sess=<sess>`; bridge explicitly re-issues live state (`perms.reissue()`,
`_reissue_halt`, `_reissue_turn`, etc. at `server.py:874-903`) rather than relying on a
replay buffer. **Always `shutdown(SD_SEND)` + short grace before `closesocket`** — an abrupt RST
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

Search and export become bridge operations over the full transcript.

### Renderer: MSHTML
**What forced the reversal**, from `htmlview.h`'s own header comment: MSHTML measures at
about 4MB against a 32MB guest, which is real money on this budget, but it buys padding,
borders, background tints, real bulleted lists and tables -- none of which RichEdit 2.0
can do -- and the engine is already resident because Explorer hosts it for folder Web View,
so a second host pays only its own instance data rather than the DLL tax in full. The
"MSHTML rejected for v1" paragraph below weighed COM boilerplate against
`CreateWindowEx`, and that tradeoff didn't change; what changed is that RichEdit's
ceiling on what a transcript could look like turned out to matter more than the
boilerplate cost of the alternative.

### Multi-session

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

## Permissions

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

## The tool surface

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

## The RPC worker thread

The third thread, and the split is mandatory: an `EXEC` can run 60 seconds, so
running it on the UI thread freezes the window and running it on the reader
thread stops display frames being read.

- `_beginthreadex`, verified by 0.11 along with a message loop.
- `win/rpc/exec.cpp` derived from `run_capture()` in `probe.c`, rewritten rather
  than forked from retro-agent to eliminate six upstream defects (including
  unconditional `taskkill` and `HeapReAlloc` leaks; see `win/rpc/exec.cpp:8-22`).
- Output is capped at 32KB and streamed in frames beyond that. On a machine
  with tight memory, an unbounded capture buffer is not a
  performance question.

## The file sync layer

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

  shared mirror with a per-guest dimension added to the baseline, or one mirror
  with a writer lease. Keying it to the CONVERSATION instead dissolves the
  problem rather than managing it: the number of mirrors is bounded by chats
  rather than by clients times chats, so the combinatorial case never arises.

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

## Protocol reference

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
synthesizing the agent's initial system prompt. Format: newline-delimited
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

- **Tool detection. [R -- 2026-09-21] Done (4.18).**
  The guest environment carries toolchains, compilers and interpreters (Open Watcom,
  DJGPP, Visual C++, Perl, etc.) that the model needs to know about to invoke the right
  tools.
  **Three layers, and the split is the point:**
  1. `win/tools_detect.ini` declares what to check (environment variables, marker files);
     `win/app/tooldet.cpp` probes them locally and reports raw observations without deciding
     identity.
  2. The inventory rides in `HELLO`'s body (`win/net/net.cpp:603-609`) so the bridge has it
     before initializing the agent session and building the system prompt.
  3. `bridge/agent/toolrules.py` evaluates detection rules in Python; `toolrep.py` parses wire rows;
     `toolnotes.py` provides toolchain usage notes; and `machine.py`'s `tools_block` formats
     the prompt block.
## UI automation: the control tree is the primary sense

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
measurement at all.

**[R -- 2026-10-10: the collector landed, so the paragraph above is no longer
the operative answer.]** `win/inproc/` is built -- `shm.h`, `collect.c`,
`hookdll.c`, with `win/ui/inject.cpp` as the controller -- and it is reachable
as `ui(inproc=true)`. It was run on the guest on 2026-09-26 and corrected its
own design twice (`WH_CALLWNDPROC` never fires; `WH_GETMESSAGE` does; eight
`DrawText*`/`TextOut*` entry points needed patching, not two). List *items* from
outside the process are still an MSAA-or-OCR problem; from inside they are read
directly. The safety question that outranks the capability is whether a run leaves the guest healthy,
and until it closes the tier is attended-only.

**Identity.** HWNDs are stable within a session and meaningless across one.
Every node returns its HWND, its child-index path (`0/3/1`), its control id and
its class. `GetDlgCtrlID` is the most stable handle inside a dialog; the path
survives a repaint.

**A full-screen DIB is 307KB and physical memory and heaps are tight.** So capture proceeds in
**strips** of N scanlines into a small fixed buffer, each strip compressed and
sent as it is taken. Region capture is the default -- the control tree already
returns window rects, so "read this dialog" sends ~10KB instead of ~120KB and
costs ~80 vision tokens instead of ~850. Full-screen is the explicit exception.

`screen_palette_for_capture()` is the one piece of retro-agent's `screen.c`
worth keeping: on an 8bpp display a fresh memory DC has only the 20 static
colours, so `GetDIBits` needs the live hardware palette selected in or the
result is colour-index noise. Hardware-verified, non-obvious, easy to lose.

**`screen.c` is otherwise not lifted.** Under "the guest transforms nothing",
`SCREENDIFF`, the 64x64 tiling, `g_prev_pixels` and `resp_buf` all move to the
bridge, which has memory to spare. That deletes the worst memory item the plan
ever carried: a ~4.4MB guest peak to compute a diff the bridge computes free.

### OCR, on the bridge, as a fallback

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

### click: one execution path, two ways to resolve a point

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

### send_keys: focus first, and the real hazards are not the famous one

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

### send_action_sequence: closed-loop, never open-loop

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

### The UI tier's tools

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
same chunked sink Export as HTML needs, which is why it treats the
two as one primitive with two callers rather than two features.

---

## Repo layout

```
RetroAI/
  README.md  NOTICE  LICENSE
  Makefile                    # exe test lint wine qemu
  make/ow.mk                  # -bm, WLINK_LNK, WINVER=0x0400, verified rules
  win/                        # the guest side -- SOURCE ONLY
    app/                      # GUI, window, HTML view, toolbar, dialogs
    core/                     # App/Service lifecycle, INI, log, guards
    wire/                     # frame codec, deflate, Connection, Listener
    exec/ fs/                 # command exec and the file lane
    automation/ ui/ inproc/   # input, the walk, the in-process tier
    remote/ power/            # gremote's own app, tray, suspend handling
    view/ resources/          # the transcript view and its assets
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
  tests/guest/                # the check binaries: the shipping C, run under Wine
  research/                   # guest/ probes/ deflate-speeds/ measurements/ -- apparatus
  tools/                      # vm/ gen/ check/
  vm/                         # disk images and run scripts (gitignored)
  gremote/                    # host-side Python driver for gremote.exe
  docs/                       # published: how it works, and GUIDE.md
  notes/                      # working state, gitignored
```

`win/` also builds `gremote.exe` and the static libraries `libcore`,
`libwire`, `libexec`, `libfs` and `libautomation`, which the client links
too. `docs/FORKED.md` records which retro-agent files at which commit with
what changes.

**There is no console on this platform.** "No log file created" is the
primary signal that `main()` never ran — a single NT-only static import
kills the image at load, before `WinMain`, with nothing written. Help >
Diagnostics shows connection state, the last Winsock error, and frames and
bytes in and out.

## Verification

- `make check` — PE imports, W-suffix list, CMOV, stack frames, static
  allocation against a per-image budget.
- `make lint` — grep for RichEdit 3.0-only messages
- `make test` — protocol round-trip, markdown→runs, CP1252 fold; PE allowlist from Phase 3
- `make wine-smoke` — code sanity only, never a Win98 proof
- `bridge/fakegui.py` — full end-to-end chat with no Win98 involved
- Per-phase demos as listed; Phase 2 has four
- **[R3] Test over a throttled link, not just the LAN.** `tc netem rate 40kbit` on the
  bridge side simulates dialup without a modem. Every phase demo should be run once
  throttled — it is the cheapest way to stop LAN-speed assumptions leaking into the design
