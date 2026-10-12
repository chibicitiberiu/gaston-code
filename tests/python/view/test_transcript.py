"""The transcript: a list of records, and the window a client asks for.

WHAT THIS FILE REPLACED. `test_pages.py` had 30 tests about cutting a
stream of HTML into MSHTML-sized chunks: soft caps, hard caps, a block cap,
sealing at a message boundary, forward numbering that could never renumber,
and anchors into page-relative byte offsets. Stage 3b deleted the mechanism
-- a record is indivisible since stage 3, so every boundary is safe, and the
client picks its own window because it is the one with the memory
constraint.

Most of those tests went with it. What is kept is what was never really
about pagination: records come back in the order they went in, whole, and a
record too large for anybody's window is still one record.
"""

from __future__ import annotations

import pytest

from bridge.view.blocks import Block, decode, encode
from bridge.view.pages import Transcript


def rec(text: str) -> bytes:
    return encode(Block("para", {}, [("t", text)]))


def test_records_come_back_in_the_order_they_went_in():
    """The property forward page numbering existed to protect, stated
    directly. Numbering backwards from the end -- so the newest block is
    index 0 -- would renumber every earlier one on every append, which is
    why the paginator filled forward and never renumbered a sealed page.
    A list does that by being a list."""
    t = Transcript()
    for i in range(500):
        assert t.add(rec(f"line {i:04d}")) == i
    assert t.count == 500
    assert t.all_records() == [rec(f"line {i:04d}") for i in range(500)]
    # And an append does not disturb what is already there.
    t.add(rec("later"))
    assert t.range(0, 3) == [rec("line 0000"), rec("line 0001"),
                             rec("line 0002")]


def test_every_record_in_a_window_decodes_whole():
    """`test_pages.py` asserted this per page, because the paginator's hard
    cap was allowed to break mid-run and mark the page `continued`. Nothing
    cuts a record now -- but a window is still what the guest's renderer
    consumes, and half a record reaches it as a fragment it must refuse."""
    t = Transcript()
    for i in range(200):
        t.add(rec(f"line {i} with some padding to make it wider"))

    for offset in (0, 90, 180):
        window = t.range(offset, 90)
        for r in window:
            blocks = list(decode(r))
            assert len(blocks) == 1, r
            assert blocks[0].kind == "para"


def test_a_record_too_large_for_any_window_is_still_one_record():
    """The paginator gave an oversize record a page of its own rather than
    splitting it, which is the rule the client's `maxblock` replaced. The
    transcript has no size opinion at all: it holds what it is given, and
    refusing an oversize block is `write_block`'s job, upstream."""
    t = Transcript()
    big = encode(Block("code", {}, [("t", "x" * 200_000)]))
    t.add(rec("before"))
    t.add(big)
    t.add(rec("after"))
    assert t.count == 3
    assert t.range(1, 1) == [big]
    assert t.all_records() == [rec("before"), big, rec("after")]


def test_a_window_is_a_slice_and_a_short_one_is_normal():
    t = Transcript()
    for i in range(10):
        t.add(f"r{i}".encode())
    assert t.range(0, 3) == [b"r0", b"r1", b"r2"]
    assert t.range(8, 5) == [b"r8", b"r9"]      # short at the end
    assert t.range(10, 5) == []                  # exactly at the end
    assert t.range(99, 5) == []                  # past it, not an error
    assert t.range(0, 0) == []
    assert t.range(0, -1) == []


def test_a_negative_offset_is_a_bug_on_the_asking_side():
    """Empty for a window past the end, because a transcript can shrink
    under a guest that is mid-request -- `reset()` does exactly that on a
    conversation switch, and a stale page button is a race the asker should
    not need a recovery path for. An offset below zero races nothing."""
    t = Transcript()
    t.add(b"r0")
    with pytest.raises(ValueError):
        t.range(-1, 5)


def test_all_records_is_a_copy():
    """The export walks it while the agent may still be appending."""
    t = Transcript()
    t.add(b"r0")
    out = t.all_records()
    t.add(b"r1")
    assert out == [b"r0"]
