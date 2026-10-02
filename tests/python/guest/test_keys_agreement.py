r"""The key names the agent is offered are the key names the guest has.

`win/automation/input.cpp`'s KEY_NAMES is the only place a `[Delete]` becomes a
virtual key. `bridge/prompts/tools.toml` tells the agent which names exist. Two
lists, two languages, nothing connecting them -- and the failure is silent in
the direction that matters: a name advertised and not implemented comes back as
"not a key this knows" for a key the agent was told to use.

This exists because the gap it guards HAD a live consequence. Before the
brackets were implemented at all, the tool description promised "plain
characters plus Tab, Enter, Escape and the arrow keys" and an arrow key was not
expressible in a plain string. An agent clearing a text box sent
`[Delete][Delete][Delete][Delete]Tiberiu`, got `John[Delete][Delete][D` in the
box, and was told "Typed 39 characters" (live session on real hardware,
2026-10-02).

Reads both sources as text. No Wine, no build.
"""

from __future__ import annotations

import re
from pathlib import Path

import conftest

from bridge.agent import prompts

INPUT_CPP = Path(conftest.REPO) / "win" / "automation" / "input.cpp"


def _guest_names() -> set[str]:
    """Every name in KEY_NAMES, lower-cased as the table stores them."""
    src = INPUT_CPP.read_text()
    m = re.search(r"static const KeyName KEY_NAMES\[\]\s*=\s*\{(.*?)\n\};",
                  src, re.S)
    assert m, f"no KEY_NAMES table in {INPUT_CPP}"
    names = set(re.findall(r'\{\s*"([a-z0-9]+)"\s*,', m.group(1)))
    assert names, "KEY_NAMES parsed to nothing; the regex has gone stale"
    return names


def _advertised() -> set[str]:
    """The names the `input` tool's prose spells out, after "Key names:".

    Taken from the prose rather than a list, because the prose is what the
    agent reads -- a list kept beside it would be the thing that drifts.
    `F1-F24` and `Numpad0-Numpad9` are ranges and are expanded; `Escape (Esc)`
    is two names. (It read the `keys` tool's prose until that tool was
    retired for `input`, 2026-10-02.)
    """
    said = prompts.text("tools.input.args.steps")
    m = re.search(r"Key names: (.*?)\. Chords", said, re.S)
    assert m, "the input tool no longer lists its key names after 'Key names:'"
    out: set[str] = set()
    for item in re.split(r",\s*", m.group(1)):
        for word in re.findall(r"[A-Za-z][A-Za-z0-9]*(?:-[A-Za-z]+\d+)?",
                               item):
            rng = re.fullmatch(r"([A-Za-z]+)(\d+)-\1(\d+)", word)
            if rng:
                out |= {f"{rng.group(1).lower()}{n}"
                        for n in range(int(rng.group(2)),
                                       int(rng.group(3)) + 1)}
            else:
                out.add(word.lower())
    return out


def test_every_advertised_key_exists_on_the_guest():
    """The direction with the silent failure: a name the agent is told to use
    and the guest refuses."""
    # Modifiers are parsed separately from the name table, by
    # `test_the_modifiers_are_the_three_the_guest_parses`.
    missing = _advertised() - _guest_names() - _PROSE_WORDS - _MODIFIERS
    assert not missing, (
        f"the agent is offered {sorted(missing)}, which KEY_NAMES in "
        f"{INPUT_CPP} does not have -- using one is refused as 'not a key "
        f"this knows'")


def test_the_modifiers_are_the_three_the_guest_parses():
    """`Ctrl+`, `Alt+` and `Shift+`, and `Control` as an alias. A fourth in the
    prose would be refused as 'a word that is not a modifier'."""
    src = INPUT_CPP.read_text()
    for word in ("ctrl", "control", "shift", "alt"):
        assert f'"{word}"' in src, f"{word} is not parsed as a modifier"


def test_ctrl_alt_del_is_refused_in_the_source_that_sends_keys():
    """Not a message, a branch. On 9x the VMM owns it at ring 0, and it is
    checked on the COMPOSED modifiers because `[Ctrl+Alt+Delete]` arrives as one
    token and would otherwise miss a guard that only reads keyboard state."""
    src = INPUT_CPP.read_text()
    assert "KEYMOD_CTRL" in src and "KEYMOD_ALT" in src
    assert re.search(r"vk == VK_DELETE && \(mods & KEYMOD_CTRL\)", src), (
        "typeText no longer refuses a composed Ctrl+Alt+Delete")


def test_the_modifier_bits_are_not_windows_own():
    """`MOD_SHIFT`, `MOD_CONTROL` and `MOD_ALT` are winuser.h macros for
    RegisterHotKey with DIFFERENT values (`MOD_ALT` is 1 there, 4 here). Watcom
    caught the collision as a syntax error; had the names merely shadowed, every
    modifier would have been silently wrong."""
    src = INPUT_CPP.read_text()
    assert "KEYMOD_SHIFT = 1" in src
    assert re.search(r"\bMOD_SHIFT\s*=", src) is None, (
        "the modifier enum is back on Windows' own macro names")


#: Parsed by `parseKeyBody`, not looked up in KEY_NAMES.
_MODIFIERS = {"ctrl", "control", "shift", "alt"}

#: Words that appear in the prose and are not key names -- the sentence has to
#: read as English, so it contains some. Listed rather than excluded by a looser
#: regex, which would also stop catching a real omission.
_PROSE_WORDS = {
    "characters", "named", "keys", "backspace", "an", "unknown", "name",
    "types", "nothing", "says", "write", "literal", "bracket", "box", "clear",
    "optional", "hello", "world", "four", "the", "and", "with", "each",
    "taking", "is", "a", "to", "not", "this", "knows", "so", "are", "for",
    "typed",
}


def test_every_frozen_key_name_resolves_on_the_guest():
    """The `input` tool's contract: every name in the bridge's frozen table
    (bridge/agent/keynames.py) and every alias is one the guest's KEY_NAMES
    maps. A name the bridge accepts and the guest does not is refused at run
    time as a bad key -- after the steps before it already ran."""
    from bridge.agent import keynames as K
    missing = (set(K.KEYS) | set(K.ALIASES)) - _guest_names()
    assert not missing, (
        f"bridge/agent/keynames.py accepts {sorted(missing)}, which KEY_NAMES "
        f"in {INPUT_CPP} does not resolve")


def test_the_guest_has_no_name_the_frozen_table_lacks():
    """The other direction, for the names a CHORD or a hold can reach: a
    guest-only name is one the bridge refuses and the agent can never use,
    which is drift waiting to be mistaken for a feature."""
    from bridge.agent import keynames as K
    extra = _guest_names() - set(K.KEYS) - set(K.ALIASES)
    assert not extra, (
        f"KEY_NAMES has {sorted(extra)}, which bridge/agent/keynames.py does "
        f"not list")
