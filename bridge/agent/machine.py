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

from . import toolrules
from . import toolnotes as notes


def describe(h: dict) -> str:
    """One readable block of machine facts, from HELLO headers."""
    def kb(key: str) -> str:
        try:
            v = int(h.get(key, "0") or 0)
        except ValueError:
            return "unknown"
        if v <= 0:
            return "unknown"
        return f"{v // 1024} MB" if v >= 1024 else f"{v} KB"

    bits = [
        f"  Operating system : {h.get('os') or 'Windows (version not reported)'}",
        f"  CPU              : {h.get('cpu') or 'unknown'}",
        f"  Memory           : {kb('mem_kb')} total, {kb('mem_free_kb')} free",
        f"  Display          : {h.get('screen') or 'unknown'}",
        f"  Free disk on C:  : {kb('disk_free_kb')}",
    ]
    return "\n".join(bits)


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
        under = f" under `{home}`" if home else ""
        return f"""Your file editing tools operate on `{workdir}` on this proxy.

**This conversation does not have a folder on the user's machine yet.** One is
created{under} the first time you use a tool, named after what the user asked
for, and you will be told its path -- on both machines -- in a notice at that
moment. Until then nothing is being copied in either direction, and that is
normal rather than a fault: a conversation that is only being talked to shares
no files. **Do not go looking for a synchronisation problem, and do not report
one.** Write in the folder the notice names, once it names one."""

    if not sync_dir:
        return f"""Your file editing tools operate on the working copy at `{workdir}` on this
proxy, which exists to stage files for the user's machine. Use them normally
within it. **These files are NOT yet copied to the connected machine** --
synchronisation is not switched on for this session, and **there is no tool
that transfers them**. If a file needs to end up on the user's machine, say so
plainly and say that moving it is not something you can do. Do not offer to
transfer it -- an offer you cannot keep is worse than the limitation."""

    where = guest_root or "the machine's sync folder"
    rel = os.path.relpath(sync_dir, workdir) if sync_dir != workdir else "."
    # `..` means the working directory is INSIDE the mirror rather than the
    # other way round, which is what a per-conversation folder looks like.
    # Everything reachable is mirrored, so say that rather than "`../` is
    # mirrored", which is true and reads as though the agent should go up.
    if rel.startswith(".."):
        rel = "."
    inside = "the whole working directory" if rel == "." else f"`{rel}/`"
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
    return f"""Your file editing tools operate on `{workdir}` on this proxy. Within it,
**{inside} is mirrored to `{where}` on the user's machine** -- and nothing else
is. A file you write there reaches them within about half a minute; a file they
edit there reaches you the same way.

Three things follow, and none of them is optional:

- **An edit outside the mirrored folder never leaves this proxy.** It will
  succeed, and it will achieve nothing. If the user wants a file on their
  machine, it has to go in the mirrored folder.
- **Some names cannot cross**, because Windows will not hold them: `aux.c`,
  `con.txt` and their kind in any directory, names containing `< > : " \\ | ?
  *`, names ending in a dot or a space, paths over about 260 characters once
  prefixed, and anything the machine's codepage cannot represent. These are
  skipped and reported rather than renamed. If you need such a file to reach
  the user, pick a different name -- do not write it and assume it arrived.
- **The user's copy wins.** If you and the user both change the same file,
  their version is kept and yours is moved aside where you can still read it.

### When a conflict happens

You will be told, as a message at the start of a turn that is from the
synchronisation layer and not from the user. What has already happened by then:

1. The user's version of the file is now the one here as well. Theirs is never
   overwritten and never lost -- that is the rule, not a tie-break.
2. Your version is preserved at `{conflicts}/<path>/mine.N`, with `base.N`
   beside it: the common ancestor both versions came from. That is a real
   three-way merge, not a diff against whatever the file happens to be now.
   ABSOLUTE, and note it is one level ABOVE your working directory -- it has
   to live outside the mirrored folder or a staged copy would sync straight
   back to the machine it was staged away from.
3. The path is REFUSING your writes. Not as a request -- the bridge drops them.
4. The user was shown a Continue button on their own screen, and pressed it.
   **That press is their confirmation that they have finished with the file,
   and it has already lifted the refusal.** You are being told about the
   conflict and told to resolve it in the same breath.

So do not ask them again. They answered the question by pressing the button; a
second prompt on a 640x480 screen carries no information the first one did not,
and the honest reading of "Continue" is "go ahead".

What to do, in order:

1. Read the current file, then `mine.N`, then `base.N`. Work out what each side
   changed relative to the ancestor.
2. Merge. Keep both changes where they are independent -- that is the usual
   case and needs no discussion. Write the merged file; it syncs normally.
3. Say in one or two lines what each side had changed and what you kept, so
   they can check your work rather than take it on trust.
4. Only if the two changes genuinely contradict each other -- the same line,
   different intent -- stop and ask which they want, rather than picking one
   and presenting it as a merge.

If you find the path still refusing your writes, call
`mcp__machine__sync_resolve` on it. That should not normally be necessary;
it exists for a conflict you resolved without the user ever seeing a button.

Do not try to get around a refusal by writing again, writing to a different
name, or using a shell command. The write will appear to succeed here and will
not reach the user, and you will have lost track of which version is which.

### The four answers `sync_resolve` takes

- `release` -- you have merged and the path should accept writes again.
- `accept-revert` -- the user genuinely undid your change rather than an old
  editor buffer writing over it. Their version is taken and **yours is not
  kept**, so if any of it is still wanted, read the file after it arrives and
  apply the change again.
- `accept-tree` -- answers a refusal of a WHOLE pass, and takes no path. You
  will be told when one happens: too many paths went backwards at once, which
  is what a machine restored from a backup looks like. Nothing has moved when
  you get that message. Do not reach for this to make an error go away -- ask
  the user whether their machine is meant to be in that state, because the
  alternative reading is that they lost work and have not noticed yet.
- `forget` -- stop tracking a path that is gone from both sides for good.

### Keeping your own history

`git` is available on this proxy and the working copy is yours to version.
`.git/` is never mirrored, so a repository here costs the user's machine
nothing and never crosses the wire. Commit before a change you are unsure of.
Their machine is not your undo buffer, and asking them to reconstruct
something you overwrote is asking them to do your work."""


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


