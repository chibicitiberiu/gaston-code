"""GuestFsService: the fs lane's half of the conversation.

The guest is not here, so what these pin is the bridge's side of the
contract -- the ids it allocates, the frames it builds, what it does with a
refusal, and the two places where being wrong would hand the agent a
confident wrong answer.
"""
from __future__ import annotations

import asyncio
import zlib

import pytest

from bridge.guest import FsCall
from bridge.services.guest_fs import CHUNK, FsError, GuestFsService
from bridge.wire.frame import Frame


class FakeConnection:
    def __init__(self) -> None:
        self.sent: list[Frame] = []

    async def send(self, guest, frame) -> None:
        self.sent.append(frame)


class FakeGuest:
    def __init__(self) -> None:
        self.fs_pending: dict[str, FsCall] = {}
        self.fs_seq = 0


def _reply(svc, guest, frame):
    """Deliver a frame from the guest, as server.py's dispatch would."""
    return svc.fs_frame(guest, frame)


async def _answer_after(svc, guest, frames):
    await asyncio.sleep(0)
    for f in frames:
        await _reply(svc, guest, f)


@pytest.mark.asyncio
async def test_get_reassembles_chunks_and_checks_the_checksum():
    conn, guest = FakeConnection(), FakeGuest()
    svc = GuestFsService(connection=conn)
    body = b"hello there, this is a file"
    crc = zlib.crc32(body) & 0xFFFFFFFF

    task = asyncio.create_task(svc.get(guest, r"C:\WINDOWS\SYSTEM.INI"))
    await asyncio.sleep(0)
    await _answer_after(svc, guest, [
        Frame("FSDAT", {"id": "f1", "off": "0"}, body[:10]),
        Frame("FSDAT", {"id": "f1", "off": "10"}, body[10:]),
        Frame("FSDATEND", {"id": "f1", "status": "ok", "crc": str(crc),
                           "truncated": "0"}),
    ])
    got = await task

    assert got.data == body
    assert not got.truncated
    assert conn.sent[0].type == "FSGET"
    assert conn.sent[0].headers["path"] == r"C:\WINDOWS\SYSTEM.INI"
    # `f`-prefixed, because the guest's id_matches_family drops anything else
    # on this lane -- silently, and the caller then waits out its whole budget.
    assert conn.sent[0].headers["id"] == "f1"


@pytest.mark.asyncio
async def test_a_checksum_disagreement_is_refused_and_not_returned():
    """The guest checksums as it reads, so a mismatch means the WIRE changed
    the bytes. Returning them anyway is how a corrupted read becomes a
    confident wrong answer in the transcript."""
    conn, guest = FakeConnection(), FakeGuest()
    svc = GuestFsService(connection=conn)

    task = asyncio.create_task(svc.get(guest, r"C:\A.TXT"))
    await asyncio.sleep(0)
    await _answer_after(svc, guest, [
        Frame("FSDAT", {"id": "f1", "off": "0"}, b"corrupted"),
        Frame("FSDATEND", {"id": "f1", "status": "ok", "crc": "1"}),
    ])
    with pytest.raises(FsError) as exc:
        await task
    assert exc.value.status == "crc_mismatch"


@pytest.mark.asyncio
async def test_a_refusal_carries_the_guests_own_word():
    conn, guest = FakeConnection(), FakeGuest()
    svc = GuestFsService(connection=conn)

    task = asyncio.create_task(svc.get(guest, r"C:\NOPE.TXT"))
    await asyncio.sleep(0)
    await _answer_after(svc, guest,
                        [Frame("FSDATEND", {"id": "f1", "status": "missing"})])
    with pytest.raises(FsError) as exc:
        await task
    assert exc.value.status == "missing"


