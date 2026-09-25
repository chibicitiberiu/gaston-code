"""Unit tests for PermissionService."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from bridge.services.permission import PermissionService


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


def test_deny_all_passes_both_the_reason_and_the_message(mock_conn, mock_guest):
    """`PermissionDesk.deny_all` takes a reason AND a message the user reads.

    This asserted `assert_called_once_with("idle")` against a MagicMock, which
    accepts any arity -- so it passed while the service dropped the message
    and would have raised TypeError against the real desk. mypy found it.
    """
    svc = PermissionService(connection=mock_conn)
    mock_guest.perms = MagicMock()

    svc.deny_all(mock_guest, "idle", "The Windows machine stopped responding.")
    mock_guest.perms.deny_all.assert_called_once_with(
        "idle", "The Windows machine stopped responding.")


def test_deny_all_is_a_no_op_without_a_desk(mock_conn, mock_guest):
    """`perms` is `PermissionDesk | None`; the guard is why this relay exists."""
    svc = PermissionService(connection=mock_conn)
    mock_guest.perms = None
    svc.deny_all(mock_guest, "idle", "gone")   # must not raise
    svc.deny_all(None, "idle", "gone")


def test_retarget(mock_conn, mock_guest):
    svc = PermissionService(connection=mock_conn)
    mock_guest.perms = MagicMock()
    mock_guest.perms._cwd = "/old/dir"

    svc.retarget(mock_guest, "/new/dir")
    assert mock_guest.perms._cwd == "/new/dir"
