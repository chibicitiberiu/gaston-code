"""What the proxied machine is, and how to tell the agent about it.

The model runs on the bridge, which is a modern Linux box, and without being
told otherwise it will answer as though that is the machine in question. It is
not: it is acting for a 1998 PC with a couple of hundred megahertz and single
-digit megabytes free, and that changes which answers are correct. "Install the
toolchain and rebuild" is a perfectly good suggestion that is wrong here.

The facts come from the guest's HELLO rather than from a table, so they
describe the machine actually connected.
"""

from __future__ import annotations

import os

from . import prompts, toolrules
from . import toolnotes as notes


def describe(h: dict) -> str:
    """One readable block of machine facts, from HELLO headers."""
    def kb(key: str) -> str:
        try:
            v = int(h.get(key, "0") or 0)
        except ValueError:
            return prompts.text("machine.facts.unknown")
        if v <= 0:
            return prompts.text("machine.facts.unknown")
        return f"{v // 1024} MB" if v >= 1024 else f"{v} KB"

    unknown = prompts.text("machine.facts.unknown")
    return prompts.text(
        "machine.facts.block",
        os=h.get("os") or prompts.text("machine.facts.os_unreported"),
        cpu=h.get("cpu") or unknown,
        mem_total=kb("mem_kb"), mem_free=kb("mem_free_kb"),
        screen=h.get("screen") or unknown,
        disk_free=kb("disk_free_kb"),
    ).strip("\n")


def _sync_paragraph(workdir: str, sync_dir: str | None,
                    guest_root: str | None,
                    home: str | None = None) -> str:
    """What to say about files, which depends on whether sync is configured.

    THREE paragraphs now, and only one of them is ever true at a time. Getting
    this wrong in any direction costs the user real work: promising a transfer
    that does not exist means the model reports a file as delivered when it is
    sitting on a Linux box the user cannot see; denying one that does exist
    means it refuses a thing it could have done; and NAMING A FOLDER THAT DOES
    NOT EXIST YET sent one agent off to diagnose a sync fault instead of
    writing the file it had been asked for.

    The third is the new normal: this prompt is built when the session is,
    which is before the conversation has committed to a folder -- that happens
    at the first tool call, and the agent is told the path in a notice then.
    """
    if sync_dir and not guest_root:
        under = (prompts.text("machine.sync.under_home", home=home)
                 if home else "")
        return prompts.text("machine.sync.no_folder_yet",
                            workdir=workdir, under=under)

    if not sync_dir:
        return prompts.text("machine.sync.no_sync", workdir=workdir)

    where = guest_root or prompts.text("machine.sync.default_root")
    rel = os.path.relpath(sync_dir, workdir) if sync_dir != workdir else "."
    # `..` means the working directory is INSIDE the mirror rather than the
    # other way round, which is what a per-conversation folder looks like.
    # Everything reachable is mirrored, so say that rather than "`../` is
    # mirrored", which is true and reads as though the agent should go up.
    if rel.startswith(".."):
        rel = "."
    inside = (prompts.text("machine.sync.inside_all") if rel == "."
              else prompts.text("machine.sync.inside_one", rel=rel))
    # WHERE THE STAGED COPIES ACTUALLY ARE, computed the same way
    # `syncrun.Pass._conflict_dir` computes it: one level above the working
    # directory, so a staged copy does not sync back to the machine it was
    # staged away from. This used to be written as a bare relative
    # `.gaston/conflict/...`, which resolves from the agent's cwd to a
    # directory nothing creates -- the model was told to read a path that has
    # never existed, and a Read of the real one was refused as being outside
    # the working copy.
    conflicts = os.path.join(
        os.path.dirname(workdir.rstrip(os.sep)), ".gaston", "conflict")
    return prompts.text("machine.sync.mirrored", workdir=workdir,
                        inside=inside, where=where, conflicts=conflicts)


#: How many fact blocks may be resident at once, on top of the shells'.
#:
#: THE CAP IS LOAD-BEARING, not a tidiness preference. A fresh conversation's
#: prompt is ~3,500 characters (`guest_root` is empty until the first tool
#: call and nothing rebuilds the session), and one toolchain note is ~600 of
#: them. Two measured toolchains on one machine and this block is a third of
#: the prompt -- so the budget would be honest only for as long as the
#: feature was unsuccessful. A second toolchain goes in as an inventory row
#: and nothing more; if that starts costing calls, that is the trigger to
#: look at skills, not to raise this number.
MAX_FACT_BLOCKS = 1


