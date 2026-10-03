"""Reconciliation: manifests, the baseline, and the three-way diff.

Pure logic plus local file I/O. No socket, so the whole state table is testable
without a guest -- which matters, because `fakegui.py` runs on CPython and cannot
model case-insensitivity, 8.3 aliasing, reserved device names, trailing-dot
stripping or FAT's 2-second mtime granularity. A green fakegui is evidence about
framing, never about the guest.

TWO INVARIANTS. Everything else follows from them.

  I1  The baseline advances LAST, from the CRC in the receiver's ack, after the
      receiver's write is durable. Every other ordering loses data on an
      interruption: advance first, crash, and the next pass reverses the
      direction of the transfer with no conflict copy.

  I2  After a transfer, crc(guest) == crc(bridge) == baseline, or it is
      REPORTED. Never retried -- retrying a transfer that cannot establish this
      is how a livelock starts.

DIRECTION COMES FROM DIVERGENCE, NEVER FROM TIME. The guest's clock is its own
and FAT rounds mtime to two seconds, so "which is newer" is a question this
system cannot answer honestly. It asks "which side moved away from what both
sides last agreed on" instead, which is a content question.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import time
from dataclasses import asdict, dataclass, field, replace
from enum import Enum
from typing import Any

from ..status import SyncStatus
from . import paths as P

# A file saved this recently may still be inside its FAT 2-second tick, so its
# CRC must not be CACHED (Entry.cacheable) -- two saves in one tick, same size,
# are indistinguishable, and the second would be lost.
SETTLE_S = 3.0

#: A constant, not a literal: the apply loop has to tell this SKIP apart from
#: an `ignored` one, which is the normal state of most of a tree and not worth
#: reporting.
UNREADABLE = ("the machine can see this file but cannot open it -- it may be "
              "open in another program, or its name may be one the machine "
              "cannot address")

IGNORED = "ignored"

#: The directory itself is announced through UNREADABLE; repeating that per
#: descendant would turn one problem into a hundred messages.
UNDER_UNREADABLE = "inside a directory the machine could not list"

#: Named so syncrun.py's aggregate-revert check counts what `diff` already
#: decided instead of re-deriving it from a string that could drift.
REVERTED_PUSH = ("the guest reverted a push (stale editor buffer?) -- "
                 "preserving the pushed version")
GONE_ON_GUEST = "deleted on the guest, kept here"


class Type(str, Enum):
    FILE = "f"
    DIR = "d"
    #: Locked by another process. NOT absent -- treating it as absent makes it a
    #: tombstone, and then the user's later edits are invisible forever.
    UNREADABLE = "u"


class Act(str, Enum):
    NOTHING = "nothing"
    PULL = "pull"
    PUSH = "push"
    MKDIR = "mkdir"
    ADOPT = "adopt"                  # same content both sides; baseline only
    CONFLICT = "conflict"
    REPORT = "report"                # diverged, and doing anything loses data
    SKIP = "skip"
    TOMBSTONE = "tombstone"
    CLEAR_TOMBSTONE = "clear_tombstone"
    RMDIR_HERE = "rmdir_here"        # gone on the guest; rmdir here if empty


@dataclass
class Entry:
    """One manifest row. `crc` is over the CLASS-APPROPRIATE bytes."""

    type: Type
    cls: str = P.CLASS_TEXT
    mtime: float = 0.0
    crc: int = 0
    #: BOTH checksums, when the producer had them: `.gitattributes` can call a
    #: path byte-exact that reads as text, so a row must be re-classifiable
    #: without a second scan over the wire.
    crc_raw: int = 0
    crc_norm: int = 0
    #: Whether `crc_raw`/`crc_norm` are real, not equal by accident -- a
    #: client sending one checksum computed it for its own classification.
    dual: bool = False
    #: The spelling THIS side holds. The canonical key folds case for
    #: matching; the folded spelling is never what gets written.
    display: str = ""

    @property
    def cacheable(self) -> bool:
        """May this row's CRC be stored for reuse?

        Gating the cache LOOKUP is not enough, which is the subtle half. A read
        taken while the file's 2-second tick is still open can be superseded by
        a second save in that same tick, at the same size -- so the entry must
        never be WRITTEN, not merely distrusted later.
        """
        return (time.time() - self.mtime) > SETTLE_S

    def as_class(self, cls: str) -> Entry:
        """This row re-read under `cls`, for an attributes override.

        Returns self when nothing moves, so the common path allocates nothing.
        """
        if cls == self.cls:
            return self
        crc = P.crc_for(cls, self.crc_norm, self.crc_raw)
        return replace(self, cls=cls, crc=crc)

    def same_content(self, other: Entry | None) -> bool:
        """Cross-side equality: (type, class, crc).

        Size and mtime are per-side cache keys and are NEVER compared between
        sides -- a uniform text file has different sizes on the two sides by
        construction, so comparing them reads every text file as changed.
        """
        if other is None:
            return False
        return (self.type, self.cls, self.crc) == (other.type, other.cls, other.crc)


@dataclass
class Tomb:
    """A path both sides agree is gone.

    Carries the last agreed content, without which "untouched since the
    deletion" is indistinguishable from "edited since the deletion" -- and the
    edit is then silently never synced.
    """

    cls: str
    crc: int


@dataclass
class Wait:
    """One outstanding request to the guest, and what kind it is.

    These were four different ad-hoc dict shapes sharing one id namespace:
    a scan carried {"future", "rows", "display"} and a put, get or mkdir
    carried {"future"} alone. `sync_frame` then indexed entry["rows"]
    whenever a SYNCENT arrived -- so a SYNCENT bearing a PUT's id raised
    KeyError, which is not caught anywhere between there and the read loop,
    and took the whole connection down.

    A guest would have to be confused or hostile to send that. The point is
    that the bridge's answer to a confused guest should be a log line, not a
    dropped link -- and a shape it can check is what makes that possible.
    """

    kind: str                       # "scan" | "put" | "get" | "mkdir"
    #: Typed, not `object`: every `fut.set_result(...)`/`fut.done()` that
    #: resolves these was otherwise unchecked by mypy.
    future: asyncio.Future[Any]
    #: Scans only; None on the others, which makes a mismatched frame
    #: detectable rather than a KeyError.
    rows: dict | None = None
    display: dict | None = None
    #: Gets only: file bytes as SYNCDAT chunks arrive.
    data: bytearray | None = None


# --- what a Wait resolves TO -------------------------------------------
#
# Readability half of the same fix: these futures used to resolve to
# positional tuples of different arities per kind, unpacked with nothing to
# check the shape against what the caller expected.


@dataclass
class ScanResult:
    """SYNCEND -- what a manifest request came back with."""

    status: SyncStatus
    files: int
    tree_id: str
    root: str
    rows: dict
    display: dict


@dataclass
class AckResult:
    """SYNCPUTACK and SYNCMKDIRACK: one shape, two producers. A push reads
    `crc` as the guest's checksum of what it just wrote; a mkdir has no crc
    to ack and simply ignores it -- the SAME wire shape, not a coincidence
    two call sites happened to agree on."""

    status: SyncStatus
    crc: int


@dataclass
class GetResult:
    """SYNCDATEND -- the guest's own reading of a file `pull` just fetched."""

    status: SyncStatus
    crc: int
    cls: str
    data: bytes


