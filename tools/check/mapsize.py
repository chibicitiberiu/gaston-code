#!/usr/bin/env python3
"""Where an exe's bytes went, per object module, read from the wlink map.

`make check` says how much is committed at load; it never says by whom. This
does: it charges each symbol the distance to the next symbol and groups the
result by the `Module:` the map attributes it to.

    python3 tools/check/mapsize.py build/win/gaston.map
    python3 tools/check/mapsize.py build/win/gremote.map --top 30
    python3 tools/check/mapsize.py build/win/gaston.map --module uuid

It REPORTS; it never fails a build. The gates are `make check`'s business.

Two things it will not pretend to know, both learned by getting them wrong:

  AN UNNAMED RUN IS CHARGED TO WHOEVER PRECEDES IT, and that happens in
  code as well as in data. Open Watcom emits string literals with no public
  symbol, and it puts read-only data in the AUTO group beside code, so a
  literal pool lands INSIDE _TEXT. Measured in gaston: CONST has no named
  symbols at all (51,374 bytes nobody can break down), the largest gap in
  CONST2 is 8,764 bytes charged to a uiengine.o literal whose successor
  belongs to steps.cpp -- and in _TEXT, `ComposeBox::hasFocus()` is charged
  13,559 bytes because the next named symbol belongs to optsheet.o. It is
  about thirty bytes of code.

  So: the data classes are not attributed by module at all, and in the code
  classes every charge above --suspect (2 KB by default) is listed
  separately and subtracted into an `unnamed` bucket. Each module gets two
  figures, `bytes` without its suspect charges and `+unnamed` with them,
  because the first is a floor and the second is a ceiling and the truth is
  not knowable from this file.

  A SYMBOL IS CLAMPED TO ITS SEGMENT, NOT TO A PAGE. The first cut of this
  dropped any gap over 4,096 bytes as "a segment jump", which also discarded
  every symbol genuinely larger than a page: it accounted for 374,033 of
  gaston's 406,591-byte _TEXT and lost 32 KB in silence. The segment table
  at the top of the map gives the real bounds, so each symbol is charged up
  to the end of the segment it starts in, and the totals are reconciled
  against that table. A table that does not add up prints a warning instead
  of a ranking, because a plausible wrong number is worse than none.

Watcom writes read-only data into the AUTO group beside code (which is also
why `make check`'s CMOV gate has to locate its hits rather than count them),
so a CODE-class segment holds both. The reconciliation is therefore exact
per segment, and a module's figure is "code plus whatever rodata the linker
put inside the code segment under a name".
"""
from __future__ import annotations

import argparse
import os
import re
import sys

SEG_ROW = re.compile(
    r"^(\S+)\s+(\S+)\s+(\S+)\s+([0-9a-f]{8})\s+([0-9a-f]{8})\s*$")
MODULE = re.compile(r"^Module:\s+(.*)$")
SYMBOL = re.compile(r"^([0-9a-f]{8})([*+]?)\s+(\S.*)$")


class Segment:
    def __init__(self, name: str, cls: str, group: str, addr: int, size: int):
        self.name = name
        self.cls = cls
        self.group = group
        self.addr = addr
        self.size = size

    @property
    def end(self) -> int:
        return self.addr + self.size

    @property
    def is_code(self) -> bool:
        return self.cls == "CODE"


def tidy(path: str) -> str:
    """The map spells every module as an absolute path, twice."""
    name = path.split("(")[0].strip()
    for marker in ("/obj/", "/lib/"):
        if marker in name:
            return name.split(marker, 1)[1]
    return os.path.basename(name)


def parse(path: str) -> tuple[list[Segment], list[tuple[int, str, str, str]]]:
    segs: list[Segment] = []
    syms: list[tuple[int, str, str, str]] = []
    module = None
    where = "head"
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.rstrip("\n")
            if "Segments" in line and "|" in line:
                where = "segments"
                continue
            if "Memory Map" in line and "|" in line:
                where = "memory"
                continue
            # Imported Symbols / Libraries Used / Linker Statistics all follow
            # the memory map and must not be read as symbols.
            if "|" in line and ("Imported" in line or "Libraries" in line
                                or "Statistics" in line):
                where = "tail"
                continue
            if where == "segments":
                m = SEG_ROW.match(line.strip())
                if m and m.group(1) != "Segment":
                    segs.append(Segment(m.group(1), m.group(2), m.group(3),
                                        int(m.group(4), 16),
                                        int(m.group(5), 16)))
                continue
            if where == "memory":
                m = MODULE.match(line.strip())
                if m:
                    module = tidy(m.group(1))
                    continue
                m = SYMBOL.match(line.strip())
                if m and module:
                    syms.append((int(m.group(1), 16), m.group(2), module,
                                 m.group(3)))
    return segs, syms


