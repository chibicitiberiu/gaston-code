"""Tests for SessionCoordinator service."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from bridge.services.coordinator import SessionCoordinator
from bridge.status import GuestState
from bridge.wire.protocol import Frame


@pytest.fixture
def coordinator_deps():
    connection = MagicMock()
    connection.send = AsyncMock(return_value=True)
    connection._send = AsyncMock(return_value=True)
    connection.synccoord = MagicMock()
    connection.synccoord.halt_id = ""
    connection.synccoord.halt_text = ""
    connection.synccoord.halt_paths = frozenset()

    turn_runner = MagicMock()
    turn_runner.run_turn = AsyncMock()
    turn_runner.resume_turn = AsyncMock()

    sync = MagicMock()
    permission = MagicMock()

    session = MagicMock()
    session.set_effort_locked = AsyncMock()
    session.new_session_locked = AsyncMock()
    session.switch_locked = AsyncMock()

    workspace = MagicMock()
    workspace.ensure_work_dir = AsyncMock()

    guest_exec = MagicMock()
    guest_exec.kill_exec = MagicMock()

    attachment = MagicMock()
    attachment.validate_attach_target = MagicMock(
        side_effect=lambda g, p: f"/safe/{p}")

    spawn_calls = []

    def mock_spawn(coro):
        spawn_calls.append(coro)

    coordinator = SessionCoordinator(
        connection=connection,
        turn_runner=turn_runner,
        sync=sync,
        permission=permission,
        session=session,
        workspace=workspace,
        guest_exec=guest_exec,
        attachment=attachment,
        spawn=mock_spawn,
    )

    guest = MagicMock()
    guest.turn_lock = asyncio.Lock()
    guest.exec_pending = set()
    guest.claude = MagicMock()
    guest.claude.busy = False
    guest.claude.interrupt = AsyncMock()
    guest.perms = MagicMock()
    guest.perms.reissue = AsyncMock()
    guest.synccoord = connection.synccoord
    guest.work_guest = ""
    guest.first_prompt = ""
    guest.view.view_page = "1"
    guest.turn_produced = True
    guest.turn_degraded = "rate_limit"

    try:
        yield {
            "coordinator": coordinator,
            "connection": connection,
            "attachment": attachment,
            "turn_runner": turn_runner,
            "session": session,
            "workspace": workspace,
            "guest_exec": guest_exec,
            "guest": guest,
            "spawn_calls": spawn_calls,
        }
    finally:
        for c in spawn_calls:
            c.close()


async def test_quiesce_turn_not_locked(coordinator_deps):
    coord = coordinator_deps["coordinator"]
    guest = coordinator_deps["guest"]

    await coord.quiesce_turn(guest)
    coordinator_deps["guest_exec"].kill_exec.assert_not_called()
    guest.claude.interrupt.assert_not_called()


async def test_quiesce_turn_locked(coordinator_deps):
    coord = coordinator_deps["coordinator"]
    guest = coordinator_deps["guest"]
    guest.exec_pending.add("exec_1")

    async with guest.turn_lock:
        await coord.quiesce_turn(guest)

    coordinator_deps["guest_exec"].kill_exec.assert_called_once_with(guest, "exec_1")
    guest.claude.interrupt.assert_called_once()


async def test_quiesce_turn_claude_interrupt_error(coordinator_deps):
    coord = coordinator_deps["coordinator"]
    guest = coordinator_deps["guest"]
    guest.claude.interrupt.side_effect = RuntimeError("SDK client died")

    async with guest.turn_lock:
        # Should not raise
        await coord.quiesce_turn(guest)


async def test_set_effort_quiesces_and_delegates(coordinator_deps):
    coord = coordinator_deps["coordinator"]
    guest = coordinator_deps["guest"]
    session = coordinator_deps["session"]

    await coord.set_effort(guest, "high")
    session.set_effort_locked.assert_called_once_with(guest, "high")


async def test_new_session_quiesces_and_delegates(coordinator_deps):
    coord = coordinator_deps["coordinator"]
    guest = coordinator_deps["guest"]
    session = coordinator_deps["session"]

    await coord.new_session(guest, folder="project_x")
    session.new_session_locked.assert_called_once_with(guest, folder="project_x")


async def test_switch_quiesces_and_delegates(coordinator_deps):
    coord = coordinator_deps["coordinator"]
    guest = coordinator_deps["guest"]
    session = coordinator_deps["session"]

    await coord.switch(guest, "sess_123")
    session.switch_locked.assert_called_once_with(guest, "sess_123")


async def test_reissue_halt(coordinator_deps):
    coord = coordinator_deps["coordinator"]
    guest = coordinator_deps["guest"]
    connection = coordinator_deps["connection"]

    # When no halt_id
    await coord.reissue_halt(guest)
    connection.send.assert_not_called()

    # When halt_id is set
    connection.synccoord.halt_id = "h1"
    connection.synccoord.halt_text = "Conflict notice"
    await coord.reissue_halt(guest)
    assert connection.send.call_count == 1
    frame = connection.send.call_args[0][1]
    assert frame.type == "HALT"
    assert frame.headers["id"] == "h1"


async def test_reissue_turn(coordinator_deps):
    coord = coordinator_deps["coordinator"]
    guest = coordinator_deps["guest"]
    connection = coordinator_deps["connection"]

    # When not busy
    guest.claude.busy = False
    await coord.reissue_turn(guest)
    connection.send.assert_not_called()

    # When busy
    guest.claude.busy = True
    await coord.reissue_turn(guest)
    assert guest.turn_produced is False
    assert guest.turn_degraded == ""
    assert connection.send.call_count == 1
    frame = connection.send.call_args[0][1]
    assert frame.type == "TURN"
    assert frame.headers["state"] == "start"


async def test_sync_state_to_guest(coordinator_deps):
    coord = coordinator_deps["coordinator"]
    guest = coordinator_deps["guest"]
    connection = coordinator_deps["connection"]
    guest.claude.busy = True
    connection.synccoord.halt_id = "h2"
    connection.synccoord.halt_text = "Halt notice"

    await coord.sync_state_to_guest(guest)
    assert guest.view.view_page is None
    guest.perms.reissue.assert_called_once()
    assert connection.send.call_count == 2


async def test_on_input_valid(coordinator_deps):
    coord = coordinator_deps["coordinator"]
    guest = coordinator_deps["guest"]
    spawn_calls = coordinator_deps["spawn_calls"]

    frame = Frame("INPUT", {"attach": "foo.txt", "attach_id": "a1", "attach_name": "foo.txt"}, b"hello")
    await coord.on_input(guest, frame)

    assert guest.first_prompt == "hello"
    coordinator_deps["workspace"].ensure_work_dir.assert_called_once_with(guest)
    coordinator_deps["attachment"].validate_attach_target.assert_called_once_with(guest, "foo.txt")
    assert len(spawn_calls) == 1
    # Run spawned coro
    await spawn_calls[0]
    coordinator_deps["turn_runner"].run_turn.assert_called_once_with(
        guest, "hello", attach="foo.txt", attach_id="a1", attach_name="foo.txt"
    )


async def test_on_input_invalid_attach(coordinator_deps):
    coord = coordinator_deps["coordinator"]
    guest = coordinator_deps["guest"]
    coordinator_deps["attachment"].validate_attach_target.side_effect = None
    coordinator_deps["attachment"].validate_attach_target.return_value = None

    frame = Frame("INPUT", {"attach": "bad/../path"}, b"unsafe")
    await coord.on_input(guest, frame)

    connection = coordinator_deps["connection"]
    assert connection.send.call_count == 1
    err_frame = connection.send.call_args[0][1]
    assert err_frame.type == "ERROR"
    assert err_frame.headers["code"] == "bad_attach"
    assert len(coordinator_deps["spawn_calls"]) == 0


async def test_on_resume(coordinator_deps):
    coord = coordinator_deps["coordinator"]
    guest = coordinator_deps["guest"]
    connection = coordinator_deps["connection"]
    connection.synccoord.halt_id = "halt_99"
    connection.synccoord.halt_paths = frozenset(["file.c"])

    # Mismatched id
    mismatch_frame = Frame("RESUME", {"id": "wrong_id"}, b"")
    await coord.on_resume(guest, mismatch_frame)
    connection.send.assert_not_called()

    # Matching id
    match_frame = Frame("RESUME", {"id": "halt_99"}, b"")
    await coord.on_resume(guest, match_frame)
    assert connection.synccoord.halt_id == ""
    assert connection.synccoord.halt_paths == frozenset()
    assert connection.send.call_count == 1
    haltend_frame = connection.send.call_args[0][1]
    assert haltend_frame.type == "HALTEND"
    assert haltend_frame.headers["id"] == "halt_99"
    assert len(coordinator_deps["spawn_calls"]) == 1
    await coordinator_deps["spawn_calls"][0]
    coordinator_deps["turn_runner"].resume_turn.assert_called_once_with(guest, frozenset(["file.c"]))


async def test_on_interrupt(coordinator_deps):
    coord = coordinator_deps["coordinator"]
    guest = coordinator_deps["guest"]
    guest.exec_pending.add("exec_2")

    await coord.on_interrupt(guest)
    coordinator_deps["guest_exec"].kill_exec.assert_called_once_with(guest, "exec_2")
    guest.claude.interrupt.assert_called_once()


async def test_on_input_rejected_when_running_turn(coordinator_deps):
    coord = coordinator_deps["coordinator"]
    guest = coordinator_deps["guest"]
    connection = coordinator_deps["connection"]
    guest.state = GuestState.RUNNING_TURN

    frame = Frame("INPUT", {}, b"concurrent prompt")
    await coord.on_input(guest, frame)

    assert connection.send.call_count == 1
    err = connection.send.call_args[0][1]
    assert err.type == "ERROR"
    assert err.headers["code"] == "turn_busy"
    assert len(coordinator_deps["spawn_calls"]) == 0


async def test_on_input_rejected_when_halted(coordinator_deps):
    coord = coordinator_deps["coordinator"]
    guest = coordinator_deps["guest"]
    connection = coordinator_deps["connection"]
    guest.state = GuestState.HALTED

    frame = Frame("INPUT", {}, b"prompt during halt")
    await coord.on_input(guest, frame)

    assert connection.send.call_count == 1
    err = connection.send.call_args[0][1]
    assert err.type == "ERROR"
    assert err.headers["code"] == "halted"
    assert len(coordinator_deps["spawn_calls"]) == 0


async def test_on_input_ignored_when_disconnected(coordinator_deps):
    coord = coordinator_deps["coordinator"]
    guest = coordinator_deps["guest"]
    connection = coordinator_deps["connection"]
    guest.state = GuestState.DISCONNECTED

    frame = Frame("INPUT", {}, b"prompt when disconnected")
    await coord.on_input(guest, frame)

    assert connection.send.call_count == 0
    assert len(coordinator_deps["spawn_calls"]) == 0

