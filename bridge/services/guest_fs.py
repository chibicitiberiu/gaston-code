"""Files on the connected machine, at any path.

The sync layer mirrors ONE folder that the person chose, and everything about
it -- the manifest, the classifier, the conflict rules -- exists to make that
folder converge. This is the other thing: read or write a named file anywhere
on the machine, byte for byte, with no baseline and no opinion about line
endings. The guest answers on the same worker (`win/rpc/fsrpc.cpp`), because
it is the same kind of work, but the two never share a decision.

Ids are `f`-prefixed and the guest enforces it (`id_matches_family` in
win/rpc/rpc.cpp). They travel the same STREAM lane as the sync frames, and
the prefix is what stops one family's reply resolving the other's future.
"""

from __future__ import annotations

import asyncio
import logging
import zlib
from dataclasses import dataclass

from ..guest import FsCall, GuestSession
from ..wire.protocol import fs_drives, fs_get, fs_put, fs_putend

log = logging.getLogger("bridge.fs")

#: Per call. A get is capped on the guest, so the only unbounded thing here is
#: a slow link; a put of a few megabytes over a modem is minutes, and the MCP
#: tool's own ceiling is 300s, which this has to sit inside.
BUDGET_S = 240.0

#: What one FSPUT frame carries. The guest's inbound frame buffer is 64KB and
#: the sync layer has used 8KB against it for years.
CHUNK = 8192


@dataclass
class Drive:
    letter: str
    type: int
    label: str
    serial: str
    free_kb: int
    total_kb: int = 0
    #: Whether the volume answered: True (probed, present), False (probed,
    #: nothing in it) or None (a removable or CD-ROM the guest did not probe
    #: -- BACKLOG 20). None is NOT the same as False: a mounted CD-ROM has
    #: zero bytes free and is not an empty drive, and a drive nobody asked
    #: is not known to be empty either.
    has_media: bool | None = None


@dataclass
class FileBytes:
    data: bytes
    crc: int
    truncated: bool


class FsError(Exception):
    """A refusal from the guest, carrying its status word.

    The vocabulary is the sync layer's -- bad_path, missing, is_directory,
    locked, readonly, no_parent, no_space, too_long, out_of_order,
    crc_mismatch, busy -- because test_guest_agreement.py asserts that
    win/sync/syncrpc.cpp's literals and bridge/status.py's enum are the same
    SET in both directions. A token the fs handlers could send and that file
    could not would fail that test, which is the intended pressure: it keeps
    one vocabulary rather than letting a second grow beside it.
    """

    def __init__(self, status: str) -> None:
        super().__init__(status)
        self.status = status


class GuestFsService:
    def __init__(self, connection) -> None:
        self.connection = connection

    # -- sending ------------------------------------------------------------

    def _begin(self, guest: GuestSession, kind: str) -> tuple[str, FsCall]:
        guest.fs_seq += 1
        fs_id = f"f{guest.fs_seq}"
        wait = FsCall(kind=kind,
                      future=asyncio.get_running_loop().create_future())
        guest.fs_pending[fs_id] = wait
        return fs_id, wait

    async def get(self, guest: GuestSession, path: str, *,
                  max_bytes: int = 0) -> FileBytes:
        fs_id, wait = self._begin(guest, "get")
        try:
            await self.connection.send(guest, fs_get(fs_id, path,
                                                     max_bytes=max_bytes))
            headers = await asyncio.wait_for(wait.future, timeout=BUDGET_S)
        except TimeoutError as exc:
            raise FsError("timeout") from exc
        finally:
            guest.fs_pending.pop(fs_id, None)

        status = headers.get("status", "")
        if status != "ok":
            raise FsError(status or "no_status")

        data = bytes(wait.data)
        want = int(headers.get("crc", "0") or 0)
        got = zlib.crc32(data) & 0xFFFFFFFF
        if want != got:
            # Not a warning. The guest checksummed the bytes as it read them,
            # so a disagreement means the wire changed them -- and handing
            # those bytes to the agent as though they were the file is how a
            # corrupted read becomes a confident wrong answer.
            raise FsError("crc_mismatch")
        return FileBytes(data=data, crc=got,
                         truncated=headers.get("truncated") == "1")

    async def put(self, guest: GuestSession, path: str, data: bytes) -> int:
        """Bytes written. Raises FsError on a refusal."""
        fs_id, wait = self._begin(guest, "put")
        try:
            off = 0
            # An empty file still needs its opening frame, or the guest never
            # learns the path and FSPUTEND arrives for a transfer that was
            # never begun.
            while True:
                piece = data[off:off + CHUNK]
                await self.connection.send(guest,
                                           fs_put(fs_id, path, off, piece))
                off += len(piece)
                if off >= len(data):
                    break
            await self.connection.send(
                guest, fs_putend(fs_id, zlib.crc32(data) & 0xFFFFFFFF))
            headers = await asyncio.wait_for(wait.future, timeout=BUDGET_S)
        except TimeoutError as exc:
            raise FsError("timeout") from exc
        finally:
            guest.fs_pending.pop(fs_id, None)

        status = headers.get("status", "")
        if status != "ok":
            raise FsError(status or "no_status")
        return int(headers.get("bytes", "0") or 0)

    async def drives(self, guest: GuestSession, *,
                     probe: bool = False) -> list[Drive]:
        fs_id, wait = self._begin(guest, "drives")
        try:
            await self.connection.send(guest, fs_drives(fs_id, probe=probe))
            await asyncio.wait_for(wait.future, timeout=BUDGET_S)
        except TimeoutError as exc:
            raise FsError("timeout") from exc
        finally:
            guest.fs_pending.pop(fs_id, None)
        return list(wait.rows)

    # -- receiving ----------------------------------------------------------

    async def fs_frame(self, guest: GuestSession, f) -> None:
        """FSDAT / FSDATEND / FSPUTACK / FSDRIVE / FSDRIVEEND."""
        fs_id = f.headers.get("id", "")
        wait = guest.fs_pending.get(fs_id)
        if wait is None:
            # Not silence: a reply nobody awaits is either a late frame from a
            # call that timed out or an id bug, and the second one is
            # invisible without this line.
            log.debug("%s for %s, which nobody is waiting for", f.type, fs_id)
            return

        if f.type == "FSDAT":
            wait.data.extend(f.body or b"")
            return
        if f.type == "FSDRIVE":
            h = f.headers
            wait.rows.append(Drive(
                letter=h.get("letter", ""),
                type=int(h.get("type", "0") or 0),
                label=h.get("label", ""),
                serial=h.get("serial", ""),
                free_kb=int(h.get("free_kb", "0") or 0),
                total_kb=int(h.get("total_kb", "0") or 0),
                has_media={"1": True, "0": False}.get(h.get("media"))))
            return

        if not wait.future.done():
            wait.future.set_result(dict(f.headers))

    def fail_calls(self, guest: GuestSession | None) -> None:
        """The connection went. Every waiter hears about it now rather than
        at the end of its budget."""
        if guest is None:
            return
        for fs_id, wait in list(guest.fs_pending.items()):
            if not wait.future.done():
                wait.future.set_exception(FsError("disconnected"))
            guest.fs_pending.pop(fs_id, None)
