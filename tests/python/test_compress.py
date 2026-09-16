"""Compression and coalescing tests.

The thing most worth protecting here is the persistent stream. Compressing each
frame independently is a silent 1.27x regression -- everything still works, the
bytes are just bigger, and nobody notices for months.
"""

import asyncio
import zlib

import pytest

from bridge.coalesce import DEFAULT_INTERVAL, SLOW_INTERVAL, Coalescer
from bridge.compress import (
    MIN_WORTH_COMPRESSING,
    StreamCompressor,
    StreamDecompressor,
    should_compress,
)

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


def test_fast_link_skips_small_frames_but_not_big_ones():
    fast = 750_000
    assert not should_compress(b"x" * 200, fast)
    assert should_compress(b"x" * 8192, fast)


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
