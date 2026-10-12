# Stage 3c — several guest machines against one bridge

## Context

`ROADMAP.md:37` is the whole of it: *"two guests hold two mirrors and two baselines at once."*
`BACKLOG.md` §7 (`:576-763`) holds the decision record and an implementation list; §21
(`:2172-2311`) holds the question it defers to this stage.

**The bridge holds one guest.** `ConnectionService.guest` (`connection.py:57`) is a single
slot. Two consequences, the second a live bug:

- A second **simultaneous** connection is refused process-wide, not per machine
  (`connection.py:209-213`).
- A second **sequential** connection silently inherits the first machine's session.
  `connection.py:280` is `is_reconnect = self.guest is not None` — nothing compares HELLO's
  `machine`. Attach replaces the link, caps, prefs and tools and leaves `guest.claude` (the
  live conversation), `work_dir`, `grants`, `mode_id`, `perms`, `sess_id` and the transcript
  belonging to the previous machine. Both of this project's machines connect to the same
  bridge, so it is reachable today. **Fixed by this stage's first commit**, by the keying
  change rather than a separate guard, so it becomes impossible rather than guarded against.

## Decisions

1. **Scope: two machines, two different conversations, at the same time.** §7's answer is one
   mirror per *conversation* (`:578-587`). One conversation is never driven from two machines —
   the MCP surface forbids it anyway, the server being named `machine` with no target argument
   (`tools.py:1805,1820`). Say so in the stage's text, because "several clients" reads like the
   other thing.
