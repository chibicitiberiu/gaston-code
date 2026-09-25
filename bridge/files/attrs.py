"""Per-path overrides for how content is compared and converted.

The classifier in `paths.py` decides from content alone, and content is not
always enough. A Makefile for a cross toolchain has to keep LF on a machine
whose every other text file is CRLF. A `.patch` is a file whose line endings
ARE its content. A fixture exists precisely to hold the bytes nobody should
tidy. In all three the automatic answer is defensible and wrong, and -- this is
what makes it worth code -- **wrong silently**: converting a uniform text file
does not change its normalised CRC, so the file is rewritten on the user's
machine and nothing anywhere reports it.

A source tree already states this. `.gitattributes` is where a project says
which files are text, which are binary and what endings they take, it is
already correct because the build depends on it, and re-deriving it by hand is
both work and a second opinion waiting to disagree. So it is read natively,
with the same nesting rules git uses, alongside `.gastonattributes` for trees
that want to say something to sync without saying it to git.

Three attributes are honoured; anything else is parsed and ignored, so an
ordinary `.gitattributes` full of `diff=`, `merge=` and `filter=` does not have
to be edited to be usable here.

    *.mk      -text        byte-exact: never convert, both sides hold the
                           same bytes, and a difference is a real difference
    *.bin     binary       git's macro for `-text -diff -merge`; the `-text`
                           half is what matters here
    Makefile  eol=lf       still compared as text, so an ending-only change is
                           not an edit -- but written with LF on the client
    *.txt     eol=crlf     written with CRLF even on an LF-native client
    *.py      text         treat as text even when the content is ambiguous,
                           subject to the safety rule below

**`text` cannot force the unsafe cases.** Normalisation has to be idempotent or
the algorithm oscillates forever -- `N("\\r\\r\\n") == "\\r\\n"` and
`N("\\r\\n") == "\\n"` -- so a file containing a lone CR, a `\\r\\r\\n`, or mixed
endings stays byte-exact whatever the attributes say, and the override is
reported rather than applied. `-text` has no such limit: refusing to touch
something is always safe.

**`-text` and `eol=` are different requests and the difference matters.**
`eol=lf` keeps the path in the text class, where the two sides are compared
after folding CRLF to LF, so the file may legitimately differ in endings
between them and only the *content* has to agree. `-text` says the bytes
themselves must match on both sides -- so applying it to a file that is
currently LF here and CRLF there makes them genuinely different, one side wins
(the user's), and the other is rewritten. That is the correct reading of
`-text` and it is not what someone reaching for "keep my Makefile LF" wants.
`class_change_report` exists so they find out before it happens rather than
after.
"""

from __future__ import annotations

import os

from . import paths as P
from .ignores import _Rule

#: Files consulted, in the order git consults them. `.gastonattributes` last so
#: a tree can override what its `.gitattributes` says without editing it.
FILES = (".gitattributes", ".gastonattributes")

#: What `eol=` may say, mapped to the `caps.eol` enum `paths._EOL` is indexed
#: by. A value outside this table is ignored rather than guessed at.
#:
#: `cr` is absent deliberately, and refusing it at the handshake is not enough:
#: this table is consulted per PATH, so `*.txt eol=cr` in a real
#: .gitattributes would reach a CRLF client that declared itself perfectly
#: legally. The guest's normaliser folds only CRLF, so a lone-CR file's
#: crc_norm still contains the CRs, the compare-and-swap never matches, and
#: the path fails on every pass forever. Ignoring the word leaves the file
#: alone, which is the honest answer until someone implements it.
EOL_NAMES = {"lf": 0, "crlf": 1}


class PathAttrs:
    """The attributes in force for one path. Absent is not the same as unset:
    `text is None` means nobody said, which is what lets a later file override
    an earlier one without having to re-state everything."""

    __slots__ = ("eol", "text")

    def __init__(self, text=None, eol=None):
        #: True (force text), False (force byte-exact), or None (decide by
        #: content, which is what almost every path does).
        self.text = text
        #: An index into `paths._EOL`, or None for the client's own default.
        self.eol = eol

    def __repr__(self) -> str:      # pragma: no cover - debugging only
        return f"PathAttrs(text={self.text!r}, eol={self.eol!r})"

    @property
    def stated(self) -> bool:
        return self.text is not None or self.eol is not None


