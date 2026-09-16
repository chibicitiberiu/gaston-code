# Progress

Rolling log. Plan is `PLAN.md`; phases 0–2 are executable, 3–6 are a sketch
that gets re-planned once Phase 0 answers land.

**Current goal: complete phases 0–2.**

---

## VM checkpoints

Snapshot after each milestone, before anything else touches the disk. A ruined
Win98 install is an hour to redo; a snapshot is seconds.

    export LIBVIRT_DEFAULT_URI=qemu:///session
    virsh snapshot-create-as retroclaude-win98 <name> "<description>"
    virsh snapshot-list       retroclaude-win98
    virsh snapshot-revert     retroclaude-win98 <name>

| Snapshot | When | State |
|---|---|---|
| `clean-install` | Win98 SE booted to desktop, drivers settled, before anything is installed | **done 2026-09-16 20:53** |
| `networked` | TCP/IP reaching the bridge | **done 2026-09-16 22:04** |
| `tools` | RetroClaude binaries copied to C:\RETRO | **done 2026-09-16 22:57** |

## Blockers

| Blocker | Blocks | Needs |
|---|---|---|
| ~~Win98 SE install media~~ | — | **RESOLVED 2026-09-16.** User supplied `Win98SE_bootable.iso` + product key. Unattended install running under libvirt. |
| Real hardware access | end-of-Phase-1 gate | The physical P200MMX on the LAN |
| Samba server not installed | 0.8 (LFN over `vers=1.0`) | `sudo dnf install samba` on the bridge host. `samba-common` is present but there is no `smbd`, and this session has no passwordless sudo. Everything else in Phase 3 is unaffected until then. |

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
| 0.5c | Deflate throughput on the real P200MMX | **provisional** | Ran on the guest: L1 7707 KB/s compress, 16379 KB/s inflate, round trip PASS. **Not usable as a threshold**: TCG on a modern host is nothing like a P200, and `probe.c`'s sample is a two-line block repeated (164:1), which flatters deflate badly. Needs a real corpus and real hardware |
| 0.6 | Win98 SE guest under QEMU | **done** | Unattended via MSBATCH.INF. Required config: TCG (not KVM), no APIC, no ACPI |
| 0.7 | W-suffix import load test (`GetCommandLineW`/`GetModuleFileNameW`) | **done — PASS** | The image loads on Win98 SE 4.10.2222 A. Both symbols resolve against Win98's kernel32. The last unverified toolchain assumption is settled; Open Watcom stands |
| 0.8 | Samba LFN over `vers=1.0` | **blocked** | No `smbd` on the host (see blockers). Guest-side LFN itself does work: `copy retrocl.ini "retroclaude.ini"` created and read a long name from COMMAND.COM |
| 0.9 | 16-bit `EXEC` capture — **correct non-empty output**, not "no hang" | **done — PASS** | `command.com /c ver` 36B, `dir c:\ /w` 352B, and `echo hello \| find "hello"` 8B first line `hello`. Non-empty **and correct**, including the known-bad 16-bit FIND. **Phase 3's EXEC design survives**; the claudewin9x temp-file fallback is not needed |
| 0.10 | `GetDIBits` → `BI_RLE8`? | **done — PASS** | 640x480 8bpp: 307,200B raw -> **27,088B in 14ms, 11.3:1**, done by GDI. Beats the plan's 3-8:1 estimate for hand-rolled RLE, and the ~40 lines of RLE8 are not needed |
| 0.11 | `_beginthreadex` vs `CreateThread` under `-bm`, with a message loop | **done — PASS** | 3/3 `_beginthreadex` threads plus a message sink window, 275 `PostMessage` round trips. **Correction to the plan**: `CreateThread(lpThreadId=NULL)` was *accepted* on this Win98 SE, so retro-agent's hardware note does not reproduce. `_beginthreadex` stays, on the OW per-thread-data argument, which is independent |

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
| 1.9 | Real hardware gate (period PC) | blocked |
| 1.10 | **Guest gate: `netcheck.exe` on real Win98 -> bridge -> Claude** | **done — PASS** |
| 1.11 | **Guest gate: `retroclaude.exe` GUI on real Win98** | **done — PASS** |

## Phase 2 — four demos

