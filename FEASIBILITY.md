# Claude Code on Windows 98 — Feasibility Report

Date: 2026-09-16
Status: research complete, no code written

Sources: full source reads of two prior-art projects, platform research against
primary sources (MS KB/MSDN archives, Samba docs, QEMU docs, binutils docs), and
the current Claude Code programmatic-integration docs. Local environment probed
on the Fedora 44 host.

---

## 1. Verdict

The idea is feasible. Every individual capability has been demonstrated by
someone, on real hardware, and the pieces compose. Nothing in the plan depends on
an unsolved problem.

The risk is not technical possibility. It is scope: a polished version is roughly
10,000-15,000 lines across three codebases in two languages, one of which is C
targeting a platform whose failure modes are undocumented and only discoverable
by running into them.

Component-by-component:

| Component | Verdict | Evidence |
|---|---|---|
| Native Win98 app talking to a modern box | Proven | Both prior-art projects do it |
| Modern box proxying Claude Code | Proven | claudewin9x drives the real CLI via stream-json |
| Remote exec with output capture | Proven | retro-agent captures stdout+stderr via pipes |
| Screenshots from the guest | Proven | retro-agent, including the 8bpp palette fix |
| Mouse/keyboard injection | Proven | retro-agent via mouse_event/keybd_event |
| Window enumeration | Proven | retro-agent, EnumWindows, capped at 64 |
| Registry read/write | Proven | retro-agent |
| Approval dialogs on the Win98 box | Feasible, unbuilt | canUseTool has no timeout; nobody has built the UI |
| Encoding/codepage layer | Feasible, unbuilt | Neither project has one; both punt to ASCII |
| Native GUI (not a console) | Feasible, unbuilt | No prior art anywhere in this space |
| Hang detection and recovery | Partly solved | retro-agent has a watchdog; nobody has out-of-band recovery |
| Native TLS on Win98 (purist path) | Proven | OpenSSL 1.0.2 runs on stock Win98 |

The three "unbuilt" rows are your contribution. They are also the three hardest
rows.

---

## 2. Prior art, in detail

### 2.1 claudewin9x (ryandeering)

~2,617 lines of C (client) + ~2,522 lines of C# (server). A few-weeks solo
project. HEAD is a single squashed commit dated 2026-01-09: "deprecate mingw, use
Open Watcom, 386 compatibility".

What it proves: the modern side genuinely works. The server spawns the real
`claude` CLI with:

    --input-format stream-json --output-format stream-json --print --verbose
    --dangerously-skip-permissions --append-system-prompt

and parses events line by line. Streaming text display works via
`content_block_delta` accumulated server-side and drained on a 1-second client
poll.

What it gets wrong, and what to learn from each:

- **No MCP.** The entire remote tool surface is taught to Claude as curl
  one-liners inside the Bash tool, via a ~1,400-token appended system prompt. That
  prompt contains a section titled "CRITICAL BACKSLASH ESCAPING RULES" walking the
  model through quadrupling backslashes across four interpreter layers. When your
  system prompt needs an escaping tutorial to survive its own interface, the
  interface is the wrong shape. Use typed MCP tools.
- **`--dangerously-skip-permissions` unconditionally.** Claude runs unguarded
  against the host Linux machine. The approval dialog only gates two operations
  against the retro machine, and it is opt-in by prompt instruction: `session_id`
  is an optional field the model can simply omit, and the server does not enforce
  it. Do not inherit this threat model.
- **Hardcoded API key** duplicated in client and server source and embedded
  verbatim in the system prompt.
- **No session resume.** `--resume` is never passed. Reconnect gets a fresh Claude
  process with no memory.
- **No encoding handling at all.** Zero calls to MultiByteToWideChar or
  WideCharToMultiByte. Raw byte passthrough assuming 7-bit ASCII.
- **32KB fixed response buffer**, so files above that need a zip-bundle-then-
  manual-download dance the human has to drive.
- **Binary files corrupt on read**: content is treated as a NUL-terminated C
  string.
- No screenshots, no input injection, no window enumeration, no registry.
  Console REPL only.

### 2.2 retro-agent (voidsstr)

A different order of magnitude: **25,041 lines of C** in the Win32 agent alone,
across 41 files, plus a Linux port, a DOS real-mode port, and a Python client, all
speaking the same wire protocol. 618 commits. Zero stars.

Protocol (`agent/shared/frameproto.h`), four implementations kept in sync:

    [uint32 LE payload length][payload]
    response: [status_byte][payload]   0x00 text, 0x01 binary, 0xFF error
    first frame: "AUTH <secret>"
    TCP 9898 (primary), 9897 (alt), UDP 9899 (discovery)
    MAX_FRAME_SIZE 32MB, MAX_COMMAND_LEN 4096, AUTH_TIMEOUT_MS 10000

