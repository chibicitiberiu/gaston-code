"""Batches streaming text deltas into frames the guest can keep up with.

This module does two jobs that turn out to be the same job.

1. **Repaint budget.** A Pentium MMX 200 cannot repaint a RichEdit control per
   token. Flushing at ~10Hz is what makes streaming readable rather than a
   slideshow of half-drawn frames.

2. **Compression ratio.** Measured on this project's own prose-plus-code:
   256-byte frames deflate to 1.68:1, 2KB frames to 2.04:1, flattening after.
   Frame size is the single biggest lever on bandwidth, which matters a lot at
   dialup speeds.

So the coalescer was designed for latency and pays for itself twice. The flush
policy below targets >=2KB where it can without ever letting text sit longer
than the interval.

Markdown needs lookahead -- `**` is ambiguous until you see what follows -- so
flushing on a line boundary is also what the Phase 2b parser wants. One policy,
three reasons.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable

# ~10Hz on a fast link: faster than the guest can usefully repaint.
DEFAULT_INTERVAL = 0.10

# On a slow link the interval is raised deliberately. Measured 2026-09-16:
# at realistic token rates a 100ms window accumulates only ~35 bytes, which is
# far below the 2KB where deflate gets good -- so the first implementation
# never compressed anything.
#
# Buffering longer is free when the wire is the bottleneck: at 5 KB/s a frame
# already takes seconds to send, so 600ms of extra buffering is invisible and
# buys a much better ratio. At LAN speed the opposite is true and latency wins.
SLOW_INTERVAL = 0.60
SLOW_LINK_BPS = 250_000          # ~2 Mbit/s

# Below this, prefer to wait for more text rather than emit a small frame that
# compresses badly. Above it, flush regardless.
TARGET_BYTES = 2048

# Never let a single frame get near the 64KB wire cap.
MAX_BYTES = 32 * 1024


class Coalescer:
    """Accumulates text, flushes on size, line boundary or timeout.

    One instance per style stream. Call `add()` freely; `flush()` is driven by
    a background task and by explicit boundaries (turn end, style change).
    """

    def __init__(
        self,
        sink: Callable[[str], Awaitable[None]],
        *,
        interval: float = DEFAULT_INTERVAL,
        target: int = TARGET_BYTES,
    ) -> None:
        self._sink = sink
        self._interval = interval
        self._target = target
        self._base_interval = interval
        self._buf: list[str] = []
        self._size = 0
        self._last_flush = time.monotonic()
        self._task: asyncio.Task | None = None
        self._lock = asyncio.Lock()

    def set_link_speed(self, bps: float | None) -> None:
        """Trade latency for compression ratio as the link gets slower."""
        if bps is not None and bps < SLOW_LINK_BPS:
            self._interval = SLOW_INTERVAL
        else:
            self._interval = self._base_interval

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._ticker())

    async def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None
        await self.flush()

    async def add(self, text: str) -> None:
        if not text:
            return
        async with self._lock:
            self._buf.append(text)
            self._size += len(text)
            ready = self._size >= self._target and text.endswith("\n")
            forced = self._size >= MAX_BYTES
        if ready or forced:
            await self.flush()

    async def flush(self) -> None:
        async with self._lock:
            if not self._buf:
                return
            payload = "".join(self._buf)
            self._buf.clear()
            self._size = 0
            self._last_flush = time.monotonic()
        await self._sink(payload)

    async def _ticker(self) -> None:
        """Timeout flush, so text never sits waiting for a line that a model
        stopped mid-sentence will never send."""
        try:
            while True:
                await asyncio.sleep(self._interval)
                async with self._lock:
                    due = (
                        bool(self._buf)
                        and (time.monotonic() - self._last_flush) >= self._interval
                    )
                if due:
                    await self.flush()
        except asyncio.CancelledError:
            raise