@pytest.mark.asyncio
async def test_truncation_is_reported_rather_than_hidden():
    conn, guest = FakeConnection(), FakeGuest()
    svc = GuestFsService(connection=conn)
    body = b"first part only"

    task = asyncio.create_task(svc.get(guest, r"C:\BIG.DAT", max_bytes=15))
    await asyncio.sleep(0)
    await _answer_after(svc, guest, [
        Frame("FSDAT", {"id": "f1", "off": "0"}, body),
        Frame("FSDATEND", {"id": "f1", "status": "ok",
                           "crc": str(zlib.crc32(body) & 0xFFFFFFFF),
                           "truncated": "1"}),
    ])
    got = await task
    assert got.truncated
    assert conn.sent[0].headers["max_bytes"] == "15"


@pytest.mark.asyncio
async def test_put_chunks_the_body_and_closes_with_a_checksum():
    conn, guest = FakeConnection(), FakeGuest()
    svc = GuestFsService(connection=conn)
    body = b"x" * (CHUNK + 100)

    task = asyncio.create_task(svc.put(guest, r"C:\OUT.BIN", body))
    await asyncio.sleep(0)
    await _answer_after(svc, guest, [
        Frame("FSPUTACK", {"id": "f1", "status": "ok",
                           "bytes": str(len(body))}),
    ])
    assert await task == len(body)

    puts = [f for f in conn.sent if f.type == "FSPUT"]
    assert len(puts) == 2
    assert puts[0].headers["off"] == "0"
    assert puts[1].headers["off"] == str(CHUNK)
    end = next(f for f in conn.sent if f.type == "FSPUTEND")
    assert end.headers["crc"] == str(zlib.crc32(body) & 0xFFFFFFFF)


@pytest.mark.asyncio
async def test_an_empty_file_still_opens_the_transfer():
    """Otherwise the guest never learns the path, and FSPUTEND arrives for a
    transfer that was never begun."""
    conn, guest = FakeConnection(), FakeGuest()
    svc = GuestFsService(connection=conn)

    task = asyncio.create_task(svc.put(guest, r"C:\EMPTY.TXT", b""))
    await asyncio.sleep(0)
    await _answer_after(svc, guest,
                        [Frame("FSPUTACK", {"id": "f1", "status": "ok",
                                            "bytes": "0"})])
    assert await task == 0
    puts = [f for f in conn.sent if f.type == "FSPUT"]
    assert len(puts) == 1
    assert puts[0].headers["path"] == r"C:\EMPTY.TXT"


@pytest.mark.asyncio
async def test_drives_collects_rows_until_the_end_frame():
    conn, guest = FakeConnection(), FakeGuest()
    svc = GuestFsService(connection=conn)

    task = asyncio.create_task(svc.drives(guest))
    await asyncio.sleep(0)
    await _answer_after(svc, guest, [
        Frame("FSDRIVE", {"id": "f1", "letter": "A", "type": "2",
                          "label": "", "serial": "0", "free_kb": "0"}),
        Frame("FSDRIVE", {"id": "f1", "letter": "C", "type": "3",
                          "label": "WIN98", "serial": "123",
                          "free_kb": "1048576"}),
        Frame("FSDRIVEEND", {"id": "f1", "count": "2"}),
    ])
    rows = await task
    assert [d.letter for d in rows] == ["A", "C"]
    # An empty removable still reports its letter and type. "There is a floppy
    # drive and it is empty" is an answer.
    assert rows[0].type == 2 and rows[0].label == ""
    assert rows[1].free_kb == 1048576


@pytest.mark.asyncio
async def test_a_dropped_connection_ends_every_waiter():
    conn, guest = FakeConnection(), FakeGuest()
    svc = GuestFsService(connection=conn)

    task = asyncio.create_task(svc.get(guest, r"C:\A.TXT"))
    await asyncio.sleep(0)
    svc.fail_calls(guest)
    with pytest.raises(FsError) as exc:
        await task
    assert exc.value.status == "disconnected"
    assert guest.fs_pending == {}


@pytest.mark.asyncio
async def test_a_reply_nobody_awaits_is_dropped_without_raising():
    conn, guest = FakeConnection(), FakeGuest()
    svc = GuestFsService(connection=conn)
    await svc.fs_frame(guest, Frame("FSDATEND", {"id": "f99", "status": "ok"}))