| # | Item | State |
|---|---|---|
| 2a | Streaming raw text + `enc=deflate` bridge→guest | **done** — verified on the guest |
| 2b | Styled runs (markdown → `CHARFORMAT2`) | **done** — verified on the guest |
| 2c | Pagination | **done** — verified on the guest, sealed at a message boundary |
| 2d | Session list | **done** — verified on the guest, switch replays history |

All four demoed on real Windows 98 SE on 2026-09-16, not just under Wine.

---

### Phase 2 verification status

- 2a streaming + adaptive deflate: verified both link regimes over Wine
- 2b markdown -> styled runs: verified over the wire; no markdown syntax
  reaches the guest
- 2c pagination: 32 tests including wire-level integration; history pages never
  receive live text
- 2d session list: `list_sessions()` against real on-disk sessions, chat list
  renders in the client under Wine

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

### 2026-09-17 — menu bar, and the app is called Gaston Code

**The name.** "Claude Code" is Anthropic's product name and this is a
third-party client for it, so shipping under that name is not on. Gaston is a
French given name of roughly the right vintage for the machine it runs on. The
About box says "Not affiliated with Anthropic" for the same reason.

**The menu bar** is built with CreateMenu/AppendMenu rather than from a
resource: it is a dozen items, and the two interesting menus are filled in at
runtime anyway.

  File      Export as HTML... (wired, not implemented), Close
  Configure Server Host... (shows the current setting; dialog still to come)
  Model     from the bridge, with a checkmark
  Effort    from the bridge, with a checkmark
  Help      About...

**Model and effort lists come from the bridge**, the same argument as the
stylesheet: model names change, and changing a string should not mean
rebuilding and re-deploying a Win98 binary. The guest numbers the menu entries
from a base (IDM_MODEL0, IDM_EFFORT0) because the lists are not known at
compile time, and "which one is current" has exactly one source of truth.

**Effort cannot be changed on a live session, and that is now confirmed rather
than assumed.** The installed SDK has `set_model()` and **no** `set_effort()`;
effort is a field of ClaudeAgentOptions, fixed when the session starts. So the
plan's fork dance is what actually happens: close the client, resume the same
conversation with `fork_session=True`, and tell the reader in the transcript
that the conversation was forked and why. Model changes are live.

Both verified on the guest: selecting Sonnet 5 moved the checkmark and logged
`model -> claude-sonnet-5`; selecting High logged `effort -> high (forking ...)`
and reconnected as `model=claude-sonnet-5 effort=high`, so the per-guest choice
survives the fork rather than reverting to the process defaults.


### 2026-09-17 — what HTML bought, and what it cost

Things the transcript can now do that RichEdit 2.0 could not do at all:

  * **tables.** Markdown tables render as real tables with a tinted header
    row, borders and wrapping cells. The styled-run renderer could only ever
    have shown the pipe characters.
  * **ordered lists.** `1.` becomes `<ol>` rather than silently turning into a
    bullet and losing the numbering the model meant.
  * a code block with a language label bar that **scrolls inside its own box**
    rather than giving the whole transcript a horizontal scrollbar.
  * padding, margins, borders and background tints, which is most of what made
    the old view look like a text dump.

Two rendering bugs found by reading the screen rather than the tests:

  * **`code` inside `**bold**` was escaped rather than parsed**, so backticks
    appeared literally. The inline matcher now recurses into the emphasis
    span; the delimiters are stripped first, so it cannot re-match itself.
  * **underscores inside identifiers were emphasis.** `BPB_RsvdSecCnt and
    BPB_NumFATs` rendered as one italic run from the first underscore to the
    last, mangling exactly the identifiers this project talks about all day.
    Underscore emphasis now requires a word boundary on both sides, which is
    what every markdown implementation does; asterisks are unrestricted.

### 2026-09-17 — pages are bounded by element count now, not just bytes

The byte cap alone is the wrong bound for an HTML renderer, because MSHTML's
cost scales with **element count**: a hundred one-line paragraphs cost far more
than one paragraph of a hundred lines, and a byte cap cannot tell those apart.
`MAX_BLOCKS = 90` (hard cap 180) now sits alongside the line and byte caps.
Unlike the byte cap it never splits a run -- a block boundary is already a
clean place to stop, so `add()` seals between blocks before starting the next.

