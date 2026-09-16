"""Markdown -> HTML for the MSHTML transcript.

Two properties carry this module. Everything the model writes must be escaped,
because a transcript that renders <script> from model output is executing it.
And a block must stream: the paragraph being written has to be visible before
it is finished, or streaming reads as a stall.
"""

from bridge.html import APPEND, LIVE, HtmlStream, document, esc, inline, user_turn


def appended(updates):
    return [u.html for u in updates if u.mode == APPEND]


def live(updates):
    return [u.html for u in updates if u.mode == LIVE]


def run(text):
    s = HtmlStream()
    out = s.feed(text)
    out.extend(s.flush())
    return out


# --- escaping ---------------------------------------------------------------


def test_markup_in_model_output_is_escaped():
    out = "".join(appended(run("a <script>alert(1)</script> b\n")))
    assert "<script>" not in out
    assert "&lt;script&gt;" in out


def test_ampersands_are_escaped():
    assert esc("a & b") == "a &amp; b"


def test_escaping_happens_inside_code_spans_too():
    assert "&lt;b&gt;" in inline("`<b>`")


def test_escaping_happens_inside_fenced_blocks():
    out = "".join(appended(run("```\n<i>hi</i>\n```\n")))
    assert "<i>hi</i>" not in out
    assert "&lt;i&gt;hi&lt;/i&gt;" in out


def test_user_text_is_escaped():
    assert "<img" not in user_turn('<img src=x onerror=1>')


# --- blocks -----------------------------------------------------------------


def test_paragraph():
    assert appended(run("hello there\n\n")) == ["<p>hello there</p>"]


def test_bold_and_inline_code():
    out = "".join(appended(run("a **b** and `c`\n\n")))
    assert "<b>b</b>" in out and "<code>c</code>" in out


def test_bullets_become_one_list():
    out = "".join(appended(run("- one\n- two\n\n")))
    assert out == "<ul><li>one</li><li>two</li></ul>"


def test_fence_language_becomes_a_label_not_bracket_text():
    out = "".join(appended(run("```python\nx = 1\n```\n")))
    assert 'class="lang">python<' in out
    assert "[python]" not in out


def test_heading():
    out = "".join(appended(run("## Title\n\n")))
    assert out == "<h4>Title</h4>"


def test_blockquote():
    out = "".join(appended(run("> quoted\n\n")))
    assert "<blockquote>" in out


def test_markdown_inside_a_fence_stays_literal():
    out = "".join(appended(run("```\n- not a bullet **not bold**\n```\n")))
    assert "<li>" not in out and "<b>" not in out


# --- streaming --------------------------------------------------------------


def test_a_partial_paragraph_is_visible_before_it_is_finished():
    """The point of the live block: without it nothing shows until the
    paragraph ends, which reads as a stall rather than as streaming."""
    s = HtmlStream()
    out = s.feed("partial senten")
    assert appended(out) == []
    assert "partial senten" in live(out)[-1]


def test_the_live_block_clears_once_the_block_is_committed():
    s = HtmlStream()
    s.feed("some text")
    out = s.feed("\n\n")
    assert appended(out) == ["<p>some text</p>"]
    assert live(out)[-1] == ""


def test_fence_state_survives_across_feeds():
    s = HtmlStream()
    s.feed("```\n")
    s.feed("- still literal\n")
    out = s.feed("```\n")
    assert "<li>" not in "".join(appended(out))


def test_a_line_is_not_classified_until_it_is_complete():
    """'*' might open a bullet or open **bold**, and you cannot tell yet."""
    s = HtmlStream()
    out = s.feed("*")
    assert appended(out) == []


def test_flush_closes_an_unterminated_block():
    s = HtmlStream()
    s.feed("no trailing newline")
    assert appended(s.flush()) == ["<p>no trailing newline</p>"]


# --- the document -----------------------------------------------------------


def test_document_declares_cp1252():
    """The bridge folds text to CP1252, so the document has to say so."""
    assert "windows-1252" in document()


def test_document_has_both_containers():
    d = document()
    assert 'id="t"' in d and 'id="l"' in d


def test_inline_code_inside_bold_is_parsed_not_escaped():
    """`code` inside **bold** is common in model output. Escaping the inner
    span instead of parsing it puts literal backticks on screen."""
    out = inline("**bold with `code` in it**")
    assert "<b>bold with <code>code</code> in it</b>" == out
    assert "`" not in out


def test_inline_code_inside_italics_too():
    assert "<code>x</code>" in inline("*see `x` here*")
