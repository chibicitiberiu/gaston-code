"""The record codec: what crosses the wire once the markup stops.

The guest's parser is the only reader of a record in production, and it is on
a platform that cannot be debugged. So the codec is pinned here, on the side
that can be, and `decode` exists for no other reason -- nothing in the bridge
calls it.
"""

from __future__ import annotations

import pytest

from bridge.view import blocks as B


def rt(block: B.Block) -> B.Block:
    """One record, encoded and read back."""
    out = B.decode(B.encode(block))
    assert len(out) == 1
    return out[0]


# --- the shape -------------------------------------------------------------


def test_a_record_is_a_head_some_payload_and_a_dot():
    assert B.encode(B.Block("para", {}, [("t", "hello")])) == b"para\nt hello\n.\n"


def test_attributes_ride_on_the_head_in_a_stable_order():
    got = B.encode(B.Block("toolresult", {"error": 1, "clipped": 9},
                           [("t", "x")]))
    assert got.split(b"\n")[0] == b"toolresult clipped=9 error=1"


def test_a_tag_with_no_payload_is_the_tag_alone():
    """Load-bearing: a blank line in a pasted message is an empty `t`.

    user_turn("a\\n\\nb") renders as <div class="you">a<br><br>b</div>, so an
    empty payload line has to be expressible or the blank lines vanish.
    """
    rec = B.encode(B.Block("user", {}, [("t", "a"), ("t", ""), ("t", "b")]))
    assert rec == b"user\nt a\nt\nt b\n.\n"
    assert rt(B.Block("user", {}, [("t", "a"), ("t", ""), ("t", "b")])).payload \
        == [("t", "a"), ("t", ""), ("t", "b")]


def test_a_record_with_no_payload_at_all():
    assert B.encode(B.Block("rule")) == b"rule\n.\n"
    assert rt(B.Block("rule")) == B.Block("rule", {}, [])


def test_there_is_no_dot_stuffing_because_there_cannot_be_an_ambiguity():
    """A payload line always begins with its tag letter; only the terminator
    begins with a dot. An earlier draft specified dot-stuffing and it would
    have been dead code."""
    rec = B.encode(B.Block("para", {}, [("t", ". not a terminator"),
                                        ("t", "...")]))
    assert b"\nt . not a terminator\n" in rec
    assert rt(B.Block("para", {}, [("t", ". not a terminator")])).texts() \
        == [". not a terminator"]


# --- every kind round-trips ------------------------------------------------


EVERY_KIND = [
    B.Block("para", {}, [("t", "one"), ("t", "two")]),
    B.Block("code", {}, [("l", "c"), ("t", "int x;")]),
    B.Block("code", {"clipped": 40}, [("t", "cut")]),
    B.Block("heading", {"level": 3}, [("t", "H")]),
    B.Block("heading", {"level": 6}, [("t", "deep")]),
    B.Block("bullet", {}, [("t", "a"), ("t", "b")]),
    B.Block("numbered", {}, [("t", "a")]),
    B.Block("quote", {}, [("t", "q")]),
    B.Block("rule"),
    B.Block("table", {"header": 1},
            [("r", ""), ("c", "a"), ("c", "b"), ("r", ""), ("c", "1")]),
    B.Block("table", {"header": 0}, [("r", ""), ("c", "x")]),
    B.Block("user", {}, [("t", "typed")]),
    B.Block("notice", {}, [("t", "n")]),
    B.Block("error", {}, [("t", "e")]),
    B.Block("tool", {}, [("n", "exec"), ("t", "dir")]),
    B.Block("toolresult", {"error": 0}, [("t", "out")]),
    B.Block("toolresult", {"error": 1, "clipped": 7}, [("t", "bad")]),
    B.Block("thinking", {}, [("t", "hm")]),
    B.Block("permission", {"settleable": 1},
            [("h", "ask"), ("t", "may i"), ("w", "careful"),
             ("p", "preview"), ("s", "step"), ("k", "path"), ("v", "C:\\X")]),
    B.Block("clip", {"dropped": 1234}),
    B.Block("exportheading", {}, [("h", "Title"), ("t", "when")]),
]


@pytest.mark.parametrize("block", EVERY_KIND, ids=lambda b: b.kind)
def test_every_kind_round_trips(block):
    assert rt(block) == block


