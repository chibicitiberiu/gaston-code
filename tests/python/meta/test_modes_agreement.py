"""The client's preset table is the bridge's, cell for cell.

The client SHOWS a preset (Options > Permissions) and the bridge ENFORCES
one; a client table that read differently would describe a policy that is
not the one in force -- the one way to make the dialog dishonest without
touching a line of dialog code. Parsed from win/app/modes.cpp so the two
cannot drift.
"""
import pathlib
import re

import conftest

from bridge.agent import categories as K
from bridge.agent import policy as P

SRC = pathlib.Path(conftest.REPO) / "win" / "app" / "modes.cpp"
LETTER = {"o": P.OFF, "a": P.ASK, "c": P.CHECK, "y": P.ALLOW}


def _client():
    text = SRC.read_text(encoding="utf-8")
    cats = re.search(r"RC_CAT_NAMES\[RC_CATS\]\s*=\s*\{(.*?)\};", text, re.S)
    names = re.findall(r'"([a-z_]+)"', cats.group(1))
    rows = re.findall(r'\{\s*"([a-z]+)",\s*"([^"]+)",\s*"([ocay]+)",\s*'
                      r'(true|false)\s*\}', text)
    return names, rows


def test_the_categories_are_the_bridges_in_its_order():
    names, _ = _client()
    assert tuple(names) == K.CATS


def test_every_preset_matches_cell_for_cell():
    _, rows = _client()
    assert [r[0] for r in rows] == list(P.PRESET_ORDER)
    for pid, label, letters, _locked in rows:
        bridge = P.preset(pid)
        assert label == bridge.label, pid
        assert len(letters) == len(K.CATS), pid
        for cat, letter in zip(K.CATS, letters, strict=True):
            assert LETTER[letter] == bridge.value(cat), (pid, cat)


def test_only_plan_first_is_locked():
    _, rows = _client()
    assert [r[0] for r in rows if r[3] == "true"] == ["plan"]
