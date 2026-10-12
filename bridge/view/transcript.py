"""TranscriptView: what one guest is watching and how much of it fits.

docs/audit/02-bridge-python.md 02.2 groups `GuestSession`'s 30 fields into
seven, by who reads them; this is the "View" group -- `pages`, `render`,
`coalescers`, `view_page` and the two page-size limits scaled to the
guest's RAM at handshake. `GuestLink` (bridge/wire/link.py) already moved
the "Link" group out the same way; this follows it.

Deliberately NOT the "paginate, stream only if live, then seal" rule
itself (02.18): that stays on `PresentationService.write_block`/`send_page`, because
enforcing it here would mean this class sending frames, which means it
needs the guest's `GuestLink` -- and `GuestLink.send(guest, frame)` takes
the owning `GuestSession` back on purpose, to keep `GuestLink` reachable
only through it (see that method's own docstring for why -- it used to be
so it could reach `guest.replay`, and stays now that the replay buffer is
retired). A `TranscriptView` stored as one of that same `GuestSession`'s
fields cannot hold a `GuestLink` of its own without a construction-order
cycle. `_switch`'s own bypass of that rule during history replay is a separate,
already-investigated question (02.39) and is unaffected by this move: it
still reaches `.pages` directly, now through `guest.view.pages` rather than
`guest.pages`, exactly as deliberately as before.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..wire.coalesce import Coalescer
from .html import HtmlStream
from .pages import Paginator

#: Paginator's own defaults, used when a guest's scaled limits are still 0
#: -- either genuinely unset (limits_for() has not run yet, e.g. a bare
#: GuestSession in a unit test) or the harmless `0 or 16384` case
#: `reset()`'s docstring explains. Named so `16384`/`90` are not typed
#: twice in this file.
DEFAULT_PAGE_BYTES = 16384
DEFAULT_PAGE_BLOCKS = 90


@dataclass
class TranscriptView:
    """One guest's transcript: what has been rendered, and what it sees now."""

    #: The transcript lives here, in full. The guest only ever holds one
    #: page, which is what bounds its memory by page size rather than by
    #: conversation length.
    pages: Paginator = field(default_factory=Paginator)
    render: HtmlStream = field(default_factory=HtmlStream)
    #: One coalescer per source stream (assistant text, thinking). Coalescing
    #: happens BEFORE rendering, so the renderer receives whole lines in
    #: ~2KB batches rather than single tokens -- which is what lets it close
    #: blocks properly and keeps the number of DOM updates on the guest down.
    coalescers: dict[str, Coalescer] = field(default_factory=dict)
    #: Which page the guest is showing. None means LIVE (follow the last
    #: page). In HISTORY mode the bridge keeps buffering silently, and the
    #: page counter ticking up in the status bar is the new-content
    #: notification.
    view_page: int | None = None
    #: Page caps for THIS guest, scaled to the RAM it reported in HELLO.
    page_bytes: int = 0
    page_blocks: int = 0
    #: How much of a tool result this guest wants to see, declared at the
    #: handshake from stage 3 C1. 0 means it did not say and blocks.py
    #: chooses; blocks.NO_TOOL_CAP means all of it.
    toolmax: int = 0

    async def reset(self) -> None:
        """Start over: a fresh paginator and renderer, every live coalescer
        stopped and dropped, and the view back on LIVE.

        `_new_session` and `_switch` each rebuilt this by hand, with their
        own copy of the default page limits -- one of them written as
        `guest.view.page_bytes or None or 16384`, where `or None` did nothing at
        all (`x or None` is `None` exactly when `x or 16384` would already
        have chosen 16384). docs/audit/02-bridge-python.md 02.17/02.18.
        """
        self.pages = Paginator(self.page_bytes or DEFAULT_PAGE_BYTES,
                               self.page_blocks or DEFAULT_PAGE_BLOCKS)
        self.render = HtmlStream()
        for c in self.coalescers.values():
            await c.stop()
        self.coalescers.clear()
        self.view_page = None