**It did not help the case that prompted it, and the reason is worth keeping.**
A 200-line answer was assumed to be 200 elements. It is not: consecutive
non-blank lines are one markdown paragraph, so it renders as a single `<p>`
with 199 `<br>` separators -- one block, one element, and the cap never fires.
Measured either way, the guest sat at 20-48 KB free, so the document was never
the expensive part. The cap is still correct for a conversation made of many
separate blocks; it simply was not the lever for this workload, and the "200
elements" reading of the first measurement was wrong.

Memory on the 32MB guest, free of 32288 KB:

| | free | reading |
|---|---|---|
| desktop, before MSHTML has ever loaded | 9928 KB | the honest baseline |
| engine resident, app closed | 2548 KB | the engine holds ~7.4 MB and **does not give it back** |
| app running, 200-line answer | 20-48 KB | app + document ~2.5 MB on top |

Total ~9.7 MB against the 20 MB budget, so the gate passes, but the engine is
7.4 MB of it and that part is fixed.

**The obvious lever was tried and does not work yet.** Hosting
`CLSID_HTMLDocument` directly instead of `CLSID_WebBrowser` should avoid
shdocvw and urlmon along with navigation, history and the WinInet cache, none
of which a transcript needs. It compiles, activates, and paints nothing: MSHTML
hosted directly is a **document object**, not an ordinary embedded control, so
it does not simply create a window on `DoVerb`. It calls back through
`IOleDocumentSite::ActivateMe` and hands over an `IOleDocumentView`, which the
host must then `SetInPlaceSite` / `UIActivate` / `SetRect` / `Show`. That was
implemented -- and it still came up blank, so something else in the DocObject
contract is missing.

Reverted to the WebBrowser control, which does that plumbing and works. If the
~2 MB is ever needed, this is where to start, and the missing piece is in the
activation sequence rather than in the interfaces themselves.

Worth stating plainly: `dwAvailPhys` on Win9x excludes the disk cache, which
shrinks under pressure, so "48 KB free" is the edge of the cache being
squeezed out rather than the edge of failure -- the app stayed responsive
throughout. It is still much tighter than the RichEdit version's 1.4 MB, and
that is the price of the look.


### 2026-09-17 — the transcript is HTML now

RichEdit 2.0 is gone. `win/transcript.c` is deleted, `win/htmlview.c` hosts
MSHTML, and the bridge emits HTML instead of styled runs.

**Why, given the plan said no.** The plan's objection was a "multi-MB DLL tax
... fixed per process so pagination doesn't reduce it", which was an estimate,
and the counter-argument is that Windows 98's own Explorer hosts MSHTML for
folder Web View. Measured rather than argued (32MB guest, all figures free of
32288 KB):

| | free | marginal |
|---|---|---|
| desktop, nothing running | 9928 KB | — |
| RichEdit client, chat loaded | 8688 KB | ~1.4 MB |
| **HTML client, chat loaded** | **700 KB** | **~9.2 MB** |
| after closing the HTML client | 3236 KB | engine stays resident: ~6.7 MB |

So ~9.2 MB against a 20 MB budget: it passes, and it is ~7.8 MB more than
RichEdit. Most of that is the engine itself, which **stays loaded after the
app exits** -- free memory does not return to the 9928 KB baseline. Physical
free drops to ~700 KB while running, so the machine leans on its swap file;
`dwAvailPhys` excludes the disk cache, so the real headroom is better than that
number looks, but it is not comfortable.

**What it buys**, all of which RichEdit 2.0 cannot do at all: padding and
margins, borders, background tints, real bulleted lists, a language label bar
on code blocks, a code block that scrolls inside its own box, and inline
screenshots later. The look is not a marginal improvement.

**Architecture.** Unchanged where it matters: pagination still bounds memory,
the coalescer still batches at ~10fps, compression is untouched, and the guest
still transforms nothing. Two changes:

  * **The stylesheet lives on the bridge**, sent once in a `DOC` frame. The
    look can be changed by restarting the bridge instead of rebuilding and
    re-deploying a binary, which turned a 4-minute iteration into a 30-second
    one while tuning the CSS.
  * **Two containers, `#t` and `#l`.** Finished blocks accumulate in `#t` and
    are never touched again; `#l` holds the block still being written and is
    replaced wholesale as it grows. Without the second one a paragraph would
    only appear once complete, which reads as a stall rather than as
    streaming. Only `#t` blocks are recorded in the paginator -- storing the
    live one would put a dozen half-finished copies of the same sentence into
    the page.