@dataclass
class Decision:
    key: str
    act: Act
    reason: str = ""
    display: str = ""


@dataclass
class Baseline:
    """What both sides last agreed on. Bridge-side only; the guest is stateless.

    Persisted OUTSIDE the agent's reach. `permissions.off_limits` does not cover
    Bash, so `sed -i` from the working directory could otherwise rewrite this,
    and a corrupted-yet-parseable baseline makes the diff reach wrong conclusions
    with complete confidence.
    """

    rows: dict[str, Entry] = field(default_factory=dict)
    tombs: dict[str, Tomb] = field(default_factory=dict)
    ignored: dict[str, Entry] = field(default_factory=dict)
    #: The ancestor each push replaced, per path. This is what makes a stale
    #: editor buffer detectable -- see diff().
    prev: dict[str, Entry] = field(default_factory=dict)
    tree_id: str = ""
    #: The sync root the guest reported, in full. `tree_id` truncates the root
    #: to 54 characters (a 64-byte buffer on the guest), so two roots sharing
    #: that prefix produce the SAME id. Compared here in full instead, since
    #: lengthening the guest's buffer would re-key every existing baseline.
    guest_root: str = ""
    #: The codepage the matching keys were folded through. Changing it re-keys
    #: every row, so the pass refuses rather than re-keying in place.
    acp: str = ""
    generation: int = 0
    #: The USER's copy: cleared when it actually reaches their screen.
    pending_notice: list[str] = field(default_factory=list)
    #: The AGENT's copy: cleared when handed to the model. Durable, not just
    #: in-memory, so a restart before the agent's next turn cannot lose it (02.20).
    pending_notice_agent: list[str] = field(default_factory=list)
    latched: dict[str, str] = field(default_factory=dict)
    #: Fingerprint of what's already been announced, per path -- a STANDING
    #: divergence recurs every pass, and a bare flag would silence it forever.
    reported: dict[str, str] = field(default_factory=dict)
    #: The last pass's full report lines. `sync_status` wants the whole
    #: standing set; the LOG wants only the delta, or an unfixable one repeats.
    reported_lines: list = field(default_factory=list)
    #: Why the last pass REFUSED to run, if it did -- so recovery can be
    #: announced, and a DIFFERENT abort announced again.
    aborted: str = ""
    #: Set by `sync_resolve(action="accept-tree")`, consumed by the next pass.
    #: A speed bump, not a verdict -- refusing forever is data loss too.
    accept_tree: bool = False
    #: Every abort reason announced since the last SUCCESSFUL pass, not just
    #: `aborted` (the latest) -- two reasons alternating would otherwise
    #: defeat repeat-suppression. Cleared on success.
    announced: list = field(default_factory=list)
    #: (size, mtime, auto_class, crc_raw, crc_norm, content_size) per path.
    #: Caches what the CONTENT says, never the effective class, so
    #: `.gitattributes` changing re-decides every path for free.
    scan_cache: dict = field(default_factory=dict)

    # --- persistence -------------------------------------------------------

    def save(self, path: str) -> None:
        """Atomic, with the previous generation kept.

        An in-place truncate-and-write that is interrupted leaves JSON that does
        not parse, and the natural recovery -- "no baseline" -- means every file
        is new on both sides. One power cut would destroy the only record
        distinguishing "here but not there" from "deleted there".
        """
        blob = json.dumps(
            {
                "rows": {k: asdict(v) for k, v in self.rows.items()},
                "tombs": {k: asdict(v) for k, v in self.tombs.items()},
                "ignored": {k: asdict(v) for k, v in self.ignored.items()},
                "prev": {k: asdict(v) for k, v in self.prev.items()},
                "tree_id": self.tree_id,
                "guest_root": self.guest_root,
                "acp": self.acp,
                "generation": self.generation,
                "pending_notice": self.pending_notice,
                "pending_notice_agent": self.pending_notice_agent,
                "latched": self.latched,
                "reported": self.reported,
                "reported_lines": self.reported_lines,
                "aborted": self.aborted,
                "accept_tree": self.accept_tree,
                "announced": self.announced,
                "scan_cache": {k: list(v) for k, v in self.scan_cache.items()},
            },
            indent=1,
        )
        tmp = path + ".new"
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(blob)
            fh.flush()
            os.fsync(fh.fileno())
        # Copy the old baseline aside BEFORE replacing, so the window where
        # `path` is absent never exists. shutil.copy2 is not atomic but .prev
        # is only a debugging aid — losing it is harmless; losing `path` is not.
        if os.path.exists(path):
            shutil.copy2(path, path + ".prev")
        os.replace(tmp, path)
        # Fsync the directory so the rename is durable against a host crash.
        dfd = os.open(os.path.dirname(path) or ".", os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)

    @classmethod
    def load(cls, path: str) -> Baseline:
        """Raises on a corrupt baseline rather than returning an empty one.

        "No baseline" is a first sync, which conflicts the entire tree. Refusing
        to run is the safe failure; guessing is not.
        """
        if not os.path.exists(path):
            if os.path.exists(path + ".prev"):
                raise RuntimeError(
                    f"baseline {path!r} is missing but {path + '.prev'!r} "
                    f"exists — this looks like a crash during save. Refusing "
                    f"rather than treating it as a first sync, which would "
                    f"resurrect every deletion.")
            return cls()
        with open(path, encoding="utf-8") as fh:
            d = json.load(fh)          # a JSONDecodeError here must propagate
        out = cls(
            tree_id=d.get("tree_id", ""),
            guest_root=d.get("guest_root", ""),
            acp=d.get("acp", ""),
            generation=int(d.get("generation", 0)),
            pending_notice=list(d.get("pending_notice", [])),
            pending_notice_agent=list(d.get("pending_notice_agent", [])),
            latched=dict(d.get("latched", {})),
            reported=dict(d.get("reported", {})),
            reported_lines=list(d.get("reported_lines", [])),
            aborted=d.get("aborted", ""),
            accept_tree=bool(d.get("accept_tree", False)),
            announced=list(d.get("announced", [])),
            scan_cache={k: tuple(v)
                        for k, v in (d.get("scan_cache") or {}).items()},
        )
        for name, dest in (("rows", out.rows), ("ignored", out.ignored),
                           ("prev", out.prev)):
            for k, v in (d.get(name) or {}).items():
                dest[k] = Entry(type=Type(v["type"]), cls=v["cls"],
                                mtime=v["mtime"],
                                crc=v["crc"],
                                crc_raw=v.get("crc_raw", 0),
                                crc_norm=v.get("crc_norm", 0),
                                dual=v.get("dual", False),
                                display=v.get("display", ""))
        for k, v in (d.get("tombs") or {}).items():
            out.tombs[k] = Tomb(cls=v["cls"], crc=v["crc"])
        return out


