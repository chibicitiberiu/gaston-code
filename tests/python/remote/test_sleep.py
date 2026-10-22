"""Prevent sleep: the option that stops a Windows 98 box dropping the link.

A real Pentium 200 with APM goes into standby on its idle timer, the network
goes with it, and every connection gremote was holding dies. `[machine]
prevent_sleep=1` in gremote.ini -- or the checked Prevent sleep item on the tray
menu -- refuses that. `win/remote/tray.h` has the four mechanisms and what is
and is not documented about each of them on 9x.

WHAT THIS SUITE CAN AND CANNOT SETTLE, because the distinction is the whole
reason the probe exists.

It CANNOT ask whether Windows 98 sends a suspend query when its standby timer
expires. That is the question the option turns on, and no amount of Wine or QEMU
answers it: the QEMU guest has NO APM AT ALL, which is why `REBOOT
mode=poweroff` there runs the whole teardown, cannot switch the machine off, and
comes back as a logoff (research/measurements/gremote-shutdown-fix-vm-20261001.txt).
With no APM there is no power driver to send anything. Only the user's real
machine, left idle, can answer it, and gremote logs the query EVEN WHEN THE
OPTION IS OFF so that measurement does not change the machine's behaviour.

It CAN settle everything that is code rather than platform, by SYNTHESISING the
query that the guest's hardware will never send. `probe power` sends both veto
messages at gremote's own hidden tray window from another process and prints
what came back, so these cases drive the real shipping window procedure in the
real shipping binary:

  * the refusal really is BROADCAST_QUERY_DENY and PWR_FAIL, not a lookalike;
  * the same window GRANTS the suspend when the option is off, which is what
    makes "default off" a behaviour and not a comment;
  * the default IS off, so an install that predates the option is unchanged;
  * the ini turns it on, and the log says so and names which mechanisms are
    running -- an invisible mode that changes how the hardware behaves is the
    complaint that got the tray icon built in the first place;
  * a typo or a non-boolean in [machine] refuses startup rather than quietly
    leaving the machine asleep-able, which is IniFile's rule everywhere else.

`probe power` also prints whether this KERNEL32 exports SetThreadExecutionState,
which no document settled either way for 98. Under Wine it is present, so these
cases must not assert on its absence -- that is a line to read on the guest, not
a thing to pin here.

ONE THING DELIBERATELY NOT PINNED HERE: the line the option writes when it goes
OFF again as gremote stops. `GuestServer` stops the guest by terminating the
process, and under Wine that takes it without a message loop teardown -- the log
ends without even libcore's own exiting banner, so `signalStop()` never runs and
there is nothing for a case to read. It is reachable on both of the paths that
matter on the real machine (tray > Exit, and a Windows shutdown, which runs
`signalStop()` on every service from inside the WM_ENDSESSION handler), so it
belongs on the P200 checklist in docs/REMOTE.md rather than in a case here that
could only pass by pretending.
"""

import conftest
import pytest

pytestmark = conftest.needs_guest

SECRET = "not-the-default"


def ini(port: int, machine: str = "") -> str:
    return (
        "[server]\n"
        "bind=127.0.0.1\n"
        f"port={port}\n"
        f"secret={SECRET}\n"
        "allow=\n"
        "heartbeat_timeout_s=30\n"
    ) + machine


def ask_to_suspend(workdir) -> str:
    """`probe power`: send both veto messages at the running gremote's window
    from another process, and hand back what it printed.

    A separate process on purpose. The messages are SENT, so the reply is the
    window procedure's return value, and a program asking itself would prove
    nothing about the message ever crossing a process boundary -- which on 9x is
    how the power driver delivers it.
    """
    out = conftest.run_guest("probe.exe", "power", cwd=workdir, timeout=60)
    text = out.stdout.decode("latin-1", "replace")
    assert "gremote window          : found" in text, (
        f"probe could not find gremote's window, so nothing below it means "
        f"anything:\n{text}")
    return text


@pytest.fixture
def awake(tmp_path):
    """gremote with prevent_sleep=1 from the ini."""
    port = conftest.free_port()
    body = ini(port, "\n[machine]\nprevent_sleep=1\n")
    with conftest.GuestServer("gremote.exe", tmp_path, body, port) as server:
        yield server


@pytest.fixture
def default(tmp_path):
    """gremote with no [machine] section at all -- an existing install.

    `ini()` writes [server] and [exec] and stops, which is exactly the file
    sitting on the real machine. The fixture entering at all is the assertion
    that matters: GuestServer waits for `server: listening on` in the log and
    fails the case if the process exits first."""
    port = conftest.free_port()
    with conftest.GuestServer("gremote.exe", tmp_path, ini(port),
                              port) as server:
        yield server


# --- the veto ---------------------------------------------------------------

def test_a_suspend_request_is_refused_when_prevent_sleep_is_on(awake):
    """The answer, and the exact answer. BROADCAST_QUERY_DENY is 0x424D5144 and
    PWR_FAIL is -1; anything else -- including a plain FALSE, which looks like a
    refusal and is not -- lets the machine suspend. Both messages are answered
    because which of them Windows 98 really sends is not documented anywhere,
    and WM_POWER is the one whose delivery set Microsoft DOES document (each
    window EnumWindows returns, which appcheck proves ours is in)."""
    text = ask_to_suspend(awake.dir)
    assert "WM_POWERBROADCAST reply : 424d5144" in text, text
    assert "WM_POWER reply          : ffffffff" in text, text