~70 verbs including EXEC/EXECW, SCREENSHOT/SCREENDIFF/CLICKSHOT, WINLIST, UICLICK/
UIDRAG/UIKEY, REGREAD/REGWRITE/REGDELETE, PCISCAN, PROCLIST/PROCKILL, SMARTINFO,
UPLOAD/DOWNLOAD, SHUTDOWN/REBOOT/RESTART.

Things worth stealing outright:

- **SCREENDIFF** sends only changed 64x64 tiles against the previous frame.
  **CLICKSHOT** composites click + settle delay + diff into one round trip. Both
  are better than the naive full-screenshot design and directly address the token
  and latency cost of GUI automation.
- **stdout and stderr both redirected to the same pipe handle**
  (`si.hStdOutput = si.hStdError = hWritePipe`), read with a non-blocking
  `PeekNamedPipe` + 200ms wait loop so a hung child never blocks the agent.
- **The watchdog**: a thread polling every 8s; if a handler has been in flight
  >75s and a known fullscreen game process is running, it kills the game and calls
  `ChangeDisplaySettingsA(NULL,0)` to release a stale Glide display lock.
- **RESTART writes a detached batch file** that sleeps via
  `ping -n 4 127.0.0.1` then relaunches, because nothing supervises the agent on
  Win9x and a bare QUIT takes the box off the network until someone walks over.
- **A PE import-table regression test** (`tests/python/test_agent_win9x_imports.py`)
  that inspects the built binary rather than grepping source, because a single
  NT-only static import kills the EXE at load with no log output.

Gaps, which are exactly your opportunity:

- No native GUI anywhere. `retro_chat.exe` is a console app using
  `SetConsoleTextAttribute` with a fixed 6-color palette. That console *is* the UI.
- No markdown rendering. The brain folds Claude's Unicode output down to plain
  ASCII before it hits the wire.
- Not Claude Code. Built on the Claude Agent SDK with `query()`, because Claude
  Code has no embeddable form.
- **No approval UI at all.** `permission_mode="bypassPermissions"` unconditionally,
  with the system prompt stating "the user cannot click approval dialogs, so just
  act." Compensating controls are server-side only: a PreToolUse hook and a
  `confirm=true` convention for destructive verbs.
- No power control, no VM snapshots. Recovering a wedged box is documented as
  sometimes requiring physical presence.
- One piece of the system (`retro_chat_daemon.py`) lives in a sibling repo and is
  not in this checkout, so it is not runnable standalone.

### 2.3 Adjacent

- **WinGPT** (dialup.net): Windows 3.1 native chat client, 218KB. Its TLS is a
  heavily modified Win16 port of **wolfSSL**, GPLv2, distributed as a 14MB zip. The
  author does not claim Win32 portability. TLS 1.2 primary, TLS 1.3 as one cipher
  suite.
- **Crypto Ancienne**: Unix-likes and classic Mac/Amiga/IRIX/AIX only. **No Win32
  target.** My earlier suggestion that it was an option here was wrong.
- **exo labs llama98.c**: local Llama inference on a Pentium II, Borland C++ 5.02.
  Different axis, but it is the comparison everyone will make.

---

## 3. Your ideas, assessed

### 3.1 "Native app on Win98 that talks to a modern machine" — SOUND

Both prior projects do it. Design points settled by their experience:

- **The 98 box dials out.** Both do this. Avoids inbound firewall and DHCP issues.
- **Length-prefixed binary framing over one socket** beats HTTP polling.
  retro-agent's `[uint32 LE len][payload]` is the right shape; claudewin9x's
  1-second HTTP poll wastes a second of latency on every interaction.
- **Threading is constrained on 9x.** retro-agent forces a single-threaded
  `select()` multiplex loop on Win9x, capped at 10 clients, because GCC's emulated
  TLS misbehaves in `CreateThread` threads. Consequence: long-polls must be clamped
  (they use 1000ms on 9x) or one client's wait starves the whole machine. They
  diagnosed exactly that on 2026-07-29 when a local chat client's 30-second poll
  made a Deskpro invisible to the LAN for 90 seconds.

### 3.2 "Modern machine proxies Claude Code" — SOUND, use the Agent SDK

Confirmed current invocation:

    claude -p "<prompt>" --input-format stream-json --output-format stream-json \
      --verbose --include-partial-messages

