"""GuestSyncState and SyncTransport: the sync subprotocol's own RPC layer.

Before this, `Pass` (syncrun.py) was constructed as
`Pass(self, guest, self.sync_dir, self.sync_state)` -- the entire `Bridge`
and the entire `GuestSession` -- and used exactly four things off them:
`bridge._send`, `bridge.wire_yield`, `guest.sync.pending`, `guest.sync.seq`
(plus a fifth the audit's four-thing count missed on this reading of the
tree: `guest.perm_pending`, read once in `run()` to avoid competing with an
open permission prompt for the wire). The oversized interface produced a
defensive `getattr(guest, "caps", None) or C.Caps()` in `Pass.__init__` -- a
sign `Pass` could not be sure what it had been handed.
docs/audit/02-bridge-python.md 02.19, landed together with 02.8 as the
previous pass judged ("02.19 ... judged as riding on 02.8 -- which you are
now doing, so do them together").

`GuestSyncState` is the audit's "Sync RPC" `GuestSession` group
(`sync_pending`, `sync_seq`, `sync_lock`, `sync_active`), with the id
allocator moved onto it from `Pass` (`_next_id` used to live there and reach
back to mutate `guest.sync.seq` directly -- 02.2's own suggestion).

`SyncTransport` is what `Pass` actually needs: one guest connection to send
sync frames over and register replies against. It owns the four
request/send/await/cleanup blocks that used to be duplicated, one per RPC,
across `scan_guest`, `push`, `pull` and `mkdir` in syncrun.py (02.8's
"Transport" bullet) -- allocate an id, install a `Wait`, send, await the
future under a timeout, and always pop the entry in a `finally`, regardless
of the shape of the reply.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from . import sync as S
from ..wire.protocol import Frame

#: Matched against the guest's own chunked-read size by
#: test_guest_agreement.py, which imports this as `syncrun.CHUNK` --
#: syncrun.py re-exports it (`from .synctransport import CHUNK`) rather than
#: moving the constant out from under that test.
CHUNK = 8192


@dataclass
class GuestSyncState:
    """The sync subprotocol's own id namespace, in-flight table and lock.

    Reached directly by `SyncService.sync_frame`/`_fail_syncs`/`run_sync`, which
    still index `.pending` and toggle `.active` themselves -- they are
    answering "which `Wait` does this reply belong to" and "is a pass
    running", the same questions `SyncTransport` answers for `Pass`, and do
    not need an extra layer to ask them. `GuestSession` forwards
    `sync_pending`/`sync_seq`/`sync_lock`/`sync_active` to this as
    properties, the same pattern `link: GuestLink` established for
    `writer`/`send_lock`/`zip_out`/`link_bps`, so nothing outside server.py
    had to change shape.
    """

    pending: dict[str, S.Wait] = field(default_factory=dict)
    seq: int = 0
    #: Serialises writers to base.json. Lives here, in the group the audit
    #: calls "Sync RPC", rather than with the file itself -- see
    #: `SyncService.baseline_lock`'s own docstring, which the audit's §5.3
    #: criticises for the same reason without asking this pass to relocate
    #: it; that is a separate, unassigned finding, and this class only
    #: gives the existing placement a name.
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    #: Whether a pass is running right now, for `_reap_idle` and
    #: `_release_all_latches` to check without racing `sync_lock` itself.
    active: bool = False

    def next_id(self) -> str:
        self.seq += 1
        return f"s{self.seq}"


class SyncTransport:
    """What `Pass` needs from the connection, and nothing else.

    One instance per pass, built by `SyncService.run_sync` from the guest whose
    `.link` and `.sync` it wraps. `Pass` itself never sees a `Bridge` or a
    `GuestSession` -- it is handed this and a `Caps`.
    """

    def __init__(self, guest) -> None:
        # `guest` itself, not just `guest.link`: `.sync` (the id allocator
        # and in-flight table) and `.perm_pending` (has_pending_prompt below)
        # both live on the `GuestSession`, not on `GuestLink`.
        #
        # `GuestLink.send` also takes `guest` as an argument, though nothing
        # below relies on that -- it used to append to `guest.replay` when
        # `stamp=True`, which every send here was `stamp=False` against on
        # purpose (a replayed SYNCPUT would re-write the file; a replayed
        # SYNCSCAN would re-run a scan that can legitimately take minutes).
        # The replay buffer that guarded against is retired (docs/BACKLOG.md
        # #5), and with it `stamp=`; `GuestLink.send` still takes `guest` for
        # a reason unrelated to sync -- see its own docstring.
        self._guest = guest

    @property
    def has_pending_prompt(self) -> bool:
        """Is there a permission prompt on the guest's screen right now.

        Sync's own policy (`Pass.run`) is not to compete with it for the
        wire; this is the one fact about the guest, outside the sync
        subprotocol itself, that policy needs. See `wire_yield`'s docstring
        in bridge/wire/link.py for why the check cannot live inside the
        yield primitive instead.
        """
        return self._guest.perm_pending is not None

    async def yield_(self) -> None:
        await self._guest.link.wire_yield(self._guest)

    async def scan(self) -> S.ScanResult:
        state = self._guest.sync
        sid = state.next_id()
        fut = asyncio.get_running_loop().create_future()
        state.pending[sid] = S.Wait(kind="scan", future=fut, rows={}, display={})
        try:
            await self._guest.link.send(
                self._guest, Frame("SYNCSCAN", {"id": sid}))
            return await asyncio.wait_for(fut, timeout=600)
        finally:
            state.pending.pop(sid, None)

    async def put(self, rel_wire: bytes, data: bytes, *, crc: int, cls: str,
                  if_crc: str) -> S.AckResult:
        """Chunked SYNCPUT.../SYNCPUTEND against one id, one reply.

        `if_crc` is the precondition: the guest re-reads and refuses if the
        world moved underneath it, which turns a silent overwrite into a
        conflict the next pass resolves correctly. `Pass.push` decides what
        it is; this only carries it.
        """
        state = self._guest.sync
        sid = state.next_id()
        fut = asyncio.get_running_loop().create_future()
        state.pending[sid] = S.Wait(kind="put", future=fut)
        prefix = str(len(rel_wire)).encode() + b" " + rel_wire
        try:
            off = 0
            while True:
                piece = data[off:off + CHUNK]
                await self._guest.link.send(
                    self._guest,
                    Frame("SYNCPUT", {"id": sid, "total": str(len(data)),
                                      "off": str(off)}, prefix + piece),
                    compress=True)
                off += CHUNK
                if off >= len(data):
                    break
                # Between chunks, not merely between files (04.12): a
                # megabyte is 128 chunks and none of them suspends on its
                # own.
                await self.yield_()
            await self._guest.link.send(
                self._guest,
                Frame("SYNCPUTEND", {
                    "id": sid, "crc": str(crc), "cls": cls, "if_crc": if_crc,
                }))
            return await asyncio.wait_for(fut, timeout=300)
        finally:
            state.pending.pop(sid, None)

    async def get(self, rel_wire: bytes) -> S.GetResult:
        state = self._guest.sync
        sid = state.next_id()
        fut = asyncio.get_running_loop().create_future()
        state.pending[sid] = S.Wait(kind="get", future=fut, data=bytearray())
        try:
            await self._guest.link.send(
                self._guest, Frame("SYNCGET", {"id": sid}, rel_wire))
            return await asyncio.wait_for(fut, timeout=300)
        finally:
            state.pending.pop(sid, None)

    async def get_attachment(self, att_id: str) -> S.GetResult:
        state = self._guest.sync
        sid = state.next_id()
        fut = asyncio.get_running_loop().create_future()
        state.pending[sid] = S.Wait(kind="get", future=fut, data=bytearray())
        try:
            await self._guest.link.send(
                self._guest, Frame("ATTGET", {"id": sid, "att": att_id}))
            return await asyncio.wait_for(fut, timeout=300)
        finally:
            state.pending.pop(sid, None)

    async def mkdir(self, rel_wire: bytes) -> S.AckResult:
        state = self._guest.sync
        sid = state.next_id()
        fut = asyncio.get_running_loop().create_future()
        state.pending[sid] = S.Wait(kind="mkdir", future=fut)
        try:
            await self._guest.link.send(
                self._guest, Frame("SYNCMKDIR", {"id": sid}, rel_wire))
            return await asyncio.wait_for(fut, timeout=120)
        finally:
            state.pending.pop(sid, None)
