"""GuestLink: the one place a byte leaves the bridge for a guest.

Before this, the outbound half of the protocol was 44 `self._send(...)`
calls and 38 hand-built `Frame(...)` literals spread across `Bridge` -- so
header stringification and `fold.encode` were decided independently at every
call site. This class owns the writer, the send lock, the persistent
compressor and the line that chunks outbound HTML under the frame cap. The
services (`ConnectionService`, `PresentationService`, and the others that
hold a `guest`) still decide WHAT to send and WHEN; this decides how it
reaches the wire.

Deliberately NOT the frame vocabulary itself -- see
`bridge/wire/protocol.py`'s typed constructors for that. This module never
builds a `Frame` whose headers depend on server-side policy.
"""

from __future__ import annotations

import asyncio
import logging
import time

from ..view.html import LIVE
from . import fold
from .compress import (
    ENC_DEFLATE,
    ENC_RAW,
    StreamCompressor,
    StreamDecompressor,
    should_compress,
)
from .protocol import (
    MAX_FRAME,
    Frame,
    ProtocolError,
    export_data,
    export_end,
    export_start,
    text_frame,
)

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
        # reset on reconnect -- see ConnectionService.attach.
        self.zip_out: StreamCompressor = StreamCompressor()
        self.zip_in: StreamDecompressor = StreamDecompressor()
        self.link_bps: float | None = None
        self.deflate_bps: float | None = None

    @property
    def connected(self) -> bool:
        return self.writer is not None and not self.writer.is_closing()

    async def send(self, guest, frame: Frame, *, compress: bool = False) -> bool:
        """Returns whether the frame actually reached the wire.

        `guest` is currently unused here -- it used to be how this reached
        `guest.replay` to stamp a seq, which was retired with the buffer
        itself (docs/BACKLOG.md #5). It stays on the signature anyway:
        `GuestLink` is deliberately reachable only through the owning
        `GuestSession` -- see `TranscriptView`'s docstring (bridge/view/
        transcript.py) for why that boundary exists independently of
        replay -- and a future per-guest send policy has somewhere to hang
        without another signature change.

        A closed writer is an ordinary, expected condition (the guest is
        simply gone) and returns False -- most callers have nothing better
        to do with that than log and carry on, and a lost NOTICE is not
        fatal. An oversized frame is not ordinary: see below.
        """
        async with self.send_lock:
            if compress and frame.body and should_compress(frame.body, self.link_bps, self.deflate_bps):
                frame = Frame(frame.type,
                              dict(frame.headers, enc=ENC_DEFLATE),
                              self.zip_out.compress(frame.body))
            try:
                payload = frame.encode()
            except ProtocolError:
                log.error("refusing to send oversized %s frame (%d byte body); "
                          "it exceeds the %d byte wire cap and can never be "
                          "delivered -- the caller must chunk or truncate it",
                          frame.type, len(frame.body), MAX_FRAME)
                raise
            w = self.writer
            if w is None or w.is_closing():
                # Simply lost: nothing buffers this for a later retry.
                return False
            try:
                w.write(payload)
                await w.drain()
            except (ConnectionResetError, BrokenPipeError):
                self.writer = None
                return False
        return True

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
            # total/off are in DECOMPRESSED bytes and are on every chunk, not
            # just the first: the guest shows a progress figure from them, and
            # a reconnect mid-transfer would otherwise leave it with a
            # denominator it never received.
            await self.send(guest, text_frame(mode, ENC_RAW, page_no, chunk,
                                              total=total, off=i),
                            compress=True)
            if i + budget < total:
                # Between chunks, not merely between calls: without this, a
                # big page or export starves the event loop -- see wire_yield.
                await self.wire_yield(guest)

    async def send_export(self, guest, data: bytes) -> None:
        """Stream an export to the guest, chunked, with a START and an END.

        Unlike `send_html` this never truncates and never drops. An export is
        a file the user asked for; a shortened one with a marker in the middle
        is not a smaller export, it is a broken file. If a chunk cannot be
        sent the transfer ends with `status=error` and the guest throws away
        what it had.
        """
        budget = MAX_FRAME - 1024
        total = len(data)
        if not await self.send(guest, export_start(total)):
            return
        sent = 0
        for i in range(0, total, budget):
            chunk = data[i:i + budget]
            if not await self.send(guest, export_data(i, total, ENC_RAW, chunk),
                                   compress=True):
                log.warning("export: the link went away at %d/%d bytes",
                            i, total)
                await self.send(guest, export_end(
                    False, bytes_=sent,
                    reason="The connection dropped during the export."))
                return
            sent = i + len(data[i:i + budget])
            # Between chunks, for the same reason send_html yields: a
            # multi-hundred-KB export would otherwise hold the event loop for
            # its whole duration and a Stop press would wait it out.
            if i + budget < total:
                await self.wire_yield(guest)
        await self.send(guest, export_end(True, bytes_=sent))

    async def wire_yield(self, guest=None) -> None:
        """Give everything else a turn on the wire, and count as liveness.

        The liveness stamp matters here because `ConnectionService.pump` does
        not read the socket again until the handler it is awaiting returns,
        and two handlers push for a long time while sending: a full-page
        render and an export of the whole transcript. `guest.last_seen` is
        otherwise only set in `ConnectionService.handle` -- so at
        `--link-bps 5000` a 450 KB export is 90 seconds of inbound
        silence, and the reaper read that as a dead guest: it denied whatever
        prompt was on screen, closed the writer, and `send_export` then tried
        to report the failure down the socket it had just lost. The user
        asked for a file and the bridge tore its own connection down to
        deliver it.

        This is called BETWEEN chunks, which is what makes it the right place
        to stamp rather than `send`. A single small frame proves nothing --
        it lands in the kernel buffer whether or not anything is alive at the
        far end -- but a bulk push that keeps completing chunks is the
        receive window advancing, which no dead guest can fake. And when the
        far end IS dead, `drain()` blocks as soon as the window fills, no
        further chunk completes, nothing is stamped, and the reaper does its
        job and closes the writer out from under the stalled send. So this
        exempts a live transfer without making the guest unreapable, which a
        plain `transferring` flag would have.

        Measured, because the obvious reasoning is wrong: a chunk loop over an
        UNCONTENDED lock, with a `drain()` below the high-water mark, contains
        no await point that actually suspends. 128 chunks of a one-megabyte
        push therefore run to completion before any other task runs at all --
        so a permission prompt is not queued behind sync, it has not been
        composed yet. FIFO lock fairness cannot help with a task that never
        gets scheduled.

        `sleep(0)` is what reschedules us behind whatever is already
        runnable. That is the whole of this function's job: it
        used to also return whether sync should pause for an open permission
        prompt, which made it the wrong primitive for any OTHER bulk sender
        to call -- a page or a future transcript export has no reason to cut
        itself short because the user has a dialog open elsewhere. That
        policy is sync's own to apply; see syncrun.py's `guest.perms.pending`
        checks right after the calls to this.
        """
        if guest is not None:
            guest.last_seen = time.monotonic()
        await asyncio.sleep(0)

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
