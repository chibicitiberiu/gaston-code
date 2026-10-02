"""gremote.exe listening under Wine, with pytest as the caller.

**This is the direction docs/REMOTE.md said had no precedent here**, and the
thing step 4 asks to be proved before anything is built on it: "first prove
Wine can host a listening exe that pytest connects to -- every existing Wine
test runs the exe as a client". `wirecheck` does prove libwire's server half
under Wine, but with both ends inside one Wine process; `test_guest_net.py`
crosses the boundary in the other direction, with Python listening and Wine
dialling out. Nothing had connected INTO a socket held by a Wine process.

So these run the real `gremote.exe`, over a real socket, through the real
`gremote` package. Wine proves the code and never the platform -- the guest
run is still the gate for FAT, MAX_PATH and modal dialogs -- but the
handshake, the refusals and the frame grammar are code, and this is where
they are settled.
"""

import socket

import conftest
import pytest

from gremote import Disconnected, Refused, Remote

pytestmark = conftest.needs_guest

SECRET = "not-the-default"


def ini(port: int, *, secret: str = SECRET, allow: str = "",
        heartbeat: int = 30, ui: str = "") -> str:
    return (
        "[server]\n"
        "bind=127.0.0.1\n"
        f"port={port}\n"
        f"secret={secret}\n"
        f"allow={allow}\n"
        f"heartbeat_timeout_s={heartbeat}\n"
        + (f"[ui]\n{ui}\n" if ui else "")
    )


@pytest.fixture
def server(tmp_path):
    port = conftest.free_port()
    with conftest.GuestServer("gremote.exe", tmp_path, ini(port), port) as s:
        yield s


def test_the_handshake_answers_with_hello(server):
    with Remote("127.0.0.1", SECRET, port=server.port) as r:
        assert r.hello["ver"] == "1", server.log()
        # Load-bearing, not decoration: the caller decodes exec output with
        # oemcp and filenames with acp, so a HELLO without them would leave
        # every byte from the machine guessed at.
        assert r.hello["acp"].isdigit(), r.hello
        assert r.hello["oemcp"].isdigit(), r.hello
        assert r.hello["os"].startswith("Windows "), r.hello
        # v0 has no ui lane and no compression, and says so rather than
        # leaving the caller to find out.
        assert r.hello["msaa"] == "0"
        assert r.hello["deflate"] == "0"


def test_ping_is_answered(server):
    with Remote("127.0.0.1", SECRET, port=server.port) as r:
        r.ping()


def test_an_unknown_op_is_answered_not_dropped(server):
    """Silence would cost the caller its whole budget and tell it nothing --
    the same duty `reject_busy` has on the client."""
    with Remote("127.0.0.1", SECRET, port=server.port) as r:
        with pytest.raises(Refused) as e:
            r.request("NOSUCHOP")
        assert e.value.code == "unknown_op"


def test_a_wrong_secret_is_refused_and_the_link_is_dropped(server):
    with pytest.raises((Refused, Disconnected)) as e:
        Remote("127.0.0.1", "wrong", port=server.port).connect()
    if isinstance(e.value, Refused):
        assert e.value.code == "bad_secret"


def test_a_frame_before_auth_is_refused(server):
    """Nothing but AUTH gets through first, and the refusal says so."""
    from bridge.wire.frame import Frame, FrameReader

    s = socket.create_connection(("127.0.0.1", server.port), 10)
    try:
        s.sendall(Frame("PING", {"id": "x"}).encode())
        reader = FrameReader(Frame)
        frames = []
        while not frames:
            frames = reader.feed(s.recv(65536))
        assert frames[0].type == "ERR", frames[0]
        assert frames[0].headers.get("code") == "not_authed", frames[0].headers
    finally:
        s.close()


