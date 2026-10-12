"""PresentationService: coalescing, rendering, pagination, and page delivery.

Operates on guest.view (TranscriptView) and manages text streaming, HTML block
recording into paginator, and sending PAGESTART/PAGEEND and HTML frames.
"""

from __future__ import annotations

import logging
from typing import Any

from ..guest import GuestSession
from ..view import html as H
from ..view.blocks import KINDS, MAX_RECORD, Block, encode
from ..view.html import APPEND, LIVE
from ..wire.coalesce import Coalescer
from ..wire.protocol import Frame, error, notice, pageend, pagesealed, pagestart

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
            else:
                body = encode(u.block) if u.block is not None else b""
                await self.connection.send_records(
                    guest, body, LIVE, guest.view.pages.live.number
                )

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
        sealed, chunks = guest.view.pages.write(record)

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
        # Safe to send under a page the guest is not showing: Turn::onText
        # ignores the page header entirely and appends what arrives. A
        # reader following LIVE wants continuous text, and one who has
        # paged away is excluded by the view_page test below.
        landed = chunks[0][0] if chunks else guest.view.pages.live.number
        if stream and (guest.view.view_page is None
                       or guest.view.view_page == landed):
            await self.connection.send_records(
                guest, record, APPEND, landed
            )

        if notify:
            for pg in sealed:
                await self.connection.send(
                    guest, pagesealed(pg.number, guest.view.pages.count)
                )

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

    async def on_getpage(self, guest: GuestSession, f: Frame) -> None:
        await self.send_page(guest, f.headers.get("page", ""))

    async def send_page(self, guest: GuestSession, want: str) -> None:
        """Render one page to the guest. LIVE is requested as page=live."""
        if want == "live":
            number = guest.view.pages.live.number
            guest.view.view_page = None
        else:
            try:
                number = int(want)
            except ValueError:
                await self.connection.send(guest, error("bad_page", want))
                return
            guest.view.view_page = (
                None if number == guest.view.pages.live.number else number
            )

        page = guest.view.pages.page(number)
        if page is None:
            await self.connection.send(guest, error("no_such_page", want))
            return

        await self.connection.send(
            guest,
            pagestart(
                page.number,
                guest.view.pages.count,
                guest.view.view_page is None,
                page.continued,
            ),
        )
        for run in page.runs:
            await self.connection.send_records(
                guest, run.body, APPEND, page.number
            )
            await self.connection.wire_yield(guest)
        await self.connection.send(guest, pageend(page.number))

        if guest.view.view_page is None and not page.runs:
            await self.connection.send(
                guest,
                notice(
                    "empty", "Type a message below to start this conversation."
                ),
            )

    async def reset_transcript(self, guest: GuestSession) -> None:
        """Reset the guest's transcript view."""
        await guest.view.reset()

    def mark_user_turn(self, guest: GuestSession, line: str) -> Any:
        """Record a user turn marker in the paginator."""
        return guest.view.pages.mark_user_turn(line)

