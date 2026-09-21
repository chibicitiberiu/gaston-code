"""What is known about the tools on the machine at the other end.

THE DURABLE OUTPUT OF THE TOOL-DETECTION WORK. Detection decides *when* a
note is shown; this file decides what it *says*. Those are separate jobs and
they rot differently: a detection rule that guesses wrong costs one unused
line, and a note that is wrong costs the agent acting confidently on
folklore.

So the rule is: **detection may guess, notes may not.** A note ships only for
something measured on a real machine, and carries `measured` saying which
machine and which version. A test asserts that field is present, because the
day it becomes optional is the day the first unmeasured note arrives.

Measured, not argued. The `watcom` note below was worth 9 of 19 exec calls on
one task -- see `docs/PROGRESS.md` for the A/B.

WHY A PYTHON MODULE and not markdown files: `bridge/` has no non-`.py` files
at all, and the house answer for static text is a string literal -- the
`STYLESHEET` precedent in `bridge/view/html.py`. A directory of notes plus a
loader plus its corruption story is machinery this does not need yet. Revisit
past roughly ten notes.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Note:
    """One tool's facts.

    `body` is written against `{root}`, never a literal path. The whole point
    of detection is finding a toolchain somewhere other than the obvious
    place, so a note that hardcodes `C:\\WATCOM` contradicts the inventory
    line above it the moment someone installs to `D:\\OW` -- two authoritative
    sources disagreeing, with the more detailed one wrong.
    """

    title: str
    #: Which machine, and which version of the tool. Not a date alone: the
    #: layout facts below are true of Open Watcom 1.9 and not of 2.0, whose
    #: `binnt` holds no compilers at all.
    measured: str
    body: str

    def render(self, root: str) -> str:
        return self.body.replace("{root}", root).strip("\n")


#: Keyed by the name a detection rule reports. A test asserts every key here
#: matches a section in the shipped rules, so a note for a tool nothing can
#: detect cannot accumulate.
NOTES: dict[str, Note] = {
    "watcom": Note(
        title="Open Watcom C/C++ 1.9",
        measured="Open Watcom 1.9 on the vm/ Windows 98 SE guest, 2026-09-21",
        body=r"""
Open Watcom 1.9 at {root} -- measured on this machine, not inferred:

  * A WINDOWED app, in one step:
        wcl386 -bt=nt -l=nt_win hello.c
    `-l=nt_win` is the Windows subsystem. `-l=nt` is the CONSOLE subsystem,
    and the linker cheerfully says "creating a Windows NT character-mode
    executable" while you wait for a window that never appears. This is the
    costliest thing here to get wrong, and it is not in wcl386's own help --
    it is in SAMPLES\WIN\GENERIC\WIN32\MAKEFILE.
  * Layout, so you never need to list it: binnt\ (wcc386, wpp386, wlink,
    wmake, wrc), binw\ (DOS-hosted tools, and the linker's wlsystem.lnk,
    which it reads on every link), h\ and h\nt\ (headers), lib386\ and
    lib386\nt\ (import libraries), samples\.
  * {root}\SETVARS.BAT sets WATCOM, PATH, INCLUDE and EDPATH, and is called
    from AUTOEXEC.BAT -- so a fresh DOS box already has them.
  * Worked Win32 examples: {root}\SAMPLES\WIN\GENERIC\WIN32\.
""",
    ),
    "command.com": Note(
        title="COMMAND.COM",
        measured="Windows 98 SE 4.10.2222 on the vm/ guest, 2026-09-20",
        # This is the one note whose body must be RESIDENT rather than
        # fetched: there is no "look it up first" moment for a shell, because
        # the first command fails before the agent could know to ask.
        body=r"""
The shell is COMMAND.COM, which is not cmd.exe. Every line below was
measured on this machine, mostly by an agent getting it wrong first:

  * One command, one line. `&`, `&&`, `||` and a newline are not separators;
    the complaint varies ("Too many parameters - &", "Syntax error") and
    `call x.bat && y` is the silent case -- no output, no error, `y` never
    ran. For several steps write a .bat into the mirrored folder and run it.
  * No `2>&1`: it creates a file literally named `&1`, and a following
    `| more` eats the rest of the output.
  * `start prog.exe` returns instantly with no output whether it ran,
    crashed, or does not exist. You cannot see a window -- do not report one.
  * A running .exe is locked: "Permission denied" from the linker, "Access
    denied" from `del`, and `attrib` shows nothing wrong. Ask the user to
    close it.
""",
    ),
}
