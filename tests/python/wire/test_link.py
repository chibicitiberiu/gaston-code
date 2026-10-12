"""Unit tests for bridge/wire/link.py (GuestLink).

Covers failure paths and invariants called out in Audit 04.13:
- Oversized frame raises ProtocolError (logged and re-raised).
- Disconnected or closing writer returns False without attempting to write.
- ConnectionResetError / BrokenPipeError resets writer to None and returns False.
- Successful send writes payload and awaits drain.
- send_export_records chunking and failure handling.
- send_raw ignores writer exceptions.
"""

from __future__ import annotations

import pytest

from bridge.view import blocks as B
from bridge.view.html import APPEND, LIVE
from bridge.wire.link import GuestLink
from bridge.wire.protocol import MAX_FRAME, Frame, ProtocolError


class FakeWriter:
    def __init__(self, closing: bool = False):
        self.closing = closing
        self.written = bytearray()
        self.drain_count = 0
        self.raise_on_write: Exception | None = None
        self.raise_on_drain: Exception | None = None

    def is_closing(self) -> bool:
        return self.closing

    def write(self, data: bytes) -> None:
        if self.raise_on_write:
            raise self.raise_on_write
        self.written.extend(data)

    async def drain(self) -> None:
        if self.raise_on_drain:
            raise self.raise_on_drain
        self.drain_count += 1


@pytest.fixture
def link():
    return GuestLink()


def test_connected_property(link):
    assert not link.connected

    link.writer = FakeWriter(closing=True)
    assert not link.connected

    link.writer = FakeWriter(closing=False)
    assert link.connected


@pytest.mark.asyncio
async def test_send_oversized_frame_raises_protocol_error(link):
    link.writer = FakeWriter()
    oversized = Frame("TEST", {}, b"x" * (MAX_FRAME + 10))
    with pytest.raises(ProtocolError):
        await link.send(None, oversized)


@pytest.mark.asyncio
async def test_send_with_none_writer_returns_false(link):
    link.writer = None
    frame = Frame("PING", {})
    assert await link.send(None, frame) is False


@pytest.mark.asyncio
async def test_send_with_closing_writer_returns_false(link):
    writer = FakeWriter(closing=True)
    link.writer = writer
    frame = Frame("PING", {})
    assert await link.send(None, frame) is False
    assert len(writer.written) == 0


@pytest.mark.asyncio
async def test_send_connection_reset_clears_writer_and_returns_false(link):
    writer = FakeWriter()
    writer.raise_on_drain = ConnectionResetError("Connection reset by peer")
    link.writer = writer

    frame = Frame("PING", {})
    assert await link.send(None, frame) is False
    assert link.writer is None


@pytest.mark.asyncio
async def test_send_broken_pipe_clears_writer_and_returns_false(link):
    writer = FakeWriter()
    writer.raise_on_write = BrokenPipeError("Broken pipe")
    link.writer = writer

    frame = Frame("PING", {})
    assert await link.send(None, frame) is False
    assert link.writer is None


@pytest.mark.asyncio
async def test_send_happy_path(link):
    writer = FakeWriter()
    link.writer = writer
    frame = Frame("PONG", {"key": "val"})

    assert await link.send(None, frame) is True
    assert writer.drain_count == 1
    assert b"PONG" in bytes(writer.written)


@pytest.mark.asyncio
async def test_send_compression(link):
    writer = FakeWriter()
    link.writer = writer
    link.link_bps = 100
    link.deflate_bps = 1000000

    compressible = b"A" * 5000
    frame = Frame("TEXT", {"page": "1"}, compressible)

    assert await link.send(None, frame, compress=True) is True
    payload = bytes(writer.written)
    assert b"enc=deflate" in payload
    # Payload on wire should be much smaller than 5000 bytes
    assert len(payload) < 200


