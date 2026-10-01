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

A NOTE IS ABOUT A VERSION, NOT ABOUT A PRODUCT, and until 2026-10-01 there
was nowhere to say so. The only defence against describing a toolchain the
note was not measured on was to REFUSE TO DETECT IT: `jdk.toml` lists 1.1-era
install paths and nothing later, with a comment saying why -- its body is a
list of what Java 1.1 lacks, and that list is "confidently wrong in front of
a 1.2+ install where half of it came back". So a JDK 1.3 on the machine was
reported as no JDK at all. `msvc.toml` does the same thing from the other
end: it maps `\\VC98\\` to 6.0 and deliberately declares no `default`,
because the 16-bit `C:\\MSVC` and `C:\\MSDEV` markers would be mislabelled.

Now a body may declare the version it was measured against (`for_version`),
a profile may carry more than one body (`[[note.variant]]`), and `resolve`
picks the nearest -- SAYING SO when the nearest is not the one asked for. The
disclosure is the point. Silently approximate is worse than refusing, because
an agent cannot tell which it got.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from . import profiles

log = logging.getLogger("bridge.toolnotes")

#: A version is a leading run of dot-separated integers, with an optional `v`.
#: Everything after the digits is dropped, which is what makes `6.0a` and the
#: JDK's own `JDK1.1.3/jws:97.08.28` both land somewhere sane instead of
#: being parsed component-by-component into `(1, 1, 3, 97, 8, 28)`.
_VERSION = re.compile(r"\s*v?(\d+(?:\.\d+)*)")


def parse_version(s: str) -> tuple[int, ...] | None:
    """`"1.1.3"` -> `(1, 1, 3)`, or None if there is no version in there.

    None is a real answer and callers must handle it: `VC98`, `6.0a`'s
    sibling `a`, an empty string and a model typing "latest" all land here,
    and the honest response to an unplaceable version is to say it was
    ignored rather than to guess which release it meant.
    """
    m = _VERSION.match(s or "")
    if not m:
        return None
    return tuple(int(p) for p in m.group(1).split("."))


def _pad(a: tuple[int, ...], b: tuple[int, ...]) -> tuple[tuple, tuple]:
    n = max(len(a), len(b))
    return a + (0,) * (n - len(a)), b + (0,) * (n - len(b))


def covers(have: tuple[int, ...], want: tuple[int, ...]) -> bool:
    """Is a body measured on `have` entitled to speak for `want`?

    Two ways yes, and the second is the one that matters:

      * They are the same number. `6` and `6.0` are the same release
        written two ways, so padding with zeros before comparing is
        correct rather than lax.
      * `have` is a PREFIX of `want`, strictly shorter. A note that says it
        was measured on `1.1` is making a claim about the 1.1 line, so it
        covers 1.1.3 and 1.1.8 without qualification -- which is exactly
        what `jdk.toml`'s title ("Sun JDK 1.1") already asserts in prose.

    Note the asymmetry, and it is deliberate: `1.1` covers `1.1.8`, and
    `1.1.8` does NOT cover `1.1`. A body measured on one point release is
    not a statement about the line it came from.
    """
    pa, pb = _pad(have, want)
    if pa == pb:
        return True
    return len(have) < len(want) and want[:len(have)] == have


def distance(have: tuple[int, ...], want: tuple[int, ...]) -> tuple[int, ...]:
    """How far apart two versions are, MAJOR FIRST, as a sortable tuple.

    Component-wise absolute difference, compared lexicographically -- so a
    difference in the major number beats any difference in the minor, and a
    minor beats any patch. This is not arithmetic distance and it should not
    be: with 1.1 and 2.0 documented and 1.9 asked about, plain subtraction
    picks 2.0 (0.1 away against 0.8) and this picks 1.1, because the thing
    that breaks a build is crossing a major release, not counting minors.
    Java 1.1 -> 1.2 moved the whole collections framework; 1.1.3 -> 1.1.8
    moved almost nothing.
    """
    pa, pb = _pad(have, want)
    return tuple(abs(x - y) for x, y in zip(pa, pb, strict=True))


