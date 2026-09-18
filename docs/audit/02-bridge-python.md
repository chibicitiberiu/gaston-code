# 02 — Bridge audit: bridge/, architecture and code quality
#
# Audit date 2026-09-18. The original report is preserved below as
# context. Findings are the lines at the end: one per finding,
# prefixed "Fixed:" once the thing it describes has been dealt with.
#
## Audit 02 — `bridge/` architecture and code quality
#
# Scope: `bridge/*.py` only, 7,669 lines across 20 modules. Read against
# `docs/PLAN.md`, `docs/PROGRESS.md` and `docs/BACKLOG.md` as of 2026-09-18.
#
# This is not primarily a bug hunt, but bugs found on the way are in §8.
#
# The code is, on the whole, unusually well reasoned. Almost every non-obvious
# decision carries the measurement or the failure that produced it, and that is
# worth more than most structure. What follows is about the places where the
# structure has not kept up with the reasoning: where a decision that was once
# local has been copied to nine call sites, where a module's job has grown two
# extra jobs, and where something the comments describe confidently no longer
# happens.
#
# ---
#
### Top findings
#
# 1. **`Bridge` is 55 methods over 2,116 lines and holds nine unrelated
#    responsibilities.** The seam that pays for itself first is not sync or
#    permissions — it is the *wire*. 44 `_send` calls and 38 hand-built
#    `Frame(...)` literals mean the frame vocabulary is defined at the call site,
#    which is the same shape as the `rc_html_append` finding on the C side. §1.
#
# 2. **`GuestSession` is four objects wearing one dataclass**: a connection, a
#    view, a permission queue, and an RPC multiplexer. 30 fields, three separate
#    locks, two id namespaces. §2.
#
# 3. **Multi-client (R7) is blocked by seven pieces of per-guest state living on
#    `Bridge`**, not by `self._guest` being singular. Changing `_guest` to a dict
#    without moving `_early`, `guest_sync_root`, `_last_sync_reports`,
#    `_agent_notices`, `registry`, `sync_dir`/`sync_state` and the baseline lock
#    gives you two clients sharing one agent notice queue and one `base.json`.
#    §7.1.
#
# 4. **`sync_pending` entries are four different ad-hoc dict shapes keyed by one
#    id namespace, and `_sync_frame` indexes them without checking.** A `SYNCENT`
#    arriving against a push id raises `KeyError` out of `_handle`, out of
#    `_pump`, and tears down the connection. §3.2, §8.1.
#
# 5. **The bridge never schedules a sync pass.** `run_sync` has exactly one
#    production caller: the `SYNCDIRTY` handler. The docstring claims three
#    triggers. The 30-second cadence that the entire "say it once" machinery
#    (`first_report`, `reported_lines`, `aborted`) is designed around lives in
#    `win/rpc.c`, not here. §4.4.
#
# 6. **`STAT` and `PING` are handled and never sent.** The guest has no
#    keepalive and no throughput reporting, so `link_bps` is always `None` unless
#    `--link-bps` is passed. Every adaptive path in `compress.py` and
#    `coalesce.py` is running its unknown-link default in production. §6.2.
#
# 7. **With sync off, an idle guest is disconnected every 90 seconds**, and a
#    permission prompt left on screen for 90 seconds is denied — against a
#    documented 600-second deadline. §8.2.
#
# 8. **`Pass` does orchestration, policy, transport and disk I/O in one class**,
#    reaching into `bridge._send` six times and mutating `guest.sync_pending` and
#    `guest.sync_seq` directly. `note()` rewrites the whole baseline, with fsync
#    and a `.prev` copy, once per notice. §4.2.
#
# 9. **Five places build `base.json`'s path by hand**; three different code paths
#    report "could not open a session"; three different tables answer "which
#    argument of this tool is the interesting one". §3.1, §3.4.
#
# 10. **`paths._Default` is a second, drifted definition of `caps.Caps`**, kept
#     only to dodge an import cycle that would disappear if `_ACP_CODECS` moved.
#     §4.1.
#
# 11. **Dead or vestigial**: `replay.py` (known), four `protocol` frame
#     constructors, `paths.case_collision` and `paths.join_guest` (test-only,
#     with `scan_bridge` reimplementing the first inline), `caps.shortnames`
#     (parsed, validated, never read), `Tomb.reported`, `Entry.size`,
#     `Pass._guest_display`, `Pass.caps`/`Pass.guest_root` class attributes,
#     `PendingPrompt.expired`, `Replay.can_resume_from`, dead `key` parameters on
#     three `Pass` methods, and five stale `.bak` copies of bridge modules
#     sitting in the package directory. §6.
#
# 12. **`fakegui.py` no longer speaks the protocol it exists to conform to.** It
#     does not inflate `enc=deflate`, does not handle `DOC`/`PAGESTART`/`HALT`/
#     any sync or exec frame, and renders `TEXT` by a `style=` header the bridge
#     stopped sending. The stated break-glass tool would show garbage. §6.4.
#
# ---
#
### 1. `bridge/server.py` — the god object
#
# `bridge/server.py:149-2166`. One class, 55 methods, holding:
#
# | # | Responsibility | Lines |
# |---|---|---|
# | 1 | TCP accept, connection lifecycle, graceful teardown | 211-303 |
# | 2 | HELLO parse, version/cap/secret/caps negotiation, attach/resume | 305-451 |
# | 3 | Claude session construction, switch, new, effort-fork | 455-530, 1968-2074 |
# | 4 | Permission prompt queue, deadline, reissue, reply | 534-740, 1830-1865 |
# | 5 | Transcript rendering: coalescers, HtmlStream, paginator, page view | 752-864, 2076-2103 |
# | 6 | Frame dispatch (17 inbound types in one `if/elif` chain) | 885-1025 |
# | 7 | Turn orchestration and the sync preamble | 1027-1113 |
# | 8 | `exec` RPC: id allocation, output accumulation, status vocabulary, kill | 1117-1203, 1737-1828 |
# | 9 | Sync orchestration, halt/latch state, baseline I/O, agent-facing status text | 1205-1632, 1634-1727 |
# | 10 | Model/effort menus | 1913-2000 |
# | 11 | Framing, seq stamping, chunking, compression, send lock | 2107-2166 |
# | 12 | Idle reaper | 1867-1911 |
# | 13 | CLI, argument validation, sync-state placement rules | 2184-2261 |
#
#### 1.1 The seam that pays first: a `GuestLink` that owns the frame vocabulary
#
# **Problem.** `grep -c "self._send(" bridge/server.py` → 44.
# `grep -c "Frame(" bridge/server.py` → 38. Every outbound frame is composed
# where it is sent. So the protocol's shape is spread across eleven
# responsibilities, and three separate decisions are re-made at each site:
#
# * **`stamp=` is a per-call-site judgement.** `stamp=False` appears on READY,
#   RESYNC, replayed frames, HALT, HALTEND, EXEC, EXECKILL, PONG, the unknown-type
#   error, and every sync frame in `syncrun.py`. `stamp=True` (the default) on
#   everything else. The rule is real and load-bearing — `server.py:1155-1158`
#   explains that a replayed `EXEC` would re-run `del c:\temp\*` — but it is
#   encoded as "remember to pass the flag", not as a property of the frame type.
#   A new sync or exec frame added by Phase 6 defaults to the dangerous value.
# * **Header values are stringified by hand, every time.** `str(len(...))`,
#   `"1" if x else "0"`, `f"{cost:.4f}"`. Fourteen `"1" if ... else "0"`
#   expressions.
# * **Body encoding is per site.** Some bodies go through `fold.encode`
#   (`server.py:439`, `624`, `659`, `1160`, `2044`), some are raw bytes
#   (`syncrun.py:345`), and `paths.py`'s rule 2 — paths never go through `fold` —
#   is enforced by each caller remembering it rather than by the type.
#
# **Consequence.** Adding a frame type means picking three defaults correctly with
# nothing to check you against, and the wrong `stamp` is silent until a reconnect
# re-delivers it. It also makes the protocol un-enumerable: there is no single
# place that lists what the bridge can send, which is why `protocol.py`'s
# constructor functions (§6.1) rotted without anyone noticing.
#
# **Fix.** A `GuestLink` class owning one writer, the send lock, the compressor,
# the seq stamp and the chunking — plus one typed constructor per frame, in
# `protocol.py` where four already live. The constructor decides `stamp`,
# encoding and header formatting; the caller passes values.
#
# ```python
# class GuestLink:
#     async def send(self, frame: Frame) -> None: ...        # stamp from frame type
#     async def send_html(self, html, mode, page) -> None: ...  # fold/compress/chunk
#     async def send_raw(self, frame) -> None: ...           # pre-attach, no guest
# ```
#
# `Bridge._send`, `_send_html`, `_try_send_raw` and `wire_yield` move onto it
# whole. The 38 `Frame(...)` literals become named calls. This is the single
# change that most reduces `server.py` and it touches nothing else's logic.
#
#### 1.2 `HelloNegotiator` / `handshake.py`
#
# `server.py:305-451` does five separable things: read the first frame under a
# deadline, check version and frame cap, compare the secret, parse and validate
# caps, and then *attach* — create or resume a `GuestSession`, reset the
# compressor, scale the page limits, build the Claude session, send READY, DOC,
# options and three reissues.
#
# **Consequence.** `_attach` is 115 lines and is the only place that knows the
# correct order of eleven post-connect actions. It is also where multi-client
# will need the most surgery (§7.1), and it currently returns `None` for six
# unrelated reasons with the refusal frame built inline each time.
#
# **Fix.** `bridge/handshake.py`:
#
# ```python
# @dataclass
# class Handshake:
#     caps: Caps
#     page_bytes: int
#     page_blocks: int
#     hello: dict
#
# def negotiate(headers: dict, secret: str) -> Handshake      # raises Refused(code, msg)
# ```
#
# Pure, no I/O, directly unit-testable — today the six refusal paths are only
# reachable through a real socket in `test_server_integration.py`. `_attach`
# keeps only the session lifecycle and the reissue sequence, and shrinks to about
# forty lines.
#
#### 1.3 `PermissionDesk`
#
# `server.py:534-740` plus `1830-1865`, ~250 lines: build the `can_use_tool`
# callback, enqueue/show/reissue/refresh/finish, pump the queue, deny-all, and
# translate a `PERMREPLY` action into a `PermissionResult`.
#
# This is already a coherent object with a clear interface. It owns
# `perm_pending` and `perm_queue`, and every other reader of those fields is
# either the reaper (`1882`) or `wire_yield` (`2158`) asking one question: "is
# there a prompt on screen?"
#
# **Fix.** `bridge/permdesk.py`:
#
# ```python
# class PermissionDesk:
#     def callback(self):                  -> can_use_tool
#     async def reply(self, id, action)
#     async def reissue(self)              # after reconnect
#     def deny_all(self, reason, message)  # sync, callable from a cancelled scope
#     @property
#     def busy(self) -> bool               # what wire_yield and the reaper want
# ```
#
# `permissions.py` already holds the policy (`off_limits`, `allows_always`,
# `session_scoped`, rendering); this is the missing state machine that sits on top
# of it. Note the rule that must survive the move: `_finish_prompt` and
# `_resolve_all` are deliberately synchronous because they run inside a cancelled
# scope (`server.py:591-599`, `690-701`).
#
#### 1.4 `SyncCoordinator`
#
# `server.py:1205-1727` is 520 lines and is not the sync *algorithm* — that is
# `sync.py` and `syncrun.py`. It is the part that decides when a pass runs, what
# the user is told, what the agent is told, when the halt bar goes up, and who may
# write `base.json`. It has no business sharing a class with frame dispatch.
#
# **Fix.** `bridge/synccoord.py` owning `sync_dir`, `sync_state`,
# `guest_sync_root`, `_last_sync_reports`, `_agent_notices`, the baseline lock,
# and the halt/latch pair. Interface:
#
# ```python
# class SyncCoordinator:
#     async def run_pass(self, why: str) -> PassResult
#     def status_text(self) -> str                 # the mcp__machine__sync_status body
#     async def resolve(self, path, action) -> str
#     async def on_continue(self, answered: frozenset) -> int
#     def take_agent_notices(self) -> list[str]    # what _with_sync_preamble wants
# ```
#
# This is also the piece R7 needs to become per-client (§7.1), so the split is
# work you will have to do anyway.
#
#### 1.5 `ExecChannel`
#
# `server.py:1117-1203` and `1737-1828`. Id allocation, the one-at-a-time lock,
# the 64KB output cap, the status→prose table, and the kill path. Roughly 190
# lines with one entry point (`run_on_guest`) and two frame handlers. Moves out
# cleanly onto a class holding `exec_pending`, `exec_seq`, `exec_lock`.
#
# Worth doing before Phase 6, which adds `screenshot`, `click`, `send_keys` and
# `send_action_sequence` — every one of which is the same
# allocate-id / send / await-future / handle-reply / kill-on-cancel shape. See §7.3.
#
#### 1.6 The dispatch chain
#
# `server.py:885-1025`, 17 branches in one `if/elif`, mixing one-line handlers
# (`PING`, `STAT`) with 25-line ones (`RESUME`, `GETSESSIONS`, `INTERRUPT`).
#
# **Fix.** A `dict[str, handler]` built once, populated by each extracted object
# (`PermissionDesk` registers `PERMREPLY`; `ExecChannel` registers `EXECOUT`,
# `EXECRESULT`; `SyncCoordinator` registers `SYNCDIRTY` and the seven sync
# replies). `_handle` becomes four lines plus the unknown-type fallback. This is
# what makes "Phase 6 adds many more tools" a local change.
#
# ---
#
### 2. `GuestSession` — one thing or several?
#
# `bridge/server.py:58-146`. 30 fields, three locks, two id counters.
#
# Grouped by who reads them:
#
# | Group | Fields | Who touches them |
# |---|---|---|
# | **Link** | `writer`, `send_lock`, `zip_out`, `link_bps`, `replay`, `last_seen`, `lost_at`, `connected` | `_send`, `_on_client`, `_reap_idle` |
# | **Identity** | `sess_id`, `hello`, `claude`, `model`, `effort` | `_attach`, `_make_session`, `_send_options` |
# | **View** | `pages`, `render`, `coalescers`, `view_page`, `page_bytes`, `page_blocks` | `_write_block`, `_send_page`, `_new_session`, `_switch` |
# | **Prompts** | `perm_pending`, `perm_queue` | the permission methods, plus `wire_yield` and the reaper |
# | **Halt** | `halt_id`, `halt_paths`, `halt_text` | `_raise_halt`, `_clear_halt`, the RESUME branch |
# | **Exec RPC** | `exec_pending`, `exec_seq`, `exec_lock` | `run_on_guest`, `_exec_out`, `_exec_result`, `_fail_execs` |
# | **Sync RPC** | `sync_pending`, `sync_seq`, `sync_lock`, `sync_active` | `Pass` (directly), `_sync_frame`, `_fail_syncs`, `_baseline_lock` |
#
# Seven groups, and the only field read across more than two of them is
# `connected`.
#
# **The tell that it is not one object:** three locks with three different scopes
# on one dataclass. `send_lock` protects a socket, `exec_lock` protects a resource
# on the *other* machine (the guest's single worker thread), and `sync_lock`
# protects a *file on this machine* (`base.json`) — and is reached through
# `Bridge._baseline_lock()` (`server.py:1547-1556`), whose docstring admits the
# placement is opportunistic: "It lives on the guest because that is where `Pass`
# takes it." That is the wrong reason, and §5.3 shows what it costs.
#
# **What splitting looks like.** Keep `GuestSession` as the *composition root* —
# one per connected machine, holding the pieces — and give each group its own
# type:
#
# ```python
# @dataclass
# class GuestSession:
#     sess_id: str
#     hello: dict
#     caps: Caps                 # parsed once at handshake, not re-derived (§5.4)
#     link: GuestLink            # writer, send_lock, zip_out, link_bps, replay, liveness
#     view: TranscriptView       # pages, render, coalescers, view_page, limits
#     claude: Session | None
#     model: str
#     effort: str
#     perms: PermissionDesk
#     execs: ExecChannel
#     sync: GuestSyncState       # sync_pending, sync_seq, sync_active
# ```
#
# Two of these have a real invariant to enforce that is currently spread across
# call sites:
#
# * **`TranscriptView`** is the direct analogue of the `rc_html_append` finding.
#   `_write_block` (`server.py:802-826`) is the only method that knows the rule
#   "record into the paginator, stream only if the guest is on the live page, then
#   emit PAGESEALED for anything sealed" — and `_switch` (`2064-2067`) bypasses it
#   entirely, calling `pages.add` and `message_boundary` by hand while replaying
#   history. `_new_session` (`2008-2014`) and `_switch` (`2050-2055`) each rebuild
#   the paginator, renderer and coalescers with their own copy of the default
#   limits, including the vestigial `guest.page_bytes or None or 16384` at
#   `server.py:2050`. A `TranscriptView.reset()` / `.write(html, stream=)` /
#   `.show_page(n)` gives one owner for the scroll-and-seal policy.
#
# * **`GuestSyncState`** should own `_next_id`, which currently lives on `Pass`
#   (`syncrun.py:494-496`) and reaches back to mutate `guest.sync_seq`.
#
# The halt trio is not really guest state at all; it is the *display* of a
# coordinator decision, and §5.2 argues it belongs with the latch set it answers
# for.
#
# ---
#
### 3. Duplication and inconsistency
#
#### 3.1 Five ways to spell one path
#
# ```
# server.py:1329   state = os.path.join(self.sync_state, "base.json")
# server.py:1365   return Baseline.load(os.path.join(self.sync_state, "base.json"))
# server.py:1516   state = os.path.join(self.sync_state, "base.json")
# server.py:1599   base.save(os.path.join(self.sync_state, "base.json"))
# syncrun.py:70    self.state = os.path.join(state_dir, "base.json")
# ```
#
# Plus `Pass.base_dir` (`syncrun.py:638-640`) derives `<state>/base` from
# `os.path.dirname(self.state)`, i.e. from the joined-then-split form.
#
# **Consequence.** The baseline's location is a fact with five authors. It is also
# the one file the design says must be outside both `--sync-dir` and `--cwd`
# (`server.py:2231-2254`), and that check runs in `main()` against
# `bridge.sync_state` — not against any of the five constructions.
#
# **Fix.** A `BaselineStore` (or just a `SyncState` value object) owning
# `root`, `.json_path`, `.ancestor_dir`, `load()`, `save()`, and the placement
# validation currently in `main()`. `Pass` takes one instead of a `state_dir`
# string.
#
#### 3.2 Ad-hoc dicts used as records
#
# **`exec_pending` entries** (`server.py:1145`) are `{"future", "out"}` at
# creation, grown with `"killed"` (`1200`) and `"capped"` (`1743`, `1747`) by
# other methods. Every read is `entry.get("capped")` / `entry.get("killed")` —
# `.get` because the key may not exist yet. A dataclass makes both booleans
# `False` by construction and removes four `.get` calls.
#
# **`sync_pending` entries** are worse: **four different shapes in one id
# namespace**.
#
# ```
# syncrun.py:128   {"future", "rows", "display"}      # SYNCSCAN
# syncrun.py:335   {"future"}                         # SYNCPUT
# syncrun.py:403   {"future"}                         # SYNCGET  (+ "data" grown at server.py:1708)
# syncrun.py:478   {"future"}                         # SYNCMKDIR
# ```
#
# `_sync_frame` (`server.py:1634-1716`) dispatches on **frame type**, not on what
# the entry is, and indexes without checking:
#
# ```python
# entry["rows"][key] = SyncEntry(...)        # :1682 — KeyError on a push entry
# entry["display"][key] = rel                # :1690
# fut.set_result((..., entry["rows"], entry["display"]))   # :1699
# ```
#
# And the four futures resolve to four differently-shaped tuples (6-tuple, 2-tuple,
# 2-tuple, 4-tuple) that the four `Pass` methods unpack positionally. The backlog
# already records the consequence from the other direction: `reject_sync_busy` on
# the guest answers every full-queue sync frame with `SYNCPUTACK`, so SCAN and GET
# get a 2-tuple where they unpack six or four, surfacing as "sync pass aborted:
# not enough values to unpack". That is the same defect seen from the guest; the
# bridge-side half is that nothing here validates.
#
# **Fix.** One `SyncCall` base with four subclasses, each owning its own
# `on_frame(frame)` and resolving a named result:
#
# ```python
# @dataclass
# class ScanCall(SyncCall):
#     rows: dict[str, Entry] = field(default_factory=dict)
#     display: dict[str, str] = field(default_factory=dict)
#     def on_frame(self, f): ...              # SYNCENT / SYNCEND, ignores the rest
#     # resolves ScanResult(status, files, tree_id, root, rows, display)
# ```
#
# `_sync_frame` becomes `guest.sync.call(sid).on_frame(f)` with an unknown-type
# branch that logs instead of raising. The four positional tuples become four
# result dataclasses, which also removes the "not enough values to unpack" failure
# mode entirely rather than fixing one instance of it.
#
#### 3.3 Status-string vocabularies
#
# Three independent vocabularies, each a bare literal at the point of use:
#
# * **exec statuses** — `server.py:1780-1810`: `spawn_failed`, `too_long`,
#   `timeout`, `busy`, `cancelled`, plus `ok` implied. The `isError` set is
#   re-listed at `1809-1810` as a second literal tuple, so adding a status means
#   editing two places six lines apart.
# * **sync statuses** — `syncrun.py:137` (`!= "ok"`), `:368`, `:412`,
#   `:487` (`in ("ok", "exists")`).
# * **prompt end reasons** — `"timeout"` (`server.py:585`), `"cancelled"`
#   (`:598`), `"answered"` (`:1864`), and `_resolve_all`'s free-text `reason`
#   (`"rebuild"`, `"gone"`, `"idle"`) which is *only* logged, never sent — two
#   parameters called `reason` with different audiences.
#
# `tests/python/test_guest_agreement.py` exists to keep bridge and guest constants
# in step; these strings are not in it.
#
# **Fix.** A `bridge/status.py` with `class ExecStatus(str, Enum)` and
# `SyncStatus(str, Enum)`, and the prose table as a dict keyed by the enum.
# Extend `test_guest_agreement.py` to diff the enum members against the `#define`s
# in `win/rpc.h`/`win/syncrpc.h`, which is exactly the "two numbers in two
# languages describing one rule" pattern `synccheck --limits` already uses for the
# path caps.
#
#### 3.4 Three tables answering "which argument matters"
#
# ```
# server.py:2178      ("command", "file_path", "pattern", "path", "url", "prompt")
# permissions.py:51   ("command", "file_path", "path", "pattern", "url", "prompt",
#                      "old_string", "new_string", "content")
# permissions.py:150  ("command", "file_path", "path", "url", "pattern")
# permissions.py:199  ("command", "file_path", "path", "url", "pattern")
# ```
#
# Four lists, three orderings, one question. `server._summarise` feeds the tool
# line in the transcript; the others feed the permission block and the bar. A tool
# added in Phase 6 with a `selector` or `region` argument needs all four edited,
# and the symptom of missing one is a bar that says "Click" with no coordinates —
# `permissions.py:188-192` records that this exact failure already shipped once
# for `exec`.
#
# **Fix.** One `permissions.telling_argument(args)` and one `PREFERRED` tuple.
# `server._summarise` calls it.
#
#### 3.5 Error handling that varies by call site
#
# Three ways to report "could not open a Claude session", all reachable from the
# same menu:
#
# ```
# server.py:2020   await self._send(guest, error("new_failed",  str(exc)[:200]))   # ERROR frame
# server.py:2061   await self._send(guest, error("resume_failed", str(exc)[:200])) # ERROR frame
# server.py:1995   await self._write_block(guest, H.error("Could not reopen..."))  # transcript block
# ```
#
# The guest renders an ERROR frame in one place and a transcript block in another,
# so a failed effort-change looks different from a failed switch for no reason
# anyone chose.
#
# Similarly, `_send` swallows an oversized frame (`server.py:2122-2126`) and
# returns normally, so every caller believes it sent. And `_send` on a closed
# writer returns with the comment "buffered in replay; the guest will ask for it"
# (`server.py:2133`) — which is false, because replay is dead (§6.1). Those frames
# are simply lost. `_reissue_prompt`/`_reissue_halt`/`_reissue_turn` exist to paper
# over exactly this, one frame type at a time.
#
# **Fix.** `_send` returns a bool or raises; the caller decides. And pick one
# channel for user-visible failures — `H.error` into the transcript is the better
# one, because it paginates and survives a reconnect, which is precisely what the
# ERROR frame does not.
#
#### 3.6 Smaller repetitions
#
# * `for c in guest.coalescers.values(): c.set_link_speed(...)` at `:407-408`
#   and `:998-999`; `for c in ...: await c.stop(); guest.coalescers.clear()` at
#   `:2011-2013` and `:2053-2055`. All four belong on `TranscriptView`.
# * Fourteen `"1" if x else "0"` expressions across the frame literals.
# * `getattr(prompt, "summary", prompt.tool_name)` at `:624` and `:638` —
#   `summary` is not a declared field of `PendingPrompt`; it is attached
#   dynamically at `server.py:578`. Add the field.
# * `attrs.load` walks the tree, then calls `_sources` which walks it *again*
#   (`attrs.py:180-199`). With `ignores.load`'s walk and `scan_bridge`'s walk, a
#   pass does four full traversals; `sync_status_text:1410` adds a fifth on
#   demand.
#
# ---
#
### 4. Layering: `sync.py` / `syncrun.py` / `paths.py` / `attrs.py` / `ignores.py` / `caps.py`
#
# The intended stack is clean and mostly holds:
#
# ```
# caps.py ──┐
# paths.py ─┴─► sync.py (pure decisions) ──► syncrun.py (apply) ──► server.py
# attrs.py ─┘                                      │
# ignores.py ──────────────────────────────────────┘
# ```
#
# `sync.py` is the best module here. `diff()` is a pure function over three dicts,
# exhaustively enumerated, with every row's reasoning stated. `attrs.py` and
# `ignores.py` are cohesive and testable. Three problems.
#
#### 4.1 `paths._Default` is a drifted second `Caps`
#
# `paths.py:75-102` defines a duck-typed stand-in for `caps.Caps`, with the
# comment: "Duck-typed rather than imported from `caps`, because `caps` imports
# THIS module for the codepage table and a cycle here would be gratuitous."
#
# The cycle is real but the remedy is wrong. `_Default` has already drifted: it
# lacks `shortnames`, `manifest`, `declared` and `summary()`, and it duplicates
# `maxdirpath`/`maxfilepath` as a second implementation of the same two
# properties. Anything reading `DEFAULT.manifest` — which `_apply_attrs` does
# through `self.caps` — would raise; it does not today only because `Pass` always
# sets a real `Caps` in `__init__` (which also makes the `Pass.caps` class
# attribute unreachable, §6.3).
#
# **Consequence.** Two definitions of "what the client is", one of which is
# silently incomplete, and a `check()`/`canonical()` signature that accepts either.
#
# **Fix.** Move `_ACP_CODECS` and `codec_for_acp` out of `paths.py` — they are
# codepage knowledge, not path knowledge. Either into `caps.py` (which is their
# only caller, `caps.py:215`) or into a two-function `bridge/codepages.py`. Then
# `paths.py` imports `caps`, `DEFAULT` becomes `caps.WIN9X`, and `_Default` is
# deleted. Roughly forty lines move and one class disappears.
#
#### 4.2 `Pass` does orchestration, policy, transport and I/O
#
# `syncrun.py:53-960`. One class, 907 lines, and it holds:
#
# * **Transport** — four near-identical request/await/cleanup blocks
#   (`scan_guest:126-135`, `push:333-366`, `pull:401-410`, `mkdir:477-485`), each
#   allocating an id, installing a raw dict in `guest.sync_pending`, calling
#   `bridge._send`, `wait_for`ing a positional tuple, and popping in a `finally`.
#   Six reaches into `bridge._send` and two into `bridge.wire_yield`, all private.
# * **Policy** — `_ignores` (scope-change bookkeeping), `_apply_attrs`
#   (re-classification), `first_report` (the announce-once fingerprint).
# * **I/O** — `scan_bridge` (the walk and the CRC cache), `stage_conflict`
#   (`shutil.copytree`), `keep_ancestor`, `note`, `notices_delivered`.
# * **Orchestration** — `run()`, a 275-line method with an 11-branch apply chain.
#
# **Consequences, concretely:**
#
# * **`Pass.__init__` does I/O and can raise.** It loads the baseline
#   (`syncrun.py:71`) and raises `SyncError` for a caps refusal (`:78`) or a
#   re-keying refusal (`:88`). So constructing a `Pass` is a side-effecting
#   operation, and the two refusals that most need testing are only reachable by
#   building one.
# * **`note()` rewrites the entire baseline per notice** (`syncrun.py:599-600`),
#   and `Baseline.save` is not cheap: `json.dumps(..., indent=1)` over `rows`,
#   `prev`, `ignored`, `tombs`, `reported`, `reported_lines` **and `scan_cache`**,
#   then `fsync`, then a rename of the old file to `.prev`, then a rename in.
#   `first_report` + `note` fire once per standing divergence, so a tree with
#   twenty unfixable names does twenty full serialisations of the whole baseline
#   on the pass that first sees them. The durability argument for `note` is sound;
#   the granularity is not.
# * **`run()` cannot be tested per-action.** `test_sync_wire.py` is 1,618 lines,
#   and `PROGRESS.md` records that two of its tests passed with their fix
#   reverted because they asserted on counters inside `Pass` while the fix lived
#   in `run_sync`. That is the shape a 275-line orchestrator produces.
#
# **Fix, in order of payoff:**
#
# 1. **Extract the transport.** `SyncTransport` (or the `SyncCall` objects from
#    §3.2) with `scan()`, `put()`, `get()`, `mkdir()`. It owns the id namespace,
#    the `sync_pending` map and the `GuestLink`. `Pass` stops importing `Frame`
#    and stops knowing `bridge._send` exists. This alone removes ~90 lines of
#    fourfold-duplicated boilerplate and is what makes `Pass` testable against a
#    fake transport instead of a fake socket.
# 2. **Extract `BridgeScanner`** — `scan_bridge` plus the cache-write gate
#    (`syncrun.py:181-308`, 128 lines). It is a pure `(root, ignore, attrs, caps,
#    cache) -> (rows, reports, fresh)` function today in everything but form.
# 3. **Make the apply chain a dispatch table.** `run()`'s eleven `elif dec.act is
#    S.Act.X` branches become `_apply[dec.act](dec, g, b)`. The `else` branch at
#    `:917-928` — "an action with no branch used to fall off the end of this
#    chain and vanish", which is how MKDIR was lost — becomes a `KeyError` the
#    table cannot silently produce.
# 4. **Move the reporter out.** `first_report`, `note`, `notices_delivered`,
#    `reports` and `reported_lines` are one object: "what has already been said,
#    to whom". Today it is spread across `Pass`, `Baseline` and
#    `Bridge._agent_notices`/`_last_sync_reports` (§5.1).
#
#### 4.3 The `Bridge`↔`Pass` interface is one object passed whole
#
# `Pass(self, guest, self.sync_dir, self.sync_state)` (`server.py:1224`). `Pass`
# receives the entire `Bridge` and the entire `GuestSession` and uses four things:
# `bridge._send`, `bridge.wire_yield`, `guest.sync_pending`, `guest.sync_seq`.
#
# **Fix.** Pass the transport and the caps, nothing else. That also makes the
# `getattr(guest, "hello", {}) or {}` defensive access at `syncrun.py:76` — a
# sign that `Pass` is not sure what it was handed — unnecessary.
#
#### 4.4 Nobody schedules a pass
#
# `run_sync`'s docstring (`server.py:1205-1212`): "Three triggers — the guest's
# watcher, a periodic sweep, and the agent asking."
#
# Production callers of `run_sync`: **one**, `server.py:932`, the `SYNCDIRTY`
# handler. There is no periodic sweep on the bridge, and `mcp__machine__sync_status`
# calls `sync_status_text()` (`tools.py:122`), which reads the baseline and does
# not run a pass. The remaining callers are all in `test_sync_wire.py`.
#
# The 30-second cadence exists, but it is `RPC_SWEEP_MS` in `win/rpc.c:78`, sent
# as `SYNCDIRTY` whether or not anything changed. So:
#
# * **The comments are wrong in a way that misleads.** `first_report`'s
#   "this pass runs every thirty seconds" (`syncrun.py:750-752`), `_report_abort`'s
#   "a pass runs every thirty seconds" (`server.py:1318-1321`) and `run_sync`'s
#   own "coalescing" note all describe a bridge-side timer that does not exist.
#   All of the announce-once machinery is correct, but its justification lives in
#   the other binary.
# * **The scheduling authority is inverted.** The standing rule is that the guest
#   reconciles nothing and the bridge owns the baseline and decides. The bridge
#   does decide *what* — and the guest decides *when*, including whether a pass
#   happens at all: `rpc_maybe_nudge` returns early if `rc_sync_root()` is empty
#   (`win/rpc.c:93`). A bridge configured with `--sync-dir` against a guest whose
#   INI has no sync root will never run a pass and will never say so.
# * **Nothing syncs at attach.** `_attach` does not trigger a pass, so the first
#   reconciliation after a connect waits for the guest's sweep.
# * **`run_sync` returns three different shapes** — `{}` when there is no guest or
#   a pass is already running, `{"error": ...}` on failure, the full `done` dict
#   on success — and the sole production caller is `self._spawn(self.run_sync(...))`,
#   which discards it. The return value is test-only API.
#
# **Fix.** Put the sweep on the bridge (`SyncCoordinator` owning an
# `asyncio.Task` alongside the reaper), treat `SYNCDIRTY` as a *hint that
# shortens the next interval* rather than the trigger, run one pass at attach, and
# make `run_sync` return a `PassResult` dataclass or `None`.
#
# ---
#
### 5. State in two places
#
#### 5.1 Notices: `base.pending_notice` (disk) vs `Bridge._agent_notices` (memory)
#
# `server.py:199-203` states the design: the user's copy is cleared when it reaches
# their screen, the agent's when it is handed to the model, and neither implies the
# other. That is right. What is not stated is that **one is durable and the other
# is not**.
#
# `run_sync:1242-1248` appends each notice to both. `p.notices_delivered(delivered)`
# (`:1286`) clears the durable copy. `_with_sync_preamble:1036` clears the memory
# copy. Restart the bridge between a pass and the next turn and the agent's copy is
# gone with no trace, while the user's has already been cleared because it reached
# the screen. The news the agent most needs — a conflict that latched its writes —
# is the one that does not survive.
#
# **Fix.** Two durable queues on the baseline: `pending_notice_user` and
# `pending_notice_agent`, each cleared by its own delivery. The reporter object
# from §4.2.4 owns both.
#
#### 5.2 Latches: `base.latched` (disk) vs `guest.halt_id`/`halt_paths` (memory)
#
# `_reissue_halt` (`server.py:640-660`) exists precisely because this pair got out
# of step across a *reconnect*, and its docstring describes the resulting state
# well: "a guest that restarted while a halt was up came back with NO BAR, a
# bridge still holding the halt, and every latched path refusing the agent's
# writes — unanswerable."
#
# **The same hole is still open across a bridge restart.** `base.latched` persists;
# `halt_id` does not. Nothing at startup or at attach reads `base.latched` and
# raises a halt for it. `_raise_halt` is only called from `run_sync:1278` on
# `got["new_conflicts"]`, and a conflict that already latched will not be
# re-decided — the baseline advanced past it. So after a bridge restart with
# latched paths: the agent's writes are refused, the user has no bar, and the only
# route out is the agent noticing via `sync_status` and calling `sync_resolve`.
#
# **Fix.** The halt is a *view* of the latch set, not independent state. Derive it:
# `SyncCoordinator.reconcile_halt()` called at attach and at the end of every pass,
# raising a bar if `base.latched` is non-empty and none is showing, clearing it if
# `base.latched` is empty. `halt_paths` becomes "the subset the current bar was
# raised for", which is the only part that genuinely cannot be derived.
#
#### 5.3 The baseline lock lives on the wrong object
#
# `_baseline_lock()` (`server.py:1547-1556`) returns `guest.sync_lock`, or a null
# context when there is no guest. The docstring: "with no guest there is nothing to
# serialise against and a null context is not a shortcut, it is the accurate
# answer."
#
# That reasoning holds only while `Pass` is the sole writer *and* there is one
# guest. Neither survives contact with §7.1: two guests means two `sync_lock`s and
# one `base.json`, so `sync_resolve` from client A's agent races a `Pass` for
# client B with no lock between them. `Baseline.save` is atomic per write, so you
# lose a whole update rather than corrupting the file — the failure that
# `_release_all_latches`'s docstring already describes ("a release that lands in
# between was simply overwritten a moment later: latches restored, no bar up to
# say so, and no notice").
#
# **Fix.** The lock belongs to the `BaselineStore` from §3.1, one per baseline
# file. `Pass` takes the store, not a path.
#
#### 5.4 `caps` is re-parsed from `hello` at three sites
#
# ```
# server.py:383    C.from_hello(h).summary()        # attach, for the log; result discarded
# server.py:1514   C.from_hello(self._guest.hello if self._guest else {})
# server.py:1648   C.from_hello(guest.hello)        # per SYNCENT frame
# syncrun.py:76    C.from_hello(getattr(guest, "hello", {}) or {})
# ```
#
# `from_hello` raises `CapsError`. At `:383` the refusal is handled; at `:1648`
# and `:1514` it is not, and `:1648` is inside a per-frame loop. `:1514` is worse
# than a re-parse: with no guest connected it passes `{}`, silently getting the
# Win9x defaults, so `sync_resolve` canonicalises the agent's path under assumed
# rules rather than the client's.
#
# **Fix.** Parse once in `negotiate()` (§1.2) and store `guest.caps`. The four
# sites read a field. `Pass.caps` stops being re-derived and its class-attribute
# fallback can go.
#
#### 5.5 `Bridge.model`/`effort` vs `GuestSession.model`/`effort`
#
# `Bridge.model` (`:172`) is the CLI default; `guest.model` (`:75`) is the menu
# choice. `_make_session` reads `guest.model or self.model` (`:508`). But
# `_send_options` reads **only** `guest.model`:
#
# ```python
# in_use = resolved_of(guest.model or "default")                 # :1933
# current = (m.get("resolvedModel") or value) == in_use          # :1941
# "current": "1" if lvl == (guest.effort or "") else "0"         # :1964
# ```
#
# **Consequence (a real bug).** Start the bridge with `--model claude-opus-5`; the
# session runs on Opus, and the Model menu ticks whatever `default` resolves to.
# Same for `--effort`: the session uses it, the Effort menu shows nothing ticked.
#
# **Fix.** One accessor — `guest.effective_model` / `effective_effort` — or seed
# `guest.model`/`guest.effort` from the CLI values at `_attach`. The latter is
# simpler and also makes R7's per-client model a pure `GuestSession` concern.
#
#### 5.6 `sess_id` vs `claude.session_id`
#
# `guest.sess_id` starts as a `uuid4` (`:398`) and is sent in READY. Then
# `_new_session:2023` and `_set_effort:1999` overwrite it with the SDK's session
# id, while `_switch` does **not** (compare `:2023` against `:2070`, which sends
# `"id": session_id` but leaves `sess_id` alone). So `sess_id` means "a bridge
# handle" on a fresh connection and "the SDK session" after a new-session or a
# fork, and is stale after a switch. Nothing depends on it today beyond the READY
# header, which is why it has drifted.
#
# **Fix.** Pick one meaning. R7 wants a stable per-client handle for the HELLO
# `client_id`, so keep `sess_id` as the bridge handle and never overwrite it; the
# SDK id already travels in `SWITCHED` and `READY`'s `model`/registry path.
#
#### 5.7 `view_page` survives a reconnect
#
# `_attach` resets `zip_out` and `lost_at` but not `view_page`. A guest that
# reconnects while paged back keeps `view_page` set bridge-side, so `_write_block`
# (`:821`) keeps buffering silently. The backlog already records that the guest
# never re-requests page content on reconnect, so the two halves compound: the
# transcript is frozen and the bridge thinks that is correct.
#
# ---
#
### 6. Dead and vestigial
#
#### 6.1 `bridge/replay.py` and its dependants
#
# Known-dead (BACKLOG §5). Three things hang off it that are worth recording
# together, because retiring replay touches all of them:
#
# * `Replay.can_resume_from` (`replay.py:79`) — test-only.
# * The `RESYNC` frame (`server.py:428`) — the guest has no handler
#   (`win/main.c:575-871`).
# * `_send`'s size probe (`server.py:2118-2126`) stamps a fake ten-digit `seq`
#   into the encode budget of **every** frame, including `stamp=False` ones that
#   will never carry a seq. Its justification — "the replay buffer is append-only,
#   so one oversized frame poisons every future replay" — is entirely about
#   replay. The 64KB cap check is still worth having; the reason has to be
#   restated, not preserved.
#
# **Also dead in `protocol.py`:** `text()` (`:206`, test-only — and its `style=`
# parameter is a frame shape the bridge no longer sends), `user_input()` (`:216`),
# `status()` (`:220`) and `interrupt()` (`:225`) have **no callers at all**.
# `hello()` is used by `fakegui` and the tests. So of six constructor functions,
# one is live.
#
#### 6.2 `STAT`, `PING` and the adaptive-link machinery
#
# Neither frame exists in the guest: `grep -c '"PING"' win/*.c` → 0, `"STAT"` → 0,
# and the guest has no keepalive (`rc_net_send` callers in `win/*.c` are all
# event-driven). So:
#
# * `_handle`'s `PING` branch (`server.py:1002-1003`) is unreachable, and
#   `_reap_idle`'s docstring premise — "The guest pings every few seconds"
#   (`:186`) — is false.
# * `_handle`'s `STAT` branch (`:988-1000`) is unreachable, so `guest.link_bps` is
#   `self.link_bps`, i.e. `None` unless `--link-bps` is passed.
# * Therefore `should_compress(chunk, None)` (`compress.py:100-101`) always
#   returns True above 64 bytes, and `Coalescer.set_link_speed(None)`
#   (`coalesce.py:77-80`) always selects the fast interval. Both modules' careful
#   measured thresholds — `SLOW_LINK_BPS`, the `400_000` branch — never execute in
#   production.
#
# This is not necessarily wrong behaviour (always-compress is the safe default),
# but the code reads as adaptive and is not. Either implement `STAT` on the guest
# or delete the branches and state the fixed policy.
#
# `GETOPTIONS` (`server.py:1008-1009`) is likewise never sent by the guest —
# options are pushed at attach and after every change.
#
# `TOOLRESULT` is the reverse: the bridge sends it on every tool result
# (`server.py:478-482`), folding and shipping up to 2,000 characters, and the
# guest has no handler for it. That is wasted bandwidth on every tool call, which
# matters more on the dialup path than anywhere else. The guest *does* have a
# `TOOL` handler (`win/main.c:621`) that the bridge stopped feeding when tool
# lines moved into the HTML transcript. Two frame types, both half-implemented,
# pointing opposite ways.
#
#### 6.3 Unused fields, parameters and functions
#
# | Item | Where | Note |
# |---|---|---|
# | `caps.shortnames` | `caps.py:136, 242` | Declared, enum-validated, stored, **never read**. `SHORTNAMES_83`/`_NONE` unused outside it. |
# | `Tomb.reported` | `sync.py:151` | Serialised and loaded; never set `True` in production. |
# | `Entry.size` | `sync.py:87` | Written everywhere, read nowhere — `same_content` excludes it by design, and `scan_cache` keeps its own size. |
# | `Decision.display` | `sync.py:159` | Set for PUSH and MKDIR; read only for MKDIR (`syncrun.py:888`). PUSH uses `b.display` directly. |
# | `Pass._guest_display` | `syncrun.py:176` | Assigned, never read. |
# | `Pass.caps`, `Pass.guest_root` class attrs | `syncrun.py:58, 64` | Documented as pre-SYNCEND fallbacks; `__init__` always overwrites both, so unreachable. |
# | `key` parameter | `Pass.push:312`, `Pass.mkdir:466`, `Pass.stage_conflict:500` | Passed, unused. |
# | `PendingPrompt.expired` / `.created` | `permissions.py:68, 74-76` | The deadline is enforced by `asyncio.wait_for` (`server.py:582`). Nothing reads either. |
# | `paths.case_collision` | `paths.py:351` | Test-only. `scan_bridge:246-254` reimplements the same rule inline with a `seen` dict. |
# | `paths.join_guest` | `paths.py:366` | Test-only; the guest builds its own paths. |
# | `Session.context_usage` | `session.py:252` | No caller. |
# | `Session.MODELS`, `Session.EFFORTS` | `session.py:76-82` | Superseded by `server_models()`; `EFFORT_ORDER`/`EFFORT_LABEL` in `server.py:2171` are the live table. |
# | `sessions.STYLE_*` | `sessions.py:24-27` | Internal to `_text_of`/`history_html`, fine — but `html.APPEND`/`LIVE` and these are two vocabularies for adjacent ideas. |
#
#### 6.4 `fakegui.py` has fallen behind the protocol
#
# `fakegui.py` is described as "the protocol conformance harness" and "the
# break-glass tool when a permission prompt has the session wedged". As of today it:
#
# * **does not inflate `enc=deflate`** — `render` does `f.body.decode("cp1252")`
#   (`:111`) with no `StreamDecompressor`, while `_send_html` compresses anything
#   over 64 bytes with `link_bps=None`. Every `TEXT` frame of any size renders as
#   binary noise. `test_server_integration.py:112` gets this right; the tool does not.
# * renders `TEXT` by `f.headers.get("style", ...)` (`:119`) — the bridge sends
#   `mode`, `page`, `enc`, `total`, `off`, and no `style`.
# * has no handler for `DOC`, `PAGESTART`/`PAGEEND`/`PAGESEALED`, `ANCHOR`,
#   `HALT`/`HALTEND`, `OPTION`/`OPTIONSEND`, `SESSIONS`/`SESSION`/`SESSIONSEND`,
#   `SWITCHED`, or any `EXEC*`/`SYNC*` frame — the last group meaning it cannot
#   exercise the sync layer at all.
# * has no `/continue` for the halt bar, which is the newest wedge-the-session
#   case.
#
# The permission path (`/allow`, `/always`, `/deny`) still works, so the
# break-glass half is intact. The conformance half is not, and "if fakegui works
# and the guest does not, the bug is in the C" no longer holds.
#
# **Fix.** Either restore it — inflate, handle HTML append/live into a scrollback,
# add the sync and exec frames — or shrink its stated job to "permission
# break-glass" and say so in the docstring. The file's own instruction, "It is a
# ~200 line script and should stay one", argues for the second, with the
# conformance role moving to the integration tests that already do it properly.
#
#### 6.5 Five stale `.bak` copies inside the package
#
# ```
# bridge/fold.py.bak.20260916-233612
# bridge/markdown.py.bak.20260916-231246     <- module replaced by html.py
# bridge/protocol.py.bak.20260916-221107
# bridge/sessions.py.bak.20260917-002641
# bridge/server.py.bak2.20260917-010809
# ```
#
# They are not importable, but they are in every `grep -rn bridge/` and
# `markdown.py.bak` in particular advertises a module that no longer exists. Move
# them out of the source tree.
#
# ---
#
### 7. What blocks the planned features
#
#### 7.1 R7 — multiple clients (the important one)
#
# PLAN.md R7 says the shape is `self._guests: dict[client_id, GuestSession]` and
# that "everything already per-guest stays that way". The problem is how much is
# *not* already per-guest. Concretely, what breaks:
#
# | # | State | Line | What happens with two clients |
# |---|---|---|---|
# | 1 | `self._early` | `:182, 330, 875` | B's HELLO read stores pipelined frames on the **shared** `Bridge`; whichever `_pump` reaches `:875` first consumes them. B's frames get handled as A's, against A's session. Silent and non-deterministic. |
# | 2 | `self._agent_notices` | `:203, 1036, 1248, 1358` | One queue, two agents. A's conflict notice is consumed by whichever client sends the next INPUT. The other agent is never told its writes are refused — the exact failure `_with_sync_preamble` exists to prevent. |
# | 3 | `self.guest_sync_root` | `:197, 1226, 1386, 504` | Client B's guest root overwrites A's, then goes into A's **system prompt** at `:503-504`. A's agent is told the wrong folder is mirrored. |
# | 4 | `self._last_sync_reports` | `:198, 1230, 1434` | `sync_status` answers with the other machine's skip list. |
# | 5 | `self.registry` | `:164, 966, 529, 1096` | R7 notes this; it is also what makes R1 correct on a multi-machine bridge. |
# | 6 | `self.sync_dir` / `self.sync_state` | `:191-192` | Process-global. Two clients cannot mirror two trees, and if they mirror the same tree they share one `base.json` whose `tree_id` and `guest_root` guards (`syncrun.py:149-172`) will refuse the second client's every pass — correctly, and with no way to say "these are two machines". |
# | 7 | `_baseline_lock()` → `guest.sync_lock` | `:1547` | Two locks, one file. §5.3. |
# | 8 | `self.link_bps` | `:168, 406` | A CLI default applied to every client; harmless today only because §6.2 makes it inert. |
#
# Beyond the state, three structural blockers:
#
# * **`_attach`'s busy check** (`:365-368`) rejects on `self._guest.connected`, with
#   no notion of identity. R7 wants the rejection to narrow to "same `client_id`",
#   which means `client_id` has to exist in HELLO and be validated — a `caps.py`-style
#   addition — and `_attach` has to grow a lookup-or-create keyed on it. That is
#   the easy part; it is `negotiate()` (§1.2) that makes it a ten-line change
#   instead of a rewrite of a 115-line method.
# * **`run_on_guest`/`run_sync` resolve `self._guest` directly** (`:1120`,
#   `:1213`). With a dict there is no "the guest" — the MCP tool has to know which
#   client its session belongs to. That means `tools.build(self)` (`:516`,
#   `tools.py:40`) must become `tools.build(self, guest)` and the server is built
#   per-session rather than per-bridge, or the tool bodies must carry a client
#   handle. This is the part most likely to be discovered late.
# * **`close()` closes only `self._guest.claude`** (`:227-228`).
#
# **Recommended order.** Do §1.1 (`GuestLink`), §1.2 (`negotiate`) and §1.4
# (`SyncCoordinator`) *before* R7. Rows 2, 3, 4, 6 and 7 all move into
# `SyncCoordinator` as a group, at which point "one coordinator per client, one
# baseline per client" is a constructor argument rather than an audit of every
# `self.` reference in a 2,000-line class.
#
#### 7.2 Phase 4 — per-client model/effort and a settings dialog
#
# Mostly fine, because `model` and `effort` are already on `GuestSession`. Three
# snags:
#
# * **The menu bug in §5.5** must be fixed first, or per-client model will show
#   the wrong tick the moment a CLI default exists.
# * **The effort fork changes the session id and the registry does not follow**
#   (PLAN Phase 4 records this). The mechanism is worth naming: `_make_session`
#   does call `self.registry.add(s.session_id)` (`:529`), but `_session_id` is
#   only set from the SDK's `init` `SystemMessage` during `_consume`
#   (`session.py:282-283`), which needs a query — so at `start()` it is `None` and
#   the add is a no-op. `_set_effort:1999` then reads the same `None` and leaves
#   `guest.sess_id` on the pre-fork value. Both only correct themselves on the
#   next turn at `:1096`. The fix belongs with the fork, not with the turn.
# * **A settings dialog means more `SET*` frames.** `SETMODEL` and `SETEFFORT` are
#   already two near-identical branches in the dispatch chain, one inline
#   (`:1011-1018`) and one delegated (`:1020-1021`). A third and fourth want §1.6's
#   handler table and a single `_apply_setting(name, value)` rather than a branch
#   each.
#
#### 7.3 Phase 6 — many more tools
#
# Phase 6 adds `screenshot`, `click`, `send_keys`, `send_action_sequence`,
# `list_windows` and the control tree. Every one of them is the same
# request/await/reply/kill shape as `exec`. Today that shape is 190 lines welded
# into `Bridge` with a hardcoded id prefix (`f"x{guest.exec_seq}"`, `:1143`), a
# hardcoded output cap, a hardcoded status→prose table and a hardcoded lock.
#
# **What blocks it:**
#
# * **No reusable RPC primitive.** `run_on_guest` and the four `Pass` transport
#   methods are five hand-rolled copies of it already (§3.2, §4.2). A sixth
#   through tenth is where this stops being tolerable.
# * **`exec_lock` is a serialisation policy stated as a field.** The rule is "one
#   worker thread over there" (`:1130`), which is a property of the *guest*, not of
#   `exec`. A screenshot and a click both go through that worker, so they need the
#   same lock — and the current placement makes that a thing someone has to
#   remember rather than a thing the channel enforces.
# * **Every new tool costs a permission bar and a summary line**, and §3.4's four
#   argument tables must all learn about `x`/`y`/`selector`. The measured failure
#   in `permissions.py:188-192` — the bar rendering as just "Exec" with no command —
#   is the one that recurs.
# * **`_summarise` (`:2176`) and the `TOOLRESULT` body cap (`:482`)** are tuned for
#   text tools. An image result needs a different path entirely, and §6.2 shows
#   `TOOLRESULT` currently goes nowhere.
#
# **Fix.** Extract `ExecChannel` (§1.5) as `GuestRpc` with `call(verb, headers,
# body, *, timeout, serialise=True) -> Result`, and let each tool be a thin body
# in `tools.py`. The permission and summary surface gets one table.
#
# ---
#
### 8. Bugs found along the way
#
#### 8.1 `_sync_frame` can raise `KeyError` and kill the connection
#
# `server.py:1682, 1690, 1699`. A `SYNCENT` or `SYNCEND` frame carrying a `sid`
# that belongs to a `SYNCPUT`/`SYNCGET`/`SYNCMKDIR` call indexes `entry["rows"]`
# on a dict that has only `"future"`. The exception propagates through `_handle`
# → `_pump` → `_on_client`'s bare `except Exception` (`:264`), which logs and tears
# the connection down. The guest's own `reject_sync_busy` already produces
# mismatched replies (BACKLOG §5), so the ingredients are on the table.
#
# **Fix.** §3.2's typed calls; or minimally, `entry.get("rows")` with a
# `log.warning` and return.
#
#### 8.2 An idle guest is reaped every 90 seconds when sync is off
#
# `_reap_idle` (`server.py:1867-1911`) closes the link after
# `idle_timeout_s = 90` of silence, on the stated premise that "the guest pings
# every few seconds". It does not (§6.2). The only unprompted guest traffic is
# `SYNCDIRTY`, which `win/rpc.c:93` suppresses when the guest's INI has no sync
# root.
#
# So with sync off: a user who reads for ninety seconds without typing has the
# connection closed underneath them and the guest enters its 1/2/4/8s reconnect
# backoff. And worse — `_resolve_all(guest, "idle", ...)` runs first, so **a
# permission prompt on screen for ninety seconds is denied**, against
# `permissions.DEADLINE_S = 600`. A user who walks away from a permission bar for
# two minutes comes back to a denied tool call and a reconnect.
#
# **Fix.** Either add a `PING` to the guest (the bridge's `PONG` handler is
# already written), or gate the reaper on `perm_pending is None` the way it is
# already gated on `exec_pending`/`sync_active` (`:1891`), or both. The second is
# one line and closes the harmful half immediately.
#
#### 8.3 The Model and Effort menus ignore `--model` / `--effort`
#
# §5.5. `_send_options:1933, 1964` read `guest.model`/`guest.effort` only, while
# the session runs on `guest.model or self.model`.
#
#### 8.4 A block split across a page boundary is rendered twice
#
# `_write_block` (`:815-822`) captures `page_no` before `pages.add`, then sends the
# **whole** html as an APPEND to `page_no`. If `add` split the run across the seal
# (`pages.py:164-186`), the tail is also stored in page `page_no+1` and is re-sent
# verbatim when the reader pages forward. Rare — it needs a block larger than the
# remaining hard-cap room — but a fenced code block is exactly the case that hits it.
#
#### 8.5 `sync_status` walks the whole working directory on every call
#
# `_edits_outside_the_mirror` (`:1461-1489`) does an unbounded `os.walk` of `cwd`,
# stats every file, sorts and takes 20. `tools.py:118` tells the model the tool is
# "Read-only and free to call". It is not free, and the cost scales with a
# directory the agent controls.
#
#### 8.6 `sync_resolve` canonicalises under guessed caps when disconnected
#
# `server.py:1513-1515` passes `{}` to `from_hello` when `self._guest` is None, so
# the path is folded through cp1252 with case-folding regardless of what the client
# actually declared. For a `CASE_SENSITIVE` client this releases the wrong key, or
# none. §5.4.
#
#### 8.7 Two `INPUT`s produce two turns, with a bare `create_task`
#
# Known (BACKLOG §5), recorded here for completeness because both sites are also
# outside the strong-reference set: `server.py:890` and `:924` use
# `asyncio.create_task` rather than `self._spawn`, so the task can be collected
# mid-turn — the exact hazard `_spawn`'s docstring (`:742-747`) exists to describe.
#
#### 8.8 `sync_state` defaults to `""`
#
# `server.py:192`. Every caller of `_load_baseline` is guarded by
# `if not self.sync_dir` except `_report_abort` (itself only reachable from a
# guarded `run_sync`). If that stops being true, `os.path.join("", "base.json")`
# reads and **writes** `./base.json` relative to the process cwd. `sync_dir` uses
# `None` as its sentinel; this should too.
#
# ---
#
### 9. What is good, and should not be refactored away
#
# Worth stating, because a restructuring pass is exactly when these get lost.
#
# * **`sync.py:diff()`** is a pure, exhaustively enumerated state table with the
#   reasoning for each row inline. It is the strongest piece of design in the
#   bridge. Every extraction proposed above leaves it untouched.
# * **`caps.py`'s refuse-don't-guess rule** and the header-per-option argument
#   against bit packing. Both are correct and both are load-bearing.
# * **`paths.py`'s three non-negotiables** and the surrogateescape round trip.
# * **The synchronous-by-design methods** — `_finish_prompt`, `_resolve_all`,
#   `_kill_exec`, `_fail_execs`, `_fail_syncs` — are synchronous because they run
#   inside cancelled scopes. Any move must keep that property, and the reason
#   should travel with the code.
# * **`_send`'s encode-before-stamp probe** catches a real class of permanent
#   failure. Its stated reason is about replay, but the check outlives it.
# * **The comment density.** Most of these comments are a record of a measurement
#   or a shipped failure, not commentary. The ones flagged in §4.4 and §6.2 are
#   wrong now and should be corrected rather than deleted — a wrong comment about
#   a 30-second timer is how the next person reimplements the timer that is
#   already in `win/rpc.c`.
#
#
# ======================================================================
# FINDINGS. A line without "Fixed:" is outstanding work.
# ======================================================================
#
02.1 Bridge IS 55 METHODS OVER 2,116 LINES holding nine unrelated responsibilities (socket accept, framing, HELLO/caps, permission prompts, exec dispatch, sync orchestration, halt/latch state, session lifecycle, paging, model/effort options). The seam that pays first is the WIRE: 44 _send calls and 38 hand-built Frame(...) literals, so stamp=, header stringification and fold.encode are decided per call site -- the same shape as rc_html_append. Fix: a GuestLink owning writer/lock/compressor/chunking, plus typed constructors in protocol.py where four already live.
02.2 GuestSession IS SEVEN GROUPS IN ONE DATACLASS -- 30 fields and three locks with three different scopes: a socket, the guest's worker thread, and a file on THIS disk. Split into GuestLink / TranscriptView / PermissionDesk / ExecChannel / GuestSyncState with GuestSession as the composition root.
02.3 MULTI-CLIENT (R7) IS BLOCKED BY EIGHT PIECES OF PER-GUEST STATE LIVING ON Bridge, not by _guest being singular: _early (a second client's pipelined frames get handled as the first's), _agent_notices (one queue, two agents, so the other agent never learns its writes are refused), guest_sync_root (goes into the OTHER client's system prompt), _last_sync_reports, registry, sync_dir/sync_state, and the baseline lock. Plus run_on_guest/run_sync resolving self._guest directly, which forces tools.build() to become per-session.
02.4 sync_pending HOLDS FOUR DIFFERENT AD-HOC DICT SHAPES IN ONE ID NAMESPACE, and _sync_frame indexes them unchecked -- a SYNCENT against a push id raises KeyError out to connection teardown. The four futures also resolve to four positional tuples of different arity, which is the bridge half of the reject_sync_busy 'not enough values to unpack' entry in BACKLOG §5.
Fixed: 02.5 THE BRIDGE NEVER SCHEDULED A SYNC PASS. run_sync has one production caller (the SYNCDIRTY handler) although its docstring claims three triggers. The 30-second cadence that all the say-it-once machinery (first_report, reported_lines, aborted) is designed around lived in win/rpc.c, on the GUEST -- so if its watcher thread died, or a guest were built without one, nothing on this side would ever have noticed. Fixed: the docstring's three triggers now exist. SYNCDIRTY stays the primary one and the only one that reacts to an actual change rather than a clock; a pass runs ON ATTACH, because a machine edited while it was disconnected reconciled only when its watcher next happened to fire; and a bridge-side sweep every 120s is the backstop for a guest that has stopped telling us anything, deliberately slower than the guest's own 30s because a pass it starts is one the guest failed to ask for. All three coalesce on the existing sync_lock -- the test for the sweep has to answer the attach pass first, because otherwise the sweep correctly declines to start a second pass over a baseline the first is still holding.
Fixed: 02.6 STAT AND PING WERE HANDLED AND NEVER SENT. PING is fixed: the guest now sends it on its sweep when there is no sync root, which is what makes the keepalive real -- see 02.7. STAT is NOT sent, so guest.link_bps stays None and every adaptive threshold in compress.py/coalesce.py is inert in production; that half is recorded in BACKLOG rather than built, because the adaptive codec wants the throttled-link gate to measure against and that gate has not been run.
Fixed: 02.7 WITH SYNC OFF AN IDLE GUEST WAS DISCONNECTED EVERY 90 SECONDS, and a permission prompt on screen was denied at 90s against its documented 600s deadline. Root cause: rpc_maybe_nudge returned early when there was no sync root, so the guest sent NOTHING between turns and last_seen never moved. Fixed by sending PING on the sweep. A first attempt gave the reaper a longer leash while a prompt was up; that was wrong and broke test_idle_guest_is_reaped_so_it_can_reconnect correctly -- a prompt on screen is no evidence of life, and the exemption meant a guest that CRASHED while one was up held the slot for ten minutes. Reverted in favour of the keepalive: a living machine pings, a dead one does not.
02.8 Pass DOES ORCHESTRATION, POLICY, TRANSPORT AND DISK I/O IN ONE CLASS: six reaches into bridge._send, mutates guest.sync_pending/sync_seq, does I/O and raises in __init__, and note() rewrites the whole baseline (including scan_cache, fsync and the .prev rename) once per notice.
Fixed: 02.9 FIVE PLACES BUILT base.json's PATH BY HAND and three different code paths reported 'could not open a session'. The path half is fixed: os.path.join(self.sync_state, 'base.json') appears once, in _load_baseline, and _baseline_lock is the single place that decides which lock guards it. The three session-error messages are NOT unified and stay outstanding under 02.1, because they belong to the Bridge decomposition rather than to this.
02.10 paths._Default IS A SECOND, DRIFTED DEFINITION OF caps.Caps (missing manifest, shortnames, declared), kept only to dodge an import cycle that disappears if _ACP_CODECS moves to caps.py.
02.11 DEAD OR VESTIGIAL beyond replay.py: four of six protocol constructors, caps.shortnames (parsed, validated, never read), Tomb.reported, Entry.size, Pass._guest_display, Pass.caps/guest_root class attributes, PendingPrompt.expired, dead key params on three Pass methods, paths.case_collision (reimplemented inline in scan_bridge), paths.join_guest, and Session.context_usage/MODELS/EFFORTS.
02.12 fakegui.py NO LONGER SPEAKS THE PROTOCOL IT EXISTS TO CONFORM TO: it does not inflate enc=deflate, renders TEXT by a style= header that no longer exists, and handles no sync, exec, page or halt frames. The claim that 'if fakegui works the bug is in the C' does not hold; only the permission break-glass half still works.
