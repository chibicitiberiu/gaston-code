# 04 — Can the architecture carry Phases 4, 5 and 6?
#
# Audit date 2026-09-18. The original report is preserved below as
# context. Findings are the lines at the end: one per finding,
# prefixed "Fixed:" once the thing it describes has been dealt with.
#
## Can the current architecture carry Phases 4, 5 and 6?
#
# Forward-looking design review, 2026-09-18. Not a bug hunt: everything below is
# about what the *shape* of the code does to work that has not started yet.
# Where a live defect turns up it is noted, but the question being asked is
# always "what does this cost when Phase N arrives".
#
# Read alongside `PLAN.md` (Phase 4-6, R5/R7/R8) and `BACKLOG.md`, which already
# owns the hardening list. Nothing here duplicates the backlog.
#
# ---
#
### Top findings
#
# 1. **The MCP tool closure binds the *bridge*, not the guest.**
#    `machine_tools.build(self)` (`bridge/server.py:516`) is called from
#    `_make_session(guest)` and has a `guest` in scope, and does not use it.
#    Every tool body then re-derives the guest from `self._guest`
#    (`server.py:1120`, `1213`, `1555`). That is three call sites today and
#    eleven after Phase 6, and it is the single hardest blocker to R7. Fix it
#    now, while it is a ten-line change.
#
# 2. **The sync baseline is bound to exactly one machine by construction, and
#    R7 does not say so.** `Pass.__init__`/`scan_guest` refuse on a `tree_id` or
#    `guest_root` change (`bridge/syncrun.py:149`, `:165`). Two clients sharing
#    one `--sync-state` do not degrade, they deadlock: every pass raises
#    `SyncError` and `_report_abort`'s once-only guard fails because the reason
#    alternates between the two machines. R7 needs a decision about the mirror
#    before it needs a dict.
#
# 3. **There are already four ad-hoc chunked transfers, not one**, and Phases 4,
#    5 and 6 add three more. The guest side is the reusable half and it is
#    already written twice: `do_put` is a chunked *sink*
#    (`win/syncrpc.c:199-345`), `do_get` is a chunked *source*
#    (`win/syncrpc.c:388-457`). Export-as-HTML is `do_put` with a different
#    destination; the Phase 5 upload and the Phase 6 screenshot strips are
#    `do_get` with a different source. Extract the two before writing a fifth.
#
# 4. **`CANCEL` does not exist anywhere**, although `PLAN.md` mandates it
#    ("Transfers are cancellable mid-stream via a `CANCEL\nid=` frame"), and
#    there is no progress bar in the guest — no `PBM_*`, and
#    `InitCommonControlsEx` asks for `ICC_BAR_CLASSES` only
#    (`win/main.c:1705`), not `ICC_PROGRESS_CLASS`. A 10 MB Phase 5 upload is
#    13 s on the LAN and 33 minutes on a modem, with no way to stop it and a
#    status-bar string as the only feedback.
#
# 5. **The RPC worker has one job slot, and it conflates "runs for two minutes"
#    with "is a syscall".** `g_queued` holds at most one frame
#    (`win/rpc.c:44`, `:447-460`). A `list_windows` arriving while an `EXEC`
#    runs is answered `status=busy`. The sync layer already learned this lesson
#    and solved it with a second lane (`win/rpc.c:47-63`); Phase 6's eight tools
#    are the third case and need the lane generalised, not a third special case.
#
# 6. **The permission bar is one single-line STATIC, three buttons and two
#    mutually-arbitrated modes** (`win/main.c:127-134`, `:474-533`,
#    `:1282-1283`). The arbitration between the two existing modes was four
#    data-loss bugs. Phase 6 wants a third mode ("the agent is driving") and a
#    multi-step summary, and the label is `SS_LEFTNOWORDWRAP | SS_ENDELLIPSIS`
#    at roughly 45 characters on a 640x480 screen.
#
# 7. **Frame dispatch is a hand-written if/elif chain in four places, with no
#    registry and no tripwire** (`server.py:885-1025`, `win/main.c:550-884`,
#    `win/net.c:183-223`, `win/syncrpc.c:459-467`). `test_sync_apply_coverage.py`
#    exists precisely because "decided correctly, ranked correctly, fell off the
#    end of the chain" cost four defects. That tripwire was never generalised to
#    frame types, and the next three phases add roughly ten of them.
#
# 8. **`guest.sess_id` means two different things**, which is why the effort
#    fork strands the chat list. It is a bridge-minted uuid at attach
#    (`server.py:397-399`) and a Claude conversation id after a fork or a new
#    session (`server.py:1999`, `:2023`). `Session._session_id` is only set from
#    the init `SystemMessage` (`bridge/session.py:283`), which does not arrive
#    until a turn runs, so the assignment at `:1999` is a no-op and the registry
#    does not learn the forked id until `_run_turn` (`server.py:1096`).
#
# 9. **`self._early` is process-global connection state** (`server.py:182`,
#    read at `:875`). A second connection's `_read_hello` overwrites it *before*
#    `_attach` rejects that connection, so frames pipelined behind the first
#    client's HELLO are silently dropped. Reachable today; routine under R7.
#
# 10. **`make check` prints the import table but does not gate on it**
#     (`win/Makefile:130-133`). Phase 6 adds `oleacc.dll`, which must be
#     `LoadLibrary`/`GetProcAddress` or the image dies before `WinMain` with no
#     log. The one audit that exists for that failure mode is advisory.
#
# ---
#
### Cross-cutting: what should change before any of Phase 4 starts
#
# These are not owned by a phase. They are cheap now and expensive once eight
# more tools, three more transfers and a third bar mode exist.
#
#### C1. Bind the tool server to the guest
#
# `bridge/tools.py:40` — `def build(bridge) -> dict`. Each tool body calls
# `bridge.run_on_guest(...)`, `bridge.sync_status_text()`,
# `bridge.sync_resolve(...)`, and each of those re-derives the guest from
# `self._guest`.
#
# *Consequence.* With two clients, guest A's session calls a tool that runs the
# command on whichever guest happens to be in `self._guest`. That is not a race
# that shows up as an error; it is a command executing on the wrong person's
# computer with the right person's approval. It is also why `run_on_guest`
# cannot be tested against two guests today.
#
# *Shape of the fix.* `build(bridge, guest)`, and the three bridge methods take
# `guest` as their first parameter. `run_sync`, `sync_status_text`,
# `sync_resolve`, `_baseline_lock` and `_release_all_latches` all lose their
# `self._guest` lookups. `_reap_idle` becomes a loop. This is the whole of the
# R7 mechanical change and it is worth doing on its own merits before R7 is
# scheduled, because it makes the tool surface honest about what it acts on.
#
#### C2. Extract the generic guest call
#
# `run_on_guest` (`server.py:1117-1185`) is 70 lines, of which about ten are
# about `exec`. The rest is the pattern every Phase 6 tool needs: refuse if not
# connected, take the guest's worker lock, re-check after acquiring it, mint a
# correlation id in the right namespace, register a future, send, wait with a
# budget above the guest's own clamp, kill on cancellation, pop in `finally`.
#
# *Consequence of leaving it.* Eight Phase 6 tools each copy that dance, and
# each copy is an opportunity to omit the re-check after the lock (the comment
# at `:1132-1136` explains why that one matters), the `stamp=False`
# (`:1154-1158`, which exists so a future `from=` cannot re-run a command), or
# the `finally` pop.
#
# *Shape of the fix.* `async def call_guest(guest, frame_type, headers, body,
# *, budget_s, id_prefix) -> reply` on the Bridge, with `run_on_guest` becoming
# a caller that adds the exec-specific headers and the exec-specific result
# rendering. `_exec_result`'s status-to-prose table (`:1779-1804`) stays
# exec-specific; the transport does not.
#
#### C3. One chunked-transfer primitive on each side
#
# Existing implementations, all independent:
#
# | # | Direction | Where | Framing | Ack | Precondition | Cancel |
# |---|---|---|---|---|---|---|
# | 1 | bridge to guest | `_send_html` `server.py:828-864` | `total`/`off` | none | none | no |
# | 2 | bridge to guest | `Pass.push` `syncrun.py:339-364` | `total`/`off` + `SYNCPUTEND` | `SYNCPUTACK` | `if_crc` | no |
# | 3 | guest to bridge | `do_get` `syncrpc.c:432-455` | `total`/`off` + `SYNCDATEND` | n/a | n/a | no |
# | 4 | guest to bridge | `send_out` `rpc.c:125-146` | none at all | `EXECRESULT` | n/a | no |
#
# Phase 4 Export adds a fifth (bridge to guest), Phase 5 upload a sixth (guest
# to bridge), Phase 6 screenshot strips a seventh (guest to bridge).
#
# *The guest-side half is the valuable one*, because it is the half that must
# not hold the payload in memory. `do_put` already opens on `off=0`, writes each
# chunk straight to disk, and commits on END. Export-as-HTML has exactly that
# requirement: a full transcript is hundreds of kilobytes and the guest has tens
# of kilobytes free, so it must stream to a file and never assemble. `do_get`
# already reads a source in `RC_SYNC_CHUNK` pieces and sends each one; a
# screenshot strip is the same loop with a DIB row range instead of `ReadFile`.
#
# *Shape of the fix.* Two small structs in a new `win/xfer.c`:
#
# - a **sink** (`begin(dest) / write(chunk) / commit(crc) / abort(why)`) that
#   `do_put` becomes a caller of, and that Export and an inline-image write also
#   use;
# - a **source** (`open(reader_fn, total) / pump()`) that `do_get` becomes a
#   caller of, and that the Phase 5 upload and Phase 6 strips also use.
#
# Both carry `id`, `total`, `off` and an END frame with a status and a CRC,
# because that is what all three existing ones converged on independently. Add
# `CANCEL\nid=` while extracting: the sender checks between chunks, which is one
# line in each pump loop and impossible to retrofit into four separate loops.
#
# Do **not** unify #1 into this. `_send_html` is a display path with no ack by
# design, and giving it one would add a round trip per block to the streaming
# hot path.
#
#### C4. `_send` still starves the event loop outside sync
#
# `wire_yield` (`server.py:2140-2158`) exists because a chunk loop over an
# uncontended `asyncio.Lock` with a `drain()` below the high-water mark contains
# no await point that suspends. Measured at 128 of 128 chunks.
#
# The same is true of `_send_html`'s loop (`server.py:851-864`) and of
# `_send_page`'s run loop (`server.py:2101-2102`), neither of which calls
# `wire_yield`. Today a page is 16 KB and at most a couple of chunks, so it does
# not bite. **Export-as-HTML is the whole transcript**, and if it is written as
# `for page in pages: for run in page.runs: await self._send(...)` it reproduces
# the exact starvation the sync layer paid a rewrite to discover, on a path
# where the user is sitting and watching.
#
# *Shape of the fix.* `wire_yield` gets called from the generic transfer
# primitive in C3, so no future bulk sender can forget it. Note that its second
# job (return True to pause while a permission prompt is open) is right for sync
# and wrong for Export, so the pause and the yield want separating.
#
#### C5. Frame-type registry with a completeness tripwire
#
# Four dispatchers, no shared list of frame types anywhere. `bridge/protocol.py`
# defines the grammar and deliberately does not enumerate types.
#
# *Consequence.* The recorded failures are already of this class: `SYNC*`
# falling through to the guest's "unhandled frame type" branch (`PROGRESS.md`,
# sync-on-the-real-machine), and `Act.MKDIR`/`Act.PULL` falling off the end of
# `Pass.run`. Phase 4 adds at least `EXPORT`/`EXPORTDAT`/`EXPORTEND` and a
# settings frame; Phase 5 adds attach and upload; Phase 6 adds the UI-tier
# request/reply pairs.
#
# *Shape of the fix.* A single `FRAMES` table in `bridge/protocol.py` naming
# every type, its direction, and — for guest-bound types — its **lane** (see
# C6). Two tests in the shape of `test_sync_apply_coverage.py`: one asserting
# `Bridge._handle` has a branch for every guest-to-bridge type, one asserting
# `win/main.c`'s `render_frame` plus `win/net.c`'s router between them cover
# every bridge-to-guest type (source inspection is the right technique here for
# the same reason it was for `Pass.run` — a fake peer lets a missing branch look
# like "nothing to do").
#
#### C6. Make the guest's frame routing a table with lanes
#
# `win/net.c:183-223` routes by hand: `EXECKILL` handled inline on the reader,
# `EXEC` to the job slot, `strncmp(type, "SYNC", 4)` to the sync FIFO,
# everything else `PostMessage`d to the UI. `win/rpc.c:384-387` repeats the
# prefix test.
#
# The code has already discovered four lanes and named none of them:
#
# | lane | meaning | today |
# |---|---|---|
# | UI | render it | default `PostMessage` |
# | JOB | one at a time, may run minutes | `EXEC` |
# | STREAM | state-machine input, must never queue behind a JOB | `SYNC*` |
# | IMMEDIATE | must run on the reader, cannot wait for the worker | `EXECKILL` |
#
# *Consequence of leaving it.* Every Phase 6 tool needs a lane decision, and a
# wrong one is silent: put `list_windows` in JOB and it returns `busy` whenever
# a command is running; put `screenshot` in UI and it blocks the message pump
# for the duration of a capture; put a `CANCEL` frame anywhere but IMMEDIATE and
# it cannot cancel the transfer it is queued behind.
#
# *Shape of the fix.* One `{ "TYPE", LANE }` table in `frameproto.h`, consulted
# by the reader. Prefix matching goes away with it, which also closes the
# `id_matches_family` hard-coding at `rpc.c:393-398`.
#
# Explicitly **not** part of this: a second worker thread. Whether the UI-tier
# lane needs its own thread is a Phase 6 measurement, not a Phase 3 guess.
#
# ---
#
### Phase 4 — settings and the rest of the surface
#
#### 4.1 Server Host dialog
#
# **Blocker:** `rc_config_save` is called from exactly one place,
# `rc_config_load`, and only when the INI does not already exist
# (`win/ini.c:38-39`). There is no code path that persists a changed setting.
#
# **Consequence beyond the dialog.** R7 wants `client_id` "persisted in the
# guest's INI so a reconnect is recognised as the same client". With save wired
# only to first run, a `client_id` minted at startup is minted again on every
# launch. This is not hypothetical: `syncrpc.c:126-128` records the identical
# trap and works around it by deriving `tree_id` from the volume serial rather
# than storing one.
#
# **Fix:** the dialog's OK handler calls `rc_config_save` unconditionally, and
# that is the whole prerequisite for R7's identity field. Doing the dialog first
# makes R7's persistence free. `rc_config` (`win/ini.h:9-16`) gains
# `client_id[40]`; `inicheck.exe` already asserts a round trip per field, which
# is the test that catches the `sync_root` shape of bug.
#
# **Do it with `DialogBoxIndirectParam`, not `wrc`,** unless R5 lands first —
# see 4.6. One resource-bearing build step is worth adding once, not twice.
#
#### 4.2 Per-client model and effort
#
# Already per-guest (`GuestSession.model`, `.effort`, `server.py:75-76`), and
# `_make_session` reads them in preference to the process defaults
# (`server.py:508-509`). Nothing blocks this; it falls out of R7.
#
# One real gap: `_send_options` (`server.py:1913`) calls
# `guest.claude.server_models()` on every options refresh, which is a round trip
# to the CLI per reconnect per guest. Cache it on the Bridge — the model list is
# a property of the installed CLI, not of a guest.
#
#### 4.3 The effort fork strands the chat list
#
# **Blocker, traced.** `_set_effort` (`server.py:1968-2000`) closes the client,
# reopens with `resume=old, fork=True`, then does
# `guest.sess_id = guest.claude.session_id or guest.sess_id` at `:1999`.
# `Session._session_id` is assigned only in `_dispatch` on the init
# `SystemMessage` (`session.py:282-283`), which is consumed inside `send()` via
# `_consume`. After `start()` alone it is still `None`. So:
#
# - `:1999` is a no-op and `guest.sess_id` keeps the pre-fork value;
# - `_make_session`'s `self.registry.add(s.session_id)` at `:529` adds nothing
#   (`Registry.add` returns early on a falsy id, `registry.py:49`);
# - the registry learns the new id only at `_run_turn:1096`, one turn later;
# - the guest's `g_net.sess` is set only by `READY` and `SWITCHED`
#   (`win/main.c:577`, `:750`), neither of which is sent after a fork, so the
#   guest also holds the old id;
# - `SESSION current=` is computed as `guest.claude.session_id == r["id"]`
#   (`server.py:976-978`), which is correct once a turn has run, but the list is
#   not refreshed after `SETEFFORT` anyway.
#
# **Shape of the fix, and it is the same fix as item 8 in the top findings.**
# Separate the two meanings. `GuestSession` keeps `client_id` (who is connected,
# minted at attach or read from HELLO) and `conv_id` (which conversation is
# open). `conv_id` is *the bridge's own* identity for the conversation and
# survives a fork; the Claude session id is a property of the live client, read
# through `guest.claude.session_id` at the point of use and never copied. Then:
#
# - the chat list marks "current" from `conv_id`, not from an id the bridge may
#   not have yet;
# - `_set_effort` sends `SWITCHED` after the fork so the guest's own copy
#   follows;
# - `Registry` maps `conv_id -> {session ids}` so a forked conversation does not
#   appear twice, which is what "the old conversation stays listed" actually is.
#
# This is worth doing before R7, because `sess_id` is also the field R7 wants to
# overload with client identity, and overloading it a third time is how the
# effort-fork bug got here.
#
#### 4.4 Export as HTML
#
# **Blocker:** nothing exists. `IDM_EXPORT` is a message box
# (`win/main.c:1571-1580`).
#
# **What complicates it.** Three things the plan's one-line description hides:
#
# 1. *It cannot be assembled in guest memory.* The guest holds one page at a
#    time by design; a whole transcript is many pages. Export must stream to a
#    file handle. That is the C3 sink.
# 2. *`_send_html` is not reusable for it.* No END frame, no status, and LIVE
#    oversized frames are silently dropped (`server.py:847-848`). The guest
#    would have no way to know the export was complete, which for a file the
#    user then keeps is the wrong failure.
# 3. *It starves the loop* unless it goes through `wire_yield` (C4). The whole
#    transcript is the largest single thing the bridge ever sends.
#
# **Fix:** implement Export *as the first caller of the C3 sink*, not before it.
# The bridge side is `Paginator.page(n).runs` for `n` in `1..count`, folded and
# chunked, plus a standalone `<html>` wrapper reusing `H.document()`
# (`bridge/html.py:394`) so the exported file looks like the transcript. Add
# `CANCEL` handling in the same change; a long export on a modem is the first
# transfer a user will want to stop.
#
#### 4.5 Read-only permission rule table
#
# **Blocker:** none in the code; it is unbuilt, and nothing in the current shape
# fights it. `bridge/permissions.py` already owns rendering for a 640x480
# screen, and `session_scoped` (`permissions.py:79-96`) already knows the
# destination vocabulary.
#
# **One thing to get right:** the effective rules are per-session, and with R7
# per-client. Parse `.claude/settings*.json` once on the Bridge and render per
# guest; do not cache the rendered table on the Bridge.
#
# **Worth stating:** the plan's framing ("no API reads the effective rules") is
# still true, but the threat model changed under it — `PROGRESS.md` records that
# Claude Code resolves read tools and sandboxed Bash locally and never consults
# `can_use_tool`. So a rule table read from settings files is *incomplete* by
# construction: it cannot show the CLI's own local resolutions. Say so in the
# dialog, or it will be read as "these are all the things that can happen
# without asking me", which is false.
#
#### 4.6 R5 — icons
#
# **Blocker:** no resource-compile step. `WRC` is defined in `make/ow.mk:13` and
# used nowhere; `win/Makefile` has no `.rc` rule.
#
# **Complication:** `make check`'s CMOV gate (`win/Makefile:140-146`) already
# works around objdump linearly disassembling data as code. Linking an `.ico`
# adds a lot more data to the image and will move that false-positive count. The
# baseline is 1; expect to re-baseline, and expect that to look like a
# regression if nobody is warned.
#
# **Fix:** add the `.rc` rule when the dialog in 4.1 needs it, and take the icon
# in the same change. Re-baseline CMOV with a comment saying why it moved.
#
#### 4.7 R7 — multiple clients
#
# This is the largest item and deserves its own section. See **"R7 in detail"**
# below.
#
#### 4.8 R8 — Command menu
#
# **Blocker:** none. The menu-building machinery already handles runtime-filled
# lists with a numbered id base (`IDM_MODEL0`, `win/main.c:52-54`, filled from
# `OPTION` frames at `:647-686`).
#
# **The right shape is the one already used:** a third `kind` on the `OPTION`
# frame (`kind=command`), so the command list comes from the bridge and changes
# without rebuilding a Win98 binary — the same argument that moved the model
# list and the stylesheet. Selecting one inserts text into the input box, which
# needs no protocol at all.
#
# **Watch:** `MAX_OPTS` is 16 and `g_opt_model`/`g_opt_effort` are fixed arrays
# (`main.c:150-152`). A commands array wants the same cap and the same
# silently-drop-past-the-cap behaviour, which is fine, but it is a third
# parallel array. If a fourth appears, make it one table.
#
# ---
#
### R7 in detail — what breaks, and what the migration is
#
#### The mechanical part is small
#
# `self._guest` appears 25 times, all in `server.py`, and no other module
# references it. `GuestSession` already holds everything the plan says stays
# per-guest: paginator, renderer, coalescers, compressor, model, effort,
# permission queue, exec and sync pending maps, locks. `bridge/caps.py`'s
# `from_hello` ignores unknown headers, so adding `client_id` to HELLO is
# backward compatible, and `RC_HELLO_MAX` is 32 with 22 headers in use
# (`win/net.c:304`, `:372-478`). None of that is the problem.
#
#### The parts that actually break
#
# **B1. The tool closure has no guest.** `server.py:516` — covered in C1. This
# is the one that silently runs a command on the wrong machine.
#
# **B2. The sync layer is singular in five places on the Bridge, not the
# guest.** `self.sync_dir`, `self.sync_state`, `self.guest_sync_root`,
# `self._last_sync_reports`, `self._agent_notices` (`server.py:191-203`), plus
# `run_sync(why)` taking no guest (`:1205`), `_baseline_lock` reaching for
# `self._guest` (`:1555`), `_release_all_latches` loading the one baseline
# (`:1558`), and `sync_status_text`/`sync_resolve` doing the same (`:1367`,
# `:1491`).
#
# **B3. And the baseline cannot be shared, by design.** This is the finding R7's
# text misses. `scan_guest` raises `SyncError` when the guest's `tree_id`
# differs from the baseline's (`syncrun.py:149-153`) and again when the reported
# root differs (`:165-171`). `tree_id` is `"%08lX-%s" % (volume serial, root)`
# (`win/syncrpc.c:129-137`), so two machines *always* differ.
#
# Trace the consequence: guest A's pass sets `base.tree_id`. Guest B's pass
# raises. `run_sync`'s `except SyncError` calls `_report_abort`
# (`server.py:1288-1295`), which writes `base.aborted` and returns early next
# time *only if the reason is identical* (`:1338-1339`). A and B produce
# different reasons, so they alternate: every pass from either machine
# announces a stop, in the transcript and into `_agent_notices`, every thirty
# seconds, for both users. And `sync_status_text` reports
# "SYNCHRONISATION IS STOPPED" to both agents permanently.
#
# *So the migration is not "make the baseline per-guest field".* It is a
# decision:
#
# - **One mirror per client** — `sync_dir`, `sync_state` and the baseline move
#   onto `GuestSession`, seeded from per-client config. Two clients on the same
#   project means two working copies and two baselines, which is correct and
#   which the three-way merge already assumes (the ancestor in `.gaston/base/`
#   is per-tree).
# - **One mirror shared** — needs a genuinely new design: N-way reconciliation,
#   a baseline per (tree, client) pair, and an answer to "A and B both edited
#   this and the client wins on conflict, but which client". Do not build this.
#
# Recommend the first, and record the second as explicitly out of scope, because
# "the bridge takes multiple clients" reads as though it is free and it is not.
# `--sync-dir` becoming per-client also means the CLI arguments become a config
# file or a per-client section, which is a Phase 4 settings question and is why
# this belongs with 4.1 rather than after it.
#
# **B4. `Registry` is process-wide.** One `~/.gaston-code/sessions.json`, one
# set (`registry.py:22-33`). R1's whole point was that a Win98 client should not
# see conversations from a machine with different tools. With two clients, each
# sees the other's. R7 says so; the fix is `{client_id: [session ids]}` in the
# same file, with `filter` taking the client. A one-file-per-client layout would
# be worse: the file is written on every new session id and an atomic replace of
# one small file is already the right cost.
#
# **B5. `self.cwd` is process-wide.** `sesslist.listing(cwd=self.cwd)`
# (`server.py:966`), `off_limits(..., self.cwd)` (`:562`),
# `_edits_outside_the_mirror` (`:1468`). If clients get separate mirrors (B3)
# they need separate working directories, or `off_limits` lets client A's agent
# read client B's staged files. This follows from B3 and should be decided with
# it.
#
# **B6. `self._early`.** `server.py:182`, set in `_read_hello:330`, consumed in
# `_pump:875`. Per-connection state on a per-process object. Live bug today: a
# second client that connects and is rejected as `busy` (`:365-368`) has already
# clobbered the first client's pipelined frames, because `_read_hello` runs
# before `_attach` decides. Fix by returning it from `_read_hello` and passing
# it to `_pump` — pure local, no behaviour change, and worth doing now
# independently of R7.
#
# **B7. Reconnect identity.** `_attach` never reads `sess` from HELLO, although
# the guest sends it (`win/net.c:442`) and stores it from `READY`
# (`win/main.c:576-577`). So the reconnect path works by "there is only one
# `GuestSession`, so it must be yours". Under R7 that is exactly the assumption
# that has to go. The vestigial `sess=` header is the natural place for
# `client_id`, and the code to send it already exists; what is missing is
# `rc_config_save` on a changed setting (4.1) so the id survives a restart.
#
# **B8. The "reject, do not evict" rule narrows correctly but needs a test.**
# `:365-368` currently rejects on `self._guest and self._guest.connected`. Under
# R7 it becomes "reject when a *live* connection already claims this
# `client_id`". Note that `client_id` is an unauthenticated assertion — the
# shared secret is the only credential — so a client claiming another's id
# inherits its transcript and its pending permission prompts. That is consistent
# with the existing accepted risk (one secret grants everything) and should be
# written down rather than fixed with per-client secrets.
#
# **B9. `_reap_idle` and `close`.** `server.py:1877` and `:227-228` each handle
# one guest. Mechanical.
#
#### Migration order that keeps each step demoable
#
# 1. C1 (bind tools to guest) + B6 (`_early` local) — no behaviour change, tests
#    unaffected.
# 2. 4.1 Server Host dialog with an unconditional save, and `client_id` in the
#    INI and in HELLO. Bridge reads it and logs it, still one guest.
# 3. 4.3 split `sess_id` into `client_id` and `conv_id`. Fixes the effort fork
#    on its own.
# 4. Decide B3/B5: mirror and cwd per client. Move `sync_dir`, `sync_state`,
#    baseline path and `cwd` onto `GuestSession`, still one entry in the dict.
# 5. `self._guest` becomes `self._guests: dict[str, GuestSession]`. At this
#    point it is a rename plus a loop in `_reap_idle` and `close`.
#
# Steps 1-3 are worth doing even if R7 never happens.
#
# ---
#
### Phase 5 — attachments
#
#### 5.1 Toolbar dropdown
#
# **Blocker:** there is no toolbar. The guest has a menu bar built in code
# (`win/main.c:342-370`) and individual buttons, no `comctl32` toolbar. Two
# things are already right: `_WIN32_IE` is left at 0x0400 so `TBSTYLE_*` is
# available (`make/ow.mk:40-42`), and `ICC_BAR_CLASSES` (`win/main.c:1705`)
# already registers the toolbar class alongside the status bar.
#
# **Complication:** a toolbar with bitmaps is GDI objects on a fixed 64 KB USER
# heap, which is the reason the chat list is a ListBox and not a ListView
# (`main.c:136-139`) and the reason the permission bar reuses one set of
# controls. Adding a toolbar and R5's icon bitmaps at once is the first time
# this project spends real GDI. Measure `GetGuiResources`-equivalent by counting
# our own objects (the plan already rules out `GetFreeSystemResources`) before
# and after.
#
#### 5.2 Images read bridge-side by path
#
# **Blocker:** none — and this is the cheap one. The bridge reads the file and
# base64s it; the guest sends a path. Under R7-with-per-client-mirrors (B3) the
# path is relative to that client's mirror, which is another reason to settle B3
# early.
#
# **One thing that is not in the plan:** `off_limits` (`permissions.py:227`)
# refuses path tools outside cwd. An attachment path is not a tool call, so it
# does not go through that check at all. A guest sending
# `ATTACH path=/etc/shadow` would have the bridge read it and hand it to the
# model. Add the same containment check on the attach path — one call to the
# existing function's logic, not a new one.
#
#### 5.3 Chunked upload from the guest's local disk
#
# **Blocker:** C3 and item 4. There is no cancellable transfer and no progress
# bar.
#
# **What makes this the worst case in the plan.** `MAX_FILE` in the sync layer
# is 1 MB with an explicit reason ("one file must not hold the wire",
# `syncrun.py:46`, enforced at `:316-320`). A 10 MB attachment is ten times
# that, on a link where the guest has one sender thread and a 64 KB outbound
# queue (`win/net.h:59`). While it runs, the queue is full of upload chunks and
# `rc_net_send` blocks its callers for up to 5 s — `BACKLOG.md` §1 already names
# `do_send`, `do_stop`, `chats_switch` and the permission bar as the UI-thread
# callers that block there. So a 33-minute dialup upload makes Stop and the
# permission bar unresponsive in bursts, and that path *has never been executed*
# because the netem gate has never run.
#
# **Fix, in order:**
# 1. Close the netem gate (`BACKLOG.md` §1 records it is no longer blocked on
#    sudo). Not optional before this feature: the upload is the first thing that
#    fills that queue.
# 2. Build the C3 source primitive with `CANCEL` and a determinate progress bar.
# 3. Cap the upload bridge-side *and* guest-side, as `MAX_FILE` does, and report
#    the cap rather than truncating.
#
# **Do not** make the upload adaptive-compressed in v1. The guest→bridge
# compression thresholds rest on `0.5c`, which `PROGRESS.md` explicitly marks
# "provisional, not usable as a threshold" (TCG is not a P200, and the sample
# was pathological at 164:1). Ship raw, measure on hardware, then decide.
#
#### 5.4 "Attach a screenshot of my desktop"
#
# **Blocker:** shares the capture path with Phase 6 and should be built there,
# not here. The plan says "whichever lands first builds it", and the honest
# answer now is that Phase 6 has the harder constraint (28 KB free against a
# 307 KB full-screen DIB) and therefore owns the design. Building a
# non-strip-based capture here and then rewriting it in Phase 6 is the
# speculative-generality trap in reverse.
#
# **One piece is worth lifting regardless:** `screen_palette_for_capture()`. It
# is hardware-verified, non-obvious, and easy to lose.
#
# ---
#
### Phase 6 — the UI tier
#
#### 6.1 Eight tools through one job slot
#
# **Blocker:** `win/rpc.c:44` (`g_queued`, at most one) and `:447-460` (a second
# job is answered `busy`). `list_windows`, `screenshot`, `click`, `send_keys`,
# `launch`, `get_clipboard`, `set_clipboard` are all sub-second syscalls;
# `send_action_sequence` and `exec` are not.
#
# **Consequence.** The model's documented workflow is "enumerate the control
# tree, then batch against what you found". If a background `exec` is running —
# and on a 200 MHz machine a compile runs for minutes — every `list_windows` in
# that window returns `busy`, which the model reads as "the machine is broken"
# rather than "wait". The sync layer hit precisely this and the comment at
# `rpc.c:47-59` explains why a stream is not a job. UI-tier calls are a third
# category: not a stream, not a long job.
#
# **Fix:** the lane table from C6, with UI-tier frames in their own lane. Whether
# that lane needs its own thread is a measurement: a `list_windows` over
# `EnumChildWindows` plus a `WM_GETTEXT` per control on a 200 MHz box may be
# tens of milliseconds or may be hundreds if a target process is wedged (a
# cross-process `SendMessage` to a hung window blocks until it answers). Use
# `SendMessageTimeout`, not `SendMessage`, and then one extra lane on the
# existing worker is probably enough.
#
#### 6.2 The tool surface file
#
# **Blocker:** `bridge/tools.py` defines three tools inline in one `build()`
# function (`:48`, `:110`, `:124`) and registers them in one hand-written list
# at `:162-163`.
#
# **Consequence.** Adding a tool to `build()` and forgetting the registration
# list makes it silently absent — the `Act.PULL` shape, and there is no
# tripwire. `test_tools_surface.py` tests `session.TOOLS`, the built-in list; it
# never looks at the `machine` server's tools. At three tools the omission is
# visible on inspection. At eleven it is not.
#
# **Right shape, and the order matters:**
#
# 1. **Now, with Phase 4:** `build(bridge, guest)` (C1), and a test asserting
#    every `@tool`-decorated function in the module appears in the registered
#    list. Both are small.
# 2. **When Phase 6 starts, not before:** split into `bridge/tools/` with
#    `exec.py`, `sync.py`, `ui.py` and an `__init__.py` that composes them. Each
#    module exports a list; `build` concatenates. A single file is correct at
#    three tools and wrong at eleven, and splitting early is generality nobody
#    needs yet.
# 3. **The argument coercion is the repeated part, not the descriptions.**
#    `machine_exec` spends ten lines on `int()`/`bool()`/default handling
#    (`tools.py:97-105`). Eight more tools with rects, coordinates, key strings
#    and step lists will repeat it eight times. A small `arg(args, "x", int,
#    default=0, clamp=(0, 4096))` helper pays for itself at tool four.
# 4. **Keep the descriptions long.** They are the model's only guide to a
#    machine it cannot see, and the `exec` description's warning about
#    interactive prompts is the difference between a working tool and a
#    two-minute hang. Do not compress them into a shared template.
#
# **One tool-surface decision worth making early.** `list_windows` returns a
# tree, and the plan's own argument is that the tree is ~200 tokens against a
# screenshot's ~850. That is true for one dialog and false for a desktop. The
# tool needs a scope argument (foreground window / by class / by pid) and a node
# cap from day one, or the first call on a busy desktop returns every control on
# the screen and the token argument inverts.
#
#### 6.3 The permission bar
#
# **Blocker:** `win/main.c:1282-1283` — one `STATIC` with
# `SS_LEFTNOWORDWRAP | SS_ENDELLIPSIS`, laid out at `PERM_H - 6` = 20 px high
# (`:1204-1205`) in the width left over after the chat list (170 px) and up to
# three 58 px buttons. At 640x480 in Tahoma 8 that is roughly 45 characters.
# `PROGRESS.md` already records this truncating a halt message at "you have".
#
# **What Phase 6 needs and what it costs:**
#
# *A multi-step summary.* `type "hello" -> click [OK] -> wait "Notepad"` is 40
# characters for three steps. The plan's own example. Six steps do not fit, and
# the plan says to cap the step count without saying at what — the bar's width
# is the actual constraint and nobody has measured it.
#
# *A "the agent is driving, hands off" state.* That is a third bar mode. The
# existing two modes share one control set and arbitrate in two functions:
# `halt_show` returns early if `g_bar_mode == BAR_PERM` (`:487`), and
# `perm_hide` re-shows a remembered halt (`:467-470`). The comment at
# `:100-120` records that the *pairwise* reasoning about these two modes was
# false and cost four bugs, two of which were permanent write refusals. A third
# mode makes it three pairs, and "driving" is not a request — it is a state with
# no answer button, which none of the current machinery models.
#
# **Shape of the fix.**
#
# 1. **Make the bar a stack, not a mode.** One list of bar requests, each with
#    an id, a kind, a priority and whether it is answerable. `show()` picks the
#    highest-priority entry; `resolve(id)` removes that entry and re-picks.
#    That turns N² pairwise rules into one ordering, and the two existing bugs
#    become impossible rather than fixed. It is maybe 60 lines and it is the
#    single highest-value guest-side refactor before Phase 6.
# 2. **Make the label height variable**, capped at three lines, with
#    `SS_LEFT` (wrapping) rather than `SS_LEFTNOWORDWRAP`. `layout()` already
#    recomputes everything when the bar shows or hides (`:1164`,
#    `perm_layout` at `:435-441`), so a variable `PERM_H` is a local change.
# 3. **Cap `send_action_sequence` steps at what three lines hold**, measured,
#    and have the bridge render the compact form. The transcript keeps the full
#    step list (`render_request` already writes an argument table,
#    `permissions.py:162-168`) — but note `permissions.py:143` records that the
#    transcript is *not* guaranteed visible, so the bar must stand alone. Three
#    lines is the budget; design the rendering to it.
# 4. **`allows_always` already refuses "Always" for `mcp__machine__*`**
#    (`permissions.py:112-113`), which is exactly the protection Phase 6 needs
#    and it is already right. Do not weaken it for the read-only UI tools
#    (`list_windows`, `screenshot`, `ocr`) without a specifier mechanism — a
#    whole-tool Always on `screenshot` is continuous unattended screen capture.
#    If those three ever want an Always, it wants to be a bridge-side rule
#    keyed on the tool name with its own storage, and that is new design, not a
#    tweak.
#
# **One thing the plan says that the code contradicts.** "Everything here goes
# through the 3.0 prompt bar" is true, but `PROGRESS.md` records that Claude
# Code resolves some tools locally and never consults `can_use_tool`. That
# applies to built-ins, not to `mcp__machine__*`, so the Phase 6 statement holds
# — worth verifying with a spike before relying on it, since the same assumption
# was wrong once already.
#
#### 6.4 Screenshots in strips
#
# **Blocker:** C3 again. This is the C3 *source* primitive with a DIB row range
# instead of a file read, and it is the case that proves the primitive is worth
# extracting: `do_get` already opens a source, loops `RC_SYNC_CHUNK` at a time,
# sends `total`/`off`, and closes with a status and a CRC
# (`win/syncrpc.c:426-455`). A strip sender is the same loop.
#
# **What complicates it beyond the plan:**
#
# *Inline images in the transcript cannot use `data:` URIs.* The transcript is
# MSHTML under IE5 (`win/htmlview.c`), and IE did not support `data:` URIs until
# IE8. `PROGRESS.md` lists "inline screenshots later" as one of the things HTML
# bought. They will need a real file on the guest's disk. The good news is the
# plumbing exists: `rc_html_set_document` writes `view.htm` beside the exe and
# navigates `file:///` (`htmlview.c:282-332`), so `<img src="shot1.png">`
# resolves relative to it. The image bytes must be written to that directory —
# which is the C3 *sink*, the same one Export needs. Two Phase 4/6 features, one
# primitive.
#
# *`queue_add` is unbounded.* `htmlview.c:248-266` doubles a heap buffer for
# fragments that arrive before the document is ready. On a machine the project
# describes as having tens of kilobytes free, that is the one allocation in the
# guest with no cap. It has never bitten because the document becomes ready in
# under a second, but an inline-image path that appends larger fragments makes
# it a real risk. Cap it and drop with a log line, as `manifest_add` does
# (`syncrpc.c:74-75`).
#
# *The import gate is advisory.* `win/Makefile:130-133` prints DLL names and
# W-suffix imports; nothing fails. Phase 6 adds `oleacc.dll`, which must be
# resolved dynamically, and the failure mode is the image not loading at all
# with no log. Turn the DLL list into an allowlist that exits non-zero on
# anything outside `{KERNEL32, USER32, GDI32, COMCTL32, OLE32, OLEAUT32,
# WSOCK32}`. Ten lines, and it is the only automated protection against the
# worst failure this project can have.
#
# *`make lint` is now dead.* It greps `win/` for RichEdit 3.0-only messages
# (`Makefile:33-43`) and RichEdit was deleted. It passes vacuously, which is
# worse than not existing — a green gate that cannot fail teaches people to
# trust gates. Either repoint it at the new hazards (a static `oleacc`/`mshtml`
# import, `ReadDirectoryChangesW`, `VirtualAllocEx`, `SetWindowLongPtr`) or
# delete it.
#
#### 6.5 OCR
#
# **Blocker:** none. It is bridge-side, consistent with "the guest transforms
# nothing", and it consumes whatever `screenshot(rect)` produces. Sequence it
# after the capture path and after the encoding-vs-legibility experiment the
# plan calls for.
#
#### 6.6 send_keys and click
#
# **Blocker:** none structural. Two guest-side details worth recording before
# they are rediscovered:
#
# - `send_keys` needs `AttachThreadInput` before a cross-thread `SetFocus`. If
#   it runs on the RPC worker (which it should, per C6), the attach is
#   worker-to-target, and the worker must detach afterwards or it inherits the
#   target's input queue state.
# - "Never send keys to our own window" is a thread comparison against the UI
#   thread, not against `g_main` — a child control has the same thread. Compare
#   `GetWindowThreadProcessId(target)` against our process id and refuse the
#   whole process, not one HWND.
#
# ---
#
### Do NOT build this yet
#
# Speculative generality is its own defect, and several of these look like
# obvious groundwork.
#
# - **A second RPC worker thread.** The lane table (C6) is the abstraction; a
#   second thread is an implementation choice that depends on a measurement
#   nobody has taken. Add the lane, keep one worker, measure `list_windows` on
#   the guest, then decide.
# - **A warm `ClaudeSDKClient` pool.** Already rejected twice with good reasons
#   (`PLAN.md` Multi-session, R4). R7 gives one live client *per client*, not a
#   pool per client.
# - **Per-client secrets or any authentication beyond the shared secret.** The
#   plaintext-secret-on-a-trusted-segment posture is an accepted, documented
#   risk. `client_id` is an identity assertion inside that boundary and adding
#   crypto to it protects nothing that is not already exposed.
# - **A shared sync mirror between two clients.** N-way reconciliation with
#   "the client wins on conflict" has no defined meaning for two clients. One
#   mirror per client, and write down that the other option was considered.
# - **A patch/delta format for sync transfers.** Already deferred with a reason
#   (source files are a few KB). Nothing in Phases 4-6 changes that; a 10 MB
#   attachment is a one-shot upload, not a synced file.
# - **Splitting `bridge/tools.py` into a package now.** Correct at eleven tools,
#   premature at three. Do it in the first Phase 6 commit.
# - **A generic "capability" negotiation for anything beyond paths and
#   encodings.** `caps.py`/`caps.h` is well-scoped to filesystem and text rules.
#   Do not stretch it into a feature-flag system for Export, attachments or the
#   UI tier — those are protocol version questions, and `PROTO_VERSION` already
#   exists.
# - **Guest-side markdown, image scaling, OCR, or diffing.** The standing rule
#   holds and Phase 6 is where it will be most tempting.
# - **Retiring the replay buffer.** `BACKLOG.md` §5 explains why it is larger
#   than it looks. It is dead code but it is *inert* dead code, and nothing in
#   Phases 4-6 is blocked by it. Leave it.
# - **A settings dialog framework.** One Server Host dialog. If Phase 6 wants
#   more settings, that is the moment to decide whether a tabbed dialog earns
#   its GDI objects.
# - **Compression on the Phase 5 upload path.** Ship raw, close the netem gate,
#   measure on the P200, then decide. The only throughput number on file is
#   marked unusable.
#
# ---
#
### Suggested order
#
# Everything in "Cross-cutting" is cheap now and expensive later, and none of it
# changes behaviour:
#
# 1. C1 (tools bind the guest), B6 (`_early` local), C5's tripwire tests.
# 2. 4.1 Server Host dialog with an unconditional `rc_config_save`, plus
#    `client_id` in the INI and HELLO.
# 3. 4.3 split `sess_id` into `client_id` and `conv_id` — fixes the effort fork.
# 4. C3 the two transfer primitives, with `CANCEL` and a progress bar, and
#    4.4 Export as their first caller.
# 5. C6 the lane table on the guest.
# 6. 6.3 the bar becomes a stack with a variable-height label.
# 7. Close the netem gate, then R7, then Phase 5, then Phase 6.
#
# Items 1-3 are worth doing even if R7 is never scheduled. Item 4 is worth doing
# even if Export is never shipped, because Phase 5 and Phase 6 both need it.
# Item 6 is worth doing before anything adds a third bar state, which both
# Phase 5 (transfer progress with Cancel) and Phase 6 (agent driving) do.
#
#
# ======================================================================
# FINDINGS. A line without "Fixed:" is outstanding work.
# ======================================================================
#
Fixed: 04.1 THE MCP TOOL CLOSURE BOUND THE BRIDGE, NOT THE GUEST: tools.build(self) captures the Bridge although _make_session has a guest in scope, so every tool re-derives the guest from self._guest. With two clients that runs a command on the WRONG MACHINE with the right person's approval. Three call sites today, eleven after Phase 6. This was the hardest R7 blocker, not the 25 mechanical self._guest references. Fixed: build(bridge, guest) captures the session's own guest, and run_on_guest, sync_status_text and sync_resolve all take it -- threaded as far as _baseline_lock, so it is used rather than decorative. A test asserts all four signatures, because the whole point is that the single-client assumption is now visible in a signature instead of buried in a closure that resolves _guest at call time. Done while there is one client precisely so it cannot be something to remember during the multi-client work.
Deferred: 04.2 THE SYNC BASELINE IS BOUND TO ONE MACHINE BY CONSTRUCTION: scan_guest refuses on a tree_id or guest_root change and tree_id is the volume serial, so two guests sharing one --sync-state do not degrade, they deadlock -- and _report_abort's once-only guard fails because the two reasons alternate, so both users get a 'sync stopped' notice every 30 seconds forever. R7 needs a mirror-per-client decision before it needs a dict. The RATE-LIMITING half is fixed: Baseline.announced remembers every reason said since the last successful pass, so two alternating reasons are each announced once instead of forever -- the previous guard compared against the LAST reason, which suppresses a repeat and nothing else, and two guests refusing each other's tree_id produce exactly the alternation that defeats it. The DESIGN question is deliberately not answered here, because it is a choice between one mirror per client (a weekend, and two people cannot share a tree), one shared mirror with a per-guest baseline dimension (a redesign of the state table five review rounds went into), and one mirror with a writer lease. BACKLOG §7 states all three with what each costs; the point is that it gets picked before self._guest becomes self._guests, rather than discovered from whichever tests break.
Deferred: 04.3 THERE ARE ALREADY FOUR AD-HOC CHUNKED TRANSFERS, NOT ONE -- recorded in BACKLOG §6, which already held the CANCEL deferral and now holds the primitive that CANCEL belongs to. The shape is the audit's own: do_put is a chunked sink, do_get a chunked source, and export-as-HTML, the Phase 5 upload and Phase 6 screenshot strips are each one of those with a different end. Deferred because no caller exists, and because building a fifth special case beside four others is what the finding is complaining about.
Partial: 04.4 CANCEL DOES NOT EXIST ANYWHERE although PLAN.md mandates it for any transfer with a progress bar, and ICC_PROGRESS_CLASS was never registered -- only ICC_BAR_CLASSES, which covers the toolbar, status bar, trackbar and tooltip and NOT the progress bar. The registration is fixed now, because it is one word against a failure that would present as "the progress bar does not appear" on a machine with no debugger. CANCEL itself is deliberately NOT built: it has no caller, the transfers that exist are each already interruptible by other means, and the one that genuinely needs it is Phase 5's attachment upload, which does not exist. Building it now would mean guessing its shape against no user -- and the audit's own 04.3 says there are already four ad-hoc chunked transfers with three more coming, so Cancel belongs to whatever primitive unifies those rather than beside them as a fifth special case. Recorded in BACKLOG §6 with what would close it.
Fixed: 04.5 THE RPC WORKER HAD ONE JOB SLOT and conflated "runs for two minutes" with "is a syscall", so Phase 6's list_windows would answer busy during any exec. The four lanes the code had already discovered are named now and live in a TABLE in frameproto.h rather than in branches: EXEC is JOB (the single g_queued slot, up to 120s, a second one refused with EXECRESULT status=busy); EXECKILL is IMMEDIATE (handled on the reader thread, never queued, because the worker is busy running the very command it cancels); SYNC* is STREAM (the bounded 24-slot g_syncq the worker drains before it looks at g_queued, refused in the SYNC vocabulary); everything else is UI. rc_rpc_route is a switch on rc_frame_lane() and is_sync() delegates to the same table instead of keeping its own duplicate prefix test, so a Phase 6 tool costs one row rather than another branch in the worker. A second worker thread is deliberately NOT added -- 04.5 says not until it is measured. THE PROPERTY IS TESTED AT RUNTIME, not by table lookup: tests/guest/rpccheck.c links the real rpc.o and frameproto.o, starts a genuine EXEC that appends to a marker file forever, then routes EXECKILL and shows it returns in single-digit ms AND that the marker stops growing before rc_rpc_stop runs, so the result is attributable to the kill and not to cleanup. The check was mutation-tested: EXECKILL was temporarily re-routed to RC_LANE_JOB and both assertions failed (the marker kept growing, 28809 to 42247), which is what makes the passing run mean something. 2125 tests pass (2122 plus 3 new), all four gates green, import table identical at 171 symbols across 7 DLLs.
Fixed: 04.6 THE PERMISSION BAR NEEDS TO BECOME A STACK, NOT A MODE -- done, as the same object as 01.55. Two modes arbitrated pairwise had already cost four data-loss bugs; there is now a bounded stack whose visible entry is derived rather than stored, so a third kind is one descriptor row instead of a new pairwise interaction to get right. VERIFIED ON THE REAL MACHINE for the two permission renderings: allows=1 shows Allow, Always and Deny with Allow focused and Send disabled; allows=0 hides Always entirely and gives the label the width the old code reserved for a button it was not going to show. The SIX-STEP ACTION SUMMARY is deliberately NOT built: no frame carries that data yet, so there is nothing to render, and the finding's actual ask -- the structural prerequisite -- is what landed. NOT YET VERIFIED on hardware: that a HALT arriving behind a live PERMREQ leaves the permission answerable. The priority table and derived visibility say it must, and the duplicate-id guard beside it was confirmed on the machine, but my own probe timing kept missing the window and I am not recording a check I did not get.
Fixed: 04.7 FRAME DISPATCH IS A HAND-WRITTEN if/elif CHAIN IN FOUR PLACES with no shared table, so a frame type added on one side and missed on another fails silently -- which is how TOOLRESULT went unrendered for the whole of Phase 3. The SILENT-FAILURE half is closed: every frame the bridge sends now has a guest branch (03.5), and the guest's routing vocabulary moved out of net.c into rc_rpc_route (01.4), so there are three dispatch sites rather than four and the transport is no longer one of them. The guest's reject_sync_busy also answered every full-queue sync frame with SYNCPUTACK regardless of type, so a busy SCAN reached the right future with the wrong shape and surfaced as "not enough values to unpack" -- it now answers SYNCEND, SYNCDATEND or SYNCMKDIRACK to match the request. STILL OUTSTANDING: there is no shared TABLE, so nothing structurally prevents the next frame type from being added on one side only. That wants the lane table 04.5 describes and belongs with Phase 6, which is what adds the frames.
Fixed: 04.8 guest.sess_id MEANS TWO DIFFERENT THINGS, which is why the effort fork leaves the chat list on the pre-fork id: Session._session_id is only set from the init SystemMessage, so guest.sess_id = guest.claude.session_id or ... is a no-op and the registry learns nothing until the next turn writes a file. Fixed for the symptom: the id is now picked up in on_system where it actually becomes known -- the init SystemMessage, which arrives DURING a turn -- and the chat list is pushed when it changes, so the list follows a fork instead of naming the pre-fork conversation until some later turn happened to write a file. PLAN.md Phase 4 records that symptom under "still to do"; this is its cause. STILL OUTSTANDING: the field is still one name for two concepts, and the split into client_id plus a bridge-owned conv_id belongs with R7 (02.3), which is what wants it.
Fixed: 04.9 self._early WAS PROCESS-GLOBAL CONNECTION STATE, so a second client clobbered the first's pipelined frames BEFORE being rejected as a duplicate -- a connection about to be refused corrupting the live one. Fixed: the frames travel WITH the HELLO (_read_hello returns them, _pump takes them as a parameter), so there is no shared slot to clobber; the attribute is gone from Bridge entirely and a test asserts it has not come back.
Partial: 04.10 make check PRINTED THE IMPORT TABLE BUT DID NOT GATE ON IT, which is the only automated protection against Phase 6's oleacc.dll killing the image before WinMain -- and the top-level make check audited spike/hello98.exe, a Phase-0 hello-world, rather than the real binary. Partly fixed and partly superseded: the spike is deleted and make check now audits retroclaude.exe, and two real gates were ADDED that fail the build -- unbounded string functions, and static constructors (which run before WinMain and therefore before rc_log_open). The import table is still printed rather than gated against an allowlist; that remains outstanding and is the piece Phase 6 needs.
Deferred: 04.11 run_on_guest'S GENERIC GUEST-CALL PATTERN IS NOT EXTRACTED -- recorded in BACKLOG §9 as part of 02.32, because they are the same extraction named from two audit files and recording it twice would be the duplication the finding is about. Not urgent for the first Phase 6 tool; overdue by the second.
Fixed: 04.12 wire_yield WAS NOT CALLED FROM THE HTML OR PAGE SEND LOOPS, so those paths could starve the event loop the way sync once did. Closed, and the investigation found the finding understated it: wire_yield ALSO returned whether sync should pause for an open permission prompt, which made it the wrong primitive for any other bulk sender to borrow -- a page, or Phase 6's transcript export, has no reason to cut itself short because the user has a dialog open somewhere else. Mechanism and policy are separated now: wire_yield is the sleep(0) reschedule and nothing else, syncrun owns the perm_pending pause, and the chunk loops yield BETWEEN CHUNKS rather than between calls, which is where the starvation actually was.
Deferred: 04.13 rc_config_save IS NEVER CALLED WHEN A SETTING CHANGES -- confirmed exactly as described AND confirmed currently unreachable, which changes what to do about it. Nothing in the shipping guest writes any g_cfg field after load: IDM_HOST is a message box that says a dialog is still to come, and PLAN.md agrees. So no setting can be lost today because no setting can be changed. Recorded as layer 2 of BACKLOG §10 with the one-line call site named, explicitly NOT to be built speculatively -- it acquires a caller when the 4.1 dialog or R7's client_id does, and building the dialog now to give it one would be inventing a requirement.
Fixed: 04.14 server_models() WAS RE-FETCHED FROM THE CLI ON EVERY OPTIONS REFRESH -- and that is every attach, every model change and every effort change, each one shelling out to the CLI. The list is a property of the installed CLI, not of a session or a guest, so none of those calls could have returned anything different from the last. Cached on the Bridge (not on GuestSession: a second client would get the same answer). An EMPTY result is deliberately not cached, so a CLI that was briefly unavailable is asked again rather than leaving the menu blank for the life of the process.
Deferred: 04.15 EXPORT AS HTML IS ENTIRELY UNIMPLEMENTED -- recorded in BACKLOG §6 as one of the transfer primitive's three future callers rather than as its own feature. One of its three stated blockers is already closed: wire_yield is now called from the HTML and page send loops (04.12). The other two stand.
Deferred: 04.16 THE PLANNED PERMISSION-RULE TABLE AND PHASE 6'S "EVERYTHING GOES THROUGH THE PROMPT BAR" ARE BOTH UNDERCUT BY CLAUDE CODE RESOLVING SOME TOOLS LOCALLY -- recorded in BACKLOG §11 as a thing to VERIFY before either is designed, not as a build item. PROGRESS already records the measured fact that can_use_tool is bypassed for some local resolutions; what is missing is knowing which, and that is a measurement rather than a decision.
Deferred: 04.17 THERE IS NO RESOURCE-COMPILE STEP FOR ICONS OR DIALOGS -- WRC is defined and used nowhere. Recorded as layer 1 of BACKLOG §10, where it is the genuine prerequisite for the settings dialog rather than a peer of it. The cost note carries the trap the finding spotted: adding a .rc shifts make check's CMOV false-positive baseline, so the gate has to be re-baselined in the same change or it fails for an unrelated reason.
Deferred: 04.18 Registry IS ONE PROCESS-WIDE sessions.json, WHICH BREAKS R1 UNDER R7 -- recorded in BACKLOG §7 with the R7 cluster. Mechanical once self._guests exists and carries no independent cost, which is why it is one line in a cluster rather than its own task. Gated on the mirror-per-client decision (04.32).
Deferred: 04.19 self.cwd IS PROCESS-WIDE -- recorded in BACKLOG §7 with the R7 cluster; mechanical once self._guests exists, and it moves together with the mirror decision (04.32) because a per-client cwd and a per-client mirror are the same question asked about two values.
Deferred: 04.20 RECONNECT IDENTITY IS NOT ACTUALLY CHECKED -- _attach never reads sess from HELLO even though the guest sends it, so reconnect works today only because there is exactly one GuestSession. Recorded in BACKLOG §7. It is the SAME client_id-persistence gap as 01.35's first point and 04.13's future caller: one fix, three citations, recorded once rather than three times.
Deferred: 04.21 THE "REJECT, DO NOT EVICT" DUPLICATE-CONNECTION CHECK NEEDS NARROWING AND A TEST FOR R7 -- recorded in BACKLOG §7, including the part the finding flags and nothing else does: client_id is unauthenticated, so narrowing the check to it makes identity claimable by anyone who can reach the port. Worth deciding with the rest of R7, not before.
Deferred: 04.22 _reap_idle AND close EACH HANDLE EXACTLY ONE GUEST -- recorded in BACKLOG §7; both become loops the moment self._guests exists, with no design question of their own.
Deferred: 04.23 NO TOOLBAR EXISTS YET FOR 5.1 -- recorded in BACKLOG §11 with the cost that actually matters and that the finding named: it is the project's first real GDI spend against a fixed 64KB USER heap, so it needs object counting before and after rather than an assumption that a toolbar is free.
Deferred: 04.24 off_limits IS NEVER CONSULTED ON THE PHASE 5 ATTACHMENT PATH -- recorded in BACKLOG §11 AND flagged as premature, which is the useful part. ATTACH does not exist anywhere in the tree; this is a requirement for a Phase 5.2 feature rather than a live hole, and filing it as a defect would have had someone hunting for the path that ignores off_limits.
Deferred: 04.25 THE PHASE 5 UPLOAD WILL FILL THE GUEST'S ONE 64KB OUTBOUND QUEUE AND BLOCK THE UI THREAD -- recorded in BACKLOG §6 with the transfer primitive, because the queue behaviour is a property of how the upload is built rather than a separate defect. It is also untestable until BACKLOG §1's throttled-link gate runs, which is noted there rather than assumed away.
Fixed: 04.26 NOTHING ASSERTED THAT EVERY @tool-DECORATED FUNCTION IN build() IS ACTUALLY REGISTERED -- three of them are decorated and then hand-listed in a separate tools=[...] a few lines later, so omitting one registers nothing and reports nothing. The audit names the shape correctly: it is the Act.PULL bug, which already shipped once. Closed with an AST test that walks build()'s body for @tool-decorated functions and diffs them against the Names passed to create_sdk_mcp_server, in BOTH directions. Proven by removing machine_sync_resolve from the list and watching it fail by name, then restoring it -- a check nobody has watched fail is not evidence, and this is exactly the check that would have caught the bug it is modelled on.
Deferred: 04.27 list_windows HAS NO SCOPE ARGUMENT OR NODE CAP PLANNED -- recorded in BACKLOG §9. The finding's own argument is the reason: PLAN.md justifies a control tree at ~200 tokens against a screenshot's ~850, and that holds for one dialog and INVERTS on a busy desktop, so the tool's justification depends on a cap it does not have.
Deferred: 04.28 INLINE SCREENSHOTS CANNOT USE data: URIs UNDER MSHTML/IE5 -- recorded in BACKLOG §9, cross-referencing §6's sink rather than inventing a fourth transfer design for images. Images have to be written as real files beside the document, which is a constraint on the Phase 6 design rather than work to do now.
Fixed: 04.29 queue_add WAS AN UNCAPPED HEAP-DOUBLING BUFFER -- the one uncapped allocation in the guest, on a machine whose first standing rule is that memory is the binding constraint, and Phase 6's inline images were about to feed it. Capped at 64KB to match RC_MAX_FRAME, with growth past the cap refused and logged naming both the current size and the fragment that would not fit. The pre-existing HeapReAlloc-failure path now logs as well: dropping a fragment rather than leaking was the right call and stays, but it was doing it in silence. This buffer only holds appends that arrive before the document is ready, so it does not compete with the steady-state budget the way a permanent allocation would.
Fixed: 04.30 make lint WAS A VACUOUS GATE -- it greps win/ for RichEdit-3.0-only message names, and RichEdit was deleted from the project, so it could not fail. Kept rather than deleted, and the Makefile comment already says why: the guest could regain a RichEdit control (an input box, a dialog) and the trap would be live again the same day. But investigating it found something worse and newer than the audit's complaint. The grep said --include='*.c' --include='*.h', and the guest became C++ on 2026-09-18, so the trap had quietly stopped covering EVERY FILE IN THE TREE -- the comment's own claim that it goes live again the same day had become false, by my own migration, hours earlier. Fixed by adding --include='*.cpp' and recording the rule that any grep-based gate here must list it. Verified by demonstration rather than by reading: a file containing EM_SETTEXTEX was placed in win/app/ as .cpp, `make lint` failed on it, and it passes again with the file removed. Repointing it at Phase 6's hazards (a static oleacc/mshtml import, ReadDirectoryChangesW, VirtualAllocEx) is deliberately NOT done here -- that is inventing checks against code nobody has written, and 04.17 already notes a new gate will shift make check's CMOV baseline.
Deferred: 04.31 send_keys/click HAVE TWO CORRECTNESS GOTCHAS -- folded into BACKLOG §9 under 01.38 as implementation depth rather than filed as a second risk, since it is the same object. Both gotchas are recorded in full, including the sharp one: the own-window check must compare PROCESS ids, because a child control shares the UI thread and has a different HWND, so an HWND comparison misses exactly what it was written to catch.
Deferred: 04.32 THE MIRROR-PER-CLIENT DESIGN QUESTION IS UNANSWERED AND R7 CANNOT START WITHOUT IT -- ANSWERED by the user on 2026-09-18, and with a fourth option none of the three costed ones had: key the mirror to the CHAT rather than to the client. That bounds the number of mirrors by conversations instead of by clients times chats, so the combinatorial case all three options were trying to manage never arises. It keeps what was worth protecting: one mirror, one baseline and one guest still means a three-way reconcile, so the state table five adversarial review rounds went into is untouched -- no N-way merge, no writer lease to time out or steal, and the tree_id guard works as written. Two people work at once by working on different conversations. Default work folder is C:\Gaston\<shortened chat name>, with a different folder selectable when a chat is started; that UI is recorded as a Phase 4 item in PLAN.md so it is not rediscovered when the dialog is built. Two consequences are written down rather than left to be found: the guest's sync root stops being something it DECLARES in HELLO and becomes something the bridge TELLS it per conversation, and the path budget shrinks by the prefix -- C:\WORK\ is 8 characters against C:\Gaston\ plus a chat name, out of RC_CAP_MAXPATH's 259 and before RC_CAP_WORKRESERVE's 11 for .gaston-tmp -- so the shortened name needs a hard cap and paths.check must count the real prefix, or a long chat title silently costs the user deep paths. Recorded in PLAN.md §3.3 and BACKLOG §7; the R7 items are ordinary implementation work now rather than blocked.
Deferred: 04.33 CANCEL DOES NOT EXIST although PLAN.md mandates it for any transfer with a progress bar -- closed as a RECORDED DEFERRAL with a costed design, which is the honest disposition rather than building it. ICC_PROGRESS_CLASS is registered (that half was real and is done). CANCEL itself has no caller: every transfer that exists today is already interruptible by other means, and the one that genuinely needs it is Phase 5's attachment upload, which does not exist. Building it now would mean guessing its shape against no requirement -- and the audit's own 04.3 says there are already four ad-hoc chunked transfers with three more coming, so a fifth special case beside them is precisely the wrong move. BACKLOG §6 now carries the transfer primitive CANCEL belongs to, with the do_put-as-sink / do_get-as-source shape and a win/xfer.c target named, so when a caller appears the design is waiting rather than being invented under time pressure.
Deferred: 04.34 THERE IS STILL NO SHARED FRAME-DISPATCH TABLE -- recorded in BACKLOG §11 with the concrete shape the audit itself proposed (a shared FRAMES table plus two coverage tests, one per direction) rather than a vague "add a table". The specific bug it generalises, TOOLRESULT going unrendered for a whole phase, is closed; the structure that allowed it is not, and a test that enumerates both sides is what would close it.
Deferred: 04.35 guest.sess_id IS STILL ONE NAME FOR TWO CONCEPTS -- recorded in BACKLOG §7 with the R7 cluster, where the client_id / conv_id split belongs. The SYMPTOM it names, the chat list keeping the pre-fork conversation's id, was already fixed by a805df3; the field split is what remains, and it is gated on R7 rather than free-standing.
