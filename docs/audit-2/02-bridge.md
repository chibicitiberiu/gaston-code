# 02 — Bridge audit: bridge/

Audit date 2026-09-21, against `master` at 6299e26.

**Scope.** `bridge/server.py`, `bridge/agent/*`, `bridge/view/*`,
`bridge/status.py`, `bridge/handshake.py`, `bridge/synccoord.py`,
`bridge/devtools/fakegui.py`. `bridge/files/` and `bridge/wire/` are another
auditor's; they were read where a caller's behaviour depends on them
(`GuestLink.send_html`/`send_export`, `fold.use`, `caps.Caps`,
`paths.component_ok`) and are not themselves findings here.

**Method.** Every module in scope read end to end. `docs/audit/02-bridge-python.md`
read first — all 48 of its findings are marked `Fixed:` and none is repeated
below. `docs/BACKLOG.md` §§1–11c read so that deliberate deferrals (the
transcript cache, "Always" across a rebuild, the chunked-transfer primitive,
04.35's first-tool-call ordering, R7's seven singular fields) are not reported
as defects. Where a verdict could be checked by running code it was:
`cmdguard.reason` was driven over 27 command lines against a real
`command.com` profile, and `_default_shell`'s degradation was reproduced by
injecting a second `[shell]` profile. Tests in `tests/python/agent/`,
`tests/python/view/`, `tests/python/server/` and `tests/python/meta/` were read
for coverage of each claim.

**Summary.** The code is still unusually well reasoned, and the previous
round's structural work landed: the god object is genuinely smaller, the
dispatch table exists, the halt/latch reconciliation is honest in both
directions, and the exec/sync/permission state machines are typed. The defects
below cluster in one place the previous round did not look at: **what happens
when two things that both own `guest.claude` or the frame pump run at once.**
Five of the top eight are that. The tool guard is sound in its intent and
correctly advisory; its problems are that it shouts on every command in the
default deployment, and that it turns itself off the day a second shell profile
is added.

---

## 02.1 A conversation switch during a live turn corrupts the new transcript and wedges the guest

**What.** `guest.turn_lock` is taken by exactly two callers — `_run_turn`
(`bridge/server.py:2132`) and `_resume_turn` (`:2103`). The three methods that
destroy and replace the object a turn is currently running on do **not** take
it:

* `_switch` — `bridge/server.py:3677-3680` (`_abandon_pending`,
  `await guest.claude.close()`, `guest.claude = None`)
* `_new_session` — `bridge/server.py:3612-3655` (same, plus
  `await guest.reset_transcript()` at `:3634`)
* `_set_effort` — `bridge/server.py:3554-3556` (`close()` then `_make_session`)

All three are reached from `_handle`, which runs in `_pump`'s task, while
`_run_turn_locked` is suspended inside `await guest.claude.send(text)`
(`:2177`) in a task spawned by `_on_input` (`:1725`). They are concurrent by
construction, not by race.

**Failure scenario.** The model is streaming an answer. The user clicks a
conversation in the chat list.

1. `_switch` calls `guest.reset_transcript()` (`:3721`), which replaces
   `view.pages` and `view.render` and clears `view.coalescers`
   (`bridge/view/transcript.py:74-79`).
2. The suspended turn's SDK callbacks keep firing. `on_text` →
   `_emit_text` → `_coalescer(guest, "plain")` (`:1590`) finds no coalescer,
   builds a fresh one over the **new** `guest.render`, and the old
   conversation's remaining prose is appended to the conversation the user just
   opened — recorded into its paginator, so it is also in its Export and on its
   sealed pages. Same for `on_tool` / `on_tool_result`, which go through
   `_write_block` directly.
3. When `close()` makes `receive_response()` end without a `ResultMessage`,
   `_consume` returns `Turn(ok=False, subtype="error_stream_ended")`
   (`bridge/agent/session.py:346`) rather than raising, so `_run_turn_locked`
   falls through its `except` and reaches
   `self.registry.add(guest.claude.session_id)` at `bridge/server.py:2187`
   with `guest.claude` now `None` → `AttributeError`. The task dies inside
   `_spawn`, which never inspects the result (02.13), so **no `TURN state=end`
   frame is ever sent**: the guest's Send button stays disabled and
   `g_turn_active` stays set until the link is re-established.

The tell that this line is wrong is eleven lines further down: `:2236` guards
the *same* object — `await guest.claude.context_usage() if guest.claude else
None` — for the same reason, and `:2187` does not.

