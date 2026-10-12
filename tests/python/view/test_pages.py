"""Pagination tests.

Two properties carry the whole design:

  1. **Page numbers never change.** An anchor captured now must still point at
     the same text after an hour of streaming.
  2. **Page size is genuinely bounded.** Otherwise the 32MB memory argument is
     fiction, and the single most likely way to break it is one enormous fenced
     code block arriving as a single run.

A page holds RECORDS from stage 3 C2, not markup, and the third property
that used to be here went with the change: **a record is never split.**
The mid-run split and everything that tested it are gone -- see the last
section for what replaced them and why deleting the arm alone would have
hung the caller.
"""

from bridge.view.blocks import Block, encode
from bridge.view.pages import (
                               HARD_BYTES,
                               MAX_BYTES,
                               MAX_LINES,
                               Paginator,
                               )


def line(n: int) -> str:
    return f"line {n:04d}: the quick brown fox jumps over the lazy dog\n"


def rec(text: str) -> bytes:
    """One paragraph record of roughly `len(text)` bytes.

    Split on newlines, because a payload line cannot contain the separator
    that ends it -- which is the wire-format rule the equality proof found
    by breaking error("x\ny")."""
    return encode(Block("para", {}, [("t", ln) for ln in text.split("\n")]))


def big(nbytes: int) -> bytes:
    """A record of about `nbytes`, as one fenced block would be."""
    return encode(Block("code", {}, [("t", "x" * max(1, nbytes - 16))]))


def test_starts_on_page_one():
    p = Paginator()
    assert p.count == 1
    assert p.live.number == 1


def test_small_content_stays_on_one_page():
    p = Paginator()
    p.add(rec("hello\n"))
    assert p.count == 1
    assert not p.live.sealed


def test_records_accumulate_in_one_run_and_stay_separable():
    """A run holds a LIST of records, not a concatenated buffer.

    This used to test that adjacent same-STYLE runs merged, with "plain"
    and "code" as the two styles. There was only ever one style --
    presentation.py passed the constant "html" -- and merging was how a
    page avoided a run per block. Records need neither: there is one run,
    and the records in it stay individually addressable, which is what
    lets send_page hand the guest whole records without re-parsing a flat
    buffer to find where each begins.
    """
    p = Paginator()
    p.add(rec("a"))
    p.add(rec("b"))
    p.add(rec("c"))
    assert len(p.live.runs) == 1
    assert len(p.live.runs[0].records) == 3
    assert p.live.records == [rec("a"), rec("b"), rec("c")]
    assert p.live.body == rec("a") + rec("b") + rec("c")


# --- sealing ---------------------------------------------------------------


def test_soft_cap_alone_does_not_seal():
    """A page over the soft cap keeps taking text until a message boundary --
    that is what stops a turn being split mid-sentence.

    Added as one run rather than one call per line, so this exercises the LINE
    cap without also tripping the block cap, which is a separate override
    tested below."""
    p = Paginator()
    p.add(rec("".join(line(i) for i in range(MAX_LINES + 50))))
    assert p.count == 1, "no boundary yet, so no seal"
    assert p.live.over_soft_cap()


def test_message_boundary_seals_when_over_the_soft_cap():
    p = Paginator()
    p.add(rec("".join(line(i) for i in range(MAX_LINES + 5))))
    sealed = p.message_boundary()
    assert sealed is not None
    assert sealed.number == 1
    assert sealed.sealed
    assert p.live.number == 2
    assert not p.live.sealed


def test_message_boundary_does_nothing_when_under_the_cap():
    p = Paginator()
    p.add(rec("short\n"))
    assert p.message_boundary() is None
    assert p.count == 1


def test_an_oversize_record_gets_a_page_of_its_own():
    """THE ARM THAT REPLACED THE MID-RUN SPLIT.

    One enormous fenced code block used to be split across pages by byte
    count. That cannot work on records -- a byte split lands between
    payload lines and leaves two fragments the guest must refuse -- so the
    rule is that a record too large for a page gets a page to itself,
    whole, and the bound on how large that can be is the client's declared
    maxblock rather than anything here.

    Deleting the split WITHOUT naming this would have hung the caller: a
    record larger than hard_bytes on a fresh page failed every remaining
    condition and nothing advanced.
    """
    p = Paginator()
    huge = big(HARD_BYTES * 3)
    p.add(rec("something first"))
    sealed = p.add(huge)
    assert sealed, "the page holding other blocks should have sealed"
    assert p.live.records == [huge]
    assert p.live.nbytes > HARD_BYTES
    # And the next record does not join it.
    p.add(rec("after"))
    assert p.live.records == [rec("after")]


def test_every_page_stays_within_the_hard_cap():
    """The property the 32MB argument rests on."""
    p = Paginator()
    for i in range(2000):
        p.add(rec(line(i)))
        if i % 37 == 0:
            p.message_boundary()
    for n in range(1, p.count + 1):
        pg = p.page(n)
        assert pg.nbytes <= HARD_BYTES, f"page {n} is {pg.nbytes} bytes"