`--include-partial-messages` is what gives token-level `content_block_delta`
events; without it you only get whole blocks. Also relevant: `--bare` (skips
hook/MCP/CLAUDE.md auto-discovery, docs say it will become the `-p` default),
`--forward-subagent-text`, `--include-hook-events`.

Recommendation: build the bridge on the **Python or TypeScript Agent SDK** rather
than hand-parsing NDJSON. You get typed messages, `canUseTool`, in-process hooks,
`interrupt()`, and session helpers. The raw CLI is a supported surface, not a
side-channel, but the SDK is strictly more capable for a custom front-end.

Event types you will need to render: `system` (init, compact_boundary,
permission_denied), `assistant` (one per completed content block), `user` (tool
results), `stream_event` (the partial deltas), `result`.

Note: subagent output only arrives as complete messages tagged with
`parent_tool_use_id`. Token deltas are main-session only.

### 3.3 "MCP server for on-machine services" — SOUND, and the key architectural call

Do this, and specifically do NOT do what claudewin9x did. Typed MCP tools remove
the escaping problem entirely. Use `createSdkMcpServer()` for in-process tools so
there is no extra process to supervise.

Proposed tool surface, informed by retro-agent's verb list:

    win98_exec(command, timeout_ms, cwd)
    win98_screenshot(scale)          -> full/half/quarter
    win98_screendiff()               -> changed tiles only
    win98_clickshot(x, y, button, settle_ms)
    win98_list_windows()             -> the cheap semantic tree; prefer over screenshots
    win98_control_click(hwnd, ...)   -> survives window movement
    win98_key(keys) / win98_type(text)
    win98_registry_read/write
    win98_ini_read/write
    win98_processes / win98_kill
    host_snapshot / host_restore / host_reset    (bridge-side, not guest-side)

On files: my earlier advice stands. Do not put the filesystem behind RPC. Mount a
Samba share from the Linux box so Claude Code's native Read/Edit/Grep/Glob work at
full speed on modern storage. Confirmed viable in 2026 (see 4.4).

### 3.4 "Bridge handles interop: encodings, timeouts" — CORRECT, AND IT IS GENUINELY UNBUILT

Neither project has an encoding layer. This is the least-explored part of the
space and the one where you would add the most.

What they actually do:

- retro-agent uses the ANSI Win32 API family throughout, so everything is raw
  bytes in the box's local codepage. Its JSON escaper preserves bytes >= 0x80 as
  `\u00XX` (byte preservation, not decoding), and its Python client decodes text
  responses with `ascii`, `errors="replace"` — lossy. The brain folds Claude's
  Unicode down to ASCII with a hand-built map plus NFKD normalization before it
  hits the wire.
- claudewin9x does nothing at all.

The good news: **CP_UTF8 in MultiByteToWideChar/WideCharToMultiByte has been
supported since Windows 95.** The June 1998 MSDN archive documents it explicitly
with "Requires Windows 95 or later", excluding only Windows CE. Popular belief
that this is a Vista addition conflates it with `WC_ERR_INVALID_CHARS`, a strictness
flag added later. So the OS will do the conversion for you, though pre-Vista
Windows uses a lenient decoder, so hand-rolling remains cheap insurance.

The three conversions to keep distinct:

- Console output from spawned programs: OEM (CP437/CP850)
- File content, window titles, filenames: ANSI (CP1252 on a US install)
- The wire: UTF-8

Do the folding of display text on the Linux side where you have full Unicode
tables. Keep the RPC channel strictly UTF-8.

On timeouts, the Claude Code side is better than I expected:

- **`canUseTool` has no timeout.** The callback can stay pending indefinitely.
  A native Win98 approval dialog that waits while you make coffee is directly
  supported. After ~6 seconds pending, Claude Code fires a `Notification` hook
  (`permission_prompt` type) you can use to drive a "waiting" UI state.
- **`MCP_TOOL_TIMEOUT`** bounds how long a tool call may run before erroring out
  instead of hanging the agent loop. This closes the failure mode I was most
  worried about.
- If the GUI may be offline for a long time, a `PreToolUse` hook returning the
  `defer` decision lets the process exit and resume the call later from the
  persisted session.

### 3.5 "The Win98 machine may freeze and misbehave" — CORRECT, AND THIS IS THE REAL WORK

This is where retro-agent's 618 commits mostly went. Their hard-won findings, which
you would otherwise rediscover one at a time:

1. **`CreateThread(..., lpThreadId=NULL)` is rejected on Win95/98** with
   ERROR_INVALID_PARAMETER while NT accepts it. Every fire-and-forget helper thread
   in their agent silently never started on real Win98 for years.
