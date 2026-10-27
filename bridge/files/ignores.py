"""Which paths are out of the mirror.

`--sync-dir` is mandatory and separate from `--cwd` precisely so that nobody
points this at a repository and discovers `.venv/` and `vm/*.qcow2` arriving on
a machine with 1.7GB free. But a source tree already carries an exact list of
what must not cross -- its `.gitignore` -- and re-deriving that list by hand is
both work and a source of disagreement with the build.

So: `.gitignore` is honoured where present, at any directory level the way git
applies it, plus `.gastonignore` for things that should not sync but should
stay in the repository, plus built-in defaults for the cases every tree has.

**The defaults are written into the tree rather than kept hidden.** `seed()`
puts them in a new work folder's `.gastonignore`, marked with `MARKER`, and a
file carrying that marker IS the list -- `load()` applies no defaults over the
top of it, so a line deleted there is really deleted. A `.gastonignore`
without the marker is somebody's own file from before this existed, and the
defaults still apply to it.

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
    #: SCRATCH, BY CONVENTION RATHER THAN BY GUESS. The rest of this tuple
    #: is "things every tree has"; these two are a place the agent is TOLD
    #: to put bulk output, in prompts/machine.toml's `mirrored`, and the
    #: user is told by `seed` writing this whole tuple into the folder. A
    #: convention nobody is told about is a guess, which is what the note
    #: above warns against -- the prompt line, `seed` and these two entries
    #: are one change and should not be separated.
    #:
    #: Why it is needed: a 640x480x24bpp BMP is 921,654 bytes and MAX_FILE
    #: is 1,048,576, so a screen recorder's frames each cleared the
    #: per-file cap by 127 KB; SYNC_MAX_FILES is 2000 and there were 565.
    #: Both caps passed and 499 MB crossed a 10 Mbit link from a 1998
    #: machine, because the quantity that mattered was their product and
    #: nothing measures that (BACKLOG 47).
    #:
    #: NOT `build/`. In this project the thing being built is usually FOR
    #: the guest and the compiled exe is the deliverable -- ignoring it
    #: would keep the one file the user wanted from coming back. Object
    #: files are already covered by *.o and *.obj above, which is the part
    #: of a build directory that is genuinely noise.
    "temp/",
    "tmp/",
)

#: Ours. Never syncable, and not something a user should have to list.
ALWAYS = ("*.gaston-tmp", "*.gaston-old")

#: The first line of a list this bridge wrote, and the whole of how `load`
#: tells one from a file somebody typed. Versioned so a later DEFAULTS can
#: be migrated into an old file rather than guessed at.
MARKER = "# gaston ignore list v1"

#: Read in Notepad on the machine, so the lines are short enough not to
#: wrap at 640x480. Each one is its own literal: the prose ratchet
#: (tests/python/meta) gives this file a budget of zero bytes over 120.
_HEADER = (
    "",
    "What does not cross to your machine, one pattern per line.",
    "",
    "A trailing / means a folder. * and ? are wildcards. A line",
    "starting with ! puts something back. Blank lines and lines",
    "starting with # are ignored. Any .gitignore here is read too.",
    "",
    "THESE LINES ARE THE LIST. The built-in set was copied in when",
    "this folder was made, so a line deleted here really is gone.",
    "Keep the first line above and it stays that way.",
    "",
)


def template() -> str:
    """The starter list, as the text of a file."""
    head = [MARKER] + [("# " + s).rstrip() for s in _HEADER]
    return "\n".join(head + list(DEFAULTS) + [""])


def seed(root: str) -> bool:
    """Write the starter list into `root`. True if this call wrote it.

    `O_EXCL` rather than "does it exist": the one file this may never
    touch is one somebody else wrote, and a check leaves a window where
    two callers both find it missing. Any other `OSError` is the caller's
    to report -- a work folder that cannot be written to is worth a line
    in the log, not a silent pass.
    """
    try:
        fd = os.open(os.path.join(root, ".gastonignore"),
                     os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    except FileExistsError:
        return False
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(template())
    return True


def _generated(root: str) -> bool:
    """Is the list at the top of this tree one we wrote?"""
    try:
        with open(os.path.join(root, ".gastonignore"),
                  encoding="utf-8", errors="replace") as fh:
            return fh.readline().strip() == MARKER
    except OSError:
        return False


def _translate(pat: str, flags: int = 0) -> re.Pattern:
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
    return re.compile("".join(out) + r"\Z", flags)


class _Rule:
    __slots__ = ("anchored", "base", "dir_only", "negate", "rx")

    def __init__(self, pattern: str, base: str, fold: bool = False):
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
        # FOLDED for the client's own patterns on a machine whose names
        # fold: `*.bak` written in Options means IGN_TEST.BAK too, and it
        # must mean it identically wherever it is applied (see syncrun).
        self.rx = _translate(pattern.lstrip("/"), re.IGNORECASE if fold else 0)

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


def load(root: str, *, extra: tuple[str, ...] = (),
         fold_extra: bool = False) -> Ignores:
    """Collect the rules for a tree.

    Nested files are read too, because git applies them that way and a tree
    whose `src/.gitignore` is ignored would push build output from exactly the
    directory the user was most careful about.
    """
    # A marked file carries the defaults already; applying them again on
    # top would make deleting a line from it do nothing, which is the
    # hidden-guess problem this is here to end. ALWAYS is not negotiable
    # and stays either way.
    built_in = () if _generated(root) else DEFAULTS
    rules = [_Rule(p, "") for p in built_in + ALWAYS]
    rules += [_Rule(p, "", fold_extra) for p in extra]
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


def client_patterns(extra: tuple, fold: bool) -> Ignores:
    """Only the client's own patterns (Options > Files, "Never copy"), as
    a matcher. Applied to the MACHINE's rows as well as this side's: the
    person asked for these never to be copied, in either direction. The
    bridge's DEFAULTS above stay what they were -- host clutter kept off the
    machine -- and are not applied to what the machine makes."""
    return Ignores([_Rule(p, "", fold) for p in extra], [])
