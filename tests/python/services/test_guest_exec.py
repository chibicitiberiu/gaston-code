"""Unit tests for GuestExecService."""

import asyncio
from unittest.mock import AsyncMock, MagicMock
import pytest

from bridge.services.guest_exec import ExecCall, GuestExecService
from bridge.wire.protocol import Frame


@pytest.fixture
def mock_conn():
    conn = MagicMock()
    conn.send = AsyncMock(return_value=True)
    return conn


@pytest.fixture
def mock_guest():
    guest = MagicMock()
    guest.connected = True
    guest.exec_lock = asyncio.Lock()
    guest.exec_seq = 0
    guest.exec_pending = {}
    guest.tools = MagicMock()
    guest.tools.shells = []
    return guest


@pytest.mark.asyncio
async def test_run_on_guest_not_connected(mock_conn, mock_guest):
    mock_guest.connected = False
    svc = GuestExecService(connection=mock_conn)

    res = await svc.run_on_guest(
        "dir", timeout_ms=1000, shell="none", cwd="", guest=mock_guest
    )
    assert res.get("isError") is True
    assert "not reachable" in res["content"][0]["text"]
    assert not mock_conn.send.called


@pytest.mark.asyncio
async def test_run_on_guest_unrepresentable_char(mock_conn, mock_guest):
    svc = GuestExecService(connection=mock_conn)

    # \u2603 is a snowman, not in CP1252/ASCII
    res = await svc.run_on_guest(
        "echo \u2603", timeout_ms=1000, shell="none", cwd="", guest=mock_guest
    )
    assert res.get("isError") is True
    assert "cannot represent" in res["content"][0]["text"]
    assert not mock_conn.send.called


@pytest.mark.asyncio
async def test_run_on_guest_cwd_too_long(mock_conn, mock_guest):
    svc = GuestExecService(connection=mock_conn)

    long_cwd = "C:\\" + "a" * 300
    res = await svc.run_on_guest(
        "dir", timeout_ms=1000, shell="none", cwd=long_cwd, guest=mock_guest
    )
    assert res.get("isError") is True
    assert "working directory is too long" in res["content"][0]["text"]
    assert not mock_conn.send.called


@pytest.mark.asyncio
async def test_run_on_guest_success(mock_conn, mock_guest):
    svc = GuestExecService(connection=mock_conn)

    async def simulate_guest():
        # Wait until exec_pending has the entry
        while not mock_guest.exec_pending:
            await asyncio.sleep(0.005)
        exec_id = next(iter(mock_guest.exec_pending.keys()))
        await svc.exec_out(
            mock_guest, Frame("EXECOUT", {"id": exec_id}, b"Volume in drive C\n")
        )
        await svc.exec_result(
            mock_guest,
            Frame("EXECRESULT", {"id": exec_id, "status": "ok", "code": "0"}),
        )

    task = asyncio.create_task(
        svc.run_on_guest(
            "dir", timeout_ms=5000, shell="none", cwd="", guest=mock_guest
        )
    )
    sim_task = asyncio.create_task(simulate_guest())

    res = await task
    await sim_task
    assert res.get("isError") is False
    assert "Volume in drive C" in res["content"][0]["text"]
    assert mock_conn.send.called


@pytest.mark.asyncio
async def test_kill_exec(mock_conn, mock_guest):
    spawned = []
    def spawn_mock(coro):
        spawned.append(coro)
        coro.close()

    svc = GuestExecService(connection=mock_conn, spawn=spawn_mock)

    fut = asyncio.get_running_loop().create_future()
    mock_guest.exec_pending["x1"] = ExecCall(future=fut)

    svc.kill_exec(mock_guest, "x1")
    assert mock_guest.exec_pending["x1"].killed is True
    assert len(spawned) == 1


@pytest.mark.asyncio
async def test_fail_execs(mock_conn, mock_guest):
    svc = GuestExecService(connection=mock_conn)

    fut = asyncio.get_running_loop().create_future()
    mock_guest.exec_pending["x1"] = ExecCall(future=fut)

    svc.fail_execs(mock_guest, "Connection lost")
    assert fut.done()
    res = fut.result()
    assert res.get("isError") is True
    assert "Connection lost" in res["content"][0]["text"]
    assert len(mock_guest.exec_pending) == 0