def test_both_messages_get_their_own_log_line(awake):
    """Not decoration, and the reason the rate limit is per MESSAGE rather than
    per refusal. On the real machine this log is the only evidence of which of
    the four mechanisms did anything -- and, because nothing documents whether
    Windows 98 sends WM_POWERBROADCAST or the obsolete WM_POWER, which message
    arrived is half the answer. A single counter would rate-limit the first
    WM_POWER away and lose it."""
    ask_to_suspend(awake.dir)
    log = awake.log()
    assert "(WM_POWERBROADCAST); REFUSED (1 so far)" in log, log
    assert "(WM_POWER); REFUSED (2 so far)" in log, log


def test_the_same_window_grants_a_suspend_when_the_option_is_off(default):
    """The half that makes "default off" a behaviour rather than a comment: the
    window procedure is there either way, so the only thing being tested by the
    case above is the option, not the plumbing."""
    text = ask_to_suspend(default.dir)
    assert "424d5144" not in text.lower(), text
    assert "not a denial, so Prevent sleep is off" in text, text
    assert "not a refusal, so Prevent sleep is off" in text, text


def test_a_granted_suspend_is_logged_too(default):
    """THE LINE THE WHOLE MEASUREMENT RESTS ON. Leaving a real box idle with
    prevent_sleep=0 and finding this in GREMOTE.LOG is what proves an
    idle-timeout standby asks permission at all; finding nothing proves no veto
    could ever have helped. So it has to be written on the path where the answer
    is yes, which is the path nobody would think to log."""
    ask_to_suspend(default.dir)
    log = default.log()
    assert "Prevent sleep is off, so yes" in log, log
    assert "REFUSED" not in log, log


# --- the option -------------------------------------------------------------

def test_an_ini_with_no_machine_section_starts_and_takes_the_default(default):
    """THE REGRESSION A ROLLED-BACK DEPLOYMENT PAID FOR, 2026-10-01.

    Shipping this option refused to start on the real Pentium 200, because its
    installed gremote.ini has [server] and [exec] and no [machine] at all -- the
    ordinary state of every install that updates. `IniFile::has()` answered TRUE
    for the absent key (it probed with a sentinel containing control characters,
    and 9x does not hand those back unaltered), so the key read as the empty
    string and refused startup as `[machine] prevent_sleep: must be 0 or 1`.
    gupdate rolled the image back, which is the only reason the box kept a
    server.

    ABSENT IS NOT INVALID. The strict 0/1 check below stays; it must simply not
    fire on a key that is not there.

    HONEST ABOUT WHAT THIS CASE IS WORTH: it passed before the fix as well,
    because Wine returns the sentinel intact and so the bug does not exist here.
    It is pinned anyway -- the shape is now named in the suite, so a future
    reader knows it is load-bearing rather than incidental -- but the case that
    actually fails against the broken code is in
    tests/python/guest/test_guest_inifile.py, and the only place this one is
    real is the guest."""
    log = default.log()
    assert "server: listening on" in log, log
    assert "Prevent sleep ON" not in log, log
    assert "[awake]" not in log, log
    assert "must be 0 or 1" not in log, log


def test_the_ini_turns_it_on_and_says_which_mechanisms_are_running(awake):
    """An unattended box has to be able to have this on from boot, and the log
    has to name what is actually running -- there are four mechanisms and only
    two of them are documented for 9x, so "it stayed awake" is worthless
    without knowing which one to credit."""
    log = awake.log()
    assert "Prevent sleep ON (gremote.ini)" in log, log
    assert "refusing suspend requests" in log, log
    # Under Wine SetThreadExecutionState exists, so the nudge is not used. On
    # Win98 it is the other branch. Either is a named mechanism; a line naming
    # neither would be the failure.
    assert ("SetThreadExecutionState is holding the system awake" in log or
            "nudging the input queue" in log), log


# --- refusals ---------------------------------------------------------------

def test_a_typo_in_the_machine_section_refuses_startup(tmp_path):
    """IniFile's rule everywhere else, and it matters more here than usual:
    `prevent_sleeep=1` would otherwise leave the box able to suspend while its
    owner believes they turned that off, and the symptom is a dropped
    connection hours later."""
    port = conftest.free_port()
    body = ini(port, "\n[machine]\nprevent_sleeep=1\n")
    server = conftest.GuestServer("gremote.exe", tmp_path, body, port)
    code, log = server.refusal()
    assert code != 0, log
    assert "prevent_sleeep" in log, log


def test_a_value_that_is_not_a_boolean_refuses_startup(tmp_path):
    """Never guess. `prevent_sleep=yes` is somebody's reasonable expectation and
    it is not what IniFile reads, so it stops the machine rather than being
    silently read as off."""
    port = conftest.free_port()
    body = ini(port, "\n[machine]\nprevent_sleep=yes\n")
    server = conftest.GuestServer("gremote.exe", tmp_path, body, port)
    code, log = server.refusal()
    assert code != 0, log
    assert "prevent_sleep" in log, log
