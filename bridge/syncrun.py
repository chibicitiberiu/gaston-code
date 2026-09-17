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

#: Our own working names, on both sides. The scanners must skip them or an
#: interrupted write becomes a permanent file: the other side has never seen
#: it, so it transfers it, and now `x.md.gaston-tmp` syncs forever alongside
#: `x.md`. This bit on the real machine -- a put that failed on a path bug left
#: a temp behind, and the next pass pulled it onto the bridge as real content.
WORKING_SUFFIXES = (".gaston-tmp", ".gaston-old")
#: One file must never hold the wire for long. The guest has one sender thread
#: and one outbound queue, so a big transfer starves permission prompts and exec
#: output behind it.
MAX_FILE = 1024 * 1024


class SyncError(Exception):
    """A pass that must abort rather than act on what it received."""


class Pass:
    """One reconciliation pass against one guest."""

    #: Class-level so a scan can run before SYNCEND has reported the real one.
    #: `scan_guest` overwrites it on the instance from the guest's own answer.
    guest_root = "C:\\WORK"

    #: The guest's ANSI codepage. Every path on the wire is encoded with it,
    #: and every path off the wire decoded with it, because that is the
    #: representation the guest's filesystem actually stores. HELLO does not
    #: carry GetACP() yet, so this is still an assumption -- a CP1250 guest
    #: would case-fold through the wrong table -- but it is one assumption in
    #: one place rather than UTF-8 scattered through the transfers.
    acp = "cp1252"

    def __init__(self, bridge, guest, root: str, state_dir: str):
        self.bridge = bridge
        self.guest = guest
        self.root = root
        self.state = os.path.join(state_dir, "base.json")
        self.base = S.Baseline.load(self.state)
        self.reports: list[str] = []
        #: Where the guest keeps the mirror. Reported by SYNCEND, because the
        #: MAX_PATH test measures the FULL path the guest would hold and has to
        #: run here, before a push -- the guest cannot create the file in order
        #: to tell us it is too long. Until SYNCEND arrives this is the
        #: conventional default; scan_bridge always runs after scan_guest.
        self.guest_root = "C:\\WORK"
        #: Rows established by a pull's ack, keyed by canonical path. The
        #: baseline takes its value from HERE and never from a re-scan of our
        #: own tree -- a re-scan could pick up a write the agent made during
        #: the transfer, and the next pass would then pull over it.
        self._pulled: dict[str, S.Entry] = {}
        #: Inside the agent's reach so it can read a conflict and its ancestor
        #: without a new permission path -- unlike the baseline itself, which
        #: lives outside cwd because off_limits does not cover Bash.
        self._conflict_dir = os.path.join(
            os.path.dirname(root.rstrip("/")), ".gaston", "conflict")

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
            status, files, tree_id, root, rows, display = await asyncio.wait_for(
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

        if root:
            self.guest_root = root
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
            # Directories go through exactly the same filter as files, and
            # this used to be file-only. `con/x.c` was correctly refused as a
            # file while `con` itself went out as a SYNCMKDIR -- so the bridge
            # asked Windows 98 to create `C:\WORK\con`, which is the `\con\`
            # shape that can take the machine down. It came back `bad_path`
            # that time; "the guest refused it" is not a filter.
            #
            # A refused directory prunes its subtree. Walking into it would
            # produce children that are individually legal and have nowhere to
            # live, each reported separately -- one cause, twenty messages.
            keep = []
            for name in dirnames:
                rel = P.rel_from_posix(self.root, os.path.join(dirpath, name))
                bad = P.check(rel, guest_root=self.guest_root, is_dir=True)
                if bad is not None:
                    self.reports.append(f"skipped {rel}/: {bad}")
                    continue
                keep.append(name)
                rows[P.canonical(rel)] = S.Entry(type=S.Type.DIR, display=rel)
            dirnames[:] = keep
            for name in filenames:
                full = os.path.join(dirpath, name)
                if os.path.islink(full):
                    continue
                if name.endswith(WORKING_SUFFIXES):
                    continue
                rel = P.rel_from_posix(self.root, full)
                if ignore and ignore(rel):
                    continue
                bad = P.check(rel, guest_root=self.guest_root)
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
        rb = P.wire_encode(rel, acp=self.acp)
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

    async def pull(self, key: str, rel: str, expect: S.Entry | None) -> bool:
        """Fetch one file from the guest. Returns True to advance the row.

        The mirror of `push`, and it was missing entirely: `Act.PULL` was
        produced by the diff, ranked by `order`, and then fell off the end of
        the apply chain -- so every guest-side edit was decided correctly and
        silently discarded. Nothing on either side reported it, because from
        the bridge's point of view the pass had simply finished.

        Three things here are load-bearing:

        * **The baseline advances from SYNCDATEND's crc, not the manifest's.**
          The file can be saved again between the scan and the GET, and taking
          the manifest's value would record an ancestor the guest never held --
          inverting the direction the next pass computes.
        * **Temp file, then replace.** A torn bridge write becomes a conflict
          over a half-written file next pass, and the junk ends up in
          `conflict/` as if it were the agent's work.
        * **The class comes back on the wire.** The side that owns the file
          decides whether its CRC covers raw or normalised bytes; using the
          bridge's own guess here is how the two sides end up comparing the
          outputs of different functions forever.
        """
        sid = self._next_id()
        fut = asyncio.get_running_loop().create_future()
        self.guest.sync_pending[sid] = {"future": fut}
        rb = P.wire_encode(rel, acp=self.acp)
        try:
            await self.bridge._send(
                self.guest, Frame("SYNCGET", {"id": sid}, rb), stamp=False)
            status, crc, cls, wire = await asyncio.wait_for(fut, timeout=300)
        finally:
            self.guest.sync_pending.pop(sid, None)

        if status != "ok":
            self.reports.append(f"pull {rel} refused: {status}")
            return False

        data = P.to_bridge(wire, cls)
        got = zlib.crc32(P.normalise(data, cls))
        if got != crc:
            # Invariant 2: after a transfer the two sides agree, or it is
            # reported. Never retried -- retrying the same transfer is how a
            # livelock starts.
            self.reports.append(
                f"pull {rel}: the guest said crc {crc}, the bytes that arrived "
                f"are {got}. Nothing written.")
            self.note(f"SYNC: {rel} arrived from the machine with a checksum "
                      f"that does not match what the machine reported. The "
                      f"file was NOT updated here.")
            return False
        if P.classify(data) != cls:
            # The guest's judgement and ours must agree on the bytes we now
            # hold, or the next pass CRCs different functions on each side and
            # the path never converges.
            self.reports.append(
                f"pull {rel}: the guest called it class {cls!r} and the same "
                f"bytes read as {P.classify(data)!r} here. Nothing written.")
            return False

        full = os.path.join(self.root, rel)
        os.makedirs(os.path.dirname(full) or ".", exist_ok=True)
        tmp = full + ".gaston-tmp"
        with open(tmp, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, full)

        self.keep_ancestor(rel, data)
        self._pulled[key] = S.Entry(type=S.Type.FILE, cls=cls, size=len(data),
                                    mtime=os.stat(full).st_mtime, crc=crc,
                                    display=rel)
        return True

    async def mkdir(self, key: str, rel: str) -> bool:
        """Create one directory on the guest. Returns True to advance the row.

        Its own frame and its own ack, because Invariant 1 says a row advances
        from the RECEIVER, and a directory row has no file ack to advance from.
        `exists` is success: the directory is there, which is all the row
        claims. Anything else is a refusal that must reach the agent -- a
        directory that fails to appear makes every push into it fail with
        `readonly`, which reads as a permissions problem and is not one.
        """
        sid = self._next_id()
        fut = asyncio.get_running_loop().create_future()
        self.guest.sync_pending[sid] = {"future": fut}
        rb = P.wire_encode(rel, acp=self.acp)
        try:
            await self.bridge._send(
                self.guest, Frame("SYNCMKDIR", {"id": sid}, rb), stamp=False)
            status, _ = await asyncio.wait_for(fut, timeout=120)
        finally:
            self.guest.sync_pending.pop(sid, None)

        if status in ("ok", "exists"):
            return True
        self.reports.append(f"mkdir {rel} refused: {status}")
        self.note(f"SYNC: could not create the directory {rel} on the machine "
                  f"({status}), so nothing under it can be copied there.")
        return False

    def _next_id(self) -> str:
        self.guest.sync_seq += 1
        return f"s{self.guest.sync_seq}"

    # --- conflicts ---------------------------------------------------------

    def stage_conflict(self, key: str, rel: str) -> str:
        """Preserve the bridge's version before the guest's overwrites it.

        Versioned, and mirroring the tree. A second conflict on one path would
        otherwise overwrite the only copy of the agent's work -- the mechanism
        meant to preserve it destroying it. And the ancestor is FROZEN here
        alongside, because the live baseline is about to advance and a merge
        needs the version both sides actually came from.
        """
        d = os.path.join(self.conflict_dir, rel)
        os.makedirs(d, exist_ok=True)
        n = 1
        while os.path.exists(os.path.join(d, f"mine.{n}")):
            n += 1
        mine = os.path.join(d, f"mine.{n}")
        src = os.path.join(self.root, rel)
        if os.path.exists(src):
            with open(src, "rb") as fh:
                data = fh.read()
            with open(mine, "wb") as fh:
                fh.write(data)
        anc = os.path.join(self.base_dir, rel)
        if os.path.exists(anc):
            with open(anc, "rb") as a, open(os.path.join(d, f"base.{n}"), "wb") as b:
                b.write(a.read())
        return mine

    def note(self, text: str) -> None:
        """Record a notice durably BEFORE acting on it.

        A crash between the baseline advance and the notification leaves the
        agent's work in conflict/ with nobody told, and the next pass sees
        agreement and says nothing.
        """
        self.base.pending_notice.append(text)
        self.base.save(self.state)

    def keep_ancestor(self, rel: str, data: bytes) -> None:
        """Retain the agreed content as history, not just its checksum.

        `prev` is what makes a reverted push detectable, and a real three-way
        merge needs the bytes rather than a number.
        """
        dst = os.path.join(self.base_dir, rel)
        os.makedirs(os.path.dirname(dst) or self.base_dir, exist_ok=True)
        with open(dst, "wb") as fh:
            fh.write(data)

    @property
    def base_dir(self) -> str:
        return os.path.join(os.path.dirname(self.state), "base")

    @property
    def conflict_dir(self) -> str:
        return self._conflict_dir

    # --- the whole pass ----------------------------------------------------

    async def run(self, ignore=None) -> dict:
        """One reconciliation, start to finish.

        Aborts happen before ANY row is touched, which is why the manifest is
        buffered until SYNCEND.
        """
        guest_rows = await self.scan_guest()
        bridge_rows = await asyncio.to_thread(self.scan_bridge, ignore)

        decisions = S.order(S.diff(guest_rows, bridge_rows, self.base))
        done = {"pushed": 0, "pulled": 0, "dirs": 0, "conflicts": 0,
                "reported": 0, "skipped": 0, "latched": 0, "unhandled": 0}

        for dec in decisions:
            if dec.act is S.Act.NOTHING:
                continue
            g, b = guest_rows.get(dec.key), bridge_rows.get(dec.key)

            if dec.act is S.Act.SKIP:
                done["skipped"] += 1
                self.reports.append(f"{dec.key}: {dec.reason}")
            elif dec.act is S.Act.REPORT:
                done["reported"] += 1
                self.reports.append(f"{dec.key}: {dec.reason}")
                self.note(f"SYNC: {dec.key} -- {dec.reason}")
            elif dec.act is S.Act.CONFLICT:
                done["conflicts"] += 1
                rel = (b.display if b else dec.key)

                # A conflict is COPY -> PULL -> ADVANCE -> NOTIFY, and for a
                # while this branch did only the copy and the notify. The
                # divergence therefore survived the pass, so the next one found
                # it again: a fresh mine.N/base.N pair and another notice every
                # thirty seconds, forever, with the guest's version never
                # reaching the bridge at all. Advancing the baseline is what
                # makes the conflict happen ONCE, which is what it did.
                #
                # Order matters and is not arbitrary. The copy first, because
                # it is the thing being protected. The notice next, written
                # durably before anything moves, so a crash mid-resolution
                # cannot leave the agent's work staged with nobody told. The
                # pull after that. The baseline LAST, from the pull's ack --
                # Invariant 1. Advancing it earlier and then crashing inverts
                # the direction on the next pass and overwrites the user's file
                # on their own machine, with no copy of it anywhere.
                mine = self.stage_conflict(dec.key, rel)

                # A latched path REFUSES pushes. The agent's in-flight turn
                # cannot react to a conflict it has not been told about yet,
                # so "the agent stops" is enforced here rather than requested.
                self.base.latched[dec.key] = dec.reason
                done["latched"] += 1
                self.note(
                    f"SYNC CONFLICT {rel} -- {dec.reason}. The machine's "
                    f"version wins. Yours is preserved at {mine} with the "
                    f"common ancestor beside it. STOP editing this file, tell "
                    f"the user, and merge only once they confirm they have "
                    f"finished with it.")

                if g is not None and g.type is S.Type.FILE:
                    if await self.pull(dec.key, g.display or dec.key, b):
                        done["pulled"] += 1
                        self.base.rows[dec.key] = self._pulled[dec.key]
                    else:
                        # The guest's version could not be fetched, so the
                        # baseline must NOT advance -- it would then claim an
                        # agreement that does not exist. The latch holds and
                        # the next pass tries again.
                        self.reports.append(
                            f"{rel}: conflict staged, but the machine's version "
                            f"could not be fetched; nothing here was changed")
                else:
                    # A type change, or the guest side is a directory. There is
                    # no file to fetch and no delete primitive to apply, so the
                    # honest outcome is the staged copy plus the report.
                    self.reports.append(
                        f"{rel}: conflict staged; the machine's side is not a "
                        f"plain file, so it was not applied here")
            elif dec.act is S.Act.PUSH:
                if dec.key in self.base.latched:
                    done["latched"] += 1
                    self.reports.append(f"{dec.key}: latched, push withheld")
                    continue
                rel = b.display or dec.key
                if await self.push(dec.key, rel, g):
                    done["pushed"] += 1
                    with open(os.path.join(self.root, rel), "rb") as fh:
                        data = fh.read()
                    self.keep_ancestor(rel, data)
                    # The ancestor this push REPLACED, which is what makes a
                    # stale editor buffer detectable next pass.
                    if g is not None:
                        self.base.prev[dec.key] = g
                    self.base.rows[dec.key] = b      # advanced LAST
            elif dec.act is S.Act.PULL:
                rel = (g.display if g else dec.key)
                if await self.pull(dec.key, rel, b):
                    done["pulled"] += 1
                    self.base.rows[dec.key] = self._pulled[dec.key]
            elif dec.act is S.Act.MKDIR:
                rel = dec.display or (b.display if b else dec.key)
                if await self.mkdir(dec.key, rel):
                    done["dirs"] += 1
                    self.base.rows[dec.key] = b
            elif dec.act is S.Act.ADOPT:
                # A directory the guest has and we do not is adopted, and
                # adopting it means CREATING it -- otherwise the row says the
                # two sides agree while the bridge has nothing there, and an
                # empty directory the user made on their own machine never
                # appears for the agent at all. Files inside one arrive anyway
                # because `pull` makes parents, so the case that actually goes
                # missing is the empty directory, which is exactly the one
                # nobody notices until a build fails.
                if g is not None and g.type is S.Type.DIR and b is None:
                    try:
                        os.makedirs(os.path.join(self.root, g.display),
                                    exist_ok=True)
                        done["dirs"] += 1
                    except OSError as exc:
                        self.reports.append(f"mkdir {g.display} here: {exc}")
                        continue
                self.base.rows[dec.key] = g or b
            elif dec.act is S.Act.TOMBSTONE:
                old = self.base.rows.pop(dec.key, None)
                if old is not None:
                    self.base.tombs[dec.key] = S.Tomb(cls=old.cls, crc=old.crc)
            elif dec.act is S.Act.CLEAR_TOMBSTONE:
                self.base.tombs.pop(dec.key, None)
                self.base.rows[dec.key] = g or b
            else:
                # An action with no branch used to fall off the end of this
                # chain and vanish. That is how MKDIR was lost: `src` was never
                # created, every push into it came back `readonly`, and the
                # pass reported six of seven files transferred with no error
                # anywhere. Silence is the one outcome this layer may not have.
                done["unhandled"] += 1
                self.reports.append(
                    f"{dec.key}: BUG -- no handler for {dec.act.value}")
                self.note(f"SYNC BUG: {dec.key} resolved to {dec.act.value}, "
                          f"which the bridge has no code to apply. Nothing was "
                          f"transferred for this path.")

        self.base.generation += 1
        self.base.save(self.state)
        done["reports"] = list(self.reports)
        done["notices"] = list(self.base.pending_notice)
        return done
