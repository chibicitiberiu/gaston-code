"""What happens when two things that both own `guest.claude` run at once.

audit-2's 02-bridge.md put five of its top eight findings in this one place,
and they share a shape: `_handle` runs in `_pump`'s task while a turn spawned
by `_on_input` is suspended inside `guest.claude.send()`. They are concurrent
by construction, not by race, and the previous audit round did not look here.

Covered:

  02.1  a conversation switch during a live turn
  02.2  SETMODEL, the one control path with no error handling
  02.3  a long transfer read as a dead guest
  02.7  the sync preamble turning a slash command into prose
  02.8  the reaper defeating _on_client's own teardown
  02.9  abandoning a conversation without telling the guest to stop
  02.13 _spawn never looking at what its task did
  02.14 close() leaving the sweeper running
  02.18 per-guest sync state
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging

import pytest
from harness import _recv, harness  # noqa: F401

import bridge.server as srv
from bridge.guest import ExecCall
from bridge.wire.protocol import Frame


async def _hold_turn(harness, guest, *, unwind=0.0):
    """Start a turn and leave it suspended inside `send()`.

    `interrupt()` releases it, which is what the real SDK does and what
    makes `_quiesce_turn`'s wait bounded. `unwind` delays that release, so a
    test can observe the caller actually parked on the lock rather than
    racing an instantaneous one.
    """
    gate = asyncio.Event()
    session = guest.claude
    real_send, real_interrupt = session.send, session.interrupt

    async def slow_send(prompt):
        await gate.wait()
        return await real_send(prompt)

    async def interrupt():
        if unwind:
            await asyncio.sleep(unwind)
        gate.set()
        await real_interrupt()

    session.send = slow_send
    session.interrupt = interrupt
    await harness.bridge.coordinator.on_input(guest, Frame("INPUT", {}, b"hello"))
    for _ in range(300):
        if guest.turn_lock.locked():
            return gate
        await asyncio.sleep(0.01)
    pytest.fail("the turn never took the turn lock")


async def _turn_end(harness, reader, timeout=5.0):
    """The TURN frame that says `state=end`.

    `recv_until({"TURN"})` stops at the START frame, which is not the one any
    of these tests is about.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        for f in await harness.recv_until(reader, {"TURN"}, timeout):
            if f.type == "TURN" and f.headers.get("state") == "end":
                return f
    pytest.fail("the turn never reported an end")


# --- 02.1 -------------------------------------------------------------------


async def test_a_switch_waits_for_the_live_turn(harness, monkeypatch):
    """`_switch` destroys and replaces the object a turn is running ON.

    Without the lock: `reset_transcript()` replaced `view.pages` and
    `view.render` and cleared the coalescers while the suspended turn's SDK
    callbacks kept firing, so the old conversation's remaining prose was
    appended to the conversation the user had just opened -- into its
    paginator, so also into its Export and onto its sealed pages.
    """
    monkeypatch.setattr(srv.sesslist, "history_html",
                        lambda sid_, limit=2000: [])
    r, w = await harness.connect()
    await _recv(harness, r, "OPTIONSEND")
    guest = harness.bridge.connection.guest
    await _hold_turn(harness, guest, unwind=0.3)

    pages_before = guest.view.pages
    task = asyncio.create_task(harness.bridge.coordinator.switch(guest, "stored-1"))
    await asyncio.sleep(0.1)
    # The interrupt has gone out and has not landed yet, so the turn still
    # holds the lock -- and the transcript has NOT been replaced underneath
    # it. Without the lock this assertion fails: `_switch` runs to the end
    # while the turn is still suspended inside `send()`.
    assert guest.turn_lock.locked()
    assert guest.view.pages is pages_before

    await asyncio.wait_for(task, 10)
    assert guest.view.pages is not pages_before
    await harness.close(w)


async def test_a_switch_interrupts_rather_than_waiting_out_the_model(
    harness, monkeypatch,
):
    """Taking the lock without interrupting would park `_pump` -- and every
    inbound frame with it -- for as long as the model felt like streaming."""
    monkeypatch.setattr(srv.sesslist, "history_html",
                        lambda sid_, limit=2000: [])
    r, w = await harness.connect()
    await _recv(harness, r, "OPTIONSEND")
    guest = harness.bridge.connection.guest
    session = guest.claude
    await _hold_turn(harness, guest)

    await asyncio.wait_for(harness.bridge.coordinator.switch(guest, "stored-1"), 10)
    assert session.interrupted, "the live turn was never interrupted"
    await harness.close(w)


