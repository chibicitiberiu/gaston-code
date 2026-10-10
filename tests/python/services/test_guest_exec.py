"""Unit tests for GuestExecService."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from bridge.guest import ExecCall
from bridge.services.guest_exec import GuestExecService
from bridge.wire.protocol import Frame


@pytest.fixture
def mock_conn():
    conn = MagicMock()
    conn.send = AsyncMock(return_value=True)
    return conn


@pytest.fixture
def mock_guest():
    guest = MagicMock()
    guest.link.connected = True
    guest.exec_lock = asyncio.Lock()
    guest.exec_seq = 0
    guest.exec_pending = {}
    guest.tools = MagicMock()
    guest.tools.shells = []
    return guest


@pytest.mark.asyncio
async def test_run_on_guest_not_connected(mock_conn, mock_guest):
    mock_guest.link.connected = False
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


@pytest.mark.asyncio
async def test_a_bare_spawn_failure_says_there_was_no_shell(mock_conn, mock_guest):
    """No shell reported and none named: `ver` is spawned as a program and
    fails, and the result has to say why (BACKLOG 17)."""
    svc = GuestExecService(connection=mock_conn)
    mock_guest.tools = []

    async def simulate_guest():
        while not mock_guest.exec_pending:
            await asyncio.sleep(0.005)
        exec_id = next(iter(mock_guest.exec_pending.keys()))
        assert mock_guest.exec_pending[exec_id].no_shell
        await svc.exec_result(
            mock_guest,
            Frame("EXECRESULT", {"id": exec_id, "status": "spawn_failed"}),
        )

    task = asyncio.create_task(
        svc.run_on_guest("ver", timeout_ms=5000, shell="", cwd="",
                         guest=mock_guest))
    sim = asyncio.create_task(simulate_guest())
    res = await task
    await sim
    assert "reported no shell" in res["content"][0]["text"]


# --- when the exit status could not be established -------------------------
#
# The wrapper (bridge/services/execwrap.py) recovers zero-versus-non-zero on a
# shell that loses it. These are the cases where it could not, and where a
# silent `code=0` would otherwise read as success -- BACKLOG 17.


def _seed(guest, **kw):
    fut = asyncio.get_running_loop().create_future()
    guest.exec_pending["x1"] = ExecCall(future=fut, **kw)
    return fut


async def _finish(svc, guest, body=b"out\n", code="0"):
    if body:
        await svc.exec_out(guest, Frame("EXECOUT", {"id": "x1"}, body))
    await svc.exec_result(
        guest, Frame("EXECRESULT", {"id": "x1", "status": "ok", "code": code}))


@pytest.mark.asyncio
async def test_a_skipped_wrapper_says_the_status_is_unknown(mock_conn,
                                                            mock_guest):
    """No fs lane, no temp directory, or a failed write: the command still
    runs and the 0 beside it means nothing. Saying nothing is how the agent
    reads a silent 0 as success."""
    svc = GuestExecService(connection=mock_conn)
    fut = _seed(mock_guest, code_lost=True)
    await _finish(svc, mock_guest)
    assert "Exit status unknown" in fut.result()["content"][0]["text"]


@pytest.mark.asyncio
async def test_a_wrapper_whose_verdict_never_arrived_says_so(mock_conn,
                                                             mock_guest):
    """The wrapper ran, and its line is not in the output -- the command
    killed the shell, or the cap bit before the sentinel."""
    svc = GuestExecService(connection=mock_conn)
    fut = _seed(mock_guest, wrapped=True)
    await _finish(svc, mock_guest, body=b"half an answer\n")
    assert "Exit status unknown" in fut.result()["content"][0]["text"]


@pytest.mark.asyncio
async def test_a_verdict_that_arrived_claims_nothing_extra(mock_conn,
                                                           mock_guest):
    """An `ok` verdict is the one case that says nothing at all: the output
    is the answer, and a sentence claiming success is a line in every reply
    saying nothing."""
    svc = GuestExecService(connection=mock_conn)
    fut = _seed(mock_guest, wrapped=True)
    await _finish(svc, mock_guest, body=b"it worked\n.gaston-rc ok\n")
    text = fut.result()["content"][0]["text"]
    assert "Exit status unknown" not in text
    assert ".gaston-rc" not in text


@pytest.mark.asyncio
async def test_a_shell_that_reports_its_code_claims_nothing(mock_conn,
                                                            mock_guest):
    """`shell: "none"` spawns the program directly and the code is real, so
    there is nothing to disclaim."""
    svc = GuestExecService(connection=mock_conn)
    fut = _seed(mock_guest)
    await _finish(svc, mock_guest)
    assert "Exit status unknown" not in fut.result()["content"][0]["text"]


@pytest.mark.asyncio
async def test_no_fs_lane_marks_the_call_as_unverifiable(mock_conn,
                                                         mock_guest):
    """The flag is set where the knowledge is -- which shell, and whether the
    verb is a built-in -- not where the sentence is written."""
    from types import SimpleNamespace

    # An inventory is a flat list of facts filtered by `kind`, not an object
    # with a `.shells` attribute -- toolrules.shells_of.
    mock_guest.tools = [SimpleNamespace(kind="shell", name="command.com",
                                        path="C:\\WINDOWS\\COMMAND.COM")]
    mock_guest.hello = {"temp": "C:\\WINDOWS\\TEMP"}
    svc = GuestExecService(connection=mock_conn)      # fs is None

    async def simulate():
        for _ in range(400):            # bounded: a hang here is a test bug
            if mock_guest.exec_pending:
                break
            await asyncio.sleep(0.005)
        assert mock_guest.exec_pending, "exec was never submitted"
        assert mock_guest.exec_pending["x1"].code_lost is True
        assert mock_guest.exec_pending["x1"].wrapped is False
        await svc.exec_result(mock_guest, Frame(
            "EXECRESULT", {"id": "x1", "status": "ok", "code": "0"}))

    task = asyncio.create_task(svc.run_on_guest(
        "myprog", timeout_ms=5000, shell="command.com", cwd="",
        guest=mock_guest))
    sim = asyncio.create_task(simulate())
    res = await task
    await sim
    assert "Exit status unknown" in res["content"][0]["text"]
