"""What the bridge knows about the tools on the other machine, as data.

ONE FILE PER TOOL under `bridge/profiles/`, named for what the guest reports
it as. Each holds everything the bridge knows about that tool: how to
recognise it from what the client found (`[detect]`), what to tell the
assistant when it is present (`[note]`), and -- for a shell -- the tables the
command guard reads (`[shell]`).

WHY DATA AND NOT PYTHON. Three reasons, in order of how much they cost when
ignored:

  * None of it is logic. A verb list, a set of reserved device names, a
    paragraph about a linker flag -- these are facts about somebody else's
    computer, and facts about somebody else's computer change without this
    program being rebuilt.
  * The bridge is supposed to survive being pointed at MS-DOS, Windows 3.1 or
    NT 4 (`CLAUDE.md`, first standing rule). That claim is only honest if
    supporting a new machine means adding a file. While `command.com`'s verb
    table lived in `doscmd.py`, the tool surface was portable and its guard
    was not.
  * A fact written in Python reads as a decision the program makes. A fact
    written in a profile reads as a claim about a machine, which is what it
    is -- and can carry its own provenance next to it.

TOML rather than JSON because every one of these entries has a reason
attached, and the reason is the part that stops the next person "tidying up"
a line that was measured the hard way. JSON cannot hold a comment.

This supersedes the note in `toolnotes.py` arguing for Python string
literals on the grounds that `bridge/` had no non-`.py` files. That was true
and it was a weak argument: it described the tree rather than the problem.

REFUSING IS NOT AN OPTION. A profile that will not parse costs one tool's
knowledge; raising during the handshake costs the user their machine. A bad
file is logged by name and skipped, which is the same posture `toolrep` takes
for a bad wire row and the opposite of `Baseline`'s, which refuses because
the alternative there is data loss.
"""

from __future__ import annotations

import logging
import pathlib
import tomllib

log = logging.getLogger("bridge.profiles")

DIR = pathlib.Path(__file__).resolve().parent.parent / "profiles"


def _load() -> dict:
    out: dict = {}
    if not DIR.is_dir():
        log.error("no profile directory at %s; nothing is known about any "
                  "tool and detection will report nothing", DIR)
        return out
    for path in sorted(DIR.glob("*.toml")):
        try:
            with path.open("rb") as fh:
                out[path.stem] = tomllib.load(fh)
        except Exception as exc:
            # By name, always. The failure this guards against is a profile
            # that silently stops loading and a tool that silently stops
            # being known -- which looks like the detector breaking.
            log.error("profile %s did not parse (%s); skipping it", path.name, exc)
    if not out:
        log.error("no profiles loaded from %s", DIR)
    return out


#: Read once at import. These are shipped files that change with a deploy,
#: not user data that changes under a running process.
ALL: dict = _load()


def detect(name: str) -> dict:
    return ALL.get(name, {}).get("detect", {})


def note(name: str) -> dict:
    return ALL.get(name, {}).get("note", {})


def shell(name: str) -> dict:
    """The command guard's tables for one shell, or {} if it has none."""
    return ALL.get(name, {}).get("shell", {})


def shells() -> list[str]:
    """Every profile that carries guard tables."""
    return [n for n, p in ALL.items() if p.get("shell")]


def names() -> list[str]:
    return sorted(ALL)