async def test_a_turn_that_lost_its_session_still_ends(harness):
    """The line that dies is `registry.add(guest.claude.session_id)`, and
    the tell that it is wrong is eleven lines below it: `context_usage()`
    guards the SAME object. The AttributeError died inside a task nothing
    inspected, so no TURN end frame went out and the guest's Send button
    stayed disabled until the link was re-established."""
    r, w = await harness.connect()
    await _recv(harness, r, "OPTIONSEND")
    guest = harness.bridge.connection.guest
    session = guest.claude

    real_send = session.send

    async def send_then_vanish(prompt):
        turn = await real_send(prompt)
        guest.claude = None          # exactly what close() + `= None` leaves
        return turn

    session.send = send_then_vanish
    await harness.bridge.turn_runner.run_turn(guest, "hello")
    await _turn_end(harness, r)
    await harness.close(w)


# --- 02.2 -------------------------------------------------------------------


async def test_a_refused_model_change_does_not_drop_the_link(harness):
    """`_on_setmodel` called `set_model` bare while both its siblings wrap.

    The CLI refuses control requests against a client that exists but has
    never run a turn, and `_attach` builds exactly such a session eagerly --
    so opening the Model menu before typing anything took the connection
    down, and the reconnect built another never-run session so the next press
    did it again.
    """
    r, w = await harness.connect()
    await _recv(harness, r, "OPTIONSEND")
    guest = harness.bridge.connection.guest

    async def refuse(_m):
        raise RuntimeError("Could not change the model")

    guest.claude.set_model = refuse
    await harness.bridge.options.on_setmodel(guest, Frame("SETMODEL",
                                                   {"model": "opus"}))
    await _recv(harness, r, "OPTIONSEND")
    assert guest.link.connected, "the connection was torn down"
    await harness.close(w)


async def test_a_refused_model_is_not_remembered(harness):
    """`guest.model = model` happened before the call and was persisted, so
    a model the CLI refused was written to settings.json and applied to the
    next session built -- and the menu re-ticked it."""
    r, w = await harness.connect()
    await _recv(harness, r, "OPTIONSEND")
    guest = harness.bridge.connection.guest
    before = guest.model

    async def refuse(_m):
        raise RuntimeError("nope")

    guest.claude.set_model = refuse
    await harness.bridge.options.on_setmodel(guest, Frame("SETMODEL",
                                                   {"model": "opus"}))
    assert guest.model == before
    await harness.close(w)


# --- 02.3 -------------------------------------------------------------------


async def test_a_bulk_transfer_counts_as_the_guest_being_alive(harness):
    """`last_seen` moved in exactly one place -- `_handle` -- and `_pump`
    does not read the socket again until the handler it is awaiting returns.
    At --link-bps 5000 a 450 KB export is 90 seconds of inbound silence, and
    the reaper read that as a dead guest: it denied whatever prompt was on
    screen and closed the writer, and `send_export` then tried to report the
    failure down the socket it had just lost."""
    r, _w = await harness.connect()
    await _recv(harness, r, "OPTIONSEND")
    guest = harness.bridge.connection.guest
    guest.last_seen = 0.0
    await guest.link.wire_yield(guest)
    assert guest.last_seen > 0.0


async def test_a_single_frame_is_not_evidence_of_life(harness):
    """A small `send` lands in the kernel buffer whether or not anything is
    alive at the far end, so it must NOT stamp -- otherwise a guest that
    GPFed could never be reaped while the bridge had anything to say to it.
    Only a bulk push that keeps completing chunks proves the receive window
    is advancing."""
    r, w = await harness.connect()
    await _recv(harness, r, "OPTIONSEND")
    guest = harness.bridge.connection.guest
    guest.last_seen = 0.0
    await harness.bridge.connection.send(guest, Frame("PONG", {}))
    assert guest.last_seen == 0.0
    await harness.close(w)


