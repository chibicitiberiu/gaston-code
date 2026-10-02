"""The `input` tool's step list: what is refused before anything is sent,
what goes on the wire, and how the guest's answer is told back."""
from __future__ import annotations

import asyncio

import pytest

from bridge.agent import keynames as K
from bridge.agent import steps as S
from bridge.agent import tools as machine_tools
from bridge.files import caps as C


def _lines(c: S.Compiled) -> list[str]:
    return c.body.decode("latin-1").rstrip("\n").split("\n")


# --- compiling ----------------------------------------------------------


def test_a_string_splits_into_text_and_chords():
    c = S.compile_steps(["hi[Enter][Ctrl+S]"])
    assert _lines(c) == ["type\ttext=hi", "tap\tkey=enter",
                         "tap\tkey=S\tmods=2"]
    assert c.origin == [1, 1, 1]


def test_a_doubled_bracket_is_a_literal():
    assert _lines(S.compile_steps(["a[[b"])) == ["type\ttext=a[b"]


def test_text_is_sent_in_the_guests_code_page_and_escaped():
    c = S.compile_steps(["€\tx%"])
    assert c.body == b"type\ttext=\x80%09x%25\n"


def test_a_selector_in_a_german_caption_is_code_page_bytes_too():
    """Compared on the guest against window text in its own code page --
    UTF-8 would never match."""
    c = S.compile_steps([{"focus": "[text~=\"Öffnen\"]"}])
    assert b"\xd6ffnen" in c.body


def test_a_character_the_code_page_lacks_is_refused_by_step():
    with pytest.raises(S.StepError) as e:
        S.compile_steps([{"focus": "Notepad"}, "中"])
    assert e.value.index == 2


def test_an_unknown_key_name_is_refused_with_the_names():
    with pytest.raises(S.StepError) as e:
        S.compile_steps([{"down": "Hyper"}, {"up": "Hyper"}])
    assert "Hyper" in str(e.value) and "altgr" in str(e.value)


def test_a_hold_never_released_is_refused_naming_where_it_began():
    with pytest.raises(S.StepError) as e:
        S.compile_steps(["x", {"down": "Shift"}, "y"])
    assert e.value.index == 2 and "up" in str(e.value)


def test_a_release_never_held_is_refused():
    with pytest.raises(S.StepError):
        S.compile_steps([{"up": "Ctrl"}])


def test_aliases_hold_and_release_the_same_key():
    c = S.compile_steps([{"down": "Control"}, "[s]", {"up": "ctrl"}])
    assert _lines(c)[0] == "kd\tkey=ctrl" and _lines(c)[2] == "ku\tkey=ctrl"


def test_ctrl_alt_delete_is_never_sent():
    with pytest.raises(S.StepError):
        S.compile_steps(["[Ctrl+Alt+Delete]"])
    with pytest.raises(S.StepError):
        S.compile_steps([{"down": "Ctrl"}, {"down": "Alt"}, {"down": "Del"},
                         {"up": "Del"}, {"up": "Alt"}, {"up": "Ctrl"}])


def test_timeouts_out_of_range_are_refused_not_clamped():
    with pytest.raises(S.StepError) as e:
        S.compile_steps([{"wait_for": "X", "timeout": 120_001}])
    assert "120000" in str(e.value)
    with pytest.raises(S.StepError):
        S.compile_steps(["x"], timeout=600_001)
    assert S.compile_steps(["x"], timeout=600_000).timeout_ms == 600_000
    assert S.compile_steps(["x"]).timeout_ms == 120_000


def test_a_misspelt_field_is_refused_rather_than_ignored():
    with pytest.raises(S.StepError) as e:
        S.compile_steps([{"wait_for": "X", "timout": 5000}])
    assert "timout" in str(e.value)


def test_a_drag_is_a_held_button_and_moves():
    c = S.compile_steps([
        {"down": "LButton", "where": "Notepad Edit", "text": "hello"},
        {"move": "Notepad Edit", "text": "hello", "edge": "end"},
        {"up": "LButton"}])
    assert _lines(c) == [
        "bd\tbutton=left\tsel=Notepad Edit\ttext=hello",
        "mv\tsel=Notepad Edit\ttext=hello\tedge=end",
        "bu\tbutton=left"]


def test_screen_pixels_are_their_own_form():
    c = S.compile_steps([{"click": {"screen": [10, 20]}, "in": "Notepad"}])
    assert _lines(c) == ["click\tbutton=left\tscreen=10,20\tsel=Notepad"]
    with pytest.raises(S.StepError):
        S.compile_steps([{"click": {"screen": [10, 20]}, "item": "x"}])


def test_item_and_text_name_different_places():
    with pytest.raises(S.StepError):
        S.compile_steps([{"click": "List", "item": "a", "text": "b"}])


def test_the_line_cap_counts_expanded_lines():
    with pytest.raises(S.StepError):
        S.compile_steps(["[a]" * (S.MAX_LINES + 1)])


def test_the_permission_line_says_what_each_step_does():
    line = S.summary([{"focus": "Notepad"}, "hi[Enter]",
                      {"click": "List", "item": "readme.txt",
                       "double": True}, {"close": "Notepad"}])
    assert line == ('bring Notepad to the front · type "hi" + Enter · '
                    'double-click List item "readme.txt" · close Notepad')


def test_a_malformed_list_still_gets_a_line():
    assert "malformed" in S.summary([{"nonsense": 1}])


