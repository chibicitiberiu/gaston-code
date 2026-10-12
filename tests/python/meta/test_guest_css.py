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
