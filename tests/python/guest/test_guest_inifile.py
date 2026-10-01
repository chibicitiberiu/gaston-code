"""IniFile's refusals, run as the shipping code under Wine.

`win/core/inifile.cpp` is the config reader for gremote.exe (docs/REMOTE.md,
Configuration), and its whole job is to refuse: a value present but wrong
stops startup instead of running on something nobody wrote. These diff
`win/checks/inifilecheck.cpp`'s verdicts rather than trusting the header.

The cases worth naming are the ones GetPrivateProfileString gets wrong on its
own, because nothing else in the tree would notice them.

AND THE ONE THAT WINE CANNOT SEE AT ALL, which is why this file now says so out
loud. `has()` decided a key was absent by probing with a sentinel string and
checking whether it came back; on real Windows 98 the 16-bit profile code behind
the API does not return a control character unaltered, so EVERY ABSENT KEY
LOOKED PRESENT, read back as the empty string, and refused startup as "not a
whole number". That stopped a real Pentium 200 from starting on 2026-10-01 the
first time a new optional setting shipped into an installed ini that predated it
-- `gremote` refused, `gupdate` rolled the image back, and the only record was
the `.PRV` rotation that had landed hours earlier.

`has-absent` and `has-absent-section` below had covered that path since this file
was written, and passed on Wine throughout, because Wine hands the sentinel back
intact. They still pass against the broken implementation. So the case that
earns its place is `has-a-value-that-looks-like-a-probe`: a key whose VALUE
equals the probe string, which the old code reported absent and whose user-written
value it threw away for a default. Same root cause -- a lookup that works only
while one particular string survives a round trip -- and it is the one shape of
the failure Wine CAN reproduce. Verified: against the old implementation it and
its getString sibling fail and the eight absent-key cases all still pass.
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
    assert len(verdicts) == 36, sorted(verdicts)


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


def test_a_lookup_does_not_depend_on_a_sentinel_surviving(verdicts):
    """The case with teeth, and the only one here that fails against the
    implementation it replaced.

    `has()` now asks twice with two ordinary defaults that differ in content and
    in length: a key that is there answers the same both times because the answer
    comes out of the file, and a key that is not there hands each default back.
    Nothing has to survive a round trip, so no value can collide with a marker
    and no platform's mangling of one can be mistaken for absence.

    The old sentinel version got this exactly backwards for a value that happened
    to equal its marker -- it reported the key absent and silently substituted a
    default for something a person had written into the file."""
    assert verdicts["has-a-value-that-looks-like-a-probe"][0] == "pass"
    assert verdicts["string-value-that-looks-like-a-probe"][0] == "pass"


def test_an_absent_key_or_section_takes_its_documented_default(verdicts):
    """ABSENT IS NOT INVALID. CLAUDE.md's rule is that a value present and wrong
    refuses startup; a key that is not there takes its default and is not an
    error. Without that, shipping any new optional setting breaks every install
    whose file predates it -- and on the P200 "breaks" means walking over with a
    floppy, which is what 2026-10-01 cost.

    Both polarities for the boolean, because a default of false being honoured
    and a default of false arriving by accident look identical from one side."""
    for case in ("has-absent", "has-absent-section",
                 "bool-absent-default", "int-absent-default",
                 "string-absent-default",
                 "bool-absent-section-takes-the-default",
                 "int-absent-section-takes-the-default",
                 "string-absent-section-takes-the-default"):
        assert verdicts[case][0] == "pass", (case, verdicts[case])


def test_a_key_with_nothing_after_it_is_still_present(verdicts):
    """`key=` is PRESENT, which is what makes getInt refuse it rather than take
    a default nobody wrote. It is the case a two-probe absence test could most
    easily get wrong, since both probes return the same empty string -- and the
    same string is exactly the right answer here."""
    assert verdicts["has-empty-value"][0] == "pass"
    assert verdicts["string-empty-value"][0] == "pass"
    assert verdicts["int-empty-refused"][0] == "pass"
