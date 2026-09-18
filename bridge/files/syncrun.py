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
import shutil
import zlib

from . import attrs as A
from . import caps as C
from . import ignores as I
from . import paths as P
from . import sync as S
from ..wire.protocol import Frame

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
#: The acts that actually put bytes on the wire, and therefore the ones that
#: have to yield first. A REPORT or a TOMBSTONE touches nothing and pausing
#: before one would only make a pass slower for no benefit.
_MOVES_BYTES = frozenset((S.Act.PUSH, S.Act.PULL, S.Act.MKDIR,
                          S.Act.CONFLICT))

MAX_FILE = 1024 * 1024


class SyncError(Exception):
    """A pass that must abort rather than act on what it received."""


class Pass:
    """One reconciliation pass against one guest."""

    #: Class-level fallbacks. `__init__` always overwrites both on a real
    #: `Pass`, but `test_sync_wire.py` builds bare `Pass` subclasses that skip
    #: `__init__` to exercise `scan_bridge` in isolation, and those reach these
    #: directly -- verified live by `make test` before this was trimmed away.
    guest_root = "C:\\WORK"
    caps = C.Caps()

    def __init__(self, bridge, guest, root: str, state_dir: str):
        self.bridge = bridge
        self.guest = guest
        self.root = root
        self.state = os.path.join(state_dir, "base.json")
        self.base = S.Baseline.load(self.state)

        # What the client says it is, or the Win9x assumption if it is too old
        # to say. A value the bridge cannot honour stops the pass here.
        try:
            self.caps = C.from_hello(getattr(guest, "hello", {}) or {})
        except C.CapsError as exc:
            raise SyncError(str(exc)) from None

        # A codepage or case-rule CHANGE re-keys the whole baseline, because
        # `canonical` folds through both. Every row would then miss, read as
        # "absent on both sides", and resolve as a tombstone or a first-sync
        # conflict -- across the entire tree, in one pass, with the agent's
        # work staged into conflict/ at best. Refuse, exactly like a tree_id
        # mismatch, and let a human decide.
        keying = f"{self.caps.acp}/{self.caps.case}"
        if self.base.acp and self.base.acp != keying:
            raise SyncError(
                f"this baseline keys its paths as {self.base.acp} and the "
                f"machine now reports {keying}. Refusing: the matching keys "
                f"are folded through the codepage and the case rule, so "
                f"continuing would miss every row and read the whole tree as "
                f"deleted.")
        self.base.acp = keying
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
        #: The tree's `.gitattributes`/`.gastonattributes`, loaded once per
        #: pass. None until `run` loads it, so a caller driving scan_bridge
        #: directly gets content-only classification.
        self.attrs = None
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
        self.guest.sync_pending[sid] = S.Wait(
            kind="scan", future=fut, rows={}, display={})
        await self.bridge._send(self.guest, Frame("SYNCSCAN", {"id": sid}),
                                stamp=False)
        try:
            status, files, tree_id, root, rows, _display = await asyncio.wait_for(
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

        # The root, compared in full, because tree_id cannot be trusted to
        # differ when the root does. The guest builds it as
        # "%08lX-%s" % (serial, root) into 64 bytes, so only the first 54
        # characters of the root survive -- two roots on one volume sharing
        # that prefix give the SAME id and the check above passes. A changed
        # root also means the baseline's paths, which are relative to it,
        # describe different files: adopting it silently would reconcile one
        # tree against another's history.
        if root and self.base.guest_root and root != self.base.guest_root:
            raise SyncError(
                f"this baseline was built against {self.base.guest_root!r} "
                f"and the machine now reports {root!r}. Refusing: the stored "
                f"paths are relative to the root, so continuing would compare "
                f"one tree against another tree's history.")
        if root and not self.base.guest_root:
            self.base.guest_root = root

        if root:
            self.guest_root = root
        return rows

    # --- the bridge's half -------------------------------------------------

    def scan_bridge(self, ignore=None, attrs=None) -> dict[str, S.Entry]:
        rows: dict[str, S.Entry] = {}
        seen: dict[str, str] = {}
        base = getattr(self, "base", None)
        cache = base.scan_cache if base is not None else None
        fresh: dict[str, tuple] = {}
        for dirpath, dirnames, filenames in os.walk(self.root):
            # Symlinks are not followed: one pointing outside the workdir would
            # have its target's CONTENTS pushed, bypassing off_limits entirely.
            keep_dirs = []
            for d in dirnames:
                full = os.path.join(dirpath, d)
                rel = P.rel_from_posix(self.root, full)
                if os.path.islink(full):
                    # Say so. Containment was always right, but silence here
                    # meant an agent could write into a symlinked directory,
                    # be told the write succeeded, and never learn that not
                    # one byte of it was going to the machine.
                    self.reports.append(
                        f"skipped {rel}/: a symbolic link, which is not "
                        f"followed -- its contents never reach the machine")
                    continue
                if ignore and ignore(rel + "/"):
                    continue
                keep_dirs.append(d)
            dirnames[:] = keep_dirs
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
                bad = P.check(rel, guest_root=self.guest_root,
                              caps=self.caps, is_dir=True)
                if bad is not None:
                    self.reports.append(f"skipped {rel}/: {bad}")
                    continue
                keep.append(name)
                rows[P.canonical(rel, caps=self.caps)] = S.Entry(
                    type=S.Type.DIR, display=rel)
            dirnames[:] = keep
            for name in filenames:
                full = os.path.join(dirpath, name)
                if os.path.islink(full):
                    self.reports.append(
                        f"skipped {P.rel_from_posix(self.root, full)}: a "
                        f"symbolic link, which is not followed -- it never "
                        f"reaches the machine")
                    continue
                if name.endswith(WORKING_SUFFIXES):
                    continue
                rel = P.rel_from_posix(self.root, full)
                if ignore and ignore(rel):
                    continue
                bad = P.check(rel, guest_root=self.guest_root, caps=self.caps)
                if bad is not None:
                    self.reports.append(f"skipped {rel}: {bad}")
                    continue
                key = P.canonical(rel, caps=self.caps)
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
                    st = os.stat(full)
                except OSError as exc:
                    rows[key] = S.Entry(type=S.Type.UNREADABLE, display=rel)
                    self.reports.append(f"unreadable {rel}: {exc}")
                    continue

                # The cache holds the CLASSIFICATION INPUTS -- both checksums
                # and what the content says on its own -- rather than a class
                # and one checksum. An attributes change can then re-decide a
                # path without re-reading it, and the cached value cannot be
                # stale with respect to a rule the file knows nothing about.
                hit = cache.get(key) if cache is not None else None
                if hit and hit[0] == st.st_size and hit[1] == st.st_mtime:
                    auto, craw, cnorm, size = hit[2], hit[3], hit[4], hit[5]
                else:
                    try:
                        data = open(full, "rb").read()
                    except OSError as exc:
                        rows[key] = S.Entry(type=S.Type.UNREADABLE, display=rel)
                        self.reports.append(f"unreadable {rel}: {exc}")
                        continue
                    auto = P.classify(data)
                    craw = zlib.crc32(data)
                    cnorm = zlib.crc32(P.normalise(data, P.CLASS_TEXT))
                    size = len(data)
                    fresh[key] = (st.st_size, st.st_mtime,
                                  auto, craw, cnorm, size)

                cls = auto
                if attrs is not None:
                    cls, refused = attrs.effective_class(rel, auto)
                    if refused and self.first_report(
                            key, f"attrs:{refused}", None, None):
                        self.reports.append(f"{rel}: {refused}")
                rows[key] = S.Entry(
                    type=S.Type.FILE, cls=cls, mtime=st.st_mtime,
                    crc=P.crc_for(cls, cnorm, craw),
                    crc_raw=craw, crc_norm=cnorm, dual=True, display=rel)

        # Only now, and only the entries whose read cannot have raced a save in
        # a tick that was still open. Gating the LOOKUP is the obvious half and
        # the wrong one: a read taken inside the window is correct for the pass
        # that took it and can be superseded a moment later at the same size,
        # so what must never happen is STORING it.
        if cache is not None:
            for key, val in fresh.items():
                probe = S.Entry(type=S.Type.FILE, mtime=val[1])
                if probe.cacheable:
                    cache[key] = val
            for gone in [k for k in cache if k not in rows]:
                del cache[gone]
        return rows

    # --- applying ----------------------------------------------------------

    async def push(self, rel: str, expect: S.Entry | None) -> bool:
        """Send one file. Returns True when the baseline may advance."""
        full = os.path.join(self.root, rel)
        data = open(full, "rb").read()
        if len(data) > MAX_FILE:
            self.reports.append(
                f"skipped {rel}: {len(data)} bytes exceeds the {MAX_FILE} cap; "
                f"one file must not hold the wire")
            return False
        # The tree's rules, not just the content's. `-text` turns off both the
        # conversion and the folded comparison; `eol=lf` keeps the folded
        # comparison and changes only what lands on the client -- which is the
        # whole point for a Makefile a cross toolchain has to read.
        cls = P.classify(data)
        eol = self.caps.eol
        if self.attrs is not None:
            cls, _ = self.attrs.effective_class(rel, cls)
            eol = self.attrs.eol_for(rel, eol)
        wire = P.to_guest(data, cls, self.caps, eol=eol)
        crc = zlib.crc32(P.normalise(data, cls))

        sid = self._next_id()
        fut = asyncio.get_running_loop().create_future()
        self.guest.sync_pending[sid] = S.Wait(kind="put", future=fut)
        rb = P.wire_encode(rel, acp=self.caps.acp)
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
                # Between chunks, not merely between files. A megabyte is 128
                # chunks and none of them suspends on its own.
                await self.bridge.wire_yield(self.guest)
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
        self.guest.sync_pending[sid] = S.Wait(kind="get", future=fut,
                                              data=bytearray())
        rb = P.wire_encode(rel, acp=self.caps.acp)
        try:
            await self.bridge._send(
                self.guest, Frame("SYNCGET", {"id": sid}, rb), stamp=False)
            status, crc, cls, wire = await asyncio.wait_for(fut, timeout=300)
        finally:
            self.guest.sync_pending.pop(sid, None)

        if status != "ok":
            self.reports.append(f"pull {rel} refused: {status}")
            return False

        # `cls` is the client's own reading of the bytes, and it is the right
        # thing to interpret the wire with and to verify the transfer against:
        # it is what the client checksummed. The tree's attributes decide what
        # we STORE and compare under, which can differ -- and when it says
        # byte-exact, the client's bytes are kept exactly as they arrived
        # rather than folded on the way in.
        want = cls
        if self.attrs is not None:
            want, _ = self.attrs.effective_class(rel, cls)
        data = wire if want == P.CLASS_EXACT else P.to_bridge(wire, cls)
        got = zlib.crc32(P.normalise(wire, cls))
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
        if want == cls and P.classify(data) != cls:
            # The guest's judgement and ours must agree on the bytes we now
            # hold, or the next pass CRCs different functions on each side and
            # the path never converges.
            self.reports.append(
                f"pull {rel}: the guest called it class {cls!r} and the same "
                f"bytes read as {P.classify(data)!r} here. Nothing written.")
            return False

        full = os.path.join(self.root, rel)
        os.makedirs(os.path.dirname(full) or ".", exist_ok=True)
        tmp = full + WORKING_SUFFIXES[0]
        with open(tmp, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, full)

        self.keep_ancestor(rel, data)
        craw = zlib.crc32(data)
        cnorm = zlib.crc32(P.normalise(data, P.CLASS_TEXT))
        self._pulled[key] = S.Entry(
            type=S.Type.FILE, cls=want,
            mtime=os.stat(full).st_mtime,
            crc=P.crc_for(want, cnorm, craw),
            crc_raw=craw, crc_norm=cnorm, dual=True, display=rel)
        return True

    async def mkdir(self, rel: str) -> bool:
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
        self.guest.sync_pending[sid] = S.Wait(kind="mkdir", future=fut)
        rb = P.wire_encode(rel, acp=self.caps.acp)
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

    def stage_conflict(self, rel: str) -> str:
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
        if os.path.isdir(src):
            # A type change: the guest put a file where we hold a directory.
            # The whole subtree is the thing being protected -- row 3 preserves
            # descendants, and copying only the leaf would lose every file
            # under it. `open(src, "rb")` used to run here and raised
            # IsADirectoryError, which the caller does not catch, so ONE such
            # collision aborted the entire pass -- every other path with it,
            # on every pass, until someone renamed the file by hand.
            shutil.copytree(src, mine, symlinks=True)
        elif os.path.exists(src):
            with open(src, "rb") as fh:
                data = fh.read()
            with open(mine, "wb") as fh:
                fh.write(data)
        anc = os.path.join(self.base_dir, rel)
        if os.path.isfile(anc):
            with open(anc, "rb") as a, open(os.path.join(d, f"base.{n}"), "wb") as b:
                b.write(a.read())
        return mine

    def _apply_attrs(self, rows: dict, usable: bool = True) -> dict:
        """Re-read the client's manifest rows under the tree's attributes.

        The client classifies from content, which is the only thing it can see.
        `.gitattributes` lives here, so the override is applied here -- to BOTH
        sides, or the two are compared under different rules and the path never
        converges. This is why a v2 manifest carries both checksums: re-reading
        a row is free, asking the client to scan again under a new rule is not.
        """
        if self.attrs is None or not len(self.attrs):
            return rows
        out = {}
        for key, row in rows.items():
            if row.type is not S.Type.FILE:
                out[key] = row
                continue
            want, refused = self.attrs.effective_class(
                row.display or key, row.cls)
            if want == row.cls:
                out[key] = row
            elif not usable or not row.dual:
                # The client sent one checksum for its own classification and
                # the other cannot be derived -- a pure-LF file has them equal,
                # so equality proves nothing. Honouring the override here would
                # compare a normalised checksum against a raw one forever.
                out[key] = row
                if self.first_report(key, "attrs:v1-client", None, None):
                    self.reports.append(
                        f"{row.display or key}: the attributes ask for "
                        f"{want!r} but this client sends a v1 manifest, which "
                        f"carries only its own checksum. Left as {row.cls!r}.")
            else:
                out[key] = row.as_class(want)
            if refused and self.first_report(
                    key, f"attrs-guest:{refused}", None, None):
                self.reports.append(f"{row.display or key}: {refused}")
        return out

    def first_report(self, key: str, reason: str, g, b) -> bool:
        """Is this situation new, or the same one we announced last pass?

        Fingerprinted on the reason AND the content of whichever side still
        holds the path, so "the user deleted main.c and you still have it"
        is said once -- but said again if one of them then changes it, which
        is a different fact about a different file.
        """
        fp = "|".join([
            reason,
            f"g:{g.cls}:{g.crc}" if g is not None else "g:-",
            f"b:{b.cls}:{b.crc}" if b is not None else "b:-",
        ])
        if self.base.reported.get(key) == fp:
            return False
        self.base.reported[key] = fp
        return True

    def note(self, text: str) -> None:
        """Record a notice durably BEFORE acting on it.

        A crash between the baseline advance and the notification leaves the
        agent's work in conflict/ with nobody told, and the next pass sees
        agreement and says nothing.
        """
        self.base.pending_notice.append(text)
        self.base.save(self.state)

    def notices_delivered(self, texts: list) -> None:
        """Called once the caller has actually put them on the wire.

        The queue is PENDING, not a log. Nothing cleared it, so every notice
        ever raised was re-delivered on every pass -- the user would have seen
        the same conflict message every thirty seconds, which is worse than not
        reporting it, because it buries the next real one.

        Cleared by value rather than by emptying the list: a pass can raise a
        new notice while the previous batch is being written, and truncating
        would drop it with nobody told, which is the exact failure `note` is
        durable to prevent.
        """
        if not texts:
            return
        gone = list(texts)
        keep = []
        for t in self.base.pending_notice:
            if t in gone:
                gone.remove(t)
            else:
                keep.append(t)
        self.base.pending_notice = keep
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

    def _ignores(self, ignore):
        """The matcher for this pass, and the bookkeeping a change to it needs.

        A path that was syncing and is now ignored must FREEZE at its last
        agreed `(class, crc)`, not disappear. Adding one line to `.gitignore`
        is routine and can remove a thousand paths from the scan; read as
        "present last pass, absent now" that is a mass deletion, and the bridge
        would act on it.
        """
        if ignore is None:
            ignore = I.load(self.root)

        # The trailing slash is load-bearing: `build/` is a pattern about a
        # DIRECTORY and will not match the string "build". A row's type is the
        # only thing that knows which question to ask.
        def ig(row) -> bool:
            return ignore(row.display + ("/" if row.type is S.Type.DIR else ""))

        newly = [k for k, row in self.base.rows.items() if ig(row)]
        for key in newly:
            self.base.ignored[key] = self.base.rows.pop(key)
        if newly:
            self.reports.append(
                f"scope changed: {len(newly)} path(s) are now ignored and have "
                f"been frozen at their last agreed content, not deleted")

        # And the reverse. Un-ignoring resumes from the real ancestor rather
        # than looking like a first sync, which would conflict the whole set.
        back = [k for k, row in self.base.ignored.items() if not ig(row)]
        for key in back:
            self.base.rows[key] = self.base.ignored.pop(key)
        if back:
            self.reports.append(
                f"scope changed: {len(back)} path(s) are no longer ignored and "
                f"resume from their stored ancestor")
        return ignore

    async def run(self, ignore=None) -> dict:
        """One reconciliation, start to finish.

        Aborts happen before ANY row is touched, which is why the manifest is
        buffered until SYNCEND.
        """
        guest_rows = await self.scan_guest()
        ignore = await asyncio.to_thread(self._ignores, ignore)
        self.attrs = await asyncio.to_thread(A.load, self.root)
        # ONE decision, for both sides. Overriding here and not there is worse
        # than not overriding at all: the two sides would then be compared as
        # different classes, which is a permanent disagreement rather than a
        # missing feature.
        usable = self.caps.manifest >= 2
        guest_rows = self._apply_attrs(guest_rows, usable)
        bridge_rows = await asyncio.to_thread(
            self.scan_bridge, ignore, self.attrs if usable else None)

        decisions = S.order(S.diff(guest_rows, bridge_rows, self.base))
        #: Conflicts that CANNOT be applied -- a file on one side, a directory
        #: on the other. They never resolve, so the pruning below must not
        #: forget it announced them or they are announced again every pass.
        standing: set[str] = set()
        done = {"pushed": 0, "pulled": 0, "dirs": 0, "conflicts": 0,
                "reported": 0, "skipped": 0, "latched": 0, "unhandled": 0,
                # Conflicts ANNOUNCED this pass, which is not the same as
                # conflicts found. A conflict that cannot be applied -- a file
                # where we hold a directory -- never resolves, so every pass
                # finds it again; counting those towards the halt put the bar
                # back up every thirty seconds after the user had answered it.
                # See run_sync, which raises the halt on this and not on the
                # total.
                "new_conflicts": 0}

        # Which paths this pass is stopping the user for. The halt bar answers
        # these and nothing else -- see GuestSession.halt_paths.
        new_conflict_paths: set[str] = set()

        for dec in decisions:
            if dec.act is S.Act.NOTHING:
                continue
            g, b = guest_rows.get(dec.key), bridge_rows.get(dec.key)

            # Sync never holds the wire for more than one file. A pass is a
            # sequence of small operations that can be interleaved, never a
            # batch that runs to completion -- and while the user is looking at
            # a permission prompt, the next file waits for the pass after this
            # one. Each path commits independently, so stopping here leaves
            # some paths at the new agreement and the rest at the old one, and
            # both are valid resting states.
            if dec.act in _MOVES_BYTES and await self.bridge.wire_yield(
                    self.guest):
                self.reports.append(
                    "pass cut short: the user has a permission prompt open, "
                    "and sync does not compete with it for the link")
                break

            if dec.act is S.Act.SKIP:
                done["skipped"] += 1
                self.reports.append(f"{dec.key}: {dec.reason}")
                # An unreadable path is worth saying out loud; an ignored one
                # is the normal state of most of a tree and would bury it.
                #
                # This went nowhere at all before: `reports` is only read if
                # the agent thinks to call sync_status, so a file it had just
                # written and could not deliver produced silence. Through
                # first_report, so a name the machine can never open is
                # announced once rather than every thirty seconds.
                if dec.reason is S.UNREADABLE and self.first_report(
                        dec.key, dec.reason, g, b):
                    self.note(f"SYNC: {dec.key} -- {dec.reason}")
            elif dec.act is S.Act.REPORT:
                done["reported"] += 1
                self.reports.append(f"{dec.key}: {dec.reason}")
                # Announced ONCE per distinct situation. A divergence the
                # design cannot resolve -- the agent deleted a file the user
                # still has -- is a standing condition, and this pass runs
                # every thirty seconds. Telling the agent about the same
                # unresolvable thing forever trains it to ignore the channel
                # that also carries conflicts. The fingerprint includes the
                # content, so the user EDITING that file is news again.
                if self.first_report(dec.key, dec.reason, g, b):
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

                # Can the guest's side actually be applied here? Only if it
                # is a plain file AND we do not hold a directory at that path
                # -- a pull writes a file, and it cannot replace a directory
                # without a delete primitive we deliberately do not have.
                applicable = (
                    g is not None and g.type is S.Type.FILE
                    and (b is None or b.type is S.Type.FILE))

                # An unapplyable conflict never resolves, so nothing advances,
                # so the next pass decides it again. Staging it every time gave
                # a fresh mine.N -- and a fresh notice -- every thirty seconds
                # forever, which is the same "say it once" bug the standing
                # divergences had, wearing a type change as a disguise.
                if not applicable:
                    standing.add(dec.key)
                    if not self.first_report(dec.key, dec.reason, g, b):
                        done["latched"] += 1
                        continue

                # Past the gate above, so this one is genuinely news: either it
                # can be applied (and therefore resolves, and cannot recur), or
                # it cannot and first_report has just said so for the first
                # time. Either way it is worth stopping the user for.
                done["new_conflicts"] += 1
                new_conflict_paths.add(dec.key)
                mine = self.stage_conflict(rel)

                # A latched path REFUSES pushes. The agent's in-flight turn
                # cannot react to a conflict it has not been told about yet,
                # so "the agent stops" is enforced here rather than requested.
                self.base.latched[dec.key] = dec.reason
                done["latched"] += 1
                if applicable:
                    self.note(
                        f"SYNC CONFLICT {rel} -- {dec.reason}. The user's "
                        f"version is kept, here and on their machine. Yours is "
                        f"preserved at {mine}, with the ancestor both came "
                        f"from beside it. Merge the two: read the current "
                        f"file, then that copy, then the ancestor, and keep "
                        f"both changes where they do not contradict. The user "
                        f"has already confirmed they are finished with the "
                        f"file, so do not ask them again -- say what each side "
                        f"changed and what you kept.")
                else:
                    # Telling the agent to "merge the two" here would send it
                    # looking for a file that is a directory on one side. The
                    # situation needs a human decision about which shape the
                    # path should have, so say exactly that instead.
                    self.note(
                        f"SYNC CONFLICT {rel} -- {dec.reason}. One side holds "
                        f"a file and the other a directory, so nothing was "
                        f"applied and nothing was deleted. Your side is "
                        f"preserved at {mine}. Sync for this path is stopped "
                        f"until the user decides which shape it should have; "
                        f"every other path keeps syncing.")

                if applicable:
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
                    # A type change in either direction. There is no file to
                    # fetch, or we hold a directory where the machine holds a
                    # file and applying it would mean deleting that subtree.
                    # The honest outcome is the staged copy plus the report --
                    # said once, because nothing here will change it.
                    self.reports.append(
                        f"{rel}: conflict staged; a file on one side and a "
                        f"directory on the other, so nothing here was changed")
            elif dec.act is S.Act.PUSH:
                if dec.key in self.base.latched:
                    done["latched"] += 1
                    self.reports.append(f"{dec.key}: latched, push withheld")
                    continue
                rel = b.display or dec.key
                if await self.push(rel, g):
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
                if await self.mkdir(rel):
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

        # A path that resolved is no longer "already reported": if it diverges
        # again later, that is new and must be said again.
        live = {d.key for d in decisions
                if d.act in (S.Act.REPORT, S.Act.SKIP)} | standing
        for key in [k for k in self.base.reported if k not in live]:
            del self.base.reported[key]

        self.base.generation += 1
        # What the log should say. The full set goes to sync_status, which is
        # asked on demand; the log wants the delta, or a name that can never
        # be fixed prints a warning twice a minute until warnings stop being
        # read at all.
        # This pass ran, so whatever stopped the last one is over. Clearing it
        # is what makes the next abort news again -- and a recovery worth
        # saying, because "sync has stopped" with no matching "sync resumed"
        # leaves the user believing it is still down.
        if self.base.aborted:
            log.info("sync: recovered from %r", self.base.aborted)
            done["recovered"] = self.base.aborted
            self.note("SYNC: file synchronisation is working again. It had "
                      "stopped: " + self.base.aborted)
            self.base.aborted = ""

        was = set(self.base.reported_lines)
        done["new_reports"] = [r for r in self.reports if r not in was]
        self.base.reported_lines = list(self.reports)
        self.base.save(self.state)
        done["reports"] = list(self.reports)
        done["notices"] = list(self.base.pending_notice)
        done["conflict_paths"] = sorted(new_conflict_paths)
        return done
