"""The desktop service: issue a request, assemble the reply, refuse clearly.

The guest end is tested under Wine; this is the half that has to behave when
the guest does something unhelpful -- answers late, answers a request nobody
is waiting for, refuses, or goes away mid-call. Those are the paths that
take a connection down if they are written optimistically, which is how the
sync path learned to look before it indexed.
"""
import asyncio
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[3]))

import conftest

from bridge.agent import prompts
from bridge.guest import GuestSession
from bridge.services.ui import UiError, UiService
from bridge.wire.protocol import Frame

pytestmark = pytest.mark.asyncio


class FakeLink:
    """Records what went out and answers on demand."""

    def __init__(self):
        self.sent = []

    async def send(self, guest, frame):
        self.sent.append(frame)


def guest_and_service():
    g = GuestSession(addr=("127.0.0.1", 0)) if _takes_addr() else GuestSession()
    return g, UiService(connection=FakeLink())


def _takes_addr():
    import inspect
    return "addr" in inspect.signature(GuestSession).parameters


async def reply(svc, guest, *frames):
    """Feed replies once the request is actually in flight."""
    for _ in range(200):
        if guest.ui_pending:
            break
        await asyncio.sleep(0)
    for f in frames:
        ui_id = next(iter(guest.ui_pending))
        f.headers.setdefault("id", ui_id)
        await svc.ui_frame(guest, f)


async def test_a_tree_round_trips_and_gets_selectors():
    g, svc = guest_and_service()
    body = (b"w 0 - 0 0 0 0 640 480 3 6 7 0 0 0 0 0 #32770Save As\n"
            b"w 0/0 0 1 0 12 40 90 60 3 6 2 10 0 0 0 0 ButtonOKpushbutton\n")
    task = asyncio.create_task(svc.tree(g))
    await reply(svc, g,
                Frame("UIENT", {"n": "2"}, body),
                Frame("UIEND", {"status": "ok", "nodes": "2",
                                "walk": "complete"}))
    nodes, headers = await task

    assert [n.path for n in nodes] == ["0", "0/0"]
    assert nodes[0].cls == "#32770"
    assert nodes[1].text == "OK"
    assert headers["walk"] == "complete"
    #: The whole point: the caller gets something it can click with.
    assert all(n.sel for n in nodes), [n.sel for n in nodes]
    assert nodes[0].sel == '[class="#32770"]'


async def test_records_arrive_in_several_frames():
    """A tree is bigger than one 62KB frame, so the accumulator is the
    normal path rather than an edge case."""
    g, svc = guest_and_service()
    task = asyncio.create_task(svc.tree(g))
    await reply(svc, g,
                Frame("UIENT", {}, b"w 0 - 0 0 0 0 1 1 3 1 0 0 0 0 0 0 A\n"),
                Frame("UIENT", {}, b"w 0/0 0 0 0 0 0 1 1 3 1 0 0 0 0 0 0 B\n"),
                Frame("UIEND", {"status": "ok", "nodes": "2"}))
    nodes, _ = await task
    assert [n.cls for n in nodes] == ["A", "B"]


async def test_a_refusal_is_raised_with_its_status():
    g, svc = guest_and_service()
    task = asyncio.create_task(svc.tree(g, wait_for="Button"))
    await reply(svc, g, Frame("UIEND", {"status": "ambiguous",
                                        "detail": "4 controls match"}))
    with pytest.raises(UiError) as got:
        await task
    assert got.value.status == "ambiguous"
    assert "4 controls" in got.value.detail


async def test_a_frame_nobody_awaits_is_dropped_not_raised():
    """A late reply, or one for a call that was cancelled. A KeyError here
    goes all the way to the connection pump and drops the guest."""
    g, svc = guest_and_service()
    await svc.ui_frame(g, Frame("UIEND", {"id": "u99", "status": "ok"}))
    await svc.ui_frame(g, Frame("UIENT", {"id": "u99"}, b"junk\n"))


async def test_records_for_a_screenshot_are_refused():
    """The two accumulators share an id namespace and an END frame. Letting
    a UIENT land in a shot's buffer is the cross-resolve the sync service
    guards against by kind."""
    g, svc = guest_and_service()
    task = asyncio.create_task(svc.shot(g))
    await reply(svc, g,
                Frame("UIENT", {}, b"w 0 - 0 0 0 0 1 1 3 1 0 0 0 0 0 0 A\n"),
                Frame("UIDAT", {}, b"BMbytes"),
                Frame("UIEND", {"status": "ok", "bytes": "7", "w": "640",
                                "h": "480", "bpp": "8"}))
    data, headers = await task
    assert data == b"BMbytes"
    assert headers["w"] == "640"


async def test_a_short_image_is_refused_rather_than_returned():
    """Half a screenshot decodes to something, and that something is
    wrong."""
    g, svc = guest_and_service()
    task = asyncio.create_task(svc.shot(g))
    await reply(svc, g,
                Frame("UIDAT", {}, b"BM"),
                Frame("UIEND", {"status": "ok", "bytes": "99999"}))
    with pytest.raises(UiError) as got:
        await task
    assert got.value.status == "short_image"


