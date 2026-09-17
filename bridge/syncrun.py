"""Running one reconciliation pass over the wire.

`sync.py` decides WHAT to do; this decides how to get it done without losing
anything when the world moves underneath. The two rules it exists to enforce:

  * every write carries the precondition it assumes (`if_crc`), because the
    manifest is a snapshot taken over seconds while a human is typing;
  * the baseline advances LAST, from the receiver's ack, so an interruption
    costs a redundant transfer and never a reversed direction.
"""

from __future__ import annotations

import asyncio
import logging
import os
import zlib

from . import paths as P
from . import sync as S
from .protocol import Frame

log = logging.getLogger("bridge.syncrun")

CHUNK = 8192
#: One file must never hold the wire for long. The guest has one sender thread
#: and one outbound queue, so a big transfer starves permission prompts and exec
#: output behind it.
MAX_FILE = 1024 * 1024


class SyncError(Exception):
    """A pass that must abort rather than act on what it received."""


class Pass:
    """One reconciliation pass against one guest."""

    def __init__(self, bridge, guest, root: str, state_dir: str):
        self.bridge = bridge
        self.guest = guest
        self.root = root
        self.state = os.path.join(state_dir, "base.json")
        self.base = S.Baseline.load(self.state)
        self.reports: list[str] = []

    # --- the guest's half --------------------------------------------------

    async def scan_guest(self) -> dict[str, S.Entry]:
        """Ask for a manifest and wait for it.

        Entries are buffered until SYNCEND, because a pass-level abort must
        happen before any row is touched -- and whether to abort depends on the
        whole manifest.
        """
        sid = self._next_id()
        fut = asyncio.get_running_loop().create_future()
        self.guest.sync_pending[sid] = {"future": fut, "rows": {}, "display": {}}
        await self.bridge._send(self.guest, Frame("SYNCSCAN", {"id": sid}),
                                stamp=False)
        try:
            status, files, tree_id, rows, display = await asyncio.wait_for(
                fut, timeout=600)
        finally:
            self.guest.sync_pending.pop(sid, None)

        if status != "ok":
            # A truncated manifest is indistinguishable from a mass deletion,
            # and the natural implementation acts on what arrived -- pushing the
            # bridge's whole tree at a machine that just said it had too many
            # files.
            raise SyncError(f"guest scan returned {status}")
        if len(rows) != files:
            raise SyncError(
                f"manifest arrived short: {len(rows)} entries, SYNCEND said "
                f"{files}. Treating a short manifest as truth would read the "
                f"missing paths as deletions.")

        if self.base.tree_id and tree_id and tree_id != self.base.tree_id:
            raise SyncError(
                f"this baseline belongs to {self.base.tree_id!r} and the guest "
                f"reports {tree_id!r}. Refusing rather than treating it as a "
                f"first sync, which would conflict the whole tree.")
        if not self.base.tree_id and tree_id:
            self.base.tree_id = tree_id

        self._guest_display = display
        return rows

    # --- the bridge's half -------------------------------------------------

    def scan_bridge(self, ignore=None) -> dict[str, S.Entry]:
        rows: dict[str, S.Entry] = {}
        seen: dict[str, str] = {}
        for dirpath, dirnames, filenames in os.walk(self.root):
            # Symlinks are not followed: one pointing outside the workdir would
            # have its target's CONTENTS pushed, bypassing off_limits entirely.
            dirnames[:] = [d for d in dirnames
                           if not os.path.islink(os.path.join(dirpath, d))
                           and not (ignore and ignore(
                               P.rel_from_posix(self.root,
                                                os.path.join(dirpath, d)) + "/"))]
            for name in dirnames:
                rel = P.rel_from_posix(self.root, os.path.join(dirpath, name))
                rows[P.canonical(rel)] = S.Entry(type=S.Type.DIR, display=rel)
            for name in filenames:
                full = os.path.join(dirpath, name)
                if os.path.islink(full):
                    continue
                rel = P.rel_from_posix(self.root, full)
                if ignore and ignore(rel):
                    continue
                bad = P.check(rel, guest_root="C:\\WORK")
                if bad is not None:
                    self.reports.append(f"skipped {rel}: {bad}")
                    continue
                key = P.canonical(rel)
                clash = seen.get(key)
                if clash is not None:
                    # The NEWCOMER is refused. Refusing both would retroactively
                    # break a path that has been syncing for weeks.
                    self.reports.append(
                        f"skipped {rel}: collides with {clash} on a "
                        f"case-insensitive filesystem")
                    continue
                seen[key] = rel
                try:
                    data = open(full, "rb").read()
                except OSError as exc:
                    rows[key] = S.Entry(type=S.Type.UNREADABLE, display=rel)
                    self.reports.append(f"unreadable {rel}: {exc}")
                    continue
                cls = P.classify(data)
                st = os.stat(full)
                rows[key] = S.Entry(type=S.Type.FILE, cls=cls, size=len(data),
                                    mtime=st.st_mtime,
                                    crc=zlib.crc32(P.normalise(data, cls)),
                                    display=rel)
        return rows

    # --- applying ----------------------------------------------------------

    async def push(self, key: str, rel: str, expect: S.Entry | None) -> bool:
        """Send one file. Returns True when the baseline may advance."""
        full = os.path.join(self.root, rel)
        data = open(full, "rb").read()
        if len(data) > MAX_FILE:
            self.reports.append(
                f"skipped {rel}: {len(data)} bytes exceeds the {MAX_FILE} cap; "
                f"one file must not hold the wire")
            return False
        cls = P.classify(data)
        wire = P.to_guest(data, cls)
        crc = zlib.crc32(P.normalise(data, cls))

        sid = self._next_id()
        fut = asyncio.get_running_loop().create_future()
        self.guest.sync_pending[sid] = {"future": fut}
        rb = rel.encode("utf-8")
        prefix = str(len(rb)).encode() + b" " + rb

        try:
            off = 0
            while True:
                piece = wire[off:off + CHUNK]
                await self.bridge._send(
                    self.guest,
                    Frame("SYNCPUT", {"id": sid, "total": str(len(wire)),
                                      "off": str(off)}, prefix + piece),
                    stamp=False)     # replay would RE-WRITE the file
                off += CHUNK
                if off >= len(wire):
                    break
            await self.bridge._send(
                self.guest,
                Frame("SYNCPUTEND", {
                    "id": sid, "crc": str(crc), "cls": cls,
                    # The precondition. The guest re-reads and refuses if the
                    # world moved -- which turns a silent overwrite into a
                    # conflict the next pass resolves correctly.
                    "if_crc": str(expect.crc) if expect else "absent",
                }),
                stamp=False)
            status, ack_crc = await asyncio.wait_for(fut, timeout=300)
        finally:
            self.guest.sync_pending.pop(sid, None)

        if status != "ok":
            self.reports.append(f"push {rel} refused: {status}")
            return False
        if ack_crc != crc:
            # Invariant 2: reported, never retried.
            self.reports.append(
                f"push {rel}: the guest acked crc {ack_crc}, we sent {crc}")
            return False
        return True

    def _next_id(self) -> str:
        self.guest.sync_seq += 1
        return f"s{self.guest.sync_seq}"
