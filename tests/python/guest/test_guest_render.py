"""libview's renderer, run as the shipping code under Wine.

`win/view/render.cpp` is stage 3's whole point: the bridge stops emitting
markup and sends what a block IS, and this turns that back into the HTML
MSHTML has always been given. These diff `win/checks/rendercheck.cpp`'s
verdicts rather than trusting the header.

The cases worth naming are the ones that killed a design choice while the
stage was being planned, because nothing else in the tree would notice them
and each was found by RUNNING the old renderer rather than reading it:

  * `code` is specified with the `font:` SHORTHAND, which resets weight and
    style, so <b><code>x</code></b> and <code><b>x</b></code> do not render
    alike and the nesting order is visible.
  * Nesting reaches depth THREE: "__a *b `c` d* e__".
  * "**a****b**" is two <b> spans a flat run list cannot tell from one.

Those three are why the inline encoding is a stack and not three bits of
per-character state.

AND THE ESCAPE COUNT, which is the subtlest thing in the stage. The bridge's
`esc` is html.escape(quote=False) -- THREE characters -- and
user_turn('say "hi"') emits a raw double quote today. The guest's own
`esc_into` escapes four, correctly, because it escapes text the guest
composed and that never passes through a record. A renderer that escaped the
quote in record content would produce &quot; where the bridge produces ", in
the one place none of the bridge's five escaping tests looks.

WHAT THIS FILE CANNOT PROVE is that the markup is the SAME markup the bridge
used to send: every expectation here was typed by the same person who wrote
the renderer. test_render_equivalence.py is the half that can, by running
both and comparing bytes.
"""

import os

import conftest
import pytest

EXE_NAME = "rendercheck.exe"

#: Asserted EXACTLY. A case that silently stops printing would otherwise
#: pass by absence, which is the failure mode guest_verdicts's own docstring
#: warns about: a binary printing nothing parses to {} and every assertion
#: over it holds.
CASE_COUNT = 76

pytestmark = conftest.needs_guest


@pytest.fixture(scope="module")
def verdicts():
    # The repo's own view.tpl, not a copy: this test is also what proves
    # the SHIPPED file is the one that produces today's markup, which is
    # what lets B2's equality proof stand behind it.
    tpl = os.path.join(conftest.REPO, "win", "resources")
    return conftest.guest_verdicts(EXE_NAME, "--template", tpl)


def test_every_case_passes(verdicts):
    bad = {k: v for k, v in verdicts.items() if v[0] != "pass"}
    assert not bad, bad


def test_every_case_ran(verdicts):
    assert len(verdicts) == CASE_COUNT, sorted(verdicts)


# --- the three that killed the flat encoding --------------------------------


def test_inline_runs_nest_three_deep(verdicts):
    assert verdicts["span.depth.three"][0] == "pass"


def test_a_run_nests_inside_one_of_its_own_kind(verdicts):
    """Which is why the stack cannot be a bitmask or one slot per kind."""
    assert verdicts["span.same.kind.nested"][0] == "pass"


def test_two_adjacent_runs_stay_two(verdicts):
    assert verdicts["span.adjacent.same"][0] == "pass"


def test_an_unclosed_run_is_closed_rather_than_left_open(verdicts):
    """A LIVE update carries the open block mid-write, so a truncated run is
    normal -- and an unbalanced <b> handed to MSHTML leaks the styling into
    everything appended after it."""
    assert verdicts["span.unclosed.is.closed"][0] == "pass"


# --- escaping ---------------------------------------------------------------


def test_record_content_escapes_three_characters_not_four(verdicts):
    assert verdicts["esc.three"][0] == "pass"
    assert verdicts["esc.quote.is.raw"][0] == "pass"


def test_text_is_not_escaped_twice(verdicts):
    assert verdicts["esc.not.twice"][0] == "pass"


# --- the characters that travel ---------------------------------------------


def test_cr_and_tab_reach_the_document(verdicts):
    """\\r is already a line break on the far side -- HtmlStream.feed splits
    with str.splitlines(), which splits on it -- so one that survives into a
    payload is in the unterminated tail, where today's HTML carries it raw.
    An earlier draft of the plan had this backwards and specified dropping
    it, which would have made the equality proof unsatisfiable."""
    assert verdicts["nl.cr.travels"][0] == "pass"
    assert verdicts["nl.tab.travels"][0] == "pass"


