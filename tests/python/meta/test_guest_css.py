"""The class names AND container ids the guest hard-codes must exist on the bridge.

The bridge renders the transcript and owns the stylesheet, which the guest
receives whole in the DOC frame. But the guest builds three blocks itself --
its own echo of what you typed, a notice, an error -- and hard-codes their
class names.

That is a contract between two files in two languages with nothing linking
them. Rename `.notice` on the bridge and the guest's notices silently become
unstyled text: no error, no log line, nothing to notice until someone looks
at a screenshot.

This is a pure-Python test on purpose -- it reads the C header rather than
running the guest, so it works on a machine with no Open Watcom and no Wine
and cannot be one of the checks that skips silently.
"""

import os
import re

import conftest

from bridge.view import html as H

HERE = conftest.REPO
HEADER = os.path.join(HERE, "win", "app", "htmlview.h")


def guest_classes() -> dict:
    src = open(HEADER, encoding="utf-8").read()
    total = len(re.findall(r'#define\s+RC_CSS_\w+', src))
    found = dict(re.findall(r'#define\s+(RC_CSS_\w+)\s+"([^"]+)"', src))
    assert total > 0, "no RC_CSS_* defines found; did the header move?"
    assert len(found) == total, (
        f"matched {len(found)} of {total} RC_CSS_* defines; check regex or header syntax"
    )
    return found


def test_every_class_the_guest_emits_is_in_the_bridge_s_stylesheet():
    css = H.document()
    for macro, name in guest_classes().items():
        for cls in name.split():
            assert f".{cls}" in css, (
                f"{macro} names class \"{cls}\" and the bridge's stylesheet has no "
                f"`.{cls}` rule -- the guest's blocks would render unstyled, "
                f"silently")


def test_the_guest_and_the_bridge_agree_on_what_each_class_means():
    """Not just that the name exists, but that both sides use it for the same
    kind of block -- otherwise an error would render as a notice."""
    names = guest_classes()
    # Through the renderer, because the constructors return structure now and
    # the class name is the renderer's business. Stage 3 A7; at C2 the
    # renderer is the guest's and this half of the test becomes a check
    # against view.tpl instead.
    from bridge.view.htmlrender import render
    assert names["RC_CSS_NOTICE"] in render(H.notice("x"))
    assert names["RC_CSS_ERROR"] in render(H.error("x"))
    assert names["RC_CSS_USER"] in render(H.user_turn("x"))


def guest_slots() -> dict:
    src = open(HEADER, encoding="utf-8").read()
    found = dict(re.findall(r'#define\s+(RC_SLOT_\w+)\s+"([a-z0-9-]+)"', src))
    assert found, "no RC_SLOT_* defines found; did the header move?"
    return found


def test_the_container_ids_the_guest_looks_up_exist_in_the_skeleton():
    """03.21: the guest finds #t and #l by literal in a document the bridge wrote.

    This one is worse than an unstyled block. `find_id` returning NULL is not
    an error anywhere -- every append then goes nowhere and the transcript is
    blank forever, on a machine with no console to say so.
    """
    doc = H.document()
    for macro, ident in guest_slots().items():
        assert f'id="{ident}"' in doc, (
            f'{macro} is "{ident}" and the bridge\'s skeleton has no '
            f'element with that id -- the guest would find nothing and every '
            f'append would land in no element at all')


def test_the_two_slots_are_distinct():
    """They do different jobs: one accumulates, one is replaced wholesale.

    If both macros ever named the same element, finished blocks and the
    in-progress block would share a container and each new live update would
    wipe the transcript above it.
    """
    slots = guest_slots()
    assert slots["RC_SLOT_DONE"] != slots["RC_SLOT_LIVE"]


# --- the other direction ----------------------------------------------------
#
# Everything above walks the classes the GUEST names. A class the BRIDGE
# invents is invisible to all of it, which is how `.thinking` came to be
# emitted for as long as the class had existed with no rule anywhere:
# presentation.py built `<div class="{style}">` from the style NAME, so
# there was no literal for anything to grep and no macro for anything to
# sweep. Found while planning stage 3, fixed in A10.
#
# After C2 the renderer is the guest's and this reads win/resources/view.tpl
# instead of htmlrender.py. The assertion does not change.