def test_every_page_holds_only_whole_records():
    """What replaced the line-boundary preference, and it is a stronger
    property than the thing it replaces: the old split tried to land on a
    newline so a code block broke between lines, and got it right most of
    the time. Nothing is split now, so every page parses."""
    from bridge.view.blocks import decode

    p = Paginator()
    for i in range(500):
        p.add(rec(line(i)))
    for n in range(1, p.count + 1):
        body = p.page(n).body
        if body:
            assert decode(body), f"page {n} does not parse as records"


def test_no_page_is_ever_marked_continued():
    """`continued` means "split mid-run from the previous page" and nothing
    is split any more, so it is always false. Setting it for a page sealed
    by the block cap would be a lie the guest draws a marker for. The field
    stays because PAGESTART carries it; it dies with pagination."""
    p = Paginator()
    for i in range(400):
        p.add(rec(line(i)))
    p.add(big(HARD_BYTES * 2))
    for i in range(400):
        p.add(rec(line(i)))
    assert p.count > 3
    assert not any(p.page(n).continued for n in range(1, p.count + 1))


def test_boundary_seal_is_not_marked_continued():
    p = Paginator()
    p.add(rec("".join(line(i) for i in range(MAX_LINES + 5))))
    p.message_boundary()
    assert p.page(2).continued is False


# --- numbering stability ---------------------------------------------------


def test_page_numbers_are_forward_and_never_renumber():
    """The non-obvious rule. Numbering backwards from the end would invalidate
    every anchor the moment new text arrived."""
    p = Paginator()
    for i in range(MAX_LINES + 5):
        p.add(rec(line(i)))
    p.message_boundary()

    page1_text = p.page(1).body
    assert p.page(1).number == 1

    for i in range(MAX_LINES * 6):
        p.add(rec(line(i)))
        if i % 50 == 0:
            p.message_boundary()

    assert p.page(1).number == 1
    assert p.page(1).body == page1_text, "a sealed page must be frozen forever"


def test_sealed_pages_are_immutable_after_more_writing():
    p = Paginator()
    for i in range(MAX_LINES + 5):
        p.add(rec(line(i)))
    p.message_boundary()
    before = [p.page(n).body for n in range(1, p.count)]
    for i in range(500):
        p.add(rec(line(i)))
    after = [p.page(n).body for n in range(1, len(before) + 1)]
    assert before == after


def test_page_lookup_out_of_range():
    p = Paginator()
    assert p.page(0) is None
    assert p.page(99) is None
    assert p.page(1) is not None


# --- anchors ---------------------------------------------------------------


def test_anchor_records_page_and_offset():
    p = Paginator()
    first = rec("some earlier text")
    p.add(first)
    a = p.mark_user_turn("what does the frame cap do?")
    assert a.page == 1
    # RECORD BYTES, not characters of markup. The anchor is an offset into
    # the page, and what a page holds changed; the property it is for --
    # "this user turn starts here" -- did not.
    assert a.offset == len(first)
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
        p.add(rec(line(i)))
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
            p.add(rec(line(i)))
        p.add(rec("int main(void) { return 0; }\n"))
        p.message_boundary()

    assert p.count > 5, "40 turns should span several pages"
    for n in range(1, p.count + 1):
        assert p.page(n).nbytes <= HARD_BYTES
    assert [a.page for a in p.anchors] == sorted(a.page for a in p.anchors)


# --- block count ------------------------------------------------------------
#
# The cap that matters now the transcript is HTML. MSHTML's cost scales with
# element count, not text length: 200 one-word paragraphs cost far more than
# one paragraph of 200 words, and a byte cap cannot tell those apart. Measured
# on the 32MB guest, a 200-element page took it down to 48 KB free.


def test_the_block_cap_seals_between_blocks_not_inside_one():
    """A block boundary is already a clean stopping point, so unlike the byte
    cap this override never splits a run."""
    from bridge.view.blocks import decode
    from bridge.view.pages import HARD_BLOCKS
    p = Paginator()
    for i in range(HARD_BLOCKS + 10):
        p.add(rec(f"block {i}"))
    assert p.count > 1
    for n in range(1, p.count + 1):
        body = p.page(n).body
        if body:
            # Whole records, every page. The old version of this asserted
            # that no page started with a fragment of a <p>, which is the
            # same property stated in terms of the markup that is no
            # longer here -- and a weaker one, because a parse catches
            # damage anywhere in the page rather than only at its start.
            assert decode(body)


def test_many_tiny_blocks_still_seal():
    from bridge.view.pages import MAX_BLOCKS
    p = Paginator()
    for i in range(MAX_BLOCKS + 5):
        p.add(rec(f"<p>{i}</p>"))
    assert p.live.over_soft_cap(), "a byte cap alone would not have noticed"
    assert p.message_boundary() is not None


