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


def system_prompt(h: dict, workdir: str) -> str:
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

## What this means in practice

Your file editing tools operate on the working copy at `{workdir}` on this
proxy. Use them normally. Changes are synchronised to the connected machine
automatically, with a short delay -- you do not need to copy files across
yourself, and you should not try to.

To interact with the connected machine itself -- running a program on it,
looking at its screen, listing its windows -- use the tools provided for that
purpose. Never assume a shell command you run here has run over there. They
are different computers.

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
