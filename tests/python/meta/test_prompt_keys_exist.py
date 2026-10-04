"""Every prompt fragment the bridge names in code exists.

`prompts.text` raises on a missing key, which is right -- but only when that
line runs, and the lines that name a fragment are refusal and timeout paths
that nothing exercises until they fire. On 2026-10-04 four policy messages
were appended below a table header in policy.toml, so TOML filed them into
that table and every one of them would have raised at runtime; a timeout test
was what noticed. This reads each literal key from the source and loads it.
"""
from __future__ import annotations

import pathlib
import re

import conftest
import pytest

from bridge.agent import prompts

ROOT = pathlib.Path(conftest.REPO) / "bridge"
#: prompts.text("x.y"), and the local aliases the bridge binds it to.
CALL = re.compile(r'(?:prompts\.text|prompts_text|policy_text|_t)\(\s*'
                  r'"([a-z_][a-z0-9_.]*)"')
#: `_t` in categories.py prefixes "policy.rule.".
PREFIX = {"categories.py": "policy.rule."}


def _keys():
    for path in sorted(ROOT.rglob("*.py")):
        if "devtools" in path.parts:
            continue
        text = path.read_text(encoding="utf-8")
        for m in CALL.finditer(text):
            key = m.group(1)
            if m.group(0).startswith("_t(") and path.name in PREFIX:
                key = PREFIX[path.name] + key
            elif m.group(0).startswith("_t("):
                continue
            if key.endswith("."):
                continue            # a prefix, completed at runtime
            yield f"{path.relative_to(ROOT)}", key


@pytest.mark.parametrize("where,key", sorted(set(_keys())))
def test_the_fragment_exists(where, key):
    node = prompts.all()
    for part in key.split("."):
        assert isinstance(node, dict) and part in node, (where, key)
        node = node[part]
    assert isinstance(node, str), (where, key)
