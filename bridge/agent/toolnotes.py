"""What is known about the tools on the machine at the other end.

THE TEXT LIVES IN `bridge/profiles/<tool>.toml`, under `[note]`. This module
is only the shape it is loaded into, and the rules about what may be in one.

Detection decides *when* a note is shown; the profile decides what it
*says*. Those are separate jobs and they rot differently: a detection rule
that guesses wrong costs one unused inventory line, and a note that is wrong
costs the assistant acting confidently on folklore -- under an instruction
not to go and check.

So: **detection may guess, notes may not.** A note ships only for something
measured on a real machine and carries `measured` naming which machine and
which version. A test asserts that field is present, because the day it
becomes optional is the day the first unmeasured note arrives.

`measured` NAMES A DATE FOR A REASON. A fact does not stay true. The corpus
that produced these contained a correctly-measured `PATH` that had since
become false, because installing a toolchain changed it -- and it was written
into a prompt as fact before anyone re-checked. See `docs/PROGRESS.md`.
"""

from __future__ import annotations

from dataclasses import dataclass

from . import profiles


@dataclass(frozen=True)
class Note:
    """One tool's facts.

    `body` is written against `{root}`, never a literal path. The whole point
    of detection is finding a toolchain somewhere other than the obvious
    place, so a note that hardcodes a path contradicts the inventory line
    above it the moment someone installs elsewhere -- two authoritative
    sources disagreeing, with the more detailed one wrong.
    """

    title: str
    #: Which machine, and which version of the tool. Not a date alone: a
    #: layout fact true of one release is false of the next.
    measured: str
    body: str

    def render(self, root: str) -> str:
        return self.body.replace("{root}", root).strip("\n")


def _build() -> dict[str, Note]:
    out: dict[str, Note] = {}
    for name in profiles.names():
        n = profiles.note(name)
        if not n:
            continue
        out[name] = Note(title=n.get("title", name),
                         measured=n.get("measured", ""),
                         body=n.get("body", ""))
    return out


#: Keyed by the name a detection rule reports, which is the profile filename.
#: A note therefore cannot exist for a tool nothing can detect -- the two are
#: the same file.
NOTES: dict[str, Note] = _build()
