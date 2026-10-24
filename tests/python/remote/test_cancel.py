"""CANCEL, on 9P's Tflush contract.

notes/BACKLOG.md 6 deferred this for years with a clear reason: it had no
caller, and "the hard half of Cancel is not the message, it is the race --
did the cancel beat the reply, and when may the id be reused? 9P specifies
that away, and the rule is worth copying verbatim."

So the promise being tested is not "the work stops". It is that **the
cancelled request is never answered**, whoever wins the race, and that its id
is free the moment the ack arrives. The guest keeps that with a mark rather
than a wait: an id marked cancelled has its replies dropped at the Responder,
so a handler already most of the way through its answer still produces no
frame. Waiting on the reader thread instead would have put the heartbeat
behind a Toolhelp walk.

Dropping the reply is also what stops the work -- the send reports failure,
ExecJob reads that as EXEC_SINK_GONE and ends the child, ChunkSource's pump
returns CONSUMER_STOPPED. That is the second half of what BACKLOG 6 said this
primitive was for.
"""

import threading
import time

import conftest
import pytest

from gremote import Cancelled, Refused, Remote

pytestmark = conftest.needs_guest

SECRET = "not-the-default"

#: Keeps running and prints nothing. `pause` returns at once under Wine.
FOREVER = "cmd.exe /c for /l %i in (1,1,100000000) do @rem"


def ini(port: int) -> str:
    return (
        "[server]\n"
        "bind=127.0.0.1\n"
        f"port={port}\n"
        f"secret={SECRET}\n"
        "allow=\n"
        "heartbeat_timeout_s=30\n"
        "[exec]\n"
        "max_timeout_ms=60000\n"
        "max_output_kb=64\n"
    )


@pytest.fixture(scope="module")
def remote(tmp_path_factory):
    port = conftest.free_port()
    d = tmp_path_factory.mktemp("cancel")
    with conftest.GuestServer("gremote.exe", d, ini(port), port) as server:
        with Remote("127.0.0.1", SECRET, port=port) as r:
            r.server = server
            yield r


def _run_in_background(remote, command, timeout=45.0):
    """Start an EXEC on another thread and hand back its id as soon as the
    client has one. There is no other way to name a call that is still in
    flight, which is the whole difficulty of cancelling one."""
    got_id: list[str] = []
    outcome: dict = {}
    ready = threading.Event()

    def note(ident):
        got_id.append(ident)
        ready.set()

    def body():
        try:
            outcome["res"] = remote.exec(command, timeout=timeout,
                                         on_id=note)
        except Exception as e:  # recorded, then asserted on
            outcome["err"] = e

    t = threading.Thread(target=body)
    t.start()
    assert ready.wait(10), "the client never issued an id"
    return t, got_id[0], outcome


def test_cancelling_a_running_command_is_acked(remote):
    t, ident, outcome = _run_in_background(remote, FOREVER)
    try:
        time.sleep(1.5)          # let it genuinely be running
        assert remote.cancel(ident) is True
    finally:
        t.join(timeout=60)
    assert "res" not in outcome, (
        "the cancelled request was ANSWERED -- that is the one thing the ack "
        f"promised would not happen: {outcome.get('res')}")
    assert isinstance(outcome.get("err"), Cancelled), outcome


def test_the_command_actually_stops(remote):
    """Not the contract, but the point. The refused reply is what does it:
    the send fails, ExecJob reads that as EXEC_SINK_GONE and ends the child
    rather than running it to its timeout for nobody."""
    t, ident, _outcome = _run_in_background(remote, FOREVER, timeout=45.0)
    try:
        time.sleep(1.5)
        t0 = time.monotonic()
        remote.cancel(ident)
        t.join(timeout=30)
        took = time.monotonic() - t0
    finally:
        t.join(timeout=60)
    assert not t.is_alive(), "the job never ended"
    # Well inside the 45s the command was given, so it was stopped rather
    # than left to time out.
    assert took < 25, f"took {took:.1f}s to stop"


def test_the_lane_comes_back_though_not_instantly(remote):
    """The ack says the request will never be ANSWERED. It does not say the
    machine is idle, and the difference is worth pinning: a cancelled EXEC
    is still walking the child's process tree and stopping it, so the next
    request on that lane can be told `busy` for a moment afterwards. Only
    the first of those is Tflush's promise.
    """
    t, ident, _ = _run_in_background(remote, FOREVER)
    try:
        time.sleep(1.5)
        remote.cancel(ident)
        t.join(timeout=30)
        deadline = time.monotonic() + 30
        refusals = 0
        while True:
            try:
                res = remote.exec("cmd.exe /c echo after-the-cancel",
                                  timeout=20)
                break
            except Refused as e:
                assert e.code == "busy", e.code
                refusals += 1
                assert time.monotonic() < deadline, "the lane never came back"
                time.sleep(0.25)
        assert "after-the-cancel" in res.text(remote.oemcp), res
    finally:
        t.join(timeout=60)


def test_cancelling_something_that_is_not_there(remote):
    """The ordinary outcome of a cancel that lost the race against a reply.
    False rather than an exception: the caller asked for the request to be
    over, and it is."""
    assert remote.cancel("c99999") is False


def test_cancel_with_no_target_is_refused(remote):
    """`no_target` and not `no_such_id`: "you did not say what to cancel"
    and "there is nothing by that name" are different mistakes, and while
    they shared a code the wrong branch fired without saying so."""
    with pytest.raises(Refused) as e:
        remote.request("CANCEL")
    assert e.value.code == "no_target", e.value.code


def test_a_cancelled_transfer_leaves_nothing_behind(remote, tmp_path_factory):
    """A PUT is not one frame, so there is no handler in flight to refuse
    anything -- the sink sits open with a temp beside somebody else's file,
    waiting for chunks that are not coming. Cancelling it has to take that
    down, and on the lane's OWN thread: the reader may not touch a handle
    this one can be inside WriteFile on.
    """
    d = tmp_path_factory.mktemp("xfer")
    target = d / "half-sent.bin"
    from bridge.wire.frame import Frame
    from gremote.client import _Call

    ident = "put-to-cancel"
    call = _Call()
    with remote._calls_lock:
        remote._calls[ident] = call
    try:
        remote._send(Frame("PUT", {"id": ident,
                                   "path": conftest.wine_path(target),
                                   "off": "0"}, b"A" * 4096))
        time.sleep(0.5)
        assert remote.cancel(ident) is True
    finally:
        with remote._calls_lock:
            remote._calls.pop(ident, None)

    # The lane's idle pass does the teardown; give it a couple of turns.
    for _ in range(40):
        if not list(d.iterdir()):
            break
        time.sleep(0.2)
    assert not list(d.iterdir()), (
        f"a cancelled transfer left {[p.name for p in d.iterdir()]}")
    assert not target.exists()
