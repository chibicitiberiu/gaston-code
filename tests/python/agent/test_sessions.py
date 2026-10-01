"""Session listing and stored-history replay.

`_text_of` walks persisted message data whose exact shape is not guaranteed
across releases, so the tests lean on malformed input. A KeyError in there
would break the whole chat list rather than one message.
"""

from bridge.agent.sessions import (
    STYLE_CODE,
    STYLE_PLAIN,
    STYLE_TOOL,
    STYLE_USER,
    _compact_summary,
    _text_of,
)


def styles(runs):
    return [s for s, _ in runs]


def texts(runs):
    return "".join(t for _, t in runs)


def test_plain_string_content():
    runs = _text_of({"role": "assistant", "content": "hello"})
    assert runs == [(STYLE_PLAIN, "hello")]


def test_user_role_gets_the_user_style():
    runs = _text_of({"role": "user", "content": "a question"})
    assert styles(runs) == [STYLE_USER]


def test_text_blocks():
    runs = _text_of({
        "role": "assistant",
        "content": [{"type": "text", "text": "first"},
                    {"type": "text", "text": " second"}],
    })
    assert texts(runs) == "first second"


def test_tool_use_is_summarised_not_dumped():
    """ONE argument, the telling one, clipped -- exactly what the live path
    writes.

    This used to assert the opposite: that no input was replayed at all, so a
    call rendered as `tool -> Bash` when you came back to a conversation and
    `Bash command: ls -la ...` while it was happening. That is the "opening an
    old conversation makes it lose formatting" complaint surviving in the one
    block type nobody checked. Summarised still means summarised -- the whole
    input dict never appears, and the value is clipped at 160 characters, so
    a replayed page is no larger than the live one was.
    """
    runs = _text_of({
        "role": "assistant",
        "content": [{"type": "tool_use", "name": "Bash",
                     "input": {"command": "ls -la /very/long/path",
                               "timeout_ms": 20000, "shell": True}}],
    })
    assert styles(runs) == [STYLE_TOOL]
    assert texts(runs) == "Bash command: ls -la /very/long/path"
    assert "timeout_ms" not in texts(runs), "the whole input is not dumped"


def test_tool_use_with_no_telling_argument_is_just_the_name():
    runs = _text_of({
        "role": "assistant",
        "content": [{"type": "tool_use", "name": "Glob"}],
    })
    assert texts(runs) == "Glob"


def test_a_replayed_tool_call_is_the_same_markup_as_a_live_one():
    """The renderer takes (name, summary); replay used to hand it the literal
    word "tool" and everything else as the summary, so the tool name was not
    in `.toolname` and did not get the styling a live one did."""
    from bridge.agent import sessions as S
    from bridge.view import html as H

    live = H.tool_call("Bash", "command: ls")
    runs = S._text_of({
        "role": "assistant",
        "content": [{"type": "tool_use", "name": "Bash",
                     "input": {"command": "ls"}}],
    })
    name, _, rest = runs[0][1].partition(" ")
    assert H.tool_call(name, rest) == live


def test_tool_result_is_truncated():
    runs = _text_of({
        "role": "user",
        "content": [{"type": "tool_result", "content": "x" * 5000}],
    })
    assert styles(runs) == [STYLE_CODE]
    assert len(texts(runs)) < 600


def test_tool_result_with_block_list_content():
    runs = _text_of({
        "role": "user",
        "content": [{"type": "tool_result",
                     "content": [{"type": "text", "text": "output here"}]}],
    })
    assert "output here" in texts(runs)


def test_thinking_is_not_replayed():
    """Long, and it was never the point of scrolling back."""
    runs = _text_of({
        "role": "assistant",
        "content": [{"type": "thinking", "thinking": "a" * 1000},
                    {"type": "text", "text": "the answer"}],
    })
    assert texts(runs) == "the answer"


# --- malformed input --------------------------------------------------------


def test_none_message():
    assert _text_of(None) == []


def test_non_dict_message():
    assert _text_of("just a string") == []
    assert _text_of([1, 2, 3]) == []


def test_missing_content():
    assert _text_of({"role": "assistant"}) == []


def test_content_of_unexpected_type():
    assert _text_of({"role": "assistant", "content": 42}) == []