# --- the wrap ---------------------------------------------------------------


def test_zero_width_does_not_wrap_and_does_not_hang(verdicts):
    """NEW BEHAVIOUR WITH NO ORACLE. The bridge's helper HUNG on width 0:
    rfind(" ", 0, 0) is -1, the cut lands at 0 and the line never advances.
    Nothing on the bridge could pass 0; the guest can, because wrapcol comes
    from a window. The bridge helper gained the same guard so the two still
    agree."""
    assert verdicts["wrap.zero.does.not.wrap"][0] == "pass"


def test_an_unbreakable_run_is_broken_anyway(verdicts):
    """A 300-character path with no spaces is the case that started this:
    IE5 has no pre-wrap, so a <pre> holding one long line draws a horizontal
    scrollbar inside a box one line tall, on top of the only text there."""
    assert verdicts["wrap.unbreakable.breaks.anyway"][0] == "pass"


# --- the permission card ----------------------------------------------------


def test_payload_order_is_honoured_not_the_tag_order(verdicts):
    """render_request puts its argument table last and rules_view puts its
    paragraphs after it, so a renderer emitting by tag in a fixed order gets
    one of the two wrong."""
    assert verdicts["perm.steps.then.table"][0] == "pass"
    assert verdicts["perm.table.then.paragraphs"][0] == "pass"


def test_only_a_settleable_card_carries_the_id(verdicts):
    """settle_perm keys on it, so a rules view carrying one could be
    collapsed by answering an unrelated prompt."""
    assert verdicts["perm.ask"][0] == "pass"
    assert verdicts["perm.view.is.not.settleable"][0] == "pass"


def test_a_head_this_build_does_not_know_is_absent(verdicts):
    """A bridge newer than the client. A card missing its heading still says
    what it is asking; the literal word "future" at the top of a permission
    prompt would be worse."""
    assert verdicts["perm.unknown.head.is.absent"][0] == "pass"


# --- refusals ---------------------------------------------------------------


def test_a_malformed_record_is_refused(verdicts):
    assert verdicts["refuse.no.terminator"][0] == "pass"
    assert verdicts["refuse.truncated.head"][0] == "pass"
    assert verdicts["refuse.attr.without.value"][0] == "pass"


def test_an_unknown_kind_renders_nothing_rather_than_refusing(verdicts):
    """The opposite of the above, deliberately: an unknown KIND means a
    bridge newer than this client, and a transcript missing one block beats
    a transcript that stopped."""
    assert verdicts["unknown.kind.renders.nothing"][0] == "pass"


def test_a_body_holds_whole_records_and_zero_is_real(verdicts):
    """An empty LIVE body is what clears the open slot."""
    assert verdicts["two.records"][0] == "pass"
    assert verdicts["empty.body"][0] == "pass"
    assert verdicts["count.zero"][0] == "pass"


def test_a_sink_saying_no_stops_the_render(verdicts):
    """ByteSink's whole design. A producer that ignores it is the bug the
    return value exists to make visible."""
    assert verdicts["sink.false.stops.the.render"][0] == "pass"


# --- the template layer -----------------------------------------------------


def test_the_shipped_template_file_loads(verdicts):
    assert verdicts["tpl.load"][0] == "pass", verdicts["tpl.load"][1]
    assert verdicts["tpl.version"][0] == "pass", verdicts["tpl.version"][1]


def test_a_repeated_value_hole_is_filled_every_time(verdicts):
    """{level} is in a heading's prefix AND its suffix."""
    assert verdicts["tpl.repeated.name.hole"][0] == "pass"


def test_a_missing_file_degrades_and_says_so(verdicts):
    """IniFile cannot see a missing file at all -- ok() only checks the path
    has a separator, so every read succeeds with the caller's default. The
    loader does its own existence check, and the result is a degraded
    renderer rather than a client that will not start: the box is updated by
    hand with COPY and a half-updated one is a real state."""
    assert verdicts["tpl.missing.file.is.reported"][0] == "pass"
    assert verdicts["tpl.missing.file.degrades"][0] == "pass"
    assert verdicts["tpl.degraded.still.renders"][0] == "pass"