**Fix.** Take `guest.turn_lock` in `_switch`, `_new_session` and `_set_effort`
before touching `guest.claude` (interrupting the live turn first, so the wait is
bounded), and guard `:2187` the way `:2236` already is. There is no test for a
session-changing frame arriving mid-turn anywhere in
`tests/python/server/test_server_integration.py` —
`test_a_switch_denies_what_the_old_conversation_left_pending` (`:1096`) covers
pending *prompts*, not a running turn.

---

## 02.2 `SETMODEL` is the one control path with no error handling, and it drops the connection

**What.** `_on_setmodel` (`bridge/server.py:1817-1830`) calls
`await guest.claude.set_model(model)` bare. Its two siblings do not:
`_set_mode` wraps `set_permission_mode` in `try/except` (`:3582-3590`) and
`_set_effort` wraps `_make_session` (`:3558-3563`). An exception out of
`_on_setmodel` propagates through `_handle` → `_pump` → `_on_client`'s
catch-all `except Exception` (`:639`) and the connection is torn down.

**Failure scenario.** This is not hypothetical, and the project already
measured the cause. `_new_session`'s own comment (`:3636-3643`) records that
"the CLI refuses control requests against [a client that existed but had never
run a turn] — so picking a mode said 'Could not change the permission mode'".
That message is `_set_mode`'s except branch firing. `_attach` still builds a
session eagerly on a fresh connection (`:860-861`), so a client that has just
connected and has not yet typed anything holds exactly such a never-run
`Session`. Open the Model menu before sending the first message and the control
request is refused — `_set_mode` would have written an error block; `_on_setmodel`
takes the link down instead. The guest then reconnects and, because `_attach`
builds another never-run session, the next press does it again.

Secondary: `guest.model = model` at `:1824` happens before the call and is
persisted by `_remember_settings`, so the refused model is written to
`settings.json` and applied to the next session built. `_set_mode` deliberately
validates its header against `MODE_ORDER` because "it still arrives off a
socket" (`:3571-3575`); `SETMODEL` validates nothing.

**Fix.** Wrap the `set_model` call the way `_set_mode` wraps its own, report
through `H.error` into the transcript (the channel 02.16 settled on), and
re-send the options so the menu re-ticks the model actually in force.

---

## 02.3 A long outbound transfer blocks the frame pump, and the idle reaper reads that silence as a dead guest

**What.** `guest.last_seen` is advanced in exactly one place: `_handle`
(`bridge/server.py:1708`). `_pump` (`:1700-1705`) does not read the socket
again until the handler it is awaiting returns. Two handlers can run for a long
time while sending: `_on_export` (`:1934-1951`, which awaits
`guest.link.send_export` for the whole document) and `_send_page` for a full
page. `_reap_idle` (`:3332-3376`) exempts `guest.exec_pending` and
`guest.sync_active` but knows nothing about a transfer in progress, and
`send_export` yields between chunks (`bridge/wire/link.py:198-199`), so the
reaper *does* get scheduled during the export.

**Failure scenario.** File > Export as HTML on a long conversation. The
document is `guest.pages.all_text()` — the whole transcript, uncapped. At
`--link-bps 5000` (the dialup path the project explicitly targets), 450 KB
takes 90 s; on a real 200 MHz guest writing each chunk to disk, `drain()`
stalls well before the nominal link rate. At 90 s of no inbound frames the
reaper logs "guest silent", calls `perms.deny_all("idle", ...)`, and closes the
writer (`:3371-3376`). `send_export`'s next `send` returns False, it tries to
send `export_end(status=error)` on the same dead writer, and
`win/app/export.cpp` throws away the temp file. The user asked for a file, the
bridge tore its own connection down to deliver it, and any permission prompt on
screen was denied as a side effect.

**Fix.** Either advance `last_seen` on a successful send (the guest being able
to receive is at least as good evidence of liveness as it talking), or set a
`guest.transferring` flag that `_reap_idle` exempts alongside `exec_pending`,
or spawn `_on_export` with `_spawn` so `_pump` keeps reading. The first is the
smallest and also fixes `_send_page`.

`tests/python/server/test_server_integration.py:954` exercises the export but
against an in-memory pipe with no reaper running.

---

## 02.4 On the default deployment the guard's warning eats the permission bar, and the command is what falls off the end