2. **One NT-only static import kills the whole EXE at load**, before `main()`,
   with no log. There is no lazy binding. Hence their PE-import regression test.
3. **vcache exhaustion on >512MB RAM mimics a video driver bug.** Three different
   NVIDIA driver versions "failed" with NVCORE.VXD protection errors before anyone
   found the real cause. Fix: `MaxFileCache=262144` in `[vcache]`.
4. **Reading a file via `EXEC type`/`find` on Win9x can kill the agent.** A 300KB
   `find` over a single-threaded 165MHz/127MB agent did it twice in one day.
   Use a file-transfer verb, never EXEC, to read files.
5. **A successful TCP connect is not liveness.** Their alt listener kept accepting
   connections after the core handlers died, so connect-based health checks
   reported a dead box as healthy. Health check must be protocol-level PING.
6. **Win9x shutdown needs a message-pumping hidden window** to receive
   WM_QUERYENDSESSION, and the agent must not set its own exit flag during the
   negotiation or it cancels the shutdown it just requested.
7. **8bpp screenshots return coloured noise, not black**, because a fresh memory DC
   carries only the 20 static system colors and GetDIBits needs the live hardware
   palette selected in. Looks like evidence of a real bug, is not.
8. **DirectDraw exclusive-mode titles program the DAC directly** and can never be
   screenshotted via GDI. PrintScreen is the only path, and
   `MapVirtualKey(VK_SNAPSHOT)` returns 0 on some layouts so you need the hardcoded
   scan code 0x37 as fallback.
9. **Win98 has no `reg.exe`.** Registry edits go through hand-written REGEDIT4
   `.reg` files applied with `regedit /s`.
10. **Spaces in paths break LAUNCH** because the command line is built without
    quoting; the workaround in the field is typing 8.3 short names.

On the TCP RST question, the two sources disagree and both are worth knowing.
retro-agent documents in three separate places that an abrupt TCP disconnect
crashes Win98's Winsock and can take down the whole machine, and their Python
client always sends `SHUT_WR` plus a 100ms grace before closing. Their `main.c`
also skips `SO_RCVTIMEO` on Win9x with the comment that setting it crashes Win98
Winsock. Independent research found **no Microsoft-documented bug** matching this;
the closest documented Win9x stack crash is fragmented IGMP packets (MS99-034).

Treat it as real. Field evidence from a production fleet beats the absence of a
KB article, and the mitigation (always FIN before close) costs nothing.

**What nobody has built: out-of-band recovery.** Under QEMU, `screendump` and
`send-key` read and write the emulator's own device model, so they work while the
guest vCPU is wedged. You can screenshot a hung Win98, OCR the protection-error
text, feed it to Claude, then `system_reset` or `snapshot-load`. Neither prior
project has anything like this; retro-agent explicitly documents needing a person
at the keyboard sometimes.

---

## 4. Corrections to earlier assumptions in this conversation

- **Pipes on Win9x.** I said anonymous pipes were broken and temp files were the
  workaround. More precisely: the standard CreatePipe + STARTF_USESTDHANDLES +
  CreateProcess pattern works identically on 9x and NT for genuine **32-bit console
  children**. Only **16-bit** children break (KB Q150956: redirected ReadFile blocks
  forever). The trap is that COMMAND.COM, FIND, FC and SORT are themselves 16-bit.
  retro-agent pipes to `command.com /c` successfully, which I believe is because it
  polls with `PeekNamedPipe` rather than blocking in ReadFile. That reconciliation
  is inference, not a cited fact, and is worth verifying empirically early.
- **Threads on 9x.** I said Win32 threads are fine. They exist, but see 3.5 item 1,
  and GCC's `__thread` emulated TLS emits CMOV which faults on pre-Pentium-Pro
  hardware. retro-agent uses native `TlsAlloc`/`TlsGetValue` and forces
  single-threaded multiplexing on 9x.
- **CP_UTF8.** I said to hand-roll UTF-8 conversion because CP_UTF8 support on 98
  was doubtful. It is documented as supported since Win95. Hand-rolling is now
  optional rather than necessary.
- **TLS.** I said the SChannel ceiling forces a proxy. The ceiling is real (TLS 1.0,
  confirmed; TLS 1.1/1.2 landed in schannel.dll only with Windows 7 and were never
  backported to 9x), but it does not force a proxy: **OpenSSL 1.0.2u built with
  VS2005 gets TLS 1.2 running on unmodified Win98**, demonstrated by
  github.com/OmegaAOL/curl-windows98. I also cited Crypto Ancienne as an option
  for Win98; it has no Win32 target at all.
