"""The key names the `input` tool accepts: a contract, frozen.

**Append-only.** A name in here is never renamed, removed or repurposed.
An agent's step list, a person's saved example, a prompt written months ago
all name keys this way, and a name that quietly starts meaning another key
is a wrong keystroke on somebody's machine. New names go in a NEW version,
and a guest declares in HELLO (`keys=`) the newest version it resolves.

**OS-neutral.** These are names, not virtual-key codes. The wire carries the
canonical name and the guest maps it to its own codes
(win/automation/input.cpp, KEY_NAMES) -- a VK is a Win32 fact, and the bridge
has to survive a DOS or NT guest unchanged. The agreement test
(tests/python/guest/test_keys_agreement.py) holds the two tables together.

Lookup ignores case. Aliases are spelled out rather than guessed: `Esc` and
`Escape` are the same key because both are listed, not because of a rule.
"""

from __future__ import annotations

#: canonical name -> the version it arrived in. Canonical spelling is what
#: goes on the wire, lower-cased.
KEYS: dict[str, int] = {
    # version 1 (2026-10-02)
    "backspace": 1, "tab": 1, "enter": 1, "escape": 1, "space": 1,
    "delete": 1, "insert": 1, "home": 1, "end": 1, "pageup": 1,
    "pagedown": 1, "up": 1, "down": 1, "left": 1, "right": 1,
    **{f"f{n}": 1 for n in range(1, 25)},
    "shift": 1, "lshift": 1, "rshift": 1,
    "ctrl": 1, "lctrl": 1, "rctrl": 1,
    "alt": 1, "lalt": 1, "ralt": 1, "altgr": 1,
    "lwin": 1, "rwin": 1, "apps": 1,
    "capslock": 1, "numlock": 1, "scrolllock": 1, "printscreen": 1,
    "pause": 1,
    **{f"numpad{n}": 1 for n in range(10)},
    "numpadmult": 1, "numpadadd": 1, "numpadsub": 1, "numpaddot": 1,
    "numpaddiv": 1, "numpadenter": 1,
}

#: other spellings -> canonical. Version is the canonical name's.
ALIASES: dict[str, str] = {
    "bksp": "backspace", "return": "enter", "esc": "escape",
    "del": "delete", "ins": "insert", "pgup": "pageup", "pgdn": "pagedown",
    "control": "ctrl", "prtsc": "printscreen",
}

#: Mouse buttons: holdable with `down`/`up` like keys, but sent as buttons,
#: so they are not in KEYS and the guest's key table never sees them.
BUTTONS: dict[str, str] = {
    "lbutton": "left", "rbutton": "right", "mbutton": "middle",
}

#: The modifiers a chord (`[Ctrl+S]`) may name, and their bits on the wire.
MODIFIERS: dict[str, int] = {"shift": 1, "ctrl": 2, "control": 2, "alt": 4}

#: The newest version this bridge knows.
LATEST = 1


def canonical(name: str) -> str | None:
    """The wire spelling of a key name, or None if it is not one."""
    n = name.strip().lower()
    n = ALIASES.get(n, n)
    return n if n in KEYS else None


def version_of(name: str) -> int:
    c = canonical(name)
    return KEYS[c] if c else 0


def all_names() -> list[str]:
    """Every accepted spelling, canonical and alias."""
    return sorted(set(KEYS) | set(ALIASES))