# --- 02.7 -------------------------------------------------------------------


async def test_a_slash_command_is_not_buried_under_the_sync_preamble(
    harness,
):
    """The preamble is PREPENDED, so `/compact` reached the CLI as twenty
    lines of prose with `/compact` on the end -- which is not a slash command
    at all. Nothing was compacted, nothing said so, and the "finished without
    sending anything back" notice did not fire either, because the model had
    dutifully answered the preamble."""
    r, w = await harness.connect()
    await _recv(harness, r, "OPTIONSEND")
    b = harness.bridge
    b.config.sync_dir = b.config.sync_dir or "/tmp"
    b.connection.synccoord.agent_notices = ["SYNC CONFLICT notes.txt"]
    guest = b.connection.guest
    sent = []
    real_send = guest.claude.send

    async def capture(prompt):
        sent.append(prompt)
        return await real_send(prompt)

    guest.claude.send = capture
    await b.turn_runner.run_turn(guest, "/compact")
    assert sent and sent[0] == "/compact"
    # And the notice is HELD, not dropped -- it rides in on the next
    # ordinary turn.
    assert b.connection.synccoord.agent_notices == ["SYNC CONFLICT notes.txt"]
    await harness.close(w)


async def test_an_ordinary_turn_still_carries_the_preamble(harness):
    """Holding must not become dropping."""
    r, w = await harness.connect()
    await _recv(harness, r, "OPTIONSEND")
    b = harness.bridge
    b.config.sync_dir = b.config.sync_dir or "/tmp"
    b.connection.synccoord.agent_notices = ["SYNC CONFLICT notes.txt"]
    guest = b.connection.guest
    sent = []
    real_send = guest.claude.send

    async def capture(prompt):
        sent.append(prompt)
        return await real_send(prompt)

    guest.claude.send = capture
    await b.turn_runner.run_turn(guest, "please carry on")
    assert sent and "SYNC CONFLICT notes.txt" in sent[0]
    assert sent[0].endswith("please carry on")
    await harness.close(w)


# --- 02.8 -------------------------------------------------------------------


async def test_a_reaped_guest_is_recorded_as_lost(harness):
    """The reaper nulls `guest.link.writer` ITSELF before closing the socket, and
    `_on_client`'s `finally` is keyed on `guest.link.writer is writer` -- so after
    a reap the whole teardown block was skipped. `lost_at` stayed None, and
    `_reap_idle`'s grace expiry is gated on it, so a prompt raised by the
    still-running turn afterwards waited the full 600 second answer deadline
    instead of the 60 second grace."""
    b = harness.bridge
    # BEFORE connecting: `_reap_idle` sleeps at the top of its loop, and the
    # reaper is created inside _on_client, so a value set afterwards would
    # not be read until the first full-length sleep had already elapsed.
    b.config.idle_timeout_s = 0.0
    b.config.idle_check_s = 0.01
    r, _w = await harness.connect()
    await _recv(harness, r, "OPTIONSEND")
    guest = b.connection.guest
    guest.last_seen = 0.0

    for _ in range(300):
        if guest.lost_at is not None:
            break
        await asyncio.sleep(0.01)
    else:
        pytest.fail("the reaper never stamped lost_at")
    assert guest.link.writer is None


async def test_an_exec_registered_after_a_reap_is_still_failed(harness):
    """`_fail_execs` runs from `_mark_lost`, which both teardown paths now
    call. Before that, anything registered after the reap was never failed
    and held the model for the whole MCP budget."""
    r, w = await harness.connect()
    await _recv(harness, r, "OPTIONSEND")
    b = harness.bridge
    guest = b.connection.guest
    loop = asyncio.get_running_loop()
    fut = loop.create_future()
    guest.exec_pending["e1"] = ExecCall(future=fut)

    b.connection.mark_lost(guest)
    assert fut.done()
    assert fut.result()["isError"] is True
    assert not guest.exec_pending
    await harness.close(w)


# --- 02.9 -------------------------------------------------------------------