@pytest.mark.asyncio
async def test_send_raw_ignores_writer_exceptions():
    writer = FakeWriter()
    writer.raise_on_drain = ConnectionResetError("Lost")
    # Must not raise
    await GuestLink.send_raw(writer, Frame("PING", {}))


@pytest.mark.asyncio
async def test_send_records_sends_one_frame_of_whole_records(link):
    writer = FakeWriter()
    link.writer = writer
    body = B.encode(B.Block("para", {}, [("t", "a")])) \
        + B.encode(B.Block("rule"))

    await link.send_records(None, body, APPEND)

    assert _body_of(writer) == body
    assert B.decode(_body_of(writer))          # and it parses


@pytest.mark.asyncio
async def test_an_empty_body_is_still_sent(link):
    """ZERO IS REAL and is not an early return: an empty LIVE body is what
    clears `#l` at the end of a turn. send_html had a special arm for this
    and it was easy to lose."""
    writer = FakeWriter()
    link.writer = writer
    await link.send_records(None, b"", LIVE)
    assert len(writer.written) > 0
    assert _body_of(writer) == b""


@pytest.mark.asyncio
async def test_an_oversized_live_body_is_dropped(link):
    """A LIVE update replaces an element wholesale, so half of one is
    meaningless; the APPEND that follows carries the whole thing."""
    writer = FakeWriter()
    link.writer = writer
    await link.send_records(None, b"x" * (MAX_FRAME - 1024 + 1), LIVE)
    assert len(writer.written) == 0


@pytest.mark.asyncio
async def test_an_oversized_append_body_is_refused_and_logged(link, caplog):
    """UNREACHABLE BY CONSTRUCTION, and kept anyway.

    Two things upstream prevent it: write_block refuses a record larger
    than the client declared it can hold, and the paginator gives an
    oversize record a window of its own rather than cutting it. An
    unreachable branch that fires is worth more than one that was deleted
    -- it names both of the things that should have stopped it, which is
    the whole of the debugging.

    This is also where send_html's truncation went. It truncated because
    it could not know where the element boundaries were; a body of whole
    records has nothing to guess at, so there is nothing to truncate TO.
    """
    import logging

    writer = FakeWriter()
    link.writer = writer
    with caplog.at_level(logging.ERROR, logger="bridge.link"):
        await link.send_records(None, b"x" * (MAX_FRAME - 1024 + 1), APPEND)
    assert len(writer.written) == 0
    assert any("maxblock" in r.message or "paginator" in r.message
               for r in caplog.records), caplog.records


@pytest.mark.asyncio
async def test_send_export_lifecycle_success(link):
    writer = FakeWriter()
    link.writer = writer
    data = [b"para\nt export file content\n.\n"]

    await link.send_export_records(None, data)
    written = bytes(writer.written)
    assert b"EXPORTSTART" in written
    assert b"EXPORTDAT" in written
    assert b"EXPORTEND" in written
    assert b"status=ok" in written


@pytest.mark.asyncio
async def test_send_export_failure_aborts_with_error(link):
    writer = FakeWriter()
    link.writer = writer

    # Allow EXPORTSTART to succeed, then drop link on EXPORTDAT
    orig_write = writer.write
    def write_hook(data):
        if b"EXPORTDAT" in data:
            raise BrokenPipeError("Drop mid export")
        orig_write(data)
    writer.write = write_hook

    await link.send_export_records(None, [b"para\nt some content\n.\n"])
    # Writer will be set to None when EXPORTDAT fails, and export_end(False) will return False
    assert link.writer is None


def _body_of(writer) -> bytes:
    """The one TEXT frame a send wrote, decompressed."""
    from bridge.wire.compress import StreamDecompressor
    from bridge.wire.protocol import FrameReader
    frames = list(FrameReader().feed(bytes(writer.written)))
    assert len(frames) == 1, f"expected one frame, got {len(frames)}"
    if frames[0].headers.get("enc") == "deflate":
        return StreamDecompressor().decompress(frames[0].body)
    return frames[0].body
