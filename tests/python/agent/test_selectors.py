"""Selectors on the bridge: step splitting and the ambiguity suggestions."""
from __future__ import annotations

from bridge.agent import selectors as S
from bridge.agent.tools import _ui_explain
from bridge.services.ui import UiError


def _cand(*fields: str) -> str:
    return "".join(f"{len(f)}:{f}" for f in fields)


def test_steps_end_at_gt_and_at_space():
    assert S.split_steps("A > B C") == ["A", "B", "C"]


def test_nothing_splits_inside_brackets_or_quotes():
    assert S.split_steps('#32770[text~="save as*"] Button[text="a > b"]') == \
        ['#32770[text~="save as*"]', 'Button[text="a > b"]']


def test_a_candidate_round_trips():
    v = _cand("Button", "&OK", "", "", "#32770", "Save As")
    c = S.decode_candidate(v)
    assert c == S.Candidate("Button", "&OK", "", "", "#32770", "Save As")


def test_a_caption_holding_the_separator_survives():
    """Why netstrings: a caption may hold any byte, colons and digits too."""
    v = _cand("Static", "12:30 PM", "", "", "", "")
    assert S.decode_candidate(v).text == "12:30 PM"


def test_a_malformed_candidate_is_dropped_not_raised():
    assert S.decode_candidate("9:short") is None
    assert S.decode_candidate("x:y") is None


def test_the_suggestion_names_the_window_with_a_prefix_glob():
    c = S.Candidate("Button", "&OK", "", "", "#32770", "Save As")
    assert S.suggest(c, 'Button[text~="ok"]') == \
        '[class="#32770"][text~="Save As*"] Button[text~="ok"]'


def test_the_ambiguous_message_lists_who_matched():
    exc = UiError("ambiguous", "2 controls match", {
        "detail": "2 controls match",
        "cand1": _cand("Button", "&OK", "", "", "#32770", "Save As"),
        "cand2": _cand("Button", "OK", "", "", "#32770", "Print"),
    })
    out = _ui_explain(exc, 'Button[text~="ok"]')
    assert "Multiple results found" in out
    assert 'Save As*' in out and 'Print*' in out
    assert "ancestor" in out


def test_an_older_client_still_gets_the_advice():
    exc = UiError("ambiguous", "2 controls match", {})
    assert "ancestor" in _ui_explain(exc, "Button")


def test_not_found_names_a_space_separated_part():
    exc = UiError("not_found", "", {"step": "1"})
    out = _ui_explain(exc, 'Notepad Button[text~="close"]')
    assert 'Button[text~="close"]' in out
