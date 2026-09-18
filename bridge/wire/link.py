"""GuestLink: the one place a byte leaves the bridge for a guest.

Before this, the outbound half of the protocol was 44 `self._send(...)`
calls and 38 hand-built `Frame(...)` literals spread across `Bridge` -- so
`stamp=`, header stringification and `fold.encode` were decided
independently at every call site (docs/audit/02-bridge-python.md #1). This
class owns the writer, the send lock, the persistent compressor and the
line that chunks outbound HTML under the frame cap. `Bridge` still decides
WHAT to send and WHEN; this decides how it reaches the wire.

Deliberately NOT the frame vocabulary itself -- see
`bridge/wire/protocol.py`'s typed constructors for that. This module never
builds a `Frame` whose headers depend on server-side policy.
"""

from __future__ import annotations

import asyncio
import logging

from ..view.html import LIVE
from . import fold
from .compress import ENC_DEFLATE, ENC_RAW, StreamCompressor, should_compress
from .protocol import MAX_FRAME, Frame, ProtocolError
from .protocol import text_frame

log = logging.getLogger("bridge.link")


class GuestLink:
    """Everything about writing bytes to one guest connection.

    One instance per `GuestSession`, held across reconnects the same way the
    session is: the writer is swapped out on every connect/disconnect, but
    the compressor is deliberately NOT reset except on a fresh connection --
    see the comment on `zip_out` below.
    """

    def __init__(self) -> None:
        self.writer: asyncio.StreamWriter | None = None
        self.send_lock: asyncio.Lock = asyncio.Lock()
        # One persistent deflate stream for the whole connection. Resetting it
        # mid-connection would desynchronise the guest's inflater, so it is only
        # reset on reconnect -- see Bridge._attach.
        self.zip_out: StreamCompressor = StreamCompressor()
        self.link_bps: float | None = None

    @property
    def connected(self) -> bool:
        return self.writer is not None and not self.writer.is_closing()

    async def send(self, guest, frame: Frame, *, stamp: bool = True) -> None:
        # P4: check encodability BEFORE stamping. Frame.encode raises above
        # MAX_FRAME and the replay buffer is append-only, so stamping an
        # oversized frame makes every future replay raise -- permanently, and
        # the guest can then never reconnect.
        #
        # The check is on a copy because append() mutates headers["seq"], and
        # encoding the real frame first would produce a payload with no seq in
        # it. The placeholder is wider than any seq we will reach, so a copy
        # that encodes guarantees the stamped original does too.
        probe = Frame(frame.type, dict(frame.headers), frame.body)
        probe.headers["seq"] = "9999999999"
        try:
            probe.encode()
        except ProtocolError:
            log.error("refusing to send oversized %s frame (%d byte body); "
                      "it would poison the replay buffer",
                      frame.type, len(frame.body))
            return
        if stamp:
            guest.replay.append(frame)
        payload = frame.encode()
        async with self.send_lock:
            w = self.writer
            if w is None or w.is_closing():
                return  # buffered in replay; the guest will ask for it
            try:
                w.write(payload)
                await w.drain()
            except (ConnectionResetError, BrokenPipeError):
                self.writer = None

    async def send_html(self, guest, html: str, mode: str,
                        page_no: int) -> None:
        """Fold to CP1252, compress if worthwhile, chunk under the frame cap.

        Chunking keeps the send lock free between chunks, so a long block
        cannot delay a keystroke or a Stop. A LIVE update is never chunked --
        it replaces an element wholesale, so half of one is meaningless; an
        oversized one is simply dropped and the APPEND that follows carries the
        whole thing.
        """
        data = fold.encode(html)
        if not data:
            if mode == LIVE:
                await self.send(guest, text_frame(mode, ENC_RAW, page_no, b""))
            return

        budget = MAX_FRAME - 1024   # headroom for headers and deflate overhead
        if mode == LIVE and len(data) > budget:
            return

        # An APPEND block is ONE element, and the guest does not reassemble.
        #
        # `total`/`off` are on every chunk and look like a reassembly
        # protocol; they are not. win/app/main.c's TEXT branch calls
        # rc_html_block(f->body) per frame and uses total/off only to drive
        # the progress figure -- so a block split here arrives as several
        # separate appends, each a fragment of markup, and insertAdjacentHTML
        # gets half a <div>. What the user sees is the tail of a code block
        # rendered as page furniture.
        #
        # Splitting arbitrary HTML safely would mean knowing where its element
        # boundaries are, which is not a thing this function can know. So it
        # truncates at the boundary it CAN guarantee and says it did -- the
        # same answer tool_result gives, and the model still has the whole
        # text either way.
        if mode != LIVE and len(data) > budget:
            cut = "\n<div class=\"clip\">-- this block was too large to send "
            cut += "to the machine in one piece and has been shortened here; "
            cut += "the model still has all of it --</div>"
            marker = fold.encode(cut)
            log.warning("block of %d bytes exceeds the %d frame budget; "
                        "truncating rather than splitting an element",
                        len(data), budget)
            data = data[:budget - len(marker)] + marker

        total = len(data)
        for i in range(0, total, budget):
            chunk = data[i:i + budget]
            enc = ENC_RAW
            if should_compress(chunk, self.link_bps):
                chunk = self.zip_out.compress(chunk)
                enc = ENC_DEFLATE
            # total/off are in DECOMPRESSED bytes and are on every chunk, not
            # just the first: the guest shows a progress figure from them, and
            # a reconnect mid-transfer would otherwise leave it with a
            # denominator it never received.
            await self.send(guest, text_frame(mode, enc, page_no, chunk,
                                              total=total, off=i))

    async def wire_yield(self, guest) -> bool:
        """Give everything else a turn on the wire. True if sync should pause.

        Measured, because the obvious reasoning is wrong: a chunk loop over an
        UNCONTENDED lock, with a `drain()` below the high-water mark, contains
        no await point that actually suspends. 128 chunks of a one-megabyte
        push therefore run to completion before any other task runs at all --
        so a permission prompt is not queued behind sync, it has not been
        composed yet. FIFO lock fairness cannot help with a task that never
        gets scheduled.

        `sleep(0)` is what reschedules us behind whatever is already runnable.
        The return value is the second half of the rule: while the user has a
        prompt in front of them, sync does not start another file. The pass
        ends early and the next one finishes the job -- every path commits
        independently, so a partially applied pass is a consistent state.
        """
        await asyncio.sleep(0)
        return guest.perm_pending is not None

    @staticmethod
    async def send_raw(writer: asyncio.StreamWriter, frame: Frame) -> None:
        """Send one frame with no session, no lock and no replay.

        Used before a `GuestSession` exists to hang a send helper off:
        every refusal during HELLO/handshake, and a protocol error on a
        connection that never got that far.
        """
        try:
            writer.write(frame.encode())
            await writer.drain()
        except Exception:
            pass
