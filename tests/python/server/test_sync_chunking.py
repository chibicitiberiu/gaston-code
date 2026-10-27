"""A pushed file bigger than one chunk has to arrive intact.

THE PREFIX GOES ON THE FIRST CHUNK ONLY. `win/sync/syncrpc.cpp` says so --
"Only the FIRST chunk carries the prefix; later chunks are all data" -- and
skips `g_put.data_off` bytes when `off == 0` and nothing otherwise.

The bridge sent it on every chunk. The guest therefore wrote the path bytes
INTO the file at each boundary, and its ChunkSink (`win/fs/xfer.cpp`) counts
what it wrote, so `m_received` ran ahead of the offset being sent by
len(prefix) per chunk and `off != m_received` refused the third chunk as
`out_of_order`.

Which meant the sync layer could not push a file bigger than CHUNK:

    <= 8192 B    one chunk, no boundary, fine -- which is why every file
                 pushed during development worked
    8193-16384   two chunks: the second carries a prefix that is written as
                 data, so the file is the wrong length and fails its CRC at
                 SYNCPUTEND
    > 16384      three or more: refused out_of_order

Found on the Athlon on 2026-10-27, where a 22,437-byte screcord.cpp was
refused twice. The agent renamed the file between attempts and concluded the
name was at fault, which is what a size bug looks like when you only see it
through two samples.
"""

from __future__ import annotations

import asyncio

import pytest

from bridge.files.synctransport import CHUNK, SyncTransport


class _Link:
    def __init__(self):
        self.frames = []

    async def send(self, guest, frame, compress=False):
        self.frames.append(frame)
        return True

    async def wire_yield(self, guest):
        # The real one paces the link between chunks; here it only has to
        # not be missing, since what is under test is what goes on the wire.
        await asyncio.sleep(0)


class _Sync:
    def __init__(self):
        self.pending = {}
        self._n = 0

    def next_id(self):
        self._n += 1
        return str(self._n)


class _Guest:
    def __init__(self):
        self.link = _Link()
        self.sync = _Sync()
        self.perm_pending = {}


def _put(data: bytes, rel: bytes = b"DIR/screcord.cpp"):
    """Run one put() and return its SYNCPUT frames. The ack never comes, so
    the wait is cancelled once the chunks are out -- the chunks are the
    thing under test."""
    guest = _Guest()
    t = SyncTransport(guest)

    async def drive():
        task = asyncio.ensure_future(
            t.put(rel, data, crc=0, cls="text", if_crc=""))
        for _ in range(200):
            await asyncio.sleep(0)
            if any(f.type == "SYNCPUTEND" for f in guest.link.frames):
                break
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(drive())
    return [f for f in guest.link.frames if f.type == "SYNCPUT"], rel


def _prefix(rel: bytes) -> bytes:
    return str(len(rel)).encode() + b" " + rel


@pytest.mark.parametrize("size", [1, CHUNK - 1, CHUNK])
def test_a_file_that_fits_one_chunk_carries_the_prefix(size):
    frames, rel = _put(b"x" * size)
    assert len(frames) == 1
    assert frames[0].body.startswith(_prefix(rel))


@pytest.mark.parametrize("size", [CHUNK + 1, CHUNK * 2, CHUNK * 3 + 7])
def test_only_the_first_chunk_carries_the_prefix(size):
    frames, rel = _put(b"x" * size)
    assert len(frames) > 1, "this size should not fit one chunk"
    assert frames[0].body.startswith(_prefix(rel))
    for i, f in enumerate(frames[1:], 1):
        assert not f.body.startswith(_prefix(rel)), (
            "chunk %d repeats the path prefix; the guest writes it as file "
            "data and then refuses the next chunk as out_of_order" % i)


def test_the_offsets_are_what_the_guest_will_have_received():
    """`ChunkSink::write` refuses unless `off == m_received`, and
    m_received counts DATA bytes. So each chunk's offset must be the total
    data sent before it -- the prefix must not be in that sum."""
    size = CHUNK * 3 + 7
    frames, rel = _put(b"x" * size)
    received = 0
    for f in frames:
        assert int(f.headers["off"]) == received, (
            "offset %s but the guest has %d bytes"
            % (f.headers["off"], received))
        body = f.body
        if received == 0:
            body = body[len(_prefix(rel)):]
        received += len(body)
    assert received == size, "the guest ends up with the wrong length"