def tools_block(found: list, path: list | None = None) -> str:
    r"""The inventory, plus the measured notes for what was found.

    `found` is `toolrules.inventory(...)`. Empty gives "" -- a heading over
    an empty table is a paragraph the model reads on every request to learn
    nothing, and "nothing was detected" is not the same claim as "nothing is
    installed", which is why silence is the honest output.

    Every note is rendered against the root that was actually FOUND. A user
    with Watcom at `D:\OW` would otherwise get an inventory row saying
    `D:\OW` and a note saying `C:\WATCOM` -- two authoritative sources
    disagreeing, the more detailed one wrong.
    """
    if not found:
        return ""

    width = max(len(f.name) for f in found)
    rows = []
    for f in found:
        ver = f" (v{f.version})" if getattr(f, "version", "") else ""
        rows.append(f"  {f.name:<{width}}  {f.kind:<9}  {f.path}{ver}")
    # The basis goes on its own aligned column so the eye reads down it.
    pad = max(len(r) for r in rows)
    rows = [f"{r:<{pad}}   {f.basis}"
            for r, f in zip(rows, found, strict=True)]

    out = [
        "## What is installed on that machine",
        "",
        *rows,
        "",
        "Detected at connect from paths and environment variables, so it is",
        "evidence and not proof -- an unconfirmed row in particular is the",
        "machine's claim about itself. One command to confirm something is",
        "fine; a survey of the disk is not. An entry being absent means",
        "nothing matched, not that the thing is missing.",
    ]

    # THE SHELLS, NAMED, because the model is the one that picks.
    #
    # A machine has as many shells as it has -- `command.com` and `4dos` on a
    # DOS box, `sh`/`bash`/`zsh`/`csh` on a Unix one -- and which of them a
    # command runs under changes what the command line may contain. Nothing
    # on this side chooses: the machine reports the list, this prints it, and
    # `exec` takes the name. Before, the GUEST picked from `GetVersion()` and
    # told nobody, so a second installed shell was unreachable and the
    # bridge's command guard was judging by a grammar the guest might not
    # have been running.
    shells = toolrules.usable_shells(found)
    if shells:
        names = ", ".join(f"`{f.name}`" for f in shells)
        out += [
            "",
            f"Shells on that machine: {names}.",
            "",
            f"`exec` runs through `{shells[0].name}` unless you name",
            "another in its `shell` argument, and what a command line may",
            "contain -- how (or whether) commands chain, what quoting means,",
            "which built-ins exist -- is that shell's business. Pass",
            "`shell: \"none\"` to spawn a program directly instead, which",
            "gives cleaner quoting and loses every built-in and all",
            "redirection.",
        ]

    # THE PATH, STATED, because `Bad command or file name` names the PROGRAM
    # and not the environment -- and three separate sessions read that as
    # "the compiler is not installed" and went hunting the disk, which is
    # exactly what the inventory above exists to prevent.
    #
    # PRINTED FROM DETECTION, never asserted, and that distinction was nearly
    # lost. The corpus contains a measured `PATH=C:\WINDOWS;C:\WINDOWS\
    # COMMAND` and a note saying the machine's start-up scripts had not run.
    # Both were true when recorded and neither is true now: installing Open
    # Watcom put SETVARS.BAT into AUTOEXEC.BAT, and `exec` inherits the
    # client's environment (CreateProcessA with lpEnvironment=NULL,
    # win/rpc/exec.cpp:180), so today the same command answers
    # `...;C:\WATCOM\BINNT`. A measurement has a shelf life; printing the
    # live value has none.
    if path:
        out += [
            "",
            "Commands run with this PATH:",
            "",
            "  " + ";".join(path),
            "",
            "As at connect: anything installed since is not on it yet, and a",
            "tool above that is missing from it still runs by full path.",
        ]

    # Shell notes are not counted against MAX_FACT_BLOCKS. Which shell is
    # present changes how every single command must be written, and unlike a
    # toolchain there is no moment when the agent could look it up first --
    # the first command fails before it knows to ask.
    # Toolchains are capped at MAX_FACT_BLOCKS (1) so multiple compilers do
    # not overwhelm the prompt. Standalone tools get their own budget.
    toolchain_budget = MAX_FACT_BLOCKS
    tool_budget = 1
    for f in found:
        note = notes.NOTES.get(f.name)
        if note is None:
            continue
        if f.kind == "toolchain":
            if toolchain_budget <= 0:
                continue
            toolchain_budget -= 1
        elif f.kind != "shell":
            if tool_budget <= 0:
                continue
            tool_budget -= 1
        version = getattr(f, "version", "")
        out += ["", note.render(f.path, version=version)]

    return "\n".join(out)


def system_prompt(h: dict, workdir: str, sync_dir: str | None = None,
                  guest_root: str | None = None,
                  home: str | None = None,
                  tools: list | None = None,
                  path: list | None = None) -> str:
    """Text appended to Claude Code's own system prompt.

    APPENDED, never substituted: the preset is what makes Claude Code work,
    and replacing it to say a few sentences about an old PC would be a very
    poor trade.
    """
    return prompts.text(
        "machine.system.text",
        describe=describe(h),
        workdir=workdir,
        sync_paragraph=_sync_paragraph(workdir, sync_dir, guest_root, home),
        tools_block=tools_block(tools or [], path),
    ).strip()
