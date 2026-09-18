"""The manifest row format, parsed in exactly one place.

The format was written four times and verified zero times: `win/syncrpc.c`
produces it, `bridge/server.py` parsed it inline, `tests/python/test_sync_wire.py`
re-implemented the producer for the tests, and `tools/syncprobe.py`
re-implemented the parser. `synccheck.exe` -- the only thing that runs the real
C against the real Python -- printed a *different*, tab-separated format of its
own, so it proved the classifier and the checksums and never touched the wire
format. A field added on one side passed the whole suite.

Pulling the parser out of the frame handler is half the fix. The other half is
`synccheck --wire`, which now emits rows through the SAME C function the guest
puts on the wire, so `tests/python/test_guest_manifest.py` can feed real guest
output to this real parser and compare the result against the bridge's own
classification. The loop is closed: a field added on either side fails.

Row format, both versions:

    v2: <type> <cls> <size> <mtime> <crc_norm> <crc_raw> <pathlen> <SP><bytes>
    v1: <type> <cls> <size> <mtime> <crc>                <pathlen> <SP><bytes>

Length-prefixed rather than separated, because TAB and newline are legal in a
Linux filename and a separator-based format breaks on the first one.
"""

from __future__ import annotations

import logging

from . import paths as P
from .sync import Entry, Type

log = logging.getLogger("bridge.manifest")


def parse_rows(body: bytes, caps) -> tuple[dict, dict]:
    """Parse a SYNCENT body into `{key: Entry}` and `{key: display name}`.

    Unparseable rows are logged and skipped rather than raising: one bad row
    must not cost the whole manifest. The CALLER still checks the count
    against `files=` in SYNCEND, which is what turns a dropped row into a
    refused pass instead of a silent mass deletion.
    """
    rows: dict = {}
    display: dict = {}

    for line in body.split(b"\n"):
        if not line:
            continue
        try:
            # Seven fields, or six from a client that predates
            # .gitattributes support. A client sending one checksum cannot
            # have a class overridden from here -- it computed that number for
            # its own classification and there is no way to derive the other --
            # so both slots take the same value and `effective_class` refuses
            # to move it.
            if caps.manifest >= 2:
                parts = line.split(b" ", 6)
                crc_norm, crc_raw = parts[4], parts[5]
                plen_and_path = parts[6]
            else:
                parts = line.split(b" ", 5)
                crc_norm = crc_raw = parts[4]
                plen_and_path = parts[5]
            typ, cls, size, mtime = parts[0], parts[1], parts[2], parts[3]
            sp = plen_and_path.index(b" ")
            plen = int(plen_and_path[:sp])
            raw = plen_and_path[sp + 1:sp + 1 + plen]
        except (ValueError, IndexError):
            log.warning("unparseable manifest line: %r", line[:80])
            continue

        # surrogateescape, never "replace": the five undefined CP1252
        # positions are legal in a VFAT long name, and "replace" maps them all
        # to U+FFFD -- so two distinct files get one key, one of them silently
        # stops syncing, and neither name can be encoded back to ask the guest
        # for the file.
        rel = P.wire_decode(raw, acp=caps.acp)
        key = P.canonical(rel, caps=caps)
        auto = cls.decode() if cls != b"-" else P.CLASS_TEXT
        rows[key] = Entry(
            type=Type(typ.decode()),
            cls=auto,
            mtime=float(mtime),
            crc=int(P.crc_for(auto, crc_norm, crc_raw)),
            crc_norm=int(crc_norm), crc_raw=int(crc_raw),
            dual=caps.manifest >= 2,
            display=rel)
        display[key] = rel

    return rows, display