def diff(guest: dict[str, Entry], bridge: dict[str, Entry],
         base: Baseline) -> list[Decision]:
    """The state table, in evaluation order. First match wins.

    Enumerated exhaustively over G,B in {absent, file, dir, unreadable} and the
    baseline states; every combination resolves to a stated action.
    """
    out: list[Decision] = []
    # `ignored` is in the union deliberately, so it stays visitable to report
    # and un-ignoring resumes from the real ancestor. An unreadable directory
    # has NO descendants here -- without this, every file under it reads as
    # absent, announcing a whole subtree as deletions instead of one.
    unreadable_dirs = tuple(
        k + "/" for k, e in list(guest.items()) + list(bridge.items())
        if e is not None and e.type is Type.UNREADABLE)

    for key in sorted(set(guest) | set(bridge) | set(base.rows)
                      | set(base.tombs) | set(base.ignored)):
        # 0. Under something we could not list. Not absent -- unknown.
        if unreadable_dirs and key.startswith(unreadable_dirs):
            out.append(Decision(key, Act.SKIP, UNDER_UNREADABLE))
            continue

        g, b = guest.get(key), bridge.get(key)
        a = base.rows.get(key)
        t = base.tombs.get(key)

        # 1. Unreadable. Not absent, not unchanged -- just not this pass. Not
        # necessarily "locked": `u` also covers a name the machine can
        # enumerate but never open at all, which never clears.
        if (g and g.type is Type.UNREADABLE) or (b and b.type is Type.UNREADABLE):
            out.append(Decision(key, Act.SKIP, UNREADABLE))
            continue

        # 2. Out of the scan set. The row FREEZES with its last (class, crc) so
        #    editing .gitignore -- routine -- is never a mass deletion, and
        #    un-ignoring later resumes from the real ancestor.
        if key in base.ignored:
            out.append(Decision(key, Act.SKIP, IGNORED))
            continue

        # 3. Type change. Resolved over the subtree, guest wins, and every
        #    bridge descendant is preserved first.
        if g and b and g.type is not b.type:
            out.append(Decision(key, Act.CONFLICT, "type changed (file <-> directory)"))
            continue

        # Directories carry no CONTENT, so existence's own history matters:
        # ignoring the ancestor made every one-sided absence a creation, so a
        # directory deleted on the machine kept reappearing via MKDIR every
        # pass (02.45). The ancestor now answers it exactly as rows 11-13 do
        # for files: REPORT, since there is no SYNCDEL (BACKLOG §4).
        if (g and g.type is Type.DIR) or (b and b.type is Type.DIR):
            if g and b:
                out.append(Decision(key, Act.NOTHING))
            elif g:
                # No ancestor: genuinely new on the machine, so adopt it. With
                # one, the BRIDGE is what lost it.
                out.append(Decision(key, Act.ADOPT, "directory exists on the guest")
                           if a is None else
                           Decision(key, Act.REPORT,
                                    "directory deleted on the bridge, still on "
                                    "the machine"))
            else:
                # g is falsy, so the outer `or` matched through b: b is not None.
                assert b is not None
                # With an ancestor the MACHINE deleted it -- the person, or the
                # agent's own `delete`. A directory has no content to lose, so
                # follow it: rmdir here, after the files below it have gone
                # (RMDIR_HERE sorts last, deepest first). Reporting it instead
                # left empty folders on the bridge after every agent delete,
                # sent the agent sync notices it could not resolve, and once it
                # `forget`-ed them the folders went BACK to the machine as new
                # (measured on the Athlon, 2026-10-03). rmdir refuses a folder
                # that is not empty, and the executor reports that case.
                out.append(Decision(key, Act.MKDIR, "directory only on the bridge",
                                    display=b.display)
                           if a is None else
                           Decision(key, Act.RMDIR_HERE,
                                    "directory deleted on the machine",
                                    display=b.display))
            continue

        # 4-6. Tombstoned.
        if t is not None:
            if not g and not b:
                out.append(Decision(key, Act.NOTHING, "tombstoned"))
            elif g and not b:
                if g.cls == t.cls and g.crc == t.crc:
                    out.append(Decision(key, Act.NOTHING, "tombstoned, unchanged"))
                else:
                    out.append(Decision(key, Act.PULL,
                                        "edited on the guest after deletion"))
            elif b and not g:
                if b.cls == t.cls and b.crc == t.crc:
                    out.append(Decision(key, Act.NOTHING, "tombstoned, unchanged"))
                else:
                    out.append(Decision(key, Act.PUSH,
                                        "edited on the bridge after deletion",
                                        display=b.display))
            # g, b present by elimination; `is not None` just narrows for mypy.
            elif g is not None and g.same_content(b):
                out.append(Decision(key, Act.CLEAR_TOMBSTONE, "recreated identically"))
            else:
                out.append(Decision(key, Act.CONFLICT, "recreated differently on both"))
            continue

        # 7-10. No ancestor.
        if a is None:
            if not g and not b:
                out.append(Decision(key, Act.NOTHING))
            elif g and not b:
                out.append(Decision(key, Act.PULL, "new on the guest"))
            elif b and not g:
                out.append(Decision(key, Act.PUSH, "new on the bridge",
                                    display=b.display))
            # g, b present by elimination; `is not None` just narrows for mypy.
            elif g is not None and g.same_content(b):
                out.append(Decision(key, Act.ADOPT, "same content, no ancestor"))
            else:
                out.append(Decision(key, Act.CONFLICT,
                                    "first-sync collision: no common ancestor"))
            continue

        # 11-13. Absences with an ancestor.
        if not g and not b:
            out.append(Decision(key, Act.TOMBSTONE, "deleted on both sides"))
            continue
        if not g:
            # Guest deleted it. Untouched here since baseline: tombstone so it
            # doesn't resurrect. Edited here since: report, or the edit is lost.
            if b and a and b.same_content(a):
                out.append(Decision(key, Act.TOMBSTONE, "deleted on the guest",
                                    display=b.display))
            else:
                out.append(Decision(key, Act.REPORT, GONE_ON_GUEST))
            continue
        if not b:
            same = g.same_content(a)
            out.append(Decision(
                key, Act.REPORT,
                "deleted on the bridge (half of a rename?), still on the guest"
                if same else
                "deleted on the bridge AND edited on the guest"))
            continue

        # 14-18. Both present, ancestor known.
        g_moved, b_moved = not g.same_content(a), not b.same_content(a)
        if not g_moved and not b_moved:
            out.append(Decision(key, Act.NOTHING))
        elif g_moved and not b_moved:
            # A stale editor buffer reverts a push and looks like an edit; if
            # the guest holds what the LAST PUSH REPLACED, pulling would erase
            # the agent's work with no conflict copy.
            prev = base.prev.get(key)
            if prev is not None and g.same_content(prev):
                out.append(Decision(key, Act.CONFLICT, REVERTED_PUSH))
            else:
                out.append(Decision(key, Act.PULL, "the guest moved"))
        elif b_moved and not g_moved:
            out.append(Decision(key, Act.PUSH, "the bridge moved", display=b.display))
        elif g.same_content(b):
            out.append(Decision(key, Act.ADOPT, "the same edit arrived on both sides"))
        else:
            out.append(Decision(key, Act.CONFLICT, "both sides moved"))
    return out


def order(decisions: list[Decision]) -> list[Decision]:
    """Order within a pass. Not cosmetic.

    Type-change conflicts must stage descendants before anything transfers into
    them, and a directory must exist before a file lands inside it -- CreateFileA
    into a missing directory fails on Win98, so a pass that pushes children first
    fails identically every time.
    """
    rank = {Act.CONFLICT: 0, Act.MKDIR: 1, Act.REPORT: 2, Act.SKIP: 2,
            Act.TOMBSTONE: 3, Act.CLEAR_TOMBSTONE: 3, Act.ADOPT: 3,
            Act.PULL: 4, Act.PUSH: 4, Act.NOTHING: 5, Act.RMDIR_HERE: 6}
    # RMDIR_HERE goes deepest FIRST: a parent is only empty once its
    # children are gone. Everything else is shallowest first.
    return sorted(decisions, key=lambda d: (
        rank.get(d.act, 9),
        -d.key.count("/") if d.act is Act.RMDIR_HERE else d.key.count("/"),
        d.key))