**What.** `PermissionDesk.callback` builds the bar line as
`"! %s -- %s" % (warning, perms.summary(...))` (`bridge/agent/permdesk.py:196-199`)
— warning first. `perms.summary` clips its half to 160 characters
(`bridge/agent/permissions.py:249`); the concatenation is not clipped again.
The guest renders this in a single-line `SS_ENDELLIPSIS` static
(`win/app/bar.cpp:84`), so roughly the first seventy characters are what the
user actually reads.

`cmdguard.reason` returns `"no folder is mirrored from this machine, so nothing
on it can be restored"` — 73 characters — whenever `guest_root` is empty
(`bridge/agent/cmdguard.py:459-461`). `guest_root` is
`guest.work_guest`, and `_name_work_dir` returns without setting it when
`self.sync_dir` is unset (`bridge/server.py:1151-1153`) or when the guest's
HELLO carries no `sync_root` (`:1157-1158`).

**Failure scenario.** A bridge started without `--sync-dir` — or any guest
whose INI has no sync root — prefixes **every single** `mcp__machine__exec`
prompt with that sentence. On a 640×480 bar the user sees
`! no folder is mirrored from this machine, so nothing on it can be re…` and
nothing at all of the command they are being asked to approve. The module's own
docstring names this failure: "someone who learns the guard's phrasing is wrong
stops reading it" (`cmdguard.py:259-261`); a warning that fires on 100 % of
calls is the same defect in a different form, and here it also removes the one
piece of information the decision rests on.

**Fix.** Two separable changes. (a) Put the command first and the marker last,
or clip the combined line so the summary's share is preserved. (b) Treat "there
is no mirror at all" as *not a per-command finding*: say it once, when the
connection has no mirror, rather than on every prompt — the honest reading of
`reason()`'s contract is that this is a property of the deployment, not of the
command.

**No test.** Both guard tests (`test_a_command_reaching_outside_the_mirror_is_announced`,
`test_a_contained_command_gets_no_added_reassurance`,
`tests/python/server/test_server_integration.py:2246` and `:2269`) set
`harness.bridge._guest.work_guest = "C:\\WORK"` by hand. The state every
sync-disabled bridge is actually in is not covered.

---

## 02.5 The guard turns itself off the day a second shell profile gains `[shell]` tables

**What.** `grammar_for("")` falls back to `_default_shell()`
(`bridge/agent/cmdguard.py:215-222`), which returns the single profile carrying
guard tables if there is exactly one and `""` otherwise
(`:64-81`). `""` yields `NOTHING`, an unusable grammar, and `reason()` answers
"the shell on this machine is not one this bridge has rules for" for every
command (`:454-456`).

The caller passes `""` for every guest that does not report an inventory:
`PermissionDesk._shell` is `toolrules.shell_of(guest.tools)`
(`bridge/agent/permdesk.py:97`), and `guest.tools` comes from HELLO's body,
which `bridge/server.py:1463-1465` says is "empty for any guest built before
the body existed, which is every deployed one until it is reflashed".

**Failure scenario.** Reproduced, not reasoned about:

```
shells now: ['command.com']        default: 'command.com'   -> guard works
# add a [shell] section to bridge/profiles/4dos.toml
shells after: ['4dos', 'command.com']   default: ''         -> guard is off
reason(r"del c:\io.sys", shell="") == "the shell on this machine is not one
    this bridge has rules for, so what this command touches cannot be known"
```

So adding support for a second shell — the exact change
`bridge/agent/profiles.py`'s docstring says must be "adding a file" — silently
degrades every legacy guest to the always-warn state described in 02.4.
`tests/python/meta/test_no_os_in_code.py:102` iterates `profiles.shells()` and
asserts each has the tables; nothing asserts the guard still decides anything
once there are two.

Worse, `permdesk._shell`'s docstring claims the opposite of what the code does:
"an empty answer is honest: it then refers every command to the user rather
than judging it by whichever profile happened to be the only one installed"
(`permdesk.py:92-95`). Today it *does* judge by the only one installed, which
is the behaviour that works. Two readers of this code will disagree about which
half is the bug.

**Fix.** Decide which reading is intended and make code and comment agree. The
defensible one: keep the single-profile fallback (it is correct today), and add
a rule for "several profiles, caller said nothing" that picks the one HELLO's
`os` header implies rather than giving up — plus a meta-test that asserts a
two-shell installation still returns a usable grammar for a silent guest.

