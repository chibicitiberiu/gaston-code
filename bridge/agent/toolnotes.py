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


def line(v: tuple[int, ...]) -> tuple[int, int]:
    """The major.minor `v` belongs to. `(1, 1, 8)` and `(1, 1)` share one.

    Two components because that is where these toolchains put the boundary
    that matters. Java 1.1 -> 1.2 moved the whole collections framework;
    1.1.3 -> 1.1.8 moved almost nothing, and Visual C++ 6.0 -> 6.0a moved a
    service pack. A single-component version is `6` -> `(6, 0)`, so `6` and
    `6.0` share a line as well as being the same number.
    """
    padded = v + (0,) * max(0, 2 - len(v))
    return (padded[0], padded[1])


def nearest(pool: list, want: tuple[int, ...]) -> tuple:
    """The best of `pool` for `want`: LOWER FIRST, in the user's words.

    The greatest candidate at or below `want`; and only if there is none, the
    lowest one above it. `pool` is `[(parsed_version, Body), ...]` and the
    answer is one of its pairs.

    THERE IS NO DISTANCE METRIC HERE AND THE FIRST ATTEMPT AT ONE WAS WRONG.
    It scored the component-wise absolute difference major-first, which is
    nonsense as soon as the majors differ, because the minor numbering
    restarts: asked about 2.0 with 1.1.3 and 1.2.0 documented, it compared
    minor gaps of 1 and 2 and picked 1.1.3 -- the FURTHER of the two from
    2.0, by any reading. Ordinary version ordering has no such hole, and
    "lower first" is both simpler and the rule that was asked for.

    WHY LOWER. The two directions fail differently. A note measured on an
    older release describes a subset of what is there, so its "no ArrayList"
    is wrong in a way that costs an agent opportunity. One measured on a
    NEWER release tells the agent to use things that do not exist yet, which
    fails at the compiler. Neither is acceptable undisclosed, which is why
    both carry a warning -- this only decides which to reach for first.
    """
    below = [cb for cb in pool
             if _pad(cb[0], want)[0] <= _pad(cb[0], want)[1]]
    if below:
        return max(below, key=lambda cb: _pad(cb[0], want)[0])
    return min(pool, key=lambda cb: _pad(cb[0], want)[0])


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


#: The three tiers a match can land in, and they are the three things a
#: caller has to say something different about.
#:
#: A BOOLEAN WAS NOT ENOUGH, which is what this replaced on 2026-10-01. With
#: two outcomes, a body measured on 1.1.3 answering a 1.1.8 machine got the
#: same shouted banner as a 1.1 note answering a 1.3 machine -- and those are
#: not the same event. One is a patch release apart, where the facts are
#: about the same generation of the tool; the other is the case the JDK
#: profile exists as a refusal because of. Spending the loud warning on the
#: first is how an agent learns to skim it before the second arrives.
TIER_EXACT = 1
TIER_PATCH = 2
TIER_NEAREST = 3


