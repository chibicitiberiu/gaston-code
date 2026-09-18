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

import json
import os
import time
from dataclasses import asdict, dataclass, field, replace
from enum import Enum

from . import paths as P
# A file whose mtime is this close to now might still be inside its FAT tick, so
# a CRC taken from it must not be CACHED -- see Entry.cacheable. Two saves in one
# tick with the same size are otherwise indistinguishable, and the second is
# silently never seen.
SETTLE_S = 3.0

#: Why a path was skipped when a side reported it unreadable. A constant rather
#: than a literal because the apply loop has to tell this SKIP apart from an
#: `ignored` one: this is worth telling the agent about, and `ignored` is the
#: normal state of most of a tree.
UNREADABLE = ("the machine can see this file but cannot open it -- it may be "
              "open in another program, or its name may be one the machine "
              "cannot address")

#: Out of the scan set, and silent by design.
IGNORED = "ignored"

#: Inside a directory a side could not list. Silent too: the directory itself
#: is announced through UNREADABLE, and repeating that per descendant would
#: turn one problem into a hundred messages.
UNDER_UNREADABLE = "inside a directory the machine could not list"


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


@dataclass
class Entry:
    """One manifest row. `crc` is over the CLASS-APPROPRIATE bytes."""

    type: Type
    cls: str = P.CLASS_TEXT
    size: int = 0
    mtime: float = 0.0
    crc: int = 0
    #: BOTH checksums, when the side that produced this row could supply them.
    #: The class is no longer a property of the content alone -- `.gitattributes`
    #: can declare a path byte-exact that the content reads as text -- so the
    #: row has to be re-classifiable without re-reading the file, which on the
    #: guest's side would mean a second scan over the wire.
    crc_raw: int = 0
    crc_norm: int = 0
    #: Whether `crc_raw`/`crc_norm` are real. A client that sends one checksum
    #: computed it for its OWN classification, and the other cannot be derived
    #: from it -- a pure-LF file has them equal, so their being equal proves
    #: nothing. Without this, an override against such a client compares a
    #: normalised checksum to a raw one and the path conflicts forever.
    dual: bool = False
    #: The spelling THIS side holds. The canonical key folds case for matching;
    #: this is what actually gets written, because writing at the folded path
    #: would create a second file beside README.md on a case-sensitive bridge.
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

    def as_class(self, cls: str) -> "Entry":
        """This row re-read under `cls`, for an attributes override.

        Returns self when nothing moves, so the common path allocates nothing.
        """
        if cls == self.cls:
            return self
        crc = self.crc_norm if cls == P.CLASS_TEXT else self.crc_raw
        return replace(self, cls=cls, crc=crc)

    def same_content(self, other: "Entry | None") -> bool:
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
    reported: bool = False


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
    #: The sync root the guest reported, in full. `tree_id` is
    #: "%08lX-%s" % (volume serial, root) built into a 64-byte buffer on the
    #: guest, so it keeps only the first 54 characters of the root -- two roots
    #: on one volume sharing a 54-character prefix produce the SAME id and the
    #: mismatch guard passes when it should refuse.
    #:
    #: Compared here rather than lengthening the guest's buffer, because
    #: changing how the id is computed changes it for every existing baseline
    #: with a long root, which would then refuse every pass afterwards with no
    #: way to say "this is the same tree". `root=` is already on the wire, in
    #: full, and was never compared to anything.
    guest_root: str = ""
    #: The codepage the matching keys were folded through. Changing it re-keys
    #: every row, so the pass refuses rather than re-keying in place.
    acp: str = ""
    generation: int = 0
    pending_notice: list = field(default_factory=list)
    latched: dict[str, str] = field(default_factory=dict)
    #: What has already been announced, per path, as a fingerprint of the
    #: situation. A divergence that cannot be resolved -- `aux.c`, a path the
    #: agent deleted that the user still has -- is a STANDING condition, and
    #: the pass that finds it runs every thirty seconds. Without this the agent
    #: is told about the same unresolvable thing forever, which trains it to
    #: ignore the channel that also carries conflicts. The fingerprint, rather
    #: than a bare flag, so that the situation CHANGING is announced again.
    reported: dict[str, str] = field(default_factory=dict)
    #: Every report line the last pass produced. `sync_status` wants the whole
    #: standing set every time it is asked; the LOG wants only what changed,
    #: or an unfixable `aux.c` prints a warning twice a minute until the
    #: warnings stop being read.
    reported_lines: list = field(default_factory=list)
    #: The reason the last pass REFUSED to run, if it did. A pass-level abort
    #: touches no row and raises before anything is saved, so it left no trace
    #: at all -- it was a log warning and nothing else, on a timer that fires
    #: every thirty seconds. Recorded here so it can be said once, so a
    #: DIFFERENT abort is said again, and so sync_status can answer "why has
    #: nothing moved for an hour" without the answer being "read the log".
    aborted: str = ""
    #: (size, mtime, auto_class, crc_raw, crc_norm, content_size) per path, so
    #: an unchanged tree is not re-read end to end every thirty seconds. It
    #: caches what the CONTENT says, never the effective class, so changing
    #: `.gitattributes` re-decides every path without invalidating anything.
    #: Entries are written only when `Entry.cacheable` allows it.
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
                "latched": self.latched,
                "reported": self.reported,
                "reported_lines": self.reported_lines,
                "aborted": self.aborted,
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
        if os.path.exists(path):
            os.replace(path, path + ".prev")
        os.replace(tmp, path)

    @classmethod
    def load(cls, path: str) -> "Baseline":
        """Raises on a corrupt baseline rather than returning an empty one.

        "No baseline" is a first sync, which conflicts the entire tree. Refusing
        to run is the safe failure; guessing is not.
        """
        if not os.path.exists(path):
            return cls()
        with open(path, encoding="utf-8") as fh:
            d = json.load(fh)          # a JSONDecodeError here must propagate
        out = cls(
            tree_id=d.get("tree_id", ""),
            guest_root=d.get("guest_root", ""),
            acp=d.get("acp", ""),
            generation=int(d.get("generation", 0)),
            pending_notice=list(d.get("pending_notice", [])),
            latched=dict(d.get("latched", {})),
            reported=dict(d.get("reported", {})),
            reported_lines=list(d.get("reported_lines", [])),
            aborted=d.get("aborted", ""),
            scan_cache={k: tuple(v)
                        for k, v in (d.get("scan_cache") or {}).items()},
        )
        for name, dest in (("rows", out.rows), ("ignored", out.ignored),
                           ("prev", out.prev)):
            for k, v in (d.get(name) or {}).items():
                dest[k] = Entry(type=Type(v["type"]), cls=v["cls"],
                                size=v["size"], mtime=v["mtime"],
                                crc=v["crc"],
                                crc_raw=v.get("crc_raw", 0),
                                crc_norm=v.get("crc_norm", 0),
                                dual=v.get("dual", False),
                                display=v.get("display", ""))
        for k, v in (d.get("tombs") or {}).items():
            out.tombs[k] = Tomb(cls=v["cls"], crc=v["crc"],
                                reported=v.get("reported", False))
        return out


