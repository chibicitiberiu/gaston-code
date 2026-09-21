#!/usr/bin/env python3
"""Report (and gate on) oversized stack frames in the guest binary.

Why this exists: RETROCL.EXE died on the real Windows 98 machine with "invalid
page fault in module RETROCL.EXE at 0167:00416d07". The instruction at the
fault was a `call` -- pushing a return address -- immediately after a
`sub $0x202c,%esp`.

Open Watcom emits no stack probe for a large frame, so a function with an 8KB
frame moves ESP two pages down in one step and the first write lands BELOW the
guard page rather than on it. Windows commits the next page only when the guard
page is touched, so the page is never committed and the process dies. The RPC
worker thread starts with the image's 64KB stack COMMIT, and the sync scan had
three such frames stacked in one call chain (a ~62KB manifest buffer, an 8KB
read buffer and an 8KB normalisation buffer).

It presented as intermittent: the same build ran for a session against a flat
tree and faulted the first time the tree gained a subdirectory, because one
more `walk` frame -- about 1.1KB -- moved the skip past the guard.

`make check` runs this. Frames in our own modules are gated; miniz's zip
reader/writer and the CRT are listed and exempt, because nothing in this
program calls them.
"""
from __future__ import annotations

import bisect
import re
import subprocess
import sys

#: One page. A frame at or above this can step over the guard page.
LIMIT = 4096

#: Modules we do not compile and do not call into with a deep stack.
EXEMPT = ("miniz.o", ".lib:", "clib3r")


def symbols(map_path: str) -> list[tuple[int, str]]:
    out, mod = [], "?"
    for line in open(map_path, errors="replace"):
        m = re.match(r"Module: (\S+)", line)
        if m:
            mod = m.group(1).split("(")[0]
            continue
        m = re.match(r"([0-9A-Fa-f]{8})\*?\s+(\S+)", line)
        if m:
            out.append((int(m.group(1), 16), f"{mod}:{m.group(2)}"))
    out.sort()
    return out


def frames(exe: str) -> list[tuple[int, int]]:
    d = subprocess.run(["objdump", "-d", exe],
                       capture_output=True, text=True).stdout
    return [(int(m.group(1), 16), int(m.group(2), 16))
            for m in re.finditer(
                r"^\s+([0-9a-f]+):.*sub\s+\$0x([0-9a-f]+),%esp", d, re.M)]


def main(argv: list[str]) -> int:
    if len(argv) < 3:
        print("usage: stackframes.py <exe> <map>", file=sys.stderr)
        return 2
    syms = symbols(argv[2])
    addrs = [a for a, _ in syms]
    bad, noted = [], []
    for addr, size in frames(argv[1]):
        if size < LIMIT:
            continue
        i = bisect.bisect_right(addrs, addr) - 1
        name = syms[i][1] if i >= 0 else "?"
        (noted if any(e in name for e in EXEMPT) else bad).append((size, name))

    for size, name in sorted(noted, reverse=True):
        print(f"   note  {size:6d} bytes  {name}  (not reachable from our code)")
    for size, name in sorted(bad, reverse=True):
        print(f"   FAIL  {size:6d} bytes  {name}")
    if bad:
        print(f"\n   A stack frame of {LIMIT} bytes or more steps over the guard\n"
              f"   page and faults once the stack has grown past its commit.\n"
              f"   Put the buffer on the heap.")
        return 1
    print(f"   ok: no frame in our own code reaches {LIMIT} bytes")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