---

## 02.6 `fold.use` binds the codepage after the reaper and sweeper tasks have already copied the context

**What.** `fold.use` sets two `ContextVar`s (`bridge/wire/fold.py:61-75`) and
is called in `_attach` at `bridge/server.py:818`. `asyncio.create_task` copies
the *current* context at creation, and `_reap_idle` and `_sweep_sync` are
created at `bridge/server.py:610` and `:612` — near the top of `_on_client`,
before `_read_hello`, and therefore before `fold.use` has run. A later `set()`
in `_on_client`'s context does not propagate into tasks already created.

**Failure scenario.** A client declaring `acp=1251`. Sync passes triggered by
the guest (`_on_syncdirty` → `_spawn`, inside `_handle`, whose context
descends from `_attach`) encode correctly. Passes triggered by the bridge's own
120-second backstop (`_sweep_sync` → `run_sync`, `:3321-3330`) run in a context
where `fold.codec()` is still the default `cp1252`. Every
`await self._write_block(guest, H.notice(note))` in `run_sync` (`:2469`) and
every `_report_abort` notice (`:2625`) is then folded to CP1252 and delivered
inside a document whose `<meta charset>` says `windows-1251`
(`bridge/view/html.py:582`) — so the sweeper's sync notices are mojibake while
the guest's own are correct. `Frame.encode` also runs header values through
`fold.encode` (`bridge/wire/protocol.py:62-75`), so any header a sweeper-started
pass composes is quoted against the wrong table too.

This is the kind of bug that never shows on a `cp1252` machine and is
undiagnosable on any other, because the two code paths that produce it are
indistinguishable from the outside.

**Fix.** Create the reaper and the sweeper *after* `_attach` has bound the
codepage (they are guarded by `is None` anyway, so moving them costs nothing),
or have long-lived background tasks bind the codepage themselves from
`guest.caps` before they do any encoding. The second is more honest once R7
gives the bridge two guests with different codepages.

---

## 02.7 The sync preamble silently turns a slash command into prose

**What.** `_run_turn_locked` captures `was_command = text.lstrip().startswith("/")`
at `bridge/server.py:2165` and then rewrites `text` at `:2175` via
`_with_sync_preamble`, which returns
`"\n".join(lines) + text` (`:2071`) — the user's message is now preceded by
about twenty lines beginning `[Automatic message from the file synchronisation
layer, not from the user.]`.

