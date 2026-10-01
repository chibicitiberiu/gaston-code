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
    default_version: str = ""
    #: ONE LINE, resident where the body is not. The body is fetched now
    #: (`mcp__machine__toolnote`) rather than printed on every request, and
    #: a fetch nothing advertises never happens -- so each detected tool
    #: still costs one line saying what it is and that a note exists.
    #:
    #: Optional in the profile, because the profiles are measured text and
    #: this is a presentation detail. Absent, `title` stands in: it is
    #: already one short line naming the product and its version, which is
    #: the minimum the agent needs to decide whether to ask. A profile that
    #: wants to spend its line better adds `headline` under `[note]`.
    headline_text: str = ""

    @property
    def headline(self) -> str:
        return (self.headline_text or self.title).strip()

    def render(self, root: str, version: str = "") -> str:
        v = version or self.default_version
        return (self.body.replace("{root}", root)
                         .replace("{version}", v)
                         .strip("\n"))


def _build() -> dict[str, Note]:
    out: dict[str, Note] = {}
    for name in profiles.names():
        n = profiles.note(name)
        if not n:
            continue
        d = profiles.detect(name)
        vinfo = d.get("version", {})
        default_ver = str(vinfo.get("default", "")) if isinstance(vinfo, dict) else ""
        out[name] = Note(title=n.get("title", name),
                         measured=n.get("measured", ""),
                         body=n.get("body", ""),
                         default_version=default_ver,
                         headline_text=n.get("headline", ""))
    return out


#: Keyed by the name a detection rule reports, which is the profile filename.
#: A note therefore cannot exist for a tool nothing can detect -- the two are
#: the same file.
NOTES: dict[str, Note] = _build()
