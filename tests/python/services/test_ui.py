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
    task = asyncio.create_task(svc.click(g, "Button"))
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
