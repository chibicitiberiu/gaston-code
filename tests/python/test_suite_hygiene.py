"""The suite's own wiring, checked by the suite.

Three times now the same shape of bug has cost real time: a list of test
files maintained by hand beside a directory that grows, and the growth not
reflected in the list. The conftest docstring describes the first one -- 72
cross-language checks skipping in silence while the run reported success.
The second was `make wine-smoke`, which named eight suites when there were
ten, so `test_guest_workcopy.py` and `test_tooldet_agreement.py` were in no
gate at all. The third was `guest-checks`, missing the two binaries those
same suites need.

The fix for all three was to select by MARK rather than by filename, which
moves the question to "is the mark on it?" -- and that is a question a test
can answer. This file asks it.

Nothing here runs a guest binary or needs a toolchain. It reads the tree.
"""

import ast
import os

import pytest

import conftest

TESTS = os.path.dirname(os.path.abspath(__file__))

#: What a suite touches when it needs a built guest binary under Wine.
GUEST_API = {"guest_exe", "WINE", "wine_env", "have_toolchain"}


def _modules():
    for name in sorted(os.listdir(TESTS)):
        if name.startswith("test_") and name.endswith(".py"):
            yield name, os.path.join(TESTS, name)


def _uses_guest_api(tree) -> bool:
    """Does this module import the Wine plumbing out of conftest?"""
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "conftest":
            if GUEST_API & {a.name for a in node.names}:
                return True
    return False


def _marks(tree) -> set:
    """The names in a module-level `pytestmark`, however it is spelled."""
    out = set()
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if not any(getattr(t, "id", "") == "pytestmark" for t in node.targets):
            continue
        for sub in ast.walk(node.value):
            if isinstance(sub, ast.Attribute):
                out.add(sub.attr)
            elif isinstance(sub, ast.Name):
                out.add(sub.id)
    return out


@pytest.mark.parametrize("name,path", list(_modules()))
def test_a_suite_that_needs_wine_says_so(name, path):
    """...because otherwise it is in no gate, and looks fine.

    `test_guest_frame.py` was exactly this: it leaned on `guest_exe`'s own
    skip, which works, so nothing was red -- but `-m guest` could not see it
    and neither could the Makefile's list. An unmarked Wine suite also lands
    in `make test-fast`, where it costs the two seconds per test that target
    exists to avoid.

    The filename prefix is not the signal and never was: `test_guest_css.py`
    is pure Python, and `test_tooldet_agreement.py` needs Wine.
    """
    tree = ast.parse(open(path).read(), path)
    if not _uses_guest_api(tree):
        return
    assert "needs_guest" in _marks(tree) or "guest" in _marks(tree), (
        f"{name} imports conftest's Wine plumbing but has no module-level "
        f"`pytestmark = conftest.needs_guest`, so `-m guest` cannot select "
        f"it and `make wine-smoke` will not run it")


def test_the_guest_mark_selects_something():
    """A mark nothing carries deselects nothing, and `make wine-smoke` would
    pass instantly and green while running no guest code whatsoever."""
    marked = [n for n, p in _modules()
              if {"needs_guest", "guest"} & _marks(ast.parse(open(p).read(), p))]
    assert len(marked) >= 10, marked


def test_needs_guest_carries_both_a_selector_and_a_condition():
    """It is a list for a reason, and a future edit could quietly drop half.

    Losing `guest` puts every Wine suite back in the fast run; losing the
    `skipif` makes the suite fail rather than skip on a machine with no Open
    Watcom, which is the one case skipping is actually for.
    """
    names = {m.name for m in conftest.needs_guest}
    assert names == {"guest", "skipif"}, names
