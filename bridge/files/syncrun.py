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
import pathlib
import shutil
import zlib
from typing import TypedDict

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

#: The acts that actually put bytes on the wire, and therefore the ones that
#: have to yield first. A REPORT or a TOMBSTONE touches nothing and pausing
#: before one would only make a pass slower for no benefit.
_MOVES_BYTES = frozenset((S.Act.PUSH, S.Act.PULL, S.Act.MKDIR,
                          S.Act.CONFLICT))

MAX_FILE = 1024 * 1024

#: Fallback for a client that doesn't declare `maxfiles` at handshake,
#: mirroring the guest's own RC_SYNC_MAX_FILES default (win/sync/sync.h).
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


class PassResult(TypedDict, total=False):
    """The bookkeeping `Pass.run()` returns for one reconciliation pass.

    A TypedDict, not a dataclass: `SyncService.run_sync`
    (bridge/services/sync.py) treats this as an open-ended dict --
    `.get()` with a per-key default, `.items()` for the log line, and
    other shapes (`{"retry": ...}`, `{"error": ...}`) for its own abort
    paths. `total=False` because most keys are only added once the pass
    reaches that point (e.g. `recovered` only on a recovery). This is
    what replaces the counters and the later str/list additions sharing
    one `dict[str, int]` inferred from the literal's first values.
    """

    pushed: int
    pulled: int
    dirs: int
    conflicts: int
    reported: int
    skipped: int
    latched: int
    unhandled: int
    new_conflicts: int
    recovered: str
    new_reports: list[str]
    reports: list[str]
    notices: list[str]
    agent_notices: list[str]
    conflict_paths: list[str]


