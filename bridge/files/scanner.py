"""BridgeScanner: the bridge's own half of a sync pass's manifest.

`Pass.scan_bridge` (`syncrun.py`) was 128 lines doing the tree walk, every
per-path refusal (symlinks, reserved and illegal names, MAX_PATH, a
case-insensitive collision), and the CRC cache's read-then-write gate, all
inline on `Pass`. It reads `(root, ignore, attrs, caps, guest_root, cache)`
and produces `(rows, reports)` -- a pure function today in everything but
form (docs/audit/02-bridge-python.md #4.2, fix item 2). `scan_bridge_tree`
below is that function; `Pass.scan_bridge` is now a three-line adapter that
supplies `self`'s pieces and keeps the same name and signature nothing else
had to change for -- including the bare `Pass` subclasses
`test_sync_wire.py` builds to exercise this in isolation, which skip
`__init__` and call `Pass.scan_bridge(p)` unbound.

`first_report` is passed in as a `(key, reason) -> bool` callable rather
than imported, because deciding whether a refusal is news is the
reporter's job (`syncrun.py`'s `Reporter`, #4.2 fix item 4), not the
scanner's -- this function only asks the question once, for an
attributes-refused classification, with no guest/bridge row to fingerprint
against.
"""

from __future__ import annotations

import os
import zlib

from . import paths as P
from . import sync as S

#: Our own working names, on both sides. The scanner must skip them or an
#: interrupted write becomes a permanent file: the other side has never seen
#: it, so it transfers it, and now `x.md.gaston-tmp` syncs forever alongside
#: `x.md`. `syncrun.py`'s `Pass.pull` also uses this, for the temp name it
#: writes a fetched file under before the atomic replace -- defined here
#: rather than there because the scanner is what has to skip it, and
#: `syncrun` already depends on this module for `scan_bridge_tree`.
WORKING_SUFFIXES = (".gaston-tmp", ".gaston-old")


def scan_bridge_tree(
    root: str,
    *,
    ignore,
    attrs,
    caps,
    guest_root: str,
    cache: dict | None,
    report,
    first_report,
) -> dict[str, S.Entry]:
    """Walk `root` and classify every path the way the guest would see it.

    `report(text)` is called once per skipped, unreadable or colliding path.
    `first_report(key, reason)` is called only for an attributes-refused
    classification, to decide whether that refusal is news. `cache` is
    mutated in place -- read for a hit, written on a miss, pruned of
    anything no longer present -- the same CRC cache `Pass.base.scan_cache`
    already was; this function does not know or care that it came from a
    `Baseline`.
    """
    rows: dict[str, S.Entry] = {}
    seen: dict[str, str] = {}
    fresh: dict[str, tuple] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        # Symlinks are not followed: one pointing outside the workdir would
        # have its target's CONTENTS pushed, bypassing off_limits entirely.
        keep_dirs = []
        for d in dirnames:
            full = os.path.join(dirpath, d)
            rel = P.rel_from_posix(root, full)
            if os.path.islink(full):
                # Say so. Containment was always right, but silence here
                # meant an agent could write into a symlinked directory,
                # be told the write succeeded, and never learn that not
                # one byte of it was going to the machine.
                report(f"skipped {rel}/: a symbolic link, which is not "
                       f"followed -- its contents never reach the machine")
                continue
            if ignore and ignore(rel + "/"):
                continue
            keep_dirs.append(d)
        dirnames[:] = keep_dirs
        # Directories go through exactly the same filter as files, and this
        # used to be file-only. `con/x.c` was correctly refused as a file
        # while `con` itself went out as a SYNCMKDIR -- so the bridge asked
        # Windows 98 to create `C:\WORK\con`, which is the `\con\` shape
        # that can take the machine down. It came back `bad_path` that
        # time; "the guest refused it" is not a filter.
        #
        # A refused directory prunes its subtree. Walking into it would
        # produce children that are individually legal and have nowhere to
        # live, each reported separately -- one cause, twenty messages.
        keep = []
        for name in dirnames:
            rel = P.rel_from_posix(root, os.path.join(dirpath, name))
            bad = P.check(rel, guest_root=guest_root, caps=caps, is_dir=True)
            if bad is not None:
                report(f"skipped {rel}/: {bad}")
                continue
            keep.append(name)
            rows[P.canonical(rel, caps=caps)] = S.Entry(
                type=S.Type.DIR, display=rel)
        dirnames[:] = keep
        for name in filenames:
            full = os.path.join(dirpath, name)
            if os.path.islink(full):
                report(f"skipped {P.rel_from_posix(root, full)}: a "
                       f"symbolic link, which is not followed -- it never "
                       f"reaches the machine")
                continue
            if name.endswith(WORKING_SUFFIXES):
                continue
            rel = P.rel_from_posix(root, full)
            if ignore and ignore(rel):
                continue
            bad = P.check(rel, guest_root=guest_root, caps=caps)
            if bad is not None:
                report(f"skipped {rel}: {bad}")
                continue
            key = P.canonical(rel, caps=caps)
            clash = seen.get(key)
            if clash is not None:
                # The NEWCOMER is refused. Refusing both would retroactively
                # break a path that has been syncing for weeks.
                report(f"skipped {rel}: collides with {clash} on a "
                       f"case-insensitive filesystem")
                continue
            seen[key] = rel
            try:
                st = os.stat(full)
            except OSError as exc:
                rows[key] = S.Entry(type=S.Type.UNREADABLE, display=rel)
                report(f"unreadable {rel}: {exc}")
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
                    with open(full, "rb") as fh:
                        data = fh.read()
                except OSError as exc:
                    rows[key] = S.Entry(type=S.Type.UNREADABLE, display=rel)
                    report(f"unreadable {rel}: {exc}")
                    continue
                auto = P.classify(data)
                craw = zlib.crc32(data)
                cnorm = zlib.crc32(P.normalise(data, P.CLASS_TEXT))
                size = len(data)
                fresh[key] = (st.st_size, st.st_mtime, auto, craw, cnorm, size)

            cls = auto
            if attrs is not None:
                cls, refused = attrs.effective_class(rel, auto)
                if refused and first_report(key, f"attrs:{refused}"):
                    report(f"{rel}: {refused}")
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
