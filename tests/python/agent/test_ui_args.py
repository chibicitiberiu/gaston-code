"""`ui`'s arguments have to mean what they say.

One of these was reported from a real session (2026-10-01): the agent asked
for the windows of one process and got a 25-control dump of the whole desktop,
could not find what it was looking for in it, and had no reason to suspect the
scope it asked for was not the scope it got. `int(a.get("pid") or 0)` makes
"absent" and "0" the same value and the guest reads 0 as "no filter", so the
narrowest scope on offer silently became the widest.
"""
from __future__ import annotations

import asyncio

from bridge.agent import tools as machine_tools
from bridge.files import caps as C


class FakeUi:
    def __init__(self):
        self.calls: list[dict] = []

    async def tree(self, guest, **kw):
        self.calls.append(kw)
        return [], {}


class AutomationGuest:
    caps = C.CONSTRAINED.__class__(automation=C.AUTOMATION_SCREEN)


class _Params:
    def __init__(self, name, arguments):
        self.name, self.arguments = name, arguments


def _call(srv, name, args):
    handler = srv._request_handlers["tools/call"].handler
    return asyncio.run(handler(None, _Params(name, args)))


def _body(res) -> str:
    content = res["content"] if isinstance(res, dict) else res.content
    return "\n".join(c["text"] if isinstance(c, dict) else c.text
                     for c in content)


def _server(ui):
    return machine_tools.build(None, None, None, AutomationGuest(),
                               ui=ui)["machine"]["instance"]


def test_scope_pid_without_a_pid_is_refused():
    ui = FakeUi()
    out = _body(_call(_server(ui), "ui", {"scope": "pid"}))
    assert ui.calls == []            # the machine was never asked
    assert "ps" in out               # and the agent is told how to get one


def test_scope_pid_with_zero_is_the_same_refusal():
    """What the live session actually passed."""
    ui = FakeUi()
    out = _body(_call(_server(ui), "ui", {"scope": "pid", "pid": 0}))
    assert ui.calls == []
    assert "0 is not one" in out


def test_a_pid_with_the_wrong_scope_is_refused_too():
    """The other direction, and it was equally silent: a pid passed with
    `scope: "desktop"` was simply ignored, so the agent believed it had
    narrowed something."""
    ui = FakeUi()
    out = _body(_call(_server(ui), "ui", {"scope": "desktop", "pid": 4321}))
    assert ui.calls == []
    assert "4321" in out


def test_scope_pid_with_a_pid_goes_through():
    ui = FakeUi()
    _call(_server(ui), "ui", {"scope": "pid", "pid": 4321})
    assert ui.calls[0]["scope"] == "pid"
    assert ui.calls[0]["pid"] == 4321


def test_the_default_scope_still_needs_no_pid():
    ui = FakeUi()
    _call(_server(ui), "ui", {})
    assert ui.calls[0]["scope"] == "desktop"
    assert ui.calls[0]["pid"] == 0


# --- which PART of a compound selector was missing -------------------------
#
# `A > B` failing said only "Nothing on screen matches that selector ... the
# window may not be open", and an agent believed it: in a live session
# `HelloGuiClass[text="Hello Application"] > Button[text="Close"]` came back
# that way while the window was on screen and the program simply had no Close
# button (2026-10-02). The guest sends the index of the part that matched
# nothing; the text is recovered from the selector the caller sent.

def test_the_failing_part_is_named():
    from bridge.agent.tools import _ui_explain
    from bridge.services.ui import UiError
    exc = UiError("not_found", "", {"step": "1"})
    out = _ui_explain(exc, 'HelloGuiClass[text="Hello App"] > Button[text="Close"]')
    assert 'Button[text="Close"]' in out
    assert "the rest of it did match" in out
    # And it must NOT still be telling the agent the window may be gone.
    assert "may not be open" not in out


def test_the_container_is_named_when_that_is_what_was_missing():
    from bridge.agent.tools import _ui_explain
    from bridge.services.ui import UiError
    out = _ui_explain(UiError("not_found", "", {"step": "0"}),
                      "NoSuchWindow > Button")
    assert "NoSuchWindow" in out


def test_a_single_part_selector_gets_the_plain_message():
    """Nothing to apportion blame between, so naming a part would be noise.
    The guest does not send `step` for a one-component selector either."""
    from bridge.agent.tools import _ui_explain
    from bridge.services.ui import UiError
    out = _ui_explain(UiError("not_found"), "Button")
    assert "Read the screen again" in out


def test_a_quoted_angle_bracket_is_not_a_separator():
    """`Button[text="a > b"]` is ONE component. Miscounting shifts the index
    and names the wrong part, which is the failure being fixed."""
    from bridge.agent.tools import _selector_part
    assert _selector_part('Window > Button[text="a > b"]', "1") == \
        'Button[text="a > b"]'


def test_a_nonsense_step_names_nothing_rather_than_guessing():
    from bridge.agent.tools import _selector_part
    assert _selector_part("A > B", "7") == ""
    assert _selector_part("A > B", "") == ""


def test_an_unknown_key_is_reported_with_the_token():
    """`bad_key` alone sends the agent hunting through its own string. And the
    message has to lead with "nothing was typed", because the thing it will
    otherwise assume is that some of the string went in."""
    from bridge.agent.tools import _ui_explain
    from bridge.services.ui import UiError
    out = _ui_explain(UiError("bad_key", "", {"key": "[Frobnicate]"}))
    assert "[Frobnicate]" in out
    assert "NOTHING was typed" in out
    assert "Delete" in out          # and what it could have written instead