def rendered_classes() -> set:
    """Every class the renderer can put in the document."""
    from bridge.view import blocks as B
    from bridge.view.htmlrender import _PERM_HEAD, render

    samples = [
        B.Block("para", {}, [("t", "x")]),
        B.Block("code", {}, [("l", "c"), ("t", "x")]),
        B.Block("heading", {"level": 3}, [("t", "x")]),
        B.Block("bullet", {}, [("t", "x")]),
        B.Block("numbered", {}, [("t", "x")]),
        B.Block("quote", {}, [("t", "x")]),
        B.Block("rule"),
        B.Block("table", {"header": 1}, [("r", ""), ("c", "x")]),
        B.Block("user", {}, [("t", "x")]),
        B.Block("notice", {}, [("t", "x")]),
        B.Block("error", {}, [("t", "x")]),
        B.Block("tool", {}, [("n", "e"), ("t", "x")]),
        B.Block("toolresult", {"error": 0, "clipped": 3}, [("t", "x")]),
        B.Block("toolresult", {"error": 1}, [("t", "x")]),
        B.Block("thinking", {}, [("t", "x")]),
        B.Block("clip", {"dropped": 3}),
        B.Block("exportheading", {}, [("h", "t"), ("t", "w")]),
    ] + [B.Block("permission", {"settleable": s},
                 [("h", h), ("b", "x"), ("d", "y"), ("w", "z"), ("p", "p"),
                  ("t", "t"), ("s", "s"), ("k", "k"), ("v", "v")])
         for s, h in ((1, "ask"), (0, "view"))]

    assert {b.kind for b in samples} == set(B.KINDS), (
        "a kind with no sample here is a kind whose classes nothing checks")

    out = set()
    for b in samples:
        for m in re.findall(r'class="([^"]+)"', render(b)):
            out.update(m.split())
    for head in _PERM_HEAD.values():
        for m in re.findall(r'class="([^"]+)"', head):
            out.update(m.split())
    return out


def test_every_class_the_renderer_emits_has_a_rule():
    """The mirror of the first test, and the one that was missing.

    A class with no rule is not an error anywhere: the block renders, it
    just looks like whatever is around it. `.thinking` looked exactly like
    the model's answer for the whole life of the feature.
    """
    # Both sheets, because an export gets WIRE_CSS + EXPORT_CSS and the
    # `exportheading` kind only ever appears there. Checking only the live
    # sheet would report a false positive for it; checking only the union
    # would miss a LIVE class that had slipped into the export-only sheet,
    # so the split is asserted too, below.
    css = H.STYLESHEET + H.EXPORT_CSS
    missing = sorted(c for c in rendered_classes() if f".{c}" not in css)
    assert not missing, (
        f"the renderer emits {missing} and no stylesheet has a rule for "
        f"them -- those blocks render as whatever is around them, with no "
        f"error and no log line")


def test_only_the_export_heading_depends_on_the_export_only_sheet():
    """The live view never gets EXPORT_CSS, so a class that drifted into it
    would be unstyled on screen and fine in a saved file -- which is the
    hardest version of this bug to notice, because the export looks right."""
    live_only = {c for c in rendered_classes()
                 if f".{c}" not in H.STYLESHEET}
    assert live_only == {"exporthead"}, (
        f"{sorted(live_only - {'exporthead'})} are emitted into the live "
        f"view but only styled in EXPORT_CSS")


def test_the_two_sweeps_overlap_where_they_should():
    """Both sides name .notice, .err and .you, which is the whole point of
    the first test -- it is a contract, not a coincidence. If this ever
    fails, one of the two sweeps has stopped seeing what it was written
    for."""
    shared = {c for c in guest_classes().values() for c in c.split()}
    assert {"notice", "err", "you"} <= shared
    assert {"notice", "err", "you"} <= rendered_classes()