def test_block_count_is_bounded_on_every_page():
    from bridge.view.pages import HARD_BLOCKS
    p = Paginator()
    for i in range(700):
        p.add(rec(f"<p>{i}</p>"))
        if i % 31 == 0:
            p.message_boundary()
    for n in range(1, p.count + 1):
        assert p.page(n).blocks <= HARD_BLOCKS, f"page {n}"


# --- page size scales with the guest's memory -------------------------------


def test_page_limits_scale_with_memory():
    """A 128MB machine can hold a bigger page than the 32MB floor the design
    is built around; making it page every 90 blocks would be a restriction
    inherited from a machine it is not running on."""
    from bridge.view.pages import MAX_BLOCKS, limits_for
    assert limits_for(32 * 1024) == (MAX_BYTES, MAX_BLOCKS)
    assert limits_for(64 * 1024) == (MAX_BYTES * 2, MAX_BLOCKS * 2)
    assert limits_for(128 * 1024) == (MAX_BYTES * 4, MAX_BLOCKS * 4)


def test_a_smaller_machine_never_gets_smaller_pages_than_the_floor():
    from bridge.view.pages import MAX_BLOCKS, limits_for
    assert limits_for(16 * 1024) == (MAX_BYTES, MAX_BLOCKS)
    assert limits_for(0) == (MAX_BYTES, MAX_BLOCKS)


def test_page_limits_are_clamped_so_a_big_machine_does_not_grow_forever():
    from bridge.view.pages import limits_for
    assert limits_for(256 * 1024) == limits_for(1024 * 1024)


def test_a_paginator_honours_its_own_limits():
    p = Paginator(max_bytes=1024, max_blocks=4)
    for i in range(5):
        p.add(rec("<p>x</p>"))
    assert p.live.over_soft_cap(p.max_bytes, p.max_blocks)


# --- 02.17: a seal must not run through the middle of an element ------------


def test_a_block_that_does_not_fit_moves_to_the_next_page_whole():
    """The split was by byte count with a newline preference, which knew
    nothing about tags: page N ended `<div class="codewrap"><pre>...`
    unclosed and page N+1 opened `...</pre></div>`. MSHTML closes the tags
    itself, so the reader got a code block with no frame and a page that
    started with stray markup.

    Records make the same damage worse and easier to describe: a byte split
    lands between payload lines, and the guest refuses both halves rather
    than silently repairing one. There is a clean boundary whenever the
    page already holds something -- the start of this record -- so take it.
    """
    p = Paginator(max_bytes=1000, max_blocks=100)
    p.add(rec("a" * 500))
    block = big(1500)
    sealed, chunks = p.write(block)

    assert sealed, "the page should have sealed"
    assert len(chunks) == 1, chunks
    assert chunks[0][1] == block
    # And it landed whole on the page AFTER the seal.
    assert chunks[0][0] == sealed[-1].number + 1


def test_one_record_bigger_than_a_whole_page_is_NOT_split():
    """The case that used to have no alternative, and now has one.

    The mid-run split was kept for exactly this -- a block bigger than a
    page -- on the argument that removing it meant an unbounded page. That
    argument does not survive records: half a record is not a smaller
    record, it is two fragments the guest must throw away, so splitting
    here loses the block AND the page bound, rather than trading one for
    the other.

    So the page is allowed to exceed its cap for one record, and what
    bounds that is the client's declared maxblock -- a number the machine
    that has to hold it chose. PresentationService.write_block refuses a
    record over it before the paginator ever sees one.
    """
    p = Paginator(max_bytes=1000, max_blocks=100)
    huge = big(6000)
    sealed, chunks = p.write(huge)
    assert not sealed, "a fresh page must not seal to make room"
    assert len(chunks) == 1
    assert chunks[0][1] == huge
    assert p.page(1).body == huge
    assert p.count == 1, "and the record did not spill onto a second page"


def test_an_empty_page_takes_an_oversized_record_without_sealing_first():
    """Sealing an empty page would make a blank one, and the record still
    has to go somewhere. This is also what stops the caller looping: with
    the split gone, a record too large for a fresh page had nothing left to
    satisfy, and `while text:` never advanced."""
    p = Paginator(max_bytes=1000, max_blocks=100)
    sealed, chunks = p.write(big(1500))
    # The name of this test is the claim, and it was never checked before
    # stage 3: the return was unpacked and the seal half thrown away, so a
    # change that started sealing here would have gone unnoticed.
    assert not sealed, "an empty page must not be sealed to make room"
    assert chunks[0][0] == 1


def test_writing_an_oversize_record_terminates():
    """The infinite loop, asserted rather than reasoned about. The old
    write() looped `while text:` and shrank `text` by whatever it had
    placed; deleting the mid-run arm left a record that placed nothing."""
    p = Paginator(max_bytes=100, max_blocks=2)
    for _ in range(5):
        p.write(big(5000))
    assert p.count >= 5