def test_the_parametrised_list_covers_the_whole_vocabulary():
    """Otherwise a kind added to KINDS is untested and nobody notices."""
    assert {b.kind for b in EVERY_KIND} == set(B.KINDS)


# --- the inline marks ------------------------------------------------------


def test_a_run_opens_with_a_letter_and_closes_with_the_close_byte():
    assert B.runs("**a**") == "\x01sa\x02"
    assert B.runs("`a`") == "\x01ca\x02"
    assert B.runs("*a*") == "\x01ea\x02"


def test_nesting_reaches_depth_three():
    """The case that killed the flat 3-bit encoding. Verified against the old
    renderer: "__a *b `c` d* e__" is <b>a <i>b <code>c</code> d</i> e</b>."""
    marked = B.runs("__a *b `c` d* e__")
    assert marked == "\x01sa \x01eb \x01cc\x02 d\x02 e\x02"
    assert B.span_depth(marked) == 3


def test_a_kind_nests_inside_itself_so_the_stack_cannot_be_one_slot_per_kind():
    marked = B.runs("_a *b* c_")
    assert marked == "\x01ea \x01eb\x02 c\x02"
    assert B.span_depth(marked) == 2


def test_adjacent_runs_of_the_same_kind_stay_two_runs():
    """"**a****b**" is <b>a</b><b>b</b>, which a flat per-character state
    cannot tell from one run of four characters."""
    assert B.runs("**a****b**") == "\x01sa\x02\x01sb\x02"


def test_depth_never_exceeds_the_stated_bound():
    """Structural, not luck: **X**/*X* exclude * from X and __X__/_X_ exclude
    _, so a chain uses at most one *-family level, one _-family level and
    code. The guest sizes its stack against this number."""
    for text in ["__a *b `c` d* e__", "**_`x`_**", "*__`y`__*",
                 "`a` **b** _c_", "**a *b* `c` _d_ e**"]:
        assert B.span_depth(B.runs(text)) <= B.MAX_SPAN_DEPTH, text


def test_marks_survive_the_codec():
    block = B.Block("para", {}, [("t", B.runs("**a** and `b`"))])
    assert rt(block) == block


def test_a_code_span_does_not_recurse():
    """Backticks win: everything inside is literal, as the old renderer had
    it. Otherwise `**x**` in a code span would come out bold."""
    assert B.runs("`**x**`") == "\x01c**x**\x02"


# --- the reserved bytes ----------------------------------------------------


def test_the_reserved_bytes_are_dropped_from_model_text():
    assert B.clean("a\x00b\x01c\x02d") == "abcd"


def test_cleaning_happens_before_the_marks_not_after():
    """The one thing the plan had in the wrong place.

    Putting the strip in the encoder cannot work: by then the payload is full
    of \\x01 and \\x02 that the strip would remove, and the record would
    arrive as one unstyled run.
    """
    block = B.Block("para", {}, [("t", B.runs(B.clean("**a\x01b**")))])
    assert block.payload == [("t", "\x01sab\x02")]
    assert rt(block) == block


def test_a_nul_in_model_text_does_not_reach_a_record():
    s = B.BlockStream()
    ups = s.feed("before\x00after\n") + s.flush()
    blocks = [u.block for u in ups if u.block is not None]
    assert any("beforeafter" in t for b in blocks for t in b.texts())
    for b in blocks:
        assert "\x00" not in B.encode(b).decode("cp1252")


def test_cr_and_tab_are_not_reserved():
    """\\r is already a line break -- feed() splits with str.splitlines(),
    which splits on it -- and \\t is whitespace inside a fence. B2 compares
    the guest against this path byte for byte, so a strip here would break
    the proof rather than the transcript."""
    assert B.clean("a\tb\rc") == "a\tb\rc"


# --- refusals --------------------------------------------------------------


def test_an_unknown_kind_is_refused():
    with pytest.raises(B.RecordError):
        B.encode(B.Block("marquee", {}, [("t", "no")]))


def test_an_attribute_the_kind_does_not_have_is_refused():
    with pytest.raises(B.RecordError):
        B.encode(B.Block("para", {"level": 2}, [("t", "x")]))