- **Toolchain.** I suggested mingw-w64 would be fine. It works (retro-agent ships
  with it) but only in a non-default configuration. Open Watcom 2.0 is the
  officially-Win98-targeting, actively-maintained option, and claudewin9x migrated
  to it.

---

## 5. Recommended architecture

    Win98 guest (QEMU or real iron)
      |
      +-- retro_gui.exe        native Win32 GUI, custom-drawn transcript
      |                        approval dialogs, tool activity pane
      +-- guest_agent.exe      exec, screen, input, registry, files
      |                        (or one binary doing both)
      |
      |  [uint32 LE len][type][payload] over one TCP socket, guest dials out
      |
    Linux bridge
      +-- Agent SDK query() driving Claude Code
      +-- in-process MCP server exposing win98_* tools
      +-- canUseTool -> remote approval dialog on the guest
      +-- encoding fold: Unicode -> CP437/CP1252 for display, UTF-8 for RPC
      +-- watchdog: PING heartbeat, HEALTHY/SLOW/UNRESPONSIVE/DEAD
      +-- QMP: screendump, send-key, snapshot-save/load, system_reset
      +-- Samba share -> the project lives here, guest mounts it as P:\

Two logical channels over the one socket, with different drop policies:

- **Display** (bridge -> guest): droppable and coalescable. Coalesce streaming text
  to 10-15 fps; at 100 tok/s you cannot repaint per token on period hardware.
- **RPC**: never dropped, every request carries a deadline.

If they share one undifferentiated stream, a slow repaint applies TCP backpressure
to the bridge, which stalls the writer, which blocks the tool call.

### Verified on this host (Fedora 44, 2026-09-16)

- mingw32-gcc 16.1.1 available in repos (not installed)
- QEMU 10.2.2 with pc-i440fx, ne2k_pci/pcnet/rtl8139, cirrus-vga, sb16/ES1370
- VirtualBox present; 86Box and PCem absent
- Samba client 4.24.6 installed, server packaged
- kernel 7.1.13 has CONFIG_CIFS_ALLOW_INSECURE_LEGACY=y, `disable_legacy_dialects`
  defaults to N, so `vers=1.0` works out of the box
