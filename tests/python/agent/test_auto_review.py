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
        # The CLI's names, as these tests were written with, mapped to the
        # execution modes that replaced them.
        self.mode_id = {"default": "manual"}.get(mode, mode)
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


def _texts(block):
    """The prose inside whatever write_block was handed.

    From stage 3 A7 the constructors return a Block, and a few call sites
    still pass finished markup until C2 converts them, so this takes either.
    """
    return [block] if isinstance(block, str) else block.texts()


def _run(mode, verdict, monkeypatch):
    seen = {}

    async def fake_review(guest, tool, args, preview_text="", **kw):
        seen["preview"] = preview_text
        seen.update(kw)
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
    assert any("not by you" in t for b in blocks for t in _texts(b))
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


# --- the second gate: calls that came round the desk -----------------------


def test_a_call_the_desk_approved_passes_the_gate_once(monkeypatch):
    async def fake_review(guest, tool, args, preview_text="", **kw):
        return True, "asked for"

    monkeypatch.setattr(review, "review", fake_review)
    desk = _desk(Guest("auto"), [], [])
    args = {"path": "C:\\TEMP\\OLD"}

    async def go():
        await desk.callback()("mcp__machine__delete", args, Ctx())
        first = await desk.gate("mcp__machine__delete", args)
        return first

    assert asyncio.run(go()) is None
    assert not desk._approved            # consumed, not reusable


def test_a_call_that_came_round_the_desk_is_reviewed_in_the_tool(
        monkeypatch):
    """Measured: in Auto the CLI approved a delete without calling the desk.
    The gate puts the reviewer back in front of it."""
    seen = []

    async def fake_review(guest, tool, args, preview_text="", **kw):
        seen.append(tool)
        return True, "asked for"

    monkeypatch.setattr(review, "review", fake_review)
    desk = _desk(Guest("auto"), [], [])
    assert asyncio.run(desk.gate("mcp__machine__delete",
                                 {"path": "C:\\TEMP\\OLD"})) is None
    assert seen == ["mcp__machine__delete"]


def test_an_unsure_reviewer_in_the_gate_asks_the_person(monkeypatch):
    async def fake_review(guest, tool, args, preview_text="", **kw):
        return False, "not what was asked"

    monkeypatch.setattr(review, "review", fake_review)
    sent = []
    desk = _desk(Guest("auto"), [], sent)

    async def go():
        task = asyncio.ensure_future(desk.gate("mcp__machine__delete",
                                               {"path": "C:\\X"}))
        await asyncio.sleep(0.05)
        done = task.done()
        task.cancel()
        return done

    assert asyncio.run(go()) is False     # waiting on the person
    assert any(f.type == "PERMREQ" for f in sent)