def test_an_attribute_outside_its_range_is_refused():
    with pytest.raises(B.RecordError):
        B.encode(B.Block("heading", {"level": 7}, [("t", "x")]))
    with pytest.raises(B.RecordError):
        B.encode(B.Block("heading", {"level": 0}, [("t", "x")]))
    assert B.encode(B.Block("heading", {"level": 1}, [("t", "x")]))
    assert B.encode(B.Block("heading", {"level": 6}, [("t", "x")]))


def test_a_payload_tag_the_kind_does_not_have_is_refused():
    with pytest.raises(B.RecordError):
        B.encode(B.Block("para", {}, [("l", "c")]))


def test_the_head_cannot_reach_its_own_bound_with_this_vocabulary():
    """MAX_HEAD is a guard against a future kind, not against this one.

    The longest head the vocabulary can produce is a `clip` with its largest
    legal `dropped`, and that is 25 bytes against a 128-byte bound. Worth
    asserting rather than assuming: the bound exists so that a reader can
    parse the first line before knowing what it is reading, and a kind added
    later with six attributes is exactly what would quietly outgrow it.
    """
    longest = max(B.encode(b).split(b"\n")[0] for b in EVERY_KIND)
    assert len(longest) < B.MAX_HEAD // 2, longest
    assert len(B.encode(B.Block("clip", {"dropped": (1 << 30)}))
               .split(b"\n")[0]) <= B.MAX_HEAD


def test_an_attribute_past_its_ceiling_is_refused():
    assert B.encode(B.Block("clip", {"dropped": 10 ** 9}))      # fits
    with pytest.raises(B.RecordError):
        B.encode(B.Block("clip", {"dropped": 1 << 31}))


def test_decoding_a_record_with_no_terminator_is_refused():
    with pytest.raises(B.RecordError):
        B.decode(b"para\nt hello\n")


def test_decoding_an_unknown_kind_is_refused():
    with pytest.raises(B.RecordError):
        B.decode(b"marquee\n.\n")


# --- pack ------------------------------------------------------------------


def test_pack_groups_records_into_bodies_under_the_budget():
    items = [B.Block("para", {}, [("t", "x" * 100)]) for _ in range(10)]
    bodies = B.pack(items, 400)
    assert len(bodies) > 1
    assert all(len(b) <= 400 for b in bodies)


def test_pack_never_ends_a_body_mid_record():
    items = [B.Block("para", {}, [("t", f"line {i}" * 20)]) for i in range(30)]
    for body in B.pack(items, 500):
        assert body.endswith(b".\n")
        assert B.decode(body)                 # parses as whole records


def test_pack_puts_an_oversize_record_in_a_body_of_its_own_whole():
    """The arm that replaces the paginator's mid-run split.

    Cutting a record on a byte boundary cuts between payload lines and leaves
    two fragments the guest must refuse. Deleting the split without naming
    this replacement makes the caller loop forever -- nothing advances.
    """
    big = B.Block("code", {}, [("t", "z" * 5000)])
    small = B.Block("para", {}, [("t", "after")])
    bodies = B.pack([small, big, small], 1000)
    assert len(bodies) == 3
    assert len(bodies[1]) > 1000
    assert B.decode(bodies[1]) == [big]


def test_pack_of_nothing_is_no_bodies_at_all():
    """Not one empty body. An empty LIVE body is sent deliberately to clear
    the open slot, and it must not be something pack produces by accident."""
    assert B.pack([], 1000) == []


def test_a_body_can_hold_several_whole_records():
    a = B.Block("para", {}, [("t", "a")])
    b = B.Block("rule")
    assert B.decode(B.pack([a, b], 10_000)[0]) == [a, b]


# --- the budget is in post-fold bytes --------------------------------------


def test_the_budget_counts_folded_bytes_because_folding_expands():
    """An ellipsis becomes three dots and an em dash becomes two hyphens, so
    a budget counted in characters is not a budget. link.py cuts at a byte
    offset, which would then land inside a record."""
    block = B.Block("para", {}, [("t", "\u2026" * 10)])
    rec = B.encode(block)
    assert b"..." in rec
    assert len(rec) > len("para\nt " + "\u2026" * 10 + "\n.\n")
    for body in B.pack([block] * 20, 300):
        assert len(body) <= 300 or len(B.decode(body)) == 1
