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

from ..status import SyncStatus
from . import attrs as A
from . import caps as C
from . import ignores as I
from . import paths as P
from . import sync as S
from .reporter import Reporter
from .scanner import (
    WORKING_SUFFIXES,
    scan_bridge_tree,
)
from .synctransport import CHUNK, SyncTransport  # noqa: F401 (CHUNK re-export)

log = logging.getLogger("bridge.syncrun")

#: One file must never hold the wire for long. The guest has one sender thread
#: and one outbound queue, so a big transfer starves permission prompts and exec
#: output behind it.
#: The acts that actually put bytes on the wire, and therefore the ones that
#: have to yield first. A REPORT or a TOMBSTONE touches nothing and pausing
#: before one would only make a pass slower for no benefit.
_MOVES_BYTES = frozenset((S.Act.PUSH, S.Act.PULL, S.Act.MKDIR,
                          S.Act.CONFLICT))

MAX_FILE = 1024 * 1024

#: Default value for the guest's own RC_SYNC_MAX_FILES (win/sync/sync.h,
#: currently 2000).  Used as the fallback when the client does not declare
#: the maxfiles capability at handshake; declared clients carry their own
#: value in Caps.maxfiles.
SYNC_MAX_FILES = 2000


class SyncError(Exception):
    """A pass that must abort rather than act on what it received.

    `transient` marks the ones that are not news. A refusal is normally
    something a person has to fix -- a tree_id mismatch, a short manifest --
    and it is announced loudly because sync being dead for an hour while
    everyone believes it is working is the failure that machinery exists for.
    But `no_root` is the two sides mid-handover: the work directory has just
    changed and the other end has not applied it yet, which resolves itself
    within a second and happens on EVERY change. Announcing it produced a
    matched pair of "synchronisation has stopped" and "it is working again"
    notices every time, thirty-four of them in one session -- which teaches
    people to ignore the notice that matters.
    """

    def __init__(self, message: str, *, transient: bool = False) -> None:
        super().__init__(message)
        self.transient = transient


