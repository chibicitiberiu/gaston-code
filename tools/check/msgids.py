#!/usr/bin/env python3
"""Fail the build if two private window messages share a number.

Every message this code defines as an offset from WM_APP or WM_USER is found,
evaluated, and required to be unique across the whole of win/. Found the day
core/app.cpp's quit message and net.h's WM_RC_FRAME were both WM_APP + 1.
That is harmless only while they go to different windows, and nothing but
memory keeps it that way: the day one window receives both, a quit request
is dispatched as a frame, or a frame pointer is taken for an exit code.

Globally unique rather than unique per window, because "which windows can
this message reach" is not something a script can know and a reviewer
reliably will not re-check.

A definition this script cannot read is a FAILURE, not a skip. A gate that
quietly misses a spelling it does not understand passes everything written
that way.

Understood spellings, the ones the tree uses:

    #define NAME (WM_APP + 12)
    #define NAME WM_USER + 0x20
    static const UINT NAME = WM_APP + 1;
    NAME = WM_APP + 3,                    (an enum member)

Usage: msgids.py DIR [DIR ...]
"""
from __future__ import annotations

import os
import re
import sys

BASES = {"WM_USER": 0x0400, "WM_APP": 0x8000}

NUM = r"(0[xX][0-9a-fA-F]+|\d+)"
BASE = r"(WM_APP|WM_USER)"
DEFINE = re.compile(
    rf"^\s*#\s*define\s+(\w+)\s+\(?\s*{BASE}\s*\+\s*{NUM}\s*\)?\s*(?:/[*/].*)?$")
CONST = re.compile(
    rf"^\s*(?:static\s+)?const\s+UINT\s+(\w+)\s*=\s*\(?\s*{BASE}\s*\+\s*{NUM}"
    rf"\s*\)?\s*;")
ENUM = re.compile(rf"^\s*(\w+)\s*=\s*\(?\s*{BASE}\s*\+\s*{NUM}\s*\)?\s*,?\s*"
                  rf"(?:/[*/].*)?$")
#: Anything that builds a number from a base. Must match one of the above.
OFFSET = re.compile(rf"\b{BASE}\s*\+")

EXTS = (".h", ".c", ".cpp")


def code_lines(path: str):
    """(lineno, text) with block and line comments blanked out."""
    in_block = False
    with open(path, errors="replace") as f:
        for n, line in enumerate(f, 1):
            out, i = [], 0
            while i < len(line):
                if in_block:
                    end = line.find("*/", i)
                    if end < 0:
                        i = len(line)
                    else:
                        in_block, i = False, end + 2
                elif line.startswith("/*", i):
                    in_block, i = True, i + 2
                elif line.startswith("//", i):
                    break
                else:
                    out.append(line[i])
                    i += 1
            yield n, "".join(out)


def scan(roots: list[str]):
    """Return (definitions, unreadable). A definition is (value, name, where)."""
    defs, unreadable = [], []
    for root in roots:
        for dirpath, dirnames, files in os.walk(root):
            dirnames[:] = sorted(d for d in dirnames if d != "third_party")
            for name in sorted(files):
                if not name.endswith(EXTS):
                    continue
                path = os.path.join(dirpath, name)
                for n, text in code_lines(path):
                    if not OFFSET.search(text):
                        continue
                    where = f"{os.path.relpath(path)}:{n}"
                    m = DEFINE.match(text) or CONST.match(text) or \
                        ENUM.match(text)
                    if not m:
                        unreadable.append((where, text.strip()))
                        continue
                    value = BASES[m.group(2)] + int(m.group(3), 0)
                    defs.append((value, m.group(1), where))
    return defs, unreadable


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print("usage: msgids.py DIR [DIR ...]", file=sys.stderr)
        return 2
    defs, unreadable = scan(argv[1:])

    by_value: dict[int, list[tuple[str, str]]] = {}
    for value, name, where in defs:
        by_value.setdefault(value, []).append((name, where))
    clashes = {v: d for v, d in by_value.items()
               if len({name for name, _ in d}) > 1}

    for where, text in unreadable:
        print(f"   FAIL  {where}: cannot read this message id: {text}")
        print("         spell it `#define NAME (WM_APP + n)`")
    for value, d in sorted(clashes.items()):
        names = ", ".join(f"{name} ({where})" for name, where in d)
        print(f"   FAIL  0x{value:04X} is used by {names}")
    if unreadable or clashes:
        return 1
    print(f"   ok: {len(by_value)} private messages, all distinct")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
