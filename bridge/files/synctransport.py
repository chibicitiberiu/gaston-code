"""GuestSyncState and SyncTransport: the sync subprotocol's own RPC layer.

`Pass` (syncrun.py) used to take the entire `Bridge` and the entire
`GuestSession`, of which it touched only `bridge._send`, `bridge.wire_yield`,
`guest.sync.pending`, `guest.sync.seq`, and `guest.perm_pending` (checked
once in `run()` to avoid competing with an open permission prompt for the
wire). The oversized interface showed up as a defensive
`getattr(guest, "caps", None) or C.Caps()` in `Pass.__init__` -- `Pass` could
not be sure what it had actually been handed.

`GuestSyncState` is the id namespace, in-flight `Wait` table and lock that
`Pass` needed off `GuestSession`, held as one field (`GuestSession.sync`)
rather than loose attributes. The id allocator moved onto it too, instead of
living on `Pass` and reaching back to mutate `guest.sync.seq` directly.

`SyncTransport` is the other half: one guest connection to send sync frames
over and register replies against. It owns the request/send/await/cleanup
sequence that used to be duplicated once per RPC across `scan_guest`, `push`,
`pull` and `mkdir` in syncrun.py -- allocate an id, install a `Wait`, send,
await the future under a timeout, and always pop the entry in a `finally`,
regardless of the shape of the reply.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from ..wire.protocol import Frame
from . import sync as S

#: Matched against the guest's own chunked-read size by
#: test_guest_agreement.py, which imports this as `syncrun.CHUNK` --
#: syncrun.py re-exports it (`from .synctransport import CHUNK`) rather than
#: moving the constant out from under that test.
CHUNK = 8192


@dataclass
class GuestSyncState:
    """The sync subprotocol's own id namespace, in-flight table and lock.

    Reached directly by `SyncService.sync_frame`/`fail_syncs`/`run_sync`,
    which still index `.pending` and toggle `.active` themselves -- they are
    answering "which `Wait` does this reply belong to" and "is a pass
    running", the same questions `SyncTransport` answers for `Pass`, and do
    not need an extra layer to ask them. Held as one field, `GuestSession.sync`,
    the same pattern `link: GuestLink` established for the wire side, so
    nothing outside server.py had to change shape.
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
    #: Whether a pass is running right now, for `reap_idle` and
    #: `release_all_latches` to check without racing `lock` itself.
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
        # The replay buffer that guarded against is retired (notes/BACKLOG.md
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
                # THE PREFIX GOES ON THE FIRST CHUNK ONLY, which is the
                # guest's own contract: win/sync/syncrpc.cpp says "Only the
                # FIRST chunk carries the prefix; later chunks are all
                # data" and skips `g_put.data_off` bytes when off == 0 and
                # nothing otherwise.
                #
                # Sending it every time made the guest write the path
                # bytes INTO the file at every chunk boundary, and its
                # ChunkSink (win/fs/xfer.cpp) counts what it wrote -- so
                # `m_received` ran ahead of the offset we were sending by
                # len(prefix) per chunk, and `off != m_received` refused
                # the third chunk as out_of_order.
                #
                # Which means the sync layer could not push a file bigger
                # than one chunk: 8K-16K corrupted and failed its CRC at
                # SYNCPUTEND, and anything past 16K was refused outright.
                # Found on the Athlon 2026-10-27, where a 22,437-byte
                # screcord.cpp was refused twice and the agent worked
                # around it with put_file -- which is why it looked like a
                # filename problem rather than a size one.
                body = (prefix + piece) if off == 0 else piece
                await self._guest.link.send(
                    self._guest,
                    Frame("SYNCPUT", {"id": sid, "total": str(len(data)),
                                      "off": str(off)}, body),
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
