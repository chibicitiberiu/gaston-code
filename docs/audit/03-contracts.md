# 03 — Contracts across the C/Python boundary
#
# Audit date 2026-09-18. The original report is preserved below as
# context. Findings are the lines at the end: one per finding,
# prefixed "Fixed:" once the thing it describes has been dealt with.
#
## 03 — Contracts across the C/Python boundary
#
# Audit date 2026-09-18. Scope: everything that is written twice because it has
# to exist in both `win/` and `bridge/`, plus the consistency of conventions
# across the whole tree. Nothing here was fixed; this is a report.
#
# Method: every claim below was read on both sides. Where a test already covers
# a contract it is named; where nothing covers it, that is stated as a fact
# rather than an impression.
#
# ---
#
### Top findings
#
# 1. **The manifest row format is written four times and verified zero times.**
#    `win/syncrpc.c:68` produces it, `bridge/server.py:1657-1690` parses it,
#    `tests/python/test_sync_wire.py:29-47` re-implements the producer for the
#    tests, and `tools/syncprobe.py:83` re-implements the parser. `synccheck.exe`
#    — the only thing that runs the real C against the real Python — prints a
#    *different, tab-separated* format of its own (`win/synccheck.c:26`), so it
#    proves the classifier and the checksums and never touches the wire format.
#    A field added on one side passes the whole suite.
#
# 2. **`RC_CRC_FOR` says the rule must live in one place; Python writes it four
#    times.** `win/sync.h:59` defines the macro with the comment *"One place,
#    because getting it wrong in one of the four call sites is a permanent
#    disagreement, not a glitch."* The bridge open-codes exactly that expression
#    at `bridge/sync.py:125`, `bridge/server.py:1686`, `bridge/syncrun.py:293`
#    and `bridge/syncrun.py:462`. Four call sites, no macro, no helper.
#
# 3. **`rc_sync_contained` does not reject a backslash, and it is the check that
#    exists for the case where the bridge is wrong.** `win/sync.h:79` states the
#    reason: *"the bridge checking is not enough when the bridge is what might be
#    wrong."* `win/sync.c:209-229` splits on `/` only, so the single component
#    `a\..\..\evil.txt` has length 16, is neither `.` nor `..`, and passes. `join`
#    (`win/sync.c:232`) then produces `C:\WORK\a\..\..\evil.txt`, which Win32
#    resolves outside the root. The bridge's `_ILLEGAL_ALWAYS` (`paths.py:72`)
#    does contain `"\\"` — which is precisely the containment the guest check is
#    supposed to be independent of.
#
# 4. **The guest hand-builds HTML with CSS class names it shares with the bridge,
#    and one path does not escape.** `win/main.c` emits `<div class="notice">`
#    (589, 784, 876), `<div class="you">` (1053) and `<div class="err">` (1077,
#    1099) — the same three classes `bridge/html.py` produces at 103/109/114/118.
#    Renaming a class on the bridge silently unstyles the guest's copies. Worse,
#    the `ERROR` branch (`win/main.c:862-868`) formats `[code] msg` and calls
#    `rc_html_append` **with no escaping**, while the adjacent `NOTICE` branch
#    escapes. `code` and `msg` come from the bridge and include guest-controlled
#    text (`error("unknown_type", f.type)`, `server.py:1032`) and exception
#    strings (`error("bad_caps", str(exc)[:200])`, `server.py:387`).
#
# 5. **Three frames the bridge sends are unhandled by the shipping guest.**
#    `TOOLRESULT` (`server.py:480`) — so tool results never appear in the Win98
#    transcript at all; `RESYNC` (`server.py:428`); `PONG` (`server.py:1003`).
#    All three land in `render_frame`'s `rc_log("unhandled frame type: %s")`
#    (`win/main.c:882`). `bridge/fakegui.py` renders all three, which is why it
#    has never been noticed.
#
# 6. **Three frames the bridge handles are never sent by the guest**: `STAT`,
#    `PING`, `GETOPTIONS` (`server.py:988`, `1002`, `1008`). `STAT` is the one
#    that matters: `PLAN.md:237` makes adaptive codec selection depend on it
#    ("*the sender picks the codec from the measured link speed*"), and
#    `should_compress` reads `guest.link_bps` which, absent `STAT`, is never
#    anything but the `--link-bps` CLI default.
#
# 7. **`protocol.py`'s frame constructors are a stale fifth declaration of the
#    vocabulary.** The module says they exist *"so the guest and bridge agree on
#    one flat vocabulary that is easy to grep for"* (`protocol.py:185`). `text()`
#    emits `style=` and `page=`; the shipping bridge builds `TEXT` inline at
#    `server.py:842/862` with `mode=`/`enc=`/`page=` and no `style=`. The guest
#    reads `mode` (`main.c:611`) and never `style`. `fakegui.py:119` reads
#    `style` and never `mode`, so fakegui renders every thinking block as plain
#    text. `status()` builds a `STATUS` frame nothing sends. `user_input()` and
#    `interrupt()` are unused even by fakegui, which inlines both.
#
# 8. **`sync_root` is read from HELLO and never sent.** `server.py:503` does
#    `guest.hello.get("sync_root")` to name the mirrored folder in the system
#    prompt. `win/net.c:370-479` sends 22 HELLO headers and `sync_root` is not
#    one of them, although `rc_sync_root()` is known before the socket opens.
#    The fallback `self.guest_sync_root` is only assigned after a sync pass
#    completes (`server.py:1226`), so the first session's prompt says *"the
#    machine's sync folder"* (`machine.py:58`) instead of the path — the exact
#    failure the comment above it warns about.
#
# 9. **Replaying a compressed frame after a stream reset would break the link.**
#    `_send` stores the already-deflated `Frame` in the replay buffer
#    (`server.py:2127`), and `_attach` resets `zip_out` on reconnect
#    (`server.py:393`) *before* `replay.since()` re-sends those frames verbatim
#    with `enc=deflate` bodies produced by the old stream. The guest's fresh
#    inflater would fail and `net.c:167` drops the link. Latent only because the
#    guest never sends `from=` — which BACKLOG §5 already records as dead code,
#    but not this consequence of un-retiring it.
#
# 10. **`.gaston-tmp` / `.gaston-old` is spelled in eight places across two
#     languages, and the two C macros that exist are used once.**
#     `RC_SYNC_TMP_SUFFIX`/`RC_SYNC_OLD_SUFFIX` (`sync.h:93-94`) are referenced
#     only by `rc_sync_is_working_name` (`sync.c:252-253`); `sync.c:401`, `:459`,
#     `:503` and `:515` hardcode the literals, and four sites hardcode the
#     length `11` rather than deriving it. Python has three spellings:
#     `syncrun.WORKING_SUFFIXES` (36), an inline literal at `syncrun.py:449`, and
#     the glob form `ignores.ALWAYS` (46).
#
# 11. **The cross-language agreement tests skip silently, and the documented
#     guest audit is unreachable.** Both Wine-driven suites gate on
#     `pytest.mark.skipif(not os.path.exists(EXE), ...)`, `win/Makefile` has no
#     `test` target that builds those binaries, and root `Makefile:19` points
#     `check` at `spike/` rather than `win/` — so `CLAUDE.md`'s *"`make check`
#     audits imports"* names a command that audits the Phase-0 hello-world. A
#     green `make test` on a fresh checkout has verified nothing across the
#     boundary.
#
# ---
#
### 1. Constants defined twice
#
#### 1a. Enforced at runtime — drift is caught
#
# | Constant | C | Python | Detector | What drift does |
# |---|---|---|---|---|
# | Protocol version | `RC_PROTO_VERSION 1` (`frameproto.h:13`) | `PROTO_VERSION = 1` (`protocol.py:28`) | **Handshake.** `server.py:342` refuses `ERROR bad_version` | Connection refused with a named reason. Safe. |
# | Frame cap | `RC_MAX_FRAME (64*1024)` (`frameproto.h:17`) | `MAX_FRAME = 64*1024` (`protocol.py:32`) | **Handshake.** Guest asserts it in `max_frame=`; `server.py:349` refuses `ERROR bad_frame_cap` | Refused. This is the model every other constant here should copy. |
# | Capability enum *values* | `RC_CASE_*`, `RC_DEVICES_*`, `RC_EOL_*`, … (`caps.h:26-43`) | `CASE_*`, `DEVICES_*`, `EOL_*`, … (`caps.py:73-95`) | **Partial.** `caps._enum` refuses a value not in its allowed tuple | A *renumbering* is caught (refused). A *re-meaning* — same number, different semantics — is not: the bridge accepts `eol=2` as whatever `EOL_CR` means to it. |
#
#### 1b. Cross-tested against the real binary
#
# | Constant | C | Python | Test |
# |---|---|---|---|
# | Content class letters | `RC_CLS_TEXT 't'` / `RC_CLS_EXACT 'b'` (`sync.h:14-15`) | `CLASS_TEXT = "t"` / `CLASS_EXACT = "b"` (`paths.py:274-275`) | `test_guest_agreement.py::test_class_matches_the_bridge` — runs `synccheck.exe` over a 20-file corpus and compares the letter. |
# | Classifier rules | `scan_class` (`sync.c:83-89`) | `classify` (`paths.py:278`) | Same test. Corpus covers NUL-anywhere, lone CR, `\r\r\n`, mixed, chunk-boundary carry. |
# | Normalisation (CRLF→LF) | `scan_chunk` (`sync.c:43-81`) | `normalise` (`paths.py:311`) | `test_crc_matches_the_bridge` + `test_both_checksums_match_the_bridge`. |
# | Working-name reserve | `RC_SYNC_WORK_SUFFIX 11` (`sync.h:37`) | `WORKRESERVE_GASTON = 11` (`caps.py:123`) | `test_the_guest_refuses_a_path_with_no_room_for_its_temp_name` — reads the boundary out of `synccheck --limits` and compares against `caps.Caps().maxpath - C.WORKRESERVE_GASTON`. **This is the only numeric constant in the project verified by executing both implementations.** |
# | Working names are hidden from the manifest | `rc_sync_is_working_name` (`sync.c:249`) | `WORKING_SUFFIXES` (`syncrun.py:36`), `ignores.ALWAYS` (46) | Guest side: `test_the_guest_scanner_hides_its_own_working_files`. Bridge side: `test_sync_wire.py:598`, `test_ignores.py:77`. Both sides tested, never against each other. |
#
#### 1c. Written twice, agreement asserted only in a comment
#
# | Constant | C | Python | If they drift |
# |---|---|---|---|
# | Working-name suffix strings | `".gaston-tmp"` / `".gaston-old"` — `sync.h:93-94`, hardcoded again at `sync.c:401, 459, 503, 515` | `syncrun.py:36`, `syncrun.py:449`, `ignores.py:46` | Whichever side is not updated stops recognising the other's temp files. The scanner reports a half-written file, the bridge has never seen that path, so it pulls it — and both sides hold a permanent `x.md.gaston-tmp`. `sync.h:90-92` records this happening for real. Nothing detects the strings themselves; the tests hardcode them a ninth and tenth time. |
# | The reserve, in two headers in one directory | `RC_SYNC_WORK_SUFFIX 11` (`sync.h:37`) and `RC_WORKRESERVE_GASTON 11` (`caps.h:60`) | `WORKRESERVE_GASTON = 11` (`caps.py:123`) | `caps.h:54-56` cross-references `sync.h` in prose. No `static assert`, no build check. If `sync.h` grew to 12 and `caps.h` stayed at 11, the guest would refuse paths the bridge happily pushes — reported as `too_long` per file, forever. |
# | Sync chunk size | `RC_SYNC_CHUNK 8192` (`sync.h:19`) | `CHUNK = 8192` (`syncrun.py:29`); `tools/syncprobe.py:37` | Not actually required to agree — both sides stream — but the guest's *scan* buffer is the same macro, and `test_guest_agreement.py`'s `chunk_boundary` corpus entries are hardcoded to 8191/8190. Change `RC_SYNC_CHUNK` and the carry-logic tests stop testing the boundary while still passing. |
# | Exec timeout floor / default / ceiling | `1000` / `20000` / `120000` (`rpc.c:174-176`) | `DEFAULT_TIMEOUT_MS = 20_000`, `MAX_TIMEOUT_MS = 120_000`, floor `1000` (`tools.py:29-30, 101`) | `tools.py:28` says *"The guest clamps this too; a tool argument is a suggestion, not a promise."* Nothing tests it. If the guest's ceiling dropped, the tool description still promises 120 s, and `budget = timeout_ms/1000 + 30` (`server.py:1166`) still sizes itself from the bridge's number. The ordering rule in `PLAN.md:764` (guest < bridge < MCP `timeout: 300_000`) is a three-way constraint held by comments alone. |
# | Exec output cap | `EXEC_MAX_OUT (32u*1024u)` (`exec.c:38`) | `EXEC_OUT_MAX = 64 * 1024` (`server.py:1742`) | Deliberately different, and the message is **wrong**: `server.py:1806` always says *"Output was capped at 32KB and truncated."* even when it was the bridge's 64 KB cap that cut it. `tools.py:56` also tells the model 32 KB. |
# | Exec command cap | `EXEC_MAX_CMD 1024` (`exec.c:41`) | prose only: *"about 1000 characters"* (`server.py:1785`) | The bridge never checks length before sending; the guest refuses with `too_long`. One-sided by design, but the number in the model-facing message is a hand-copy. |
# | Manifest type letters | `'f'`, `'d'`, `'u'` (`sync.h:40`) | `Type.FILE/DIR/UNREADABLE` (`sync.py:59-64`) | A new letter from the guest raises `ValueError` inside `server.py:1671`'s `except (ValueError, IndexError)`, which logs `unparseable manifest line` and **drops the row**. The `len(rows) != files` guard (`syncrun.py:139`) then aborts the pass loudly, which is the right outcome by accident rather than by design. `test_guest_agreement` discards the type column entirely. |
# | Manifest version | `RC_CAP_MANIFEST 2` (`caps.h:86`) | `manifest: int = 1` default, `_num(..., 1, 1, 2)` (`caps.py:151, 248`) | Negotiated at the handshake and refused outside 1..2. Good. But the *default* is 1, so any client that does not declare — including `bridge/fakegui.py` — is parsed as v1. |
# | Win9x default rules, written **three** times | `RC_CAP_*` block (`caps.h:65-74`) | `Caps` dataclass defaults (`caps.py:130-151`) **and** `paths._Default` (`paths.py:75-99`) | `_Default` exists to dodge an import cycle (`paths.py:76-78`) and duplicates `case=1, devices=1, trailing=1, illegal=1, eol=1, dirreserve=12, workreserve=11, maxpath=260, maxcomp=255`. Nothing asserts `_Default` matches `Caps()`. A change to one is silent in the other. |
# | Path limits, a fourth copy | — | `paths.MAX_PATH = 260`, `MAX_DIR_PATH = MAX_PATH - 12`, `MAX_COMPONENT = 255` (`paths.py:34-48`) | **These three have no consumers.** Every rule reads `caps`. They carry the most authoritative-looking comments in the file and editing them changes nothing. |
# | HTML container ids | `find_id(v->doc, "t")` / `"l"` (`htmlview.c:373-374`) | `SKELETON` (`html.py:391`) | A typo on either side leaves `slot_t`/`slot_l` NULL and the transcript renders nothing, with no error. |
# | HTML mode strings | `strcmp(mode, "live")` (`main.c:614`) | `APPEND = "append"`, `LIVE = "live"` (`html.py:29-30`) | `"append"` is never matched by name — it is the guest's else-branch. A typo in `LIVE` makes every live update append instead of replace, so the transcript grows a copy of the answer per token on a 32 MB box. |
# | CSS class names | `"notice"`, `"you"`, `"err"` in `main.c:589, 784, 876, 1053, 1077, 1099` | `html.py:103, 109, 114, 118` + the stylesheet | Silent unstyling. No test. |
# | Transcript escaping | `esc_html` (`main.c:1011`) | `html.esc` (`html.py:63`) | They behave differently on purpose (`esc_html` turns `\n` into `<br>` and drops `\r`; `_html.escape(quote=False)` does not) but both write into the same document, so "what is escaped here" has two answers depending on which side produced the block. |
#
#### 1d. One-sided limits the other side cannot see
#
# | Constant | Side | Consequence |
# |---|---|---|
# | `RC_SYNC_MAX_FILES 2000` (`sync.h:18`) | guest | Reported as `SYNCEND status=too_many`, which the bridge turns into the generic `SyncError(f"guest scan returned {status}")` (`syncrun.py:137`). The number is never communicated, so neither the user nor the agent learns *how many* is too many. |
# | `RC_SYNC_MAX_DEPTH 16` (`sync.h:17`) | guest | Silent. `walk` just returns (`sync.c:265`). A tree deeper than 16 has its lower levels reported as absent, which the bridge reads as deletions — and deletions do not propagate (BACKLOG §4), so it reads as "bridge-only", i.e. a push. The pass will re-push the same subtree forever. |
# | `MAX_FILE = 1024*1024` (`syncrun.py:46`) | bridge | Reported through `reports`, visible via `sync_status`. Fine. |
# | `RC_MAX_INFLIGHT 256` (`net.c:19`), `RC_OUTQ_MAX_BYTES` (`net.h:59`) | guest | Backpressure only; no wire effect. BACKLOG §1 covers the soft-cap behaviour. |
# | `Replay.max_frames = 20_000` (`replay.py:33`) | bridge | Unreachable — the guest never resumes. |
# | `RC_MAX_VAL 256` (`frameproto.h:22`) | guest | **Silent truncation of any header value over 255 bytes** (`frameproto.c:82`). The bridge has no corresponding limit and no check. This is why `command` and the sync path travel in bodies (`server.py:1151`, `syncrpc.c:210`) — but `cwd` still does not (BACKLOG §3, §5). |
# | `RC_MAX_HEADERS 16` (`frameproto.h:21`) | guest | Headers past the sixteenth are **parsed and discarded** (`frameproto.c:76`), not an error. `HELLO` is guest→bridge so it is unaffected, but a bridge frame that grows a seventeenth header loses one silently. `SESSION` already carries seven; `TEXT` carries five plus `seq`. |
#
# ---
#
### 2. Frame inventory
#
#### 2a. Bridge → guest
#
# | Frame | Sent at | Guest handler | fakegui | Notes |
# |---|---|---|---|---|
# | `READY` | `server.py:413` | `main.c:575` | yes (113) | Guest reads `sess`, `next_seq`, `page`, `total`; ignores `ver`, `model` except for the log. |
# | `RESYNC` | `server.py:428` | **none** | yes (150) | Guest logs `unhandled frame type`. Unreachable today (needs `from=`). |
# | `DOC` | `server.py:439` | `main.c:592` | no (generic dump) | Guarded by `g_doc_sent`, set once per process — BACKLOG §5 records that this makes reconnect keep a stale transcript. |
# | `TEXT` | `server.py:842, 862` | `main.c:606` | yes (118) | **Header mismatch: bridge sends `mode`, fakegui reads `style`.** Guest ignores `page`, `total`, `off` except for the generic progress block at `main.c:558`. |
# | `TOOL` | **never** | `main.c:621` | yes (125) | Guest comment: *"only survives for older bridges."* Dead on both ends; fakegui still has a renderer for it. |
# | `TOOLRESULT` | `server.py:480` | **none** | yes (127) | Tool results never render on Win98. |
# | `TURN` | `server.py:678, 1073, 1089, 1107` | `main.c:626` | yes (131) | Guest branches only on `state == "start"`; everything else is treated as end. fakegui distinguishes `start`/`end`/`error`. `state=error` therefore shows as a normal completion on the real client. |
# | `NOTICE` | `server.py:2043`, `_write_block` via `H.notice` | `main.c:871` | yes (146) | Guest reads only the body; `kind=` is ignored. |
# | `ERROR` | `error()` at `server.py:322, 334, 344, 352, 358, 367, 387, 1033` | `main.c:862` | yes (152) | **Unescaped into the document.** |
# | `STATUS` | **never** (only `protocol.status()`) | **none** | yes (148) | Vestigial. |
# | `PONG` | `server.py:1003` | **none** | yes (154) | |
# | `PERMREQ` / `PERMSTATE` | `server.py:619, 633` | `main.c:805` | yes (156) | Guest reads `id`, `allows`; ignores `waiting` on PERMREQ (only reads it on PERMWAIT). fakegui reads `waiting` on both. |
# | `PERMWAIT` | `server.py:685` | `main.c:844` | yes (168) | Guest only writes it to the log — the queue depth is never shown on screen, although `PLAN.md:719`'s modeless-bar design implies it should be. |
# | `PERMEND` | `server.py:705, 1863` | `main.c:851` | yes (170) | Guest ignores `reason=`; fakegui prints it. |
# | `HALT` / `HALTEND` | `server.py:658, 910, 1621, 1632` | `main.c:815, 827` | **no** | fakegui cannot answer a halt — it dumps the frame generically. Its own docstring calls it *"the break-glass tool when a permission prompt has the session wedged"*; a halt wedges it identically and there is no `/continue`. |
# | `SESSIONS` / `SESSION` / `SESSIONSEND` | `server.py:967, 970, 980` | `main.c:688, 696, 736` | no | |
# | `SWITCHED` | `server.py:2024, 2070` | `main.c:746` | no | |
# | `OPTION` / `OPTIONSEND` | `server.py:1944-1966` | `main.c:647, 684` | no | |
# | `PAGESTART` / `PAGEEND` / `PAGESEALED` | `server.py:2095, 2103, 825, 1101` | `main.c:773, 788, 793` | no | |
# | `ANCHOR` | `server.py:1070` | `main.c:800` | no | |
# | `EXEC` | `server.py:1159` | `net.c:191` → `rpc.c` | **no** | fakegui never answers an `EXEC`, so the whole tool path is unexercisable through it. |
# | `EXECKILL` | `server.py:1202` | `net.c:183` (reader thread) | no | |
# | `SYNCSCAN`/`SYNCPUT`/`SYNCPUTEND`/`SYNCGET`/`SYNCMKDIR` | `syncrun.py:129, 345, 356, 407, 482` | `net.c:213` → `syncrpc.c:461-466` | **no** | Same gap: fakegui cannot participate in a sync pass. |
#
#### 2b. Guest → bridge
#
# | Frame | Guest sends at | Bridge handler | fakegui sends |
# |---|---|---|---|
# | `HELLO` | `net.c:480` (22 headers) | `server.py:305` | yes, **3 headers** (`protocol.hello`) |
# | `INPUT` | `main.c:1093` | `server.py:887` | yes (224) |
# | `INTERRUPT` | `main.c:1107` | `server.py:947` | yes (194) |
# | `RESUME` | `main.c:1540` (Continue on the halt bar) | `server.py:892` | **no** — `/resume` is a reconnect, an unrelated meaning |
# | `PERMREPLY` | `main.c:543` (`allow`/`always`/`deny`) | `server.py:1830` | yes (216) |
# | `GETSESSIONS` | `main.c:381` | `server.py:963` | no |
# | `SWITCH` | `main.c:414` | `server.py:982` | no |
# | `GETPAGE` | `main.c:304` | `server.py:985` | no |
# | `NEWSESSION` | `main.c:1564` | `server.py:1005` | no |
# | `SETMODEL` | `main.c:1625` | `server.py:1011` | yes (200) |
# | `SETEFFORT` | `main.c:1631` | `server.py:1020` | no |
# | `SYNCDIRTY` | `rpc.c:96` | `server.py:929` | no |
# | `SYNCENT`/`SYNCEND` | `syncrpc.c:52, 104, 151` | `server.py:1644, 1692` | no |
# | `SYNCPUTACK` | `syncrpc.c:190, 343`, `rpc.c:375` | `server.py:1701` | no |
# | `SYNCMKDIRACK` | `syncrpc.c:383` | `server.py:1701` | no |
# | `SYNCDAT`/`SYNCDATEND` | `syncrpc.c:437, 411, 455` | `server.py:1707, 1710` | no |
# | `EXECOUT` | `rpc.c:141` | `server.py:938` | no |
# | `EXECRESULT` | `rpc.c:190, 226, 241` | `server.py:941` | no |
# | `STAT` | **never** | `server.py:988` | no |
# | `PING` | **never** | `server.py:1002` | yes (197) |
# | `GETOPTIONS` | **never** | `server.py:1008` | no |
# | anything else | — | `server.py:1031`: logs + `ERROR unknown_type`, **does not close** | — |
#
#### 2c. HELLO headers
#
# Guest sends (`net.c:370-479`): `ver`, `secret`, `max_frame`, `mem_kb`,
# `mem_free_kb`, `os`, `cpu`, `screen`, `disk_free_kb`, `sess`, `acp`, `case`,
# `devices`, `trailing`, `illegal`, `eol`, `shortnames`, `dirreserve`,
# `workreserve`, `maxpath`, `maxcomp`, `manifest`.
#
# | Header | Read by | Status |
# |---|---|---|
# | `ver`, `max_frame`, `secret` | `_attach` | enforced, refuses |
# | `mem_kb` | `limits_for` (`pages.py:102`) | used |
# | `mem_free_kb`, `os`, `cpu`, `screen`, `disk_free_kb` | `machine.describe` | used, prompt only |
# | `sess` | — | **guest sends it; `_attach` never reads it.** Session identity is entirely bridge-side (`self._guest`). |
# | `from` | `_attach` (`server.py:419`) | **bridge reads it; the guest never sends it.** BACKLOG §3, §5. |
# | `acp` … `manifest` (11) | `caps.from_hello` | used |
# | `shortnames` | parsed, validated, stored — **and never consulted by any rule.** | `PLAN.md:835` names 8.3 aliasing as a hazard ("one file can arrive spelled two ways"); the capability exists, the rule was never written, and it is not in BACKLOG. |
# | `sync_root` | `server.py:503` | **bridge reads it; the guest never sends it.** See top finding 8. |
#
# `bridge/fakegui.py` sends only `ver`, `secret`, `max_frame` (+ optional `sess`,
# `from`), so every fakegui-driven test exercises `Caps(declared=False)`,
# `manifest=1` and `limits_for(0)`. The real guest exercises none of those paths.
#
# ---
#
### 3. Error and status vocabulary
#
#### 3a. The real inventory (the brief's list is incomplete)
#
# **Sync, guest → bridge.** Grouped by the frame that carries them:
#
# | Frame | Statuses actually emitted | Site |
# |---|---|---|
# | `SYNCEND` | `ok`, `no_root`, `too_many` | `syncrpc.c:90, 102, 112, 116-117` |
# | `SYNCPUTACK` | `ok`, `busy`, `bad_path`, `readonly`, `no_parent`, `no_space`, `locked`, `too_long`, `changed`, `crc_mismatch` | `syncrpc.c:221-341`, `rpc.c:374` |
# | `SYNCMKDIRACK` | `ok`, `exists`, `is_file`, `no_parent`, `too_long`, `no_space`, `bad_path` | `syncrpc.c:352-382` |
# | `SYNCDATEND` | `ok`, `locked`, `missing`, `is_directory` | `syncrpc.c:401-455` |
#
# That is **sixteen** words, not twelve. `no_root`, `too_many`, `exists` and
# `is_file` are missing from every written description of the vocabulary.
#
# **Exec, guest → bridge**: `ok`, `timeout`, `spawn_failed`, `cancelled`,
# `too_long`, `busy` (`rpc.c:189, 201-205, 240`). All six are handled
# (`server.py:1780-1810`).
#
# **Bridge → guest `ERROR` codes**: `expected_hello`, `hello_timeout`,
# `bad_version`, `bad_frame_cap`, `bad_secret`, `busy`, `bad_caps`,
# `unknown_type`. Guest renders the code as text and does nothing with it.
#
#### 3b. Is it coherent? No — six specific problems
#
# 1. **`busy` means three different things.** *Exec:* "a command is already
#    running" (`rpc.c:240` → "The connected machine is already running a
#    command."). *Sync:* "a transfer is already in flight, or this chunk does not
#    belong to the active transfer" (`syncrpc.c:221, 250, 278`). *Handshake:*
#    "another client holds this session" (`server.py:367`). Three layers, one
#    word, and the third is an `ERROR` code rather than a `status` so at least it
#    is structurally distinct.
#
# 2. **`too_long` means two different things.** *Exec:* the command line exceeds
#    `EXEC_MAX_CMD`. *Sync:* the **path** exceeds `MAX_PATH` minus the working
#    reserve. Both reach the model; the exec one carries a long explanation
#    naming the 1000-character limit (`server.py:1783`), the sync one is a bare
#    `push x refused: too_long` in `reports`.
#
# 3. **One condition, two words.** `!rc_sync_contained(rel)` produces `bad_path`
#    in `do_put` (`syncrpc.c:222`) and `do_mkdir` (`:355`), and `missing` in
#    `do_get` (`:414`). Identical cause, opposite-sounding answers — `missing`
#    reads as "the user deleted it", which is a thing the bridge acts on
#    differently from "you asked for a path I refuse".
#
# 4. **`locked` carries three unrelated causes in one function.** In `do_get`:
#    the file is genuinely held open (`:422`, `:429`), **the heap allocation for
#    the read buffer failed** (`:408`, with a comment acknowledging it), and
#    **`rc_net_send` failed, i.e. the link went away** (`:439`). "Retryable" is
#    the only property they share, and the third is not retryable over the same
#    connection.
#
# 5. **`no_space` is the catch-all for two non-disk failures.** `WriteFile`
#    failing for any reason (`syncrpc.c:258`) and `rc_sync_commit` returning `-3`
#    — which is `MoveFileA` failing (`sync.c:465`) — both report `no_space`. The
#    sibling `-4` case got its own word (`too_long`) with a comment explaining why
#    conflating it was wrong; `-3` did not get the same treatment.
#
# 6. **The bridge collapses the whole sync vocabulary to `!= "ok"`.**
#    `syncrun.py:137, 368, 412` and `487` are the only readers. Sixteen carefully
#    distinguished words produce three behaviours: abort the pass (scan), append
#    `push/pull/mkdir <rel> refused: <status>` to `reports`, or (for `exists`)
#    succeed. `no_parent` in particular is documented at `syncrpc.c:231-233` as
#    existing *precisely* so the bridge can tell a missing directory from a
#    permissions problem — and the bridge does not branch on it. The mkdir
#    docstring (`syncrun.py:470`) even describes the failure mode
#    (`readonly` "reads as a permissions problem and is not one") without acting
#    on the distinction the guest went to the trouble of sending.
#
#### 3c. Two semantic asymmetries in exec
#
# - **`timeout` is not an error.** `server.py:1809`'s `isError` set is
#   `spawn_failed`, `too_long`, `busy`, `cancelled`. A command killed at the
#   timeout returns `isError` absent, i.e. success, with a prose preamble. Every
#   other terminal failure is flagged.
# - **The guest silently clamps the timeout it was given.** `rpc.c:176` caps at
#   120 000 ms with no report. If the bridge ever asked for more (it cannot
#   today, because `tools.py:101` clamps to the same number by hand), the model
#   would be told "Timed out and was killed after the limit" for a limit it never
#   requested.
#
# ---
#
### 4. Documentation drift
#
#### 4a. PLAN.md describes a client that no longer exists
#
# | PLAN.md says | Reality |
# |---|---|
# | `### Renderer: RichEdit 2.0` (`:431`) with a full section, plus **"MSHTML rejected for v1"** (`:446`) and a `make lint` gate for RichEdit-3.0 messages | The shipping transcript is `win/htmlview.c`, **hosting MSHTML**, and `win/htmlview.h:6` gives the measurement that justified the reversal. The `lint` target (`Makefile:35`) still greps `win/` for RichEdit 3.0 messages against a renderer that is gone. PLAN.md was never updated; the decision lives only in `htmlview.h`'s header comment and PROGRESS.md. |
# | *"Wire carries pre-styled runs: `TEXT\nstyle=code\n\n<raw bytes>`. Win98 looks `style` up in a static `CHARFORMAT2` table"* (`:365`) | The wire carries finished HTML with `mode=append\|live`. `style=` survives only in `protocol.text()`, which nothing calls, and in `fakegui.py:119`. |
# | *"`seq=` monotonic per session — what `resume ... from=` replays from"* (`:376`) | Stamped on every frame; the guest never reads it and never sends `from=`. BACKLOG §3 and §5 cover this. |
# | *"`page=` — ignored in Phase 1, switches the control in Phase 2"* (`:377`) | Still ignored. The guest switches pages from `PAGESTART`/`PAGEEND`, not from `page=` on `TEXT`. |
# | *"`sess=` — see multi-session"* (`:378`) | A HELLO/READY header only, never a per-frame header. The guest does send `sess` in HELLO and the bridge does not read it. |
# | *"`enc=` — `raw` only in Phase 1; `deflate` / `rle8` / `rle8+deflate` later"* (`:379`) | `compress.py` defines `ENC_RAW` and `ENC_DEFLATE` only. `net.c:163` inflates `deflate` and drops the link on anything else. **`rle8` and `rle8+deflate` were never built and are not in BACKLOG.** They are named in the screenshot design (`:152`), which is Phase 6, so this may be intentional — but nothing says so. |
# | *"Malformed header, unknown `TYPE`, repeated key, truncated body → `ERROR` frame **and close**"* (`:384`) | Malformed framing does close on both sides. An unknown TYPE does **not**: `server.py:1031` logs, replies `ERROR unknown_type`, and continues. The guest does neither — `main.c:882` logs and drops, sending nothing back. Duplicate keys: the bridge raises `ProtocolError` (`protocol.py:149`, closes); the guest **accepts duplicates** and `rc_frame_get` returns the first (`frameproto.c:33-40`). |
# | *"`STAT` frame so the sender picks the codec from the measured link speed"* (`:237`) | Handled, never sent. See top finding 6. |
# | *"A manifest per side: `path -> (size, mtime, crc32)`"* (`:826`) | The manifest is seven fields with **two** checksums and a class, and the mtime is explicitly never compared cross-side (`sync.h:43`). PLAN's one-line description is three revisions behind. |
# | *"FAT timestamps have 2-second granularity, so mtime comparison needs a tolerance"* (`:840`) | No tolerance exists, because mtime was demoted to a per-side cache key. The PLAN entry now describes a problem the design removed rather than solved. |
# | *"the filesystem is case-insensitive … 8.3 aliasing means one file can arrive spelled two ways"* (`:841`) | Case folding is implemented (`paths.canonical`). **8.3 aliasing is not handled at all**, although `shortnames` is declared, sent and parsed. Not in BACKLOG. |
# | The tool table (`:790-800`) lists `list_dir`, `read_file`, `write_file`, `list_processes`, `kill_process`, `launch`, `get_clipboard`, `set_clipboard` as tools that pass the exec test | `tools.py` ships three: `exec`, `sync_status`, `sync_resolve`. The last two are not in PLAN's table at all. The gap is Phase-ordering rather than drift, but PLAN's `mcp__machine__*` list (`:326-341`) is the canonical tool surface and omits both shipped sync tools. |
#
#### 4b. There is no protocol section
#
# The brief asks for "the Protocol section under Phase 3.3". **There isn't one.**
# `PLAN.md` §3.3 is five bullets of design intent; the frame vocabulary appears
# nowhere in PLAN.md except four incidental mentions (`STAT`, `PERMREQ`, and the
# verbs `EXEC`/`INPUT`/`SWITCH` used as examples of OS-neutral naming at `:353`).
# Forty-one frame types, roughly ninety header keys and sixteen sync statuses are
# documented only by their implementations plus a rolling narrative in
# PROGRESS.md. `frameproto.h:1-9` and `protocol.py:1-18` each give the grammar
# and neither gives the vocabulary. **This is the single largest documentation
# gap in the project** — and it is what made this audit necessary.
#
#### 4c. Where BACKLOG is ahead of the code (credit where due)
#
# BACKLOG §3, §4 and §5 already record: `from=` on the guest, the dead replay
# buffer, `cwd` as a truncatable header, concurrent turns, `reject_sync_busy`
# returning the wrong tuple arity, the unserialised `n->sock`, the `_snprintf`
# truncation sweep, deletions not propagating, directories being resurrected,
# the invisible pass-level abort, and `DOC`'s `g_doc_sent` guard. Several
# candidates I found were already there, closed and struck through
# (`RC_EXEC_TOO_LONG`, `bytes=`, `_exec_out`'s cap, `eol=cr`). The file is doing
# its job.
#
# **Not in BACKLOG and not in PLAN:** `TOOLRESULT`/`RESYNC`/`PONG` unhandled on
# the guest; `STAT`/`PING`/`GETOPTIONS` never sent; `sync_root` never sent;
# `shortnames` never used; the unescaped `ERROR` path; the 32 KB/64 KB
# truncation message; `rc_sync_contained`'s backslash hole; the compressed-replay
# interaction; `TURN state=error` being treated as a normal end by the guest.
#
# ---
#
### 5. Logic implemented twice because it must run on both sides
#
# | Logic | C | Python | Necessary? | Agreement tested? |
# |---|---|---|---|---|
# | Content classification | `scan_class` (`sync.c:83`) | `classify` (`paths.py:278`) | **Yes** — each side classifies files it owns and the other never sees | **Yes**, `test_guest_agreement`, over a 20-file adversarial corpus, running the shipping `sync.c` under Wine. The strongest thing in the codebase. |
# | CRLF normalisation | `scan_chunk` (`sync.c:43`) | `normalise` (`paths.py:311`) | Yes, same reason | **Yes**, both checksums, same test. Chunk-boundary carry is covered. |
# | Which CRC a class selects | `RC_CRC_FOR` (`sync.h:59`) — one macro, four call sites | four inline expressions (`sync.py:125`, `server.py:1686`, `syncrun.py:293, 462`) | Yes | Indirectly, by `test_the_selected_checksum_is_one_of_the_two` — but that tests the *guest's* `crc` column, not the four Python sites. |
# | Working-name recognition | `rc_sync_is_working_name` (`sync.c:249`) | `WORKING_SUFFIXES` (`syncrun.py:237`) + `ignores.ALWAYS` (46) | Yes — an interrupted write on either side must not become content on the other | Each side tested in isolation (`test_guest_agreement:122`, `test_sync_wire:598`, `test_ignores:77`). Never against each other, and the strings are hardcoded in all five places. |
# | Working-name length reserve | `rc_sync_begin_write` + `rc_sync_commit` (`sync.c:390, 452`) | `check(..., is_dir=False)` via `caps.maxfilepath` (`paths.py:200`) | Yes — the guest cannot create the file in order to report it too long | **Yes**, `synccheck --limits` walks 230..260 and the test diffs the boundary against `caps.py`. |
# | Path containment | `rc_sync_contained` (`sync.c:209`) | `check` (`paths.py:159`) | Yes, and `sync.h:79` states why: the bridge might be the thing that is wrong | **No.** And they disagree: the C side checks absolute/drive/`.`/`..`/empty; the Python side additionally rejects backslash, control characters, DOS device names, trailing dot/space, component length, full-path length and codepage representability. The backslash gap is exploitable (top finding 3). |
# | Root + rel → full path | `join` (`sync.c:232`), open-coded again at `syncrpc.c:289, 357, 416` | `join_guest` (`paths.py:366`) | **No** — the guest is the only side that opens the file | `join_guest` is tested (`test_paths.py:195`) and **has no production caller**. Dead. Meanwhile the C helper exists and three of five sites do not use it. |
# | Canonical matching key | — | `canonical` (`paths.py:235`) | Correctly one-sided | Tested bridge-side only. `test_guest_agreement`'s docstring notes Wine cannot model case-insensitivity, so the property "these two spellings are one file on the guest" is asserted and not measured. BACKLOG §4 covers the non-Windows half of this. |
# | EOL conversion | none — the guest writes bytes verbatim (`sync.c:405` comment) | `to_guest` / `to_bridge` (`paths.py:322, 346`) | Correctly one-sided, per the "guest transforms nothing" rule | Tested bridge-side (`test_caps`, `test_attrs`). |
# | Manifest row format | `manifest_add` (`syncrpc.c:68`) | `server.py:1657-1690` | Yes | **No.** Four implementations (§Top finding 1). |
# | Frame encode/decode | `frameproto.c` | `protocol.py` | Yes | Partially: `test_protocol.py` covers the Python codec against itself. Nothing round-trips a C-built frame through the Python decoder. The `%XX` sets differ — the bridge escapes everything outside `0x20..0x7E` plus `%` (`protocol.py:51`), the guest decodes any `%XX` (`frameproto.c:14-15`) and **never encodes at all** when building (`rc_frame_build` copies values raw, `frameproto.c:126-129`). A guest header value containing `\n` or `%` would corrupt the frame. Reachable via `tree_id` (`syncrpc.c:134`, built from `g_root`, which is user-supplied INI text) and `root=` (`:150`). |
# | HTML escaping | `esc_html` (`main.c:1011`) | `esc` (`html.py:63`) | Arguably — the guest echoes the user's own input locally | Neither tested against the other. Different behaviour on `\n`. |
#
# ---
#
### 6. Conventions
#
# Figures below are counts over `win/*.c`, `win/*.h` (15 `.c`, 10 `.h`, 6,898
# lines), `bridge/*.py` (22 modules) and `tests/python/*.py` (23 files, 424
# tests). `.bak*`, `.o`, `.exe`, `third_party/` and `.venv` excluded.
#
#### 6a. What is genuinely enforced (state it, because most of it is)
#
# Zero trailing whitespace, zero CRLF files, zero tabs outside Makefiles, zero
# `//` comments in C, zero camelCase Python identifiers, zero non-`static`
# file-scope variables in C, zero `sprintf`/`strncpy`/`wsprintf` (95 `_snprintf`
# against 6 `strcpy`, four of which are in test binaries and two of which are
# length-checked on the line above — `frameproto.c:60, 81`). C brace style is
# next-line for function bodies in all 15 `.c` files. 4-space indent everywhere.
#
#### 6b. C naming
#
# - **`rc_` on non-static functions: 45 of 45**, once the six platform-mandated
#   entry points (`WinMain` ×2, `main` ×4) are excluded. There are no legacy or
#   half-converted names. This is a real, fully-applied rule.
# - **Static helpers drop the prefix entirely** and are short snake_case verbs
#   (`set_child`, `hexval`, `unquote`, `append`, `to_bstr`, `find_id`,
#   `queue_add`, `do_scan`, `do_put`, `recv_exact`, `outq_free`). The prefix
#   marks external linkage, not the file — a defensible and consistently held
#   distinction. No camelCase anywhere except the COM vtable thunks
#   (`site_SaveObject`, `frame_GetWindow`, `place_CanInPlaceActivate`), which
#   mirror the interface method names one-for-one.
# - **Globals: `g_` on every one except `win/inicheck.c:22` (`static int fails`).**
#   A deliberate second prefix `p_` marks `GetProcAddress` slots
#   (`probe.c:619-621`, `rpc.c:34-36`), and `rpc.c` holds both in one block, so
#   it reads as intentional rather than drift.
# - **Public typedefs are uniformly lowercase `rc_*`** (13 of them). **File-local
#   typedefs are not:** lowercase-unprefixed (`scan_state`, `walk_ctx`,
#   `manifest_ctx`, `exec_ctx`), ALL-CAPS function pointers (`FFCN`, `FNCN`,
#   `FCCN`, `PFN`), and **the only CamelCase types in the codebase** — `Host` in
#   `htmlview.c:41` and `htmlspike.c:53`.
# - **`RC_` is a header convention, not a C one.** All 45 macros in `caps.h`,
#   `frameproto.h`, `sync.h` and `net.h` are `RC_*` (plus the Win32-idiomatic
#   `WM_RC_FRAME`/`WM_RC_STATE`). In `.c` files it is a free-for-all:
#   `EXEC_*` (`exec.c:37-41`), `RPC_*` (`rpc.c:60, 76-78`), `ID_*`/`IDM_*`/
#   `TIMER_*`/`BAR_*`/`PERM_*` (`main.c:23-139`), bare `HDR(k,v)`
#   (`net.c:310`), `FROM_SITE`/`STD_IUNKNOWN` (`htmlview.c:43-64`). `main.c`
#   alone mixes four prefixes and *also* uses `RC_INPUT_MAX`, `RC_TITLE`,
#   `RC_USER_RULE`. The two constants this audit cares about —
#   `RC_MAX_INFLIGHT` in `net.c:19` rather than `net.h`, and `EXEC_MAX_OUT` —
#   are both on the wrong side of that line.
# - **One prefix collision that matters for the contract**: `win/syncrpc.c`
#   claims `rc_sync_*` (`rc_sync_root`, `rc_sync_set_root`, `rc_sync_handle`),
#   already owned by `win/sync.c` (`rc_sync_scan`, `rc_sync_commit`,
#   `rc_sync_recover`), declared across two headers. Every other module gets its
#   own segment (`rc_net_`, `rc_rpc_`, `rc_html_`, `rc_exec_`, `rc_frame_`).
#   "Which file is `rc_sync_contained` in" is not answerable from the name.
#
#### 6c. C comments
#
# - **`/* */` only, 100%.** The house style is multi-paragraph *why*, usually
#   with the incident attached — `sync.h:21-37`, `caps.h:8-14`, `net.h:5-27`,
#   `sync.h:88-92` ("*the bridge's tree grew `café.md.gaston-tmp` from a put that
#   failed on a path bug*"). This is the strongest thing about the codebase and
#   it is why this audit was possible at all: most of my findings started from a
#   comment that stated a rule the code then only half kept.
# - **Four `.c` files have no file-header block**: `frameproto.c`, `ini.c`,
#   `log.c`, `net.c` — all four start at `#include`. Each has a narrative header
#   file, which is a plausible rule, except `sync.c`/`sync.h` and
#   `exec.c`/`exec.h` both carry one, so it is not applied uniformly.
# - **Comment density spans 61% to 3%.** `sync.h` 61%, `caps.h` 59%, `net.h` 49%
#   at the top; `frameproto.c` 8%, `netcheck.c` 9%, `htmlview.c` 10%,
#   `htmlspike.c` 10%, `log.c` 3% at the bottom. The two MSHTML hosts are the
#   interesting case: 47 and 39 functions of near-uncommented COM boilerplate,
#   duplicated between the two files, in the module PLAN.md still says does not
#   exist.
# - Three section-divider styles coexist: left-labelled (`sync.c`, `syncrpc.c`,
#   `htmlview.c`), right-labelled (`net.c`, `main.c`), and numbered-experiment
#   (`probe.c`).
#
#### 6d. Python
#
# - **Module docstrings: 21 of 22** (only the empty `__init__.py` lacks one), all
#   in the same incident-driven voice as the C headers.
# - **`from __future__ import annotations`: 21 of 22 bridge modules** — the most
#   consistently applied rule on the Python side. `bridge/machine.py` is the
#   omission. Tests are the mirror image: 5 of 23.
# - **The module-alias table is real and honoured**: `P`=paths, `C`=caps,
#   `A`=attrs, `I`=ignores, `S`=sync, `H`=html, `perms`=permissions,
#   `machine_tools`=tools, `sesslist`=sessions. `syncrun.py` uses `S.` 37 times,
#   `P.` 23. **Where it breaks:** `server.py:31-32` imports `html` both ways on
#   adjacent lines, and its import block is unsorted with the third-party SDK
#   import *last* (`:51`) while `session.py` and `sessions.py` put it first.
# - **`session.py` vs `sessions.py`** — two different modules one character
#   apart, disambiguated only by an alias (`sesslist`) at the single call site.
# - **No `__all__` anywhere**; `_` prefix is the only visibility signal, and it
#   is crossed three times, once in production: `attrs.py:57` imports
#   `ignores._Rule`.
# - **Three ways to write an enum in one package**: real `Enum` (`sync.py:59`,
#   `67`), 18 bare module ints (`caps.py:73-123`), string constants
#   (`html.py:29-30`, `sessions.py:24-27`). The bare-ints form is the one that
#   mirrors `win/caps.h`, so that choice is defensible; the other two are not
#   reconciled.
# - **Dataclass vs hand-rolled**: 14 `@dataclass`, against two `__slots__`
#   classes with manual `__init__` (`attrs.PathAttrs`, `ignores._Rule` — the
#   latter with no docstring at all). Five dataclasses have no class docstring,
#   including `protocol.Frame`, the type this entire audit is about.
# - **Logging is airtight**: six modules, all
#   `logging.getLogger("bridge.<module>")` bound to `log`, **zero f-strings in
#   52 log calls** — 100% lazy `%s`. `print()` is confined to `fakegui.py`,
#   correctly. (`syncrun.py` declares a logger and uses it once; `server.py` 56
#   times.)
# - **`rc_log` is `subsystem: lowercase message`, and 13 of 91 call sites are
#   not** — all in `main.c` (`"switching to session %s"` :408, `"stop requested"`
#   :1106, `"unhandled frame type: %s"` :882, `"bridge error: %s"` :867), with
#   mixed capitalisation on the Win32 failures (`"RegisterClass failed"` :1724 vs
#   `"net: socket() failed"`). The two "I do not know this frame" messages are
#   spelled differently in the two files that emit them (`main.c:882` vs
#   `syncrpc.c:466`).
# - **Quoting**: 97.5% double in bridge, 99.5% in tests; almost every single-quote
#   is forced by nesting. One genuine lapse: `test_paths.py:56-57` mixes both
#   inside one list literal.
# - **Line length** effectively 79 with a soft 88 ceiling; only four lines in the
#   whole repo exceed 88. **Nothing configures this** — no `pyproject.toml`,
#   `setup.cfg`, `.flake8` or `ruff.toml` exists. It is held by hand.
# - **`os.path` vs `pathlib`**: `os.path` in 7 modules (25 uses in `syncrun.py`
#   alone), `pathlib` in exactly one (`registry.py`).
# - **Constant placement** is "immediately after imports" everywhere except
#   `server.py:2171-2172`, where `EFFORT_ORDER`/`EFFORT_LABEL` sit 2,100 lines in.
#
#### 6e. Error-handling idioms
#
# - **C has no stated return convention and five of them in use.** `0`/`-1`
#   (`rc_net_start`, `rc_net_send`, `rc_frame_parse`, `rc_exec_run`,
#   `rc_rpc_start`); a count or `-1`/`-2` (`rc_sync_scan`); a byte count or `-1`
#   (`rc_frame_build`); five distinct unnamed negatives (`rc_sync_commit` returns
#   `0`, `-1`, `-2`, `-3`, `-4`); and 1-is-true predicates (`rc_sync_contained`,
#   `rc_sync_is_working_name`, `rc_html_ready`, `rc_rpc_syncq_full`). **Only 3 of
#   ~17 public int-returning functions document which** (`frameproto.h:43`,
#   `rpc.h:18`, `sync.h:68`). `rc_sync_commit`'s five codes are mapped to wire
#   statuses in `syncrpc.c:328-333`, and §3b(5) above is the direct cost: `-3`
#   became `no_space` because nothing named it.
# - **Python** has three module exceptions (`CapsError`, `ProtocolError`,
#   `SyncError`) and otherwise raises builtins (`LookupError` in `replay.py:72`,
#   `RuntimeError` in `session.py:202`). `sync.py` has none of its own;
#   `syncrun.py` owns `SyncError` for both.
#
#### 6f. Tests
#
# - **All 424 tests are bare module-level functions**, all sentence-shaped
#   (`test_a_value_the_bridge_does_not_know_is_refused_not_defaulted`). ~11% (46)
#   are terse, and they cluster in the older files (`test_fold.py` 8/22,
#   `test_sessions.py` 6/15, `test_html.py` 5/31) — so it reads as a convention
#   that tightened over time rather than one nobody agreed on.
# - **Fixtures in 3 of 23 files.** `harness`, `guest_answers`/`guest_limits`, and
#   a single-letter `b`. Everything else is inline setup.
# - **There is no `conftest.py`**, and it costs twice. `test_sync_wire.py:24-26`
#   reaches the shared `harness` via a `sys.path` insert — the only such hack and
#   the only `# noqa` in the repo. And **the Lutris wine-ge path and the skip
#   gate are copy-pasted** between `test_guest_agreement.py:28-38` and
#   `test_guest_config.py:27-37`.
# - **15 `parametrize` decorators against 27 loop-and-assert tests**, often in
#   the same file (`test_paths.py` has 3 and 6; `test_sync_corruption.py` 5 and
#   5).
# - **Per-test docstrings are ~50% and the rule is "justify the non-obvious"** —
#   `test_sync_corruption.py` 16/21 and `test_paths.py` 27/39 at the top,
#   `test_sessions.py` 1/17 at the bottom. Where present they explain why the
#   case matters, which is the right rule.
# - Section dividers appear in 8 test files but in only 4 bridge modules — **not**
#   in `server.py` (2,265 lines), `syncrun.py` or `sync.py`, the three that most
#   need them.
#
#### 6g. Test wiring — three gaps that bear on this audit
#
# 1. **`make check` from the root does not check the guest.** `CLAUDE.md` says
#    *"`make check` audits imports"*, and the NT-only import audit is the single
#    rule whose violation kills the image before `WinMain`. That audit lives at
#    `win/Makefile:126-160`. Root `Makefile:19-20` delegates `check` to
#    `spike/` — the Phase-0 hello-world. So do `spike` and `wine-smoke`. The
#    real gate is unreachable from the documented command.
# 2. **`win/Makefile` has no `test` or `all-checks` target.** `inicheck.exe`,
#    `synccheck.exe`, `netcheck.exe` and `probe.exe` must each be built by hand,
#    and the two pytest suites that drive them **skip silently** when the binary
#    is absent (`pytestmark = skipif(not os.path.exists(EXE), ...)`). A green
#    `make test` on a fresh checkout has run zero cross-language agreement
#    checks. `netcheck.exe` and `probe.exe` are not driven by anything at all —
#    they have their own `PASS:`/`FAIL:` and free-text reporting conventions and
#    are run by a human or not at all.
# 3. **`bridge/fakegui.py` and `bridge/tools.py` have no tests.** Nothing in
#    `tests/` imports either. `test_tools_surface.py` — despite the name — tests
#    `bridge.session.TOOLS`, not `bridge/tools.py`. So the third implementation
#    of the client side (§2) is both undocumented as a conformance target and
#    unexecuted by CI, which is how it accumulated `style=`/`TOOL`/`STATUS`
#    handling for a protocol that moved on.
#
# ---
#
### Shape of the fixes, in rough order of value
#
# 1. **A generated contract header.** One source of truth — most cheaply a small
#    Python dict — emitting `win/contract.h` and importing into
#    `bridge/contract.py`, covering the working suffixes, the chunk size, the
#    class letters, the type letters, the status words, the exec timeouts and the
#    CSS class names. The build already runs `make`; a generation step costs
#    nothing and removes most of §1c.
# 2. **Make `synccheck.exe` emit a real manifest.** It already links the real
#    `sync.c`; adding a `--manifest <dir>` mode that calls the real
#    `manifest_add` and diffing it against `server.py`'s parser closes the
#    largest untested contract in the project.
# 3. **Wire the guest checks into `make test`.** A `win/Makefile` target that
#    builds all four check binaries, and a `conftest.py` that owns the Wine path
#    and *fails* rather than skips when `RETRO_REQUIRE_WINE=1`. Today the most
#    important test in the tree is opt-in by accident.
# 4. **Fix `rc_sync_contained` to reject `\` and control characters** — three
#    lines, and it restores the independence `sync.h:79` claims for it.
# 5. **Escape the `ERROR` body in `main.c:862`** and route it through the same
#    `<div class="err">` the other two error sites use.
# 6. **Write the protocol section.** A table of forty-one frames with their
#    headers and directions, in `PLAN.md`, generated or hand-maintained. Every
#    finding in §2 is something a table would have made obvious.
# 7. **Either send `STAT` and `sync_root` from the guest, or delete the readers
#    on the bridge.** Each is a handful of lines in `net.c`, and `sync_root` in
#    particular fixes a wrong system prompt on every first session.
# 8. **Decide what the sync status vocabulary is for.** Either the bridge
#    branches on `no_parent`, `locked`, `changed` and `readonly` — which is what
#    the guest went to the trouble of distinguishing — or the guest stops
#    distinguishing them and the words collapse to `ok` / `retry` / `refused`
#    plus a human-readable detail string.
# 9. **Point root `make check` at `win/`**, and give `CLAUDE.md`'s claim
#    something to be true about.
#
#
# ======================================================================
# FINDINGS. A line without "Fixed:" is outstanding work.
# ======================================================================
#
Fixed: 03.1 THE MANIFEST ROW FORMAT WAS WRITTEN FOUR TIMES AND VERIFIED ZERO TIMES: win/sync/syncrpc.c produces it, bridge parses it, test_sync_wire.py re-implements the producer, tools/syncprobe.py re-implements the parser -- and synccheck.exe, the only thing that runs the real C against the real Python, prints a DIFFERENT tab-separated format of its own, so it proves the classifier and never touches the wire format. A field added on one side passes the whole suite. Fixed on both sides. The parser moved out of the frame handler into bridge/files/manifest.py so it can be called by a test. The producer was extracted into rc_sync_manifest_row in sync.cpp, so syncrpc.c's manifest_add and the new `synccheck --wire` emit through the SAME function that puts bytes on the wire -- and test_guest_agreement.py now runs the real guest over a corpus, takes its real rows, and parses them with the bridge's real parser, asserting class and checksum for each. Verified by mutation: adding a field to the C producer fails that test. Two details that mattered: --wire sets stdout to binary, because text mode turns the row terminator into CRLF and the output would stop being the bytes that go on the wire; and the row formatter REFUSES rather than clamping, because with a length prefix a truncated row is not a short row -- the prefix stops matching what follows and the parser reads the next row's fields as this one's path.
Fixed: 03.2 RC_CRC_FOR SAYS THE RULE MUST LIVE IN ONE PLACE AND PYTHON WROTE IT FOUR TIMES (sync.py, server.py, syncrun.py twice). The macro's own comment explains why: getting it wrong at one of four sites is a permanent disagreement, not a glitch, because the two sides then compare the outputs of different functions and the file never converges. Fixed: P.crc_for in bridge/files/paths.py is the single place and all four sites call it.
Fixed: 03.3 rc_sync_contained DID NOT REJECT A BACKSLASH, and it is the check that exists precisely for when the BRIDGE is wrong. It split components on '/' only, so `a\..\..\x` contained no separator it recognised, was read as one nine-character component -- neither '.' nor '..' nor empty -- and passed; join() then handed <root>\a\..\..\x to Win32, which resolves it outside the root. Fixed: it splits on both separators, and synccheck --contained prints verdicts for 23 paths which the tests diff. Verified by reverting.
Fixed: 03.4 THE GUEST HAND-BUILDS HTML WITH CSS CLASS NAMES IT SHARES WITH THE BRIDGE (notice/you/err, produced in both bridge/view/html.py and the guest), so renaming a class on the bridge silently unstyles the guest's copies. The unescaped-injection half was fixed under 01.2. The shared-vocabulary half is fixed here: the three names the guest emits are RC_CSS_NOTICE/ERROR/USER in htmlview.h, and tests/python/test_guest_css.py asserts each one exists as a rule in the stylesheet the bridge actually sends AND that both sides use it for the same kind of block -- so a rename cannot silently turn an error into a notice either. Deliberately a pure-Python test that reads the C header rather than running the guest, so it works without Open Watcom or Wine and cannot become one of the checks that skips in silence. Verified by renaming .notice on the bridge and watching it fail.
Fixed: 03.5 THREE FRAMES THE BRIDGE SENDS WERE UNHANDLED BY THE SHIPPING GUEST -- TOOLRESULT, RESYNC and PONG -- all falling through to render_frame's 'unhandled frame type' log line, which nobody reads on a machine with no console. TOOLRESULT meant tool results NEVER appeared in the Win98 transcript at all: the user saw the call, then silence, then the model discussing output they could not see. All three are handled now: TOOLRESULT renders as a finished block through _write_block so it also enters the paginator and survives a reconnect; RESYNC tells the user the transcript has a hole in it; PONG is logged. fakegui.py rendered all three, which is why none was noticed -- and that masking is itself the open half, now tracked as 03.28/03.31/03.32 rather than left inside a line marked Fixed. The structural remainder, that nothing stops the next frame type being added on one side only, is 04.34.
Fixed: 03.6 THREE FRAMES THE BRIDGE HANDLES WERE NEVER SENT BY THE GUEST: STAT, PING and GETOPTIONS. PING is now sent (see 02.7). STAT still is not, and PLAN.md makes adaptive codec selection depend on it, so should_compress reads a link_bps that is never anything but the CLI default. STAT is fixed too: the guest measures bytes moved over wall time on its sweep, from counters net.c already kept, and sends it -- so link_bps is real and the adaptive thresholds in compress.py and coalesce.py stop being inert. Only when something actually moved, because an idle link would otherwise report nearly zero and pin the bridge to its slowest codec for a link that is merely quiet. The test asserts the measurement changes the codec DECISION rather than merely being stored, which is the difference between wiring it up and appearing to. GETOPTIONS remains unused and is left: it is a handler with no sender and no symptom, unlike the other two, and removing a frame the protocol documents is a decision rather than a cleanup.
Fixed: 03.7 protocol.py's FRAME CONSTRUCTORS WERE A STALE FIFTH DECLARATION OF THE VOCABULARY: text() emits style= and page= while the shipping bridge builds TEXT inline with mode=/enc=/page= and no style=; the guest reads mode and never style; fakegui reads style and never mode, so it renders every thinking block as plain text. status() built a frame nothing sends, and user_input()/interrupt() were unused even by fakegui. Fixed the honest way rather than the large way: the four dead constructors are gone (02.11), and the docstring no longer claims a vocabulary that does not exist -- it says plainly that server.py builds the other ~38 frame types inline, that this is intentional, and that hello() and error() survive because they are needed before a GuestSession exists to hang a send helper off. Converting 38 inline frames to constructors was considered and rejected: the finding itself discourages it, and a constructor per frame type is only an improvement if something enforces their use.
Fixed: 03.8 sync_root WAS READ FROM HELLO AND NEVER SENT. server.py asks hello.get('sync_root') to name the mirrored folder in the system prompt, and win/net/net.c sent 22 headers without it -- so machine.py fell back to the phrase 'the machine's sync folder' on every first session, which is exactly the vagueness the comment above that fallback warns about, and guest_sync_root is only assigned after a pass completes. Fixed: the guest sends it, from the INI, before the socket opens.
Fixed: 03.9 REPLAYING A COMPRESSED FRAME AFTER A STREAM RESET WOULD BREAK THE LINK: _send stores the already-deflated Frame in the replay buffer and _attach resets zip_out on reconnect BEFORE replay.since() re-sends those frames verbatim with enc=deflate bodies produced by the old stream, so the guest's fresh inflater fails and net.c drops the link. Latent only because the guest never sends from= -- which BACKLOG §5 records as dead code, but not this consequence of un-retiring it. Fixed by making it impossible rather than leaving it for whoever finishes resume: Replay.invalidate() empties the buffer when the compressor resets and ADVANCES the sequence counter rather than rewinding it, so a later resume asks for something older than oldest_seq, fails cleanly, and the caller sends RESYNC. That is the honest answer -- those frames existed and cannot be re-sent -- whereas rewinding the counter would make a gap look like a fresh connection. The existing test asserted the REPLAY happened and passed, because nothing checked the guest could decode what it got; it now asserts the RESYNC, and the guest renders RESYNC as of 03.5.
Fixed: 03.10 .gaston-tmp / .gaston-old WAS SPELLED IN EIGHT PLACES ACROSS TWO LANGUAGES, including four literal 11s and two `len - 11` on a filename -- where being wrong walks off the FRONT of the string rather than producing a short one. Fixed: RC_SYNC_SUFFIX_LEN is derived from the macro, every site uses it, and the Python side uses WORKING_SUFFIXES.
Fixed: 03.11 THE CROSS-LANGUAGE AGREEMENT TESTS SKIPPED SILENTLY and the documented make check pointed at the wrong binary. Caught live: a make clean left the check binaries missing, the run reported '2031 passed, 75 skipped', and that read as success while the only checks proving the guest's classifier and the bridge's still agree had stopped running. Fixed: tests/python/conftest.py FAILS when the toolchain is present but a binary is missing (skipping is reserved for a machine that cannot build the guest), make test and make wine-smoke build them first, and the Wine runner/prefix/build paths live in one conftest instead of being copy-pasted into three suites. make check now audits the guest rather than the deleted spike.
Fixed: 03.12 CAPABILITY ENUM VALUES CAN BE RE-MEANT WITHOUT DETECTION -- caps.py's _enum only RANGE-checks, so renumbering a value is caught and changing what a number MEANS is not, which is the worse of the two because both sides stay internally consistent while disagreeing. Closed with the pattern this project already trusts: win/checks/capscheck.c prints every RC_CASE_/RC_DEVICES_/RC_TRAILING_/RC_ILLEGAL_/RC_EOL_/RC_DIRRESERVE_/RC_WORKRESERVE_ value by stringified name, and tests/python/test_guest_caps.py diffs each against the matching bridge constant -- the guest's real numbers, not a copy of them. Proven by changing CASE_FOLD from 1 to 2 in caps.py and watching exactly test_guest_value_matches_the_bridge[RC_CASE_FOLD] fail.
Fixed: 03.13 THE 11-BYTE WORKING-NAME RESERVE IS DUPLICATED ACROSS TWO C HEADERS AND PYTHON WITH NO BUILD CHECK -- closed guest-side with a C89 static assert in caps.h (the negative-array-dimension trick, since there is no static_assert in this dialect) tying RC_WORKRESERVE_GASTON to RC_SYNC_WORK_SUFFIX. Proven by setting it to 12 and watching the BUILD fail with "dimension cannot be negative", which is the strongest form this check could take -- the two cannot drift even for one commit. The Python side was already covered by an existing --limits test and was left alone rather than given a second mechanism.
Fixed: 03.14 THE SYNC CHUNK-BOUNDARY TEST CORPUS IS HARDCODED, NOT DERIVED FROM RC_SYNC_CHUNK -- so changing the chunk size left the tests still testing the old boundary and still passing, which is the worst way for a boundary test to fail. synccheck gained --chunk, and test_guest_agreement.py now builds chunk_boundary, chunk_cr_last and many_cr_boundary around the guest's REAL value, cross-checked against syncrun.py's own CHUNK first. Proven by setting syncrun.CHUNK to 4096 and watching every test in the module error at fixture setup naming both numbers.
Fixed: 03.15 THE EXEC TIMEOUT CEILING WAS AN UNTESTED THREE-WAY CONSTRAINT AND THE GUEST'S CLAMP WAS UNREPORTED. Both halves closed. The three literals in run_one are named (RPC_EXEC_TIMEOUT_FLOOR/DEFAULT/CEIL_MS), and tests/python/test_exec_timeout_contract.py reads them out of rpc.cpp AND out of bridge/agent/tools.py as source text, asserting they still agree and stay correctly ordered through to the MCP timeout -- so the three-way constraint fails in a test rather than in production. The clamp now logs the requested and the effective value when it fires, so a request for twenty minutes no longer quietly becomes two and then reports as though it had been honoured. Proven fail-first: rpccheck gained a case submitting timeout_ms=999999999 and asserting the guest's own log says "clamped"; run against a reverted fix it printed FAIL and exited 1, then passed once restored.
Fixed: 03.16 THE EXEC OUTPUT TRUNCATION MESSAGE NAMED THE WRONG CAP: it always told the model "Output was capped at 32KB", the guest's limit, even when the bridge's own 64KB EXEC_OUT_MAX was what actually fired. The model then reasons about the wrong number and shortens the wrong thing. It now names whichever side did the cutting, keyed on the bridge's own capped flag.
Fixed: 03.17 THE EXEC COMMAND-LENGTH LIMIT SHOWN TO THE MODEL WAS AN UNVERIFIED HAND-COPY -- the prose said "about 1000 characters" with nothing tying it to the guest's EXEC_MAX_CMD. Named as Bridge.EXEC_MAX_CMD and interpolated, so the two numbers are a grep apart instead of unrelated. Deliberately NOT enforced bridge-side before sending, and the reason is recorded where it belongs: the guest prepends its own shell prefix, chosen by probe_exec from GetVersion() (command.com vs cmd.exe), whose length this side does not know -- so a bridge-side pre-check would reject commands that would have fit or pass some that would not. The guest's own too_long refusal stays the authority.
Fixed: 03.18 AN UNKNOWN MANIFEST TYPE LETTER IS DROPPED SILENTLY RATHER THAN DETECTED -- the finding was real and its description was wrong in both words. parse_rows's try covered splitting the line and slicing the path; every conversion after it (Type(), float(mtime), int(crc_norm), int(crc_raw)) sat OUTSIDE the guard, so a bad row raised ValueError out of parse_rows, through _sync_frame and _handle, to the connection pump's outermost except Exception. Nothing was dropped and nothing was silent: the guest's connection went down in the middle of a manifest. The guard now covers the whole row conversion and the log line names the exception, because "unparseable manifest line" over a merely unknown type letter sends the reader hunting a framing bug. Nine tests, eight of which fail without the fix. One of them pins the opposite: `size` is parsed and thrown away because Entry has no size field, so a nonsense size is correctly accepted -- I wrote a case asserting it was rejected, watched it fail, and checked rather than making it pass, because validating a number nothing reads is inventing a requirement.
Fixed: 03.19 WIN9X CAPABILITY DEFAULTS ARE WRITTEN THREE TIMES WITH NO CROSS-CHECK -- partially STALE and the checking mattered: the third copy the finding names, paths._Default, had already been folded into paths.DEFAULT = C.WIN9X in an earlier pass, and the file's own comment at that line says so. What remained was a genuine two-way duplication with different MEANINGS -- caps.h's RC_CAP_* is "what this client is", the Caps dataclass defaults are "what the bridge assumes of a client that declares nothing" -- which is exactly the pair that must agree and has no reason to stay agreeing. capscheck prints the RC_CAP_* block and test_guest_caps diffs it against a real Caps() instance. Proven by flipping one default and watching exactly one of the nine tests fail.
Fixed: 03.20 A FOURTH, UNUSED COPY OF THE PATH-LIMIT CONSTANTS EXISTS AND IS DEAD -- verified with unusual care, because this is the second finding today telling me to delete something in paths.py and the previous one was wrong. Checked every importer of bridge.files.paths including test_paths.py's own `import paths as P`, which never touches P.MAX_PATH/MAX_DIR_PATH/MAX_COMPONENT; check() and _component_ok() read the negotiated caps object exclusively. Genuinely dead, and removed. The anecdote explaining how the limits were originally found is kept as prose, and a dangling comment that said "same reasoning as MAX_DIR_PATH above" now points at the code that actually exists.
Fixed: 03.21 THE HTML SKELETON CONTAINER IDS HAVE NO SHARED SOURCE OF TRUTH -- the bridge writes <div id="t"> and <div id="l"> into SKELETON and the guest finds them by literal, with nothing linking the two. Same contract as the CSS class names beside it and a worse failure mode: an unstyled block is visible, whereas find_id returning NULL is not an error anywhere, so every append would insert into no element and the transcript would be blank forever on a machine with no console. Named as RC_SLOT_DONE and RC_SLOT_LIVE in htmlview.h next to the RC_CSS_* macros, and test_guest_css.py -- which already reads that header for the class names -- now checks the ids too, in pure Python so it cannot become one of the checks that skips silently. Proven by renaming the bridge's id to "tt" and watching it fail. A second test asserts the two ids are DISTINCT, which nothing else would catch: if both macros named the same element, finished blocks and the in-progress block would share a container and every live update would wipe the transcript above it, while each id individually still existed.
Fixed: 03.22 A TYPO IN THE HTML LIVE-MODE STRING WOULD SILENTLY GROW THE TRANSCRIPT UNBOUNDED -- mode was compared only against "live", so anything else, including a misspelling of "live", fell into append and every in-progress block accumulated instead of replacing. Made loud rather than impossible, which is what the finding asked for: a present-but-unrecognised mode logs what it received and what it wanted before falling into the same append behaviour. An ABSENT mode= stays silent deliberately, because most finished blocks legitimately have none.
Fixed: 03.23 TRANSCRIPT ESCAPING GIVES A DIFFERENT ANSWER DEPENDING ON WHICH SIDE WROTE THE BLOCK -- traced end to end, and there is no content for which the two answers compete. html.esc feeds the markdown pipeline, whose output reaches the guest already rendered and goes through rc_html::block()/live(), which insert it verbatim and never call esc_into. esc_into is reached only from say(), whose callers are notice()/error()/user() -- the user's own locally echoed input plus the handful of one-line protocol fields the bridge sends DELIBERATELY unescaped for the guest to make safe (ERROR's code and msg, NOTICE's body, TURN's error msg; confirmed in protocol.py). The two content categories are disjoint by construction, so the finding's premise does not hold and nothing was changed. What WAS missing is that the boundary was undocumented: esc_into's comment explained why escaping lives there but not why it differs from the bridge's, which is an invitation for someone to "fix" the LF/CR handling into matching html.esc and break local status rendering. That reasoning is now written above esc_into. The genuinely different question -- user_turn() strips before escaping and the guest does not -- is 01.56 and is untouched here.
Fixed: 03.24 THE 2000-FILE SYNC SCAN LIMIT WAS NEVER COMMUNICATED TO USER OR AGENT -- the guest refuses with `too_many` and the bridge turned that into a SyncError naming no number, so the one actionable fact (how many files is too many) reached nobody. SYNC_MAX_FILES is mirrored into syncrun.py from win/sync/sync.h, the same way EXEC_MAX_CMD is mirrored in server.py, and the refusal now names the limit and says what to do about it. Proven fail-first.
Fixed: 03.25 THE 16-LEVEL SYNC DEPTH LIMIT FAILS SILENTLY AND CAUSES A PERMANENT RE-PUSH LOOP -- the defect is real and the named mechanism was wrong, which matters because the wrong one is less bad. Tracing diff(): once a path deeper than the guest's walk is pushed, the baseline holds a row the scanner will never report again, so every later pass takes the `not g` branch -- Act.REPORT, "deleted on the guest, kept here". Not a re-push loop: a STANDING FALSE DELETION REPORT, and the path can never be edited-and-synced again either, because the diff never reaches the both-present comparison that would notice a bridge-side edit. Fixed at the root in paths.check(), refusing a path deep enough that the guest could not see it, which is the same shape as the existing MAX_PATH precheck and equally protocol-free. The boundary was derived by tracing walk()'s recursion by hand -- 17 components stay visible, 18 vanish -- rather than guessed.
Fixed: 03.26 HEADER VALUES OVER 255 BYTES WERE SILENTLY TRUNCATED BY THE GUEST WITH NO BRIDGE-SIDE CHECK -- STALE as written; both halves were already closed under 01.0 and the bridge refuses to send an over-long cwd. Verified by reading frameproto.cpp rather than taking the finding's word: the value path returns -1 and drops the frame. The real gap was that nothing tested it, so framecheck.cpp now asserts the refusal, and the key path beside it, against the shipping binary.
Fixed: 03.27 A SEVENTEENTH FRAME HEADER WAS SILENTLY DISCARDED, NOT AN ERROR. Same fix as 01.43: rc_frame_parse now refuses the frame instead of keeping the first sixteen, which makes it symmetric with the over-long key and over-long value paths that already refused. The bridge side is what makes strictness safe, and it is now asserted rather than assumed -- tests/python/test_guest_frame.py walks protocol.py's AST for the largest Frame(...) header count (six today) and fails if it ever reaches RC_MAX_HEADERS, so the failure lands in a test rather than as a dropped link on a user's machine.
Fixed: 03.28 FAKEGUI'S HANDSHAKE NEVER EXERCISED THE CAPABILITY PATHS THE REAL GUEST USES -- it sent three headers (ver, secret, max_frame) where the real guest sends twenty-five, including the eleven RC_CAP_* literals that ARE the capability negotiation. So every test routed through the fake client ran the UNDECLARED path (caps.declared False, manifest defaulting to 1), and the declared path -- the one every real client takes -- was untouched by the whole suite. fakegui now sends FAKE_CAPS and FAKE_IDENTITY, taken literal-for-literal from win/net/caps.h with mem_kb=32768 to match the machine CLAUDE.md measures against, and a new test asserts the bridge parses declared=True, manifest=2 and the DOS/Win9x enums. It fails against the old three-header HELLO.
Fixed: 03.29 THE waiting HEADER ON PERMREQ WAS READ INCONSISTENTLY BETWEEN THE REAL GUEST AND FAKEGUI: the bridge does send it on PERMREQ and PERMSTATE, the real guest reads it only on PERMWAIT, and fakegui read it on all three and printed a "(+N queued)" suffix the real client would never show at that moment. A fake client showing something the real one cannot is the same class of defect as one rendering a frame the real one drops. Removed from the PERMREQ/PERMSTATE branch, PERMWAIT left alone because it already matched, and three tests pin all three branches -- two of them fail against the old code.
Fixed: 03.30 PERMWAIT'S QUEUE DEPTH WAS NEVER SURFACED ON SCREEN, only written to a log nobody reads on a machine with no console -- so a user answering one prompt had no way to know four more were behind it. The bar now keeps its own prompt text separately and renders "(+N waiting)" beside it, updated from the PERMWAIT handler and reset by bar_hide. halt_show is untouched: different bar mode, and g_perm_pending gates the branch, so there is no cross-talk between the two states that 01.55/04.6 are still about.
Fixed: 03.31 FAKEGUI CANNOT ANSWER A HALT -- STALE as literally written, and the stale half was checked rather than assumed: fakegui has explicit HALT and HALTEND branches and a /continue handler that sends RESUME, closed at some point during the 03.5 pass. The OTHER half of the finding was entirely true: none of it had a test, and the halt path is the one the project's first standing rule is about, so "untestable through the fake client" was the worst gap on that list. Answering was also only reachable through a blocking input() prompt, so it is now an answer_halt() method, driven end to end by a test that calls the real Bridge._raise_halt (the same call a sync Pass makes) over a real socket and asserts the halt clears on BOTH sides through a real RESUME/HALTEND round trip.
Fixed: 03.32 FAKEGUI CANNOT EXERCISE EXEC OR ANY SYNC RPC -- STALE as written: _handle_rpc runs a real subprocess for EXEC, cancels on EXECKILL, and answers each sync frame with the honest no-root/readonly/bad_path/missing its own docstring promises. The untested half was true and is closed with five tests, including a real run_on_guest -> EXEC -> subprocess -> EXECRESULT round trip. One thing worth keeping came out of it: the attempt to test two CONCURRENT execs end to end found the bridge already serialises them under exec_lock, so fakegui's busy branch cannot be reached through run_on_guest at all -- the test drives _handle_rpc directly rather than pretending the round trip exercised it.
Fixed: 03.33 PLAN.md's RENDERER SECTION DESCRIBED A CLIENT THAT NO LONGER EXISTS -- a full RichEdit 2.0 design ending "MSHTML rejected for v1", against a shipping transcript that hosts MSHTML. Fixed by recording the reversal rather than overwriting it: a new `### Renderer: MSHTML [R]` section states what shipped and why (the ~4MB measured cost against a 20MB budget, and the padding/borders/tints/lists/tables RichEdit 2.0 cannot do), the old section is retitled SUPERSEDED with its body kept verbatim because its Open Watcom _RICHEDIT_VER investigation is still correct and still useful, and the "rejected for v1" paragraph is annotated in place as reversed with a note that its tradeoff was outweighed rather than wrong. The reversal previously lived only in htmlview.h's header comment and in PROGRESS.md.
Fixed: 03.34 PLAN.md's WIRE-FORMAT DESCRIPTION WAS STALE -- it described pre-styled runs (`TEXT\nstyle=code`) looked up in a static CHARFORMAT2 table, against an actual `mode=append|live`. Verified against source both ways before rewriting: protocol.py's text_frame() builds mode/enc/page headers with no style, and main.cpp branches on strcmp(mode, "live"). The style= constructor is not merely superseded, it is GONE -- protocol.py's own comment records text()/user_input()/status()/interrupt() being removed as dead code. The section now describes mode=append|live and explains the link to the MSHTML change: append is a finished block, live is the in-progress block replaced wholesale.
Fixed: 03.35 rle8 AND rle8+deflate WERE PROMISED IN PLAN.md AND NEVER BUILT, with nothing recording whether that was deliberate. Verified: compress.py defines only ENC_RAW and ENC_DEFLATE, and net.cpp special-cases only "deflate". PLAN.md's enc= bullet now says plainly that two of the four codecs do not exist, and BACKLOG gains a new section 8 recording it as an OPEN QUESTION with both readings stated (a scope cut nobody needs before Phase 6's screenshot codec, or a forgotten one) and what would close it. Deliberately not resolved -- inventing the answer is what the audit was complaining about.
Fixed: 03.36 AN UNKNOWN FRAME TYPE IS HANDLED DIFFERENTLY BY EACH SIDE AND BY NEITHER AS PLAN.md SPECIFIES -- DECIDED, and the decision is that what shipped was already right and the SPECIFICATION was wrong. PLAN said reply ERROR and close. That makes adding a frame type a FLAG DAY: the moment one side learns a new type, every older peer drops the link on first sight of it -- which is the exact opposite of what the HELLO capability handshake exists for, since the whole point there is that an option can be added without both ends shipping together. Closing on anything unrecognised throws that away one layer lower. So the rule is now: ignore and log, never close. The bridge additionally replies ERROR unknown_type, kept deliberately -- one frame, it is how a developer learns their new type is not wired up on the far side, and it closes nothing. The guest logs and drops without replying, which is fine and not an asymmetry worth removing: it has no console, its log IS the diagnostic, and a guest answering every stray frame gives a confused bridge a second thing to be confused about. No code changed on either side; PLAN.md did, in both the error-handling paragraph and the protocol reference.
Fixed: 03.37 DUPLICATE FRAME HEADER KEYS WERE HANDLED ASYMMETRICALLY, CONTRADICTING PLAN.md -- the bridge raised ProtocolError and tore the connection down, which is what the document always specified; the guest had no duplicate check at all. Every k=v line was appended and rc_frame_get returns the FIRST match on a linear scan, so a repeated key was accepted, stored, and never read. That is the worst of the three available answers: the sender believes it set a value, the receiver acts on a different one, and nothing disagrees out loud. The guest drops the frame now, matching the bridge and the document. Safe to be strict because the bridge builds headers from a dict and cannot emit a duplicate even by accident, and framecheck.exe pins both the refusal and the case it must NOT catch, two distinct keys. The remaining half of the original pair, unknown frame TYPE, is 03.36 and is deliberately still open.
Fixed: 03.38 PLAN.md HAD NO PROTOCOL SECTION AT ALL, which the audit called its single largest documentation gap: the frame vocabulary, header keys and limits existed only as implementation plus PROGRESS narrative. A `#### Protocol reference` subsection now closes it, built by reading both implementations rather than by copying the audit: the frame grammar and its limits table with the guest's behaviour at EACH limit, the encoding rule, the unknown-TYPE and duplicate-key divergences, the HELLO capability handshake in three groups with the refuse-rather-than-guess rule and the eol=cr example, the handshake ERROR vocabulary, and a frame inventory by direction. Building it caught three things the audit had wrong because the tree had moved: TOOLRESULT is no longer a frame type at all, STATUS and three dead constructors are gone from protocol.py, and STAT/PING are now sent by the guest.
Fixed: 03.39 PLAN.md's TOOL TABLE OMITTED THE SYNC TOOLS THAT SHIPPED and listed candidates with none marked as built. Verified from bridge/agent/tools.py's build(): exactly three tools are registered on the `machine` server -- exec, sync_status and sync_resolve -- and none of list_dir/read_file/write_file/list_processes/kill_process/launch/get_clipboard/set_clipboard exists anywhere. PLAN.md now marks what shipped, and records WHY read_file/write_file never did: R6's sync layer replaced the Samba share, so Claude's native Edit/Write against the bridge's working copy became the real file-access path and the tools would have failed the exec test. The Phase 6 candidates are left alone as unbuilt future work.
Fixed: 03.40 "busy" MEANS THREE UNRELATED THINGS ACROSS EXEC, SYNC AND THE HANDSHAKE -- confirmed as written, and closed as NOT WORTH CHANGING after looking at each. All three are disambiguated by the frame type carrying them, which the finding itself observes: exec's busy arrives on EXECRESULT, sync's on SYNCEND/SYNCDATEND/SYNCMKDIRACK, the handshake's on the refusal path, and no receiver can confuse one for another because no receiver handles two of them. Sync's own three sites (do_put twice, do_putend once) already mean exactly one thing between them -- this transfer slot is occupied, or this chunk does not belong to the active transfer. Renaming any of the three would change a wire word for a collision that cannot occur.
Fixed: 03.41 "too_long" MEANS TWO DIFFERENT THINGS WITH INCONSISTENT DETAIL SHOWN TO THE MODEL. The sync half is closed: Pass.push and Pass.mkdir re-run P.check() on the refused path to explain WHICH limit was hit, reusing the length arithmetic the bridge already does rather than inventing a wire field to carry it. It also surfaces the case where the bridge's own check finds nothing wrong and the guest refused anyway, which is a disagreement between the two sides' length rules and worth knowing about on its own. Proven fail-first. The exec half is 03.17, already closed separately.
Fixed: 03.42 ONE FAILURE CAUSE PRODUCED TWO OPPOSITE-READING STATUS WORDS -- !rc_sync_contained(rel) reported `bad_path` in do_put and do_mkdir and `missing` in do_get, so the same refusal read as "you asked for something illegal" or "there is nothing there" depending purely on which operation asked. do_get now says bad_path like its neighbours, after checking that no bridge code branches on the literal "missing" from a SYNCGET. Guest-side only, no bridge change needed. Not independently testable through the Python harness, which fakes the guest and so cannot prove what the real C emits -- verified by reading plus the build and gates.
Fixed: 03.43 "locked" CONFLATES THREE UNRELATED CAUSES IN do_get, ONLY TWO OF WHICH ARE RETRYABLE -- confirmed as an accurate reading of the C, and closed as NOT WORTH A WIRE CHANGE, which is the standard this audit asks for elsewhere. The bridge has zero differential handling of any locked-family cause today: pull() reports and the next pass retries regardless, so a new status word would be read by nothing. And the one genuinely distinct cause, the link dying mid-stream, cannot be reported by construction -- the same dead link is what would have carried the word that distinguished it. Revisit if and when the bridge grows a reason to treat them differently.
Fixed: 03.44 "no_space" IS A CATCH-ALL FOR TWO UNRELATED NON-DISK FAILURES -- and it was a THREE-way catch-all, not two. rc_sync_commit returning -1 (the just-written temp no longer stats as a plain file) fell into the same bucket as -3 (the final MoveFileA failing) and as a genuinely failed WriteFile. Both -1 and -3 now report `locked`, reusing the vocabulary that already exists and already means retryable rather than minting a new word, leaving no_space for the one cause that is actually about running out of disk.
Fixed: 03.45 THE BRIDGE COLLAPSES SIXTEEN SYNC STATUS WORDS TO THREE BEHAVIOURS, IGNORING no_parent -- closed two ways. mkdir()'s own docstring was stale, claiming children of a missing directory fail as `readonly` when the guest has said no_parent since an earlier fix; corrected, and no_parent now gets its own plain-English note() instead of the generic "(status)" text. More usefully, a failed MKDIR is now remembered for the pass and every PUSH and nested MKDIR beneath it is withheld with ONE report -- order() already sequences a directory before its contents, so the guest was previously asked for N transfers that could not possibly succeed, each reported separately. The fail-first proof here is unusually strong: without the fix the test HANGS, because the pass really does ask for a transfer that will never be answered.
Fixed: 03.46 A TIMED-OUT EXEC COMMAND WAS REPORTED TO THE MODEL AS SUCCESS: "timeout" was absent from the isError set, so a command killed at the limit came back as an ordinary result with partial output and no error flag, and the model had no reason to treat the output as incomplete. Added to the set, beside spawn_failed, too_long, busy and cancelled -- a command that was killed did not succeed.
Fixed: 03.47 THE GUEST'S PATH-CONTAINMENT CHECK REJECTS FAR LESS THAN THE BRIDGE'S -- confirmed as a factual coverage comparison and closed as OVERSTATED in its framing. rc_sync_contained's stated purpose is narrow: containment and escape, as a backstop for the bridge's containment logic being wrong, and it does that correctly with all synccheck --contained cases passing. Most of what paths.py adds (length, trailing dot and space, codepage) are LEGALITY rules that already fail safely on the guest -- CreateFileA simply errors -- so duplicating them is redundancy, not a second line of defence. The one item that does match the stated purpose is reserved DOS device names, given this project's own recorded AUX/COM1 hang-with-no-timeout history, and it was deliberately NOT added: synccheck.c's fixed cases[] array is the only harness for that function, it was outside the working agent's file scope, and an unverifiable change to containment-critical code that has been buggy before is not worth making. Recorded as the follow-up it is: add aux/aux.c cases to synccheck.c alongside the device check, together.
Fixed: 03.48 THE SHARED PATH-JOIN HELPERS ARE EITHER DEAD OR BYPASSED -- bridge half STALE (join_guest does not exist anywhere in the tree any more, including in the test the finding cites), guest half real and better than described. join() in sync.cpp was declared `static`, so the comment calling it "the shared helper" was impossible on its face: syncrpc.cpp physically cannot call a static in another translation unit, which is precisely why it carried three open-coded copies. Exported as rc_sync_join, all seven internal call sites updated, the three copies in syncrpc.cpp replaced with real calls, and two locals that only existed to serve the copies deleted.
Fixed: 03.49 THE GUEST NEVER PERCENT-ENCODED OUTGOING FRAME HEADER VALUES, so a control character could corrupt a frame -- the contracts-audit statement of the same defect as 01.42, and the contract framing is the sharper one: rc_frame_parse decoded, rc_frame_build did not, and frameproto.h documented the encoded form as though both ends honoured it. A value with LF forges a header; a value with %-plus-hex is decoded into a different string. Fixed by append_quoted in rc_frame_build. The contract is now enforced ACROSS the two implementations rather than within one: tests/python/test_guest_frame.py runs the shipping builder's bytes through bridge/wire/protocol.py's FrameReader, which is the loop bridge/files/manifest.py was given for the identical reason -- a format two halves of one file agree on is not a protocol.
Fixed: 03.50 TURN state=error IS INDISTINGUISHABLE FROM A NORMAL END ON THE SHIPPING GUEST -- STALE, already closed, and verified at HEAD rather than in a working tree that several agents were editing. main.cpp's TURN handler branches explicitly on state == "error", renders the bridge's msg through the view's error() vocabulary and sets the status bar to " error" instead of a cost line, with a comment citing this finding by number. Left untested deliberately and the reason is recorded rather than glossed: no check harness links main.o (guest-checks builds synccheck, inicheck, cppcheck, rpccheck and framecheck, none of which include it), so a behavioural test here means either extracting the TURN dispatch into a testable unit or standing up new main.cpp-linking test infrastructure -- both larger than an already-closed finding justifies, and both properly part of 01.12's dispatch-table work.
Fixed: 03.51 shortnames IS NEGOTIATED AND STORED BUT NEVER CONSULTED -- the description overstates it, and checking showed the negotiation is INCOMPLETE rather than merely unused. bridge/files/caps.py's Caps dataclass has no shortnames field at all: from_hello uses the key's PRESENCE to help decide `declared` and never parses or stores its value. So there is nothing to consult, and the 8.3-aliasing work the finding wants cannot start with a rule in paths.py -- it has to start with a real Caps.shortnames field, which is a change to the capability layer and belongs with whatever actually needs 8.3 handling. Recorded as the two-step thing it is rather than left implying a one-line fix.
Fixed: 03.52 win/syncrpc.c AND win/sync.c BOTH CLAIM THE rc_sync_ PREFIX, UNLIKE EVERY OTHER MODULE -- an earlier agent judged this unsafe from a partial file set and left it alone, which was right at the time; with both files in scope it turned out to be small. Only syncrpc's three functions moved (rc_sync_handle/set_root/root -> rc_syncrpc_*), matching the file and header they live in, and sync.cpp keeps rc_sync_ because it is the module's namesake -- the scan, the checksums, the manifest row. Five call sites updated. Deliberately the smallest change that removes the ambiguity rather than a symmetrical rename of both modules.
Fixed: 03.53 netcheck.exe AND probe.exe HAVE NO AUTOMATED TEST DRIVER AT ALL -- both now have one, wired into guest-checks and wine-smoke. test_guest_net.py drives netcheck against a real Bridge (with FakeSession, which netcheck cannot distinguish since it checks that text arrived rather than what it says), and test_guest_probe.py runs probe's deflate section ONLY -- the rest of probe asks hardware questions Wine cannot honestly answer, and running them would produce a green tick that means nothing, which is this project's standing rule about Wine. Writing the netcheck driver immediately paid for itself by surfacing a bug, and the bug turned out to be in the harness rather than the guest: netcheck stopped pumping the instant the TURN end was handled, so a frame posted in the same breath was still in flight and counted -- correctly -- as such. It stops the link before counting now, which makes inflight == 0 assert net.h's three-way ownership rule end to end against the real net.cpp, something nothing else in the suite does.
Fixed: 03.54 fakegui.py AND tools.py HAD ZERO TEST COVERAGE, AND test_tools_surface.py DOES NOT TEST tools.py DESPITE ITS NAME -- confirmed, it imports bridge.agent.session.TOOLS and never touches bridge.agent.tools. Closed with two new files: test_fakegui_conformance.py (13 tests, the handshake/HALT/EXEC/SYNC paths above) and test_agent_tools.py (13 tests) covering tools.py directly -- empty-command rejection, the 1000/20000/120000 timeout clamp, shell= being honoured, empty-path and unknown-action rejection in sync_resolve, and the module's own central claim, that two build(bridge, guest=X) calls keep their closures bound to their own guest rather than sharing state.
Fixed: 03.55 A TURN THAT CAME BACK ok=0 RENDERED AS A QUIET SUCCESS, which is the other half of 03.50 and the half that actually happens. 03.50 fixed state="error", the path an EXCEPTION takes. A quota or rate-limit refusal does not raise: the SDK returns normally with ok=0 having streamed nothing, and the guest's TURN handler branched only on state=="error" -- so everything else fell through to `done $?` with an empty transcript. The user's own report was "when an error occurs (e.g. we reached our quota), it should surface in the html view, right now i'm just getting interrupted with no message nothing". The guest now renders an error block naming the SDK's own `subtype` (the only header on that frame carrying any information) and the status bar says " failed" rather than " done". An empty subtype still gets a sentence, because "something failed and we do not know what" beats a cost line for a turn that produced nothing.
Fixed: 03.56 THE TURN ERROR MESSAGE TRAVELLED IN A HEADER, AND THE MORE INFORMATIVE IT WAS THE MORE LIKELY THE USER SAW A DISCONNECTION INSTEAD. turn_error() sent `msg=` capped at 200 CHARACTERS, but _quote expands every byte outside 0x20..0x7E to three -- so a 200-character message with roughly 28 newlines in it, which is precisely what a stack trace or a JSON API error looks like, crosses the guest's RC_MAX_VAL of 256. The guest REFUSES an over-long header value rather than truncating it (01.44, deliberately) and net.cpp treats a frame that will not parse as a dead stream and drops the LINK. So this was a hazard created by the 01.44 hardening: before it the message arrived truncated, after it a long message could take the connection down. Measured rather than assumed -- a realistic multi-line quota message encodes to 212 bytes, uncomfortably close to the 256 ceiling. Fixed by moving the text into the BODY, which has no such limit and is where HALT already carries its own text for the same reason; the cap went from 200 to 2000 characters at the same time.
