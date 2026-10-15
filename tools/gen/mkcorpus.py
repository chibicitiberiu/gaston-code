#!/usr/bin/env python3
"""Build CORPUS.TXT: the deflate corpus that gate 0.5c has been waiting for.

`win/checks/probe.c`'s deflate sample is one two-line block repeated. It
compresses about 164:1 and the comment beside it has asked for this file
since the gate was written: "a realistic corpus (the project's own prose and
code, which measured 2.16:1) read from a file next to the exe".

So the corpus is this project's own prose and code, which is both
representative of what the sync lane actually carries and reproducible from
the tree. Reproducible matters more than it sounds: a throughput number is
only comparable against another run of the SAME bytes, and the two machines
this gate needs -- the P200 and the QEMU guest -- are measured days apart.

Deterministic by construction:

  * the file list is explicit and sorted, never a directory walk, so adding
    a source file does not silently change the corpus
  * line endings are forced to CRLF, because the same text with LF is
    different bytes and deflate notices
  * it stops on a line boundary at the ceiling below, so the tail is not
    half a token

THE CEILING IS NOT ARBITRARY. `probe.c` reads this into a 256 KB static
(`static char src[256 * 1024]`), and growing that buffer costs
committed-at-load bytes on a machine with 32 MB. So the corpus is sized to
the buffer rather than the buffer to the corpus.

    python3 tools/gen/mkcorpus.py            # write vm/tools-stage/CORPUS.TXT
    python3 tools/gen/mkcorpus.py --check    # report only, write nothing
"""
from __future__ import annotations

import argparse
import os
import sys
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))

# 256 KB, matching probe.c's src[]. One byte over and the probe would have to
# refuse the file, which is the right behaviour there and a build error here.
CEILING = 256 * 1024

# Prose first, then C, then C++, then Python: the order a reader would expect,
# and it puts the most compressible material at the front where a partial read
# would still be representative. Every path is relative to the repo root and
# every one is checked to exist, because a silently missing file would change
# the corpus and nothing would say so.
SOURCES = [
    "docs/PLAN.md",
    "docs/REMOTE.md",
    "docs/UIAUTOMATION.md",
    "win/net/caps.h",
    "win/core/rc_cpp.h",
    "win/checks/probe.c",
    "win/automation/input.cpp",
    "win/remote/server.cpp",
    "win/ui/uiengine.cpp",
    "bridge/files/caps.py",
    "bridge/agent/toolrules.py",
]


def build() -> tuple[bytes, list[tuple[str, int]]]:
    """Concatenate a bounded slice of each source, CRLF, under the ceiling.

    EACH FILE GETS AN EQUAL BUDGET rather than the list being read until it
    runs out. Read in order, `docs/PLAN.md` alone fills half the buffer and
    nothing past the third file ever appears -- which would make the list a
    lie and the corpus pure prose, when the thing being measured is prose AND
    code. A per-file budget costs nothing and keeps every listed file in.
    """
    budget = CEILING // len(SOURCES)
    out = bytearray()
    used: list[tuple[str, int]] = []
    for rel in SOURCES:
        path = os.path.join(REPO, rel)
        if not os.path.isfile(path):
            sys.exit(f"mkcorpus: {rel} is missing; the corpus list is stale")
        with open(path, "rb") as fh:
            raw = fh.read()
        # Normalise to LF first so a file that is already CRLF does not become
        # CR CR LF, then to CRLF. Doing it in one pass would double them.
        text = raw.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
        took = 0
        for line in text.split(b"\n"):
            chunk = line + b"\r\n"
            if took + len(chunk) > budget or len(out) + len(chunk) > CEILING:
                break
            out += chunk
            took += len(chunk)
        used.append((rel, took))
    return bytes(out), used


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true",
                    help="report the corpus without writing it")
    ap.add_argument("-o", "--out",
                    default=os.path.join(REPO, "vm", "tools-stage",
                                         "CORPUS.TXT"))
    a = ap.parse_args()

    data, used = build()
    # Level 6 is miniz's default and what the sync lane actually sends at.
    ratio = len(data) / max(1, len(zlib.compress(data, 6)))

    print(f"mkcorpus: {len(data)} bytes of {CEILING} "
          f"({len(data) * 100 // CEILING}% of probe.c's src[])")
    for rel, n in used:
        print(f"  {n:>7}  {rel}")
    print(f"mkcorpus: compresses {ratio:.2f}:1 at level 6")
    # MEASURED 2026-10-15: 234,481 bytes at 2.76:1, with the eleven sources
    # above. That is higher than the 2.16:1 probe.c's comment cites, and the
    # reason is this project's prose: three of the eleven files are documents
    # that repeat their own vocabulary heavily. It is left at 2.76 rather than
    # tuned down to 2.16, because tuning a corpus until it hits a remembered
    # number is how the 164:1 sample got believed in the first place. What
    # matters is that it is real text, reproducible byte for byte, and nowhere
    # near pure repetition.
    #
    # The band below is a DRIFT ALARM, not a specification: it fires when the
    # file list has changed enough to stop being comparable with the runs this
    # gate closes on, so that a later reader is not silently comparing two
    # different corpora.
    if not 1.8 <= ratio <= 3.2:
        print(f"mkcorpus: WARNING -- {ratio:.2f}:1 is outside the 1.8-3.2 band "
              "this corpus measured 2.76:1 in; the file list has drifted far "
              "enough that older runs are no longer comparable",
              file=sys.stderr)

    if a.check:
        return 0
    os.makedirs(os.path.dirname(a.out), exist_ok=True)
    with open(a.out, "wb") as fh:
        fh.write(data)
    print(f"wrote {a.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
