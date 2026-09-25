"""Unit tests for PermissionService."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from bridge.services.permission import PermissionService
from bridge.wire.protocol import Frame


@pytest.fixture
def mock_conn():
    conn = MagicMock()
    conn.send = AsyncMock(return_value=True)
    return conn


@pytest.fixture
def mock_guest():
    guest = MagicMock()
    guest.perms = None
    guest.work_dir = "/local/work"
    return guest


def test_attach_creates_desk(mock_conn, mock_guest):
    ws = MagicMock()
    ws.work_dir.return_value = "/local/work"
    pres = MagicMock()

    svc = PermissionService(
        connection=mock_conn, presentation=pres, workspace=ws
    )

    desk = svc.attach(mock_guest)
    assert desk is not None
    assert mock_guest.perms is desk
    assert desk._cwd == "/local/work"


@pytest.mark.asyncio
async def test_reply_delegates(mock_conn, mock_guest):
    svc = PermissionService(connection=mock_conn)
    mock_guest.perms = MagicMock()
    mock_guest.perms.reply = AsyncMock()

    f = Frame("PERMREPLY", {"id": "p1", "action": "allow"})
    await svc.reply(mock_guest, f)
    mock_guest.perms.reply.assert_awaited_once_with("p1", "allow")


@pytest.mark.asyncio
async def test_reissue_delegates(mock_conn, mock_guest):
    svc = PermissionService(connection=mock_conn)
    mock_guest.perms = MagicMock()
    mock_guest.perms.reissue = AsyncMock()

    await svc.reissue(mock_guest)
    mock_guest.perms.reissue.assert_awaited_once()


def test_deny_all_delegates(mock_conn, mock_guest):
    svc = PermissionService(connection=mock_conn)
    mock_guest.perms = MagicMock()

    svc.deny_all(mock_guest, "idle")
    mock_guest.perms.deny_all.assert_called_once_with("idle")


def test_callback_delegates(mock_conn, mock_guest):
    svc = PermissionService(connection=mock_conn)
    mock_guest.perms = MagicMock()
    mock_guest.perms.callback.return_value = "dummy_cb"

    assert svc.callback(mock_guest) == "dummy_cb"


def test_retarget(mock_conn, mock_guest):
    svc = PermissionService(connection=mock_conn)
    mock_guest.perms = MagicMock()
    mock_guest.perms._cwd = "/old/dir"

    svc.retarget(mock_guest, "/new/dir")
    assert mock_guest.perms._cwd == "/new/dir"