async def test_abandoning_a_conversation_tells_the_guest_to_stop(harness):
    """`_fail_execs` resolves the futures and sends nothing, so the command
    kept running on the guest's single worker thread and the first `exec` in
    the NEW conversation was answered `busy` -- for a command the model in
    that conversation never issued and cannot see. `_on_interrupt` already
    does this, and says exactly why."""
    r, w = await harness.connect()
    await _recv(harness, r, "OPTIONSEND")
    b = harness.bridge
    guest = b.connection.guest
    loop = asyncio.get_running_loop()
    guest.exec_pending["e1"] = ExecCall(future=loop.create_future())

    b.session.abandon_pending(guest)
    kill = await _recv(harness, r, "EXECKILL")
    assert kill.headers["id"] == "e1"
    await harness.close(w)


# --- 02.13 ------------------------------------------------------------------


async def test_a_spawned_task_that_raises_says_so(harness, caplog):
    """The done callback only discarded the task. A raise produced NO log
    line at the moment it happened; it surfaced later, if at all, as
    asyncio's "Task exception was never retrieved" whenever the task object
    was collected. It is what would have surfaced 02.1."""
    async def boom():
        raise RuntimeError("the thing that used to vanish")

    with caplog.at_level(logging.ERROR):
        harness.bridge._spawn(boom())
        for _ in range(200):
            if "background task failed" in caplog.text:
                break
            await asyncio.sleep(0.01)
    # THE BRIDGE'S OWN LINE, not asyncio's. "Task exception was never
    # retrieved" also carries the traceback, but it is emitted whenever the
    # task object happens to be collected -- which may be much later, is easy
    # to read past, and is exactly the non-report this finding is about. An
    # assertion on the exception text alone passes on the bug.
    assert "background task failed" in caplog.text
    assert "the thing that used to vanish" in caplog.text


async def test_a_cancelled_task_is_not_reported_as_a_failure(harness, caplog):
    """`close()` cancels these deliberately, and so does the SDK on an
    interrupt. A log line per cancellation would train the reader to ignore
    the ones that matter."""
    async def sleeper():
        await asyncio.sleep(30)

    with caplog.at_level(logging.ERROR):
        harness.bridge._spawn(sleeper())
        task = next(iter(harness.bridge._tasks))
        task.cancel()
        await asyncio.sleep(0.05)
    assert "background task failed" not in caplog.text


# --- 02.14 ------------------------------------------------------------------


async def test_close_stops_the_sweeper_and_everything_spawned(harness):
    """`close()` cancelled the reaper and neither the sweeper nor anything
    in `_tasks`, so a bridge shut down cleanly left `_sweep_sync` looping
    every 120 seconds against a guest whose writer was gone."""
    r, _w = await harness.connect()
    await _recv(harness, r, "OPTIONSEND")
    b = harness.bridge
    b.config.sync_dir = b.config.sync_dir or "/tmp"
    if b._sweeper is None:
        b._sweeper = asyncio.create_task(b.sync.sweep_sync())
    sweeper = b._sweeper

    async def sleeper():
        await asyncio.sleep(30)

    b._spawn(sleeper())
    spawned = list(b._tasks)

    await b.close()
    assert b._sweeper is None
    assert sweeper.cancelled() or sweeper.done()
    assert all(t.cancelled() or t.done() for t in spawned)


# --- 02.18 ------------------------------------------------------------------


async def test_the_sync_coordinator_belongs_to_the_guest(harness):
    """One coordinator per BRIDGE meant one halt bar and one notice queue
    shared by every client. `Bridge.synccoord` is a forward now -- the
    single-client shortcut, named and in one place, for R7 to delete."""
    r, w = await harness.connect()
    await _recv(harness, r, "OPTIONSEND")
    b = harness.bridge
    assert b.connection.synccoord is b.connection.guest.synccoord
    await harness.close(w)


async def test_a_coordinator_used_before_any_guest_survives_the_attach(
    harness,
):
    """The handover keeps the object's identity, so a notice recorded before
    the first client connected is still queued after it does."""
    b = harness.bridge
    b.connection.guest = None
    b.connection.synccoord.agent_notices.append("recorded before anyone connected")
    r, w = await harness.connect()
    await _recv(harness, r, "OPTIONSEND")
    assert "recorded before anyone connected" in b.connection.synccoord.agent_notices
    assert b.connection.synccoord is b.connection.guest.synccoord
    await harness.close(w)