**Three things that cost time:**

  * `oaidl.h` declares VARIANT with **named** unions under `__STDC__`, but
    `oleauto.h`'s `V_VT`/`V_UNION` macros only follow when `NONAMELESSUNION`
    is defined. Left alone the two disagree and every VARIANT access fails to
    compile. Define `NONAMELESSUNION` before the OLE headers.
  * The control is created late, when the `DOC` frame arrives, so it has
    missed every layout and is still sized to whatever `DoVerb` was handed.
    The document loaded, `insertAdjacentHTML` returned `S_OK`, the DOM read
    back the right content -- and nothing painted, because the window was the
    wrong size. Call `layout()` right after creating it.
  * IE5 has no `pre-wrap` and no `word-wrap`, so one long line in a code block
    gives the **whole transcript** a horizontal scrollbar and every paragraph
    has to be panned to read one line of C. `overflow: auto` plus an explicit
    `width: 100%` on the wrapper confines the scrolling to the code block.

Open Watcom ships `mshtml.h`, `exdisp.h`, `docobj.h`, `oleidl.h` and
`uuid.lib`, so nothing was vendored. It does not ship `mshtmhst.h`, so
`IDocHostUIHandler` is unavailable without hand-declaring it; it only controls
chrome, and the scrollbars it would suppress are ones the transcript wants.


### 2026-09-17 — MSHTML is affordable after all, and it looks far better

The plan rejected MSHTML for v1 partly on a "multi-MB DLL tax ... fixed per
process so pagination doesn't reduce it". That was an estimate, and the user
pointed out the obvious counter: **Windows 98's own Explorer hosts MSHTML** for
folder Web View, so the engine is already resident in the shell and a second
host pays only for its own instance data.

Measured on the 32MB guest rather than argued about:

| | free of 32288 KB | cost |
|---|---|---|
| baseline, no app | 9928 KB | — |
| RichEdit client, large chat loaded | 8688 KB | **~1.4 MB** |
| CoInitialize | 8312 KB | 0 KB |
| + LoadLibrary(mshtml.dll) | 8028 KB | 284 KB |
| + CoCreateInstance(HTMLDocument) | 7932 KB | 96 KB |
| + CoCreateInstance(WebBrowser) | 7800 KB | 132 KB |
| **spike hosting a rendered document** | **5832 KB** | **~4 MB** |

Two numbers matter and they are different: **512 KB to instantiate** the engine,
**~4 MB with a document actually laid out and painted**. Quoting the first as
the cost of an HTML transcript would be wrong.

~4 MB against a 20 MB budget, with ~5.9 MB physical still free, so it fits. It
is ~2.6 MB more than RichEdit, and it buys padding, margins, borders,
background tints, real bulleted lists, tables, and eventually inline
screenshots -- none of which RichEdit 2.0 can do, and all of which the
transcript visibly wants.

**The spike** (`win/htmlspike.c`, ~360 lines) hosts the WebBrowser control from
plain C89: IOleClientSite, IOleInPlaceSite and IOleInPlaceFrame as embedded
structs with static vtables, recovered to the host by fixed offsets. It
activates and paints on real Win98 SE. Open Watcom has `mshtml.h`, `exdisp.h`,
`docobj.h`, `oleidl.h` and `uuid.lib`, so nothing had to be vendored.

One gap: OW ships `mshtml.h` but **not `mshtmhst.h`**, so `IDocHostUIHandler`
has to be hand-declared. It only controls chrome -- the 3D border, the
scrollbars, the context menu -- so the spike does without it and answers the
question that actually gated the decision.


### 2026-09-16 — the transcript was never broken; autoscroll was overshooting

Chasing "the chat window has rendering issues" produced one genuinely
misleading bug and several real ones.

**The misleading one.** After a turn the transcript went blank, with a
scrollbar showing content. It looked exactly like the riched20 priming failure
all over again, and three plausible causes were investigated and cleared
before the actual answer turned up: scrolling to the top with Ctrl+Home showed
**the answer had rendered perfectly all along**. `WM_VSCROLL`/`SB_BOTTOM` was
parking the view about a screenful BELOW the last line, because it is computed
from scroll metrics that are still stale immediately after `WM_SETREDRAW` is
turned back on. It only appeared once a turn grew past one screenful, which is
why short replies had looked fine all evening.

