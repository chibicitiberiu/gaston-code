"""The desktop lane, under Wine.

WINE PROVES THE CODE AND NEVER THE PLATFORM, and that is sharper here than
anywhere else in this suite: Wine's oleacc answers for every window class,
it emits EVENT_OBJECT_CREATE and never EVENT_OBJECT_DESTROY for a hidden
popup, and it honours WINEVENT_SKIPOWNPROCESS in name only. So what is
asserted here is the LANE -- that the ops are routed, refused and answered
the way the op table says -- and not what the desktop contains.

The guest run is docs/measurements/ui-lane-vm-20261003.txt.
"""
from __future__ import annotations

import pathlib
import sys
import threading
import time

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import conftest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[3]))
from gremote import Remote
from gremote.errors import Cancelled, Refused

pytestmark = conftest.needs_guest

SECRET = "not-the-default"


def ini(port: int, *, automation: int = 2) -> str:
    return (
        "[server]\n"
        "bind=127.0.0.1\n"
        f"port={port}\n"
        f"secret={SECRET}\n"
        "allow=\n"
        "heartbeat_timeout_s=30\n"
        "[ui]\n"
        f"automation={automation}\n"
    )


def _server(tmp_path, automation=2):
    port = conftest.free_port()
    return conftest.GuestServer("gremote.exe", tmp_path,
                                ini(port, automation=automation), port)


# FUNCTION scoped, not module. gremote holds a single-instance mutex, so a
# test that needs its own settings cannot start a second copy beside a
# module-wide one -- it just gets "another copy is already running". Paying
# a second per test is the cost of the ini being per-test.
@pytest.fixture
def remote(tmp_path):
    with _server(tmp_path) as server:
        with Remote("127.0.0.1", SECRET, port=server.port) as r:
            r.server = server
            yield r


def test_the_handshake_declares_the_lane(remote):
    """automation, keys and machine are what a caller branches on, and msaa
    is the answer to a question about the box rather than about us."""
    assert remote.hello["automation"] == "2", remote.hello
    assert remote.hello["keys"].isdigit(), remote.hello
    assert remote.hello["msaa"] in ("0", "1"), remote.hello
    assert remote.hello["machine"], remote.hello
    # Its own key, because `automation` says what this box ALLOWS and never
    # what this build HAS -- a CLIP sent to a gremote that predates the op
    # comes back `unknown_op`, and a caller has no way to tell that from a
    # refusal unless the handshake said so first.
    assert remote.hello["clip"] == "1", remote.hello


def test_a_tree_comes_back_with_selectors(remote):
    """The records are the guest's and the selectors are built here, in the
    order it walked -- `:nth(k)` counts in that order."""
    tree = remote.tree(max_nodes=200)
    assert tree.walk in ("complete", "maxnodes", "deadline", "stopped")
    assert len(tree.selectors) == len(tree.nodes)
    # Wine's desktop is not this project's to predict, so the only claim
    # about CONTENT is that a record that exists is addressable.
    for node, sel in zip(tree.nodes, tree.selectors, strict=True):
        assert sel, node


def test_a_screenshot_is_a_bmp(remote):
    img = remote.shot(w=64, h=48)
    assert img[:2] == b"BM", img[:16]
    assert len(img) > 54, len(img)


def test_the_clipboard_round_trips(remote):
    """Set then get, through the real codec and the real lane. Wine has a
    clipboard; the guest's own case asserts only self-consistency because a
    headless host may not."""
    remote.clipboard("over the wire and back")
    assert remote.clipboard() == "over the wire and back"


def test_an_empty_clipboard_is_an_empty_string_not_a_refusal():
    """Nothing copied is not an error, and the guest spends no DATA frame
    saying so -- `bytes=0` is the answer. Its own server, because emptying
    the clipboard would upset the round trip above whichever order they
    run in."""
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        with _server(pathlib.Path(tmp)) as server:
            with Remote("127.0.0.1", SECRET, port=server.port) as r:
                r.clipboard("")
                assert r.clipboard() == ""


def test_a_clipboard_read_over_the_ceiling_is_refused(remote):
    """Half of what somebody copied reads exactly like all of it, so the
    prefix is not returned."""
    from gremote.errors import RemoteError
    remote.clipboard("x" * 4000)
    with pytest.raises(RemoteError, match=r"more than|arrived"):
        remote.clipboard(max_bytes=64)


def test_a_shot_over_the_ceiling_is_refused_while_it_arrives(remote):
    """The ceiling is enforced on the READER thread, not on the caller's.

    By the time `shot()` sees the frames the memory is spent, so a ceiling
    applied there is not a ceiling at all."""
    from gremote.errors import RemoteError
    with pytest.raises(RemoteError, match="more than"):
        remote.shot(w=640, h=480, max_bytes=4096)


