"""App's lifecycle, run as the shipping code under Wine.

`win/core/app.cpp` runs a fixed sequence of hooks -- initialize, parseArgs,
initializeServices, startServices, main, stopServices, cleanup -- that a
derived app fills, holding its services as UniqueObj members it creates and
takes down itself. Its ordering promises are the kind that read right and
behave otherwise, so `win/checks/appcheck.cpp` records every call into a
trace and these assert on the trace, not on the header's description of it.

Tokens: I. initialize, P. parseArgs, C. cleanup, f. preTranslate consumed;
then i init, s start, x signalStop, j join, d destructor, and the service id.

The same binary also covers `win/core/thread.h`, which is libcore's and not
App's -- but it is what App's services start, and a second check binary for
one class would be four more hand-kept lists for no more coverage.
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
    assert len(verdicts) == 16, sorted(verdicts)


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


# --- core/thread.h ---------------------------------------------------------
#
# Every thread in the tree started life as a bare _beginthreadex with 0 for
# the stack, which takes the image default: MEASURED at 1 MB reserve and
# 64 KB commit per thread. gremote has six of them on a 32 MB machine, so the
# size became a parameter somebody chooses (docs/gremote-plan.md).

def test_a_thread_runs_its_body_and_is_joined(verdicts):
    assert verdicts["thread-runs-and-joins"][0] == "pass"


def test_a_small_stack_is_usable_at_the_depth_the_gate_allows(verdicts):
    """16 KB against the image's 64 KB commit, with four 1 KB frames actually
    used on it. `make check` caps any single frame below a page, so what a
    chosen size still has to cover is the depth."""
    assert verdicts["thread-takes-a-small-stack"][0] == "pass"


def test_joining_a_thread_that_never_started_is_not_an_error(verdicts):
    """A service whose start() failed is still signalled and joined."""
    assert verdicts["join-without-start-is-true"][0] == "pass"


def test_a_thread_starts_once(verdicts):
    assert verdicts["a-thread-starts-once"][0] == "pass"


def test_join_admits_a_thread_is_still_running(verdicts):
    """The answer joinOrLeak turns into a leak instead of a free under a live
    thread -- so join() saying true when it timed out would be the bug that
    rule exists to prevent."""
    assert verdicts["join-reports-a-thread-still-running"][0] == "pass"