Fixed by scrolling the caret instead: every append already leaves the
selection collapsed at the end, so `EM_SCROLLCARET` after the repaint is
exactly "show the newest text" and cannot overshoot.

**Two things that really do wedge riched20**, both found while hunting the
above, and both worth knowing because they look like the same symptom:

- **Deleting with `EM_REPLACESEL` stops the control painting.** The obvious
  design for a progress indicator is to append "Claude is thinking..." and
  remove it when output arrives. The delete lands on the right range and the
  control never draws again. Measured twice, including with the selection
  positions corrected: `truncate to 152 from 174 left 153`.
- **So does a select-all, when redraw is off.** `WM_GETTEXTLENGTH` counts a
  line break as CRLF while selection positions count it as one, so the two
  disagree by the number of lines. The "accurate" fix -- select `0..-1`, read
  the range back, collapse -- is a select-all on every append, and inside a
  batch (`WM_SETREDRAW FALSE`) that wedges the control too. The over-count is
  harmless for appending because `EM_EXSETSEL` clamps, so appending
  deliberately keeps using it.

Consequence: **nothing that has to disappear again goes in the transcript.**
The thinking indicator lives in the title bar ("Claude Code - thinking...")
and in a status pane that ticks the elapsed seconds, neither of which requires
touching the control.

### 2026-09-16 — the chat list was overdrawing its own border

The stray vertical marks were the listbox's `WS_EX_CLIENTEDGE` border being
painted over. Measured rather than guessed: x=8 should be `(128,128,128)` and
x=9 `(0,0,0)`, and on 152 of 365 rows both were white instead. The bands were
row-height multiples, stayed put when the list was scrolled, and survived a
minimise/restore.

They lined up exactly with the rows whose label was **wider than the client
area**: Win98's listbox overdraws its own client edge when an item overflows.
Labels are now truncated to fit with `GetTextExtentExPointA` (one GDI call per
label, not a search) and an explicit "...", which fixes the artifact and also
replaces silent truncation at the frame with something that reads as "there is
more". The list went 150px -> 170px while we were there, and the rebuild is
batched under `WM_SETREDRAW` rather than repainting once per `LB_ADDSTRING`.

Result: 360/360 rows now carry the correct border colours.

### 2026-09-16 — CP1252 0x80-0x9F has no glyphs in the stock bitmap font

An em dash rendered as a vertical bar and a euro sign as a capital I. Both are
perfectly valid CP1252, which is why `fold()` passed them straight through.

0x80-0x9F is where Windows put the extra typographic characters, and Win98's
stock UI font has nothing there; everything from 0xA0 up is Latin-1 and renders
correctly. The whole range is now folded to ASCII -- em dash to `--`, euro to
`EUR`, trademark to `(tm)` and so on -- with a test that walks all 32
codepoints rather than listing examples. Two older tests asserted the euro
survived; they were asserting something the guest disagrees with, and were
corrected.

Separately, `CFM_CHARSET` with `ANSI_CHARSET` is now set on every run. Without
it RichEdit picks a default that need not be Windows-1252, and accented Latin-1
bytes render as whatever the font's default mapping has there. With it,
`café naïve über` comes out right.

### 2026-09-16 — one definition of what a user turn looks like

`user_block()` in `bridge/markdown.py` renders a rule, the prompt lines each
prefixed `> `, and a blank line. Both ends build it: the guest echoes it the
instant Send is pressed, so a slow link never swallows your own typing, and the
bridge records the identical string into the paginator **without streaming it**
(`_write_text(..., stream=False)`).

Recording it is the point. A sealed page is re-sent in full when the reader
pages back, and before this the paginator only held an anchor, so paging back
gave a transcript of answers with no questions in it.


### 2026-09-16 — the blank transcript: riched20 must be primed with real text

The single worst bug of the guest bring-up, and the one the Wine tier was least
able to catch.

**Symptom.** On real Win98 the transcript control stayed blank forever. Not
"wrong font", not "wrong colour" — nothing at all, while everything else in the
window worked.

**What it was not.** Ruled out, in order, each by measurement rather than
argument:

- not insertion: `WM_GETTEXTLENGTH` returned 22 and `WM_GETTEXT` returned
  `Connected to bridge.`, so the text really was in the control;