2. **The machine id and the baseline identity stay separate.** *(this is §21's open question)*
   `machine` is a persistent GUID identifying *who*; `tree_id` is volume-derived and exists to
   **refuse a swapped or reformatted disk** (`syncrun.py:204-208`). Baselines are keyed on
   **(machine, guest_root)**: today `base_path` keys on the guest-root string alone
   (`sync.py:97-138`), so two machines whose INI root is `C:\WORK` share one baseline file with
   different `tree_id`s and `scan_guest` then refuses both ways forever.
   **Nothing is migrated and nothing is deleted** — the legacy `base.json` rule
   (`sync.py:112-118`) stays, so the machine that already owns that file keeps it and only a
   *second* machine on the same root gets a machine-keyed name. The review found the first
   draft would have re-keyed the Athlon's live baseline to a new path, i.e. an empty baseline
   and a first pass that reads the whole tree as new. `self.base_paths`, the per-root memo
   (`sync.py:57,137`), is keyed with it.
3. **A client that declares no machine id gets one slot, and a second is refused** with `busy`
   and a log line. Absent-means-any (`conversations.py:226-230`) is about chat *visibility*; it
   cannot identify a connection, and two id-less clients are exactly the inheritance bug again.
4. **`machine` as the reconnect identity is an unauthenticated assertion, and that is accepted
   in writing.** *(the review found this unaddressed)* `BACKLOG.md:723-730` / audit 04.21 ask
   for precisely this: narrowing "reject, don't evict" to "this id is connected" means a client
   claiming another's id inherits its transcript and its pending permission prompts. There is
   one shared secret and it already grants everything; writing the risk down is the condition
   for building on it rather than discovering it later. HELLO's `sess` stays unread — and the
   first draft's reason for that was backwards: `sess` is written from `SWITCHED`'s id
   (`mainwindow.cpp:506-509`), it is *not* the per-connection handle. The conclusion survives,
   the reason does not.
5. **Folder names stay globally unique across machines.** `Conversations.taken()` has no machine
   dimension and should not grow one: the host mirror root is one shared `config.sync_dir`.
6. **An entry leaves `_guests` when its guest has been lost past the reconnect grace**, and the
   eviction closes `claude`. *(new; the review found nothing ever removed one)* `reap_idle`
   deliberately does **not** clear the slot today (`connection.py:470-474`) so a reconnect
   reattaches the same session — free with one slot, but with a dict it retains one
   `GuestSession`, SDK subprocess and perms desk per machine id ever seen, and ids do churn
   (`machineid.cpp:135-145` re-mints over a malformed file).

## Commits

- **D0 — the plan, and `ROADMAP.md` gains a real 3c section.** No code. §7's implementation
  list is re-checked against the tree first: several items are already done, and it cites
  `self.sync_dir` where audit 04.19 is `self.cwd` (`docs/audit/04-planned-features.md:870`).
- **D1 — `self.guest` becomes `self._guests: dict[str, GuestSession]` keyed by machine id.**
  Admission and `is_reconnect` narrow to "this machine". **This is the commit that fixes the
  inheritance bug.** A `guest` property is kept so the rest stays green — and it must be
  **settable**, not read-only: five test sites assign it (`test_session_lifecycle.py:452`,
  `test_sync_tools.py:52,63,91`). Its behaviour with two entries has to be **defined here, not
  left**: `reap_idle`, `ConnectionService.synccoord`, `Bridge.close` and
  `SyncService._get_guest(None)` all resolve through it, and D1's own test creates two entries.
- **D2 — the loops iterate**: `reap_idle` (`connection.py:430-437`), `sweep_sync`
  (`sync.py:582-588`), `Bridge.close` (`server.py:331-353`), and the per-Bridge `_reaper` /
  `_sweeper` (`server.py:546-550`). Eviction (decision 6) lands here.
- **D3 — baselines keyed on (machine, guest_root)** with the legacy rule preserved (decision 2).
  The test is two machines, same guest root, **different conversations**, different `tree_id`s,
  both syncing — the first draft wrote "same conversation", which contradicts decision 1 and is
  not reachable. `test_sync_wire.py:2267-2290` does **not** already model this; it is a
  notice-dedup test that only *describes* the deadlock in its docstring.
- **D4 — one codepage per guest.** `fold`'s ContextVars (`wire/fold.py:56-72`) are per asyncio
  context; the sweeper re-binds (`sync.py:591-598`) and **`reap_idle` does not** — it calls
  `guest.perms.deny_all` (`connection.py:444-468`), writing frames for guest B through guest
  A's codepage. Both loops re-bind.
- **D5 — `_preattach_synccoord` dies** (`synccoord.py:30-39`: *"the single-client shortcut,
  named and in one place, for R7 to delete"*). It must say where a pre-attach notice goes with
  N guests: `test_session_lifecycle.py:446-458` asserts one queued before any client connects is
  still delivered, and that test assigns `connection.guest = None`.
- **D6 — the `guest` property goes.** The first draft called this small on a count of "~20 call
  sites"; it is **187 across 13 test files** plus 16 in `bridge/`, so it is the largest commit
  of the stage and may split. What it is *for* is the implicit-guest fallbacks: `guest_exec.py:60-63`
  **and** `SyncService._get_guest` behind eleven optional-guest methods, reached with no guest
  from `server.py:151,435`. `recent_prompts` (`guest.py:200`), cleared nowhere, is cleared here.
  Audit 04.19 (`config.cwd`'s three fallbacks: `server.py:362`, `workspace.py:73`,
  `sync.py:605`) is assigned here rather than left unowned.
- **D7 — two machines at once, on the real hardware**, and the docs.

---

## Verification

Most of D1-D6 is testable with two fake clients in the async tests, which is why it comes
before hardware. The real proof is D7 with both machines powered: two conversations, two
mirrors, two baselines, concurrently. Watch for the chat lists staying disjoint, the
baselines not refusing each other, a halt bar on one not appearing on the other, and `exec`
landing on the machine whose conversation asked for it.

## Risks

1. **D1 changes admission.** A bug there locks the person out of their own bridge, and the
   id-less rule is the sharp edge: refuse the SECOND such client, never the first.
2. **D6 is 187 call sites** across 13 test files plus 16 in `bridge/`. If it cannot be made
   one reviewable commit it splits by caller family, never by leaving a fallback half
   removed.
3. **Eviction is new behaviour on a path that deliberately had none.** `reap_idle` keeps the
   session alive so a reconnect reattaches it; getting the grace wrong loses a transcript
   that would have come back.
