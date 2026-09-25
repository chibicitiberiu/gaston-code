"""Unit tests for TurnRunner (Layer 7)."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from bridge.services.turn_runner import TurnRunner, turn_verdict


class DummyAnchor:
    page = 1
    offset = 42
    preview = "Hello world"


class DummyZip:
    stats = (100, 50)
    ratio = 2.0


class DummyPages:
    def __init__(self):
        self.count = 2

    def mark_user_turn(self, text):
        return DummyAnchor()

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


def test_turn_verdict():
    # Success, no degradation
    assert turn_verdict(True, "", False, "success") == (True, "success")
    assert turn_verdict(True, "", True, "success") == (True, "success")

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


def test_turn_runner_init(turn_deps):
    cfg, conn, pres, att, sync, ge, perm, opt, ws, sess, conv = turn_deps
    runner = TurnRunner(
        config=cfg,
        connection=conn,
        presentation=pres,
        attachment=att,
        sync=sync,
        guest_exec=ge,
        permission=perm,
        options=opt,
        workspace=ws,
        session=sess,
        conv=conv,
    )
    assert runner.config is cfg
    assert runner.connection is conn
    assert runner.presentation is pres
    assert runner.attachment is att
    assert runner.sync is sync
    assert runner.guest_exec is ge
    assert runner.permission is perm
    assert runner.options is opt
    assert runner.workspace is ws
    assert runner.session is sess
    assert runner.conv is conv


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

    # Anchor frame, turn_start, turn_end sent
    assert conn.send.call_count >= 2
    pres.write_block.assert_called_once()
    pres.flush_text.assert_called_once()
    conv.add.assert_called_once_with("sess-1")


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