class Pass:
    """One reconciliation pass against one guest."""

    #: Class-level fallbacks for `test_sync_wire.py`'s bare `Pass` subclasses,
    #: which skip `__init__` to exercise `scan_bridge` in isolation.
    #: `guest_root` stays EMPTY rather than a placeholder path: it is a live
    #: input to the MAX_PATH budget `P.check` measures, and an invented root
    #: would assert something the machine had not actually told us.
    guest_root = ""
    caps = C.Caps()

    def __init__(self, transport: SyncTransport, caps: C.Caps | None,
                root: str, state_dir: str):
        # `transport` wraps the one guest connection `Pass` needs (see
        # synctransport.py); `caps` is passed explicitly rather than fished
        # out of a guest that might not be one.
        self.transport = transport
        self.root = root
        # `state_dir` may name the FILE rather than the directory: re-deriving
        # a name from a directory here risked disagreeing with the one
        # `SyncService.base_path` (one baseline per guest root) already chose.
        self.state = (state_dir if str(state_dir).endswith(".json")
                      else os.path.join(state_dir, "base.json"))
        self.base = S.Baseline.load(self.state)
        #: "What has already been said, to whom" (reporter.py) -- over the
        #: same `self.base`, not a copy, so `self.base.acp =` below is visible too.
        self.reporter = Reporter(self.base, self.state)

        self.caps = caps if caps is not None else C.Caps()

        # A codepage or case-rule CHANGE re-keys the whole baseline, since
        # `canonical` folds through both -- every row would then miss, read
        # as absent on both sides, and conflict the entire tree in one pass.
        # Refuse, like a tree_id mismatch, and let a human decide.
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
        #: Where the guest keeps the mirror, set for real from SYNCEND (see
        #: the class-level fallback above); empty until then rather than a
        #: guess, since scan_bridge always runs after scan_guest.
        self.guest_root = ""
        #: Rows established by a pull's ack, keyed by canonical path. The
        #: baseline takes its value from HERE, never from a re-scan of our
        #: own tree, which could pick up a write the agent made mid-transfer.
        self._pulled: dict[str, S.Entry] = {}
        #: The tree's `.gitattributes`/`.gastonattributes`. None until `run`
        #: loads it, so calling scan_bridge directly gets content-only rules.
        self.attrs: A.Attrs | None = None
        #: Inside the agent's reach so it can read a conflict and its ancestor
        #: without a new permission path -- unlike the baseline itself, which
        #: lives outside cwd because off_limits does not cover Bash.
        self._conflict_dir = P.conflict_dir(root)

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
            # The limit itself never crosses the wire -- SYNCEND only ever
            # says status=too_many -- so it is added here, or nobody reading
            # the message could tell how many files is too many (03.24).
            raise SyncError(
                f"guest scan returned too_many: the tree under the sync root "
                f"has more than the machine's {self.caps.maxfiles}-file scan "
                f"limit. Narrow the sync root, or exclude more of the tree "
                f"with .gitignore, so the scan stays under that count.")
        if status != SyncStatus.OK:
            # A truncated manifest is indistinguishable from a mass deletion,
            # so this must abort rather than act on what arrived. `.value`,
            # not the bare member: a bare (str, Enum)'s __format__ defers to
            # Enum.__str__, so f"{status}" would read "SyncStatus.X".
            raise SyncError(
                f"guest scan returned {status.value}",
                # `no_root`: the two sides are mid-handover (a work directory
                # change), resolves within a second, and is nobody's fault --
                # `transient` lets run_sync retry instead of announcing it.
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

        # Compared in full, not just via tree_id, which truncates the root
        # (see Baseline.guest_root in sync.py) -- a changed root would
        # otherwise silently reconcile one tree against another's history.
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
        exec's own too_long (`exec_result` in guest_exec.py), which tells the
        model the exact character count. The number was always available
        here -- it is the same `P.check` the pre-push scan already ran on
        this path -- so this re-runs it purely to explain a refusal the
        guest has already made, never to decide anything.

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
        data = pathlib.Path(full).read_bytes()
        if len(data) > MAX_FILE:
            self.reports.append(
                f"skipped {rel}: {len(data)} bytes exceeds the {MAX_FILE} cap; "
                f"one file must not hold the wire")
            return False
        # The tree's rules, not just the content's: `-text` turns off both
        # conversion and folded comparison, `eol=lf` keeps the comparison and
        # changes only what lands on the client (the Makefile case).
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
            # The precondition: the guest re-reads and refuses if the world
            # moved, turning a silent overwrite into a conflict.
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

        # `cls` is what the client checksummed, so it is the right reading to
        # verify the transfer against. The tree's attributes decide what we
        # STORE and compare under, which can differ.
        want = cls
        if self.attrs is not None:
            want, _ = self.attrs.effective_class(rel, cls)
        data = wire if want == P.CLASS_EXACT else P.to_bridge(wire, cls)
        got = zlib.crc32(P.normalise(wire, cls))
        if got != crc:
            # Invariant 2: agree or report, never retry -- retrying is how a
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
            # hold, or the next pass CRCs different functions forever.
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
            # A missing-PARENT problem, not a permissions one (distinct from
            # `bad_path`, which blames the name) -- said plainly so the agent
            # stops looking for a permissions fix that does not exist.
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
            # The whole subtree is what's being protected, since row 3
            # preserves descendants and copying only the leaf would lose them.
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
                # One checksum, computed for the client's own classification;
                # the other can't be derived, so honouring the override would
                # compare normalised to raw forever.
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
    # `note`/`notices_delivered` are thin forwards onto self.reporter (see
    # reporter.py); `first_report`/`agent_notices_delivered` have no such
    # wrapper, and callers use `self.reporter.first_report(...)` directly.


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

        # The trailing slash is load-bearing: `build/` matches a DIRECTORY,
        # not the string "build", and a row's type is what knows which to ask.
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

    #: How many regressive rows before a pass looks like a restored backup
    #: rather than work -- a floor AND a share, since either alone misfires
    #: (a fraction on a four-file tree; a flat count on a tree of thousands).
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
            # Consumed here, not on later success, or it would disarm the
            # guard for every pass afterwards instead of just this one.
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
        # At least one path must have gone BACKWARDS in content, not merely
        # vanished, or this becomes a trap rather than a guard: `REPORT` never
        # advances the baseline, so an ordinary bulk deletion (`del *.tmp`)
        # would re-trip it every pass forever, with `accept-tree` buying only
        # one pass at a time -- training a reflexive answer for the one time
        # it matters. Vanishing alone is deletion, already handled harmlessly;
        # a RESTORED tree specifically brings content back to an older state.
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
        # ONE decision, for both sides -- overriding here and not there would
        # compare them as different classes, a permanent disagreement.
        usable = self.caps.manifest >= 2
        guest_rows = self._apply_attrs(guest_rows, usable)
        bridge_rows = await asyncio.to_thread(
            self.scan_bridge, ignore, self.attrs if usable else None)

        decisions = S.order(S.diff(guest_rows, bridge_rows, self.base))
        self._refuse_aggregate_revert(decisions)
        #: Conflicts that CANNOT be applied (file vs directory). They never
        #: resolve, so the pruning below must not forget it announced them.
        standing: set[str] = set()
        done: PassResult = {
            "pushed": 0, "pulled": 0, "dirs": 0, "conflicts": 0,
            "reported": 0, "skipped": 0, "latched": 0, "unhandled": 0,
            # ANNOUNCED this pass, not the same as found: an unapplyable
            # conflict recurs every pass, so counting it toward the halt would
            # raise the bar again right after the user had answered it.
            "new_conflicts": 0}

        #: Which paths this pass is stopping the user for -- what the halt bar
        #: answers (see SyncCoordinator.halt_paths, bridge/synccoord.py).
        new_conflict_paths: set[str] = set()

        #: Directories whose MKDIR failed THIS pass, as key-prefixes ("dir/").
        #: `order()` ranks a directory's MKDIR ahead of anything under it, so
        #: the failure is known by the time a PUSH reaches it -- without this,
        #: one refused directory became one `no_parent` refusal per file (03.45).
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

            # Sync never holds the wire for more than one file: each path
            # commits independently, so pausing for an open permission prompt
            # leaves some paths at the new agreement and the rest at the old,
            # both valid resting states. Sync's own policy (04.12), not
            # `wire_yield`'s -- a future bulk sender that isn't sync shouldn't
            # inherit it just by calling the same primitive.
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
                # Worth telling the agent (`note`), not just `reports` (which
                # only surfaces on sync_status) -- through first_report, so
                # it's announced once, not every thirty seconds.
                if dec.reason is S.UNREADABLE and self.reporter.first_report(
                        dec.key, dec.reason, g, b):
                    self.note(f"SYNC: {dec.key} -- {dec.reason}")
            elif dec.act is S.Act.REPORT:
                done["reported"] += 1
                self.reports.append(f"{dec.key}: {dec.reason}")
                # Announced ONCE per distinct situation, or an unresolvable
                # standing condition (found every pass) trains the agent to
                # ignore the channel that also carries conflicts. The
                # fingerprint includes content, so an edit is news again.
                if self.reporter.first_report(dec.key, dec.reason, g, b):
                    self.note(f"SYNC: {dec.key} -- {dec.reason}")
            elif dec.act is S.Act.CONFLICT:
                done["conflicts"] += 1
                rel = (b.display if b else dec.key)

                # A conflict is COPY -> PULL -> ADVANCE -> NOTIFY, in that
                # order: copy first since it's what's being protected, the
                # notice durably before anything moves so a crash mid-
                # resolution can't strand the agent's work unannounced, and
                # the baseline LAST from the pull's ack (Invariant 1) -- earlier,
                # a crash would invert direction and overwrite the user's file.

                # Applicable only if the guest's side is a plain file and we
                # don't hold a directory there -- a pull can't replace a
                # directory without a delete primitive we deliberately lack.
                applicable = (
                    g is not None and g.type is S.Type.FILE
                    and (b is None or b.type is S.Type.FILE))

                # Unapplyable never resolves, so it recurs every pass; gate on
                # first_report so it's staged and announced once, not every
                # thirty seconds (the same bug standing divergences have).
                if not applicable:
                    standing.add(dec.key)
                    if not self.reporter.first_report(dec.key, dec.reason, g, b):
                        done["latched"] += 1
                        continue

                # Past the gate, so this is genuinely news either way: worth
                # stopping the user for.
                done["new_conflicts"] += 1
                new_conflict_paths.add(dec.key)
                mine = self.stage_conflict(rel)

                # Refuses pushes: the agent's in-flight turn can't react to a
                # conflict it hasn't been told about yet.
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
                    # "Merge the two" would send the agent looking for a file
                    # that's a directory on one side; this needs a human
                    # decision about shape instead.
                    self.note(
                        f"SYNC CONFLICT {rel} -- {dec.reason}. One side holds "
                        f"a file and the other a directory, so nothing was "
                        f"applied and nothing was deleted. Your side is "
                        f"preserved at {mine}. Sync for this path is stopped "
                        f"until the user decides which shape it should have; "
                        f"every other path keeps syncing.")

                if applicable:
                    assert g is not None  # true whenever applicable is
                    if await self.pull(dec.key, g.display or dec.key, b):
                        done["pulled"] += 1
                        self.base.rows[dec.key] = self._pulled[dec.key]
                    else:
                        # Fetch failed, so the baseline must NOT advance -- it
                        # would claim an agreement that doesn't exist. The
                        # latch holds and the next pass tries again.
                        self.reports.append(
                            f"{rel}: conflict staged, but the machine's version "
                            f"could not be fetched; nothing here was changed")
                else:
                    # A type change: no file to fetch, or applying it would
                    # mean deleting a subtree we hold as a directory. The
                    # staged copy plus this report is the honest outcome.
                    self.reports.append(
                        f"{rel}: conflict staged; a file on one side and a "
                        f"directory on the other, so nothing here was changed")
            elif dec.act is S.Act.PUSH:
                if dec.key in self.base.latched:
                    done["latched"] += 1
                    self.reports.append(f"{dec.key}: latched, push withheld")
                    continue
                assert b is not None  # PUSH only decided when the bridge holds the path
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
                    assert b is not None  # MKDIR only decided for a bridge-side dir
                    self.base.rows[dec.key] = b
                else:
                    failed_dirs.add(dec.key + "/")
            elif dec.act is S.Act.ADOPT:
                # Adopting a guest-only directory means CREATING it here too,
                # or the row claims agreement while the bridge has nothing
                # there -- an empty directory nobody notices missing until a
                # build fails (files inside arrive via `pull`'s own parents).
                if g is not None and g.type is S.Type.DIR and b is None:
                    try:
                        os.makedirs(os.path.join(self.root, g.display),
                                    exist_ok=True)
                        done["dirs"] += 1
                    except OSError as exc:
                        self.reports.append(f"mkdir {g.display} here: {exc}")
                        continue
                adopted = g if g is not None else b  # ADOPT needs at least one side
                assert adopted is not None
                self.base.rows[dec.key] = adopted
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
            elif dec.act is S.Act.RMDIR_HERE:
                target = os.path.join(self.root, dec.display or dec.key)
                try:
                    if os.path.isdir(target) and not os.path.islink(target):
                        os.rmdir(target)
                    self.base.rows.pop(dec.key, None)
                    done["dirs"] += 1
                except OSError:
                    # Not empty: something here the machine never had. Keep
                    # it, and say so -- the old report, now only when true.
                    reason = "directory deleted on the machine, kept here: it " \
                             "holds files that are only here"
                    done["reported"] += 1
                    standing.add(dec.key)
                    self.reports.append(f"{dec.key}: {reason}")
                    if self.reporter.first_report(dec.key, reason, g, b):
                        self.note(f"SYNC: {dec.key} -- {reason}")
            elif dec.act is S.Act.CLEAR_TOMBSTONE:
                self.base.tombs.pop(dec.key, None)
                cleared = g if g is not None else b  # both sides agree; either works
                assert cleared is not None
                self.base.rows[dec.key] = cleared
            else:
                # An action with no branch must never fall off the end of
                # this chain and vanish silently -- that is how MKDIR was
                # lost once, with no error anywhere to point at it.
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
        # This pass ran, so whatever stopped the last one is over. Clearing it
        # makes the next abort news again, and a recovery worth saying -- or
        # the user goes on believing sync is still down.
        if self.base.aborted or self.base.announced:
            log.info("sync: recovered from %r", self.base.aborted)
            self.base.announced = []
            done["recovered"] = self.base.aborted
            self.note("SYNC: file synchronisation is working again. It had "
                      "stopped: " + self.base.aborted)
            self.base.aborted = ""

        # `sync_status` wants the full standing set; the log wants only the
        # delta, or an unfixable one repeats forever.
        was = set(self.base.reported_lines)
        done["new_reports"] = [r for r in self.reports if r not in was]
        self.base.reported_lines = list(self.reports)
        self.base.save(self.state)
        done["reports"] = list(self.reports)
        done["notices"] = list(self.base.pending_notice)
        done["agent_notices"] = list(self.base.pending_notice_agent)
        done["conflict_paths"] = sorted(new_conflict_paths)
        # `dict()`, not `done` bare: the caller (SyncService.run_sync) treats
        # this as an open-ended plain dict, and returning the TypedDict itself
        # would tie that unrelated file to PassResult's exact shape.
        return dict(done)
