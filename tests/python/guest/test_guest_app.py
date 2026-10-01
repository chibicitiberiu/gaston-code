"""App's lifecycle, run as the shipping code under Wine.

`win/core/app.cpp` runs a fixed sequence of hooks -- initialize, parseArgs,
initializeServices, startServices, main, stopServices, cleanup -- that a
derived app fills, holding its services as UniqueObj members it creates and
takes down itself. Its ordering promises are the kind that read right and
behave otherwise, so `win/checks/appcheck.cpp` records every call into a
trace and these assert on the trace, not on the header's description of it.

Tokens: I. initialize, P. parseArgs, C. cleanup, f. preTranslate consumed;
then i init, s start, x signalStop, j join, d destructor, and the service id.

The same binary also covers `win/core/thread.h` and `win/core/log.h`, which are
libcore's and not App's -- but Thread is what App's services start, and the log
is where App's shutdown trace ends up, so both are read back here. A second
check binary for either would be four more hand-kept lists for no more
coverage.
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
    assert len(verdicts) == 31, sorted(verdicts)


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


def test_windows_ending_the_session_signals_every_service_once(verdicts):
    """Signalled once, and NOT joined. Windows holds its whole shutdown on
    App's window procedure returning, so the session-ending path signals and
    leaves; `jA` appearing here would be the hang back."""
    trace = verdicts["end-session-stops-once"][1]
    assert trace.count("xA") == 1 and "jA" not in trace


def test_ending_the_session_does_not_wait_for_a_stuck_service(verdicts):
    """The regression a real Pentium 200 paid for on 2026-10-01: it hung
    during shutdown with gremote running, because WM_ENDSESSION joined the
    services from inside the window procedure. With the reboot arriving over
    gremote's own connection that is also a deadlock -- the handler waits on
    the accept thread, which waits on the reader thread, which is inside
    ExitWindowsEx SendMessaging this very window. The detail column carries
    the measured duration; against the joining code it was the full budget."""
    verdict, detail = verdicts["end-session-does-not-wait-for-a-stuck-service"]
    assert verdict == "pass", detail


def test_a_session_end_we_asked_for_does_not_quit_the_loop(verdicts):
    """The second half of the same 2026-10-01 measurement, and the one not
    waiting did not cover. On 9x the process that calls ExitWindowsEx DRIVES
    the shutdown -- the call never returns, the broadcast to every other
    application runs out of it, and the 16-bit USER code it is executing holds
    Win16Mutex between the sends. So App tearing down promptly and then
    exiting killed that driver a second later, after Explorer had gone and
    before Systray was dealt with: a bare teal desktop, one unanswering task,
    an idle CPU, and no reboot. Four runs out of four on a clean Windows 98
    guest, against two builds; the same guest rebooted in three seconds when a
    rundll32 it had launched made the call instead, because rundll32 stays
    parked inside ExitWindowsEx until the machine goes
    (docs/measurements/gremote-shutdown-vm-20261001.txt).

    Wine has no 9x shutdown and no Win16Mutex, so it cannot see any of that.
    What it CAN hold is the decision: the services are signalled and abandoned
    exactly as on any session end, and the message loop is still running when
    the handler returns. The exit code says who ended it -- 11 is the thread
    playing Windows, 0 would be App quitting by itself."""
    verdict, detail = \
        verdicts["a-session-end-we-asked-for-does-not-quit-the-loop"]
    assert verdict == "pass", detail


def test_an_ordinary_quit_still_joins_and_waits(verdicts):
    """The other half, and why the answer is not simply "never join". After
    the message loop the process goes on living, so an orphan thread is a
    real bug: the ordinary path waits out its budget and then leaks rather
    than freeing. Only the session-ending path is allowed to be in a hurry."""
    verdict, detail = verdicts["an-ordinary-quit-still-joins-and-waits"]
    assert verdict == "pass", detail


def test_the_app_itself_is_leaked_on_a_session_end(verdicts):
    """The abandoned services' threads hold pointers into the App's members
    -- gremote's lanes use a Processes that lives in GremoteApp -- so running
    its destructor under them is a fault raised on a desktop that has already
    gone. runApp leaks it there and keeps it on an ordinary quit."""
    assert verdicts["run-app-leaks-the-app-on-a-session-end"][0] == "pass"
    assert verdicts["run-app-keeps-the-app-on-an-ordinary-quit"][0] == "pass"


def test_the_hidden_window_is_top_level(verdicts):
    """It has to be, or none of the above ever runs: USER broadcasts
    WM_QUERYENDSESSION and WM_ENDSESSION to top-level windows, and App's is a
    hidden zero-size WS_POPUP because HWND_MESSAGE is Win2000+. EnumWindows
    walks that same set, so finding it there is the confirmation."""
    assert verdicts["app-window-is-top-level"][0] == "pass"


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


# --- core/log.h ------------------------------------------------------------
#
# The log used to delete the evidence. `rc_log_open` opened with CREATE_ALWAYS,
# and the first act of recovering a hung Windows 98 box is a forced restart --
# so the lines written on the way down, the only record of the hang, were
# destroyed by the act of recovering from it. Measured on the user's real
# Pentium: it hung during shutdown twice, and GREMOTE.LOG held only the run
# that had just started. rc_log_open now renames <program>.log to
# <program>.prv before it truncates anything.
#
# appcheck is where these live because it is the binary that already links
# libcore alone and runs headless under Wine, and because the second half of
# the same fix -- what App's session-end handler writes -- is this file's
# subject anyway.

def test_the_previous_run_survives_the_next_start(verdicts):
    """One generation, kept by renaming rather than copying: a rename inside
    one directory moves a directory entry and needs no free space, which
    matters because a full disk is one of the states this has to survive."""
    assert verdicts["log-keeps-the-previous-run"][0] == "pass"


def test_a_first_run_leaves_no_previous_log(verdicts):
    """No empty .prv pretending there was a run before this one."""
    assert verdicts["log-first-run-keeps-nothing"][0] == "pass"


def test_the_rotated_name_has_an_8_3_spelling(verdicts):
    """`GREMOTE.PRV` does and `GREMOTE.LOG.OLD` does not, and on FAT a name
    with no 8.3 form is a DIFFERENT file seen from DOS or from the installer
    -- a trap this project has already paid for once with an INI file."""
    assert verdicts["log-rotation-name-has-an-8-3-spelling"][0] == "pass"


def test_each_program_rotates_its_own_log(verdicts):
    """gremote.exe sits beside gaston.exe and gupdate.exe in one directory on
    the test box, so a shared .prv would have one of them eating another's
    only record of a shutdown."""
    assert verdicts["log-rotation-is-per-program"][0] == "pass"


def test_a_rotation_that_fails_still_logs(verdicts):
    """The load-bearing half. A diagnostic feature that can PREVENT logging is
    worse than the bug it fixes, so the check holds the .prv open with no
    sharing -- a read-only directory, or a file the dying copy still has --
    and asks for the current run anyway. It gets it, plus a line saying the
    previous one could not be kept."""
    assert verdicts["log-survives-a-failed-rotation"][0] == "pass"


def test_the_session_end_handler_is_traced_from_entry_to_exit(verdicts):
    """Four markers in the file, each settling one reading of a hang:

    no entry line        -- WM_ENDSESSION never arrived, and the
                            WM_QUERYENDSESSION line says whether the
                            broadcast reached us at all;
    entry and no more    -- it faulted while signalling, with the
                            abandoned-<service> lines saying how far it got;
    "cleaning up"        -- signalling finished, so a fault after this one is
                            in cleanup();
    "returning ... N ms" -- the fast path ran to the end, and N is the number
                            the whole path exists to keep small.

    The last of those is written AFTER cleanup() closed the log, by reopening
    it (`rc_log_tail`). Without it the exiting banner is the final line on a
    clean return and on a fault on the way out alike."""
    assert verdicts["end-session-writes-entry-through-exit-to-the-log"][0] \
        == "pass"


def test_the_log_says_when_the_process_is_staying_up(verdicts):
    """The closing line used to end "nothing below this line is ours", which
    on the staying-up path is false -- the watchdog writes under it fifteen
    seconds later if the machine is somehow still there. That line is the
    first thing a person reads after a forced restart, so it has to name
    which of the two endings the handler took."""
    assert verdicts["the-log-says-when-the-process-is-staying-up"][0] == "pass"


def test_the_log_says_whether_it_was_a_logoff(verdicts):
    """`ENDSESSION_LOGOFF` in the lParam, which the handler used to throw
    away. "gremote outlives a logoff but not a shutdown" is a claim about
    behaviour, and a log that does not say which one it saw cannot confirm
    or deny it (docs/REMOTE.md, P200 checklist)."""
    assert verdicts["end-session-says-whether-it-was-a-logoff"][0] == "pass"


def test_an_ordinary_quit_writes_none_of_those_lines(verdicts):
    """A line that appears on both paths settles nothing."""
    assert verdicts["an-ordinary-quit-writes-no-session-lines"][0] == "pass"
