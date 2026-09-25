"""App's lifecycle, run as the shipping code under Wine.

`win/core/app.cpp` runs a fixed sequence of hooks -- initialize, parseArgs,
initializeServices, startServices, main, stopServices, cleanup -- that a
derived app fills, holding its services as UniqueObj members it creates and
takes down itself. Its ordering promises are the kind that read right and
behave otherwise, so `win/checks/appcheck.cpp` records every call into a
trace and these assert on the trace, not on the header's description of it.

Tokens: I. initialize, P. parseArgs, C. cleanup, f. preTranslate consumed;
then i init, s start, x signalStop, j join, d destructor, and the service id.
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
    assert len(verdicts) == 11, sorted(verdicts)


def test_up_in_order_down_in_reverse_signal_before_join(verdicts):
    """Every service is signalled before any is joined, so threads wind down
    in parallel and one slow service does not hold the rest."""
    assert verdicts["normal-order"][1] == \
        "I. P. iA iB iC sA sB sC xC xB xA jC jB jA dC dB dA C."


def test_a_failed_init_stops_what_exists_and_creates_nothing_after(verdicts):
    """B was created and its init failed; C was never created. Stopping
    reaches every service that exists, so B must cope with a stop after a
    failed init."""
    assert verdicts["init-failure-unwinds"][1] == \
        "I. P. iA iB xB xA jB jA dB dA C."


def test_a_stuck_service_is_leaked_not_deleted(verdicts):
    """Deleting a service whose thread is still running frees memory that
    thread still touches: a crash later and somewhere else."""
    trace = verdicts["stuck-join-is-leaked"][1]
    assert "jB" in trace and "dB" not in trace


def test_windows_ending_the_session_stops_services_once(verdicts):
    trace = verdicts["end-session-stops-once"][1]
    assert trace.count("xA") == 1 and trace.count("jA") == 1


def test_pretranslate_sees_messages_before_dispatch(verdicts):
    """The client's accelerators and IsDialogMessage ride on this."""
    assert verdicts["pretranslate-consumes"][0] == "pass"


def test_exit_with_zero_is_not_a_failure(verdicts):
    """The single-instance check stops the second copy this way."""
    assert verdicts["exit-with-skips-the-rest"][1] == "I. C."
