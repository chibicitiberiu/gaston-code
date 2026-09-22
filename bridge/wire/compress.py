"""Deflate for the wire, with the settings measured on 2026-09-16.

The direction asymmetry is what makes this cheap. Deflate decompresses roughly
4-10x faster than it compresses, and:

  bridge -> guest  is all text, and the guest only INFLATES. The modern box
                   compresses at whatever level it likes.
  guest -> bridge  is where a Pentium MMX 200 would pay, so uploads use level 1
                   and only when the link is slow enough to justify it.

Measured on this project's prose-plus-code, 2KB frames, persistent stream:

    independent deflate per frame   1.47:1     <- what NOT to do
    persistent stream, level 1      2.04:1
    persistent stream, level 6      2.16:1

A persistent stream keeps the dictionary warm across frames, which is worth
1.27x over compressing each frame alone. Preset dictionaries measured as no
help at all (2.16 either way), so there are none.
"""

from __future__ import annotations

import zlib

# 32KB window. Dropping to 8KB costs ~6% ratio to save 24KB of guest RAM, which
# is a bad trade against 8-12MB free.
WBITS = 15

LEVEL_DOWN = 6   # bridge -> guest: free on this end
LEVEL_UP = 1     # guest -> bridge: the P200 pays here

ENC_RAW = "raw"
ENC_DEFLATE = "deflate"

# Below this, a frame's deflate header costs more than the saving.
#
# Low deliberately: because the stream is persistent, even a 256-byte frame
# still measured 1.68:1, so the dictionary carries small frames that would be
# hopeless compressed alone.
MIN_WORTH_COMPRESSING = 64


class StreamCompressor:
    """One persistent deflate stream per direction.

    Z_SYNC_FLUSH after every frame keeps the dictionary while still producing a
    self-delimiting chunk the peer can inflate immediately. NEVER create a new
    compressobj per frame -- that is the 1.47:1 case.
    """

    def __init__(self, level: int = LEVEL_DOWN) -> None:
        self._level = level
        self._co = zlib.compressobj(level, zlib.DEFLATED, WBITS)
        self._in = 0
        self._out = 0

    def compress(self, data: bytes) -> bytes:
        self._in += len(data)
        out = self._co.compress(data) + self._co.flush(zlib.Z_SYNC_FLUSH)
        self._out += len(out)
        return out

    def reset(self) -> None:
        """Start a fresh stream. The peer must reset in lockstep, so this only
        happens on reconnect."""
        self._co = zlib.compressobj(self._level, zlib.DEFLATED, WBITS)

    @property
    def ratio(self) -> float:
        return (self._in / self._out) if self._out else 1.0

    @property
    def stats(self) -> tuple[int, int]:
        return self._in, self._out


class StreamDecompressor:
    """Mirror of StreamCompressor. This is what the guest runs, via miniz."""

    def __init__(self) -> None:
        self._do = zlib.decompressobj(WBITS)

    def decompress(self, data: bytes) -> bytes:
        return self._do.decompress(data)

    def reset(self) -> None:
        self._do = zlib.decompressobj(WBITS)


ASSUMED_RATIO = 2.0


DEFAULT_DEFLATE_BPS = 800_000.0  #: P200 baseline before guest reports STAT deflate_bps


def should_compress(data: bytes, link_bps: float | None,
                    deflate_bps: float | None = None) -> bool:
    """Decide per payload from the measured link speed and deflate throughput.

    Deflating n bytes is worth it when the time to deflate them is less than
    the time saved sending n - n/ratio bytes. With an assumed 2:1 ratio:
        n / deflate_bps < (n - n/2) / link_bps  <=>  link_bps < 0.5 * deflate_bps

    When the link is faster than half the CPU's compression throughput (e.g. a
    fast LAN on a slow CPU), compressing wastes more wall clock time in CPU than
    it saves on the wire, so the compression turns itself off.
    """
    if len(data) < MIN_WORTH_COMPRESSING:
        return False
    if link_bps is not None:
        effective_def = deflate_bps if (deflate_bps is not None and deflate_bps > 0) else DEFAULT_DEFLATE_BPS
        if link_bps >= effective_def * (1.0 - 1.0 / ASSUMED_RATIO):
            return False
    return True
