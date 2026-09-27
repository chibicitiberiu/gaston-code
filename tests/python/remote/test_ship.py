"""ship(): end the running build, write the new one, start it again.

This is REMOTE.md step 5b, and the reason the fs lane was folded into step 4
-- without PUT, a new gaston.exe still travels to the test box on a CD.

Two traps are encoded here rather than remembered, both of which have cost
real time on this project: a running exe is LOCKED, and the old install
reported success while the guest kept running the previous binary; and a
second copy of the client raises the first and exits, so launching over a
live one looks like a successful start and changes nothing.

Under Wine the thing being shipped is a small exe of our own -- capscheck,
which takes no arguments and exits -- because what is being tested is the
sequence, not the program. Shipping the real client at the real client is the
guest's job.
"""

import conftest
import pytest

from gremote import Refused, Remote, RemoteError

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
    port = conftest.free_port()
    d = tmp_path_factory.mktemp("ship")
    with conftest.GuestServer("gremote.exe", d, ini(port), port) as server:
        with Remote("127.0.0.1", SECRET, port=port) as r:
            r.server = server
            yield r


def test_ship_writes_the_file_and_starts_it(remote, tmp_path_factory):
    d = tmp_path_factory.mktemp("dest")
    src = conftest.guest_exe("capscheck.exe")
    dest = d / "shipped.exe"
    res = remote.ship(src, conftest.wine_path(dest))
    assert dest.exists(), remote.server.log()
    with open(src, "rb") as f:
        assert dest.read_bytes() == f.read()
    assert res.bytes_written == dest.stat().st_size
    assert res.pid > 0, res


def test_ship_without_relaunch_leaves_nothing_running(remote,
                                                      tmp_path_factory):
    d = tmp_path_factory.mktemp("norun")
    dest = d / "quiet.exe"
    res = remote.ship(conftest.guest_exe("capscheck.exe"),
                      conftest.wine_path(dest), relaunch=False)
    assert res.pid == 0
    assert dest.exists()


def test_shipping_over_a_running_program_ends_it_first(remote,
                                                       tmp_path_factory):
    """The trap this function exists for. The target here is a program that
    keeps running, so the write would be refused -- or worse, silently do
    nothing -- if it were not ended first.
    """
    d = tmp_path_factory.mktemp("busy")
    dest = d / "holder.exe"
    # A program that stays up: gremote itself, pointed at a port nothing
    # will connect to. Shipping over it must end it.
    with open(conftest.guest_exe("gremote.exe"), "rb") as f:
        dest.write_bytes(f.read())
    (d / "gremote.ini").write_text(ini(conftest.free_port()).replace("\n", "\r\n"))
    (d / "holder.ini").write_text(ini(conftest.free_port()).replace("\n", "\r\n"))
    remote.launch(conftest.wine_path(dest), cwd=conftest.wine_path(d))

    import time
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if any(p.exe.lower().endswith("holder.exe") for p in remote.ps()):
            break
        time.sleep(0.3)
    else:
        pytest.skip("the holder never appeared in the listing")

    res = remote.ship(conftest.guest_exe("capscheck.exe"),
                      conftest.wine_path(dest), relaunch=False)
    assert res.killed, "nothing was ended, so the write raced the lock"
    assert not any(p.exe.lower().endswith("holder.exe") for p in remote.ps())
    with open(conftest.guest_exe("capscheck.exe"), "rb") as f:
        assert dest.read_bytes() == f.read(), "the old image survived"


def test_a_short_write_is_caught_rather_than_reported_as_success(remote,
                                                                 tmp_path_factory):
    """The verification after the write. The guest checks the CRC before it
    renames, so this is the other half: that the file is where it was asked
    for, at the size that was sent. It catches a path that quietly went
    somewhere else.
    """
    d = tmp_path_factory.mktemp("verify")
    # A directory where the file should go: the put fails, and ship must
    # raise rather than return a happy ShipResult.
    (d / "taken").mkdir()
    with pytest.raises((Refused, RemoteError)):
        remote.ship(conftest.guest_exe("capscheck.exe"),
                    conftest.wine_path(d / "taken"))
