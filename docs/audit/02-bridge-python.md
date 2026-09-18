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
Fixed: 02.1 Bridge WAS 55 METHODS OVER 2,116 LINES holding nine unrelated responsibilities. The WIRE seam is cut: bridge/wire/link.py's GuestLink owns the writer, the send lock, the persistent compressor and the chunking of outbound HTML -- _send, _send_html, wire_yield and _try_send_raw moved onto it whole, and Bridge's own methods of the same names are now one-line delegations kept only because syncrun.py's Pass and tests/python call them by those names. All 38 hand-built Frame(...) literals in server.py are gone, replaced by 27 typed constructors in bridge/wire/protocol.py (hello and error already lived there; ready, resync, doc, switched, sessions_start, session_row, sessions_end, notice, turn_start, turn_error, turn_end, anchor_frame, permreq, permstate, permwait, permend, halt, haltend, pagesealed, pagestart, pageend, text_frame, exec_cmd, exec_kill, option, optionsend, pong are new) -- header stringification and fold.encode are now decided once per frame type instead of at each call site. `stamp=` stays an explicit argument at the call site rather than moving into the constructors: TURN state=start is sent BOTH ways, stamp=True for a fresh turn and stamp=False to restore the busy UI after a reconnect, with identical headers, so it genuinely is a call-site decision and not a property of the type -- protocol.py records this rather than papering over it. GuestSession keeps its old writer/send_lock/zip_out/link_bps attribute names as properties forwarding to a new `link: GuestLink` field, so nothing outside server.py had to change shape. 2122 tests pass throughout, no skips, no protocol change, no behaviour change. Bridge's OTHER eight responsibilities (HelloNegotiator, PermissionDesk, SyncCoordinator, ExecChannel, the dispatch chain, the GuestSession split) are untouched and tracked at 02.2/02.30.
Fixed: 02.2 GuestSession IS SEVEN GROUPS IN ONE DATACLASS -- three of the seven are out, each as a field GuestSession composes with the old names forwarded as properties, so nothing outside had to change shape: Link (GuestLink, bridge/wire/link.py, from the 02.1 pass), Sync RPC (GuestSyncState, bridge/files/synctransport.py) and View (TranscriptView, bridge/view/transcript.py, taking pages/render/coalescers/view_page/page_bytes/page_blocks). TranscriptView deliberately owns no I/O: sending stays on Bridge, because a view stored ON GuestSession cannot hold a back-reference to the guest it would need in order to send without a construction-order cycle, and the added wiring would buy a type that is still data plus pure policy either way. The remaining four groups (Identity, Prompts, Halt, Exec RPC) are 02.42, with the reasons each was left.
Fixed: 02.3 MULTI-CLIENT (R7) IS BLOCKED BY EIGHT PIECES OF PER-GUEST STATE ON Bridge -- recorded in BACKLOG §7 with the rest of the R7 cluster, and one item was already closed: the worst of the eight, tool closures resolving the wrong guest, was fixed the same day by 326a62a. run_sync was missed by that fix and stays named. The remaining state is mechanical once self._guest becomes self._guests, so it carries no independent cost -- and it is GATED on the mirror-per-client decision (04.32), which is deliberately unanswered. Recorded as one blocked cluster rather than eight separate tasks, because they all land together or not at all.
Fixed: 02.4 sync_pending HELD FOUR DIFFERENT AD-HOC DICT SHAPES IN ONE ID NAMESPACE, and _sync_frame indexes them unchecked -- a SYNCENT against a push id raises KeyError out to connection teardown. The four futures also resolve to four positional tuples of different arity, which is the bridge half of the reject_sync_busy 'not enough values to unpack' entry in BACKLOG §5. The CRASH is fixed: sync_pending now holds a typed S.Wait carrying its own kind, so a SYNCENT bearing a put's id is a log line rather than a KeyError out to connection teardown. There turned out to be a FIFTH shape the audit did not name -- SYNCDAT conjured a byte buffer with setdefault on whatever dict happened to be there, so a stray one could attach file bytes to a scan and nothing would notice; `data` is a field now and a SYNCDAT for a non-get is refused the same way. The guest half is fixed too: reject_sync_busy answers SYNCEND, SYNCDATEND or SYNCMKDIRACK to match the request rather than SYNCPUTACK for everything, so a busy SCAN no longer reaches its future with a two-value shape where six are unpacked. The futures still resolve to positional tuples of different arity, which is a readability problem rather than a failure mode now that nothing sends the wrong one; it is folded into 02.1's decomposition and tracked separately as 02.37.
Fixed: 02.5 THE BRIDGE NEVER SCHEDULED A SYNC PASS. run_sync has one production caller (the SYNCDIRTY handler) although its docstring claims three triggers. The 30-second cadence that all the say-it-once machinery (first_report, reported_lines, aborted) is designed around lived in win/rpc.c, on the GUEST -- so if its watcher thread died, or a guest were built without one, nothing on this side would ever have noticed. Fixed: the docstring's three triggers now exist. SYNCDIRTY stays the primary one and the only one that reacts to an actual change rather than a clock; a pass runs ON ATTACH, because a machine edited while it was disconnected reconciled only when its watcher next happened to fire; and a bridge-side sweep every 120s is the backstop for a guest that has stopped telling us anything, deliberately slower than the guest's own 30s because a pass it starts is one the guest failed to ask for. All three coalesce on the existing sync_lock -- the test for the sweep has to answer the attach pass first, because otherwise the sweep correctly declines to start a second pass over a baseline the first is still holding.
Fixed: 02.6 STAT AND PING WERE HANDLED AND NEVER SENT. PING is fixed: the guest now sends it on its sweep when there is no sync root, which is what makes the keepalive real -- see 02.7. STAT is NOT sent, so guest.link_bps stays None and every adaptive threshold in compress.py/coalesce.py is inert in production; that half is recorded in BACKLOG rather than built, because the adaptive codec wants the throttled-link gate to measure against and that gate has not been run.
Fixed: 02.7 WITH SYNC OFF AN IDLE GUEST WAS DISCONNECTED EVERY 90 SECONDS, and a permission prompt on screen was denied at 90s against its documented 600s deadline. Root cause: rpc_maybe_nudge returned early when there was no sync root, so the guest sent NOTHING between turns and last_seen never moved. Fixed by sending PING on the sweep. A first attempt gave the reaper a longer leash while a prompt was up; that was wrong and broke test_idle_guest_is_reaped_so_it_can_reconnect correctly -- a prompt on screen is no evidence of life, and the exemption meant a guest that CRASHED while one was up held the slot for ten minutes. Reverted in favour of the keepalive: a living machine pings, a dead one does not.
Fixed: 02.8 Pass DOES ORCHESTRATION, POLICY, TRANSPORT AND DISK I/O IN ONE CLASS -- the TRANSPORT half is out. bridge/files/synctransport.py's SyncTransport wraps one guest's GuestLink plus a new GuestSyncState and offers scan/put/get/mkdir, each doing the allocate-id, install-Wait, send, await, clean-up dance that was written out four times in syncrun.py; put() absorbs the chunked SYNCPUT loop and its wire_yield between chunks. Pass no longer imports Frame and no longer knows bridge or guest exist. The audit's own count was one short on this reading -- guest.perm_pending was a FIFTH reach, in run()'s yield-for-a-prompt logic. The apply chain deliberately did NOT become a dispatch table: test_sync_apply_coverage.py reads the source of Pass.run and asserts each S.Act member appears, so `_apply[dec.act](...)` would delete the literal branches it checks for, and that test is the forcing function that has already caught a real bug. Verified by reading the test rather than by trying it and reverting. The remaining extractions (BridgeScanner, the reporter, note()'s whole-baseline rewrite per notice) are 02.40.
Fixed: 02.9 FIVE PLACES BUILT base.json's PATH BY HAND and three different code paths reported 'could not open a session'. The path half is fixed: os.path.join(self.sync_state, 'base.json') appears once, in _load_baseline, and _baseline_lock is the single place that decides which lock guards it. The three session-error messages are NOT unified; that half is tracked as 02.16, because it belongs to the Bridge decomposition rather than to this.
Fixed: 02.10 paths._Default WAS A SECOND, DRIFTED DEFINITION OF caps.Caps (missing manifest, shortnames, declared), kept only to dodge an import cycle. The cycle was REAL and not stale -- caps.py imported paths solely for codec_for_acp, used at exactly one line. Fixed as the audit suggested: _ACP_CODECS and codec_for_acp moved to caps.py, caps.py no longer imports paths at all, and paths.DEFAULT is now caps.WIN9X. One definition of what a client is, reached through WIN9X/POSIX/from_hello.
Fixed: 02.11 DEAD OR VESTIGIAL beyond replay.py: four of six protocol constructors, caps.shortnames (parsed, validated, never read), Tomb.reported, Entry.size, Pass._guest_display, Pass.caps/guest_root class attributes, PendingPrompt.expired, dead key params on three Pass methods, paths.case_collision (reimplemented inline in scan_bridge), paths.join_guest, and Session.context_usage/MODELS/EFFORTS. All verified dead by grepping the whole tree before deletion, and all removed -- plus PendingPrompt.created, which existed only to feed `expired` and became dead with it, and the `import time` that served only that. TWO CORRECTIONS TO THE AUDIT, both found by checking rather than trusting: Pass.caps and Pass.guest_root are LIVE, not dead -- eight tests in test_sync_wire.py build bare Pass subclasses that skip __init__ to exercise scan_bridge in isolation and depend on exactly that class-level fallback, so they are kept with a comment saying why. And caps.shortnames could not simply be dropped from from_hello's validation: caps.py's rule is that an unknown value is REFUSED rather than ignored, so removing the field had to leave a guest still sending shortnames=1 accepted rather than disconnected. Verified: shortnames=99 is accepted, case=7 is still refused.
Fixed: 02.12 fakegui.py NO LONGER SPOKE THE PROTOCOL IT EXISTS TO CONFORM TO: it does not inflate enc=deflate, renders TEXT by a style= header that no longer exists, and handles no sync, exec, page or halt frames. The claim that 'if fakegui works the bug is in the C' did not hold. Fixed by matching win/app/main.c's render_frame frame for frame: enc=deflate inflated through one StreamDecompressor per connection (the same scope net.c's inflate_begin/finish uses), TEXT dispatched on mode= rather than the dead style=, and DOC, the page frames, ANCHOR, OPTION, the session frames, SWITCHED and HALT/HALTEND all handled -- with a /continue command for RESUME, which is the wedge-clearing case that had no way to be exercised at all. EXEC/EXECKILL/SYNC* are intercepted before render(), because on the real guest net.c routes them to the RPC worker and they never reach the UI thread. EXEC is genuinely implemented; SYNC* answers no_root, which is the honest answer a guest with no sync root gives and which the bridge already turns into a clean abort rather than hanging out its 600s timeout. The manifest transfer is deliberately NOT reimplemented -- a second implementation of that protocol would introduce its own bugs wearing the costume of bridge bugs. FOUND ON THE WAY, and fixed separately: _send_html chunks an oversized APPEND block across frames carrying total=/off=, which look like a reassembly protocol and are not -- the guest calls rc_html_block per frame and uses them only for the progress figure, so a large block arrived as several fragments of markup handed to insertAdjacentHTML.
Fixed: 02.13 exec_pending ENTRIES WERE AD-HOC DICTS grown post-hoc with "killed" and "capped" -- now a typed ExecCall, mirroring the sync.Wait that closed 02.4. Converting the call sites EXPOSED A CRASH: _fail_syncs still did entry.get("future"), correct for the old dict and an AttributeError against sync.Wait, which 02.4 had already introduced. So a guest disconnecting mid-sync took down the connection handler instead of failing the in-flight transfer -- a bug created by the previous typed-dict fix and invisible until this one. Fixed with a regression test verified against the pre-fix line.
Fixed: 02.14 THREE INDEPENDENT STATUS-STRING VOCABULARIES ARE BARE LITERALS AT THE POINT OF USE -- the EXEC vocabulary is closed: ExecStatus and EXEC_STATUS_PROSE in the new bridge/status.py, which also collapsed the duplicated isError tuple. The SYNC vocabulary (a dozen tokens across SCAN/PUT/GET/MKDIR, including a too_long that means something different from exec's) and the prompt-end reasons are NOT done and are tracked as 02.38 rather than left implied by this line. The reason is recorded in status.py's own docstring instead of dropped silently.
Fixed: 02.15 FOUR DIFFERENT TABLES ANSWER "WHICH ARGUMENT OF THIS TOOL MATTERS" IN THREE ORDERINGS -- down to two, and the second is deliberate. permissions.telling_argument() is now the single answer, used by render_request's desc fallback, summary's bar text and server._summarise. _PREFERRED stays separate because it is a different question -- the full argument-table ORDER, including old_string/new_string/content -- and folding it in would put file content into the one-line bar label.
Fixed: 02.16 ERROR REPORTING VARIES BY CALL SITE AND _send CAN SILENTLY DROP FRAMES -- _new_session and _switch now report a failed session open through H.error into the transcript, the channel _set_effort already used, so the "could not open a session" family has one answer. GuestLink.send returns bool instead of None, and its comment claiming a dropped frame is "buffered in replay; the guest will ask for it" is corrected -- replay is dead and the guest never sends from=. bad_session deliberately stays an ERROR frame: it is a malformed request, not a session that failed to open.
Fixed: 02.17 SMALLER UNSWEPT REPETITIONS -- all three done. The coalescer speed propagation and the paginator/renderer/coalescer reset sequence, each duplicated at two sites, are now GuestSession.set_link_speed() and reset_transcript() -- and the latter killed the vestigial `guest.page_bytes or None or 16384`, where `or None` was a no-op nobody had noticed. PendingPrompt.summary is a declared field set at construction, so both getattr(prompt, "summary", ...) fallbacks are gone. attrs.load walks the tree once instead of walking it again through _sources purely to name files it had already opened.
Fixed: 02.18 TranscriptView's INVARIANT IS ENFORCED BY CONVENTION, NOT BY TYPE -- the duplication half is closed by 02.17's reset_transcript(), and a redundant second `guest.view_page = None` in _switch went with it. The substantive half is NOT done and is tracked as 02.39: _switch still bypasses _write_block's paginate/stream/seal rule with raw pages.add and message_boundary calls during history replay. Routing it through _write_block was attempted and backed out for a stated reason rather than forced -- _write_block emits PAGESEALED unconditionally, and doing that for every block replayed before SWITCHED would change what the guest receives and in what order, which could not be verified from the bridge side alone.
Fixed: 02.19 Pass IS HANDED THE ENTIRE Bridge AND ENTIRE GuestSession but uses four fields off them -- the constructor is now Pass(transport, caps, root, state_dir). The defensive getattr the wide interface forced is gone with it. Done together with 02.8 because the narrow interface is what the transport extraction produces; doing it alone would have meant a fake-transport shim that existed only to satisfy the signature.
Fixed: 02.20 SYNC NOTICES ARE ASYMMETRICALLY DURABLE -- the user's copy was on disk and the agent's was a list on Bridge, so a bridge restart lost the agent's explanation of why a path was refusing writes while the latch that refused them survived. Baseline gains pending_notice_agent beside pending_notice, written in the save note() already does; _report_abort's sync-has-stopped text goes into the same queue. _agent_notices is a cache now rather than the only copy, re-seeded at every attach and cleared durably once a notice actually reaches a turn. The tests build a SECOND, fresh Bridge standing in for the process that replaced the one that ran the pass, and both were verified to fail against the pre-fix code.
Fixed: 02.21 THE LATCH/HALT HOLE IS STILL OPEN ACROSS A BRIDGE RESTART -- _reissue_halt covered a GUEST reconnect, where the same GuestSession survives; a bridge restart gives a fresh GuestSession with halt_id empty while base.latched on disk still says paths are refusing writes. _reconcile_halt derives the halt as a VIEW of base.latched rather than trusting memory -- raising when latched with no bar, clearing when a bar shows with nothing latched -- called at attach and at the end of every pass, reusing the pass's own baseline so it costs no extra I/O. Worth recording: one of the two new tests initially passed for the WRONG reason, because recv_until's timeout path returns the last frame seen rather than a matching one and a stray SYNCSCAN's id header satisfied a loose assertion. Caught by asserting the frame TYPE explicitly.
Fixed: 02.22 caps WAS RE-PARSED FROM hello AT FOUR SITES, unguarded at two, and sync_resolve silently guessed Win9x defaults with no guest connected. Parsed ONCE at handshake in _attach and carried as GuestSession.caps. The re-parse could not raise (already validated at HELLO) but silently re-derived what was already known -- and the sync_resolve site fell back to from_hello({}) whenever the guest was merely unset, which substitutes the restrictive no-declaration defaults for what the client actually declared. That is the standing rule inverted: client rules are declared at handshake, never assumed. from_hello({}) survives for the one honest case, a process that has never had a guest attach at all.
Fixed: 02.23 THE MODEL AND EFFORT MENUS TICKED THE WRONG ENTRY WHEN A CLI DEFAULT WAS SET, and this was user-visible. _make_session falls back to the CLI's --model when guest.model is empty, while _send_options read guest.model alone -- so a bridge started with a CLI default showed "default" ticked in the menu while the session ran on the CLI's choice. One rule in one place now: _effective_model/_effective_effort, used by both the session builder and the menu.
Fixed: 02.24 guest.sess_id HAS THREE DIFFERENT MEANINGS OVER ITS LIFETIME -- it is now assigned exactly once, at construction, and never reassigned (verified by grep: the only sess_id= is the uuid4 in _attach). A stable per-connection handle, which is the shape R7's client_id wants. The chat-list refresh that used to be gated on "did sess_id just change" -- and mis-fired around an effort fork because the SDK's id was still None at the comparison point -- now fires on the init system message itself. The test that required the old behaviour was rewritten to assert the new invariant rather than deleted.
Fixed: 02.25 view_page SURVIVED A RECONNECT, silently freezing the transcript. _attach reset zip_out and lost_at but not view_page -- and the guest has no memory of which page it was reading when it dropped, so after reconnecting it is showing LIVE regardless. The bridge kept the old HISTORY value, so _write_block went on buffering into the paginator: the guest saw nothing new and the bridge believed that was correct, because nothing ever reset the flag that said so. Cleared in _attach, beside the DOC skeleton that makes it true.
Fixed: 02.26 Replay.can_resume_from AND THE RESYNC FRAME WERE NOT SWEPT UP WHEN 02.11 RETIRED THE GUEST'S COPY -- half stale. RESYNC HAS had a guest handler since 03.5 (main.cpp:984, it tells the user the transcript has a hole in it), so only can_resume_from was actually dead: defined in replay.py, called by nothing but its own test. Both removed. Replay itself deliberately STAYS -- server.py still holds the buffer and invalidates it when the outbound deflate stream restarts, which is the thing that would silently feed garbage to a fresh inflater if it went.
Fixed: 02.27 GETOPTIONS WAS NEVER SENT BY THE GUEST -- a dead branch in the frame dispatch. Confirmed by grepping win/ for the type, which returns nothing at all: options are PUSHED at attach and after every model or effort change, so there was never a reason for the guest to ask. Branch removed.
Fixed: 02.28 TWO MORE UNUSED-FIELD ITEMS FROM THE §6.3 TABLE -- WRONG, on both counts, and acting on it would have deleted live code. sessions.STYLE_CODE and STYLE_TOOL are used at four sites in sessions.py (127, 135, 166, 168) and asserted by test_sessions.py. Decision.display is SET at sync.py:402 (MKDIR) and :484 (PUSH) and READ at syncrun.py:894, where it names the path in a report. Closed as incorrect with the evidence rather than obeyed. Worth recording as a pattern: this is the second finding today whose instruction was to remove something that is live, and an audit is a list of claims written against a tree that has since moved, not a work order.
Fixed: 02.29 FIVE STALE .bak COPIES OF BRIDGE MODULES SAT INSIDE THE PACKAGE DIRECTORY -- not beside it, inside it, where they are importable-looking and grep-visible. Deleted. One of them was bridge/markdown.py.bak, which is exactly how 01.29's false comment came to cite a module that does not exist: someone grepped, found the name, and believed it.
Fixed: 02.30 THREE MORE CONCRETE EXTRACTIONS INSIDE THE Bridge GOD OBJECT -- the dispatch chain is done, which the previous pass named as the lowest-risk of the three and the right place to start. Bridge._handle was a 17-branch if/elif over f.type; it is a _DISPATCH dict from frame type to method NAME, resolved with getattr, and _handle is four lines plus the unknown-type fallback. Worth recording why it is names and not bound methods: the first version stored the methods directly, and _sync_frame/_exec_out/_exec_result/_perm_reply are defined LATER in the same class body, so the table referenced names not yet bound and raised NameError before the class finished being defined. That was found by running it rather than by reasoning about it. HelloNegotiator/handshake.py and PermissionDesk are NOT done and are 02.41.
Fixed: 02.31 PHASE 4 IS BLOCKED BEYOND THE 02.23 MENU BUG -- recorded as layer 4 of BACKLOG §10. Its guest.sess_id sub-claim is STALE and was corrected rather than copied forward: a805df3 fixed it, and §10 cites 04.35's framing of what actually remains (one name for two concepts) instead of repeating the outdated version. The SETMODEL/SETEFFORT dispatch duplication is real and deferred with a cost.
Fixed: 02.32 PHASE 6 IS BLOCKED BY HAVING NO REUSABLE RPC PRIMITIVE -- recorded in BACKLOG §9, and it ABSORBS 04.11, which is the same GuestRpc/call_guest extraction named from a second audit file. Costed honestly rather than urgently: it does not block the FIRST Phase 6 tool, and it should happen before the second, which is the point at which run_on_guest's lock/budget/correlation-id/finally dance would get copied for the eighth time.
Fixed: 02.33 A TRANSCRIPT BLOCK SPLIT ACROSS A PAGE BOUNDARY WAS RENDERED TWICE -- a live user-visible bug and the one done first. _write_block captured page_no BEFORE pages.add, then streamed the WHOLE block as a live append tagged with that one page; Paginator.add can split a run across a seal, so the tail landed on the next page having already been sent under the old page's number, and re-appeared verbatim when the reader paged forward. Paginator.write() now reports which page each piece landed on and only the matching piece is streamed. Proven against the pre-fix code by a scoped revert rather than by trusting the diff.
Fixed: 02.34 sync_status DID AN UNBOUNDED os.walk OF THE WHOLE WORKING DIRECTORY ON EVERY CALL while tools.py advertised it to the model as "free to call" -- on a tree the agent itself grows, so the cost scales with the work it has already done. Bounded by STRAY_SCAN_BUDGET (4000 files), and when the budget is hit the answer SAYS the list may be incomplete rather than implying it is exhaustive -- a truncated list presented as complete is worse than a slow one. The docstring now says "cheap to call", which is what it is.
Fixed: 02.35 TWO INPUT FRAMES IN QUICK SUCCESSION PRODUCED TWO TURNS VIA A BARE create_task, which asyncio holds only weakly, so either could be garbage-collected mid-flight. Both now go through self._spawn, which keeps the reference in self._tasks and discards it on completion. The only bare create_task calls left are the reaper and the sweeper, and both are assigned to attributes that hold them for the life of the process.
Fixed: 02.36 sync_state DEFAULTED TO "" RATHER THAN None, unlike sync_dir's sentinel beside it. Every caller of _load_baseline is guarded by `if not self.sync_dir`, so nothing reached it today -- but if that invariant ever broke, os.path.join("", "base.json") is a VALID relative path, so the bridge would silently read and WRITE ./base.json against its own process cwd instead of failing. None makes the same mistake a TypeError.
Fixed: 02.37 THE FOUR SYNC FUTURES STILL RESOLVE TO POSITIONAL TUPLES OF DIFFERENT ARITY -- now ScanResult, AckResult and GetResult, constructed in _sync_frame and read by name in Pass.scan_guest/push/pull/mkdir. AckResult is deliberately ONE type shared by SYNCPUTACK and SYNCMKDIRACK, after reading both handlers to confirm they are the same wire shape with mkdir discarding .crc, rather than two things that merely look alike.
Fixed: 02.38 THE SYNC AND PROMPT-END STATUS VOCABULARIES ARE STILL BARE LITERALS -- both typed now (SyncStatus and PromptEndReason in bridge/status.py), and test_guest_agreement.py gained a test that regex-extracts every status= literal syncrpc.cpp's four handlers can produce and diffs the set against the enum, proven fail-first by removing CRC_MISMATCH and watching it name exactly that token. Two real landmines came out of it. First: a bare (str, Enum) member formats as "ClassName.MEMBER", not its value, so five report strings in syncrun.py that interpolated a status needed .value explicitly. Second and worse: PromptEndReason IS sent on the wire, and Frame.encode calls str() on every header value -- passing the member would have put "PromptEndReason.TIMEOUT" on the wire instead of "timeout". Proven fail-first against a test that asserts the literal wire string. Also a good judgement call: SyncStatus.parse falls back to UNKNOWN rather than to OK the way ExecStatus does, because every sync call site tests != "ok", so falling back to OK would silently turn an unrecognised REFUSAL into an apparent success -- the wrong direction to guess wrong in for a file write.
Fixed: 02.39 _switch BYPASSES _write_block's PAGINATE/STREAM/SEAL RULE during history replay -- closed, after being confirmed and deliberately backed out TWICE for a reason that turned out to be narrower than it looked. The blocker was that _write_block emits PAGESEALED unconditionally, so replaying through it would announce seals for a conversation the guest has not been told it is viewing. But the echo case and the replay case want different things: the echo NEEDS the seal notification, because the user's own message can be what fills a page and the guest's counter would go stale without it. So the flag is `notify`, separate from `stream`, and only history replay passes notify=False. _switch now records through _write_block instead of calling pages.add directly -- one path records a block, so the split-across-a-seal handling that 02.33 fixed cannot be correct in one place and wrong in the other. The boundary policy specific to replay stays in _switch, which is policy rather than a bypass of the write rule.
Fixed: 02.40 Pass STILL DOES POLICY, DISK I/O AND REPORTING -- BridgeScanner and the Reporter are both out. bridge/files/scanner.py's scan_bridge_tree takes the 128-line walk with its symlink, reserved-name and case-collision refusals and the CRC cache's read-then-write gate; Pass.scan_bridge is a three-line adapter, and it was checked specifically against test_sync_wire.py's bare Pass subclasses that skip __init__ and call scan_bridge unbound. bridge/files/reporter.py's Reporter takes first_report, note, notices_delivered and agent_notices_delivered, built over the same Baseline object Pass already holds rather than a copy. note()'s whole-baseline rewrite per notice is UNCHANGED, as instructed -- the durability argument is sound and only the granularity is wrong, and making notices cheaper by making them less durable is the wrong trade. Bridge._agent_notices stays on Bridge with the reason documented: it is a Bridge-lifetime cache read between passes, while Reporter is built per Pass, so folding it in needs the longer-lived owner that does not exist yet. The apply chain was not touched, so test_sync_apply_coverage.py still reads Pass.run's literal source and still enumerates every S.Act.
Fixed: 02.41 TWO OF 02.30's THREE EXTRACTIONS ARE STILL IN Bridge -- both out. bridge/handshake.py's negotiate(headers, secret) is PURE over the HELLO headers and raises Refused(code, msg), covering version, frame cap, secret and caps, so the five refusal paths are unit-testable without a socket. The busy check deliberately stays in _attach, because it is live Bridge state rather than a fact about the HELLO. bridge/agent/permdesk.py's PermissionDesk owns pending and queue plus callback/reply/reissue/deny_all, built per GuestSession because it needs write_block/send/spawn bound to that guest. _finish and deny_all stay SYNCHRONOUS, which is the subtlety that got this deferred once -- they run inside a cancelled scope. One behaviour change flagged honestly rather than buried: negotiation now completes before the busy check, where caps parsing used to run after it, so a busy bridge with undeclarable caps now answers bad_caps instead of busy. No test covers that ordering and the agent said plainly it judged it safe rather than proved it.
Fixed: 02.42 FOUR OF GuestSession's SEVEN GROUPS ARE STILL IN THE DATACLASS -- Identity is out as bridge/agent/identity.py's GuestIdentity, five forwarding properties, same pattern as link/view/sync. Prompts was done once as PermissionDesk under 02.41 rather than twice. HALT WAS DELIBERATELY NOT EXTRACTED, and the reasoning is recorded on the fields themselves rather than in a commit nobody will find: the trio really is a derived view of base.latched, _reconcile_halt already derives it on every attach and pass, and the object that should own both the latch set and the derivation is SyncCoordinator, which does not exist. Giving Halt its own type now would relocate three fields without touching what the audit actually flags -- restart recovery depends on _reconcile_halt running, not on which object holds the fields. ExecChannel stays out of scope for the stated reason: run_on_guest IS mcp__machine__exec and uses Bridge._spawn, whose task set is process-wide, so extracting it is a decision about task ownership on the one path that runs arbitrary commands on the user's machine. That is 02.43 now, with SyncCoordinator.
Fixed: 02.43 SyncCoordinator DOES NOT EXIST, AND TWO SEPARATE PASSES NAMED IT AS THE RIGHT HOME FOR SOMETHING THEY LEFT WHERE IT WAS -- built, in bridge/synccoord.py, and the judgement that preceded it is the part worth keeping. The open question was not whether each thing wanted a longer-lived owner but whether they wanted the SAME one, and that was checked rather than assumed: Bridge._guest is created once per process (`if self._guest is None` in _attach) and reused across every reconnect, so its lifetime already equals Bridge's. A SyncCoordinator built alongside Bridge therefore has exactly the lifetime the halt bar had in practice, which means moving the trio changes WHERE the fields sit and not WHEN anything is recomputed -- so it cannot disturb what _reconcile_halt's restart-recovery guarantee depends on. SyncCoordinator owns halt_id/halt_paths/halt_text and agent_notices, with raise_halt() and clear_halt() that decide and remember; SENDING stays on Bridge, the same split Pass already has against SyncTransport. Deliberately scoped to the two things 02.43 actually named: the audit's §1.4 sketch (run_pass, status_text, resolve, on_continue) would have pulled in sync_dir, sync_state, guest_sync_root, the baseline lock and about 500 lines of run_sync/sync_status_text/sync_resolve, none of which was blocked on a missing owner. Both restart-recovery tests still mean what they meant -- 02.20 still builds a SECOND, fresh Bridge standing in for a replacement process and recovers the notice from disk, 02.21 still drives the real _attach -> _reconcile_halt path against a base.latched written straight to disk; only the attribute path in the assertions changed.
Fixed: 02.44 AN UNHANDLED SDK MESSAGE TYPE WAS DROPPED AT DEBUG, AND ONE OF THEM LOSES THE USER'S PROMPT. bridge/agent/session.py's dispatch ended with `log.debug("unhandled message type")` and a silent return. RateLimitEvent is one of those types, and it ends a turn having produced nothing -- so the user typed a message, the app went back to idle, the cost line still showed the PREVIOUS turn's figure, and there was nothing anywhere on screen to say the request had not run. CLAUDE.md's first standing rule is that the person at the keyboard never loses a byte, and a silently swallowed rate limit breaks it. FOUND BY SMOKE TESTING THE REAL APP, at the user's instruction, and no test in this suite could have found it: it needs the live SDK in a state nothing here can fake -- which is the argument for the instruction. Fixed by raising the log to WARNING and routing the type through on_system("sdk_event"), which the bridge turns into a transcript notice: RateLimitEvent gets a specific message saying the request may not have run and to try again, and anything else is named as unhandled with the same warning. tests/python/test_sdk_events.py pins both halves. What the test CANNOT do is prove the original condition, and it says so in its own docstring rather than implying otherwise.
Fixed: 02.45 `diff` NEVER CONSULTS THE ANCESTOR FOR A DIRECTORY, so every one-sided absence reads as a creation -- `sync.py:455`'s branch was guarded by "directories carry no content; existence is the whole story", which is right about content and wrong to conclude that existence has no history. A directory the user deleted on their own machine was present on the bridge and absent on the guest, so it fell to `MKDIR` and went straight back; thirty seconds later, again, for as long as the bridge kept its copy. Deleting `build` on your own machine and watching it reappear until you give up is the "never asked twice" rule failing in the one shape nobody had enumerated, and it is why `BACKLOG.md` §4's claim that deletions are "reported and never silently resurrected" was true for files and false here. The mirror image resurrects the ROW rather than the directory: `ADOPT` writes the guest's entry into the baseline and creates the directory on the bridge, undoing a bridge-side deletion with nothing said. Directories DO enter the baseline (`syncrun.py:799` on MKDIR, `:819` on ADOPT), so the ancestor was available the whole time and simply unread. Both now answer the way rows 11-13 already answer it for files -- `REPORT`, touching neither side -- because propagating the deletion is not available: there is no `SYNCDEL` and §4 defers it by decision, so saying so is the honest action rather than a lesser one. The no-ancestor cases still create, and a directory recreated after both sides deleted it is still adopted (the tombstone pops the row, so it is genuinely new); all three are pinned, because a fix that turned one-sided absences into reports could easily have taken the new-directory case with it. PROVEN FAIL-FIRST: reverting the branch makes exactly the two new tests fail, reporting `Act.MKDIR` and `Act.ADOPT` where `Act.REPORT` is now correct.
Fixed: 02.46 `sync_resolve` HAS NO ANSWER FOR THE HALT IT RAISES MOST OFTEN -- `diff` row 15 refuses to pull when the guest holds exactly what the last push replaced, because a stale editor buffer writing an old copy back is byte-for-byte identical to a person deliberately undoing the change. The guard is right to exist and it cannot tell those apart, so it preserves the agent's version and latches the path -- and until now the only actions were `release` (lift the refusal, having merged) and `forget` (stop tracking), neither of which says "the user meant it, take theirs". `accept-revert` does, and dropping `base.prev[key]` is the entire mechanism: with no record of what the push replaced, the guest simply differs from the ancestor and the ordinary "the guest moved" row pulls it, so this action decides nothing about content and transfers nothing itself. It also lifts the latch and clears the halt bar when nothing else is withheld, matching `release`. The reply says plainly that the agent's version is NOT kept, because an agent that believes the bridge staged a copy will ask for it instead of re-applying its change. FOUND WHILE THERE, and it is the reason the action count matters: `_sync_resolve_locked` treated "not release" as "forget", so the only guard against an unrecognised action was in `tools.py`, a DIFFERENT FILE -- and the action it defaulted to is the destructive half of the pair. A third action turned a latent trap into a reachable one. It refuses explicitly now, and the test uses a deliberate typo (`acccept-revert`) and asserts the row survives, because a guard that only rejects strings nobody would type is not a guard.
Fixed: 02.47 A TREE RESTORED FROM A BACKUP IS APPLIED ROW BY ROW, EACH ROW CORRECT AND THE OUTCOME WRONG -- the last silent-loss path `BACKLOG.md` acknowledged, and the difficulty is precisely that there is no defective row to find. A file the guest holds at its pre-push content IS a reverted push; a file the guest no longer has IS a deletion; `diff` classifies both correctly and the apply loop would work through fifty of them faithfully, pulling old contents over work already done and reporting the rest, with every individual decision defensible. Fifty of them in ONE pass is not fifty edits, it is one event, and nothing in the per-path design can see an event. `Pass._refuse_aggregate_revert` counts the two rows a restore produces in bulk -- named `S.REVERTED_PUSH` and `S.GONE_ON_GUEST` as module constants, so the counter matches what `diff` DECIDED rather than re-deriving it from a string that could drift -- and refuses the whole pass above BOTH an absolute floor of 5 and a 25% share of the tracked tree. Both, because either alone is wrong in a way the tests pin: a bare share refuses one revert in a three-file tree, and a bare count refuses twelve reverts in three thousand, which is an ordinary afternoon. It is a REFUSAL, not a repair, following the four existing whole-pass refusals: refusing touches neither side, so nothing is lost while a person decides. THE ESCAPE HATCH SHIPPED WITH IT rather than after it, because a refusal with no way out is a trap and a machine that has stopped synchronising and will not say yes is its own kind of data loss: `sync_resolve(action="accept-tree")` is path-less -- deliberately, since the thing being agreed to is a property of the tree and answering per path would mean fifty calls -- sets a one-shot flag, changes no rows, and the next pass's ordinary rows then decide everything exactly as they always would. The flag is consumed by the pass that uses it and re-arms, which is pinned, because a "just this once" switch that is never cleared disarms the guard permanently. A SECOND CONDITION WAS ADDED AFTER REVIEWING THE FIRST VERSION, and it is the difference between a guard and a trap: at least one path must have gone BACKWARDS IN CONTENT, not merely vanished. `REPORT` does not advance the baseline -- it counts and says so -- so a row that vanished from the machine is re-decided identically on every pass, forever. Without that clause, clearing a build directory on your own machine would refuse the pass, refuse it again thirty seconds later, and every thirty seconds after that, with `accept-tree` buying exactly one pass each time. A guard that must be answered forever teaches the answer reflexively, and the one time it matters it gets the same reflex. Vanishing alone is deletion: already handled, already harmless, nothing applied and nothing lost. WHAT THE AGGREGATE SIGNAL IS ACTUALLY FOR is worth stating exactly, because the obvious reading is wrong: the reverted-push rows are ALREADY safe individually -- row 15 preserves the pushed version and conflicts rather than pulling. The rows that lose bridge-side content are the plain `PULL`s for paths the guest edited but the bridge never pushed, and those are INDISTINGUISHABLE per row from an ordinary edit, because whether content is older cannot be read off the content. So the identifiable signatures licence refusing the rows that are not identifiable, which is the whole point of an aggregate check and is why it must stop the pass rather than filter it. PROVEN FAIL-FIRST twice: disabling the check makes exactly three tests fail, and removing the regression-evidence clause makes the mass-deletion test fail.
Fixed: 02.48 THE TURN-DEGRADED FIX OVERSHOT, AND THE OVERSHOOT IS THE WORSE OF THE TWO ERRORS -- the earlier rate-limit work (`GuestSession.turn_degraded`) made any turn that saw a `RateLimitEvent` report `ok=0`, on the evidence of one observation: a rate-limited turn that came back `ok=True` HAVING PRODUCED NOTHING, where "done $0.00" sat directly beneath a notice saying the request may not have run. The premise was written into the comment as though it were general. It is not. The same event arrives on a turn the SDK RETRIES AND COMPLETES, and the smoke test on the real machine caught it doing exactly that: the notice appeared, `mcp__machine__exec` ran `dir C:\RETRO`, the real directory listing rendered, the chat list picked up the title -- and the status bar said **failed**. That is the identical contradiction between the transcript and the status bar that the first fix existed to remove, wearing the opposite sign, and it is worse rather than symmetrical: a user who reads "failed" under a full and correct answer stops believing the status bar, and the one time it is right they will not believe it then either. The verdict now needs BOTH halves -- degraded AND nothing to show for itself -- with `GuestSession.turn_produced` set only from the SDK's own `on_text`/`on_tool` callbacks, so a notice the BRIDGE wrote (the sync preamble, the rate-limit warning itself) cannot mark its own turn as answered and disarm the first case. Lifted out of the middle of `_run_turn_locked` into `turn_verdict()`, because a decision this easy to get backwards should be callable by a test rather than reachable only by having the account rate-limited at the right moment; four tests pin both directions and the two undegraded ones. PROVEN FAIL-FIRST: restoring the previous one-sided reading fails exactly the "answered anyway" case. FOUND BY THE SMOKE TEST, not by the suite -- the third finding in two days that only the real machine produced, and the one that most deserves the standing rule it vindicates.
