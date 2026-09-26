"""libexec's ExecJob, run as the shipping code under Wine.

`win/checks/execcheck.cpp` drives `ExecJob` directly: the refuse-rather-than-
truncate guard, output reaching the sink, the cap, the exit code, a failed
spawn, the timeout and both kinds of cancel. What COMMAND.COM does on the
real guest -- REDIR32, the DOS VM, the always-zero exit code -- Wine cannot
show; `win/checks/execprobe.cpp` measures that on the VM
(docs/measurements/execprobe-vm-*).
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


def test_every_case_ran(verdicts):
    assert len(verdicts) == 10, sorted(verdicts)


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
