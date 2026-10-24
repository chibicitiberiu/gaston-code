"""TranscriptView: what one guest is watching and how much of it fits.

notes/audit/02-bridge-python.md 02.2 groups `GuestSession`'s 30 fields into
seven, by who reads them; this is the "View" group -- `pages`, `parse`,
`coalescers`, `view_live` and the window size the client declared,
guest's RAM at handshake. `GuestLink` (bridge/wire/link.py) already moved
the "Link" group out the same way; this follows it.

Deliberately NOT the "paginate, stream only if live, then seal" rule
itself (02.18): that stays on `PresentationService.write_block`/`send_range`, because
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

import asyncio
from dataclasses import dataclass, field

from ..wire.coalesce import Coalescer
from .blocks import BlockStream
from .pages import Transcript

#: What a client that did not declare one gets. The same 90 the bridge used
#: to compute for a 32MB guest -- a floor, not a policy: every real client
#: says, and stage 3b's point is that the one with the memory constraint is
#: the one that should.
DEFAULT_BLOCK_LIMIT = 90


@dataclass
class TranscriptView:
    """One guest's transcript: what has been rendered, and what it sees now."""

    #: The transcript lives here, in full. The guest only ever holds one
    #: page, which is what bounds its memory by page size rather than by
    #: conversation length.
    pages: Transcript = field(default_factory=Transcript)
    #: The PARSER, not a renderer -- stage 3 C2. It was an HtmlStream and
    #: the field was called `render`, which stopped being true when the
    #: markup moved to the client: what this produces is structure, and
    #: what draws it is on the far side of the wire now.
    parse: BlockStream = field(default_factory=BlockStream)
    #: One coalescer per source stream (assistant text, thinking). Coalescing
    #: happens BEFORE parsing, so the parser receives whole lines in ~2KB
    #: batches rather than single tokens -- which is what lets it close
    #: blocks properly and keeps the number of DOM updates on the guest down.
    coalescers: dict[str, Coalescer] = field(default_factory=dict)
    #: Whether the window the guest last asked for reaches the end of the
    #: transcript. True means new blocks stream to it; False means the guest
    #: has paged back, the bridge keeps buffering silently, and the total in
    #: GREW ticking up is the new-content notification.
    view_live: bool = True
    #: What a GETBLOCKS that names no limit gets. Not a declaration and not
    #: a policy: every real request carries its own `limit`, because the
    #: client is the one with the memory constraint. This is the floor under
    #: a malformed or truncated request, and it is the same 90 the bridge
    #: used to compute for a 32MB guest.
    block_limit: int = DEFAULT_BLOCK_LIMIT
    #: How much of a tool result this guest wants to see, declared at the
    #: handshake from stage 3 C1. 0 means it did not say and blocks.py
    #: chooses; blocks.NO_TOOL_CAP means all of it.
    toolmax: int = 0
    #: Held while a window is being delivered, and while an appended block is
    #: being streamed. `on_getblocks` runs in the reader task and
    #: `write_block` in the turn task, and a window is sent as several frames
    #: with a `wire_yield` between them -- so without this a block finished
    #: mid-delivery is written BETWEEN the window's own records, on screen
    #: and into the guest's cache file. The frame tag that would have made
    #: that diagnosable is what stage 3b deleted, which is the argument for
    #: the lock rather than for noticing it later.
    deliver: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def reset(self) -> None:
        """Start over: a fresh transcript and parser, every live coalescer
        stopped and dropped, and the view back at the live end.

        `_new_session` and `_switch` each rebuilt this by hand, with their
        own copy of the default page limits -- one of them written as
        `guest.view.page_bytes or None or 16384`, where `or None` did
        nothing at all (notes/audit/02-bridge-python.md 02.17/02.18). There
        are no limits to copy wrongly now: a transcript is a list.
        """
        self.pages = Transcript()
        self.parse = BlockStream()
        for c in self.coalescers.values():
            await c.stop()
        self.coalescers.clear()
        self.view_live = True
