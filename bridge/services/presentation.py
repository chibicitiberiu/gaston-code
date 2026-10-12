"""PresentationService: coalescing, rendering, pagination, and page delivery.

Operates on guest.view (TranscriptView) and manages text streaming, HTML block
recording into the transcript, and sending RANGE/RANGEEND and TEXT frames.
"""

from __future__ import annotations

import logging
from typing import Any

from ..guest import GuestSession
from ..view import html as H
from ..view.blocks import KINDS, MAX_RECORD, Block, encode
from ..view.html import APPEND, LIVE
from ..wire.coalesce import Coalescer
from ..wire.protocol import (
    MAX_FRAME,
    Frame,
    error,
    grew,
    notice,
    range_end,
    range_start,
)

log = logging.getLogger("bridge.presentation")


class PresentationService:
    """Manages transcript coalescing, pagination, and guest presentation streaming."""

    def __init__(self, connection: Any) -> None:
        self.connection = connection

    def coalescer(self, guest: GuestSession, style: str) -> Coalescer:
        """Get or create the stream coalescer for this style (plain prose, thinking)."""
        c = guest.view.coalescers.get(style)
        if c is None:
            async def sink(text: str) -> None:
                # `style` is captured, not defaulted: this closure is built
                # once per call and `style` does not change under it.
                if style == "plain":
                    await self.write_updates(
                        guest, guest.view.parse.feed(text))
                else:
                    # The only other style is "thinking" (session.py's
                    # on_thinking). It used to build its own div from the
                    # style NAME, which is how .thinking came to be emitted
                    # for as long as the class has existed without ever
                    # having a stylesheet rule: an interpolated class name
                    # is invisible to every guard on either side.
                    await self.write_block(guest, H.thinking(text))
            c = Coalescer(sink)
            c.set_link_speed(guest.link.link_bps)
            c.start()
            guest.view.coalescers[style] = c
        return c

    async def emit_text(
        self, guest: GuestSession, text: str, *, style: str
    ) -> None:
        """Buffer a delta. The coalescer decides when it goes out."""
        if not text:
            return
        await self.coalescer(guest, style).add(text)

    async def flush_text(self, guest: GuestSession) -> None:
        """Flush all stream coalescers and flush the renderer."""
        for c in list(guest.view.coalescers.values()):
            await c.flush()
        await self.write_updates(guest, guest.view.parse.flush())

    async def write_updates(
        self, guest: GuestSession, updates: list
    ) -> None:
        """Send a batch of parser updates.

        The LIVE arm BYPASSES write_block, deliberately and as it always
        has: the open block is replaced wholesale on the guest rather than
        appended, so it never reaches the paginator. It is also the one
        place an EMPTY body is meaningful -- that is what clears `#l` at
        the end of a turn -- which is why `block is None` is sent rather
        than skipped.
        """
        for u in updates:
            if u.mode == APPEND:
                if u.block is not None:
                    await self.write_block(guest, u.block)
            elif guest.view.view_live:
                # ONLY TO A READER WHO IS AT THE END. The open block belongs
                # to the end of the transcript, and a reader who has paged
                # back asked for a window that does not include it -- so
                # streaming it there puts a half-written answer underneath
                # whatever they went back to read, and spends a frame every
                # 100ms on a link measured in single digits of KB/s doing
                # it. This arm bypasses write_block, which is where the
                # same test lives for finished blocks, so it needs its own.
                body = encode(u.block) if u.block is not None else b""
                await self.connection.send_records(guest, body, LIVE)

    async def write_block(
        self,
        guest: GuestSession,
        html: str | Block,
        *,
        stream: bool = True,
        notify: bool = True,
    ) -> None:
        """Record a finished block into the paginator, then send it.

        The paginator holds RECORDS from stage 3 C2, so the encode happens
        here on the way in -- which is the line A6 said would become this.
        A string caller is one that still hand-builds markup, and there are
        none left in the bridge; the union stays on the signature until C5's
        grep proves it.
        """
        if isinstance(html, str):
            if not html:
                return
            raise TypeError(
                "write_block takes a Block from stage 3 C2; the bridge does "
                "not render markup any more and a string here would reach "
                "the guest as a record it cannot parse")
        if not html.payload and KINDS[html.kind][1]:
            return

        record = encode(html)
        if len(record) > self._maxblock(guest):
            # REFUSED, not truncated, and not split. Half a record is two
            # fragments the guest must throw away, and a block the client
            # said it cannot hold is one it will not render however it
            # arrives. The model still has the whole text either way --
            # the same answer tool_result gives.
            log.warning("block of %d record bytes exceeds the %d the client "
                        "declared it can hold; it was not sent",
                        len(record), self._maxblock(guest))
            return
        guest.view.pages.write(record)

        # TAGGED WITH THE PAGE IT LANDED ON, which is the fix for a bug
        # this rewrite exposed and did not introduce. The old code captured
        # page_no BEFORE the write and then streamed only the chunks whose
        # page matched it -- so a block that forced a seal, and therefore
        # landed on the NEXT page, matched nothing and was never streamed
        # at all. A reader following LIVE simply never saw it. Verified
        # against the shipping code: two blocks, the second forcing a seal,
        # and the second produces no TEXT frame.
        #
        # It was survivable before only because a run could be SPLIT, so
        # the part that stayed still went out; 02.17 then made splitting
        # rare by sealing between blocks instead, which made the hole the
        # common case without anyone noticing. Records cannot split at all.
        #
        # A reader following LIVE wants continuous text; one who has paged
        # back is excluded by the view_live test below and learns that the
        # conversation moved on from the GREW that follows.
        # ONE OF THESE, NEVER BOTH. A reader following live is getting an
        # APPEND anyway, so the new count rides on it; a reader who has paged
        # back is getting nothing, so the count is the whole message. Sending
        # GREW in both cases was the first draft of this stage and roughly
        # doubles the frame count of a long turn, each frame its own write
        # and drain on a link measured in single digits of KB/s, to redraw
        # the same string in the page bar.
        total = guest.view.pages.transcript.count
        async with guest.view.deliver:
            if stream and guest.view.view_live:
                await self.connection.send_records(
                    guest, record, APPEND, total if notify else None
                )
            elif notify:
                await self.connection.send(guest, grew(total))

    @staticmethod
    def _maxblock(guest: GuestSession) -> int:
        """What the client said it can hold, or the bridge's own bound.

        A client that did not say gets blocks.MAX_RECORD, which is a sanity
        bound rather than a policy: before stage 3 C1 nothing declared one
        and the only limit was the frame cap.
        """
        caps = getattr(guest, "caps", None)
        declared = getattr(caps, "maxblock", 0) or 0
        return declared or MAX_RECORD

    async def on_getblocks(self, guest: GuestSession, f: Frame) -> None:
        """GETBLOCKS offset=<n>|live limit=<k>."""
        await self.send_range(guest, f.headers.get("offset", "live"),
                              f.headers.get("limit", ""))

    async def send_range(self, guest: GuestSession, want: str,
                         limit_s: str) -> None:
        """Send one window of the transcript. LIVE is asked for as offset=live.

        THE ALIGNMENT IS THE GUEST'S, DERIVED. `offset=live` cannot simply
        mean "the last `limit` records": with 200 records and a limit of 90
        that window starts at 110, which is not where any of the guest's
        pages begin. It means the last window on the guest's own grid --
        `(count - 1) // limit * limit` -- and that arithmetic follows from
        the limit the guest supplied rather than from any opinion here about
        what a page is.
        """
        t = guest.view.pages.transcript
        limit = guest.view.block_limit
        if limit_s:
            try:
                limit = int(limit_s)
            except ValueError:
                await self.connection.send(guest, error("bad_limit", limit_s))
                return
            if limit <= 0:
                await self.connection.send(guest, error("bad_limit", limit_s))
                return

        if want == "live":
            offset = max(0, t.count - 1) // limit * limit
            live = True
        else:
            try:
                offset = int(want)
            except ValueError:
                await self.connection.send(guest, error("bad_offset", want))
                return
            if offset < 0:
                await self.connection.send(guest, error("bad_offset", want))
                return
            # Following live means this window reaches the end AND starts
            # inside the transcript. `offset + limit >= count` alone is true
            # for every offset past the end, which would have made a stale
            # request look like a live one and -- with no records to show --
            # put "type a message to start this conversation" on a
            # conversation that has a hundred of them in it.
            live = offset <= t.count and offset + limit >= t.count

        records = t.range(offset, limit)
        # SET AFTER THE WINDOW IS BUILT, not before: between the two there
        # is no await, so a block appended in that gap cannot be streamed
        # into the middle of this answer.
        guest.view.view_live = live
        async with guest.view.deliver:
            await self._deliver(guest, offset, records, t.count, live)

        # AN EMPTY TRANSCRIPT, not an empty window. A window can be empty
        # because the reader asked for one past the end -- which a stale page
        # button does -- and telling them to start a conversation they are
        # already having is worse than saying nothing. `count=0` on the RANGE
        # is how an empty window is reported; this is for a transcript with
        # nothing in it at all.
        if live and t.count == 0:
            await self.connection.send(
                guest,
                notice(
                    "empty", "Type a message below to start this conversation."
                ),
            )

    async def _deliver(self, guest: GuestSession, offset: int,
                       records: list, total: int, live: bool) -> None:
        """RANGE, the records, RANGEEND -- with nothing else between them.

        Called under `guest.view.deliver`, which is what keeps a block
        finished mid-delivery from being streamed into the middle of this.
        """
        await self.connection.send(
            guest, range_start(offset, len(records), total, live)
        )
        # CHUNKED ON RECORD BOUNDARIES, like the export. A window is `limit`
        # records and a record can be `maxblock` bytes, so a joined window is
        # not bounded by the frame cap the way a page used to be -- 90 blocks
        # of 60KB is 5MB. The guest appends whatever whole records arrive in
        # a frame, so several frames read the same as one.
        budget = MAX_FRAME - 1024
        chunk: list[bytes] = []
        size = 0
        for rec in records:
            if chunk and size + len(rec) > budget:
                await self.connection.send_records(
                    guest, b"".join(chunk), APPEND
                )
                await self.connection.wire_yield(guest)
                chunk, size = [], 0
            chunk.append(rec)
            size += len(rec)
        if chunk:
            await self.connection.send_records(guest, b"".join(chunk), APPEND)
            await self.connection.wire_yield(guest)
        await self.connection.send(guest, range_end())

    async def reset_transcript(self, guest: GuestSession) -> None:
        """Reset the guest's transcript view."""
        await guest.view.reset()