- not geometry: `GetWindowRect` gave `162,51 470x345` and `EM_GETRECT` gave a
  formatting rectangle of `3,4,463,338`, both correct, with `IsWindowVisible`
  true;
- not the character format: `EM_SETCHARFORMAT` was accepted and
  `sizeof(CHARFORMAT2A)` is 84, the RichEdit 2.0 value;
- not redraw batching: putting `WM_SETREDRAW` behind a switch and turning it off
  changed nothing;
- not `ES_READONLY` swallowing `EM_REPLACESEL`, though that was a real latent
  bug and is fixed too (the style is now applied with `EM_SETREADONLY` and
  lifted around each batch).

**What it was.** Win98's `riched20` does not lay out or paint what
`EM_REPLACESEL` puts into a control that has never held text. Two details, both
found the hard way:

- an **empty** `WM_SETTEXT` does not count. The control needs actual
  characters, which is why the priming `\r\n` is left in place rather than
  deleted afterwards;
- priming must happen with **redraw enabled and followed by an actual paint**.
  Priming from inside an append batch (which runs under `WM_SETREDRAW FALSE`)
  does nothing, which is why page loads stayed blank after the live page had
  been fixed — `clear()` -> `prime()` -> `begin()` disabled redraw before the
  control ever painted.

So `prime()` sets real text, forces `InvalidateRect` + `UpdateWindow`, and is
called from `rc_transcript_create` and from `rc_transcript_clear`, never
mid-batch.

**The lesson is about the Wine tier, not about RichEdit.** Wine's `riched20`
paints without any of this, so `make wine-smoke` passed the entire time. The
plan already said Wine proves code sanity and never platform behaviour; this is
what that sentence costs when you find out the hard way.

### 2026-09-16 — three process bugs that each cost more than the code bug

Recording these because all three made a *fixed* bug look unfixed.

1. **`win/Makefile` had no header dependencies.** Adding two fields to
   `rc_transcript` in `transcript.h` did not rebuild `main.o`, so `main.c`
   still allocated the old struct while `transcript.c` wrote the new fields
   past the end of it, corrupting the next global. The symptom was
   `PostMessage` failing on a valid HWND with `inflight=0` — nowhere near the
   change, and a dead ringer for a Win9x message-queue limit. Every object now
   depends on every header.
2. **`virsh change-media --update` with the same source path is a no-op.** The
   drive keeps the file it already has, so a rebuilt `tools.iso` is never seen
   and the guest quietly keeps running the previous binary. Several "the fix
   did not work" cycles were testing a stale exe; the guest's copy was 131,584
   bytes while the build was 132,096. `tools/vmship.sh` now ejects first and
   prints both sizes.
3. **`pkill -f` matches the shell running it.** Killed my own session, exit
   144, twice. Find the process by its listening port instead.

### 2026-09-16 — header values are CP1252, percent-encoded to ASCII

`_quote` escaped only `%`, LF and CR. `SESSION` frames carry session titles,
which are arbitrary user text, so the first real chat list sent to the guest
crashed the bridge with

    UnicodeEncodeError: 'ascii' codec can't encode character '\u2442'

on every reconnect, in a loop, so the client could never populate its chat
list. Header values are now folded to CP1252 and percent-encoded for anything
outside printable ASCII, decoded in a single pass so `%25` does not unescape
twice. Six regression tests, including control characters and a stray `%` with
no hex digits after it.

The general point: the guest has one codepage and it is not Unicode, so a
header and the body it describes have to agree about what a character is.

### 2026-09-16 — Win98 has no PCI bus here, so the NIC is ISA

The PCnet would not enumerate. `C:\DETLOG.TXT` said why, and said it about the
machine rather than the driver:

    Checking for: System Bus
    CheckInt86xCrash: int 1a,AX=b101,rc=0
    SetVar: PCIBUS=
    MatchAcpiOemIdRule: ACPI not detected
    Detected: *PNP0C00\0000 = [1] Plug and Play BIOS
    SetVar: PNPBIOS=

`INT 1Ah/AX=B101` is the PCI BIOS installation check; it came back empty, so
Win98 concluded there is no PCI bus and enumerated not one PCI device. Device
Manager has no `PCI bus` node at all and the PnP BIOS node sits at Code 24.
Both `DETLOG.TXT` and `DETLOG.OLD` agree, so it has been true since install.