- claude CLI 2.1.273, .NET 10.0.111 (claudewin9x's server would build as-is)

### SMB settings needed on the Samba side

    server min protocol = NT1
    ntlm auth = ntlmv1-permitted
    lanman auth = yes

All three still present and functional in current Samba (4.24.x). Caveat:
`lanman auth` has carried a "will be removed in a future release" deprecation
since Samba 4.11 with no removal after multiple years. Monitor it.

### QEMU config notes

Use **98 SE, not first edition** (the CPU-speed calibration crash was largely
fixed in SE). Keep ACPI **enabled** and install with `setup /p j`; the shutdown
hang is a broken APM negotiation, and the fix is the opposite of disabling ACPI.
i440fx not q35. PCnet is the most consistently-reported working NIC. SB16 works
driverless from the Win98 CD but is PCM-only. `snapshot-save`/`snapshot-load` are
QMP-native since QEMU 6.0; `savevm`/`loadvm` were HMP-only.

---

## 6. Risk register

| Risk | Severity | Mitigation |
|---|---|---|
| GDI/USER 16-bit heaps are 64KB fixed, not scaled by RAM | High | One cached font, one memory DC, reuse forever. Leaks clear only on reboot. |
| Undiscovered 9x landmines cost days each | High | Fork retro-agent rather than reimplement; mine its CLAUDE.md (170KB gotcha ledger) |
| Custom GUI transcript rendering is the bulk of the work | High | Do not use an edit control; EM_REPLACESEL reflow is quadratic |
| `lanman auth` removed from Samba mid-project | Medium | Fallback: Linux mounts the guest's share instead (also verified working) |
| Win9x single-threaded multiplex starves under long-polls | Medium | Clamp long-polls to ~1s on 9x, as retro-agent does |
| 16-bit console tools (FIND, SORT, FC) hang pipe reads | Medium | PeekNamedPipe polling; prefer 32-bit tools; never EXEC to read files |
| Real hardware has no remote recovery | Medium | Develop against QEMU first; keep the guest agent hardware-agnostic |
| Emoji/box-drawing/smart quotes in Claude output | Low | Fold on the Linux side with a transliteration map |

---

## 7. Effort estimate

Grounded in the two prior projects.

| Piece | Lines | Time | Notes |
|---|---|---|---|
| Bridge (Python, Agent SDK + MCP + QMP) | 1,500-2,500 | 1-2 weeks | Highest leverage, easiest work |
| Guest agent (exec, screen, input, registry) | 4,000-6,000 | 3-4 weeks from scratch | Or fork retro-agent and cut it down |
| Native GUI client | 4,000-7,000 | 4-8 weeks | The unclaimed part; also the hard part |
| Encoding + hang handling | ongoing | forever | |

Polished v1: **3-4 months part-time.** Ugly working spike, forking retro-agent's
agent and using the Agent SDK: **2-3 weekends**, and worth doing first.

For comparison: claudewin9x is ~5,100 hand-written lines for a console client with
no screen, input or registry. retro-agent is 25,041 lines of C for the agent alone,
built over 618 commits and a real fleet.

---

## 8. Open questions to settle empirically, before committing

1. Does Fedora's mingw32-gcc 16.1.1 still produce a loadable Win98 binary? Test
   with `-march=i586 -D__USE_MINGW_ANSI_STDIO=0 -Wl,--subsystem,console:4.0
   -Wl,--major-os-version,4`. If not, Open Watcom 2.0 is the fallback and is
   officially supported. retro-agent also needs a patched `libmsvcrt.a` with
   `_strtoui64`/`_strtoi64` removed plus a `compat.c` linked first.
2. Do pipes to a 16-bit child (COMMAND.COM) actually work with PeekNamedPipe
   polling, or did retro-agent get lucky? This determines the whole exec design.
3. How long does a full-screen BitBlt + DIB encode actually take at 800x600 on the
   target hardware? Decides whether SCREENDIFF is mandatory or an optimization.
4. Is the Win98 Winsock RST crash reproducible? Cheap to test under QEMU, and the
   mitigation is free either way.
5. Is the machine going to be the terminal you sit at, the workspace being
   operated on, or both? Both is the fun answer, but if the goal is mainly the
   aesthetic of Claude Code in a Win98 window, the exec/screen/input surface
   becomes optional and the project shrinks by more than half.

---

## 9. What is genuinely unclaimed

Nobody has built a native Win98 **GUI** client for Claude Code.

- claudewin9x proxies real Claude Code but is a console REPL with slash commands.
- retro-agent has the complete guest tool surface and a serious protocol, but the
  Win98 side is a 16-color console app, and it drives the Agent SDK rather than
  Claude Code.
- WinGPT is a native GUI with no agency, on Win3.1, against a different API.

The combination that does not exist: comctl32 chrome, a custom-drawn streaming
transcript, a tool-activity pane, permission requests as real MS Sans Serif modal
dialogs, and the same machine being both the terminal you sit at and the workspace
being operated on, with snapshot-and-reset recovery when it wedges.

---

## 10. GUI toolkit decision (added 2026-09-16)

### The WYSIWYG objection is moot

Win32 `.rc` resource scripts are a documented plain-text format. `windres`
(mingw-w64) and `wrc` (Open Watcom) both compile them from the command line with
no GUI involved. Hand-authoring DIALOG resources in dialog units is how most
pre-2000 Win32 software was actually written. Practical tip: start from an
existing dialog's coordinates rather than computing dialog units from scratch,
and keep control grids on simple aligned increments.

Free standalone editors exist if wanted: ResEdit (edits .rc source, but last
release v1.6.6 dated 2015-05-09, likely abandoned) and Resource Hacker (actively
distributed, but edits compiled binaries, not source). Neither is needed.

### Options surveyed

| Option | Last Win9x-capable version | Native controls | Cross-compiles from Linux |
|---|---|---|---|
| Raw Win32 + comctl32 | n/a | by definition | yes |
| Win32++ | 9.6.1 (2022-08-07) | yes, thin wrapper | likely (header-only), untested |
| wxWidgets | 2.8.12 (2011-03-28) | yes, wraps comctl32 | yes, needs header patches |
| Lazarus / FPC LCL | unresolved | yes | yes |
| WTL | unconfirmed | yes | no (ATL unavailable on mingw) |
| MFC | fine | yes | no (MSVC-bound) |
| FLTK | 1.0.x | NO, self-drawn | yes |
| IUP | XP+ only | yes | n/a |
| FOX, Nuklear, libui | various | NO / Vista+ | n/a |

Notes:
- Win32++ **10.0** (2022-09-13) dropped Win95-2000. The 9.6.1 line has been
  frozen since 2022.
- wxWidgets 2.8 predates mingw-w64 by a year or two; documented header
  mismatches (e.g. `TreeView_GetItemRect`) need manual patching. Static minimal
  app lands around 2-3MB.
- FPC bug #27721: the RTL emits wide (W-suffix) Win32 API calls absent on
  ANSI-only Win9x, so binaries can fail to load. `-dWIN9XPLATFORM` exists but its
  documented purpose is making the Lazarus IDE itself run on Win9x, not fixing
  compiled apps. Unproven either way; needs a build spike.
- FLTK draws its own widgets by design, so it cannot look period-correct. Out
  independent of version.

### Decision: raw Win32, in C

Rationale:

1. **The transcript view must be custom-drawn regardless.** An edit control's
   `EM_REPLACESEL` reflow is quadratic and will crawl on a long session. The hard
   part is hand-drawn either way, so a toolkit only covers the easy part: main
   window, input box, listview, status bar, menus, a few modal dialogs. That is a
   few hundred lines of `CreateWindowEx` plus an `.rc`.
2. **Every candidate dependency is frozen or dead.** Toolkit-compatibility risk
   is the dominant risk in this project. Raw Win32 has none of it.
3. **It matches the prior art.** Both retro-agent and claudewin9x are plain C
   with no toolkit. Forking retro-agent's guest agent keeps everything in one
   language.
4. **The 64KB USER/GDI heaps are the real constraint**, not ergonomics. They are
   fixed, do not scale with RAM, and leaked objects clear only on reboot. Thin
   layers help; wxWidgets creating more windows and GDI objects per widget does
   not.

Optional: smoke-test Win32++ 9.6.1 early as a convenience layer. Header-only, so
the switching cost if it breaks is near zero.

Rejected: VC++ 6 as the primary toolchain. It is Windows-hosted (Wine or the
guest VM), giving a poor edit-compile-test loop, and buys nothing over mingw-w64
or Open Watcom plus the February 2003 Platform SDK.

### On headers and libs (answering "don't we need Win98 headers?")

No separate Win98 SDK is needed with mingw-w64. It ships one set of headers and
import libraries covering the whole Win32 surface; the target era is selected by
macro:

    -DWINVER=0x0410 -D_WIN32_WINNT=0x0410     # 0x0410 = Win98, 0x0400 = Win95/NT4

That gates header *declarations* only. The import libraries are the union of all
exports across all Windows versions, so the linker will happily resolve an
NT-only symbol and hand you a binary that dies at load on Win98 with no error and
no log (there is no lazy binding). Guarding against that needs all five of:

1. Macro gating as above
2. `-Wl,--subsystem,windows:4.0 -Wl,--major-os-version,4 -Wl,--minor-os-version,0`
3. `-march=i586 -D__USE_MINGW_ANSI_STDIO=0` to keep CMOV out of the image
4. A patched `libmsvcrt.a` with `_strtoui64`/`_strtoi64` removed, placed ahead of
   the system one via `-L`, plus a `compat.c` linked FIRST supplying those and
   no-op `EncodePointer`/`DecodePointer`
5. A regression test that inspects the built binary's PE import table

Open Watcom 2.0 ships its own headers and libs and officially targets Win95/98/ME,
so most of the above disappears. It is the fallback if mingw-w64 16.x turns out to
have dropped too much.

---

## 11. Toolchain resolved: Open Watcom 2.0 (verified 2026-09-16)

Open Watcom 2.0 beta, build dated **2026-09-14** (two days old), installed at
`/usr/bin/watcom`, Linux x64 host binaries in `binl64`. Open question #1 from
section 8 is answered: we have a working Win98-targeting cross-compiler on Linux,
and it did not need mingw-w64 at all.

### Result of the smoke test (`spike/hello98.c`, a minimal Win32 GUI app)

    file:            PE32 executable for MS Windows 4.00 (GUI), Intel i386
    OS version:      4.0        <- the Win95/98/NT4 gate
    Subsystem ver:   4.0
    Size:            22,016 bytes, statically linked
    Imports:         45 symbols across KERNEL32, GDI32, USER32 only
    CMOV count:      0
    Compile:         clean, first try, no patches, no warnings

### The significant finding: no CRT DLL dependency

The binary imports **nothing from MSVCRT.DLL**. Open Watcom statically links its
own C runtime. That single fact removes the entire class of problems documented
in sections 4 and 8:

- No `_strtoui64`/`_strtoi64` absence (the bug that broke claudewin9x on
  mingw-w64 < 12.0.0 and forced its migration to Open Watcom in January)
- No patched `libmsvcrt.a` needed (retro-agent ships one)
- No `compat.c` shims for `EncodePointer`/`DecodePointer`
- No `-D__USE_MINGW_ANSI_STDIO=0` dance to keep CMOV out of MinGW's own printf

Section 8's five-part mingw checklist collapses to two: `-bt=nt -5` on the
compiler, and `system nt_win option osversion=4.0` on the linker.

### Two W-suffix imports, and why they are probably fine

`GetCommandLineW` and `GetModuleFileNameW` appear in the import table. Both ARE
exported by Win98's kernel32 (as stubs returning failure), so the EXE will load;
the W-function absence problem on Win9x is about behaviour, not about the loader.
`GetVersion` is also imported, which suggests the OW CRT version-checks before
using them. **Not yet verified by running on Win98.** This is the one thing to
confirm in a VM before building on it.

### Build gotcha worth recording

`wlink` searches **PATH**, not `$WATCOM`, for its `wlink.lnk` config, and that
file is what defines the `nt_win` system. Invoking `wlink` by absolute path
without `$WATCOM/binl64` on PATH fails with:

    Warning! W1107: undefined system name: nt_win
    Warning! W1008: cannot open clib3r.lib
    Error! E2028: __imp__CreateWindowExA@48 is an undefined reference   (x17)

which looks like a missing-libraries problem and is actually a missing-config
problem. Both `export WATCOM` and `export PATH := $(WATCOM)/binl64:...` are
required in the Makefile.

### Testing strategy: Wine and QEMU answer different questions

Wine is not installed (available as wine 11.0-3.fc44). It is worth installing,
but understand what it does and does not prove:

- **Wine tells you whether the code is correct.** Instant feedback, no VM boot,
  good for the inner loop on layout, message handling and protocol logic.
- **Wine does NOT tell you whether it runs on Windows 98.** Wine implements a
  modern Win32 surface. Setting the version to "Windows 98" in winecfg changes
  what `GetVersion` reports and toggles some behaviours, but Wine still fully
  implements W-functions that real Win98 only stubs, and its comctl32 is a
  reimplementation with different quirks and a non-period look. A binary that
  runs perfectly under Wine-set-to-98 can still fail on real Win98.
- Concretely: the two W-suffix imports above will work under Wine and are stubs
  on real Win98. Wine will never surface that difference.

Three-tier loop, cheapest gate first:

1. `make check` - static PE audit (version gate, import table, CMOV). Seconds.
2. Wine - does it run and behave. Seconds.
3. QEMU Win98 SE guest - does it actually work on the target. Minutes.

Only tier 3 is authoritative. Tiers 1 and 2 exist to keep you out of tier 3.

---

## 12. Wine tier working, zero new dependencies (2026-09-16)

Section 11 said Wine was not installed. It effectively was, via Lutris. No system
packages were added.

Reused: `~/.local/share/lutris/runners/wine/wine-ge-8-26-x86_64`, which is
wine-8.0 Staging — a standalone Wine build, not Proton, with full 32-bit support
(`lib/wine/i386-unix` and `i386-windows`). That last part is what a `win32`
prefix needs and what Proton's `bin-wow64` builds would have complicated.

Also available on this box if that build ever breaks: GE-Proton10-32/33/34 under
`~/.local/share/Steam/compatibilitytools.d/`, each with both `bin/wine` and
`bin-wow64/wine`.

Result:

    prefix:   spike/.wineprefix (project-local, disposable, 221MB)
    reports:  HKLM\...\CurrentVersion\VersionNumber = 4.10.2222
    run:      survived 6s in its message loop, window drawn
    cost:     0 new packages

`4.10.2222` is the real Windows 98 SE build number. Worth repeating that this is
a reported version string, not a restricted API surface — Wine still implements
W-functions that real Win98 stubs.

### Make targets now

    make ow           build with Open Watcom          [verified]
    make mingw        build with mingw-w64            [untested, likely unnecessary]
    make check        static PE audit                 [verified]
    make wine         run interactively under Wine    [verified]
    make wine-smoke   non-interactive, alive after 6s == pass  [verified]
    make wine-clean   delete the prefix
    make clean

`WINEDIR` is overridable, so switching to a different Wine build is a one-flag
change.

### Where this leaves the three tiers

Tiers 1 and 2 are both live and take seconds. Tier 3 (a real Win98 SE guest under
QEMU) is the only one still missing, and it is the only authoritative one. It
needs an install source.

Still outstanding, and the highest-value next step: dump the export tables from a
real Win98 `KERNEL32.DLL`, `USER32.DLL`, `GDI32.DLL` and `COMCTL32.DLL` and check
them in as a reference allowlist. That upgrades `make check` from "print the
imports for a human to eyeball" into a hard build gate that fails on any symbol
Win98 does not export. Strictly better than retro-agent's equivalent test, which
greps a known-bad list and can only catch symbols someone already got burned by.
