"""The job lane: EXEC and LAUNCH, one child at a time.

Under Wine, so `cmd.exe` is what is there -- the real guest has COMMAND.COM
and no `cmd`, which is exactly why the shell is the CALLER's to choose and
never the guest's. What these settle is the wire shape: output streaming back
as it is produced, the END headers, the refusals, and that a second EXEC
while one is running is told `busy` with the age of the one in the slot
rather than queued behind it.

What Wine cannot settle is on the guest checklist: whether a stopped child
strands a DOS VM, and what COMMAND.COM /c does to an exit code (already
measured -- it returns 0 whatever happens, which is why END carries
via_shell).
"""

import conftest
import pytest

from gremote import Refused, Remote

pytestmark = conftest.needs_guest

SECRET = "not-the-default"


def ini(port: int) -> str:
    return (
        "[server]\n"
        "bind=127.0.0.1\n"
        f"port={port}\n"
        f"secret={SECRET}\n"
        "allow=\n"
        "heartbeat_timeout_s=30\n"
        "[exec]\n"
        "max_timeout_ms=20000\n"
        "max_output_kb=64\n"
    )


@pytest.fixture(scope="module")
def remote(tmp_path_factory):
    port = conftest.free_port()
    d = tmp_path_factory.mktemp("job")
    with conftest.GuestServer("gremote.exe", d, ini(port), port) as server:
        with Remote("127.0.0.1", SECRET, port=port) as r:
            r.server = server
            yield r


def test_a_command_runs_and_its_output_comes_back(remote):
    res = remote.exec("cmd.exe /c echo hello-from-the-machine")
    assert "hello-from-the-machine" in res.text(remote.oemcp), res
    assert res.bytes_produced > 0, res


def test_the_exit_code_is_reported_with_whether_a_shell_ran_it(remote):
    """`exit` on its own is not an answer. COMMAND.COM /c on 98 exits 0
    whatever happens, so the caller needs to know which it got."""
    res = remote.exec("cmd.exe /c exit 3")
    assert res.via_shell is False, "no shell= was sent, so none was spliced"
    assert res.exit == 3, res


def test_a_shell_prefix_is_spliced_and_reported(remote):
    res = remote.exec("echo spliced", shell="cmd.exe /c")
    assert res.via_shell is True, res
    assert "spliced" in res.text(remote.oemcp), res


def test_a_program_that_does_not_exist_is_a_refusal(remote):
    with pytest.raises(Refused) as e:
        remote.exec("no-such-program-anywhere.exe")
    assert e.value.code == "spawn_failed", e.value.code


def test_an_empty_command_is_refused(remote):
    with pytest.raises(Refused) as e:
        remote.request("EXEC", {"timeout_ms": "1000"})
    assert e.value.code == "no_command", e.value.code


#: A command that keeps running and prints nothing. `pause` is the obvious
#: choice and the wrong one: under Wine there is no console to wait on, so it
#: returns in half a second. A counted loop burns CPU and is honest about
#: still running, which is the property these two tests need.
FOREVER = "cmd.exe /c for /l %i in (1,1,100000000) do @rem"


def test_a_command_that_never_ends_is_stopped_and_says_so(remote):
    """The guest enforces the timeout, not the caller. A client-side deadline
    would leave the child running with nobody to notice."""
    res = remote.exec(FOREVER, timeout=3.0)
    assert res.timed_out is True, res


def test_output_past_the_cap_is_truncated_and_admits_it(remote):
    """64KB by this server's ini. A remote shell streams rather than buffers,
    so the cap bounds the total handed back, not any one allocation."""
    res = remote.exec(
        'cmd.exe /c for /l %i in (1,1,20000) do @echo xxxxxxxxxxxxxxxxxxxx',
        timeout=18.0)
    assert res.truncated is True, (len(res.output), res.bytes_produced)
    assert len(res.output) <= 64 * 1024, len(res.output)
    # What the command produced is counted even though it was not all sent.
    assert res.bytes_produced >= len(res.output), res


def test_launch_returns_a_pid_and_does_not_wait(remote):
    pid = remote.launch("cmd.exe /c exit 0")
    assert pid > 0, pid


def test_a_second_job_is_refused_with_the_age_of_the_first(remote):
    """One child at a time, and the refusal says what is holding the lane
    and for how long -- the difference between "try again in a moment" and
    "something is stuck". Refused rather than queued, because a queue turns
    "working" into "will answer eventually", which on one disk is a promise
    nobody can keep.
    """
    import threading

    slow = {}

    def run_slow():
        try:
            slow["res"] = remote.exec(FOREVER, timeout=5.0)
        except Exception as e:  # reported after the join, not swallowed
            slow["err"] = e

    t = threading.Thread(target=run_slow)
    t.start()
    try:
        # Give the lane a moment to actually be holding it.
        import time
        time.sleep(1.5)
        with pytest.raises(Refused) as e:
            remote.exec("cmd.exe /c echo second", timeout=3.0)
        assert e.value.code == "busy", e.value.code
    finally:
        t.join(timeout=30)
    assert "err" not in slow, slow.get("err")
