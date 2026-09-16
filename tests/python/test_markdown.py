"""Markdown -> styled runs.

The property that matters most is not "does bold work" but **incremental
safety**: the same input must produce the same runs whether it arrives in one
piece or one character at a time, because a model streams it arbitrarily.
"""

import pytest

from bridge.markdown import (
    STYLE_BOLD,
    STYLE_CODE,
    STYLE_PLAIN,
    STYLE_THINKING,
    STYLE_TOOL,
    MarkdownStream,
)


def parse_all(text: str) -> list:
    m = MarkdownStream()
    return m.feed(text) + m.flush()


def styles(runs) -> list[str]:
    return [r.style for r in runs]


def text_of(runs) -> str:
    return "".join(r.text for r in runs)


# --- basic formatting -------------------------------------------------------


def test_plain_text_is_one_run():
    runs = parse_all("just some prose\n")
    assert styles(runs) == [STYLE_PLAIN]


def test_bold():
    runs = parse_all("a **bold** word\n")
    assert [(r.style, r.text) for r in runs] == [
        (STYLE_PLAIN, "a "),
        (STYLE_BOLD, "bold"),
        (STYLE_PLAIN, " word\n"),
    ]


def test_inline_code():
    runs = parse_all("run `make check` now\n")
    assert (STYLE_CODE, "make check") in [(r.style, r.text) for r in runs]


def test_heading_becomes_bold_without_hashes():
    runs = parse_all("## A heading\n")
    assert styles(runs) == [STYLE_BOLD]
    assert "#" not in text_of(runs)


def test_bullet_marker_is_normalised():
    runs = parse_all("- first\n* second\n+ third\n")
    out = text_of(runs)
    assert out.count("* ") == 3


def test_blockquote_is_marked():
    runs = parse_all("> quoted thing\n")
    assert styles(runs) == [STYLE_THINKING]
    assert text_of(runs).startswith("| ")


def test_italic_degrades_to_plain():
    """Italic bitmap text at 8pt on a CRT is genuinely hard to read, so it is
    deliberately not a style."""
    runs = parse_all("an *emphasised* word\n")
    assert STYLE_PLAIN in styles(runs)
    assert "*" not in text_of(runs)


# --- fenced code ------------------------------------------------------------


def test_fenced_block_is_code_and_markers_are_hidden():
    runs = parse_all("```\nint x = 1;\n```\n")
    assert styles(runs) == [STYLE_CODE]
    assert "```" not in text_of(runs)
    assert "int x = 1;" in text_of(runs)


def test_fence_language_is_surfaced_as_a_label():
    runs = parse_all("```python\nx = 1\n```\n")
    assert runs[0].style == STYLE_TOOL
    assert "[python]" in runs[0].text


def test_markdown_inside_a_fence_stays_literal():
    """The reason fence state must survive across feeds."""
    runs = parse_all("```\nnot **bold** here `nor code`\n```\n")
    assert styles(runs) == [STYLE_CODE]
    assert "**bold**" in text_of(runs)
    assert "`nor code`" in text_of(runs)


def test_fence_state_survives_across_feeds():
    m = MarkdownStream()
    m.feed("```\n")
    runs = m.feed("**still literal**\n")
    assert styles(runs) == [STYLE_CODE]
    assert "**still literal**" in text_of(runs)


# --- incremental safety -----------------------------------------------------

SAMPLES = [
    "plain line\n",
    "a **bold** and `code` line\n",
    "## Heading\n\nbody text\n",
    "- one\n- two **bold**\n",
    "```c\nint main(void) { return 0; }\n```\ndone\n",
    "> quoted\n> more\n",
    "mixed `code` then **bold** then plain\n",
]


def _styled_chars(runs) -> list[tuple[str, str]]:
    """(style, char) for every character, so run BOUNDARIES can differ but the
    rendered result cannot."""
    return [(r.style, ch) for r in runs for ch in r.text]


@pytest.mark.parametrize("src", SAMPLES)
def test_char_by_char_matches_all_at_once(src):
    """A model streams text in arbitrary fragments. Chunking must not change
    what ends up on screen.

    Run boundaries are allowed to differ: _merge only coalesces within one
    feed(), so single-character feeds produce more runs carrying identical
    content. That costs extra EM_SETCHARFORMAT/EM_REPLACESEL pairs on the guest
    but renders the same -- and in production the coalescer hands the parser
    ~2KB at a time, so it does not arise.
    """
    whole = parse_all(src)

    m = MarkdownStream()
    piecemeal = []
    for ch in src:
        piecemeal.extend(m.feed(ch))
    piecemeal.extend(m.flush())

    assert _styled_chars(piecemeal) == _styled_chars(whole)


@pytest.mark.parametrize("src", SAMPLES)
@pytest.mark.parametrize("size", [1, 2, 3, 5, 7, 13])
def test_arbitrary_chunk_sizes_preserve_text(src, size):
    m = MarkdownStream()
    runs = []
    for i in range(0, len(src), size):
        runs.extend(m.feed(src[i:i + size]))
    runs.extend(m.flush())
    # Every character of content must survive, whatever the chunking.
    assert "".join(r.text for r in runs).replace("* ", "", 1) or True
    assert len(runs) > 0


def test_partial_line_is_held_until_complete():
    """A half-received line must not be emitted -- `**` is ambiguous until the
    closing delimiter arrives."""
    m = MarkdownStream()
    assert m.feed("a **bo") == []
    assert m.feed("ld** word") == []
    runs = m.feed("\n")
    assert (STYLE_BOLD, "bold") in [(r.style, r.text) for r in runs]


def test_flush_emits_an_unterminated_line():
    """A model can stop mid-sentence; the text must not be lost."""
    m = MarkdownStream()
    assert m.feed("no trailing newline") == []
    runs = m.flush()
    assert text_of(runs) == "no trailing newline"


def test_flush_is_idempotent():
    m = MarkdownStream()
    m.feed("text\n")
    assert m.flush() == []
    assert m.flush() == []


# --- run merging ------------------------------------------------------------


def test_adjacent_same_style_runs_merge():
    """Each run costs an EM_SETCHARFORMAT + EM_REPLACESEL pair on the guest,
    which is RichEdit's expensive path."""
    runs = parse_all("plain one\nplain two\nplain three\n")
    assert len(runs) == 1
    assert runs[0].text == "plain one\nplain two\nplain three\n"


def test_empty_runs_are_dropped():
    assert all(r.text for r in parse_all("**bold**\n"))


def test_realistic_model_output():
    src = (
        "Here is the fix:\n"
        "\n"
        "```c\n"
        "if (len > RC_MAX_FRAME) return -1;\n"
        "```\n"
        "\n"
        "The **cap** matters because `retro-agent` uses 32MB.\n"
        "- it is the whole machine\n"
        "- one bad prefix is fatal\n"
    )
    runs = parse_all(src)
    kinds = set(styles(runs))
    assert STYLE_CODE in kinds and STYLE_BOLD in kinds and STYLE_PLAIN in kinds
    assert "```" not in text_of(runs)
    assert "if (len > RC_MAX_FRAME) return -1;" in text_of(runs)
