#!/usr/bin/env python3
"""Fail if a CMOV lands in code WE compiled.

A Pentium MMX has no CMOV; the compiler is told `-5` so it emits none, and
this is the gate that says so. It used to be a COUNT against a baseline, and
the baseline moved three times without anything being wrong:

    1 -> 2 -> 1 -> 7

None of those were instructions. `objdump -d` walks the AUTO section
linearly, and Open Watcom puts read-only data in AUTO beside code -- so a
GUID table, a jump table or the padding between two library functions decodes
as whatever those bytes happen to spell. Adding a function slides the data
that follows it, and the number changes. A gate whose green condition drifts
with unrelated edits is one people learn to raise rather than read.

So: locate each hit instead of counting them. The linker's map file says
which module every symbol came from, and the only CMOVs that matter are the
ones inside a symbol from an object file in this build -- the compiler's
output, which `-5` governs. A CMOV inside `uuid.lib`'s GUID table is not an
instruction at all, and one inside a prebuilt library routine is not
something `-5` can affect.

Prints every hit with the symbol it falls in, so a real one is identifiable
rather than merely counted.
"""
from __future__ import annotations

import re
import subprocess
import sys

#: `00401234+      void __near rc_bar::move( int, int, int )`
SYM = re.compile(r"^([0-9A-Fa-f]{8})[+*]?\s+(\S.*?)\s*$")
#: `Module: /path/to/build/win/main.o(...)`
MOD = re.compile(r"^Module:\s+(\S+?)(?:\(|\s*$)")
#: ` 4236d0:\t0f 43 50 66 \tcmovae ...` -- address, then the raw bytes.
INSN = re.compile(r"^\s*([0-9a-f]+):\s+((?:[0-9a-f]{2} )+)")


def symbols(map_path: str) -> list[tuple[int, str, str]]:
    """(address, symbol, module) for every symbol in the map, sorted."""
    out: list[tuple[int, str, str]] = []
    module = "?"
    with open(map_path, errors="replace") as f:
        for line in f:
            m = MOD.match(line)
            if m:
                module = m.group(1)
                continue
            m = SYM.match(line)
            if m:
                out.append((int(m.group(1), 16), m.group(2)[:60], module))
    out.sort(key=lambda r: r[0])
    return out


def owns(module: str, ours: set[str]) -> bool:
    """Is this module something this build compiled?"""
    return module.rsplit("/", 1)[-1] in ours


def pointer_table(addr: int, raw: str, lo: int, hi: int) -> bool:
    """Is this "CMOV" three bytes out of a table of code pointers?

    THE MODULE FILTER IS NOT ENOUGH, and finding that out cost a green build.
    Open Watcom puts read-only data in AUTO beside the code it belongs to, so
    a switch's jump table lives INSIDE one of our own object files -- and
    `objdump` walks AUTO linearly, so it decodes that table as instructions.

    A little-endian address of the form 0x00420f06 is the bytes
    `06 0f 42 00`, and the middle three spell `cmovb (%eax),%eax`. miniz's
    error-string table landed at 0x00420f00 the day two new source files
    shifted it there, and ten "CMOVs" appeared in a function nobody had
    touched. Slide it back and they vanish -- which is the same drifting
    green condition this tool was written to replace, one level down.

    So: read the DWORD that would END at this instruction's second byte. If
    it is an address inside the image's code, these bytes are the tail of a
    pointer and not an opcode. Every hit is still printed either way, so a
    real one masked by this is visible rather than silent.
    """
    b = [int(x, 16) for x in raw.split()]
    if len(b) < 3:
        return False
    # The hit is `0f 4x ..`; the pointer's low byte is the one BEFORE it,
    # which objdump attributed to the previous line, so reconstruct from the
    # three bytes here plus the known shape: 0x00_4x_0f_?? .
    #
    # Bytes here are [0f, 4x, y]. As the top three of a little-endian DWORD
    # the value is (y << 24) | (0x4x << 8) | 0x0f ... which is the wrong way
    # round. Read them as the SECOND, THIRD and FOURTH bytes instead:
    #   pointer = ?? | (0f << 8) | (4x << 16) | (y << 24)
    val = (b[2] << 24) | (b[1] << 16) | (b[0] << 8)
    return lo <= val <= hi and b[2] == 0x00


def main(argv: list[str]) -> int:
    if len(argv) < 4:
        print("usage: cmovcheck.py EXE MAP obj.o [obj.o ...]", file=sys.stderr)
        return 2
    exe, map_path, objs = argv[1], argv[2], set(
        o.rsplit("/", 1)[-1] for o in argv[3:])

    dis = subprocess.run(["objdump", "-d", "-m", "i386", exe],
                         capture_output=True, text=True).stdout
    syms = symbols(map_path)
    if not syms:
        print("   FAIL: no symbols in %s; cannot tell code from data"
              % map_path)
        return 1

    # The span the image's own symbols occupy, which is what a pointer in a
    # jump table points into.
    first, last = syms[0][0], syms[-1][0] + 0x10000

    real, benign = [], []
    for line in dis.splitlines():
        if not re.search(r"\scmov", line):
            continue
        m = INSN.match(line)
        if not m:
            continue
        addr = int(m.group(1), 16)
        raw = m.group(2)
        # The symbol this address falls in: the last one at or below it.
        lo, hi = 0, len(syms)
        while lo < hi:
            mid = (lo + hi) // 2
            if syms[mid][0] <= addr:
                lo = mid + 1
            else:
                hi = mid
        where = syms[lo - 1] if lo else (0, "(before the first symbol)", "?")
        if owns(where[2], objs) and not pointer_table(addr, raw, first, last):
            real.append((addr, where))
        else:
            benign.append((addr, where))

    for addr, where in benign:
        print("   data  %08x  in %s (%s)"
              % (addr, where[1], where[2].rsplit("/", 1)[-1]))
    for addr, where in real:
        print("   CMOV  %08x  in %s (%s)"
              % (addr, where[1], where[2].rsplit("/", 1)[-1]))
    if real:
        print("   FAIL: %d CMOV in code this build compiled -- check -5 is "
              "still set" % len(real))
        return 1
    print("   ok: none in our own code (%d in library data, listed above)"
          % len(benign))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