async def test_the_baseline_is_addressed_by_the_guest_it_is_asked_about(
    harness, tmp_path,
):
    """`sync_status_text` and `sync_resolve` both took a guest and then
    called down into `_base_path`, which read `self._guest` directly and
    undid it. With two clients, agent A's release would land in whichever
    baseline `self._guest.work_guest` happened to name."""
    b = harness.bridge
    b.config.sync_dir = str(tmp_path)
    b.config.sync_state = str(tmp_path)
    # ONE BASELINE PER ROOT, and the legacy base.json belongs to whichever
    # root already recorded itself in it -- so a second root gets a file of
    # its own. Without that file there is nothing for two roots to disagree
    # about, and the test would pass on the bug.
    (tmp_path / "base.json").write_text(
        json.dumps({"guest_root": "C:\\ATTACHED"}), encoding="utf-8")
    r, w = await harness.connect()
    await _recv(harness, r, "OPTIONSEND")
    attached = b.connection.guest
    attached.work_guest = "C:\\ATTACHED"

    other = srv.GuestSession()
    other.work_guest = "C:\\OTHER"

    assert b.sync.base_path(attached) != b.sync.base_path(other)
    assert b.sync.base_path() == b.sync.base_path(attached)
    await harness.close(w)


# --- 02.5: the model picks the shell, from what the machine reported --------


async def test_an_unknown_shell_is_refused_by_name(harness):
    """The model can write anything into that argument, and the string ends
    up in front of a command line. A name the machine never reported has to
    come back as an error that says what it DOES have -- falling back to
    another shell would run the command under a grammar it was not written
    for, which is not a smaller version of what was asked."""
    r, w = await harness.connect()
    await _recv(harness, r, "OPTIONSEND")
    guest = harness.bridge.connection.guest
    guest.tools = [srv.Found("command.com", "shell",
                             r"C:\WINDOWS\COMMAND.COM", True)]

    out = await harness.bridge.guest_exec.run_on_guest(
        "dir", timeout_ms=5000, shell="bash", cwd="C:\\WORK")
    assert out["isError"] is True
    text = out["content"][0]["text"]
    assert "bash" in text and "command.com" in text
    assert "Nothing was run" in text
    await harness.close(w)


async def test_the_named_shell_becomes_the_invocation_prefix(harness):
    """Path from detection, switch from the profile, spliced by the guest.
    The guest used to choose between command.com and cmd.exe itself from
    GetVersion(), so a machine with a second shell installed could not be
    asked to use it."""
    r, w = await harness.connect()
    await _recv(harness, r, "OPTIONSEND")
    guest = harness.bridge.connection.guest
    guest.tools = [srv.Found("command.com", "shell",
                             r"C:\WINDOWS\COMMAND.COM", True)]

    task = asyncio.create_task(harness.bridge.guest_exec.run_on_guest(
        "dir", timeout_ms=5000, shell="command.com", cwd="C:\\WORK"))
    req = await _recv(harness, r, "EXEC")
    assert req.headers["shell"] == r"C:\WINDOWS\COMMAND.COM /c"
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    await harness.close(w)


async def test_none_means_spawn_the_program_directly(harness):
    r, w = await harness.connect()
    await _recv(harness, r, "OPTIONSEND")
    harness.bridge.connection.guest.tools = [
        srv.Found("command.com", "shell", r"C:\WINDOWS\COMMAND.COM", True)]

    task = asyncio.create_task(harness.bridge.guest_exec.run_on_guest(
        "hello.exe", timeout_ms=5000, shell="none", cwd="C:\\WORK"))
    req = await _recv(harness, r, "EXEC")
    assert req.headers["shell"] == ""
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    await harness.close(w)


async def test_naming_nothing_uses_the_first_shell_the_machine_reported(
    harness,
):
    """Not the first profile this bridge ships -- the first the MACHINE
    said it has. Counting installed profiles is what made adding a second
    shell profile turn the guard off (02.5)."""
    r, w = await harness.connect()
    await _recv(harness, r, "OPTIONSEND")
    harness.bridge.connection.guest.tools = [
        srv.Found("command.com", "shell", r"C:\WINDOWS\COMMAND.COM", True)]

    task = asyncio.create_task(harness.bridge.guest_exec.run_on_guest(
        "dir", timeout_ms=5000, shell="", cwd="C:\\WORK"))
    req = await _recv(harness, r, "EXEC")
    assert req.headers["shell"] == r"C:\WINDOWS\COMMAND.COM /c"
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    await harness.close(w)