# --- Ctrl+A is not select-all in the control this OS supplies ---------------
#
# Measured 2026-10-02 on a custom Win32 edit control: `[Ctrl+A][Delete]` left it
# unchanged, twice consecutively, with a settle against the read-too-early race
# that had fooled an earlier run. It does nothing and nothing reports that.
#
# A PROGRAM MAY ADD THE ACCELERATOR, and one measured 2026-10-11 does: two text
# editors on the same OS, 1999 and 2005, disagree
# (research/measurements/clipboard-athlon-20261011.txt). So the note says "may have"
# -- an agent that watches the note be wrong learns to discount the next one.
#
# THE ARGUMENT DESCRIPTION ALREADY SAID SO AND THE AGENT DID IT ANYWAY, in the
# live VM run this was added during. An argument description is the part of a
# schema a model attends to least.

def test_ctrl_a_is_called_out_on_the_result():
    from bridge.agent.tools import _ctrl_a_note
    for text in ("[Ctrl+A]goodbye", "{control+a}x", "[ctrl + A]", "[CTRL+A]"):
        assert _ctrl_a_note(text), text
    note = _ctrl_a_note("[Ctrl+A]x")
    assert "select-all" in note
    assert "may have" in note               # not "did nothing": see above
    assert "[Home][Shift+End]" in note      # and what to do instead


def test_nothing_else_trips_it():
    """A note on every call would be noise, and `[Ctrl+Alt+A]` is a different
    key combination that nothing has measured."""
    from bridge.agent.tools import _ctrl_a_note
    for text in ("hello", "[Shift+A]", "[Ctrl+Alt+A]", "[Ctrl+B]", "a[[b"):
        assert not _ctrl_a_note(text), text


def test_it_is_a_note_and_not_a_refusal():
    """Two apps are not every app. An application that implements the
    accelerator itself would be blocked by a refusal for no reason; this costs
    a sentence and leaves the capability alone."""
    from bridge.agent.tools import _ctrl_a_note
    assert _ctrl_a_note("[Ctrl+A]x").strip().startswith("Note:")


# --- the reply has to say what actually happened ---------------------------
#
# The keystroke-count tests that lived here went with the `keys` tool
# (2026-10-02). `input` reports keystrokes per step from the guest's own
# count -- tests/python/agent/test_input_steps.py.


def test_an_ambiguous_wait_is_not_reported_as_nothing_found():
    """THE ONE THAT SENT AN AGENT LOOKING FOR AN OPEN WINDOW. `ui
    wait_for='[text="Control Panel"]'` came back `end=3 ... matched=3` and the
    bridge rendered it "Nothing on screen matches that selector ... the window
    may not be open". Three things matched; the title appears on the frame, the
    toolbar and the address bar. The guest sends `ambiguous` for that now."""
    from bridge.agent.tools import _ui_explain
    from bridge.services.ui import UiError
    out = _ui_explain(UiError("ambiguous", "3 controls match after 5005ms"))
    assert "Multiple results found" in out
    assert "Nothing on screen" not in out
    assert "ancestor" in out         # and what to do about it


def test_a_target_without_inproc_is_refused():
    """`target` only travels on the inproc branch (protocol.ui_tree), so
    without `inproc` it was dropped on the way out and the agent got the
    whole scope back believing it had named one control."""
    ui = FakeUi()
    out = _body(_call(_server(ui), "ui", {"target": "Notepad"}))
    assert ui.calls == []
    assert "inproc" in out


def test_a_target_with_inproc_goes_through():
    ui = FakeUi()
    _call(_server(ui), "ui", {"target": "Notepad", "inproc": True})
    assert ui.calls[0]["target"] == "Notepad"


def test_sync_resolve_does_not_require_a_path():
    """`accept-tree` has no path, and the code accepts that; the schema used
    to say otherwise (the Annotated shorthand marks everything required)."""
    srv = _server(FakeUi())
    listed = asyncio.run(srv._request_handlers["tools/list"].handler(None, None))
    tools = listed.root.tools if hasattr(listed, "root") else listed.tools
    schema = next(t.input_schema for t in tools if t.name == "sync_resolve")
    assert "path" not in schema.get("required", [])


def test_items_of_asks_for_one_controls_items():
    """What `input`'s `item` targets, listed: the tree service is asked for
    the accessible children of the one control named."""
    ui = FakeUi()
    out = _body(_call(_server(ui), "ui", {"items_of": "SysListView32"}))
    assert ui.calls[0]["acc"] is True
    assert ui.calls[0]["target"] == "SysListView32"
    assert "Items inside" in out


def test_an_item_line_says_the_name_to_pass():
    from bridge.wire.uirec import UiNode
    from bridge.wire.uiview import render_tree
    n = UiNode(src="a", path="0", parent="", ctrl=0, child=3,
               rect=(10, 20, 60, 36), flags=1, cls="", text="", role="list item",
               name="readme.txt", value="", state="selectable", action="")
    out = render_tree([n], {"nodes": "1", "walk": "complete"})
    assert 'item="readme.txt"' in out


def test_the_frame_for_items_carries_the_control():
    from bridge.wire import protocol as P
    f = P.ui_tree("u1", acc=True, target="SysListView32")
    assert f.headers["acc"] == "1" and f.headers["target"] == "SysListView32"
