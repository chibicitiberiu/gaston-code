"""The CSS classes the guest emits must exist in the bridge's stylesheet.

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

from bridge.view import html as H

HERE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
HEADER = os.path.join(HERE, "win", "app", "htmlview.h")


def guest_classes() -> dict:
    src = open(HEADER, encoding="utf-8").read()
    found = dict(re.findall(r'#define\s+(RC_CSS_\w+)\s+"([a-z-]+)"', src))
    assert found, "no RC_CSS_* defines found; did the header move?"
    return found


def test_every_class_the_guest_emits_is_in_the_bridge_s_stylesheet():
    css = H.document()
    for macro, name in guest_classes().items():
        assert f".{name}" in css, (
            f"{macro} is \"{name}\" and the bridge's stylesheet has no "
            f"`.{name}` rule -- the guest's blocks would render unstyled, "
            f"silently")


def test_the_guest_and_the_bridge_agree_on_what_each_class_means():
    """Not just that the name exists, but that both sides use it for the same
    kind of block -- otherwise an error would render as a notice."""
    names = guest_classes()
    assert names["RC_CSS_NOTICE"] in H.notice("x")
    assert names["RC_CSS_ERROR"] in H.error("x")
    assert names["RC_CSS_USER"] in H.user_turn("x")
