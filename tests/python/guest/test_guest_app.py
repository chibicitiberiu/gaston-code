"""App's lifecycle, run as the shipping code under Wine.

`win/core/app.cpp` is the container every service in gremote.exe (and later
the client) runs under. Its ordering promises are the kind that read right
and behave otherwise, so `win/checks/appcheck.cpp` records every call into a
trace and these assert on the trace, not on the header's description of it.

Letters: i init, s start, x signalStop, j join, d destructor, then the id.
"""

import conftest
import pytest

EXE_NAME = "appcheck.exe"

pytestmark = conftest.needs_guest


@pytest.fixture(scope="module")
def verdicts():
    return conftest.guest_verdicts(EXE_NAME, timeout=60)


def test_every_case_passes(verdicts):
    bad = {k: v for k, v in verdicts.items() if v[0] != "pass"}
    assert not bad, bad


def test_every_case_ran(verdicts):
    assert len(verdicts) == 9, sorted(verdicts)


def test_up_in_order_down_in_reverse_signal_before_join(verdicts):
    """Every service is signalled before any is joined, so threads wind down
    in parallel and one slow service does not hold the rest."""
    assert verdicts["normal-order"][1] == \
        "iA iB iC sA sB sC xC xB xA jC jB jA dC dB dA"


def test_a_failed_init_tears_down_only_what_initialised(verdicts):
    assert verdicts["init-failure-unwinds"][1] == "iA iB xA jA dC dB dA"


def test_a_stuck_service_is_leaked_not_deleted(verdicts):
    """Deleting a service whose thread is still running frees memory that
    thread still touches: a crash later and somewhere else."""
    trace = verdicts["stuck-join-is-leaked"][1]
    assert "jB" in trace and "dB" not in trace


def test_windows_ending_the_session_stops_services_once(verdicts):
    trace = verdicts["end-session-stops-once"][1]
    assert trace.count("xA") == 1 and trace.count("jA") == 1
