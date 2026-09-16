"""Pagination tests.

Two properties carry the whole design:

  1. **Page numbers never change.** An anchor captured now must still point at
     the same text after an hour of streaming.
  2. **Page size is genuinely bounded.** Otherwise the 32MB memory argument is
     fiction, and the single most likely way to break it is one enormous fenced
     code block arriving as a single run.
"""

from bridge.pages import (
    HARD_BYTES,
    MAX_BYTES,
    MAX_LINES,
    Paginator,
)


def line(n: int) -> str:
    return f"line {n:04d}: the quick brown fox jumps over the lazy dog\n"


def test_starts_on_page_one():
    p = Paginator()
    assert p.count == 1
    assert p.live.number == 1


def test_small_content_stays_on_one_page():
    p = Paginator()
    p.add("plain", "hello\n")
    assert p.count == 1
    assert not p.live.sealed


def test_adjacent_same_style_runs_merge_within_a_page():
    p = Paginator()
    p.add("plain", "a")
    p.add("plain", "b")
    p.add("code", "c")
    assert [r.style for r in p.live.runs] == ["plain", "code"]
    assert p.live.runs[0].text == "ab"


# --- sealing ---------------------------------------------------------------


def test_soft_cap_alone_does_not_seal():
    """A page over the soft cap keeps taking text until a message boundary --
    that is what stops a turn being split mid-sentence."""
    p = Paginator()
    for i in range(MAX_LINES + 50):
        p.add("plain", line(i))
    assert p.count == 1, "no boundary yet, so no seal"
    assert p.live.over_soft_cap()


def test_message_boundary_seals_when_over_the_soft_cap():
    p = Paginator()
    for i in range(MAX_LINES + 5):
        p.add("plain", line(i))
    sealed = p.message_boundary()
    assert sealed is not None
    assert sealed.number == 1
    assert sealed.sealed
    assert p.live.number == 2
    assert not p.live.sealed


def test_message_boundary_does_nothing_when_under_the_cap():
    p = Paginator()
    p.add("plain", "short\n")
    assert p.message_boundary() is None
    assert p.count == 1


def test_hard_cap_seals_even_mid_run():
    """One enormous fenced code block is a single run. Without a hard cap it is
    one unbounded page and the memory bound is fiction."""
    p = Paginator()
    huge = "x" * (HARD_BYTES * 3)
    sealed = p.add("code", huge)
    assert sealed, "a run larger than the hard cap must force seals"
    for pg in sealed:
        assert pg.nbytes <= HARD_BYTES


def test_every_page_stays_within_the_hard_cap():
    """The property the 32MB argument rests on."""
    p = Paginator()
    for i in range(2000):
        p.add("plain", line(i))
        if i % 37 == 0:
            p.message_boundary()
    for n in range(1, p.count + 1):
        pg = p.page(n)
        assert pg.nbytes <= HARD_BYTES, f"page {n} is {pg.nbytes} bytes"


def test_mid_run_split_prefers_a_line_boundary():
    p = Paginator()
    body = "".join(line(i) for i in range(2000))
    p.add("code", body)
    first = p.page(1)
    assert first.text.endswith("\n"), "a code block should split between lines"


def test_continued_flag_marks_a_mid_run_split():
    p = Paginator()
    p.add("code", "y" * (HARD_BYTES * 2))
    assert p.page(2) is not None
    assert p.page(2).continued is True


def test_boundary_seal_is_not_marked_continued():
    p = Paginator()
    for i in range(MAX_LINES + 5):
        p.add("plain", line(i))
    p.message_boundary()
    assert p.page(2).continued is False


# --- numbering stability ---------------------------------------------------


def test_page_numbers_are_forward_and_never_renumber():
    """The non-obvious rule. Numbering backwards from the end would invalidate
    every anchor the moment new text arrived."""
    p = Paginator()
    for i in range(MAX_LINES + 5):
        p.add("plain", line(i))
    p.message_boundary()

    page1_text = p.page(1).text
    assert p.page(1).number == 1

    for i in range(MAX_LINES * 6):
        p.add("plain", line(i))
        if i % 50 == 0:
            p.message_boundary()

    assert p.page(1).number == 1
    assert p.page(1).text == page1_text, "a sealed page must be frozen forever"


def test_sealed_pages_are_immutable_after_more_writing():
    p = Paginator()
    for i in range(MAX_LINES + 5):
        p.add("plain", line(i))
    p.message_boundary()
    before = [p.page(n).text for n in range(1, p.count)]
    for i in range(500):
        p.add("plain", line(i))
    after = [p.page(n).text for n in range(1, len(before) + 1)]
    assert before == after


def test_page_lookup_out_of_range():
    p = Paginator()
    assert p.page(0) is None
    assert p.page(99) is None
    assert p.page(1) is not None


# --- anchors ---------------------------------------------------------------


def test_anchor_records_page_and_offset():
    p = Paginator()
    p.add("plain", "some earlier text\n")
    a = p.mark_user_turn("what does the frame cap do?")
    assert a.page == 1
    assert a.offset == len("some earlier text\n")
    assert a.preview.startswith("what does")


def test_anchor_preview_is_truncated_and_single_line():
    p = Paginator()
    a = p.mark_user_turn("a" * 100 + "\nsecond line")
    assert len(a.preview) <= 40
    assert "\n" not in a.preview


def test_anchors_survive_later_pagination():
    """The whole point of forward numbering."""
    p = Paginator()
    a1 = p.mark_user_turn("first question")
    for i in range(MAX_LINES * 4):
        p.add("plain", line(i))
        if i % 60 == 0:
            p.message_boundary()
    a2 = p.mark_user_turn("second question")

    assert a1.page == 1
    assert a2.page == p.live.number
    assert p.page(a1.page).number == 1
    assert [a.preview for a in p.anchors] == ["first question", "second question"]


def test_realistic_session_stays_bounded():
    """Long conversation: many pages, all bounded, numbering stable."""
    p = Paginator()
    for turn in range(40):
        p.mark_user_turn(f"question {turn}")
        for i in range(60):
            p.add("plain", line(i))
        p.add("code", "int main(void) { return 0; }\n")
        p.message_boundary()

    assert p.count > 5, "40 turns should span several pages"
    for n in range(1, p.count + 1):
        assert p.page(n).nbytes <= HARD_BYTES
    assert [a.page for a in p.anchors] == sorted(a.page for a in p.anchors)
