"""libexec, run as the shipping code under Wine.

`win/checks/execcheck.cpp` drives `ExecJob` directly: the refuse-rather-than-
truncate guard, output reaching the sink, the cap, the exit code, a failed
spawn, the timeout, both kinds of cancel, a reader that goes away and the
via-shell flag. What COMMAND.COM does on the
real guest -- REDIR32, the DOS VM, the always-zero exit code -- Wine cannot
show; `win/checks/execprobe.cpp` measures that on the VM
(docs/measurements/execprobe-vm-*).

Since 2026-10-02 it also drives `exec/cpuid.cpp`, which is libexec too. Those
cases assert a SHAPE and never a particular processor, because they run here on
whatever the developer has and on the guest on a Pentium or an Athlon: that the
instruction is detected rather than assumed, that the vendor is the twelve
characters it is specified to be, that the brand string has had the processor's
own padding removed, and that a short buffer truncates rather than overruns.
"""

import conftest
import pytest

EXE_NAME = "execcheck.exe"

pytestmark = conftest.needs_guest


@pytest.fixture(scope="module")
def verdicts():
    return conftest.guest_verdicts(EXE_NAME, timeout=90)


def test_every_case_passes(verdicts):
    bad = {k: v for k, v in verdicts.items() if v[0] != "pass"}
    assert not bad, bad


#: Every case `execcheck.exe` prints, counted rather than listed.
#:
#: NOT A STYLE RULE: a case that stops running prints nothing, and
#: `test_every_case_passes` is satisfied by an empty set. The count is what
#: catches a case that silently went away -- which is why it is asserted
#: exactly rather than as a minimum.
#:
#: 24 -> 29 on 2026-10-02, when `exec/cpuid.cpp` landed in libexec and brought
#: five cases with it.
#: 29 -> 30 on 2026-10-14, stage 4: `force-kill-reclaims-the-dos-vm`, the
#: other half of BACKLOG 17. It is VACUOUS UNDER WINE by construction --
#: there is no WINOA386.MOD here, so it degrades to "a forced kill of an
#: ordinary program still works" and prints vm=0. The line worth reading is
#: the one from the guest, where vm=1; see
#: docs/measurements/forcekill-vm-20261014.txt.
CASE_COUNT = 30


def test_every_case_ran(verdicts):
    assert len(verdicts) == CASE_COUNT, sorted(verdicts)


def test_the_processor_is_asked_rather_than_assumed(verdicts):
    """`GetSystemInfo` gives a family and a model number, and the HELLO header
    built from it read `x86 family 6 model 10, 1 cpu` -- which an agent read as
    a weak machine, choosing an interpreted language over the Visual C++ 6.0
    installed beside it on a 256MB Athlon XP.

    The brand string is what System Properties and dxdiag show, because all
    three read the same CPUID leaves. Nothing here asserts WHICH processor:
    under Wine that is the developer's, and on the guest it is a Pentium with
    no brand string at all, which is a pass.
    """
    for case in ("cpuid-is-detected-not-assumed",
                 "the-vendor-is-twelve-characters",
                 "the-brand-string-has-no-padding",
                 "the-brand-string-fits-the-header-buffer",
                 "a-short-buffer-truncates"):
        assert verdicts[case][0] == "pass", (case, verdicts[case])


def test_an_overlong_command_is_refused_not_truncated(verdicts):
    """The only length guard in the system: a command cut at byte 1023 would
    still run, and `del c:\\temp\\*.tmp` cut short is `del c:\\temp\\*`."""
    assert verdicts["overlong-refused"][0] == "pass"
    assert verdicts["overlong-with-shell-refused"][0] == "pass"
    assert verdicts["short-not-refused"][0] == "pass"


def test_a_cancel_is_reported_as_a_cancel(verdicts):
    """A cancel before run() spawns nothing, and one during it must not come
    back as `ok` just because the killed child exited before the loop looked
    at the flag."""
    assert verdicts["cancel-before-run"][0] == "pass"
    assert verdicts["cancel-mid-run"][0] == "pass"


def test_a_reader_that_is_gone_stops_the_command(verdicts):
    """Otherwise the command runs to its timeout producing output for no one,
    with the worker blocked in the sink the whole time."""
    assert verdicts["sink-gone-stops-it"][0] == "pass"


def test_processes_list_kill_and_launch(verdicts):
    """Toolhelp32 resolved at runtime, the listing, the deny list refusing
    this process even when forced, a launched tree's descendants, and a
    kill that waits for the end rather than trusting TerminateProcess."""
    for case in ("toolhelp-resolves", "list-finds-self",
                 "kill-self-denied-even-forced", "kill-no-such-pid",
                 "launch-overlong-refused", "launch-and-descendants",
                 "kill-waits-for-the-end", "win16-detected-from-the-header"):
        assert verdicts[case][0] == "pass", (case, verdicts[case])


def test_a_stop_reaches_the_whole_tree(verdicts):
    """Killing only the process run() spawned left a grandchild running on
    Wine and NT, and on Win98 killed nothing at all (the pid is REDIR32)."""
    assert verdicts["stop-reaches-the-grandchild"][0] == "pass"
