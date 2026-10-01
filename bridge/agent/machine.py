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
                    home: str | None = None,
                    planned: str | None = None) -> str:
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

    `home` is the guest's OWN declared share, straight from HELLO, and it is
    read by the sync-off branch as well as the not-yet one. That is the whole
    of 2026-10-01's bug: a guest that declared `C:\\WORK` against a bridge
    with no `--sync-dir` was told nothing was mirrored and nothing about
    where to put a file instead, so it put one in the root of C:.

    `planned` is where this conversation's own folder ON THE MACHINE is about
    to be, and it is read ONLY by the sync-off branch. The first fix said
    "write into `C:\\WORK`", the share itself, which is better than the drive
    root and still not the design: a conversation gets its own folder under
    that share, created by the client at the first tool call. The prompt is
    built before that call, so a committed path (`guest_root`) is not
    available on a first turn and a planned one is -- and with no mirror,
    that path is the only destination the agent is ever handed, because every
    byte reaching the machine is a `put_file` to a path it typed itself.

    `guest_root` is deliberately NOT reused for this. The mirrored branch
    distinguishes a folder that exists from one that is coming, and feeding
    it a prediction would make it promise a mirror that is not running yet.
    """
    if sync_dir and not guest_root:
        under = (prompts.text("machine.sync.under_home", home=home)
                 if home else "")
        return prompts.text("machine.sync.no_folder_yet",
                            workdir=workdir, under=under)

    if not sync_dir:
        # THE TWO SIDES DISAGREE HERE, and until 2026-10-01 nothing said so.
        # The guest announces its configured share in HELLO (`sync_root`),
        # the bridge only mirrors when it was given `--sync-dir`, and the
        # pair "guest mirrors C:\\WORK" / "no folder is mirrored from this
        # machine" sat two minutes apart in one log with nothing joining
        # them. The agent read the second half, concluded it had nowhere
        # agreed to write, and `put_file`d into the root of C: on somebody's
        # real disk -- which this paragraph now names as the one destination
        # that is never right.
        #
        # THREE ANSWERS, and which one is true is a fact about the machine
        # rather than about this bridge:
        #
        #   folder  the machine shares a home AND this conversation has (or
        #           is one tool call away from having) its own folder inside
        #           it. The only one that hands the agent a destination.
        #   home    a home, but nothing nameable under it yet -- a title that
        #           reduces to nothing a FAT volume holds, or every candidate
        #           name already owned by another conversation.
        #   none    the client has sync unconfigured on its own side. There
        #           is nowhere agreed, and the bridge does not invent a
        #           directory on somebody's real disk to cover for it.
        folder = guest_root or planned or ""
        if folder:
            declared = prompts.text("machine.sync.no_sync_declared_folder",
                                    home=home or folder, folder=folder)
            where = prompts.text("machine.sync.no_sync_where_folder",
                                 folder=folder)
        elif home:
            declared = prompts.text("machine.sync.no_sync_declared", home=home)
            where = prompts.text("machine.sync.no_sync_where", home=home)
        else:
            declared = prompts.text("machine.sync.no_sync_undeclared")
            where = prompts.text("machine.sync.no_sync_where_unknown")
        return prompts.text("machine.sync.no_sync", workdir=workdir,
                            declared=declared, where=where)

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
    # `syncrun.Pass._conflict_dir` does: one level above the working
    # directory, so a staged copy does not sync back to the machine it was
    # staged away from. A bare relative `.gaston/conflict/...` resolves from
    # the agent's cwd to a directory nothing creates -- naming the absolute
    # path here is what makes it readable.
    conflicts = os.path.join(
        os.path.dirname(workdir.rstrip(os.sep)), ".gaston", "conflict")
    return prompts.text("machine.sync.mirrored", workdir=workdir,
                        inside=inside, where=where, conflicts=conflicts)


#: How many toolchain BODIES are resident. Zero, since 2026-10-01: they are
#: fetched with `mcp__machine__toolnote` and only a one-line headline each
#: stays in the prompt.
#:
#: THE CAP WAS 1, AND 1 WAS A BUG WEARING A BUDGET'S CLOTHES. `found` arrives
#: alphabetically from `toolrules.inventory`, so on a machine with both a Sun
#: JDK and Visual C++ 6 -- which is the machine this is built for -- `jdk`
#: took the single slot and the C++ note was dropped with nothing said. The
#: fact lost that way was `cl` printing NO OUTPUT AT ALL when a directory is
#: missing from PATH, a failure shaped exactly like success.
#:
#: Raising it was never available. Rendered bodies measured 2026-10-01: msvc
#: 1,813, watcom 964, jdk 957, cscript 804, shell 893. `shell + msvc` alone
#: is 3,707 against a 2,800 budget, so per-toolchain budgeting could only
#: ever change WHICH note was dropped.
#:
#: The constant stays, at 0, because the alternative is a silent assumption.
#: If a body is ever worth making resident again this is the number to argue
#: with, and the argument has to beat the measurements above.
MAX_FACT_BLOCKS = 0


def tools_block(found: list, path: list | None = None) -> str:
    r"""The inventory, the shell's note, and a line per note that is not.

    `found` is `toolrules.inventory(...)`. Empty gives "" -- a heading over
    an empty table is a paragraph the model reads on every request to learn
    nothing, and "nothing was detected" is not the same claim as "nothing is
    installed", which is why silence is the honest output.

    WHAT IS RESIDENT AND WHAT IS NOT, since 2026-10-01: the rows, the shell
    note in full, and one headline per other measured tool. The bodies are
    fetched with `mcp__machine__toolnote`, because they did not fit -- see
    MAX_FACT_BLOCKS above for the measurements and for the note that was
    being dropped in silence before they moved.

    Everything printed is rendered against the root that was actually FOUND.
    A user with Watcom at `D:\OW` would otherwise get an inventory row
    saying `D:\OW` and a note saying `C:\WATCOM` -- two authoritative
    sources disagreeing, the more detailed one wrong.
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

    # ONE LINE EACH for everything measured that is not a shell, naming the
    # tool as `toolnote` wants it spelled. Without this the agent cannot
    # ask: it has no way to know a note exists, and `toolnote` becomes a
    # tool nothing ever calls.
    #
    # DIRECTLY UNDER THE ROWS, and that position was measured rather than
    # chosen. Sitting at the bottom of the block -- after the shell note,
    # the shells paragraph and the PATH -- Haiku read the inventory, picked
    # `C:\PROGRA~1\MICROS~2\VC98\BIN\cl.exe hello.c` straight out of it
    # and never called `toolnote` at all, which is the exact command the
    # Visual C++ note exists to prevent. The names and the obligation
    # attached to them belong in the same eyeful.
    #
    # Rendered against the found root like the bodies are, so a headline
    # mentioning a path cannot contradict the row above it.
    fetchable = [f for f in found
                 if f.kind != "shell" and f.name in notes.NOTES]
    if fetchable:
        width = max(len(f.name) for f in fetchable)
        out += ["", prompts.text("machine.notes.heading"), ""]
        for f in fetchable:
            note = notes.NOTES[f.name]
            line = (note.headline.replace("{root}", f.path)
                                 .replace("{version}",
                                          getattr(f, "version", "")
                                          or note.default_version))
            out.append(f"  {f.name:<{width}}  {line}")
        out += ["", prompts.text("machine.notes.pointer").strip("\n")]

    # THE SHELLS, NAMED, because the model is the one that picks: a machine
    # has as many shells as it has (`command.com`/`4dos` on DOS,
    # `sh`/`bash`/`zsh`/`csh` on Unix), and which one a command runs under
    # changes what the command line may contain. Nothing on this side
    # chooses -- the machine reports the list, this prints it, `exec` takes
    # the name.
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
    else:
        # Said, because the alternative is silent: with no shell every
        # command is spawned bare, and `ver` fails to start with nothing
        # saying why (BACKLOG 17).
        out += [
            "",
            "That machine reported no shell, so `exec` starts programs",
            "directly: shell built-ins (`dir`, `ver`, `copy`) and redirection",
            "will not work. Name a program by its path.",
        ]

    # THE PATH, STATED, because `Bad command or file name` names the PROGRAM
    # and not the environment -- three separate sessions read that as "the
    # compiler is not installed" and went hunting the disk, which is exactly
    # what the inventory above exists to prevent.
    #
    # PRINTED FROM DETECTION, never asserted: `exec` inherits the client's
    # environment (CreateProcessA with lpEnvironment=NULL, win/exec/execjob.cpp),
    # so this PATH can change between two sessions on the same machine (e.g.
    # installing a toolchain that edits AUTOEXEC.BAT). A measurement has a
    # shelf life; printing the live value has none.
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

    # THE SHELL NOTE IS RESIDENT AND IS NOT NEGOTIABLE. Which shell is
    # present changes how every single command must be written, and unlike a
    # toolchain there is no moment when the agent could look it up first:
    # the first command fails before it knows to ask. Every other note is a
    # headline here and a `toolnote` call away.
    shell_notes = [f for f in found
                   if f.kind == "shell" and f.name in notes.NOTES]
    for f in shell_notes:
        out += ["", notes.NOTES[f.name].render(
            f.path, version=getattr(f, "version", ""))]

    return "\n".join(out)


def system_prompt(h: dict, workdir: str, sync_dir: str | None = None,
                  guest_root: str | None = None,
                  home: str | None = None,
                  planned: str | None = None,
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
        sync_paragraph=_sync_paragraph(workdir, sync_dir, guest_root, home,
                                       planned),
        tools_block=tools_block(tools or [], path),
    ).strip()
