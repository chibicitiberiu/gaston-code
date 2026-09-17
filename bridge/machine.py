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
                    guest_root: str | None) -> str:
    """What to say about files, which depends on whether sync is configured.

    Two paragraphs, and only one of them is ever true at a time. Getting this
    wrong in either direction costs the user real work: promising a transfer
    that does not exist means the model reports a file as delivered when it is
    sitting on a Linux box the user cannot see, and denying one that does exist
    means it refuses a thing it could have done.
    """
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
    inside = "the whole working directory" if rel == "." else f"`{rel}/`"
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
- **The user's copy wins.** If you and the user both change the same file, the
  machine's version is kept and yours is moved aside where you can still read
  it. You will be told when this happens. When you are, STOP editing that file,
  tell the user, and merge only once they say they have finished with it -- the
  bridge will refuse your writes to it until then, so continuing is not a
  disagreement you can win."""


def system_prompt(h: dict, workdir: str, sync_dir: str | None = None,
                  guest_root: str | None = None) -> str:
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

{_sync_paragraph(workdir, sync_dir, guest_root)}

To interact with the connected machine itself, use the tools whose names begin
`mcp__machine__`. Only the tools actually offered to you exist: do not assume
you can see its screen or list its windows unless a tool for that is present.
Never assume a shell command you run here has run over there. They are
different computers.

Anything you do reach for on that machine will ask the user for permission
first, on a 640x480 screen, one request at a time. Batch your work and explain
what you are about to do -- a run of separate requests is slow and is how
people start approving things without reading them.

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
