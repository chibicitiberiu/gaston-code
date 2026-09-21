"""The three answers to a filename collision, run as the shipping code.

`win/checks/workcheck.cpp` builds real trees, runs the real
`rc_workcopy_run`, and asserts the CONTENT of every file afterwards. This is
the part that makes it a gate rather than something someone ran once.

Why it is worth a binary of its own: the three modes differ only in what
happens to the two copies that clash. `stash` is handed the incoming file in
one of them and the target's own in the other, one character apart at the
call site, and swapping those would still compile, still copy, still report
success -- and would put the version the user chose to keep into a staging
folder while the one they were warned about went live. Nothing about reading
the code establishes which way round it is.

Wine proves the code and never the platform, so the same binary is run on the
Win98 guest by hand at least once per change to it; see docs/PROGRESS.md.
"""


import pytest

import conftest


EXE_NAME = "workcheck.exe"

pytestmark = conftest.needs_guest


@pytest.fixture(scope="module")
def report(tmp_path_factory):
    """Every line the binary printed, and its exit code.

    Run in a directory of its own because it makes and removes real trees --
    a test that scribbles in the repo is one nobody can run twice.
    """
    cwd = tmp_path_factory.mktemp("workcheck")
    out = conftest.run_guest(EXE_NAME, timeout=300, cwd=cwd)
    text = out.stdout.decode("latin-1")
    assert text.strip(), out.stderr.decode("latin-1", "replace")[:800]
    return text, out.returncode


def test_every_assertion_in_the_copy_engine_holds(report):
    text, code = report
    failures = [ln for ln in text.splitlines() if "FAIL" in ln]
    assert not failures, "\n".join(failures)
    assert code == 0


def test_it_actually_ran_all_four_scenarios(report):
    """A binary that exits 0 having checked nothing passes the test above.

    Not hypothetical: an earlier version of this file asserted only on
    "FAIL", so a build where the fixture could not be created would have been
    green and silent.
    """
    text, _ = report
    for heading in ("keep what is already there",
                    "replace with what is brought",
                    "let the assistant merge",
                    "a destination that does not exist yet",
                    "refusals the copy must make"):
        assert heading in text, heading
    tail = text.strip().splitlines()[-1]
    count, failed = tail.split(" checks, ")
    assert int(count) >= 40, tail
    assert failed == "0 failed", tail


def test_merge_and_replace_are_not_the_same_thing(report):
    """The one confusion that would survive every other check here.

    Both modes stage a file and both leave one live; they differ only in
    WHICH. Asserting the two lines by name means a swap fails loudly instead
    of moving a pass from one scenario to the other.
    """
    text, _ = report
    assert "ok    destination kept live              " in text
    assert "Gaston-Incoming\\BOTH.TXT = from-source" in text
    assert "Gaston-Replaced\\BOTH.TXT = from-dest" in text
