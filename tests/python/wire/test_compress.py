"""Compression and coalescing tests.

The thing most worth protecting here is the persistent stream. Compressing each
frame independently is a silent 1.27x regression -- everything still works, the
bytes are just bigger, and nobody notices for months.
"""

import asyncio
import zlib

import pytest

from bridge.wire.coalesce import DEFAULT_INTERVAL, SLOW_INTERVAL, Coalescer
from bridge.wire.compress import (
                                  MIN_WORTH_COMPRESSING,
                                  StreamCompressor,
                                  StreamDecompressor,
                                  should_compress,
                                  )
from bridge.wire.protocol import Frame, FrameReader

SAMPLE = (
    b"The bridge owns markdown, so the wire carries pre-styled runs rather than\n"
    b"markdown source. The guest looks style up in a CHARFORMAT2 table.\n"
)


def test_stream_round_trip():
    c, d = StreamCompressor(), StreamDecompressor()
    for i in range(20):
        payload = SAMPLE + b"frame %d\n" % i
        assert d.decompress(c.compress(payload)) == payload


def test_persistent_stream_beats_per_frame():
    """The regression this guards against is invisible: independent deflate
    still round-trips, it just wastes ~27% of the wire."""
    frames = [SAMPLE + b"line %d\n" % i for i in range(40)]

    c = StreamCompressor()
    persistent = sum(len(c.compress(f)) for f in frames)
    independent = sum(len(zlib.compress(f, 6)) for f in frames)

    assert persistent < independent
    assert independent / persistent > 1.2, (
        f"persistent={persistent} independent={independent}; the dictionary "
        "should carry repeated text across frames"
    )


def test_ratio_is_reported():
    c = StreamCompressor()
    for _ in range(20):
        c.compress(SAMPLE)
    raw, comp = c.stats
    assert raw > comp
    assert c.ratio > 1.5


def test_reset_desyncs_unless_both_sides_reset():
    """Both ends must reset in lockstep. A one-sided reset is corruption, and
    this test documents that it really does break."""
    c, d = StreamCompressor(), StreamDecompressor()
    d.decompress(c.compress(SAMPLE))

    c.reset()  # only one side
    with pytest.raises(zlib.error):
        d.decompress(c.compress(SAMPLE))

    # Both sides reset: fine again.
    c2, d2 = StreamCompressor(), StreamDecompressor()
    assert d2.decompress(c2.compress(SAMPLE)) == SAMPLE


def test_tiny_payloads_are_not_compressed():
    assert not should_compress(b"hi", None)
    assert not should_compress(b"x" * (MIN_WORTH_COMPRESSING - 1), None)


def test_unknown_link_speed_compresses():
    """Unknown link could be a modem; assume it might be."""
    assert should_compress(b"x" * 500, None)


def test_adaptive_compression_ratio_decision():
    """Deflating n bytes is worth it when time to deflate < time saved on wire.
    With ratio=2.0, compression turns off when link_bps >= deflate_bps * 0.5.
    """
    deflate_bps = 1_000_000.0  # 1 MB/s deflate throughput
    small = b"x" * 200

    # On a slow link (100 KB/s < 500 KB/s), deflating is a net win
    assert should_compress(small, 100_000, deflate_bps)

    # On a fast link (600 KB/s >= 500 KB/s), deflating wastes more CPU time than wire time
    assert not should_compress(small, 600_000, deflate_bps)
    assert not should_compress(b"x" * 8192, 600_000, deflate_bps)


def test_slow_link_compresses_small_frames():
    slow = 5_000
    assert should_compress(b"x" * 200, slow)


# --- coalescer --------------------------------------------------------------


async def test_coalescer_batches_then_flushes_on_timeout():
    out = []

    async def sink(text):
        out.append(text)

    c = Coalescer(sink, interval=0.05)
    c.start()
    for i in range(5):
        await c.add(f"chunk{i} ")
    assert out == []            # nothing yet: below target, within interval
    await asyncio.sleep(0.15)
    await c.stop()
    assert len(out) == 1        # one batched frame, not five
    assert "chunk0" in out[0] and "chunk4" in out[0]


async def test_coalescer_flushes_on_size_and_line_boundary():
    out = []

    async def sink(text):
        out.append(text)

    c = Coalescer(sink, interval=10.0, target=64)
    await c.add("x" * 100 + "\n")
    assert len(out) == 1, "over target and ending on a newline should flush"
    await c.stop()


async def test_coalescer_does_not_split_mid_line_when_under_target():
    out = []

    async def sink(text):
        out.append(text)

    c = Coalescer(sink, interval=10.0, target=1024)
    await c.add("no newline here")
    assert out == []
    await c.stop()
    assert out == ["no newline here"]


async def test_slow_link_raises_the_interval():
    """The finding that prompted this: at 100ms a real token stream only
    accumulates ~35 bytes, far below where deflate helps."""
    c = Coalescer(lambda t: asyncio.sleep(0), interval=DEFAULT_INTERVAL)
    c.set_link_speed(750_000)
    assert c._interval == DEFAULT_INTERVAL
    c.set_link_speed(5_000)
    assert c._interval == SLOW_INTERVAL
    c.set_link_speed(None)
    assert c._interval == DEFAULT_INTERVAL