@dataclass(frozen=True)
class Body:
    """One measured body and the version it was measured against.

    `version` is "" for the body in `[note]` itself when that profile
    declares no `for_version`, which is every shipped profile as of
    2026-10-01. An empty `version` means NO CLAIM, and a body that claims
    nothing can never be reported as an inexact match -- there is nothing to
    compare. That is the whole of why adding the key is optional and why
    leaving it off keeps today's output byte for byte.

    `body` is written against `{root}`, never a literal path. The point of
    detection is finding a toolchain somewhere other than the obvious place,
    so a note that hardcodes a path contradicts the inventory line above it
    the moment someone installs elsewhere.
    """

    version: str
    title: str
    #: Which machine, and which version of the tool. PER BODY, never
    #: inherited: a variant measured somewhere else that borrowed the base's
    #: `measured` would be laundering provenance, which is the one thing
    #: this module exists to prevent. `_build` drops a variant that has none.
    measured: str
    body: str

    def render(self, root: str, version: str = "") -> str:
        return (self.body.replace("{root}", root)
                         .replace("{version}", version)
                         .strip("\n"))


@dataclass(frozen=True)
class Resolved:
    """Which body answers for a version, and whether it really does.

    `exact` is False ONLY when a body was picked that was measured against a
    different version than the one in play. It is True when no version was
    in play at all, and True when the profile makes no version claim -- in
    both of those cases there is nothing to disclose, because nothing was
    approximated. Callers must say something when it is False: see
    `tools.toolnote.view.ahead` / `.behind`.
    """

    body: Body
    #: The version string that was in play: the caller's argument, or the one
    #: detection reported. "" if neither.
    asked: str
    exact: bool
    #: "" when exact. "ahead" when `asked` is NEWER than the body -- the
    #: dangerous direction, where the note's absences are absences of the
    #: past. "behind" when the body documents a later release than `asked`.
    direction: str = ""
    #: False when `asked` was given and could not be parsed as a version at
    #: all. The argument is then ignored, and the caller says so rather than
    #: pretending it was honoured.
    understood: bool = True


@dataclass(frozen=True)
class Note:
    """One tool's facts, in one body per version anybody has measured."""

    title: str
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
    #: Which version `body` was measured against, as a number this module can
    #: compare -- `[note] for_version`, optional.
    #:
    #: NOT `[detect.version].default`, although they will often read the
    #: same. That one answers "which version do we assume when no marker
    #: pins it", a detection question; this one answers "which version is
    #: this prose true of", a provenance question. `msvc.toml` has the two
    #: pulling opposite ways already: it declares no default on purpose,
    #: because its older markers are 16-bit installs that "6.0" would
    #: mislabel, while its note is unambiguously measured on 6.0.
    for_version: str = ""
    #: Further bodies, one per other version measured -- `[[note.variant]]`.
    #: Empty for every shipped profile today, which is why nothing in the
    #: tree changes shape until somebody measures a second release.
    variants: tuple[Body, ...] = field(default_factory=tuple)

    @property
    def headline(self) -> str:
        return (self.headline_text or self.title).strip()

    @property
    def base(self) -> Body:
        """The body in `[note]` itself."""
        return Body(version=self.for_version, title=self.title,
                    measured=self.measured, body=self.body)

    @property
    def bodies(self) -> tuple[Body, ...]:
        """Every body, base first. Ordered for readability, not for picking."""
        out = [self.base] if self.body.strip() else []
        return tuple(out) + tuple(self.variants)

    @property
    def versions(self) -> tuple[str, ...]:
        """Every version this note actually documents, lowest first."""
        got = [(parse_version(b.version), b.version) for b in self.bodies]
        return tuple(v for _, v in sorted((k, v) for k, v in got if k))

    def render(self, root: str, version: str = "") -> str:
        """The base body, as it has always been rendered.

        Unchanged on purpose: the resident headline and every caller that
        does not care about versions goes through here, and `resolve` is the
        opt-in path for the one caller that does.
        """
        return self.base.render(root, version or self.default_version)

    def resolve(self, version: str = "") -> Resolved:
        """The body nearest `version`, and whether it is really that version.

        THE ORDERING RULE, in full, because being approximate quietly is the
        failure this exists to prevent:

          1. No version in play, or no body in this profile declares one:
             the base body, `exact=True`. Nothing was approximated, so
             nothing is disclosed. This is every shipped profile today.
          2. A version that `parse_version` cannot place: same answer, with
             `understood=False` so the caller can say the argument was
             dropped. Guessing which release "latest" meant would be worse.
          3. A body that `covers` it (same number, or a shorter prefix of
             it): that body, `exact=True`.
          4. Otherwise the body at the smallest `distance`, major component
             first; ties to the LOWER version. `exact=False`, and
             `direction` says which way the gap runs.

        THE TIE GOES DOWN because the two directions fail differently. A
        note measured on an older release describes a subset of what is
        there: its "no ArrayList" is wrong, and wrong in a way that costs an
        agent nothing but opportunity. A note measured on a NEWER one tells
        the agent to use things that do not exist yet, which fails at the
        compiler. Neither is acceptable undisclosed, which is why both carry
        a warning rather than why one is preferred.
        """
        asked = (version or "").strip()
        want = parse_version(asked)
        cands: list[tuple[tuple[int, ...], Body]] = []
        for b in self.bodies:
            v = parse_version(b.version)
            if v:
                cands.append((v, b))
        fallback = self.base
        if not self.body.strip() and cands:
            # A profile that is ALL variants and no base. Serve the latest
            # thing measured rather than an empty note.
            fallback = max(cands, key=lambda cb: cb[0])[1]

        if not asked:
            return Resolved(fallback, "", True)
        if want is None:
            return Resolved(fallback, asked, True, understood=False)
        if not cands:
            return Resolved(fallback, asked, True)

        for have, b in cands:
            if covers(have, want):
                return Resolved(b, asked, True)

        have, b = min(cands, key=lambda cb: (distance(cb[0], want),
                                             _pad(cb[0], want)[0]))
        padded_have, padded_want = _pad(have, want)
        ahead = padded_have < padded_want
        return Resolved(b, asked, False, "ahead" if ahead else "behind")