def _parse_attr(word: str, into: PathAttrs) -> None:
    """One whitespace-separated attribute word. Unknown words are ignored:
    a real `.gitattributes` is mostly `diff=`, `merge=` and `filter=`, and
    refusing to read the file because of them would make the feature useless
    on exactly the trees that have one."""
    if word == "text" or word == "text=auto":
        # `text=auto` is git's "decide by content", which is our default -- but
        # it still means "this is a candidate for conversion", so it lands the
        # same way and the safety rule does the deciding.
        into.text = True
    elif word == "-text" or word == "binary":
        into.text = False
    elif word.startswith("eol="):
        idx = EOL_NAMES.get(word[4:].lower())
        if idx is not None:
            into.eol = idx
            # git implies `text` from `eol=`: asking for an ending only means
            # something for a file that is being converted at all.
            if into.text is None:
                into.text = True


class Attrs:
    """The rule set for one tree. Call `of(rel)` for a path's attributes."""

    def __init__(self, rules: list, sources: list[str]):
        self._rules = rules
        #: Which files the rules came from, so a change to one is recognisable
        #: as a change of intent rather than as edits appearing from nowhere.
        self.sources = sources

    def of(self, rel: str) -> PathAttrs:
        out = PathAttrs()
        # Later rules win, per attribute rather than per line, which is what
        # lets `*.txt eol=crlf` sit above `notes.txt -text` and both apply.
        for rule, attrs in self._rules:
            if rule.matches(rel.rstrip("/"), rel.endswith("/")):
                if attrs.text is not None:
                    out.text = attrs.text
                if attrs.eol is not None:
                    out.eol = attrs.eol
        return out

    def __len__(self) -> int:
        return len(self._rules)

    # --- what the sync layer actually asks --------------------------------

    def effective_class(self, rel: str, auto: str) -> tuple[str, str | None]:
        """The class to compare and transfer this path under, and why not.

        `auto` is what the content says. The return is the class to use plus a
        reason the override could NOT be honoured, or None when nothing was
        refused. A refusal is reported rather than swallowed: a user who wrote
        `-text` and got text behaviour has to be told, or they will trust a
        guarantee they do not have.
        """
        a = self.of(rel)
        if a.text is False:
            return P.CLASS_EXACT, None          # always safe
        if a.text is True and auto != P.CLASS_TEXT:
            return (P.CLASS_EXACT,
                    "marked text, but it holds a lone CR, a CRLF/LF mix or a "
                    "NUL -- folding those is not reversible, so it is kept "
                    "byte-exact and its endings are left alone")
        return auto, None

    def eol_for(self, rel: str, default: int) -> int:
        """Which ending this path takes on the client, `default` being the
        client's own convention from the handshake."""
        eol = self.of(rel).eol
        return default if eol is None else eol


def load(root: str) -> Attrs:
    """Collect the rules for a tree.

    Nested, because git applies them that way: a `tests/fixtures/` that states
    its own endings is the single most likely place for this file to appear,
    and honouring only the root one would quietly ignore it.

    One walk, not two: `_sources` used to re-walk the whole tree afterwards
    just to name the files `load` had already opened, so a single sync pass
    paid for this tree twice before `ignores.load` or `scan_bridge` had even
    started theirs. docs/audit/02-bridge-python.md 02.17(c).
    """
    rules: list = []
    sources: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in (".git", ".hg", ".svn")]
        base = os.path.relpath(dirpath, root).replace(os.sep, "/")
        base = "" if base == "." else base
        for name in FILES:
            if name not in filenames:
                continue
            full = os.path.join(dirpath, name)
            rules.extend(_read(full, base))
            sources.append(full)
    return Attrs(rules, sources)


def _read(full: str, base: str) -> list:
    try:
        with open(full, encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except OSError:
        return []
    out = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        pat, words = parts[0], parts[1:]
        if pat.startswith("!"):
            # gitattributes has no negation; a line starting with ! is an
            # error in git and is skipped rather than read as a pattern.
            continue
        attrs = PathAttrs()
        for w in words:
            _parse_attr(w, attrs)
        if attrs.stated:
            out.append((_Rule(pat, base), attrs))
    return out
