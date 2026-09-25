"""core/window.cpp's Window and SubclassedWindow, run as the shipping code
under Wine.

They replace a static window procedure plus a global instance in every
module that owns or subclasses a window. What they must get right is
lifetime -- WM_CREATE seeing its own handle, a window destroyed from outside
being noticed rather than destroyed again later, an object deleted taking its
window with it -- and that two objects never share state, which the static
s_instance pattern they replace could not offer.
"""

import conftest
import pytest

EXE_NAME = "windowcheck.exe"

pytestmark = conftest.needs_guest


@pytest.fixture(scope="module")
def verdicts():
    return conftest.guest_verdicts(EXE_NAME, timeout=60)


def test_every_case_passes(verdicts):
    bad = {k: v for k, v in verdicts.items() if v[0] != "pass"}
    assert not bad, bad


def test_every_case_ran(verdicts):
    assert len(verdicts) == 17, sorted(verdicts)


def test_a_window_destroyed_from_outside_is_not_destroyed_again(verdicts):
    """Win9x recycles handle values; a stale one destroyed later could be
    somebody else's window."""
    assert verdicts["outside-destroy-noticed"][0] == "pass"
    assert verdicts["subclass-outside-destroy"][0] == "pass"


def test_two_objects_do_not_share_state(verdicts):
    assert verdicts["two-instances-one-class"][1] == "10 20"
    assert verdicts["two-subclasses-independent"][0] == "pass"
