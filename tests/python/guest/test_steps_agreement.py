"""The step list's sixteen verbs, held on both sides.

The `input` tool's body is a wire format like any other: the bridge writes
one verb per line and the guest parses it. Nothing pinned the vocabulary --
`test_keys_agreement.py` covers the key NAMES and `test_uirec_agreement.py`
covers the node record, and the verbs between them had no test at all. A
verb the bridge can emit and the guest cannot parse is a list that refuses
with `unknown verb` and a reason nobody can act on; a verb the guest knows
and the bridge never writes is dead code on a machine with none to spare.

Scraped rather than imported, because the point is that the two SOURCES
agree -- importing one of them would only prove it agrees with itself.
"""
from __future__ import annotations

import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import conftest

REPO = pathlib.Path(conftest.REPO)
ENGINE = REPO / "win" / "ui" / "uiengine.cpp"
STEPS_H = REPO / "win" / "automation" / "steps.h"
BRIDGE = REPO / "bridge" / "agent" / "steps.py"


def guest_verbs() -> set[str]:
    """The VERBS[] table in the guest's decoder."""
    text = ENGINE.read_text()
    body = text[text.index("VERBS[] = {"):]
    body = body[:body.index("};")]
    found = set(re.findall(r'\{\s*"([a-z]+)"\s*,\s*STEP_', body))
    assert len(found) > 10, f"only found {sorted(found)}; shape changed?"
    return found


def guest_kinds() -> set[str]:
    """The StepKind enum, which the table maps onto."""
    text = STEPS_H.read_text()
    body = text[text.index("enum StepKind"):]
    body = body[:body.index("};")]
    found = set(re.findall(r"\b(STEP_[A-Z]+)\b", body))
    assert len(found) > 10, f"only found {sorted(found)}; shape changed?"
    return found


def bridge_verbs() -> set[str]:
    """Every verb the compiler can put on the wire.

    Two shapes: a literal first argument to `emit`, and the mapping from an
    agent-facing kind to a wire verb. A conditional first argument --
    `"bd" if kind == "down" else "bu"` -- is both of its branches.
    """
    text = BRIDGE.read_text()
    verbs: set[str] = set()
    for call in re.findall(r"self\.emit\(\s*index\s*,\s*([^,]+),", text):
        # A comparison operand is not a verb: `"bd" if kind == "down" else
        # "bu"` writes bd or bu and never down. Drop the right-hand side of
        # every `==` before reading the literals.
        call = re.sub(r'==\s*"[a-z_]+"', "", call)
        verbs |= set(re.findall(r'"([a-z_]+)"', call))
    # the kind -> verb table
    block = re.search(r"verb = \{(.*?)\}\[kind\]", text, re.S)
    assert block, "the kind-to-verb mapping moved"
    verbs |= set(re.findall(r':\s*"([a-z]+)"', block.group(1)))
    verbs.discard("index")
    assert len(verbs) > 10, f"only found {sorted(verbs)}; shape changed?"
    return verbs


def test_every_verb_the_bridge_writes_is_one_the_guest_parses():
    """The direction that breaks a live session: the list is compiled, sent,
    and refused on the machine with `unknown verb`."""
    extra = bridge_verbs() - guest_verbs()
    assert not extra, (
        f"bridge/agent/steps.py can emit {sorted(extra)}, which "
        f"win/ui/uiengine.cpp's VERBS[] does not parse")


def test_every_verb_the_guest_parses_is_one_the_bridge_writes():
    """The other direction is not a live failure, so it is a question rather
    than a crash -- but a verb nothing can send is code on an image whose
    committed size is gated."""
    extra = guest_verbs() - bridge_verbs()
    assert not extra, (
        f"win/ui/uiengine.cpp parses {sorted(extra)}, which "
        f"bridge/agent/steps.py never writes")


def test_the_table_covers_every_step_kind():
    """A StepKind with no verb is unreachable from the wire, which is the
    same defect as a verb with no kind and harder to notice."""
    table = ENGINE.read_text()
    body = table[table.index("VERBS[] = {"):]
    body = body[:body.index("};")]
    mapped = set(re.findall(r"STEP_[A-Z]+", body))
    assert mapped == guest_kinds(), (
        f"unreachable from the wire: {sorted(guest_kinds() - mapped)}")