def diff(guest: dict[str, Entry], bridge: dict[str, Entry],
         base: Baseline) -> list[Decision]:
    """The state table, in evaluation order. First match wins.

    Enumerated exhaustively over G,B in {absent, file, dir, unreadable} and the
    baseline states; every combination resolves to a stated action.
    """
    out: list[Decision] = []
    # `ignored` is in the union deliberately: an ignored path may exist on
    # neither side right now, and it still has to be visitable so it can be
    # reported and so un-ignoring it later resumes from the real ancestor.
    # A directory reported unreadable has NO descendants in that manifest --
    # the side that could not list it could not enumerate what is inside it
    # either. Without this, every file under it looks absent, which is row 12:
    # "deleted on the guest, kept here", once per file, for a directory nobody
    # touched. One unreadable directory would announce its whole subtree as
    # deletions.
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

        # 1. Unreadable. Not absent, not unchanged -- just not this pass.
        #
        # The reason used to say "locked by another process", which is only one
        # of the causes and the only transient one. The machine also reports
        # `u` for a name it can enumerate but not open -- one carrying a byte
        # with no form in its codepage, say -- and that never clears. Claiming
        # a lock sends the reader after a process that does not exist, and
        # because this was never announced at all, nobody found out either way.
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

        # Directories carry no content; existence is the whole story.
        if (g and g.type is Type.DIR) or (b and b.type is Type.DIR):
            if g and not b:
                out.append(Decision(key, Act.ADOPT, "directory exists on the guest"))
            elif b and not g:
                out.append(Decision(key, Act.MKDIR, "directory only on the bridge",
                                    display=b.display))
            else:
                out.append(Decision(key, Act.NOTHING))
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
            elif g.same_content(b):
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
            elif g.same_content(b):
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
            # The user deleted it and the bridge still holds it. NO TOMBSTONE:
            # one here would make crc(B) == T.crc read as "untouched", quietly
            # freezing a live file the agent keeps editing.
            out.append(Decision(key, Act.REPORT, "deleted on the guest, kept here"))
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
            # A stale editor buffer reverts a push and looks exactly like an
            # edit. If the guest now holds what the LAST PUSH REPLACED, it was
            # reverted, not edited -- and pulling would erase the agent's work
            # with no conflict copy, because B == A means it was never "the
            # agent's version".
            prev = base.prev.get(key)
            if prev is not None and g.same_content(prev):
                out.append(Decision(key, Act.CONFLICT,
                                    "the guest reverted a push (stale editor "
                                    "buffer?) -- preserving the pushed version"))
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
            Act.PULL: 4, Act.PUSH: 4, Act.NOTHING: 5}
    return sorted(decisions, key=lambda d: (rank.get(d.act, 9), d.key.count("/"),
                                            d.key))