def seg_of(segs: list[Segment], addr: int) -> Segment | None:
    for s in segs:
        if s.size and s.addr <= addr < s.end:
            return s
    return None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("map")
    ap.add_argument("--top", type=int, default=20,
                    help="how many modules to list (0 = all)")
    ap.add_argument("--module", default=None,
                    help="only modules whose name contains this")
    ap.add_argument("--suspect", type=int, default=2048,
                    help="a single symbol charged more than this is an "
                         "unnamed run, not a symbol (0 disables)")
    a = ap.parse_args()

    segs, syms = parse(a.map)
    if not segs or not syms:
        print("mapsize: %s has no segment table or no memory map" % a.map,
              file=sys.stderr)
        return 2

    syms.sort(key=lambda r: r[0])
    code = {s.name for s in segs if s.is_code}

    # Charge each symbol the gap to the next, clamped to its own segment.
    per_mod: dict[str, int] = {}
    per_seg_named: dict[str, int] = {}
    unref: dict[str, int] = {}
    unnamed: dict[str, int] = {}
    suspects: list[tuple[int, str, str]] = []
    for i, (addr, flag, module, name) in enumerate(syms):
        seg = seg_of(segs, addr)
        if seg is None:
            continue
        stop = seg.end
        if i + 1 < len(syms) and syms[i + 1][0] < stop:
            stop = syms[i + 1][0]
        size = max(0, stop - addr)
        per_seg_named[seg.name] = per_seg_named.get(seg.name, 0) + size
        if seg.name not in code:
            continue
        if a.suspect and size > a.suspect:
            unnamed[module] = unnamed.get(module, 0) + size
            suspects.append((size, module, name))
            continue
        per_mod[module] = per_mod.get(module, 0) + size
        if flag == "*":
            unref[module] = unref.get(module, 0) + size

    print("%s" % a.map)
    print()
    print("  segment      class     size   named   unattributed")
    for s in segs:
        if not s.size:
            continue
        named = per_seg_named.get(s.name, 0)
        print("  %-12s %-6s %8d %7d %10d%s"
              % (s.name, s.cls, s.size, named, s.size - named,
                 "" if s.is_code else "   (not attributed by module)"))
    print()

    code_total = sum(s.size for s in segs if s.is_code)
    accounted = sum(per_mod.values()) + sum(unnamed.values())
    gap = code_total - accounted
    if gap < 0 or gap > code_total // 20:
        print("  WARNING: the code segments total %d and the modules account"
              % code_total)
        print("           for %d. A difference this size means the map was"
              % accounted)
        print("           read wrongly; the ranking below is not trustworthy.")
        print()

    rows = sorted(per_mod.items(), key=lambda kv: -kv[1])
    if a.module:
        rows = [r for r in rows if a.module.lower() in r[0].lower()]
    shown = rows if not a.top else rows[:a.top]
    print("  code bytes by module (%d modules, %d shown)"
          % (len(rows), len(shown)))
    print()
    print("     bytes     +unnamed   unreferenced  module")
    for module, size in shown:
        dead = unref.get(module, 0)
        run = unnamed.get(module, 0)
        print("  %8d   %10s   %12s  %s"
              % (size, str(run) if run else "", str(dead) if dead else "",
                 module))
    print()
    print("  code segments %d, attributed %d, difference %d"
          % (code_total, accounted, gap))
    if suspects:
        suspects.sort(reverse=True)
        print()
        print("  unnamed runs -- a literal pool charged to the symbol before"
              " it, not code")
        for size, module, name in suspects[:10]:
            print("  %8d   %-28s %s" % (size, module, name[:44]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
