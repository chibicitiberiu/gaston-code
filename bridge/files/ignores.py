"""Which paths are out of the mirror.

`--sync-dir` is mandatory and separate from `--cwd` precisely so that nobody
points this at a repository and discovers `.venv/` and `vm/*.qcow2` arriving on
a machine with 1.7GB free. But a source tree already carries an exact list of
what must not cross -- its `.gitignore` -- and re-deriving that list by hand is
both work and a source of disagreement with the build.

So: `.gitignore` is honoured where present, at any directory level the way git
applies it, plus `.gastonignore` for things that should not sync but should
stay in the repository, plus built-in defaults for the cases every tree has.

**Changing the pattern set is a SCOPE change, never a deletion.** Adding one
line to `.gitignore` can remove a thousand paths from the scan, and the natural
reading of "present last pass, absent now" is that the user deleted them -- so
the bridge would propagate a mass deletion nobody asked for. `sync.py` handles
this by freezing an ignored row at its last agreed `(class, crc)` rather than
treating it as gone; this module only decides membership.
"""

from __future__ import annotations

import os
import re

#: Present in almost every tree, and none of it belongs on a 1998 machine.
#: Deliberately short. A long list of guesses is how something the user
#: actually wanted stops syncing with no report.
DEFAULTS = (
    ".git/",
    ".hg/",
    ".svn/",
    "__pycache__/",
    ".venv/",
    "node_modules/",
    "*.pyc",
    "*.o",
    "*.obj",
    "*.qcow2",
    "*.iso",
    ".gaston/",
    ".gaston-state/",
)

#: Ours. Never syncable, and not something a user should have to list.
ALWAYS = ("*.gaston-tmp", "*.gaston-old")


def _translate(pat: str) -> re.Pattern:
    """One gitignore pattern to a regex over a `/`-separated relative path.

    Written out rather than leaning on `fnmatch`, which gets the two cases that
    matter wrong: its `*` crosses `/`, so `*.o` would match `a/b.o` when git
    says it matches only the basename unless the pattern is anchored; and it
    has no `**`.
    """
    i, n, out = 0, len(pat), []
    while i < n:
        c = pat[i]
        if pat.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pat.startswith("/**", i):
            out.append("/.*")
            i += 3
        elif c == "*":
            out.append("[^/]*")
            i += 1
        elif c == "?":
            out.append("[^/]")
            i += 1
        elif c == "[":
            j = i + 1
            if j < n and pat[j] in "!^":
                j += 1
            if j < n and pat[j] == "]":
                j += 1
            while j < n and pat[j] != "]":
                j += 1
            if j >= n:
                out.append(r"\[")
                i += 1
            else:
                body = pat[i + 1:j].replace("\\", "\\\\")
                if body[:1] in ("!", "^"):
                    body = "^" + body[1:]
                out.append(f"[{body}]")
                i = j + 1
        else:
            out.append(re.escape(c))
            i += 1
    return re.compile("".join(out) + r"\Z")


class _Rule:
    __slots__ = ("anchored", "base", "dir_only", "negate", "rx")

    def __init__(self, pattern: str, base: str):
        self.base = base
        self.negate = pattern.startswith("!")
        if self.negate:
            pattern = pattern[1:]
        self.dir_only = pattern.endswith("/")
        pattern = pattern.rstrip("/")
        # A pattern with a slash anywhere but the end is anchored to the
        # directory holding the file it came from; one without is matched
        # against every basename below it. That distinction is the whole
        # difference between `build/` and `*/build/`.
        self.anchored = "/" in pattern
        self.rx = _translate(pattern.lstrip("/"))

    def matches(self, rel: str, is_dir: bool) -> bool:
        if self.base:
            if not rel.startswith(self.base + "/"):
                return False
            rel = rel[len(self.base) + 1:]

        # An excluded DIRECTORY excludes everything beneath it. git achieves
        # that by not descending; we are asked about one path at a time, so the
        # rule has to be applied to each ancestor as well. Without this,
        # `build/` matched the directory and not `build/x.c` -- so the scanner
        # pruned the walk but the row bookkeeping disagreed with it.
        parts = rel.split("/")
        for i in range(len(parts)):
            last = i == len(parts) - 1
            # Every prefix except the whole path IS a directory; the whole path
            # is one only if the caller said so.
            if self.dir_only and last and not is_dir:
                continue
            if self.anchored:
                if self.rx.match("/".join(parts[:i + 1])):
                    return True
            elif self.rx.match(parts[i]):
                return True
        return False


class Ignores:
    """A callable matcher over `/`-separated relative paths.

    **A directory must be passed with a trailing slash.** `m("build")` is a
    question about a FILE called build, and `build/` does not match a file --
    that is the whole point of the trailing slash in the pattern. Callers that
    hold a type (a manifest row, a walk) know which they have and must say so;
    getting it wrong means a directory that should be out of scope stays in it.
    """

    def __init__(self, rules: list[_Rule], sources: list[str]):
        self._rules = rules
        #: Which files the patterns came from, so a change to any of them can
        #: be recognised as a scope change rather than a mass deletion.
        self.sources = sources

    def __call__(self, rel: str) -> bool:
        is_dir = rel.endswith("/")
        rel = rel.rstrip("/")
        if not rel:
            return False
        hit = False
        # Last matching rule wins, which is what makes `!` work at all.
        for r in self._rules:
            if r.matches(rel, is_dir):
                hit = not r.negate
        return hit

    def __len__(self) -> int:
        return len(self._rules)


def load(root: str, *, extra: tuple[str, ...] = ()) -> Ignores:
    """Collect the rules for a tree.

    Nested files are read too, because git applies them that way and a tree
    whose `src/.gitignore` is ignored would push build output from exactly the
    directory the user was most careful about.
    """
    rules = [_Rule(p, "") for p in DEFAULTS + ALWAYS + tuple(extra)]
    sources: list[str] = []

    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in (".git", ".hg", ".svn")]
        base = os.path.relpath(dirpath, root).replace(os.sep, "/")
        base = "" if base == "." else base
        for name in (".gitignore", ".gastonignore"):
            if name not in filenames:
                continue
            full = os.path.join(dirpath, name)
            sources.append(full)
            try:
                with open(full, encoding="utf-8", errors="replace") as fh:
                    text = fh.read()
            except OSError:
                continue
            for line in text.splitlines():
                line = line.rstrip()
                # A trailing backslash escapes the space git would otherwise
                # strip; a leading '#' is a comment unless escaped.
                if not line or line.lstrip().startswith("#"):
                    continue
                rules.append(_Rule(line, base))
    return Ignores(rules, sources)
