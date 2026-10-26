"""Unit tests for TurnRunner (Layer 7)."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from bridge.services.turn_runner import TurnRunner, turn_verdict


class DummyZip:
    stats = (100, 50)
    ratio = 2.0


class DummyPages:
    def __init__(self):
        self.count = 2

    def message_boundary(self):
        return None


class DummyGuest:
    def __init__(self):
        self.turn_lock = asyncio.Lock()
        self.claude = MagicMock()
        # Composed like the real GuestSession: the view owns the
        # paginator, the link owns the compressor.
        self.view = SimpleNamespace(pages=DummyPages())
        self.link = SimpleNamespace(zip_out=DummyZip())
        self.turn_produced = False
        self.turn_degraded = ""
        self.synccoord = MagicMock()
        self.synccoord.agent_notices = []
        # Which machine this is, as GuestSession derives it from HELLO.
        self.machine = "guest-a"



def test_turn_verdict():
    # Success, no degradation -- but only if it actually said something.
    # An ok turn that produced nothing is `no_output`, because that is the
    # shape a Claude Code which is installed but not logged in takes, and
    # it used to reach the guest as `done 0s $?` (BACKLOG 46).
    assert turn_verdict(True, "", False, "success") == (False, "no_output")
    assert turn_verdict(True, "", True, "success") == (True, "success")
    # A slash command is output-less by design and stays a success.
    assert turn_verdict(True, "", False, "success", was_command=True) == (
        True, "success")

    # Degraded and produced nothing -> reported as failure
    assert turn_verdict(True, "rate_limited", False, "success") == (False, "rate_limited")
    assert turn_verdict(True, "unknown_event", False, "success") == (False, "unknown_event")

    # Degraded but produced output -> reported as success
    assert turn_verdict(True, "rate_limited", True, "success") == (True, "success")


@pytest.fixture
def turn_deps():
    config = MagicMock()
    config.sync_dir = "/tmp/sync"
    connection = MagicMock()
    connection.send = AsyncMock(return_value=True)
    presentation = MagicMock()
    presentation.write_block = AsyncMock()
    presentation.flush_text = AsyncMock()
    attachment = MagicMock()
    attachment.parse_display_names = MagicMock(return_value=[])
    attachment.format_display_text = MagicMock(side_effect=lambda t, n: t)
    attachment.assemble_prompt = AsyncMock(side_effect=lambda g, t, **kw: t)
    sync = MagicMock()
    sync.load_baseline = MagicMock()
    sync.baseline_lock = MagicMock()
    sync.base_path = MagicMock(return_value="/tmp/base")
    sync.release_all_latches = AsyncMock(return_value=1)
    guest_exec = MagicMock()
    permission = MagicMock()
    options = MagicMock()
    workspace = MagicMock()
    session = MagicMock()
    conv = MagicMock()
    return (
        config, connection, presentation, attachment, sync,
        guest_exec, permission, options, workspace, session, conv
    )


@pytest.mark.asyncio
async def test_run_turn_locked_success(turn_deps):
    cfg, conn, pres, att, sync, ge, perm, opt, ws, sess, conv = turn_deps
    runner = TurnRunner(
        cfg, conn, pres, att, sync, ge, perm, opt, ws, sess, conv
    )
    guest = DummyGuest()
    fake_turn = MagicMock()
    fake_turn.ok = True
    fake_turn.subtype = "success"
    fake_turn.cost_usd = 0.01
    fake_turn.duration_ms = 100
    guest.claude.send = AsyncMock(return_value=fake_turn)
    guest.claude.context_usage = AsyncMock(return_value=15)
    guest.claude.session_id = "sess-1"

    await runner.run_turn_locked(guest, "Hello agent")

    # turn_start and turn_end sent
    assert conn.send.call_count >= 2
    pres.write_block.assert_called_once()
    pres.flush_text.assert_called_once()
    # Filed under the machine it was started from, not just under this
    # bridge: `mine` alone cannot tell two guests apart (BACKLOG §21).
    conv.add.assert_called_once_with("sess-1", "guest-a")


@pytest.mark.asyncio
async def test_run_turn_locked_error(turn_deps):
    cfg, conn, pres, att, sync, ge, perm, opt, ws, sess, conv = turn_deps
    runner = TurnRunner(
        cfg, conn, pres, att, sync, ge, perm, opt, ws, sess, conv
    )
    guest = DummyGuest()
    guest.claude.send = AsyncMock(side_effect=RuntimeError("SDK disconnected"))

    await runner.run_turn_locked(guest, "Hello failure")

    # Flush text and turn_error sent
    pres.flush_text.assert_called_once()
    # Check that error frame was sent
    sent_frames = [call[0][1] for call in conn.send.call_args_list]
    assert any(f.type == "TURN" and f.headers.get("state") == "error" for f in sent_frames)


def test_a_stopped_turn_says_stopped_whatever_the_sdk_called_it():
    """Stop during a tool call ends the SDK turn as error_during_execution;
    the guest used to render that as a failure that may not have run."""
    assert turn_verdict(False, "", True, "error_during_execution", True) == (
        False, "stopped")
    assert turn_verdict(True, "", True, "success", True) == (False, "stopped")
    assert turn_verdict(False, "", True, "error_during_execution") == (
        False, "error_during_execution")


def test_the_sdks_stop_message_to_the_model_is_not_shown_to_the_user():
    from bridge.services.session import shows_tool_result
    assert shows_tool_result(False, False)
    assert shows_tool_result(True, False)      # a real tool error: shown
    assert shows_tool_result(False, True)      # output before the Stop: shown
    assert not shows_tool_result(True, True)   # the SDK's STOP note: hidden