async def test_a_lost_connection_wakes_every_waiter():
    """Otherwise each one sits out its own budget, one after another."""
    g, svc = guest_and_service()
    task = asyncio.create_task(svc.tree(g))
    for _ in range(200):
        if g.ui_pending:
            break
        await asyncio.sleep(0)
    svc.fail_calls(g, "the machine went away")
    with pytest.raises(UiError) as got:
        await task
    assert got.value.status == "disconnected"
    assert not g.ui_pending


async def test_ids_are_their_own_namespace():
    """The guest checks the id prefix, so a UI reply can never
    cross-resolve against an exec or a sync call."""
    g, svc = guest_and_service()
    task = asyncio.create_task(svc.tree(g))
    for _ in range(200):
        if g.ui_pending:
            break
        await asyncio.sleep(0)
    assert all(k.startswith("u") for k in g.ui_pending), list(g.ui_pending)
    svc.fail_calls(g, "done")
    with pytest.raises(UiError):
        await task


async def test_the_in_process_tier_is_opt_in():
    """Off unless asked. It puts our code inside a program that did not ask
    for it, so the default has to be the tier that does not."""
    g, svc = guest_and_service()
    task = asyncio.create_task(svc.tree(g))
    await reply(svc, g, Frame("UIEND", {"status": "ok", "nodes": "0"}))
    await task
    assert "inproc" not in svc.connection.sent[0].headers


async def test_asking_for_it_says_so_on_the_wire():
    g, svc = guest_and_service()
    task = asyncio.create_task(svc.tree(g, inproc=True, target="Notepad"))
    await reply(svc, g, Frame("UIEND", {"status": "ok", "nodes": "0",
                                        "tier": "inproc"}))
    _, headers = await task
    sent = svc.connection.sent[0].headers
    assert sent["inproc"] == "1"
    assert sent["target"] == "Notepad"
    assert sent["items"] == "1"
    #: The guest says which tier answered, so the caller never has to guess.
    assert headers["tier"] == "inproc"


async def test_a_refusal_does_not_become_a_quiet_fallback():
    """The two tiers give different answers and only one of them can read a
    list box. A caller that asked for the in-process one and silently got
    the other would conclude a list is empty."""
    g, svc = guest_and_service()
    task = asyncio.create_task(svc.tree(g, inproc=True))
    await reply(svc, g, Frame("UIEND", {"status": "denied",
                                        "detail": "EXPLORER.EXE is on the "
                                                  "deny list"}))
    with pytest.raises(UiError) as got:
        await task
    assert got.value.status == "denied"
    assert "deny list" in got.value.detail


async def test_list_rows_come_back_as_their_own_records():
    """The whole reason the tier exists: LB_GETTEXT takes a pointer and
    nothing marshals it across a process boundary on 9x."""
    g, svc = guest_and_service()
    body = (b"w 0 - 0 0 0 0 100 100 3 13 0 0 0 0 0 0 SysListView32\n"
            b"a 0/i0 0 0 0 0 0 0 0 1 0 12 8 12 0 0 0 "
            b"AUTOEXEC.BATlistitemAUTOEXEC.BAT\n")
    task = asyncio.create_task(svc.tree(g, inproc=True))
    await reply(svc, g,
                Frame("UIENT", {"n": "2"}, body),
                Frame("UIEND", {"status": "ok", "nodes": "2", "items": "1",
                                "tier": "inproc"}))
    nodes, headers = await task
    assert [n.src for n in nodes] == ["w", "a"]
    assert nodes[1].role == "listitem"
    assert nodes[1].name == "AUTOEXEC.BAT"
    assert headers["items"] == "1"


# --- the `input` step list --------------------------------------------------


async def test_a_run_returns_its_status_and_per_step_body():
    """Not raised on: a list that failed at step 3 still ran two steps the
    agent needs to hear about."""
    g, svc = guest_and_service()
    task = asyncio.create_task(svc.run(g, b"wait\tms=1\n", timeout_ms=5000,
                                       step_ms=1000))
    await reply(svc, g, Frame("UIEND", {"status": "focus_lost", "step": "0"},
                              b"i=0\tstatus=focus_lost\tms=3\n"))
    headers, body = await task
    assert headers["status"] == "focus_lost"
    assert body.startswith(b"i=0\tstatus=focus_lost")
    sent = svc.connection.sent[0]
    assert sent.type == "UIRUN" and sent.body == b"wait\tms=1\n"
    assert sent.headers["timeout_ms"] == "5000"


