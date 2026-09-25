"""Unit tests for SyncService."""

import asyncio
from unittest.mock import AsyncMock, MagicMock
import pytest

from bridge.services.config import BridgeConfig
from bridge.services.sync import SyncService
from bridge.synccoord import SyncCoordinator
from bridge.wire.protocol import Frame


@pytest.fixture
def mock_conn():
    conn = MagicMock()
    conn.send = AsyncMock(return_value=True)
    return conn


@pytest.fixture
def mock_ws():
    ws = MagicMock()
    ws.mirror_dir = MagicMock(return_value="/mirror/dir")
    return ws


@pytest.fixture
def mock_pres():
    pres = MagicMock()
    pres.write_block = AsyncMock()
    return pres


@pytest.fixture
def mock_guest():
    guest = MagicMock()
    guest.link.connected = True
    guest.synccoord = SyncCoordinator()
    guest.sync.pending = {}
    guest.sync.active = False
    guest.work_guest = "C:\\WORK"
    return guest


def test_current_guest_root(mock_conn, mock_ws, mock_pres, mock_guest):
    cfg = BridgeConfig(secret="", sync_dir="/sync", sync_state="/state")
    svc = SyncService(cfg, mock_conn, mock_ws, mock_pres)

    assert svc.current_guest_root(mock_guest) == "C:\\WORK"
    mock_guest.work_guest = ""
    assert svc.current_guest_root(mock_guest) == ""


def test_base_path(mock_conn, mock_ws, mock_pres, mock_guest, tmp_path):
    state_dir = str(tmp_path / "state")
    cfg = BridgeConfig(secret="", sync_dir="/sync", sync_state=state_dir)
    svc = SyncService(cfg, mock_conn, mock_ws, mock_pres)

    # With no root, returns legacy base.json
    mock_guest.work_guest = ""
    assert svc.base_path(mock_guest) == f"{state_dir}/base.json"

    # With root, returns legacy or hashed path
    mock_guest.work_guest = "C:\\WORK"
    bp = svc.base_path(mock_guest)
    assert bp.startswith(state_dir)


@pytest.mark.asyncio
async def test_raise_and_clear_halt(mock_conn, mock_ws, mock_pres, mock_guest):
    cfg = BridgeConfig(secret="", sync_dir="/sync", sync_state="/state")
    svc = SyncService(cfg, mock_conn, mock_ws, mock_pres)

    await svc.raise_halt(mock_guest, "Conflict on file.txt", paths=["file.txt"])
    assert mock_guest.synccoord.halt_id != ""
    assert mock_conn.send.called
    frame = mock_conn.send.call_args[0][1]
    assert frame.type == "HALT"

    mock_conn.send.reset_mock()
    await svc.clear_halt(mock_guest)
    assert mock_guest.synccoord.halt_id == ""
    assert mock_conn.send.called
    frame = mock_conn.send.call_args[0][1]
    assert frame.type == "HALTEND"


@pytest.mark.asyncio
async def test_fail_syncs(mock_conn, mock_ws, mock_pres, mock_guest):
    cfg = BridgeConfig(secret="", sync_dir="/sync", sync_state="/state")
    svc = SyncService(cfg, mock_conn, mock_ws, mock_pres)

    fut = asyncio.get_running_loop().create_future()
    entry = MagicMock()
    entry.future = fut
    mock_guest.sync.pending["s1"] = entry

    svc.fail_syncs(mock_guest, "Link lost")
    assert fut.done()
    assert str(fut.exception()) == "Link lost"
    assert len(mock_guest.sync.pending) == 0


def test_sync_status_text_disabled(mock_conn, mock_ws, mock_pres):
    cfg = BridgeConfig(secret="", sync_dir="", sync_state="/state")
    svc = SyncService(cfg, mock_conn, mock_ws, mock_pres)

    txt = svc.sync_status_text()
    assert "not switched on" in txt
