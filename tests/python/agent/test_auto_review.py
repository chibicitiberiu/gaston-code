"""Auto mode: a reviewer may approve a machine call in the person's place,
and may never refuse one -- anything short of SAFE reaches the bar."""
from __future__ import annotations

import asyncio

from claude_agent_sdk import PermissionResultAllow

from bridge.agent import review
from bridge.agent.permdesk import PermissionDesk
from bridge.files import caps as C
from bridge.view.transcript import TranscriptView


class Guest:
    def __init__(self, mode):
        self.perm_mode = mode
        self.caps = C.Caps(ops=2)
        self.work_dir = ""
        self.work_guest = ""
        self.hello = {}
        self.recent_prompts = ["clear out C:\\TEMP\\OLD"]
        self.tools = []
        self.view = TranscriptView()


class Ctx:
    tool_use_id = "toolu_1"
    suggestions: tuple = ()
    title = None
    description = None
    display_name = None


def _desk(guest, blocks, sent):
    async def write_block(html):
        blocks.append(html)

    async def send(frame):
        sent.append(frame)

    async def preview(tool, args):
        return "a folder of 3 files, 2 KB"

    return PermissionDesk(guest, cwd="/tmp", write_block=write_block,
                          send=send, spawn=asyncio.ensure_future,
                          preview=preview)


def _run(mode, verdict, monkeypatch):
    seen = {}

    async def fake_review(guest, tool, args, preview_text=""):
        seen["preview"] = preview_text
        return verdict

    monkeypatch.setattr(review, "review", fake_review)
    blocks, sent = [], []
    desk = _desk(Guest(mode), blocks, sent)

    async def go():
        cb = desk.callback()
        task = asyncio.ensure_future(cb("mcp__machine__delete",
                                        {"path": "C:\\TEMP\\OLD"}, Ctx()))
        await asyncio.sleep(0.05)
        if task.done():
            return task.result()
        task.cancel()
        return None

    return asyncio.run(go()), desk, blocks, sent, seen


def test_safe_in_auto_goes_ahead_without_a_bar(monkeypatch):
    result, _, blocks, sent, seen = _run(
        "auto", (True, "the person asked for it"), monkeypatch)
    assert isinstance(result, PermissionResultAllow)
    assert not sent                       # no PERMREQ reached the machine
    assert any("not by you" in b for b in blocks)
    assert seen["preview"] == "a folder of 3 files, 2 KB"


def test_ask_in_auto_reaches_the_person_with_the_reason(monkeypatch):
    result, _, _, sent, _ = _run(
        "auto", (False, "not what was asked"), monkeypatch)
    assert result is None                 # still waiting on the person
    assert any(f.type == "PERMREQ" for f in sent)
    req = next(f for f in sent if f.type == "PERMREQ")
    assert b"not what was asked" in req.body
    assert b"3 files" in req.body


def test_manual_never_consults_the_reviewer(monkeypatch):
    called = []

    async def fake_review(*a, **k):
        called.append(1)
        return True, ""

    monkeypatch.setattr(review, "review", fake_review)
    blocks, sent = [], []
    desk = _desk(Guest("default"), blocks, sent)

    async def go():
        task = asyncio.ensure_future(desk.callback()(
            "mcp__machine__delete", {"path": "C:\\X"}, Ctx()))
        await asyncio.sleep(0.05)
        task.cancel()

    asyncio.run(go())
    assert not called
    assert any(f.type == "PERMREQ" for f in sent)