async def test_a_run_the_guest_outlasts_is_cancelled_on_the_guest(
        monkeypatch):
    """The bridge giving up must also stop the list on the machine -- a list
    nobody waits for would hold the desktop lane, and any keys it holds, for
    the rest of its time."""
    import bridge.services.ui as U
    monkeypatch.setattr(U, "BUDGET_SLACK_S", 0.05)
    g, svc = guest_and_service()
    task = asyncio.create_task(svc.run(g, b"wait\tms=99999\n", timeout_ms=10,
                                       step_ms=10))
    for _ in range(100):
        await asyncio.sleep(0.02)
        if any(f.type == "UICANCEL" for f in svc.connection.sent):
            break
    cancel = [f for f in svc.connection.sent if f.type == "UICANCEL"]
    assert cancel and cancel[0].headers["id"] == svc.connection.sent[0].headers["id"]
    # And the guest's answer to the cancel is what the caller gets.
    await reply(svc, g, Frame("UIEND", {"status": "cancelled", "step": "0"},
                              b"i=0\tstatus=cancelled\tms=60\n"))
    headers, _ = await task
    assert headers["status"] == "cancelled"


# --- the clipboard ---------------------------------------------------------


async def test_the_clipboard_comes_back_on_the_bulk_channel():
    """`bytes()` on the guest, so UIDAT -- the same channel a screenshot
    uses, with a ceiling of its own, because the honest number for an 8bpp
    screen is not the honest number for text somebody copied."""
    g, svc = guest_and_service()
    task = asyncio.create_task(svc.clipboard(g))
    await reply(svc, g,
                Frame("UIDAT", {}, b"copied text"),
                Frame("UIEND", {"status": "ok", "bytes": "11"}))
    text, headers = await task
    assert text == b"copied text"
    assert headers["bytes"] == "11"


async def test_an_empty_clipboard_is_ok_and_not_a_refusal():
    """Nothing copied is not a failure, and the guest spends no frame saying
    so -- `bytes=0` is the answer."""
    g, svc = guest_and_service()
    task = asyncio.create_task(svc.clipboard(g))
    await reply(svc, g, Frame("UIEND", {"status": "ok", "bytes": "0"}))
    text, headers = await task
    assert text == b""
    assert headers["bytes"] == "0"


async def test_a_short_clipboard_is_refused_rather_than_returned():
    """Half the text reads as the whole text, and nothing says otherwise."""
    g, svc = guest_and_service()
    task = asyncio.create_task(svc.clipboard(g))
    await reply(svc, g,
                Frame("UIDAT", {}, b"half"),
                Frame("UIEND", {"status": "ok", "bytes": "900"}))
    with pytest.raises(UiError) as got:
        await task
    assert got.value.status == "short_clip"


async def test_a_write_rides_the_body_and_says_which_op():
    """A header value caps at 255 encoded bytes, so a document in one would
    be a different document -- the same reason a step list rides the body."""
    g, svc = guest_and_service()
    task = asyncio.create_task(svc.clipboard(g, text=b"put this on it"))
    await reply(svc, g, Frame("UIEND", {"status": "ok", "bytes": "14"}))
    text, _ = await task
    assert text == b""
    sent = svc.connection.sent[0]
    assert sent.type == "UICLIP"
    assert sent.headers["op"] == "set"
    assert sent.body == b"put this on it"


async def test_a_read_says_get_and_carries_no_body():
    g, svc = guest_and_service()
    task = asyncio.create_task(svc.clipboard(g))
    await reply(svc, g, Frame("UIEND", {"status": "ok", "bytes": "0"}))
    await task
    sent = svc.connection.sent[0]
    assert sent.headers["op"] == "get"
    assert not sent.body


async def test_the_guests_refusal_reaches_the_caller_verbatim():
    """`no_clip_text` -- something is on it and it is not text. Its own
    status, because an agent told "ok, 0 bytes" would read an image on the
    clipboard as an empty one and copy again."""
    g, svc = guest_and_service()
    task = asyncio.create_task(svc.clipboard(g))
    await reply(svc, g, Frame("UIEND", {"status": "no_clip_text"}))
    with pytest.raises(UiError) as got:
        await task
    assert got.value.status == "no_clip_text"


async def test_every_step_status_the_guest_sends_has_a_sentence():
    """A status steps.cpp can send and tools.toml cannot explain reaches the
    agent as `tools.ui.refused.unknown` -- a bare code where the sentence
    that makes it actionable should be.

    The same gap test_proc closes for processes, closed here for the step
    runner, and it was open: `refused_chord` was added to inputStatus and
    nothing would have noticed if its prose had not been written. The other
    direction is deliberately NOT checked -- tools.toml carries statuses
    from the engine and the bridge as well as from this table.
    """
    import re
    src = pathlib.Path(conftest.HERE, "win", "automation",
                       "steps.cpp").read_text()
    body = src[src.index("static const char *inputStatus"):]
    body = body[:body.index("\n}")]
    sent = set(re.findall(r'return "([a-z_]+)"', body))
    sent.discard("ok")
    assert sent, "found no statuses in inputStatus"
    have = set(prompts.all()["tools"]["ui"]["refused"])
    assert not (sent - have), sorted(sent - have)
