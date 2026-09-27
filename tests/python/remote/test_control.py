"""The lane that answers on the reader thread: SYSINFO, PS, KILL, REBOOT.

These are here rather than on a worker for one reason, and it is worth
stating because it looks like a performance choice and is not: on 9x a hung
16-bit application holding Win16Mutex blocks USER and GDI calls from every
32-bit thread. The reader carries the heartbeat, so it may touch neither --
and PS and KILL belong on it precisely because behind a stuck EXEC is when
you need to find something and end it.

Wine proves the code and never the platform. What runs here is the real
libexec -- Toolhelp resolved at runtime, the kill deny list -- against
whatever processes Wine happens to be running, which is enough to settle the
wire format, the refusals and the parsing. Whether killing MSGSRV32 hangs a
real 98 box is not a thing to find out under Wine, and the deny list exists
so nobody does.
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
    )


@pytest.fixture(scope="module")
def remote(tmp_path_factory):
    """One server and one connection for the whole module: starting Wine is
    the expensive part, and none of these ops leaves the machine changed."""
    port = conftest.free_port()
    d = tmp_path_factory.mktemp("control")
    with conftest.GuestServer("gremote.exe", d, ini(port), port) as server:
        with Remote("127.0.0.1", SECRET, port=port) as r:
            r.server = server
            yield r


def test_sysinfo_reports_memory_and_uptime(remote):
    info = remote.sysinfo()
    assert info.mem_kb > 0, info
    assert info.mem_free_kb > 0, info
    # GetTickCount on a machine that has been up long enough to accept a
    # connection. Zero would mean the header never arrived.
    assert info.uptime_ms > 0, info


def test_ps_lists_processes_including_gremote_itself(remote):
    procs = remote.ps()
    assert procs, remote.server.log()
    names = [p.exe.lower() for p in procs]
    assert any("gremote" in n for n in names), names


def test_a_pid_survives_the_round_trip_unsigned(remote):
    """On 9x a pid is an obfuscated pointer with the high bit set. It travels
    as hex for that reason, and a parser that read it as signed would hand
    back a negative pid that matches nothing."""
    procs = remote.ps()
    assert all(p.pid >= 0 for p in procs), [p.pid for p in procs]
    assert all(p.threads >= 0 for p in procs), procs


def test_killing_a_pid_that_is_not_there_says_so(remote):
    with pytest.raises(Refused) as e:
        remote.kill(0x7FFFFFFE)
    # no_such_pid is the honest answer; failed is what a platform that will
    # not say gives back. Either is a refusal, and neither is a crash.
    assert e.value.code in ("no_such_pid", "failed"), e.value.code


def test_killing_ourselves_is_denied(remote):
    """gremote's own pid is always on the deny list. Without it, the first
    thing a caller does with a process listing is end the program it is
    talking to."""
    mine = [p for p in remote.ps() if "gremote" in p.exe.lower()]
    assert mine, "gremote is not in its own listing"
    with pytest.raises(Refused) as e:
        remote.kill(mine[0].pid)
    assert e.value.code == "denied", e.value.code


def test_reboot_without_the_confirmation_is_refused(remote):
    """The only REBOOT test there can be. It ends every unsaved thing on the
    machine, so what is checked is that it does not happen by accident."""
    with pytest.raises(Refused) as e:
        remote.request("REBOOT")
    assert e.value.code == "unconfirmed", e.value.code


def test_an_unknown_mode_is_refused_rather_than_guessed(remote):
    with pytest.raises(Refused) as e:
        remote.request("REBOOT", {"confirm": "yes", "mode": "sideways"})
    assert e.value.code == "bad_mode", e.value.code
