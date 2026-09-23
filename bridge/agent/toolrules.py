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

import logging

from dataclasses import dataclass

from . import profiles
from .toolrep import Fact

log = logging.getLogger("bridge.toolrules")

#: How a root is recovered from the marker file that proved it, LOADED FROM
#: `bridge/profiles/<tool>.toml`. The marker is what exists; the root is what
#: the assistant needs, because INCLUDE and LIB are relative to it.
#:
#: `bins` rather than a hop count, and that was not the first design. A count
#: is per-section, but a section deliberately lists markers at DIFFERENT
#: depths -- Turbo C 2.01 installs flat (`C:\TC\TCC.EXE`) and 3.x uses
#: `C:\TC\BIN\TCC.EXE`, and both are listed because either may be what is on
#: the disk. "Drop two components" is right for one and turns the other into
#: `C:`, which is a wrong root stated as verified: the assistant then sets
#: INCLUDE to `C:\INCLUDE` and every compile fails for a reason it has been
#: told not to go exploring about. So the rule names the directories a
#: compiler is found IN and one is dropped only if it is actually there.
@dataclass(frozen=True)
class Rule:
    kind: str
    bins: "tuple | None" = None
    env: tuple = ()             # variables that name the root directly
    version_markers: tuple = () # tuple of (marker_suffix, version_str)
    version_default: str = ""


def _build() -> dict[str, Rule]:
    out: dict[str, Rule] = {}
    for name in profiles.names():
        d = profiles.detect(name)
        if not d.get("kind"):
            continue
        bins = d.get("bins")
        vinfo = d.get("version", {})
        vdefault = str(vinfo.get("default", "")) if isinstance(vinfo, dict) else ""
        vmarkers = tuple((k.upper(), str(v)) for k, v in vinfo.items() if k != "default") if isinstance(vinfo, dict) else ()
        out[name] = Rule(kind=d["kind"],
                         bins=tuple(bins) if bins is not None else None,
                         env=tuple(d.get("env", ())),
                         version_markers=vmarkers,
                         version_default=vdefault)
    return out


RULES: dict[str, Rule] = _build()


@dataclass(frozen=True)
class Found:
    name: str
    kind: str
    path: str
    verified: bool
    version: str = ""

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
    tool_facts: dict[str, list[Fact]] = {}

    for f in facts:
        rule = RULES.get(f.name)
        if rule is None:
            continue
        tool_facts.setdefault(f.name, []).append(f)
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
        path = by_file[name] if name in by_file else by_env[name]
        verified = name in by_file
        version = rule.version_default
        if rule.version_markers:
            found_markers = [f.value.upper() for f in tool_facts.get(name, []) if f.kind == "file"]
            for marker, ver in rule.version_markers:
                if any(m.endswith(marker) or marker in m for m in found_markers):
                    version = ver
                    break
        out.append(Found(name, rule.kind, path, verified, version=version))
    # Shells first: which shell is present changes how every single command
    # must be written, so it is the fact with the widest blast radius.
    out.sort(key=lambda f: (f.kind != "shell", f.name))
    return out


def shells_of(found) -> list:
    """Every shell this machine reported, in inventory order.

    A LIST, because a machine has as many shells as it has. `command.com`
    and `4dos` on a DOS box; `sh`, `bash`, `zsh` and `csh` on a Unix one.
    Nothing here picks between them -- the machine says what it has, the
    system prompt says so too, and the model names the one it wants on each
    `exec` call. `inventory` already sorts shells first.
    """
    return [f for f in (found or ())
            if getattr(f, "kind", "") == "shell"]


def shell_of(found) -> str:
    """The FIRST shell this machine reported, or "" if it reported none.

    A convenience over `shells_of`, and deliberately not a choice about
    which shell to RUN anything through -- that is the model's call, per
    call, from the list. This answers the narrower questions that are
    properties of the machine rather than of a command: which separator its
    PATH uses, and which codepage its console children write.

    It is also the fallback the command guard judges by when a call named no
    shell, which is the one place it stands in for a decision. Referring to
    the first of what the machine reported is not the same as assuming a
    shell nobody mentioned.
    """
    shells = shells_of(found)
    return (getattr(shells[0], "name", "") or "") if shells else ""


def shell_command(found, name: str) -> "str | None":
    """How to tell `name` on this machine to run one command line.

    Returns e.g. `C:\\WINDOWS\\COMMAND.COM /c`, which the guest splices in
    front of the command and otherwise knows nothing about -- or None if this
    machine did not report a shell by that name.

    BOTH HALVES ARE DATA. The path is what detection actually found, so a
    4DOS at `D:\\4DOS` is invoked from `D:\\4DOS`; the switch comes from that
    shell's own profile, because "how do I tell this program to run one
    command" is a fact about the program. Neither is written in Python, and
    the guest holds neither: it used to decide `command.com` vs `cmd.exe`
    from `GetVersion()` itself, which meant a machine with a second shell
    installed could not be told to use it, and the bridge's guard was
    judging a grammar the guest might not be running.
    """
    if not name:
        return None
    want = name.strip().lower()
    for f in shells_of(found):
        if (getattr(f, "name", "") or "").lower() != want:
            continue
        switch = str(profiles.shell(f.name).get("run_switch", "")).strip()
        if not switch:
            # REPORTED BUT NOT INVOCABLE. The machine has this shell and no
            # profile says how to hand it one command line, so composing
            # something would be a guess -- and the guess runs. `4dos` is the
            # live example: it has a profile, it is detected, and until its
            # `[shell]` table names a switch the honest answer is that this
            # bridge cannot drive it.
            log.warning("%s is installed on that machine but its profile "
                        "declares no run_switch, so nothing can be run "
                        "through it", f.name)
            return None
        path = getattr(f, "path", "") or f.name
        return f"{path} {switch}"
    return None


def usable_shells(found) -> list:
    """The shells this bridge can actually hand a command line to.

    `shells_of` is what the MACHINE has; this is the subset the model may
    pick from, which is smaller exactly when a profile has not said how to
    invoke one. Listing a shell that cannot be selected would be a lie in
    the one paragraph the model reads to decide.
    """
    return [f for f in shells_of(found) if shell_command(found, f.name)]


def path_dirs(facts: list[Fact], shell: str = "") -> list[str]:
    """PATH, split the way this machine's shell separates it.

    Reported unconditionally by the guest because every rule wants it and no
    section has to ask for it.

    THE SEPARATOR IS THE SHELL'S, not `;`. It was `;` in Python, so a machine
    that separates with `:` had its entire PATH returned as one entry and the
    system prompt then stated it as fact -- a wrong answer, stated
    confidently, under an instruction not to go and check.

    An undeclared separator returns nothing rather than guessing. The PATH
    line is then absent from the prompt, which costs the assistant one `set`
    and is strictly better than a PATH that is wrong.
    """
    sep = str(profiles.shell(shell).get("path_sep", "")) if shell else ""
    for f in facts:
        if f.kind == "env" and f.var == "PATH":
            if not sep:
                log.warning("no shell profile declares how PATH is separated "
                            "(shell=%r), so it is left out of the prompt "
                            "rather than split wrongly", shell)
                return []
            return [p for p in f.contents.split(sep) if p.strip()]
    return []