def _variants(name: str, n: dict, title: str) -> tuple[Body, ...]:
    """`[[note.variant]]` -> bodies, dropping any that is not measured.

    REFUSING ONE IS NOT REFUSING THE PROFILE, the same posture `profiles`
    takes for a file that will not parse: a bad variant costs one version's
    knowledge, and raising here costs the user their machine. It is logged
    by name, because the failure to catch is a variant that silently stops
    existing and a version that silently stops resolving.
    """
    out: list[Body] = []
    for v in (n.get("variant") or ()):
        if not isinstance(v, dict):
            log.error("profile %s: a [[note.variant]] is not a table; "
                      "skipping it", name)
            continue
        ver = str(v.get("version", "")).strip()
        body = str(v.get("body", ""))
        measured = str(v.get("measured", "")).strip()
        if not ver or not body.strip():
            log.error("profile %s: a [[note.variant]] has no %s; skipping it",
                      name, "version" if not ver else "body")
            continue
        if parse_version(ver) is None:
            log.error("profile %s: [[note.variant]] version %r is not a "
                      "version number, so nothing could ever resolve to it; "
                      "skipping it", name, ver)
            continue
        if not measured:
            # DETECTION MAY GUESS; NOTES MAY NOT, and a variant inheriting
            # the base's `measured` would claim a machine it was never seen
            # on. Dropping it is the only honest option.
            log.error("profile %s: [[note.variant]] %s says nothing about "
                      "where it was measured; skipping it, because a body "
                      "without provenance is the one thing a note may not "
                      "be", name, ver)
            continue
        out.append(Body(version=ver,
                        title=str(v.get("title") or title),
                        measured=measured,
                        body=body))
    return tuple(out)


def _build() -> dict[str, Note]:
    out: dict[str, Note] = {}
    for name in profiles.names():
        n = profiles.note(name)
        if not n:
            continue
        d = profiles.detect(name)
        vinfo = d.get("version", {})
        default_ver = str(vinfo.get("default", "")) if isinstance(vinfo, dict) else ""
        title = n.get("title", name)
        out[name] = Note(title=title,
                         measured=n.get("measured", ""),
                         body=n.get("body", ""),
                         default_version=default_ver,
                         headline_text=n.get("headline", ""),
                         for_version=str(n.get("for_version", "")).strip(),
                         variants=_variants(name, n, title))
    return out


#: Keyed by the name a detection rule reports, which is the profile filename.
#: A note therefore cannot exist for a tool nothing can detect -- the two are
#: the same file.
NOTES: dict[str, Note] = _build()