class Pass:
    """One reconciliation pass against one guest."""

    #: Class-level fallbacks. `__init__` always overwrites both on a real
    #: `Pass`, but `test_sync_wire.py` builds bare `Pass` subclasses that skip
    #: `__init__` to exercise `scan_bridge` in isolation, and those reach these
    #: directly -- verified live by `make test` before this was trimmed away.
    #: EMPTY, not a path. This used to read `C:\\WORK` -- a drive letter and a
    #: separator, in behaviour rather than a comment, and a live input to the
    #: MAX_PATH budget `P.check` measures. Nothing in production ever reads
    #: it (`run()` calls `scan_guest`, which sets the real root from SYNCEND,
    #: before anything is pushed), so naming one machine's convention here
    #: bought nothing and asserted something the bridge had not been told.
    guest_root = ""
    caps = C.Caps()

    def __init__(self, transport: SyncTransport, caps: C.Caps | None,
                root: str, state_dir: str):
        # 02.19: this used to be `Pass(bridge, guest, root, state_dir)` --
        # the entire Bridge and the entire GuestSession, of which four
        # things were ever used (bridge._send, bridge.wire_yield,
        # guest.sync.pending, guest.sync.seq) plus a fifth the audit's count
        # missed, guest.perms.pending. `transport` is those, wrapped by
        # bridge/files/synctransport.py's SyncTransport; `caps` is passed
        # explicitly rather than fished out of a guest that might not be one.
        self.transport = transport
        self.root = root
        # `state_dir` may name the FILE rather than the directory.
        #
        # One baseline per guest root (SyncService.base_path) means the caller is
        # the only thing that knows which file this pass belongs to, and
        # re-deriving `base.json` here silently ignored that -- the bridge
        # chose base-<root>.json, the pass loaded base.json, and every pass
        # was refused for an identity mismatch the bridge had already
        # resolved. Both spellings are accepted so the existing callers that
        # pass a directory keep working.
        self.state = (state_dir if str(state_dir).endswith(".json")
                      else os.path.join(state_dir, "base.json"))
        self.base = S.Baseline.load(self.state)
        #: "What has already been said, to whom" -- `first_report`/`note`/
        #: `notices_delivered`/`agent_notices_delivered` below are thin
        #: forwards onto this, moved out the same way `scan_bridge` was
        #: (docs/audit/02-bridge-python.md #4.2, #02.40). Built over the same
        #: `self.base` object, not a copy, so the `self.base.acp = keying`
        #: assignment a few lines down is visible to it too.
        self.reporter = Reporter(self.base, self.state)

        # The class-level `caps = C.Caps()` fallback above is for the
        # bare-subclass tests that skip __init__ entirely and never call
        # this at all.
        self.caps = caps if caps is not None else C.Caps()

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
        #: Empty rather than a guess: an invented root is a length budget
        #: measured against a path this machine may not have.
        self.guest_root = ""
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
        result = await self.transport.scan()
        status, files = result.status, result.files
        tree_id, root, rows = result.tree_id, result.root, result.rows

        if status == SyncStatus.TOO_MANY:
            # 03.24: the limit itself never crosses the wire -- SYNCEND only
            # ever says status=too_many, because the scan that would have
            # counted past it is the same scan that aborted instead of
            # finishing. Without a number here the message only ever said
            # "too_many", and nobody reading it -- user or agent -- could tell
            # how many files is too many, or by how much this tree misses it.
            raise SyncError(
                f"guest scan returned too_many: the tree under the sync root "
                f"has more than the machine's {self.caps.maxfiles}-file scan "
                f"limit. Narrow the sync root, or exclude more of the tree "
                f"with .gitignore, so the scan stays under that count.")
        if status != SyncStatus.OK:
            # A truncated manifest is indistinguishable from a mass deletion,
            # and the natural implementation acts on what arrived -- pushing the
            # bridge's whole tree at a machine that just said it had too many
            # files.
            # .value, not the bare member: f"{SyncStatus.X}" is
            # "SyncStatus.X" (a bare (str, Enum)'s __format__ defers to
            # Enum.__str__, not the plain string content), confirmed against
            # this interpreter -- server.py's _exec_result hits the same
            # thing and already spells it out this way for ExecStatus.
            raise SyncError(
                f"guest scan returned {status.value}",
                # The machine has no root right now, which during a work
                # directory change is true for about a second and is nobody's
                # fault. run_sync retries rather than announcing it; it only
                # becomes news if it keeps happening.
                transient=status is SyncStatus.NO_ROOT)
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
        if not root:
            raise SyncError(
                "guest SYNCEND did not report a root= header. Refusing: the "
                "MAX_PATH budget and tree identity require the root to be known "
                "in full (05.6).")
        if self.base.guest_root and root != self.base.guest_root:
            raise SyncError(
                f"this baseline was built against {self.base.guest_root!r} "
                f"and the machine now reports {root!r}. Refusing: the stored "
                f"paths are relative to the root, so continuing would compare "
                f"one tree against another tree's history.")
        if not self.base.guest_root:
            self.base.guest_root = root

        self.guest_root = root
        return rows

    # --- the bridge's half -------------------------------------------------

    def scan_bridge(self, ignore=None, attrs=None) -> dict[str, S.Entry]:
        """Thin adapter over `scanner.scan_bridge_tree` (docs/audit/
        02-bridge-python.md 02.40): supplies this Pass's root, caps and
        guest_root, the CRC cache from `self.base` (defensive `getattr`
        because `test_sync_wire.py` builds bare `Pass` subclasses that skip
        `__init__` to exercise this in isolation -- see the class-level
        `guest_root`/`caps` fallbacks above), and routes reports and the
        attrs-refused fingerprint back through `self.reports`/
        `self.first_report` exactly as before.
        """
        base = getattr(self, "base", None)
        cache = base.scan_cache if base is not None else None
        return scan_bridge_tree(
            self.root, ignore=ignore, attrs=attrs, caps=self.caps,
            guest_root=self.guest_root, cache=cache,
            report=self.reports.append,
            first_report=lambda key, reason: self.reporter.first_report(
                key, reason, None, None))

    # --- applying ----------------------------------------------------------

    def _too_long_detail(self, status: SyncStatus, rel: str, *,
                        is_dir: bool = False) -> str:
        """03.41: `too_long` named no limit in a push/mkdir refusal, unlike
        exec's own too_long (`_exec_result`), which tells the model the exact
        character count. The number was always available here -- it is the
        same `P.check` the pre-push scan already ran on this path -- so this
        re-runs it purely to explain a refusal the guest has already made,
        never to decide anything.

        Also catches the two rules disagreeing: if the bridge's own check
        finds nothing wrong with a path the guest just refused as too_long,
        that is itself worth saying, because it means the two path-length
        rules have drifted apart.
        """
        if status != SyncStatus.TOO_LONG:
            return ""
        bad = P.check(rel, guest_root=self.guest_root, caps=self.caps,
                      is_dir=is_dir)
        if bad is not None:
            return f" ({bad})"
        return (" (the machine's own length rule disagrees with the "
                "bridge's -- this path passed the pre-push check here)")

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
        rb = P.wire_encode(rel, acp=self.caps.acp)

        result = await self.transport.put(
            rb, wire, crc=crc, cls=cls,
            # The precondition. The guest re-reads and refuses if the world
            # moved -- which turns a silent overwrite into a conflict the
            # next pass resolves correctly.
            if_crc=str(expect.crc) if expect else "absent")
        status, ack_crc = result.status, result.crc

        if status != SyncStatus.OK:
            self.reports.append(f"push {rel} refused: {status.value}"
                                + self._too_long_detail(status, rel))
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
        rb = P.wire_encode(rel, acp=self.caps.acp)
        result = await self.transport.get(rb)
        status, crc, cls, wire = (result.status, result.crc, result.cls,
                                  result.data)

        if status != SyncStatus.OK:
            self.reports.append(f"pull {rel} refused: {status.value}")
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
        `no_parent` (03.45; this used to read `readonly`, back when do_put
        answered every open failure that way and the distinction did not
        exist on the wire yet), which reads as a permissions problem if
        nothing here says otherwise.
        """
        rb = P.wire_encode(rel, acp=self.caps.acp)
        result = await self.transport.mkdir(rb)
        status = result.status

        if status in (SyncStatus.OK, SyncStatus.EXISTS):
            return True
        self.reports.append(f"mkdir {rel} refused: {status.value}"
                            + self._too_long_detail(status, rel, is_dir=True))
        if status == SyncStatus.NO_PARENT:
            # 03.45: the guest already tells us this is specifically a
            # missing-PARENT problem, not a permissions one -- distinct from
            # `bad_path`, which blames the name (win/sync/syncrpc.c). Saying
            # so plainly means the agent stops looking for a permissions fix
            # that does not exist.
            self.note(f"SYNC: could not create the directory {rel} on the "
                      f"machine -- its own parent directory does not exist "
                      f"there. Nothing under {rel} can be copied there until "
                      f"that is resolved.")
        else:
            self.note(f"SYNC: could not create the directory {rel} on the "
                      f"machine ({status.value}), so nothing under it can be "
                      f"copied there.")
        return False

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
                if self.reporter.first_report(key, "attrs:v1-client", None, None):
                    self.reports.append(
                        f"{row.display or key}: the attributes ask for "
                        f"{want!r} but this client sends a v1 manifest, which "
                        f"carries only its own checksum. Left as {row.cls!r}.")
            else:
                out[key] = row.as_class(want)
            if refused and self.reporter.first_report(
                    key, f"attrs-guest:{refused}", None, None):
                self.reports.append(f"{row.display or key}: {refused}")
        return out

    # --- reporting -----------------------------------------------------------
    #
    # first_report/note/notices_delivered/agent_notices_delivered are thin
    # forwards onto self.reporter (bridge/files/reporter.py, docs/audit/
    # 02-bridge-python.md #4.2/#02.40) -- kept as same-named Pass methods so
    # `run()`'s many internal callers and test_sync_wire.py's direct calls
    # (`p.note(...)`, `p.notices_delivered(...)`) did not have to change
    # shape, the same pattern `scan_bridge` above already follows for
    # `scanner.scan_bridge_tree`.


    def note(self, text: str) -> None:
        self.reporter.note(text)

    def notices_delivered(self, texts: list) -> None:
        self.reporter.notices_delivered(texts)


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

    #: How many regressive rows in one pass before it stops looking like work
    #: and starts looking like a restored backup, as an absolute floor AND as
    #: a share of the tracked tree. Both, because either alone is wrong: a
    #: fraction misfires on a four-file tree where one revert is 25%, and a
    #: flat count misfires on a tree of three thousand where twelve reverts is
    #: an ordinary afternoon.
    REVERT_FLOOR = 5
    REVERT_SHARE = 0.25

    def _refuse_aggregate_revert(self, decisions) -> None:
        """Stop a pass that looks like the guest's tree was restored from a
        backup, rather than applying it one plausible row at a time.

        THE ROWS ARE INDIVIDUALLY CORRECT, which is the whole difficulty. A
        file the guest holds at its pre-push content is a reverted push; a
        file the guest no longer has is a deletion. Both are right. Fifty of
        them arriving together is not fifty edits, it is one event, and the
        per-row handling would work through it faithfully -- pulling the old
        contents over the agent's work and reporting the rest as deletions --
        with every individual decision defensible and the outcome wrong.

        This is the last silent-loss path `BACKLOG.md` acknowledged, and it is
        deliberately a REFUSAL rather than a repair: refusing touches neither
        side, so nothing is lost while a person decides. `sync_resolve` with
        `accept-tree` is how they say it really was intended.
        """
        if self.base.accept_tree:
            # Consumed here, not on a later success: the point of the flag is
            # to get PAST this check once, and leaving it set would disarm the
            # guard for every pass afterwards.
            self.base.accept_tree = False
            log.info("sync: aggregate-revert check waived for this pass")
            return
        tracked = len(self.base.rows)
        if not tracked:
            return              # a first sync has nothing to regress from
        reverted = [d for d in decisions if d.reason == S.REVERTED_PUSH]
        regressive = reverted + [d for d in decisions
                                 if d.reason in (S.GONE_ON_GUEST, "deleted on the guest")]
        if (len(regressive) < self.REVERT_FLOOR
                or len(regressive) < tracked * self.REVERT_SHARE):
            return
        # AT LEAST ONE PATH MUST HAVE GONE BACKWARDS IN CONTENT, not merely
        # vanished, and this condition is what stops the check becoming a trap
        # rather than a guard.
        #
        # `REPORT` does not advance the baseline -- it counts and says so --
        # so a row that vanished from the machine is re-decided identically on
        # every pass, forever. Without this clause, deleting a tenth of a small
        # tree (`del *.tmp`, a cleared build directory) would refuse the pass,
        # and would refuse it again thirty seconds later, and every thirty
        # seconds after that, with `accept-tree` buying exactly one pass each
        # time. A guard that must be answered forever is worse than no guard:
        # it teaches the answer reflexively, and the one time it matters it
        # gets the same reflex.
        #
        # Vanishing alone is deletion, which is already handled and already
        # harmless -- nothing is applied and nothing is lost. What distinguishes
        # a RESTORED tree is content coming back to an older state, and that is
        # what must be present before the whole pass is stopped.
        if not reverted:
            return
        sample = ", ".join(sorted(d.key for d in regressive)[:5])
        raise SyncError(
            f"{len(regressive)} of {tracked} tracked paths went backwards in "
            f"one pass -- reverted to the content a push replaced, or gone "
            f"from the machine while still held here ({sample}"
            f"{', ...' if len(regressive) > 5 else ''}). That is what a tree "
            f"restored from a backup looks like, and applying it row by row "
            f"would pull the old contents over work already done. Nothing has "
            f"been changed on either side. If the machine's tree is genuinely "
            f"meant to be this, call sync_resolve with action=accept-tree and "
            f"the next pass will proceed.")

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
        self._refuse_aggregate_revert(decisions)
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
        # these and nothing else -- see SyncCoordinator.halt_paths
        # (bridge/synccoord.py).
        new_conflict_paths: set[str] = set()

        #: 03.45: directories whose MKDIR failed THIS pass, as key-prefixes
        #: ("dir/"). `order()` puts a directory's own MKDIR ahead of anything
        #: under it (ranked first, then by depth), so by the time a PUSH or a
        #: nested MKDIR under a failed one is reached, the failure is already
        #: known. Without this, one refused directory turned into one refusal
        #: per file inside it -- every one of them individually reported as
        #: `no_parent` against the guest, every single pass, for a cause the
        #: first line already explained.
        failed_dirs: set[str] = set()

        for dec in decisions:
            if dec.act is S.Act.NOTHING:
                continue
            g, b = guest_rows.get(dec.key), bridge_rows.get(dec.key)

            if dec.act in (S.Act.PUSH, S.Act.MKDIR):
                parent = next((d for d in failed_dirs
                              if dec.key.startswith(d)), None)
                if parent is not None:
                    done["skipped"] += 1
                    self.reports.append(
                        f"{dec.key}: withheld -- its directory "
                        f"{parent.rstrip('/')} could not be created on the "
                        f"machine this pass")
                    continue

            # Sync never holds the wire for more than one file. A pass is a
            # sequence of small operations that can be interleaved, never a
            # batch that runs to completion -- and while the user is looking at
            # a permission prompt, the next file waits for the pass after this
            # one. Each path commits independently, so stopping here leaves
            # some paths at the new agreement and the rest at the old one, and
            # both are valid resting states.
            #
            # The pause-for-a-prompt decision is sync's own policy (04.12):
            # wire_yield only yields the wire now, so the check against
            # perm_pending happens here rather than inside it -- a future
            # bulk sender that is not sync (an export) has no reason to
            # inherit this behaviour just by calling the same primitive.
            if dec.act in _MOVES_BYTES:
                await self.transport.yield_()
                if self.transport.has_pending_prompt:
                    self.reports.append(
                        "pass cut short: the user has a permission prompt "
                        "open, and sync does not compete with it for the "
                        "link")
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
                if dec.reason is S.UNREADABLE and self.reporter.first_report(
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
                if self.reporter.first_report(dec.key, dec.reason, g, b):
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
                    if not self.reporter.first_report(dec.key, dec.reason, g, b):
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
                else:
                    failed_dirs.add(dec.key + "/")
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
                target = os.path.join(self.root, dec.display or (b.display if b else dec.key))
                if os.path.lexists(target):
                    try:
                        if os.path.isdir(target) and not os.path.islink(target):
                            os.rmdir(target)
                        else:
                            os.remove(target)
                    except OSError as exc:
                        log.warning("could not remove tombstoned %s: %s", target, exc)
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
        if self.base.aborted or self.base.announced:
            log.info("sync: recovered from %r", self.base.aborted)
            self.base.announced = []
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
        done["agent_notices"] = list(self.base.pending_notice_agent)
        done["conflict_paths"] = sorted(new_conflict_paths)
        return done
