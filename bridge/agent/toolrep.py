"""What the guest reported about its own tools, parsed.

The guest looks and reports; this reads the report. Nothing here decides what
a fact MEANS -- that is `toolrules` next door. Keeping the two apart is what
lets the interpretation change without a guest rebuild, and lets it be tested
without a Windows machine.

Wire shape, one per line, the same as the sync manifest and for the same
reason (`win/sync/sync.cpp`'s `rc_sync_manifest_row`):

    <name> <kind> <bytelen> <value>\\n

The value carries its own byte count because it is free-form: a Windows path
contains spaces (`C:\\Program Files\\...`) and an environment variable's value
can contain very nearly anything. Splitting on a separator that can appear
inside the field is how the manifest format got its length prefix, and this
inherits both the shape and the reason.

`kind` is `env` (value is `NAME=contents`) or `file` (value is a path that
exists). Anything else is skipped rather than guessed at.

REFUSING IS NOT AN OPTION HERE. A malformed row costs one missing inventory
line; refusing the connection over it costs the user their machine. So a bad
row is logged and skipped, and a body that is entirely unparseable yields an
empty list -- the same posture `bridge/files/manifest.py` takes, and the
opposite of `Baseline`'s, which refuses because the alternative is data loss.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

log = logging.getLogger("bridge.toolrep")

#: A guest that reported more than this is either broken or malicious; either
#: way the prompt cannot carry it. Matches the guest's own section cap with
#: room for several facts each.
MAX_FACTS = 128


@dataclass(frozen=True)
class Fact:
    """One thing the machine said about itself.

    `name` is the INI section it came from and is a LABEL, never an
    assertion: the INI is user-editable, so `[watcom]` means "the user calls
    this watcom", not "this is Open Watcom". Rules downstream treat it as a
    key to look up, and the prompt says the evidence rather than the
    conclusion.
    """

    name: str
    kind: str       # "env" or "file"
    value: str

    @property
    def var(self) -> str:
        """For an `env` fact, the variable name. "" for anything else."""
        if self.kind != "env" or "=" not in self.value:
            return ""
        return self.value.split("=", 1)[0]

    @property
    def contents(self) -> str:
        """For an `env` fact, the variable's value. "" for anything else."""
        if self.kind != "env" or "=" not in self.value:
            return ""
        return self.value.split("=", 1)[1]


def parse_rows(body: bytes, caps=None) -> list[Fact]:
    """Facts from a HELLO body. Never raises.

    `caps` supplies the guest's codepage so a path with a high byte in it
    decodes the way the machine meant it. It is optional because a body can
    arrive before caps are settled, and `latin-1` round-trips every byte
    rather than failing -- a mangled path in a prompt is survivable, an
    exception during the handshake is not.
    """
    codec = "latin-1"
    if caps is not None:
        codec = getattr(caps, "acp", None) or "latin-1"

    out: list[Fact] = []
    i, n = 0, len(body)
    while i < n and len(out) < MAX_FACTS:
        nl = body.find(b"\n", i)
        stop = n if nl < 0 else nl
        try:
            head = body[i:stop]
            if not head.strip():
                i = stop + 1
                continue
            name, kind, rest = head.split(b" ", 2)
            length, _, value = rest.partition(b" ")
            want = int(length)
            # The stated length is authoritative. If it disagrees with what
            # is actually there the row is corrupt, and a row whose length
            # cannot be trusted is exactly the case the prefix exists to
            # catch -- so skip it rather than take a prefix of it.
            if want != len(value):
                raise ValueError(f"says {want} bytes, carries {len(value)}")
            out.append(Fact(
                name=name.decode(codec, "replace"),
                kind=kind.decode(codec, "replace"),
                value=value.decode(codec, "replace"),
            ))
        except Exception as exc:
            log.warning("tool report: skipping a row (%s): %r",
                        exc, body[i:stop][:80])
        i = stop + 1

    if len(out) >= MAX_FACTS and i < n:
        log.warning("tool report: stopped at %d facts; the rest was ignored",
                    MAX_FACTS)
    return [f for f in out if f.kind in ("env", "file")]
