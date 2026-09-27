"""libwire's Connection and Listener, end to end, as the shipping code under Wine.

`win/checks/wirecheck.cpp` connects a Connection to another one adopted from a
Listener over loopback, in one process, and prints a verdict per case. The
server half -- adopt(), the idle timeout that is gremote's heartbeat, the
listener -- has no other user until gremote exists (docs/REMOTE.md step 4),
so this is where it is proved first. The client half is the same code the
Gaston client runs, which netcheck and rpccheck also cover.
"""

import conftest
import pytest

EXE_NAME = "wirecheck.exe"

pytestmark = conftest.needs_guest


@pytest.fixture(scope="module")
def verdicts():
    return conftest.guest_verdicts(EXE_NAME, timeout=90)


def test_every_case_passes(verdicts):
    bad = {k: v for k, v in verdicts.items() if v[0] != "pass"}
    assert not bad, bad


def test_every_case_ran(verdicts):
    assert len(verdicts) == 20, sorted(verdicts)


def test_a_listening_port_is_never_shared(verdicts):
    """No SO_REUSEADDR: on Winsock it lets a second process bind a port that
    is still listening, and which one then gets a connection is undefined."""
    assert verdicts["no-port-sharing"][0] == "pass"


def test_a_silent_peer_is_dropped_and_a_talking_one_is_kept(verdicts):
    """The heartbeat. A peer that died without a FIN would otherwise hold the
    link until TCP keepalive, about two hours."""
    assert verdicts["idle-peer-dropped"][0] == "pass"
    assert verdicts["heartbeat-keeps-it"][0] == "pass"


def test_every_pair_releases_everything(verdicts):
    """A join that timed out leaks the socket, the lock and the events on
    purpose; in a clean close nothing may take that path."""
    for case in ("both-stop-clean", "client-hears-the-drop",
                 "heartbeat-pair-stops-clean", "refusing-pair-stops-clean"):
        assert verdicts[case][0] == "pass", (case, verdicts[case])


# --- the out-queue reserve -------------------------------------------------
#
# gremote's control lane answers on the reader thread (docs/REMOTE.md), so a
# streaming GET and the PONG that keeps the peer from declaring the server
# dead share one 64KB queue. Before RC_OUTQ_RESERVE, whichever got there
# first could take all of it -- the server would have starved its own
# heartbeat with its own reply.

def test_bulk_is_refused_before_the_queue_is_full(verdicts):
    """Measured, not asserted as a boolean: the detail says how many frames
    of what size fitted, against which reserve and which total."""
    status, detail = verdicts["reserve-refuses-bulk-before-the-queue-is-full"]
    assert status == "pass", detail
    assert "reserve 8192 of 65536" in detail, detail


def test_the_reserve_is_there_for_the_control_reply(verdicts):
    """The property the constant exists for: with bulk shut out of the
    queue, a control reply still goes."""
    assert verdicts["the-reserve-is-there-for-the-control-reply"][0] == "pass"


def test_a_send_with_no_reserve_still_uses_the_whole_queue(verdicts):
    """Which is every caller in win/net -- the client passes no reserve, so
    its behaviour is unchanged by the parameter existing."""
    status, detail = verdicts["no-reserve-still-fills-the-whole-queue"]
    assert status == "pass", detail