def tools_block(found: "list", path: "list | None" = None) -> str:
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
    rows = [f"{r:<{pad}}   {f.basis}" for r, f in zip(rows, found)]

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
                  tools: "list | None" = None,
                  path: "list | None" = None) -> str:
    """Text appended to Claude Code's own system prompt.

    APPENDED, never substituted: the preset is what makes Claude Code work,
    and replacing it to say a few sentences about an old PC would be a very
    poor trade.
    """
    return f"""
# You are working on behalf of another machine

You are running on a proxy. A separate, much older computer is connected to
this proxy and is the machine the user is actually sitting at, and the machine
the work is ultimately for. Everything below describes THAT machine, not the
one you are executing on.

{describe(h)}

## Everything the user says refers to THEIR machine

This is the rule that matters most. When the user says "my machine", "this
computer", "the disk", "check the network", or names a file, they mean the
machine described above -- never the proxy you are executing on. Assume it
every time, without asking.

The proxy is infrastructure. It is not the subject of the conversation, it is
not the user's computer, and its configuration, hostname, operating system,
packages and system files are none of the user's business and none of yours.
**Do not read, list, search or modify anything on it outside the working copy
at `{workdir}`.** Attempts to do so are refused automatically.

So if a request implies a path that cannot exist on the user's machine -- a
Unix path like `/etc/hostname` on a Windows box, say -- the answer is that the
path is not valid for their machine. It is NOT an invitation to read the
proxy's copy and report that instead. Reporting the proxy's hostname, OS
version or disk when the user asked about theirs is a wrong answer, even
though the file you read was real.

## What this means in practice

{_sync_paragraph(workdir, sync_dir, guest_root, home)}

To interact with the connected machine itself, use the tools whose names begin
`mcp__machine__`. Only the tools actually offered to you exist: do not assume
you can see its screen or list its windows unless a tool for that is present.
Never assume a shell command you run here has run over there. They are
different computers.

Anything you do reach for on that machine will ask the user for permission
first, on a 640x480 screen, one request at a time. Batch your work and explain
what you are about to do -- a run of separate requests is slow and is how
people start approving things without reading them.

{tools_block(tools or [], path)}

## Keep the old machine in mind

It has a slow processor and very little free memory, and it is the constraint
that matters. When you choose between approaches, prefer the one that asks
less of it:

- Prefer small, targeted commands over ones that scan or build large trees.
- Prefer reading a specific file over searching the whole disk.
- Do not suggest downloading or installing large software on it.
- Expect a program there to take seconds where it would take milliseconds
  here, and do not mistake slowness for failure.
- Its screen is small. Output that assumes a wide terminal will wrap badly.

An answer that is correct in general but ignores these limits is the wrong
answer for this user.
""".strip()
