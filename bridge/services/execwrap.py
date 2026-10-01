r"""Getting a real exit code out of a shell that loses it.

`command.com /c <cmd>` exits 0 whatever happened -- for a failing child and for
a program that does not exist (`docs/measurements/execprobe-vm-gui-20260926.txt`).
So `exec` through that shell could not say whether a build worked, and a live
session verified every single compile with a follow-up `dir`: two round trips
per build, on a machine where each one is a second.

`if errorlevel` INSIDE a batch file does see a program's real code. Measured on
real hardware, 2026-10-01
(`docs/measurements/exit-codes-via-batch-p200-20261002.txt`):

    failing .exe, then `if errorlevel 1`      -> fired
    succeeding .exe, same                     -> did not fire
    a descending cascade                      -> the EXACT code, RC=1

The exact code was dropped by decision -- zero versus non-zero is the question
anyone asks of a build, and the exact value would have cost a 6KB cascade, a
static wrapper written once per session and two levels of `call` nesting
nobody measured. So the wrapper is four lines.

FOUR CONSTRAINTS, each from a measurement and each of which would have bitten
-- the fourth one did, in the first version of this file:

  * **errorlevel has to be forced non-zero BEFORE the command runs.**
    `if errorlevel` reads whatever the last PROGRAM left behind, and a command
    that never ran leaves it alone -- so a typo, a missing compiler or a wrong
    path read as SUCCESS: the shell printed "Bad command or file name" and
    errorlevel stayed at 0. That is the one case this feature was built to
    close, and the first version got it wrong on real hardware. Forcing it
    first turns "unchanged" into "failed", which is the honest reading: nothing
    ran, so nothing succeeded. The line that does it is in the profile, with
    the four candidates that do not work and why.

  * **`call` only for a batch file.** Without it a nested .bat never returns
    and the verdict lines simply do not run. With it, a builtin's redirection
    breaks -- `call echo REDIR> R.TXT` gives "File creation error" where the
    same line without `call` works. Opposite treatment, decided by the
    leading token.
  * **The verdict travels on stdout.** The wrapper's own exit code is
    swallowed by the same `command.com /c` the wrapper exists to work around,
    so there is nowhere else to put it. The bridge strips the LAST line that
    matches, so a program printing the same text earlier cannot confuse it.
  * **A builtin has no exit code on this shell at all.** `dir NOSUCHFILE.XYZ`
    prints "File not found" and leaves errorlevel at 0, so wrapping one would
    report success for a command that failed -- strictly worse than saying
    nothing. Builtins get no wrapper and no claim.

**The command the user approved is what runs.** The guard classifies the
original text and the permission desk shows it; this writes that text into the
file byte for byte and runs the file. If that ever stops being true, the
dialog is describing something other than what executes.

Everything shell-specific is in `[shell.exit_code]` in the shell's own profile,
because "how do I get an exit code out of this program" is a fact about the
program -- the same reason `run_switch` lives there.
"""

from __future__ import annotations

import logging
import posixpath

from ..agent import profiles

log = logging.getLogger("bridge.execwrap")

#: What the wrapper echoes, and what the bridge looks for. Not profile data:
#: it is a private agreement between this module and itself.
#:
#: Chosen to be something no program prints and no shell interprets. A leading
#: dot keeps it out of the way visually, and there is no redirect, pipe or
#: variable sigil in it -- a sentinel the shell rewrites is not a sentinel.
SENTINEL = ".gaston-rc"

#: The name the wrapper is written under, in the machine's own temp directory.
#: One file, overwritten per command, 8.3-clean so it is the same file whichever
#: name a DOS tool sees. The exec lane is one-at-a-time, so a fixed name cannot
#: collide with itself.
STEM = "GEXEC"


def _leaf(token: str) -> str:
    """The bare verb from a leading token: no directory, no extension, lower."""
    t = token.strip().strip('"').replace("\\", "/")
    t = posixpath.basename(t)
    dot = t.rfind(".")
    return (t[:dot] if dot > 0 else t).lower()


def _ext(token: str) -> str:
    t = token.strip().strip('"').replace("\\", "/")
    t = posixpath.basename(t)
    dot = t.rfind(".")
    return t[dot:].lower() if dot > 0 else ""


def applies(shell_name: str, command: str) -> bool:
    """Whether wrapping this command through this shell would tell us anything.

    False for a shell whose exit code already survives, for an empty command,
    and for a builtin -- see the module docstring for why the last one is a
    refusal rather than a best effort.
    """
    table = profiles.shell(shell_name).get("exit_code", {})
    if not table.get("lost"):
        return False
    if not table.get("wrapper"):
        return False
    first = command.strip().split(None, 1)
    if not first:
        return False
    return _leaf(first[0]) not in set(table.get("builtins", []))


def body(shell_name: str, command: str) -> bytes:
    """The wrapper file's bytes, with this shell's own line endings.

    CRLF because an LF-only .bat is read as ONE command line on COMMAND.COM and
    exits 0 -- the other half of the same measurement this module exists for.
    Not negotiable per shell today; when a shell needs LF, that is another key
    in the same table rather than a branch here.
    """
    table = profiles.shell(shell_name).get("exit_code", {})
    cmd = command.strip()
    call = ""
    if _ext(cmd.split(None, 1)[0]) in set(table.get("call_exts", [])):
        call = "call "
    text = str(table["wrapper"]).format(call=call, command=cmd,
                                        sentinel=SENTINEL)
    return text.replace("\r\n", "\n").replace("\n", "\r\n").encode("ascii",
                                                                   "replace")


def path(shell_name: str, temp_dir: str) -> str:
    """Where the wrapper goes, spelled the way that machine spells paths.

    `temp_dir` is the `temp` HELLO header, which is `GetTempPathA` and so
    already carries its trailing separator. Joined rather than normalised: the
    machine said how it spells a path and this is not the place to have an
    opinion about it.
    """
    ext = str(profiles.shell(shell_name).get("exit_code", {}).get("ext", ""))
    sep = "" if temp_dir[-1:] in ("\\", "/") else "\\"
    return f"{temp_dir}{sep}{STEM}{ext}"


def split(text: str) -> tuple[str, str]:
    """Pull the verdict off the output. Returns (output, "ok"/"fail"/"").

    THE LAST MATCHING LINE, not the first and not any line: the wrapper echoes
    it after the command, so a program that printed the same text earlier gets
    its own output back untouched. An empty verdict means the line never
    arrived -- the command killed the shell, or output was capped before it --
    and the caller must then say nothing about the exit code rather than guess.
    """
    lines = text.split("\n")
    for i in range(len(lines) - 1, -1, -1):
        stripped = lines[i].strip()
        if not stripped.startswith(SENTINEL):
            continue
        verdict = stripped[len(SENTINEL):].strip().lower()
        if verdict not in ("ok", "fail"):
            continue
        del lines[i]
        # The wrapper's own line ended with a newline, so removing it leaves a
        # trailing empty element where the output had none.
        while lines and lines[-1] == "":
            lines.pop()
        return ("\n".join(lines), verdict)
    return (text, "")
