"""ProcService and the ps/kill vocabulary."""

import asyncio
import pathlib
import re
from unittest.mock import AsyncMock, MagicMock

import conftest
import pytest

from bridge.agent import prompts
from bridge.services.proc import ProcError, ProcService
from bridge.wire.protocol import Frame


@pytest.fixture
def guest():
    g = MagicMock()
    g.proc_pending = {}
    g.proc_seq = 0
    g.caps.acp = "cp1252"
    return g


def _answer(svc, guest, frame_type, headers, body=b""):
    async def run():
        while not guest.proc_pending:
            await asyncio.sleep(0.005)
        ps_id = next(iter(guest.proc_pending))
        await svc.proc_frame(guest, Frame(frame_type, {"id": ps_id, **headers},
                                          body))
    return asyncio.create_task(run())


@pytest.mark.asyncio
async def test_list_parses_the_guests_lines(guest):
    conn = MagicMock()
    conn.send = AsyncMock(return_value=True)
    svc = ProcService(conn)
    body = (b"ffcf5477 7ea7264b 4 C:\\WINDOWS\\SYSTEM\\KERNEL32.DLL\n"
            b"fffd3ff3 fffef28b 1 C:\\EP\\WITH SPACE.EXE\n")
    t = _answer(svc, guest, "PSRESULT", {"status": "ok", "count": "2",
                                         "truncated": "0"}, body)
    rows, truncated = await svc.list(guest)
    await t
    assert [r.pid for r in rows] == ["ffcf5477", "fffd3ff3"]
    assert rows[1].exe == "C:\\EP\\WITH SPACE.EXE"   # a space in the path
    assert rows[0].threads == 4 and not truncated
    assert conn.send.call_args[0][1].type == "PSLIST"


@pytest.mark.asyncio
async def test_list_refusal_raises_with_the_status(guest):
    conn = MagicMock()
    conn.send = AsyncMock(return_value=True)
    svc = ProcService(conn)
    t = _answer(svc, guest, "PSRESULT", {"status": "no_toolhelp"})
    with pytest.raises(ProcError) as exc:
        await svc.list(guest)
    await t
    assert exc.value.status == "no_toolhelp"


@pytest.mark.asyncio
async def test_kill_returns_the_status_and_sends_force_only_when_asked(guest):
    conn = MagicMock()
    conn.send = AsyncMock(return_value=True)
    svc = ProcService(conn)
    t = _answer(svc, guest, "PSKILLRESULT", {"status": "pending"})
    assert await svc.kill(guest, "fffd3ff3") == "pending"
    await t
    sent = conn.send.call_args[0][1]
    assert sent.type == "PSKILL" and sent.headers["pid"] == "fffd3ff3"
    assert "force" not in sent.headers


def test_every_status_the_guest_sends_has_a_sentence():
    """A status procrpc.cpp can send and tools.toml cannot explain reaches
    the agent as 'unrecognised' -- the gap test_exec_status closes for
    exec, closed here for processes."""
    src = pathlib.Path(conftest.HERE, "win", "rpc",
                       "procrpc.cpp").read_text()
    sent = set(re.findall(r'status\s*=\s*"([a-z_]+)"', src))
    sent |= set(re.findall(r'vals\[1\]\s*=\s*"([a-z_]+)"', src))
    sent.discard("ok")
    assert sent, "found no statuses in procrpc.cpp"
    have = set(prompts.all()["tools"]["ps"]["refused"])
    have |= set(prompts.all()["tools"]["kill"]["view"])
    assert not (sent - have), sorted(sent - have)
