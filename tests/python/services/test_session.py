"""Unit tests for SessionService (Layer 6)."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bridge.agent.conversations import Conversations
from bridge.services.session import SessionService
from bridge.wire.protocol import Frame


class DummyGuest:
    def __init__(self, sess_id="guest-1"):
        self.sess_id = sess_id
        self.conv_id = "conv-1"
        self.model = "claude-sonnet-4-6"
        self.effort = "high"
        self.perm_mode = "manual"
        self.work_dir = "/tmp/work"
        self.work_guest = "C:\\WORK"
        self.first_prompt = "hello"
        self.pending_resume = ""
        self.claude = None
        self.turn_lock = asyncio.Lock()
        self.perms = MagicMock()
        self.exec_pending = {"exec-1": MagicMock()}
        # Composed like the real GuestSession: the view owns the paginator.
        self.view = MagicMock()
        self.view.pages.count = 2
        self.view.pages.all_text.return_value = "Turn 1\nHello World\n"
        self.link = MagicMock()
        self.link.send_export = AsyncMock()


@pytest.fixture
def mock_deps(tmp_path):
    connection = MagicMock()
    connection.send = AsyncMock(return_value=True)
    presentation = MagicMock()
    presentation.emit_text = AsyncMock()
    presentation.flush_text = AsyncMock()
    presentation.write_block = AsyncMock()
    presentation.send_page = AsyncMock()
    workspace = MagicMock()
    workspace.work_dir = MagicMock(return_value=str(tmp_path / "work"))
    workspace.stored_work_dir = MagicMock(return_value=str(tmp_path / "work"))
    workspace.guest_path = MagicMock(return_value="C:\\WORK\\SUB")
    workspace.folder_name = MagicMock(return_value="SUB")
    workspace.retarget_perms = MagicMock()
    workspace.ensure_work_dir = AsyncMock()
    workspace.config = MagicMock()
    workspace.config.sync_dir = str(tmp_path / "sync")
    options = MagicMock()
    options.clear_cache = MagicMock()
    options.effective_model = MagicMock(return_value="claude-sonnet-4-6")
    options.effective_effort = MagicMock(return_value="high")
    options.remember_settings = MagicMock()
    options.send_options = AsyncMock()
    guest_exec = MagicMock()
    guest_exec.kill_exec = MagicMock()
    guest_exec.fail_execs = MagicMock()
    conv = Conversations(tmp_path / "conv.json")
    return connection, presentation, workspace, options, guest_exec, conv


def test_session_service_init(mock_deps):
    conn, pres, ws, opt, ge, conv = mock_deps
    svc = SessionService(
        connection=conn,
        presentation=pres,
        workspace=ws,
        options=opt,
        guest_exec=ge,
        conv=conv,
    )
    assert svc.connection is conn
    assert svc.presentation is pres
    assert svc.workspace is ws
    assert svc.options is opt
    assert svc.guest_exec is ge
    assert svc.conv is conv


def test_conv_title(mock_deps):
    conn, pres, ws, opt, ge, conv = mock_deps
    svc = SessionService(conn, pres, ws, opt, ge, conv)
    guest = DummyGuest()

    with patch("bridge.agent.sessions.listing", return_value=[
        {"id": "conv-1", "title": "My Test Chat"},
        {"id": "conv-2", "title": "Other Chat"},
    ]):
        title = svc.conv_title(guest)
        assert title == "My Test Chat"

    with patch("bridge.agent.sessions.listing", return_value=[]):
        title = svc.conv_title(guest)
        assert title == "conv-1"

    guest.conv_id = ""
    title = svc.conv_title(guest)
    assert title == "Conversation"


def test_restore_settings(mock_deps):
    conn, pres, ws, opt, ge, conv = mock_deps
    svc = SessionService(conn, pres, ws, opt, ge, conv)
    guest = DummyGuest()

    conv.remember("conv-1", model="opus", effort="low", mode="plan")
    svc.restore_settings(guest, "conv-1")
    assert guest.model == "opus"
    assert guest.effort == "low"
    assert guest.perm_mode == "plan"


def test_abandon_pending(mock_deps):
    conn, pres, ws, opt, ge, conv = mock_deps
    svc = SessionService(conn, pres, ws, opt, ge, conv)
    guest = DummyGuest()

    svc.abandon_pending(guest)
    guest.perms.deny_all.assert_called_once_with(
        "rebuild", "The conversation changed before this was answered.")
    ge.kill_exec.assert_called_once_with(guest, "exec-1")
    ge.fail_execs.assert_called_once()


@pytest.mark.asyncio
async def test_send_sessions(mock_deps):
    conn, pres, ws, opt, ge, conv = mock_deps
    svc = SessionService(conn, pres, ws, opt, ge, conv)
    guest = DummyGuest()

    with patch("bridge.agent.sessions.listing", return_value=[
        {"id": "conv-1", "title": "Chat 1", "cwd": "/tmp", "group": ""},
        {"id": "conv-2", "title": "Chat 2", "cwd": "/tmp", "group": ""},
    ]):
        conv.add("conv-1")
        conv.add("conv-2")
        await svc.send_sessions(guest)
        # Check sessions_start, 2 rows, sessions_end were sent
        assert conn.send.call_count == 4


@pytest.mark.asyncio
async def test_on_export(mock_deps):
    conn, pres, ws, opt, ge, conv = mock_deps
    svc = SessionService(conn, pres, ws, opt, ge, conv)
    guest = DummyGuest()

    frame = Frame("EXPORT", {})
    await svc.on_export(guest, frame)
    guest.link.send_export.assert_called_once()
    args = guest.link.send_export.call_args[0]
    assert args[0] is guest
    assert isinstance(args[1], bytes)
    assert b"Gaston Code" in args[1]
