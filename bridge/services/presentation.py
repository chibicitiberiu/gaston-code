"""PresentationService: coalescing, rendering, pagination, and page delivery.

Operates on guest.view (TranscriptView) and manages text streaming, HTML block
recording into paginator, and sending PAGESTART/PAGEEND and HTML frames.
"""

from __future__ import annotations

import logging
from typing import Any

from ..guest import GuestSession
from ..view import html as H
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
            if style == "plain":
                async def sink(text: str) -> None:
                    await self.write_updates(guest, guest.view.render.feed(text))
            else:
                async def sink(text: str, _s=style) -> None:
                    await self.write_block(
                        guest, f'<div class="{_s}">' + H.esc(text) + "</div>"
                    )
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
        await self.write_updates(guest, guest.view.render.flush())

    async def write_updates(
        self, guest: GuestSession, updates: list
    ) -> None:
        """Send a batch of renderer updates."""
        for u in updates:
            if u.mode == APPEND:
                await self.write_block(guest, u.html)
            else:
                await self.connection.send_html(
                    guest, u.html, LIVE, guest.view.pages.live.number
                )

    async def write_block(
        self,
        guest: GuestSession,
        html: str,
        *,
        stream: bool = True,
        notify: bool = True,
    ) -> None:
        """Record a finished block into the paginator, then send it."""
        if not html:
            return

        page_no = guest.view.pages.live.number
        sealed, chunks = guest.view.pages.write("html", html)

        if stream and (guest.view.view_page is None or guest.view.view_page == page_no):
            on_this_page = "".join(text for pg, text in chunks if pg == page_no)
            if on_this_page:
                await self.connection.send_html(
                    guest, on_this_page, APPEND, page_no
                )

        if notify:
            for pg in sealed:
                await self.connection.send(
                    guest, pagesealed(pg.number, guest.view.pages.count)
                )

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
            await self.connection.send_html(
                guest, run.text, APPEND, page.number
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

