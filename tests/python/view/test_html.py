"""Markdown -> HTML for the MSHTML transcript.

Two properties carry this module. Everything the model writes must be escaped,
because a transcript that renders <script> from model output is executing it.
And a block must stream: the paragraph being written has to be visible before
it is finished, or streaming reads as a stall.
"""


from bridge.view import html as H
from bridge.view.blocks import clean, runs
from bridge.view.html import APPEND, LIVE, HtmlStream, render, user_turn
from bridge.view.htmlrender import TOOL_WRAP_COL, esc, spans


def inline(text: str) -> str:
    """Markdown source straight to HTML, as html.inline used to be.

    It was two steps all along and C2 separated them for good: the parse
    marks the inline runs (blocks.runs) and the renderer draws them
    (htmlrender.spans). The composition is still worth testing in one
    place, because every assertion below is about a grammar rule and its
    rendered result, and splitting each into two would say less.
    """
    return spans(runs(clean(text)))


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
    assert "<img" not in render(user_turn('<img src=x onerror=1>'))


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


# --- the document --------------------------------------------------------
#
# FIVE TESTS WENT FROM HERE AT C4, with the thing they tested. The bridge
# built the transcript's document and owned its stylesheet; it builds
# neither now, so there is nothing here to assert about either.
#
# Where each property went, because "deleted" and "moved" look the same in
# a diff:
#
#   the charset the document declares matches the bytes it is encoded with
#     -> tests/python/meta/test_guest_css.py, which compares the guest's
#        derivation from GetACP() against caps.html_charset. Two
#        derivations of one fact in two languages, which is a stronger
#        check than the single-source version could be.
#   the skeleton holds #t and #l
#     -> the same file, against the SKELETON_TAIL the guest writes. Same
#        failure as before if it breaks: find_id returns NULL, every
#        append goes nowhere, and the transcript is blank forever.
#   the wire stylesheet carries no comments, and stripping them changes no
#   rule
#     -> gone with WIRE_CSS. The comments live in win/resources/view.css
#        and reach nobody over a wire, so there is nothing left to strip.


# --- tables and ordered lists -----------------------------------------------
#
# Both are things the styled-run renderer could never show. A table could only
# ever have come out as its own pipe characters.


def test_markdown_table_becomes_a_table():
    out = "".join(appended(run("| a | b |\n|---|---|\n| 1 | 2 |\n\n")))
    assert out == ("<table><tr><th>a</th><th>b</th></tr>"
                   "<tr><td>1</td><td>2</td></tr></table>")


def test_a_table_without_a_separator_has_no_header():
    out = "".join(appended(run("| 1 | 2 |\n| 3 | 4 |\n\n")))
    assert "<th>" not in out
    assert out.count("<tr>") == 2


def test_table_cells_are_escaped_and_take_inline_markup():
    out = "".join(appended(run("| `x` | <b> |\n|---|---|\n| **y** | z |\n\n")))
    assert "<code>x</code>" in out
    assert "&lt;b&gt;" in out
    assert "<b>y</b>" in out


def test_numbered_list_is_ordered():
    out = "".join(appended(run("1. one\n2. two\n\n")))
    assert out == "<ol><li>one</li><li>two</li></ol>"


def test_dash_list_is_still_unordered():
    assert "<ul>" in "".join(appended(run("- one\n\n")))


def test_switching_marker_type_starts_a_new_list():
    out = "".join(appended(run("- a\n\n1. b\n\n")))
    assert "<ul><li>a</li></ul>" in out and "<ol><li>b</li></ol>" in out


def test_underscores_inside_identifiers_are_not_emphasis():
    """BPB_RsvdSecCnt and BPB_NumFATs became one italic run, which mangles
    exactly the identifiers this project talks about all day."""
    out = inline("Pull BPB_RsvdSecCnt and BPB_NumFATs now")
    assert "<i>" not in out
    assert "BPB_RsvdSecCnt" in out and "BPB_NumFATs" in out


def test_underscore_emphasis_still_works_at_word_boundaries():
    assert inline("_really_ now") == "<i>really</i> now"


def test_asterisk_emphasis_is_unaffected():
    assert inline("a *b* c") == "a <i>b</i> c"


def test_snake_case_in_a_code_span_is_untouched():
    assert inline("`a_b_c`") == "<code>a_b_c</code>"