The comment at `:2162-2164` notices the ordering problem for the *verdict* ("the
leading `/` this is testing for would no longer be leading by the time the turn
ends") and fixes that. Nothing protects the command itself.

**Failure scenario.** A conflict happens and a notice is queued. The user opens
the Commands menu and picks `/compact` (`COMMAND_MENU`, `:3887`). The CLI
receives a prompt that does not start with `/`, so it is not a slash command: it
is a plain message asking the model to resolve a sync conflict, followed by the
literal text `/compact`. The context is not compacted, nothing says so, and
`was_command and not guest.turn_produced` is False (the model answered the
preamble), so even the "that command finished without sending anything back"
notice at `:2227-2230` does not fire. The same applies to `/rename` and
`/init`, and to anything the user types with a leading slash.

**Fix.** When `was_command`, either hold the preamble for the next non-command
turn, or send it as a separate leading turn, or append it after the command
line rather than before. Holding is the cheapest and matches the existing
"say it once, on the next turn" discipline — the in-memory queue is already
drained only when it is delivered.

---

## 02.8 The idle reaper defeats `_on_client`'s own teardown, so a prompt raised after a reap waits ten minutes instead of sixty seconds

**What.** `_on_client`'s `finally` does all its per-drop bookkeeping under one
condition: `if self._guest and self._guest.writer is writer`
(`bridge/server.py:642`). Inside it: `lost_at` is stamped (`:651`),
`_fail_syncs` and `_fail_execs` run (`:657-662`). The reaper, however, nulls
`guest.writer` itself before closing the socket:
`w, guest.writer = guest.writer, None` (`:3371`).

**Failure scenario.** The guest GPFs. Ninety seconds later the reaper fires,
denies the pending prompt, nulls `writer` and closes the socket. The parked
`reader.read()` returns EOF, `_pump` returns, and `_on_client`'s `finally`
finds `guest.writer is writer` False — because the reaper already cleared it —
and skips the whole block. `guest.lost_at` stays `None`.

Now the turn that was running raises a *new* permission request (the SDK does
not cancel in-flight tool calls; `tools.py:238-245` records this). `_enqueue`
→ `_show` → `_send(permreq)` returns False with no writer, and the prompt sits
pending. On the next sweep `_reap_idle` reaches
`guest.perm_pending is not None and guest.lost_at is not None` (`:3347-3349`),
`lost_at` is `None`, and the grace denial never fires. The prompt waits the full
`perms.DEADLINE_S` of 600 s instead of `perm_grace_s` of 60 — against the
explicit intent at `:571-574` ("long enough for the guest's 1/2/4/8s reconnect
backoff to win, far short of the 10 minute answer deadline"). Any sync or exec
registered after the reap is likewise never failed.

**Fix.** Have the reaper set `guest.lost_at = time.monotonic()` and run
`_fail_syncs`/`_fail_execs` itself, or key `_on_client`'s `finally` on a
per-connection token rather than on the writer identity the reaper is allowed to
mutate. `test_idle_guest_is_reaped_so_it_can_reconnect` (`:1675`) asserts the
reconnect works and does not look at `lost_at`.

---

## 02.9 Abandoning a conversation fails the exec bridge-side but never tells the guest to stop

**What.** `_abandon_pending` (`bridge/server.py:1260-1275`) calls
`guest.perms.deny_all(...)` and `self._fail_execs(...)`. `_fail_execs`
(`:3301-3314`) resolves the futures and clears `exec_pending`. It sends no
`EXECKILL`.

`_on_interrupt` (`:1774-1788`) does the opposite and explains exactly why:
"Without telling the guest, the command keeps running over there and the NEXT
call is answered `busy` — so one interrupted turn poisons the following one for
a reason the model cannot see or act on."

**Failure scenario.** A command is running on the guest (`pause`, a compile —
up to the 120 s ceiling). The user switches conversations, presses New chat, or
changes effort; all three call `_abandon_pending` (`:3677`, `:3612`, `:3544`).
The bridge forgets the call and releases `exec_lock`; the guest's single worker
thread keeps running the child. The first `exec` in the new conversation is
answered `busy` (`ExecStatus.BUSY` → "The connected machine is already running
a command", `bridge/status.py`), for a command the model in that conversation
never issued and cannot see.

**Fix.** `_fail_execs` should `_kill_exec` each entry before clearing, or
`_abandon_pending` should do what `_on_interrupt` does. `_kill_exec` is already
deduplicated and already synchronous-safe (`:2378-2393`), so it is a two-line
change.

---

## 02.10 The guard decides "path or switch" by length, and a swallowed destination reads as contained

**What.** `Guard.is_switch` (`bridge/agent/cmdguard.py:332-351`) calls a token a
switch when it starts with the shell's switch prefix, has no separator after it,
and is at most `switch_max` characters — 8 for `command.com`
(`bridge/profiles/command.com.toml:57`). On COMMAND.COM `/` is *also* a path
separator, which the docstring acknowledges. `Guard.targets` (`:385-394`) then
filters switches out before deciding what a verb writes.

**Failure scenario.** Measured, driving `reason()` against the real profile with
`guest_root = cwd = C:\GASTON\OK`:

```
ASK   del c:\io.sys                     -> writes outside the mirrored folder
ASK   del /io.sys                       -> del with no destination this can resolve
PASS  copy c:\gaston\ok\a.txt /io.sys
PASS  move c:\gaston\ok\a.txt /temp
PASS  dir\ndel c:\io.sys
```

For `del` and `deltree` the hole closes itself: every argument is swallowed,
`targets` is empty, and the "no destination this can resolve" branch (`:510`)
asks anyway. For `copy`/`xcopy` (`src_dest`) and `move` (`move_like`) it does
not: the source is still a path, `targets` returns it, it is inside the mirror,
and the command reads as fully contained. The last row is a separate miss —
`separators` (`:31` of the profile) does not include a newline, while
`split_args` treats `\n` as ordinary whitespace, so a leading read verb hides
whatever follows it on the next line.

Neither is a high-confidence exploit against COMMAND.COM specifically —
`COPY /IO.SYS` is likely rejected as an invalid switch by COPY itself — which is
why this sits at 02.10 rather than higher. It is reported because the *rule* is
wrong rather than the instance: "short and separator-free" is not what makes a
token a switch, and the bound is a number in a data file that anybody adding a
shell will copy.

**Fix.** Treat a `/`-prefixed token as a switch only when it matches the shell's
declared switch *vocabulary* (a short letter, optionally with `:value`), not
merely by length; and where a `write`/`src_dest` verb's destination was
discarded as a switch, say so rather than falling through — the same "cannot be
resolved" answer `del` already gets. Add a newline to `command.com`'s
`separators` (over-splitting is the safe direction; the profile note already
records that a newline does not chain commands on that shell, so splitting on it
can only produce extra prompts).

`tests/python/agent/test_cmdguard.py` has 378 lines and no case where a
destination is `/`-rooted and short.

---

## 02.11 A device name inside the mirror is reported as being outside it

**What.** `Root.contains` returns False for a path whose leaf is a reserved
device (`bridge/agent/cmdguard.py:431`), and every caller turns a False into
`"writes outside the mirrored folder: %s"` (`:479`, `:516`).

**Failure scenario.** `copy x c:\gaston\ok\con` is inside the mirror and is
refused for a completely different and correct reason — `CON` is a device in
every directory. The user is told it writes outside the mirrored folder, which
is false and checkable. This is the same class the module warns about at
`:258-261`: a guard whose stated reason is demonstrably wrong is a guard people
stop reading.

**Fix.** Have `contains` distinguish "not inside" from "inside but a device" —
return a small reason rather than a bool — and give the device case its own
sentence.

---

## 02.12 A malformed numeric HELLO header escapes negotiation as `ValueError`, not `Refused`

**What.** `handshake.negotiate` does `int(headers.get("max_frame", "0") or 0)`
(`bridge/handshake.py:265`) and `int(headers.get("mem_kb", "0") or 0)`
(`:282`) outside any `try`. `_attach` catches only `handshake.Refused`
(`bridge/server.py:732`).

**Failure scenario.** A guest (or anything else that reaches the port and knows
the secret — or reaches `max_frame` before the secret is checked, which it does,
since the frame-cap check is at `:265` and the secret at `:271`) sends
`max_frame=64k`. `ValueError` propagates to `_on_client`'s catch-all, which logs
`"unhandled error serving %s"` with a traceback and closes the socket **without
sending an ERROR frame**. The guest sees a bare disconnect, applies its
1/2/4/8 s backoff, and retries forever; the bridge logs a traceback per attempt.
Every other refusal on this path sends a typed error the guest can render.

Note this is reachable before authentication, so it is also a log-flood a
non-authenticated peer can trigger.

**Fix.** Parse both inside a `try` and raise `Refused("bad_frame_cap"/"bad_mem", ...)`.
The module exists precisely so these are unit-testable
(`bridge/handshake.py:193-198`); there is no test for a non-numeric header.

---

## 02.13 `_spawn` never looks at what its task did

**What.** `Bridge._spawn` (`bridge/server.py:1580-1588`) adds a done callback
that only discards the task from `self._tasks`. Nothing retrieves the
exception.

**Failure scenario.** Everything fire-and-forget goes through here: turns
(`:1725`), the Continue turn (`:1767`), sync passes (`:905`, `:1772`),
`_kill_exec`'s EXECKILL (`:2393`), `_send_sessions` on the `init` message
(`:1434`), `_clear_halt` from `sync_resolve` (`:2958`). A raise in any of them
— 02.1's `AttributeError` is the live example — produces no log line at the
moment it happens. It surfaces only as asyncio's "Task exception was never
retrieved" whenever the task object is garbage collected, which may be much
later and is easy to read past. The project's own standard elsewhere is loud:
`run_sync` distinguishes a refusal from a bug and logs the traceback
(`:2553-2562`).

**Fix.** Make the done callback log `task.exception()` at `log.exception` level
(ignoring `CancelledError`). Four lines, and it is what would have surfaced
02.1.

---

## 02.14 `close()` leaves the sweeper and every spawned task running

**What.** `Bridge.close` (`bridge/server.py:587-595`) cancels `self._reaper`,
closes the server and closes the Claude session. It does not cancel
`self._sweeper` (created at `:612`) and does not cancel anything in
`self._tasks`.

**Failure scenario.** A bridge shut down cleanly leaves `_sweep_sync` looping
every 120 s, calling `run_sync` against a guest whose writer is gone and a
Claude session already closed. In-process (the test suite, and any future
embedding) this is a task that outlives its owner and an asyncio shutdown
warning; `run_sync` will return early on `not guest.connected`, so it is noise
rather than damage — but it is the same omission R7 will have to fix anyway
(BACKLOG §7 lists `close()` among the "loops over one guest" sites).

**Fix.** Cancel `_sweeper` beside `_reaper`, and cancel-and-gather
`self._tasks`.

---

## 02.15 The Rules view has nothing to say about Auto, which is on the menu

**What.** `MODE_ORDER` offers four modes including `auto`
(`bridge/server.py:3840`), and `MODE_CAVEAT` has a carefully measured paragraph
about what Auto does not do (`:3845-3851`). `permissions._MODE_LABELS` and
`_MODE_MEANS` have three entries each and neither includes `auto`
(`bridge/agent/permissions.py:432-440`, `:490-491`).

**Failure scenario.** In Auto mode, pressing Rules renders
`<b>Mode: auto</b> &mdash; ` — the raw mode token and an empty explanation —
on the one screen whose whole job is answering "what can run without asking?".
`test_the_rules_view_follows_the_mode`
(`tests/python/server/test_server_integration.py:924`) sets `plan` and asserts
`"Mode: Plan"`; it is parameterised over nothing, so the gap is invisible.

**Fix.** Add the `auto` entries, reusing `MODE_CAVEAT["auto"]`'s wording, and
parameterise that test over `MODE_ORDER` so the next mode added cannot ship
without a description.

---

## 02.16 `fakegui` has drifted from the guest again, in the two places it matters most

**What.** Two gaps, both in the tool whose stated job is "if fakegui works and
the guest does not, the bug is in the C".

* **No keepalive.** `bridge/devtools/fakegui.py` sends `PING` only from the
  typed `/ping` command (`:529`). The real guest sends it on its sweep —
  that is the half of audit 02.6 that was fixed, and it is what makes the 90 s
  reaper survivable. So a break-glass fakegui session left idle while somebody
  reads a permission prompt is reaped at 90 s, and the conformance harness never
  exercises the keepalive at all.
* **No EXPORT branch.** `render` (`:214-359`) has no case for `EXPORTSTART` /
  `EXPORTDAT` / `EXPORTEND`; they fall into the `else` at `:358` and print
  `[EXPORTDAT {...}] <200 chars of deflated body>` per 63 KB chunk. So the one
  transfer path with a real defect in it (02.3) cannot be driven from the
  conformance tool, and `tests/python/server/test_fakegui_conformance.py` has
  no export test.

**Fix.** Send a `PING` on a timer matching the guest's sweep, and add an
`EXPORT*` sink (open a temp file on START, append on DAT, rename or discard on
END) — the same three-method shape `win/app/export.cpp` already has, which is
what conformance means here.

---

## 02.17 A long code fence is sent as many whole copies of itself, and is then split mid-element

**What.** Two related consequences of streaming and paginating raw HTML.

* `HtmlStream._render(partial=True)` returns the **entire open block** each
  time (`bridge/view/html.py:363-381`), and `_write_updates` sends every
  non-APPEND update as a LIVE frame (`bridge/server.py:1637-1638`). One LIVE
  frame per coalescer flush — every 100 ms on a fast link, 600 ms on a slow one
  (`bridge/wire/coalesce.py:29-40`).
* `Paginator.write` splits a run at `hard_bytes` (2 × the guest's page byte
  cap, so 32 KB on a 32 MB box) preferring a newline
  (`bridge/view/pages.py:196-210`). The run being split is a complete HTML
  element, so page N ends with `<div class="codewrap"><pre>…` unclosed and
  page N+1 opens with `…</pre></div>`.

**Failure scenario.** The model emits a 60 KB fenced file. Live: at 600 ms
flushes over ~40 s, roughly 65 LIVE frames averaging 30 KB — about 2 MB on the
wire for 60 KB of content, on a link the project measures in single-digit
KB/s — until the block passes `MAX_FRAME - 1024` and `send_html` starts dropping
LIVE silently (`bridge/wire/link.py:122-123`), at which point live updating
simply stops with nothing said. Sealed: the block lands as two fragments of
markup on two pages; MSHTML will close the tags itself, so the visible damage is
a code block that loses its frame and a page that starts with stray `</pre>`,
not a crash.

I am confident about the quadratic send (it follows directly from the two cited
lines); I have not measured what IE5 renders for the split fragments, so treat
the second half as a degradation to confirm rather than a proven defect.

**Fix.** For the live half, send a delta rather than the whole block, or stop
re-sending a LIVE block once it exceeds a few KB and let the APPEND carry it —
the drop at `link.py:122` already assumes that is acceptable, so doing it
deliberately and early is strictly better than doing it by accident and late.
For the split, prefer sealing *between* blocks whenever the live page already
has runs, and reserve the mid-run split for the case where one block alone
exceeds the hard cap.

---

## 02.18 Three tools take a `guest` argument and then read `self._guest` anyway

**What.** The single-client assumption was deliberately made visible by giving
the machine-facing entry points a `guest` parameter
(`bridge/agent/tools.py:40-54` explains why). `run_on_guest` honours it. The
other two do not, all the way down:

* `sync_status_text(guest)` (`bridge/server.py:2714`) resolves
  `g = guest or self._guest` and uses `g` for the mirror — then calls
  `self._load_baseline()` → `self._base_path()` → `_current_guest_root()`,
  which reads `self._guest.work_guest` directly (`:2705-2708`), and
  `self._edits_outside_the_mirror()`, which reads `self._guest` and `self.cwd`
  (`:2861-2863`).
* `sync_resolve(path, action, guest)` (`:2894`) passes `guest` to
  `_baseline_lock` and uses `guest.caps` — then writes to `self._base_path()`
  (`:2944`, `:2952`, `:2983`, `:3005`) and clears the halt on
  `self._guest` (`:2957`, `:2985`).
* `Bridge.synccoord` is one `SyncCoordinator` per *Bridge*
  (`:570`), holding the halt id, the answered path set and the agent notice
  queue — so two clients would share one halt bar and one notice queue.
  02.43 moved these onto a type but kept them Bridge-lifetime and said so; the
  R7 item is unchanged, which is worth restating because "SyncCoordinator
  exists" reads like it was closed.

**Failure scenario.** Not reachable today — there is one guest. It becomes a
correctness bug the moment R7 lands: agent A calls `sync_resolve` for its own
conversation and the release is written into whichever baseline
`self._guest.work_guest` currently names, and the halt bar comes down on
someone else's screen. This is the same shape `tools.build`'s docstring calls
"the worst shape available".

**Fix.** Thread the guest through `_base_path`, `_current_guest_root`,
`_load_baseline`, `_edits_outside_the_mirror` and the two `_clear_halt` calls,
and make `SyncCoordinator` a constructor argument of `GuestSession` rather than
a field of `Bridge` — which its own docstring already says is the shape R7
wants (`bridge/synccoord.py:109-113`). Doing it now, while there is one guest
and the tests are cheap, is the whole reason `run_on_guest` already takes the
parameter.

---

## Things that were checked and are fine

Worth recording so the next round does not re-derive them.

* **`Bridge._guest.writer` identity check in `_on_client`'s `finally`**
  (`:642`) correctly avoids a late-closing old connection tearing down a
  live one. Its only flaw is 02.8.
* **`folder_name`'s final guard** (`:997`) reads inverted
  (`"" if out and P.component_ok(out, caps) else out`) but is correct:
  `paths.component_ok` returns `Skip | None` with `None` meaning "fine"
  (`bridge/files/paths.py:138`).
* **`Baseline.announced`** is cleared on a successful pass
  (`bridge/files/syncrun.py:972-974`), so `_report_abort`'s dedup list does not
  grow forever.
* **`ExecStatus.parse` falls back to OK and `SyncStatus.parse` does not**, and
  `bridge/status.py` states the asymmetry and why each direction is the safe one
  for its vocabulary. This is right.
* **`allows_always` refusing the Always button for `mcp__machine__*`**
  (`bridge/agent/permissions.py:126-141`) and `session_scoped` forcing every
  suggested rule to session destination are both correct and both load-bearing.
* **`off_limits` / `_inside_gaston`** use `commonpath` rather than `startswith`
  and guard both `.gaston` locations; the read-through for staged conflict
  copies is correctly restricted to non-write tools.
* **`exec` output capping** is bounded on both sides and attributes the cut to
  whichever side made it (`:3265`, `:3284-3290`).
* **`sessions.listing`** is limited to 50 rows and `history_html` to 2000
  messages, so the chat list and replay are bounded.
* **`toolrep.parse_rows`** honours the stated length prefix, caps at
  `MAX_FACTS`, and never raises — the right posture for a handshake body.