def test_the_reply_body_parses():
    rows, released = S.parse_reply(
        b"i=0\tstatus=ok\tms=12\thit=Notepad \"x\"\n"
        b"i=1\tstatus=focus_lost\tms=3\tdetail=a%09b\nreleased=1\n")
    assert rows[0]["hit"] == b'Notepad "x"' and rows[1]["detail"] == b"a\tb"
    assert released


# --- the key table ----------------------------------------------------------


def test_every_v1_name_is_the_version_it_was_frozen_at():
    """Append-only: version-1 names may never move to a later version, be
    renamed, or go. This list is the freeze; a name added later goes in a
    new version and a new test, not into this one."""
    v1 = {n for n, v in K.KEYS.items() if v == 1}
    assert v1 == {
        "backspace", "tab", "enter", "escape", "space", "delete", "insert",
        "home", "end", "pageup", "pagedown", "up", "down", "left", "right",
        *(f"f{n}" for n in range(1, 25)),
        "shift", "lshift", "rshift", "ctrl", "lctrl", "rctrl", "alt",
        "lalt", "ralt", "altgr", "lwin", "rwin", "apps", "capslock",
        "numlock", "scrolllock", "printscreen", "pause",
        *(f"numpad{n}" for n in range(10)), "numpadmult", "numpadadd",
        "numpadsub", "numpaddot", "numpaddiv", "numpadenter"}


def test_every_alias_points_at_a_real_name():
    assert all(c in K.KEYS for c in K.ALIASES.values())


# --- the tool ---------------------------------------------------------------


class _Guest:
    def __init__(self, automation, keys=1):
        self.caps = C.Caps(automation=automation, keys=keys)


def _names(guest) -> set[str]:
    srv = machine_tools.build(None, None, None, guest, ui=object())
    inst = srv["machine"]["instance"]
    listed = asyncio.run(inst._request_handlers["tools/list"].handler(
        None, None))
    tools = listed.root.tools if hasattr(listed, "root") else listed.tools
    return {t.name for t in tools}


def test_a_steps_client_gets_input_in_place_of_click_keys_focus():
    names = _names(_Guest(C.AUTOMATION_STEPS))
    assert {"ui", "input", "shot"} <= names
    assert not {"click", "keys", "focus"} & names


def test_a_screen_client_keeps_the_three_it_can_answer():
    names = _names(_Guest(C.AUTOMATION_SCREEN))
    assert {"click", "keys", "focus"} <= names and "input" not in names


class _FakeUi:
    def __init__(self, headers, body):
        self.headers, self.body, self.sent = headers, body, None

    async def run(self, guest, body, **kw):
        self.sent = (body, kw)
        return self.headers, self.body


def _call(ui, args, guest=None):
    guest = guest or _Guest(C.AUTOMATION_STEPS)
    srv = machine_tools.build(None, None, None, guest, ui=ui)
    inst = srv["machine"]["instance"]

    class _P:
        name, arguments = "input", args
    res = asyncio.run(inst._request_handlers["tools/call"].handler(None, _P))
    content = res["content"] if isinstance(res, dict) else res.content
    text = "\n".join(c["text"] if isinstance(c, dict) else c.text
                     for c in content)
    err = res.get("isError") if isinstance(res, dict) else res.is_error
    return text, err


def test_a_list_that_ran_reports_each_step():
    ui = _FakeUi({"status": "ok", "done": "3"},
                 b"i=0\tstatus=ok\tms=5\thit=Notepad \"Untitled\"\n"
                 b"i=1\tstatus=ok\tms=9\tdetail=2 keystrokes\n"
                 b"i=2\tstatus=ok\tms=1\n")
    text, err = _call(ui, {"steps": [{"focus": "Notepad"}, "hi[Enter]"]})
    assert not err
    assert "All 2 steps ran." in text
    assert "1. bring Notepad to the front: ok" in text


def test_a_failed_step_is_named_as_the_agent_counted_it():
    ui = _FakeUi({"status": "focus_lost", "step": "2", "done": "2",
                  "detail": "the window in front changed to #32770 \"Error\""},
                 b"i=0\tstatus=ok\tms=5\ni=1\tstatus=ok\tms=1\n"
                 b"i=2\tstatus=focus_lost\tms=0\n")
    text, err = _call(ui, {"steps": [{"focus": "Notepad"}, "a[Enter]b"]})
    assert err
    assert "Step 2 of 2 failed" in text
    assert "Another window came to the front" in text
    assert "Error" in text


def test_a_refused_list_never_reaches_the_machine():
    ui = _FakeUi({}, b"")
    text, err = _call(ui, {"steps": [{"down": "Shift"}]})
    assert err and ui.sent is None
    assert "refused before it reached the machine" in text


def test_a_key_written_as_an_object_is_told_the_string_form():
    """`{"key": "Alt+F4"}` -- what Haiku wrote in a live session."""
    with pytest.raises(S.StepError) as e:
        S.compile_steps([{"key": "Alt+F4"}])
    assert '"[Alt+F4]"' in str(e.value)


def test_the_precheck_refuses_what_the_tool_would():
    assert S.precheck({"steps": [{"move": {"at": [81, 21]}}]})
    assert S.precheck({"steps": ["x", {"down": "Shift"}]})
    assert S.precheck({"steps": [{"focus": "Notepad"}, "x"]}) is None
