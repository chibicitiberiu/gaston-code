"""Unit tests for bridge/wire/link.py (GuestLink).

Covers failure paths and invariants called out in Audit 04.13:
- Oversized frame raises ProtocolError (logged and re-raised).
- Disconnected or closing writer returns False without attempting to write.
- ConnectionResetError / BrokenPipeError resets writer to None and returns False.
- Successful send writes payload and awaits drain.
- send_html / send_export chunking and failure handling.
- send_raw ignores writer exceptions.
"""

from __future__ import annotations

import pytest

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
async def test_send_html_live_mode_oversized_is_dropped(link):
    writer = FakeWriter()
    link.writer = writer
    budget = MAX_FRAME - 1024
    huge_html = "<div>" + "x" * (budget + 100) + "</div>"

    await link.send_html(None, huge_html, LIVE, page_no=1)
    assert len(writer.written) == 0


@pytest.mark.asyncio
async def test_send_html_append_mode_oversized_is_truncated(link):
    writer = FakeWriter()
    link.writer = writer
    budget = MAX_FRAME - 1024
    huge_html = "<p>" + "a" * (budget + 500) + "</p>"

    await link.send_html(None, huge_html, APPEND, page_no=1)
    assert len(writer.written) > 0
    # Payload was compressed with deflate; decompress to check truncated marker
    from bridge.wire.compress import StreamDecompressor
    from bridge.wire.protocol import FrameReader
    reader = FrameReader()
    frames = list(reader.feed(bytes(writer.written)))
    assert len(frames) == 1
    decomp = StreamDecompressor()
    body = decomp.decompress(frames[0].body)
    assert b"too large to send" in body


@pytest.mark.asyncio
async def test_send_export_lifecycle_success(link):
    writer = FakeWriter()
    link.writer = writer
    data = b"export file content"

    await link.send_export(None, data)
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

    await link.send_export(None, b"some content")
    # Writer will be set to None when EXPORTDAT fails, and export_end(False) will return False
    assert link.writer is None


def _body_of(writer) -> bytes:
    """The one TEXT frame a send_html wrote, decompressed."""
    from bridge.wire.compress import StreamDecompressor
    from bridge.wire.protocol import FrameReader
    frames = list(FrameReader().feed(bytes(writer.written)))
    assert len(frames) == 1, f"expected one frame, got {len(frames)}"
    if frames[0].headers.get("enc") == "deflate":
        return StreamDecompressor().decompress(frames[0].body)
    return frames[0].body


@pytest.mark.asyncio
async def test_send_html_strips_nul_which_would_truncate_the_dom_copy(link):
    """A NUL is a partial loss the user cannot see and the cache does not share.

    `win/app/turn.cpp:190` gives the cache `(f->body, f->body_len)` and
    `rc_html::block` a `const char *`, so the DOM stops at the first NUL while
    the cache keeps the whole block. The same transcript then renders two
    different ways depending on whether it arrived live or was redrawn from
    the cache on reconnect.
    """
    writer = FakeWriter()
    link.writer = writer

    await link.send_html(None, "<p>before\x00after</p>", APPEND, page_no=1)

    body = _body_of(writer)
    assert b"\x00" not in body
    assert b"<p>beforeafter</p>" == body


@pytest.mark.asyncio
async def test_send_html_keeps_cr_and_tab(link):
    """Only the NUL goes. The other control bytes are display content.

    `\\r` reaches the renderer today and is a line break there, because
    `HtmlStream.feed` splits with `str.splitlines()`, which splits on it; a
    lone `\\r` in an unterminated tail travels verbatim. `\\t` is whitespace
    inside a `<pre>` and removing it would re-indent code. Stage 3's renderer
    is specified to carry both, and B2 compares it byte-for-byte against this
    path, so a strip added here would quietly break that proof.
    """
    writer = FakeWriter()
    link.writer = writer

    await link.send_html(None, "<pre>a\tb\rc</pre>", APPEND, page_no=1)

    assert _body_of(writer) == b"<pre>a\tb\rc</pre>"


@pytest.mark.asyncio
async def test_send_html_live_nul_is_stripped_silently(link):
    """Same strip on the live path, without the log line.

    The open block is re-sent whole on every flush, so one NUL in a long
    answer would be ten log lines a second for the rest of the turn. The
    APPEND that seals the block reports it once.
    """
    writer = FakeWriter()
    link.writer = writer

    await link.send_html(None, "<p>partial\x00</p>", LIVE, page_no=1)

    assert _body_of(writer) == b"<p>partial</p>"


@pytest.mark.asyncio
async def test_send_html_all_nul_body_sends_nothing_on_append(link):
    """Stripping can empty a body, and an empty APPEND has nothing to append."""
    writer = FakeWriter()
    link.writer = writer

    await link.send_html(None, "\x00\x00", APPEND, page_no=1)

    assert len(writer.written) == 0


@pytest.mark.asyncio
async def test_send_html_all_nul_body_still_clears_the_live_slot(link):
    """An emptied LIVE body must still be sent: it is what clears `#l`.

    `send_html`'s existing empty-body arm sends a zero-length LIVE frame for
    exactly this reason, and the strip must land on the near side of it.
    """
    writer = FakeWriter()
    link.writer = writer

    await link.send_html(None, "\x00", LIVE, page_no=1)

    assert len(writer.written) > 0
    assert _body_of(writer) == b""