def test_blocks_that_are_not_dicts_are_skipped():
    runs = _text_of({
        "role": "assistant",
        "content": ["bare string", None, 7,
                    {"type": "text", "text": "survivor"}],
    })
    assert texts(runs) == "survivor"


def test_unknown_block_type_is_ignored():
    runs = _text_of({
        "role": "assistant",
        "content": [{"type": "something_new_in_a_later_release"},
                    {"type": "text", "text": "kept"}],
    })
    assert texts(runs) == "kept"


def test_block_missing_its_fields():
    runs = _text_of({
        "role": "assistant",
        "content": [{"type": "text"},               # no 'text'
                    {"type": "tool_use"},           # no 'name'
                    {"type": "tool_result"}],       # no 'content'
    })
    # Must not raise; empty text simply contributes nothing.
    assert all(isinstance(t, str) for _, t in runs)


def test_realistic_mixed_message():
    runs = _text_of({
        "role": "assistant",
        "content": [
            {"type": "text", "text": "Let me check that.\n"},
            {"type": "tool_use", "name": "Read", "input": {"file_path": "/x"}},
        ],
    })
    assert STYLE_PLAIN in styles(runs)
    assert STYLE_TOOL in styles(runs)
    assert "Read" in texts(runs)


# --- the compaction summary -------------------------------------------------
#
# Restoring a compacted conversation used to open it with several kilobytes of
# machine-written recap in a user bubble. On a 640x480 screen that is the whole
# first page, and it is not something the user said.


def test_the_compaction_opener_is_recognised():
    text = ("This session is being continued from a previous conversation "
            "that ran out of context. The summary below covers...")
    assert _compact_summary({"role": "user", "content": text}) == text
    assert _compact_summary({
        "role": "user",
        "content": [{"type": "text", "text": text}],
    }) == text


def test_an_assistant_quoting_the_opener_is_still_a_real_reply():
    """This project's own transcripts quote that sentence while discussing
    this very code. Role is what separates the synthetic message from a reply
    about it."""
    text = ("This session is being continued from a previous conversation -- "
            "that is the string we match on.")
    assert _compact_summary({"role": "assistant", "content": text}) is None


def test_an_ordinary_user_message_is_not_a_summary():
    assert _compact_summary({"role": "user", "content": "hello"}) is None
    assert _compact_summary({"role": "user", "content": None}) is None
    assert _compact_summary(None) is None
    # A tool_result first block, not text: not a summary, and must not raise.
    assert _compact_summary({
        "role": "user",
        "content": [{"type": "tool_result", "content": "x"}],
    }) is None


# --- how wide the fetch is --------------------------------------------------
#
# `listing()` returns the newest sessions on the WHOLE MACHINE, and every
# caller narrows that afterwards. A small fetch is therefore a fetch of other
# people's work: measured on the development box, 50 rows held 33 of this
# bridge's 87 conversations and the other 54 were simply not recent enough.


def test_the_default_fetch_is_wide_enough_to_filter_from(monkeypatch):
    from bridge.agent import sessions as S

    asked = {}

    def fake(directory=None, limit=None):
        asked["directory"], asked["limit"] = directory, limit
        return []

    monkeypatch.setattr(S, "list_sessions", fake)
    S.listing()
    assert asked["limit"] == S.FETCH_LIMIT
    assert S.FETCH_LIMIT >= 200, (
        "all 87 of this bridge's conversations were inside the newest 200 "
        "when the bug was measured; a narrower fetch loses the oldest of "
        "them again")


def test_an_explicit_limit_still_wins(monkeypatch):
    """The wide default is a default, not a floor: `history_html`'s callers
    and any paging caller must still be able to ask for less."""
    from bridge.agent import sessions as S

    asked = {}

    def fake(directory=None, limit=None):
        asked["limit"] = limit
        return []

    monkeypatch.setattr(S, "list_sessions", fake)
    S.listing(limit=5)
    assert asked["limit"] == 5


def test_a_failed_listing_is_empty_rather_than_an_exception(monkeypatch):
    """The chat list is drawn from this; a raise here used to be a dead
    left-hand column with no explanation."""
    from bridge.agent import sessions as S

    def boom(directory=None, limit=None):
        raise OSError("no projects directory")

    monkeypatch.setattr(S, "list_sessions", boom)
    assert S.listing() == []
