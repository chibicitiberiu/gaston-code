"""Session listing and stored-history replay.

`_text_of` walks persisted message data whose exact shape is not guaranteed
across releases, so the tests lean on malformed input. A KeyError in there
would break the whole chat list rather than one message.
"""

from bridge.sessions import STYLE_CODE, STYLE_PLAIN, STYLE_TOOL, STYLE_USER
from bridge.sessions import _text_of


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
    runs = _text_of({
        "role": "assistant",
        "content": [{"type": "tool_use", "name": "Bash",
                     "input": {"command": "ls -la /very/long/path"}}],
    })
    assert styles(runs) == [STYLE_TOOL]
    assert "Bash" in texts(runs)
    assert "/very/long/path" not in texts(runs), "tool input is not replayed"


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
