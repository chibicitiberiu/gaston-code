"""IniFile's refusals, run as the shipping code under Wine.

`win/core/inifile.cpp` is the config reader for gremote.exe (docs/REMOTE.md,
Configuration), and its whole job is to refuse: a value present but wrong
stops startup instead of running on something nobody wrote. These diff
`win/checks/inifilecheck.cpp`'s verdicts rather than trusting the header.

The cases worth naming are the ones GetPrivateProfileString gets wrong on its
own, because nothing else in the tree would notice them.
"""

import conftest
import pytest

EXE_NAME = "inifilecheck.exe"

pytestmark = conftest.needs_guest


@pytest.fixture(scope="module")
def verdicts():
    return conftest.guest_verdicts(EXE_NAME)


def test_every_case_passes(verdicts):
    bad = {k: v for k, v in verdicts.items() if v[0] != "pass"}
    assert not bad, bad


def test_every_case_ran(verdicts):
    """A case that silently stops printing would otherwise pass by absence."""
    assert len(verdicts) == 31, sorted(verdicts)


def test_bare_filename_is_refused(verdicts):
    """The profile API reads a path with no directory from C:\\WINDOWS."""
    assert verdicts["bare-name-refused"][0] == "pass"


def test_inline_comment_is_not_a_number(verdicts):
    """`port=7401 ; note` reads as the whole string; comments go on their
    own line, and a reader that took the leading digits would hide that."""
    assert verdicts["int-inline-comment-refused"][0] == "pass"


def test_one_byte_over_is_told_apart_from_an_exact_fit(verdicts):
    """GetPrivateProfileString returns cap-1 for both."""
    assert verdicts["string-exact-fit"][0] == "pass"
    assert verdicts["string-one-over-refused"][0] == "pass"
    assert "eight" in verdicts["string-one-over-refused"][1]


def test_a_typo_is_an_error_not_a_default(verdicts):
    assert verdicts["unknown-key-refused"][0] == "pass"
    assert "prot" in verdicts["unknown-key-refused"][1]


def test_a_failed_write_is_reported(verdicts):
    """audit 01.37: a settings dialog closed as if accepted while nothing
    reached the file, and the next launch read the old values back."""
    assert verdicts["write-failure-reported"][0] == "pass"
    assert verdicts["write-then-read-back"][0] == "pass"