@dataclass(frozen=True)
class Resolved:
    """Which body answers for a version, and how well it really does.

    `tier` is the whole of it:

      1 `TIER_EXACT`    -- the body's version covers the one in play, or
                           there was no version in play at all, or the
                           profile makes no version claim. Nothing was
                           approximated, so nothing is disclosed.
      2 `TIER_PATCH`    -- same major.minor, different patch release. One
                           quiet line (`tools.toolnote.view.patch`).
      3 `TIER_NEAREST`  -- the nearest thing measured, and it is a different
                           line. The banner (`.ahead` / `.behind`).

    `exact` survives as a property because most callers only want to know
    whether to say anything at all.
    """

    body: Body
    #: The version string that was in play: the caller's argument, or the one
    #: detection reported. "" if neither.
    asked: str
    tier: int = TIER_EXACT
    #: "" at tier 1. "ahead" when `asked` is NEWER than the body -- the
    #: dangerous direction, where the note's absences are absences of the
    #: past. "behind" when the body documents a later release than `asked`.
    direction: str = ""
    #: False when `asked` was given and could not be parsed as a version at
    #: all. The argument is then ignored, and the caller says so rather than
    #: pretending it was honoured.
    understood: bool = True

    @property
    def exact(self) -> bool:
        return self.tier == TIER_EXACT


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
        """The body nearest `version`, and how near it actually is.

        THREE TIERS, TRIED IN ORDER, and the order is the user's: same
        version, then same minor version, then closest with the lower one
        first. Written out because being approximate quietly is the failure
        this exists to prevent.

        Two cases come before any of it:

          * No version in play, or no body in this profile declares one:
            the base body at `TIER_EXACT`. Nothing was approximated, so
            nothing is disclosed.
          * A version `parse_version` cannot place: the same answer, with
            `understood=False` so the caller can say the argument was
            dropped. Guessing which release "latest" meant would be worse.

        Then:

          1. `TIER_EXACT` -- a body that `covers` the version: the same
             number, or a strictly shorter prefix of it. THE PREFIX STAYS IN
             TIER 1 AND IS NOT A NEAR MISS. A profile writing
             `for_version = "1.1"` is declaring the 1.1 LINE, which is what
             `jdk.toml`'s title already says in prose, so answering a
             machine on 1.1.3 with it is exact and must not be qualified.
             Softening it would put a caveat on the one shipped profile that
             uses this key.
          2. `TIER_PATCH` -- same `line`, i.e. same major.minor, different
             patch, picked with `nearest` out of that line alone. Reached
             when both numbers name a patch level and they differ (`1.1.3`
             answering `1.1.8`), and also when the body is more specific
             than the question (`1.1.3` asked about `1.1`) -- which is the
             other half of `covers` being one-directional.
          3. `TIER_NEAREST` -- `nearest` over everything: the greatest
             version at or below the one in play, or failing that the lowest
             above it.

        TIER 2 IS A FILTER AND IT CHANGES ANSWERS, which is the argument for
        making it a tier of its own rather than letting it emerge. Asked
        about 1.1.2 with 1.0.9 and 1.1.5 measured, "lower first" alone picks
        1.0.9 -- it is below, so it wins, and the agent is handed a note from
        the previous minor line when a note from its OWN line was sitting
        right there. Tier 2 picks 1.1.5 and labels it a patch release ahead.
        Same major.minor beats closest-lower, in that order, because
        crossing a release line is the thing that breaks a build: Java
        1.1 -> 1.2 moved the whole collections framework, and 1.1.3 -> 1.1.8
        moved almost nothing.

        AND IT CHANGES WHAT THE AGENT IS TOLD, which is the other half. A
        patch release apart gets one quiet line; a different line gets the
        banner. Two outcomes could not express that, and spending the banner
        on 1.1.3-answering-1.1.8 is how an agent learns to skim it before
        the 1.1-answering-1.3 case arrives.
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
            return Resolved(fallback, "")
        if want is None:
            return Resolved(fallback, asked, understood=False)
        if not cands:
            return Resolved(fallback, asked)

        # TIER 1: the same version, however it is written.
        for have, b in cands:
            if covers(have, want):
                return Resolved(b, asked)

        def _direction(have: tuple[int, ...]) -> str:
            padded_have, padded_want = _pad(have, want)
            return "ahead" if padded_have < padded_want else "behind"

        # TIER 2: the same minor version, and it is tried BEFORE anything is
        # compared across lines. A candidate from the asked version's own
        # line wins even when one from another line is lower and would
        # therefore have been preferred.
        same_line = [cb for cb in cands if line(cb[0]) == line(want)]
        if same_line:
            have, b = nearest(same_line, want)
            return Resolved(b, asked, TIER_PATCH, _direction(have))

        # TIER 3: closest, lower first.
        have, b = nearest(cands, want)
        return Resolved(b, asked, TIER_NEAREST, _direction(have))


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