SeaBIOS is not at fault: `_32_` at 0xf6100, `$PnP` at 0xf6120 and `$PIR` at
0xf55c0 are all present, 16-byte aligned, with valid checksums.

Rather than chase it, the NIC is now an **ISA NE2000**, which needs no
enumeration at all — and is period-correct for the Pentium MMX 200 this guest
stands in for. Two gotchas: libvirt assigns a PCI slot unless an
`<address type='isa'/>` is given (QEMU then refuses with "Device 'ne2k_isa'
can't go on PCI bus"), and the card sits at **0x200/IRQ3** rather than the
NE2000 factory 0x300 because that is what Win98's "Basic Configuration 0" pins
the driver to and the Resources tab will not step off it. Moving the emulated
card was cheaper than fighting the dialog.

### 2026-09-16 — the APIC was never actually removed

The IRQ0 hang came back the moment the guest rebooted after the network
install, at the same EIP as before. `<features/>` with no `<apic/>` only drops
libvirt's own hint; the `pentium2` CPU model still carries the CPUID APIC bit,
so QEMU built a local APIC and an IOAPIC anyway. `info lapic` showed
`SPIV 0x000000ff APIC disabled` with `LVT0` masked — Win98 had software
disabled the LAPIC, and the 8259's ExtINT then had nowhere to go:
`pic0: irr=01 imr=b8 isr=00` with `IF=1`.

The fix that actually removes it is a CPU feature, not a machine feature:

    <cpu mode='custom' match='exact' check='none'>
      <model fallback='allow'>pentium2</model>
      <feature policy='disable' name='apic'/>
    </cpu>

After that `info lapic` reports "local apic state not available" and the guest
boots.

### 2026-09-16 — `virsh define` restored the floppy and Setup ran again

`vm/win98.xml` still had `<boot dev='fd'/>` and a floppy device from install
day. Redefining the domain from that file under a working installation put
Setup back on screen at "Preparing to run Windows 98 Setup". Caught at 90% and
restored from a backup taken two minutes earlier, so nothing was lost.

The floppy device is now deleted from the XML outright rather than ejected —
ejecting does not survive a restart — and `tools/vmdefine.sh` refuses to define
any config that boots `fd` first or still carries a floppy, and validates the
XML first (libvirt rejects `--` inside a comment and points at the wrong line
when it does).

### 2026-09-16 — driving the guest: what works

- **Keyboard is the reliable path.** `Ctrl+Esc`, `r`, type, Enter reaches
  anything through Start->Run. Control Panel applets by name:
  `control netcpl.cpl` for Network, and Add New Hardware is applet 1 of
  `sysdm.cpl`, reached with
  `rundll32.exe shell32.dll,Control_RunDLL sysdm.cpl,@1`.
- **The mouse needed calibrating.** QEMU's PS/2 mouse is relative, so
  `tools/qmp.py` homes the pointer into a corner and walks to the target. A
  step of 4 overshot by almost exactly 2x (a requested `100,100` landed at
  `197,192`); a step of 1 is exact. The queue is short, so the walk is paced.
- **The pointer is not in `screendump` output**, so position has to be found by
  differencing two raw PPMs rather than by looking for a cursor.
- **`COMMAND.COM` rejects a trailing backslash on a COPY destination**
  ("Invalid directory"). `COPY D:\*.EXE C:\RETRO` works, `C:\RETRO\` does not.
- **Reading a guest file from the host mid-run does not work**: the qcow2 shows
  allocated-but-unwritten clusters (all zeros) because Win98's cache has not
  flushed, and the running app holds the log open ("Sharing violation"). Use
  `find /v "..." C:\RETRO\retroclaude.log` inside the guest instead.

### 2026-09-16 — known cosmetic issues in the chat list

Not fixed, and both predate the guest bring-up — they are visible in the first
screenshots of the original binary:

- **A 1px vertical line at x=9** inside the list, three pixels into the client
  area and inside the first character cell, spanning *contiguous groups* of
  rows (1-5, 8-12, 14-15, 19, 22-23, 26 in one capture) with clean gaps
  between groups. The wire bytes are plain ASCII, so it is not corrupt text,
  and contiguous multi-row runs with no inter-row gaps argue for a partially
  overdrawn border rather than a per-row glyph. Root cause still open.
- **Titles are clipped** at the listbox width with no horizontal extent and no
  tooltip, so long names cannot be read at all.


### 2026-09-16 — Win98 needs TCG, and needs APIC/ACPI OFF

Two separate emulation faults, both diagnosed from guest CPU state rather than
guessed at. Recording them because neither is obvious and both cost hours.

**1. KVM corrupts Win98's loader.** Under `domain type='kvm'` the install
completed but Explorer died and then:

    The SHELL32.DLL file is linked to missing export SHLWAPI.DLL:...FileAttributesA

Both DLLs on the guest disk were **byte-identical to the install media** (md5
compared against the files extracted from the ISO's cabinets). A missing export
is impossible when both files are correct, so the import resolution itself was
executing wrongly. Fix: `domain type='qemu'` (TCG). Slower, correct, and still
far faster than the Pentium MMX 200 being emulated.

**2. `<apic/>` sends IRQ0 into a black hole.** Win98 then hung in real mode,
spinning forever at a fixed EIP. `info registers` and `info pic` gave it away:

    EIP=00009dfb  CR0=00000010 (PE clear -> real mode)  EFL=0x246 (IF set)
    pic0: irr=43 (IRQ0 PENDING) imr=b8 (IRQ0 UNMASKED) isr=00 (nothing blocking)
    IRQ0 delta: 92 interrupts in 5s = 18.4Hz -- the PIT was perfect

Disassembling the hang site showed Win98's CPU speed-calibration loop:

    0x8ee4b:  movw %es:0x46c, %dx   ; BIOS tick counter at 0040:006C
    0x8ee50:  cmpw %ax, %dx
    0x8ee52:  je   0x8ee4b          ; wait for it to change -- it never did

A pending, unmasked, unblocked interrupt was never delivered to a CPU with
IF=1, because `<apic/>` gives QEMU an IOAPIC whose pins Win98 (a PIC-era OS)
never unmasks. Removing `<apic/>` and `<acpi/>` fixed it: the tick immediately
resumed at 18.25Hz.

**Lesson for the install procedure:** guest CPU state via
`virsh qemu-monitor-command --hmp 'info registers' / 'info pic' / 'x/24i'` is
the tool that settles "hung or slow", and it works when the guest is wedged.
Screenshot md5s do NOT settle it -- ImageMagick stamps PNG metadata, so
identical screens produce different hashes. Compare raw PPM bytes instead.

### 2026-09-16 — switching chats is close + resume, not a pool
One live `ClaudeSDKClient`. Switching closes it, reopens with `resume=<id>`, and
replays the stored history into a fresh `Paginator` so the guest can page back
through it exactly as if it had streamed. A warm pool is ~3x the work and
invisible on a single-user box.

### 2026-09-16 — ListBox, not ListView, and double-click to switch
ListBox costs fewer GDI objects, and the 16-bit USER/GDI heaps on 9x are a fixed
64KB that does not scale with RAM. Double-click rather than selection-change,
because an accidental arrow key would otherwise tear down a live session.

### 2026-09-16 — stored history is replayed defensively
`_text_of` walks persisted message data whose shape is not guaranteed across
releases. Every branch tolerates missing keys and wrong types, because a
KeyError there breaks the whole chat list rather than one message. Thinking
blocks are skipped on replay: long, and never the point of scrolling back.

### 2026-09-16 — three test-harness bugs that all looked like server bugs
Worth recording because each wasted time and each was in the *test*, not the
code under test:

1. `Server.wait_closed()` since Python 3.12 waits for existing **connections**,
   not just the listener. A test leaving a connection open hangs teardown
   forever. Bound it with `wait_for`.
2. A fresh `FrameReader` per receive call discards whatever partial frame the
   previous call buffered.
3. Returning on the first matching frame drops the rest of that `read()` batch
   — several frames normally arrive together.

Symptom in all three cases was "the server never sent TEXT". It always had.
Confirmed by reproducing the exchange in a standalone script, which worked
first try and pointed the finger squarely at the harness.

### 2026-09-16 — HISTORY mode must not receive live text
`_write_text` streams only when `view_page` is None or matches the live page.
There is a test asserting new text never carries `page=1` while the reader is
on page 1 — without it, text silently appears in the middle of a page the user
is reading.

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