async def test_a_machine_that_reported_no_shell_runs_directly(harness):
    """No list to pick from and nothing named. Spawning directly is the only
    thing left that is not a guess."""
    r, w = await harness.connect()
    await _recv(harness, r, "OPTIONSEND")
    assert not harness.bridge.connection.guest.tools

    task = asyncio.create_task(harness.bridge.guest_exec.run_on_guest(
        "dir", timeout_ms=5000, shell="", cwd="C:\\WORK"))
    req = await _recv(harness, r, "EXEC")
    assert req.headers["shell"] == ""
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    await harness.close(w)


async def test_the_guard_judges_by_the_shell_the_call_named(harness):
    """The whole reason the name has to reach the desk. A command approved
    under one grammar and run under another is a guard that is confidently
    wrong, which this module's own docstring says is worse than none."""
    r, w = await harness.connect()
    await _recv(harness, r, "OPTIONSEND")
    desk = harness.bridge.connection.guest.perms
    harness.bridge.connection.guest.tools = [
        srv.Found("command.com", "shell", r"C:\WINDOWS\COMMAND.COM", True)]

    harness.bridge.connection.guest.work_guest = "C:\\WORK"
    # The machine's shell: its grammar places `copy a b` in the work folder.
    v = await desk.classify("mcp__machine__exec", {"command": "copy a b"})
    assert {h.cat for h in v.hits} == {"files_work"}
    # A shell this bridge has no grammar for: nothing can be placed, so it
    # is doubt -- never judged by command.com's grammar it will not run under.
    v = await desk.classify("mcp__machine__exec",
                            {"command": "copy a b", "shell": "zzsh"})
    assert v.doubtful
    await harness.close(w)


async def test_a_restarted_bridge_reopens_the_conversation_the_machine_names(
    harness, monkeypatch, tmp_path,
):
    """Measured on the Athlon, 2026-10-03: the bridge restarted, the client
    reconnected, and the conversation it had open was gone from the pane --
    a new, empty one in its place and nothing said. HELLO's `conv` names it;
    a bridge that lists that id reopens it."""
    monkeypatch.setattr(srv.sesslist, "history_html",
                        lambda sid_, limit=2000: [])
    monkeypatch.setattr(srv.sesslist, "listing",
                        lambda cwd=None, limit=50: [{"id": "stored-7"}])
    harness.bridge.config.sync_dir = str(tmp_path / "tree")
    harness.bridge.conv.add("stored-7")
    harness.bridge.conv.set_folder("stored-7", "WORKHERE", "C:\\WORK\\WORKHERE")
    r, w = await harness.connect(hdrs={"conv": "stored-7"})
    f = await _recv(harness, r, "SWITCHED")
    assert f.headers.get("id") == "stored-7"
    # And the options batch, which carries the folder the guest mirrors.
    # A freshly started bridge has no client and no cached model list, so
    # it used to send none -- sync sat on "no_root" in the reopened chat.
    frames = await harness.recv_until(r, ("OPTIONSEND",))
    work = [x for x in frames
            if x.type == "OPTION" and x.headers.get("kind") == "work"]
    assert work and work[-1].headers.get("id") == "C:\\WORK\\WORKHERE"
    # And no scan went out before that batch named the folder: the guest
    # would have answered no_root and sync waited out a retry.
    early = [x for x in frames if x.type == "SYNCSCAN"]
    assert not early, "a scan reached the guest before its folder did"
    await harness.close(w)


async def test_a_conversation_this_bridge_does_not_list_is_not_reopened(
    harness, monkeypatch,
):
    monkeypatch.setattr(srv.sesslist, "listing",
                        lambda cwd=None, limit=50: [{"id": "other"}])
    r, w = await harness.connect(hdrs={"conv": "stranger"})
    await _recv(harness, r, "OPTIONSEND")
    assert harness.bridge.connection.guest.pending_resume == ""
    await harness.close(w)