def test_a_malformed_step_list_is_refused_before_it_is_sent(remote):
    """Compiled on this side first, so a list that cannot run costs no round
    trip at all -- and the reason names the STEP and then every action that
    would have been valid, which is more use than naming the bad key."""
    from bridge.agent.steps import StepError
    with pytest.raises(StepError) as e:
        remote.run([{"wiggle": "Notepad"}])
    assert "step 1" in str(e.value), e.value
    assert "focus" in str(e.value), e.value


def test_a_failing_list_still_reports_what_ran(remote):
    """THE REASON `run` DOES NOT RAISE. A list that failed still ran the
    steps before it, and those are in the DATA frames a raising path throws
    away with the exception."""
    run = remote.run([{"wait_for": "NoSuchWindowClass", "timeout": 1200}])
    assert not run.ok, run
    assert run.steps, "the per-step report was lost"
    assert run.steps[0].status in ("timeout", "truncated", "not_found"), run
    assert run.failed_at == 0, run


def test_stop_on_an_unknown_id_says_so(remote):
    assert remote.stop("no-such-request") is False


def test_stop_lets_the_request_answer_and_cancel_does_not(remote):
    """The two promises, side by side, which is the whole design of STOP.

    CANCEL is 9P's Tflush: its ack means the request will never be answered.
    STOP promises only that the work stops, because a list that failed at
    step 5 still ran four steps the caller has to hear about.
    """
    for stopper, expect_answer in ((remote.stop, True),
                                   (remote.cancel, False)):
        ids: list[str] = []
        out: list[object] = []

        def body():
            try:
                out.append(remote.run(
                    [{"wait_for": "NeverAppears", "timeout": 20000}],
                    timeout_ms=25000, timeout=40.0, on_id=ids.append))
            except Cancelled:
                out.append("never answered")
            except Refused as e:  # pragma: no cover - a refusal is a result
                out.append(f"refused {e.code}")

        t = threading.Thread(target=body)
        t.start()
        deadline = time.monotonic() + 5
        while not ids and time.monotonic() < deadline:
            time.sleep(0.02)
        assert ids, "the call never reported an id"
        time.sleep(0.5)
        assert stopper(ids[0]) is True
        t.join(30)
        assert out, "the call never finished"
        if expect_answer:
            assert out[0] != "never answered", out
            assert getattr(out[0], "status", "") == "cancelled", out
        else:
            assert out[0] == "never answered", out


def test_automation_off_refuses_every_desktop_op(tmp_path):
    """The control, and it is read live -- so this is a refusal the machine
    makes rather than a tool the caller was not offered."""
    with _server(tmp_path, automation=0) as server:
        with Remote("127.0.0.1", SECRET, port=server.port) as r:
            assert r.hello["automation"] == "0", r.hello
            for call in (lambda: r.tree(),
                         lambda: r.shot(w=32, h=32),
                         lambda: r.clipboard(),
                         lambda: r.clipboard("nope")):
                with pytest.raises(Refused) as e:
                    call()
                assert e.value.code == "automation_off", e.value.code
            run = r.run([{"focus": "Progman"}])
            assert run.status == "automation_off", run


def test_read_only_reads_and_photographs_but_does_not_drive(tmp_path):
    """RC_AUTOMATION_SCREEN, the middle value. The client can never declare
    it -- it sends STEPS or NONE -- so gremote's ini is the first thing that
    can ask for a box which may be looked at and not typed into."""
    with _server(tmp_path, automation=1) as server:
        with Remote("127.0.0.1", SECRET, port=server.port) as r:
            assert r.hello["automation"] == "1", r.hello
            r.tree(max_nodes=50)
            assert r.shot(w=32, h=32)[:2] == b"BM"
            # The clipboard splits along the same line: reading what was
            # copied is looking, replacing it is driving. The READ may
            # legitimately refuse `no_clip_text` -- a fresh Wine has nothing
            # on its clipboard -- so what is asserted is that it was not
            # refused for the capability.
            try:
                r.clipboard()
            except Refused as e:
                assert e.code == "no_clip_text", e.code
            with pytest.raises(Refused) as e:
                r.clipboard("not at this level")
            assert e.value.code == "automation_off", e.value.code
            run = r.run([{"focus": "Progman"}])
            assert run.status == "automation_off", run


def test_inproc_is_refused_rather_than_quietly_served(remote):
    """gremote does not carry the in-process tier. A caller who asked for it
    and silently got the out-of-process answer would read a full list box as
    empty, which is why the two tiers have never been allowed to substitute
    for one another."""
    with pytest.raises(Refused) as e:
        remote.stream("TREE", {"inproc": "1", "target": "Progman"})
    assert e.value.code == "inproc_unsupported", e.value.code
