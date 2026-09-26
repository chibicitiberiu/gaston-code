"""The control record: the guest writes it, the bridge reads it.

A cross-language agreement, so it is tested the way the other agreements in
this suite are -- the guest binary itself produces the bytes and the Python
parser is held against them. Nothing here reimplements the format on either
side, which is the whole point: a fixture written by hand would drift from
the encoder the first time somebody changed a field.

What this is really defending is the decision NOT to use `key=value` lines.
A window's text comes back through `WM_GETTEXT` and can contain anything
somebody typed into an Edit control, newlines included, so the record that
matters most here is the multi-line one.
"""
import pathlib
import sys

import conftest
import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[3]))
from bridge.wire import uirec

EXE_NAME = "uicheck.exe"
pytestmark = conftest.needs_guest


@pytest.fixture(scope="module")
def verdicts():
    return conftest.guest_verdicts(EXE_NAME, timeout=60)


def test_every_case_passes(verdicts):
    bad = {k: v for k, v in verdicts.items() if v[0] != "pass"}
    assert not bad, bad


def test_every_case_ran(verdicts):
    #: A hard count, so a case that stops being emitted fails rather than
    #: vanishing. Raise it deliberately when cases are added.
    assert len(verdicts) == 31, sorted(verdicts)


@pytest.fixture(scope="module")
def nodes(tmp_path_factory):
    out = tmp_path_factory.mktemp("uirec") / "uirec.bin"
    conftest.run_guest(EXE_NAME, "--emit", conftest.wine_path(out), timeout=60)
    body = out.read_bytes()
    assert body, "the guest wrote no records"
    return uirec.parse_records(body, "cp1252")


def test_every_record_parsed(nodes):
    #: Five records go in. A parser that resynchronised past a bad one would
    #: come back with four and otherwise look fine.
    assert len(nodes) == 5, [n.path for n in nodes]


def test_a_window_round_trips(nodes):
    root = nodes[0]
    assert root.src == "w"
    assert root.path == "0"
    assert root.parent == ""          # a root has none
    assert root.cls == "#32770"
    assert root.text == "Save As"
    assert root.rect == (12, 40, 300, 60)
    assert root.visible and root.enabled and not root.focused


def test_the_parent_path_is_carried(nodes):
    ok = nodes[1]
    assert ok.path == "0/1"
    assert ok.parent == "0"
    assert ok.ctrl == 1
    assert ok.focused


def test_msaa_fields_survive(nodes):
    ok = nodes[1]
    assert ok.name == "OK"
    assert ok.state == "focusable"
    assert ok.action == "press"


def test_text_with_a_newline_in_it(nodes):
    """The record the format exists for.

    `key=value` lines would have turned this one control into three records
    that each looked plausible, which is worse than failing.
    """
    edit = nodes[2]
    assert edit.cls == "Edit"
    assert edit.text == "line one\nline two\ttabbed"


def test_a_field_that_filled_its_buffer(nodes):
    """`Node::text` is a fixed 128 bytes and a WM_GETTEXT that filled it
    exactly leaves no terminator for strlen to find."""
    full = nodes[3]
    assert full.cls == "Static"
    assert len(full.text) == 128
    assert set(full.text) == {"x"}


def test_an_accessible_element_is_not_a_window(nodes):
    """A list item, a menu entry and every element of a web page have no
    HWND. They arrive with a child id where a window has a control id."""
    link = nodes[4]
    assert link.src == "a"
    assert link.child == 7
    assert link.cls == ""             # it is not a window
    assert link.role == "link"
    assert link.name == "First link"
    assert link.value == "http://example.com/one"
    assert link.rect == (4, 8, 90, 24)


def test_no_record_carries_a_handle(nodes):
    """An HWND is meaningless across a session and 9x reuses them, so one
    that escaped to the bridge could be sent back and click whatever
    inherited it. `UiNode` has no field for one; this asserts the dataclass
    has not quietly grown one."""
    fields = {f.name for f in uirec.dataclasses.fields(uirec.UiNode)}
    assert not (fields & {"hwnd", "handle", "window"}), fields


def test_a_truncated_record_is_skipped_not_guessed(nodes):
    """Cutting the last record short must lose that record and keep the
    rest, never read past its declared length into whatever follows."""
    good = b"w 0 - 0 0 0 0 10 10 3 1 1 0 0 0 0 0 AB\n"
    assert len(uirec.parse_records(good)) == 1
    assert uirec.parse_records(good + b"w 0/1 0 0 0 0 0 5 5 3 9 0 0 0 0 0 0 xy\n") \
        [0].cls == "A"


def test_a_short_body_does_not_raise(nodes):
    """One bad record must not cost the whole tree, and must not take the
    connection down with it either -- the manifest parser learned that the
    hard way."""
    assert uirec.parse_records(b"") == []
    assert uirec.parse_records(b"nonsense\n") == []
    assert uirec.parse_records(b"w 0 - 0 0 0 0 1 1 3 99 0 0 0 0 0 0 ab") == []
