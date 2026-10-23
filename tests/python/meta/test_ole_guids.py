"""The spelled-out OLE GUIDs agree across the three files that hold them.

`win/app/htmlview.cpp` and `win/automation/calldeadline.cpp` define their own
`RC_IID_*` constants rather than linking `uuid.lib`, because that library is a
single object module and one reference to it pulls all 1,970 GUIDs -- 31,744
bytes of committed image in four exes, measured 2026-10-11
(`research/measurements/imagebytes-host-20261011.txt`).

`tests/guest/cppcheck.cpp` proves that table right by `memcmp`-ing it against
`uuid.lib`'s own copies at run time, which is the comparison that matters and
needs the toolchain. This file closes the other half, and needs nothing: that
the two SHIPPING copies still say what the checked table says. Without it a
shipping constant could be edited and the check exe would go on agreeing with
itself.

Why it matters more than its size suggests: a wrong byte here does not fail a
gate. `CoCreateInstance(CLSID_WebBrowser, ...)` fails on the guest and the
transcript never appears, and `win/Makefile:60-66` records that the Win32 GUI
layer has no headless runner to catch it.
"""
from __future__ import annotations

import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[3]

CHECKED = ROOT / "tests" / "guest" / "cppcheck.cpp"
SHIPPING = (
    ROOT / "win" / "app" / "htmlview.cpp",
    ROOT / "win" / "automation" / "calldeadline.cpp",
)

#: Every GUID the client and gremote spell out for themselves. The count is
#: asserted so a constant that stops being defined fails loudly rather than
#: silently dropping out of the comparison -- the same reason
#: test_uirec_agreement.py pins its case count.
EXPECTED_SHIPPING = 14

# `static const GUID RC_IID_IOleObject =
#      { 0x00000112, 0x0000, 0x0000,
#        { 0xc0, 0x00, ... } };`
DEFINITION = re.compile(
    r"static\s+const\s+GUID\s+RC_(\w+)\s*=\s*"
    r"\{\s*(0x[0-9a-fA-F]+)\s*,\s*(0x[0-9a-fA-F]+)\s*,\s*(0x[0-9a-fA-F]+)\s*,"
    r"\s*\{([^}]*)\}\s*\}\s*;",
    re.S,
)

# A row of cppcheck.cpp's table: `{ "IID_IOleObject", &IID_IOleObject,
#      { 0x00000112, 0x0000, 0x0000, { 0xc0, ... } } },`
TABLE_ROW = re.compile(
    r'\{\s*"(\w+)"\s*,\s*&\w+\s*,\s*'
    r"\{\s*(0x[0-9a-fA-F]+)\s*,\s*(0x[0-9a-fA-F]+)\s*,\s*(0x[0-9a-fA-F]+)\s*,"
    r"\s*\{([^}]*)\}\s*\}\s*\}",
    re.S,
)


def _bytes(m: re.Match[str]) -> tuple[int, ...]:
    tail = tuple(int(b, 16) for b in re.findall(r"0x[0-9a-fA-F]+", m.group(5)))
    return (int(m.group(2), 16), int(m.group(3), 16),
            int(m.group(4), 16), *tail)


def _checked_table() -> dict[str, tuple[int, ...]]:
    text = CHECKED.read_text(encoding="utf-8")
    return {m.group(1): _bytes(m) for m in TABLE_ROW.finditer(text)}


def _shipping_definitions() -> dict[str, tuple[tuple[int, ...], str]]:
    found: dict[str, tuple[tuple[int, ...], str]] = {}
    for path in SHIPPING:
        text = path.read_text(encoding="utf-8")
        for m in DEFINITION.finditer(text):
            name = m.group(1)
            where = "%s" % path.relative_to(ROOT)
            prev = found.get(name)
            if prev is not None:
                assert prev[0] == _bytes(m), (
                    "%s disagrees between %s and %s" % (name, prev[1], where))
            found[name] = (_bytes(m), where)
    return found


def test_every_shipping_guid_is_in_the_checked_table():
    """A constant nothing checks is a constant that can be wrong."""
    checked = _checked_table()
    assert len(checked) == 13, sorted(checked)
    missing = {}
    for name, (value, where) in _shipping_definitions().items():
        # IMessageFilter is automation's own and is not an OLE interface the
        # client hosts, so uuid.lib does not carry it for cppcheck to diff.
        if name == "IID_IMessageFilter":
            continue
        if name not in checked:
            missing[name] = where
    assert not missing, (
        "spelled out in shipping code but never compared against uuid.lib: %s"
        % missing)


def test_the_shipping_bytes_match_the_checked_bytes():
    checked = _checked_table()
    wrong = {}
    for name, (value, where) in _shipping_definitions().items():
        want = checked.get(name)
        if want is not None and want != value:
            wrong[name] = (where, value, want)
    assert not wrong, (
        "a shipping GUID no longer matches the table cppcheck verifies: %s"
        % wrong)


def test_the_count_is_pinned_so_one_cannot_quietly_vanish():
    found = _shipping_definitions()
    assert len(found) == EXPECTED_SHIPPING, sorted(found)


def test_nobody_reintroduced_the_stock_names():
    """`static const GUID IID_IOleObject` does not compile, and the reason is
    worth keeping written down: oleidl.h, exdisp.h and mshtml.h declare those
    names as `EXTERN_C const IID`, so a file-scope static of the same name is
    a redeclaration with different linkage. The RC_ prefix is load-bearing."""
    for path in SHIPPING:
        text = path.read_text(encoding="utf-8")
        bad = re.findall(r"static\s+const\s+GUID\s+(?!RC_)(\w+)", text)
        assert not bad, "%s: %s" % (path.relative_to(ROOT), bad)