def test_a_long_tool_result_line_is_broken_by_the_bridge():
    """IE5 has neither pre-wrap nor word-wrap, so a `pre` holding one long
    line cannot wrap -- and `overflow: auto` then draws a horizontal
    scrollbar inside a box one line tall, on top of the only text there is.
    Observed on the guest as a block that was nothing but a scrollbar.

    The guest transforms nothing, so the break happens here.
    """
    from bridge.view import html as H

    long_line = ("/home/tibi/gaston-work/WRITEMEA/hello.c is outside this "
                 "conversation's folder, and only that folder reaches the "
                 "user's machine. Write to /home/tibi/gaston-work/X instead.")
    out = render(H.tool_result(long_line))
    body = out[out.index("<pre>") + 5:out.index("</pre>")]
    lines = body.split("\n")
    assert len(lines) > 1, "it must actually break"
    assert all(len(line) <= TOOL_WRAP_COL for line in lines), lines
    # Broken at spaces, so it still reads as prose.
    assert not any(line.startswith(" ") for line in lines), lines
    assert ("".join(line + " " for line in lines).split()
            == long_line.split())


def test_an_unbreakable_run_is_broken_anyway():
    """A 300-character path with no spaces is the case that started this. A
    wrapper that only breaks at spaces would leave it exactly as it was."""
    from bridge.view import html as H

    out = render(H.tool_result("x" * 200))
    body = out[out.index("<pre>") + 5:out.index("</pre>")]
    lines = body.split("\n")
    assert max(len(line) for line in lines) <= TOOL_WRAP_COL
    assert "".join(lines) == "x" * 200, "and no character is lost doing it"


def test_short_output_is_left_exactly_alone():
    from bridge.view import html as H

    out = render(H.tool_result("Volume in drive C is WIN98"))
    assert "<pre>Volume in drive C is WIN98</pre>" in out



# --- 02.17: a long open block must not be re-sent whole on every flush -------


def test_a_live_code_fence_is_capped_not_re_sent_whole():
    """The quadratic send. `_render(partial=True)` returned the ENTIRE open
    block on every coalescer flush -- every 100ms on a fast link, 600ms on a
    slow one -- so a 60 KB fenced file went out as roughly 65 LIVE frames
    averaging 30 KB: about 2 MB on the wire for 60 KB of content, on a link
    this project measures in single-digit KB/s. And it ended in silence: past
    `MAX_FRAME - 1024`, `send_html` drops a LIVE frame without a word."""
    s = H.HtmlStream()
    body = "".join("line %04d in a long fenced file\n" % i for i in range(400))
    ups = s.feed("```python\n" + body)
    live = [u for u in ups if u.mode == H.LIVE]
    assert live
    # A little over the cap: the wrapper, the language label and one line's
    # worth of overshoot. Emphatically not the 12 KB the block has reached.
    assert len(live[-1].html) < s.LIVE_CAP + 600


def test_a_capped_live_block_shows_the_tail_and_says_it_is_one():
    """The newest lines are the ones being written, which is what a reader
    watching a block stream is watching FOR. The elision is marked so nobody
    reads a fragment as the whole thing."""
    s = H.HtmlStream()
    body = "".join("line %04d in a long fenced file\n" % i for i in range(400))
    live = [u for u in s.feed("```python\n" + body) if u.mode == H.LIVE][-1]
    assert "line 0399" in live.html
    assert "line 0000" not in live.html
    assert "<pre>..." in live.html


def test_the_sealed_block_still_carries_every_byte():
    """Nothing is lost -- only the intermediate copies are bounded. The
    finished block arrives whole, as an APPEND, the way it always did."""
    s = H.HtmlStream()
    body = "".join("line %04d in a long fenced file\n" % i for i in range(400))
    s.feed("```python\n" + body)
    whole = "".join(u.html for u in s.flush() if u.mode != H.LIVE)
    assert "line 0000" in whole and "line 0399" in whole
    assert "..." not in whole.split("<pre>")[1][:40]


def test_a_short_live_block_is_untouched():
    """The cap must be invisible for ordinary prose, which is all of it."""
    s = H.HtmlStream()
    live = [u for u in s.feed("a short paragraph being typed")
            if u.mode == H.LIVE][-1]
    assert live.html == "<p>a short paragraph being typed</p>"


def test_a_live_table_is_never_capped():
    """Dropping leading rows drops the header and the separator under it, so
    `_table` would re-read the table as headerless and the live view would
    disagree with the sealed one about what the columns are."""
    s = H.HtmlStream()
    rows = "".join("| cell %04d | %s |\n" % (i, "x" * 60) for i in range(200))
    s.feed("| a | b |\n|---|---|\n" + rows)
    live = [u for u in s.feed("| last | row |\n") if u.mode == H.LIVE]
    if live and live[-1].html:
        assert "<th>" in live[-1].html