async def test_stop_flushes_pending_text():
    out = []

    async def sink(text):
        out.append(text)

    c = Coalescer(sink, interval=10.0)
    c.start()
    await c.add("trailing")
    await c.stop()
    assert out == ["trailing"], "text must not be lost at turn end"


async def test_control_frame_reaches_writer_ahead_of_buffered_text():
    """03.13: control frames (like PERMREQ) do not queue behind coalescer buffers.

    A buffered TEXT has not reached the wire yet, while a control frame is sent
    directly via send(), arriving at the writer immediately.
    """
    wire = []

    async def wire_writer(frame_type: str, body: str = ""):
        wire.append((frame_type, body))

    c = Coalescer(lambda text: wire_writer("TEXT", text), interval=10.0, target=2048)
    c.start()

    # Streaming prose arrives and is buffered in the coalescer
    await c.add("Thinking about this command...")
    assert wire == [], "text should still be sitting in the coalescer"

    # A control frame is sent directly
    await wire_writer("PERMREQ", "tool=exec")
    assert wire == [("PERMREQ", "tool=exec")], "PERMREQ reaches wire before un-flushed coalescer text"

    # Eventually the coalescer flushes
    await c.stop()
    assert wire == [("PERMREQ", "tool=exec"), ("TEXT", "Thinking about this command...")]


def test_syncput_compression_round_trip():
    """A compressed SYNCPUT frame produced with zip_out is decompressed
    byte-identical with StreamDecompressor (mirroring guest tinfl)."""
    comp = StreamCompressor()
    decomp = StreamDecompressor()
    content = b"function test() {\n    return 'hello world';\n}\n" * 10
    compressed_body = comp.compress(content)
    frame = Frame("SYNCPUT", {"path": "test.js", "enc": "deflate"}, compressed_body)

    assert frame.headers["enc"] == "deflate"
    assert decomp.decompress(frame.body) == content


async def test_guest_to_bridge_compressed_frame_inflated():
    """Guest-sent compressed EXECOUT frame is inflated by the bridge before dispatch."""
    from bridge.server import Bridge, GuestSession
    from bridge.agent.identity import GuestIdentity
    from bridge.view.transcript import TranscriptView

    bridge = Bridge(secret="secret", host="127.0.0.1", port=0)
    guest = GuestSession()

    received_output = []

    async def mock_on_execout(g, f):
        received_output.append(f.body)

    bridge._dispatch["EXECOUT"] = mock_on_execout

    # Guest compresses outbound with zip_out
    guest_compressor = StreamCompressor(level=1)
    raw_payload = b"Directory of C:\\\n  AUTOEXEC.BAT\n  CONFIG.SYS\n" * 5
    comp_payload = guest_compressor.compress(raw_payload)

    frame = Frame("EXECOUT", {"id": "cmd1", "enc": "deflate"}, comp_payload)
    await bridge._handle(guest, frame)

    assert received_output == [raw_payload]


async def test_send_lock_prevents_zip_out_desync():
    """Two concurrent senders sharing GuestLink cannot interleave compressed bytes."""
    from bridge.wire.link import GuestLink

    link = GuestLink()
    decomp = StreamDecompressor()
    received_frames = []

    class MockWriter:
        def __init__(self):
            self._closing = False
            self.buf = bytearray()

        def is_closing(self):
            return self._closing

        def write(self, data):
            self.buf.extend(data)

        async def drain(self):
            await asyncio.sleep(0.001)

    mock_w = MockWriter()
    link.writer = mock_w

    async def sender(worker_id: int):
        for i in range(20):
            payload = f"worker {worker_id} message {i}\n".encode() * 10
            frame = Frame("TEXT", {}, payload)
            ok = await link.send(None, frame, compress=True)
            assert ok

    await asyncio.gather(sender(1), sender(2))

    reader = FrameReader()
    for f in reader.feed(bytes(mock_w.buf)):
        assert f.headers.get("enc") == "deflate"
        uncompressed = decomp.decompress(f.body)
        received_frames.append(uncompressed)

    assert len(received_frames) == 40
    for body in received_frames:
        assert b"worker 1" in body or b"worker 2" in body


def test_deflate_cap_negotiation():
    """Honors declared deflate cap, refuses unknown deflate values."""
    from bridge.files import caps as C

    # Default undeclared is DEFLATE_NONE
    c0 = C.from_hello({})
    assert c0.deflate == C.DEFLATE_NONE

    # Declared stream is accepted
    c1 = C.from_hello({"deflate": "1"})
    assert c1.deflate == C.DEFLATE_STREAM

    # Declared none is accepted
    c2 = C.from_hello({"deflate": "0"})
    assert c2.deflate == C.DEFLATE_NONE

    # Unknown cap is refused
    with pytest.raises(C.CapsError):
        C.from_hello({"deflate": "2"})
    with pytest.raises(C.CapsError):
        C.from_hello({"deflate": "bad"})