def test_a_second_client_is_answered_busy_and_the_first_is_undisturbed(server):
    """One live client at a time. The refusal must not cost the caller that
    already has the machine -- that is the whole difference between "busy"
    and "broken"."""
    from bridge.wire.frame import Frame, FrameReader

    with Remote("127.0.0.1", SECRET, port=server.port) as first:
        second = socket.create_connection(("127.0.0.1", server.port), 10)
        try:
            reader = FrameReader(Frame)
            frames = []
            while not frames:
                data = second.recv(65536)
                assert data, "the second client was closed without an answer"
                frames = reader.feed(data)
            assert frames[0].type == "ERR", frames[0]
            assert frames[0].headers.get("code") == "busy", frames[0].headers
            assert second.recv(1) == b"", "busy should be followed by a close"
        finally:
            second.close()
        # And the machine is still talking to the client that had it.
        first.ping()


def test_a_peer_outside_allow_never_reaches_the_handshake(tmp_path):
    """The range is checked on the accepted socket, before AUTH: a machine
    outside it never gets to guess at the secret, and never holds the one
    client slot while it tries."""
    port = conftest.free_port()
    # 10.0.0.0/8 does not contain 127.0.0.1.
    body = ini(port, allow="10.0.0.0/8")
    with conftest.GuestServer("gremote.exe", tmp_path, body, port) as s:
        sock = socket.create_connection(("127.0.0.1", port), 10)
        try:
            sock.settimeout(10)
            assert sock.recv(1) == b"", (
                "a peer outside allow= was let in\n" + s.log())
        finally:
            sock.close()


def test_a_shipped_default_secret_refuses_to_start(tmp_path):
    """A machine nobody has configured must not come up listening with a
    secret that is written in a document. It refuses, and says which setting
    -- and it says it in the log rather than a dialog, because a modal on an
    unattended box holds the machine until somebody walks over."""
    port = conftest.free_port()
    staged = conftest.GuestServer("gremote.exe", tmp_path,
                                  ini(port, secret="change-me"), port)
    code, log = staged.refusal()
    assert code != 0, log
    assert "secret" in log, log


def test_an_unreadable_setting_refuses_to_start(tmp_path):
    """The rule the whole config follows: a value that is present and wrong
    refuses startup naming itself, rather than being quietly rounded into
    something that half works."""
    port = conftest.free_port()
    staged = conftest.GuestServer("gremote.exe", tmp_path,
                                  ini(port, allow="not-an-address"), port)
    code, log = staged.refusal()
    assert code != 0, log
    assert "allow" in log, log


def test_an_unknown_automation_level_refuses_to_start(tmp_path):
    """0, 1 and 2 and nothing else, REFUSED rather than clamped.

    The handshake follows this rule for every capability and the reason is
    the same here: a level this build does not know means the file knows
    something the program does not, and both guesses are wrong. Rounding down
    throws away a capability somebody asked for; rounding up grants one they
    may not have meant, on the one program that can move somebody's mouse."""
    port = conftest.free_port()
    staged = conftest.GuestServer("gremote.exe", tmp_path,
                                  ini(port, ui="automation=3"), port)
    code, log = staged.refusal()
    assert code != 0, log
    assert "automation" in log, log


def test_a_step_longer_than_the_whole_list_refuses_to_start(tmp_path):
    """A pair of settings that cannot both be meant.

    No single getter can catch it: each number is in range on its own. A step
    ceiling above the list ceiling describes a step no step could ever use,
    which is the kind of thing that looks like it worked until a long list
    ends early and nothing says why."""
    port = conftest.free_port()
    staged = conftest.GuestServer(
        "gremote.exe", tmp_path,
        ini(port, ui="script_ms=5000\nstep_ms=60000"), port)
    code, log = staged.refusal()
    assert code != 0, log
    assert "step_ms" in log, log


def test_an_unknown_ui_key_refuses_to_start(tmp_path):
    """checkKeys, so a typo is an error rather than a silent default.

    `automaton=2` would otherwise leave the shipped default in place and look
    exactly like a setting that had been applied."""
    port = conftest.free_port()
    staged = conftest.GuestServer("gremote.exe", tmp_path,
                                  ini(port, ui="automaton=2"), port)
    code, log = staged.refusal()
    assert code != 0, log
    assert "automaton" in log or "ui" in log, log
