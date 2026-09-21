"""Turning what the machine reported into what the assistant is told.

The guest reports facts -- this variable has this value, this file exists.
Here is where they become "Open Watcom is at C:\\WATCOM". That split is the
point: a rule can change without rebuilding and reshipping a binary to a
1998 PC, and it can be tested on Linux in milliseconds.

TWO KINDS OF KNOWING, and the prompt must not blur them.

  verified   a marker file that this rule names was found. The tool is
             almost certainly there, at a root derived from where the marker
             was.
  reported   an environment variable pointed somewhere, and nothing
             confirmed it. Still worth telling the assistant, because a
             `WATCOM` variable is how the thing is usually found -- but it
             is a claim the machine made about itself, not something checked.

That distinction is load-bearing. "A false positive is one unused line" is
wrong: the inventory is the only place a toolchain is named, so it is the
one the assistant reaches for. A wrongly-claimed Turbo C gets `tcc hello.c`
for a Win32 GUI app -- which Turbo C 2.01 cannot build at all -- and then a
failure the assistant was told not to go exploring about. So every row
carries how it is known, and the instruction is "one command to confirm is
fine; a survey is not" rather than "do not check".
"""

from __future__ import annotations

import posixpath
from dataclasses import dataclass

from .toolrep import Fact

#: How a root is recovered from the marker file that proved it. The marker is
#: what exists; the root is what the assistant needs, because INCLUDE and LIB
#: are relative to it.
#:
#: `bins` rather than a hop count, and that was not the first design. A count
#: is per-section, but a section deliberately lists markers at DIFFERENT
#: depths -- Turbo C 2.01 installs flat (`C:\TC\TCC.EXE`) and 3.x uses
#: `C:\TC\BIN\TCC.EXE`, and both are in `[turboc]` because either may be what
#: is on the disk. "Drop two components" is right for one and turns the other
#: into `C:`, which is a wrong root stated as verified: the agent then sets
#: INCLUDE to `C:\INCLUDE` and every compile fails for a reason it has been
#: told not to go exploring about. So the rule names the directories a
#: compiler is found IN and one is dropped only if it is actually there.
#:
#: Data rather than code so adding a toolchain is a table entry, and so the
#: shipped `win/tools_detect.ini` and this file can be checked against each
#: other by a test instead of by memory.
@dataclass(frozen=True)
class Rule:
    kind: str
    #: None means the marker IS the answer: a single program, where the path
    #: to the program is what anyone wants. A tuple means the marker is one
    #: file inside a tree, and the tree is what is wanted -- drop the file
    #: name, then drop the directory it sat in if that directory is named
    #: here. `()` keeps the containing directory unconditionally.
    bins: "tuple | None" = None
    env: tuple = ()             # variables that name the root directly


#: The Windows-hosted and DOS-hosted halves of a Watcom install both hold a
#: `wcc386`, and BINW additionally holds `wlsystem.lnk`, which the linker
#: reads on every link whatever the target.
_WATCOM_BINS = ("BINNT", "BINW", "BINNT64")

RULES: dict[str, Rule] = {
    "command.com": Rule(kind="shell", env=("COMSPEC",)),
    "4dos":        Rule(kind="shell"),
    "watcom":      Rule(kind="toolchain", bins=_WATCOM_BINS, env=("WATCOM",)),
    "turboc":      Rule(kind="toolchain", bins=("BIN",)),
    "borlandc":    Rule(kind="toolchain", bins=("BIN",)),
    "msvc":        Rule(kind="toolchain", bins=("BIN",), env=("MSVCDIR",)),
    "djgpp":       Rule(kind="toolchain", bins=("BIN",), env=("DJGPP",)),
    "turbopascal": Rule(kind="toolchain", bins=("BIN",)),
    "masm":        Rule(kind="toolchain", bins=("BIN",)),
    "tasm":        Rule(kind="toolchain", bins=("BIN",)),
    "qbasic":      Rule(kind="tool"),
    "pkzip":       Rule(kind="tool"),
    "debug":       Rule(kind="tool"),
}


@dataclass(frozen=True)
class Found:
    name: str
    kind: str
    path: str
    verified: bool

    @property
    def basis(self) -> str:
        return ("verified on this machine" if self.verified
                else "reported by the machine, not confirmed")


def _parent(path: str) -> str:
    r"""The directory holding `path`, for a DOS path.

    Done by hand rather than with `os.path`: this is a Windows path being
    handled on Linux, where `os.path.dirname("C:\\A\\B")` is the whole string
    because the separator is not `/`. `bridge/files/paths.py` exists because
    that mistake was made once already.
    """
    out = path.rstrip("\\")
    cut = out.rfind("\\")
    if cut < 0:
        return out
    # `C:\FOO` -> `C:\`, not `C:`. A bare `C:` is not a directory on DOS, it
    # is the drive's CURRENT directory, which is whatever someone last cd'd
    # to -- so a root written that way means something different every time
    # it is used.
    if cut == 2 and out[1] == ":":
        return out[:3]
    return out[:cut]


def root_of(path: str, rule: Rule) -> str:
    """Where the tool lives, given one file belonging to it."""
    if rule.bins is None:
        return path.rstrip("\\")
    holder = _parent(path)
    last = holder.rstrip("\\").rsplit("\\")[-1].upper()
    if last in tuple(b.upper() for b in rule.bins):
        return _parent(holder)
    return holder


def inventory(facts: list[Fact]) -> list[Found]:
    """What is on that machine, most trustworthy first.

    A file wins over an environment variable for the same tool: the file was
    seen, the variable was merely stated. A tool with neither does not
    appear at all -- silence is the right answer for "nothing matched", and
    an entry saying "not found" would be a row the assistant has to read on
    every request to learn nothing.
    """
    by_file: dict[str, str] = {}
    by_env: dict[str, str] = {}

    for f in facts:
        rule = RULES.get(f.name)
        if rule is None:
            continue
        if f.kind == "file":
            # First marker wins: the INI lists them most-likely-first, and a
            # machine with two Watcoms installed is a problem the assistant
            # should be told about by seeing one and finding the other, not
            # one this silently picks between.
            by_file.setdefault(f.name, root_of(f.value, rule))
        elif f.kind == "env" and f.var in rule.env and f.contents:
            by_env.setdefault(f.name, f.contents.rstrip("\\"))

    out: list[Found] = []
    for name in sorted(set(by_file) | set(by_env)):
        rule = RULES[name]
        if name in by_file:
            out.append(Found(name, rule.kind, by_file[name], True))
        else:
            out.append(Found(name, rule.kind, by_env[name], False))
    # Shells first: which shell is present changes how every single command
    # must be written, so it is the fact with the widest blast radius.
    out.sort(key=lambda f: (f.kind != "shell", f.name))
    return out


def path_dirs(facts: list[Fact]) -> list[str]:
    """PATH, split. Reported unconditionally by the guest because every rule
    wants it and no section has to ask for it."""
    for f in facts:
        if f.kind == "env" and f.var == "PATH":
            return [p for p in f.contents.split(";") if p.strip()]
    return []
