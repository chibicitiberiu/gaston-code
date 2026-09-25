"""libautomation's selector grammar and tree walker, under Wine.

What Wine settles and what it does not, so a green run here is not mistaken
for a green run on the guest: the grammar is pure string work and Wine
proves it completely. The walk is USER32 behaviour, and Wine proves only
that it does not crash and that the shapes come back right -- the timings,
the hung-window tiers and MSAA all need the real machine (see
`docs/UIAUTOMATION.md`, "What only the P200 can answer").
"""
import conftest
import pytest

EXE_NAME = "autocheck.exe"
pytestmark = conftest.needs_guest


@pytest.fixture(scope="module")
def verdicts():
    return conftest.guest_verdicts(EXE_NAME, timeout=60)


def test_every_case_passes(verdicts):
    bad = {k: v for k, v in verdicts.items() if v[0] != "pass"}
    assert not bad, bad


def test_every_case_ran(verdicts):
    #: A hard count, so a case that stops being emitted fails instead of
    #: vanishing. Raise it deliberately when cases are added.
    assert len(verdicts) == 49, sorted(verdicts)


def test_the_grammar_refuses_what_it_should(verdicts):
    """Every rejection carries a reason a person can act on.

    A parser that returns false without saying why moves the work to
    whoever typed the selector, and they cannot see the grammar.
    """
    for case in ("reject-empty", "reject-dangling-gt", "reject-bad-attr",
                 "reject-no-equals", "reject-unterminated",
                 "reject-bad-pseudo", "reject-nth-zero",
                 "reject-nonnumeric-id"):
        verdict, detail = verdicts[case]
        assert verdict == "pass", (case, detail)
        assert detail.strip(), f"{case} refused without saying why"


def test_an_unknown_msaa_attribute_does_not_reject(verdicts):
    """The one that would silently match nothing if it were wrong.

    A selector naming an MSAA attribute is evaluated before anything has
    paid for MSAA on that node. Treating "not yet known" as "does not
    match" drops every candidate before it can be enriched, and the
    selector then matches nothing, ever, with no error anywhere.
    """
    assert verdicts["msaa-unknown-passes"][0] == "pass"
    assert verdicts["msaa-known-matches"][0] == "pass"
    assert verdicts["msaa-known-rejects"][0] == "pass"


def test_a_selector_needing_msaa_says_so(verdicts):
    """Otherwise the caller never enriches and the match never happens."""
    assert verdicts["msaa-flagged-when-needed"][0] == "pass"
    assert verdicts["msaa-not-flagged-otherwise"][0] == "pass"


def test_the_walk_stops_when_the_sink_says_stop(verdicts):
    """A sink that has found what it wanted must end the walk.

    Not an optimisation on a 44-node desktop; it is what makes a selector
    resolve without enumerating everything behind it.
    """
    verdict, detail = verdicts["walk-stops-when-sink-says"]
    assert verdict == "pass", detail
    assert "end=3" in detail, detail      # WALK_STOPPED


def test_hidden_windows_are_not_walked(verdicts):
    """The rule that stopped a walk realising the Start menu.

    docs/UIAUTOMATION.md: a tool that reports what is on screen must not
    change what is on screen.
    """
    assert verdicts["walk-skips-hidden"][0] == "pass"


def test_the_child_index_path_is_built(verdicts):
    """`0/0` for the first child of the first root.

    The path is what survives a repaint when a caption does not, so it has
    to be right even on a one-deep tree.
    """
    verdict, detail = verdicts["walk-builds-child-path"]
    assert verdict == "pass", detail
    assert detail == "0/0", detail


def test_input_refuses_our_own_process_not_our_own_hwnd(verdicts):
    """The correction BACKLOG.md section 9 records, executed.

    "Never send keys to our own window" has to compare the target's
    PROCESS against ours. Comparing the HWND against the main window
    misses a child control, which is a different HWND on the same thread
    -- and that is how the agent ends up typing into its own input box in
    a self-feeding loop.
    """
    assert verdicts["input-refuses-own-window"][0] == "pass"
    assert verdicts["input-refuses-own-child"][0] == "pass"
    assert verdicts["input-click-refuses-self"][0] == "pass"
    assert verdicts["input-type-refuses-self"][0] == "pass"


def test_the_foreground_lock_is_put_back(verdicts):
    """Leaving it at zero outlives the process.

    It is a registry value: an earlier probe set it to 0 and left it, it
    survived a reboot, and the original 15000 could never be measured
    again. Anything that changes a machine setting restores it.
    """
    verdict, detail = verdicts["input-restores-foreground-lock"]
    assert verdict == "pass", detail


def test_sendinput_is_resolved_and_not_imported(verdicts):
    """Present on 98, absent on 95 and NT4 before SP3.

    A static import of something absent kills the image at load, before
    WinMain, with nothing written and nothing to debug -- so it is found
    with GetProcAddress and the fallback is keybd_event.
    """
    assert verdicts["input-resolves-sendinput-dynamically"][0] == "pass"
