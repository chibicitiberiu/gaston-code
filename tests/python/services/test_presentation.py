"""The three TEXT producers, and that a Block reaches each of them.

Stage 3 A6. There are three, not one, and the plan said so after getting it
wrong first: write_block (APPEND), the LIVE arm of write_updates -- which
takes u.html and bypasses write_block entirely -- and send_page. A7 to A11
convert the producers on the assumption that all three are byte-exact, so
each is exercised here rather than only the obvious one.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from bridge.services.presentation import PresentationService
from bridge.view import blocks as B
from bridge.view.html import APPEND, LIVE
from bridge.view.transcript import TranscriptView


@pytest.fixture
def svc():
    conn = MagicMock()
    conn.send_records = AsyncMock()
    conn.send = AsyncMock()
    conn.wire_yield = AsyncMock()
    guest = MagicMock()
    guest.view = TranscriptView()
    # A real number, because write_block compares against it. A MagicMock
    # here made every size check a TypeError, which is the mock equivalent
    # of the capability being unset.
    guest.caps.maxblock = 61440
    return PresentationService(conn), guest, conn


PROSE = B.Block("para", {}, [("t", B.runs("**bold** and `code`"))])
RECORD = B.encode(PROSE)


@pytest.mark.asyncio
async def test_write_block_encodes_and_sends_a_record(svc):
    s, guest, conn = svc
    await s.write_block(guest, PROSE)
    conn.send_records.assert_awaited_once()
    assert conn.send_records.await_args.args[1] == RECORD
    assert conn.send_records.await_args.args[2] == APPEND


@pytest.mark.asyncio
async def test_write_block_refuses_markup(svc):
    """A6 widened the type to str | Block so the call sites could convert
    one at a time; C2 closes it. A string here would reach the guest as a
    record it cannot parse, and failing loudly beats a transcript that
    silently stops -- which is what an unparseable body does."""
    s, guest, _conn = svc
    with pytest.raises(TypeError):
        await s.write_block(guest, "<p>markup</p>")


@pytest.mark.asyncio
async def test_the_record_reaches_the_paginator_whole(svc):
    s, guest, _conn = svc
    await s.write_block(guest, PROSE)
    assert guest.view.pages.all_records() == [RECORD]


@pytest.mark.asyncio
async def test_the_live_arm_bypasses_write_block(svc):
    """The path that is easy to miss: write_updates sends LIVE straight to
    the connection, so a change made only in write_block leaves the
    streaming half unconverted. It never reaches the paginator either --
    the open block is replaced wholesale on the guest, not appended."""
    s, guest, conn = svc
    from bridge.view.blocks import BlockUpdate

    await s.write_updates(guest, [BlockUpdate(LIVE, PROSE)])
    assert conn.send_records.await_args.args[1] == RECORD
    assert conn.send_records.await_args.args[2] == LIVE
    assert guest.view.pages.all_records() == []


@pytest.mark.asyncio
async def test_an_empty_live_body_is_sent_because_it_clears_the_slot(svc):
    """`block is None` is not "nothing to do". It is what empties `#l` at
    the end of a turn, and skipping it would leave the last partial
    paragraph on screen under the finished one."""
    s, guest, conn = svc
    from bridge.view.blocks import BlockUpdate

    await s.write_updates(guest, [BlockUpdate(LIVE, None)])
    conn.send_records.assert_awaited_once()
    assert conn.send_records.await_args.args[1] == b""


@pytest.mark.asyncio
async def test_send_range_is_the_third_producer(svc):
    s, guest, conn = svc
    await s.write_block(guest, PROSE)
    conn.send_records.reset_mock()

    await s.send_range(guest, "live", "")
    sent = b"".join(c.args[1] for c in conn.send_records.await_args_list)
    assert RECORD in sent


@pytest.mark.asyncio
async def test_a_block_with_no_payload_sends_nothing(svc):
    """A kind that DECLARES payload tags and carries none has nothing to
    draw. Not the same as a payload-less KIND: `rule` has no tags to carry
    and is still a record."""
    s, guest, conn = svc
    await s.write_block(guest, B.Block("para", {}, []))
    conn.send_records.assert_not_awaited()

    await s.write_block(guest, B.Block("rule"))
    assert conn.send_records.await_args.args[1] == B.encode(B.Block("rule"))


@pytest.mark.asyncio
async def test_a_record_over_the_client_s_declared_bound_is_refused(svc):
    """REFUSED, not truncated and not split. Half a record is two
    fragments the guest throws away, and a block the client said it cannot
    hold is one it will not render however it arrives. The model still has
    the whole text either way."""
    s, guest, conn = svc
    guest.caps.maxblock = 200
    await s.write_block(guest, B.Block("code", {}, [("t", "x" * 5000)]))
    conn.send_records.assert_not_awaited()
    assert guest.view.pages.all_records() == []


# --- the client's declaration survives the user's settings ------------------


def test_only_the_handshake_sets_the_declared_tool_cap():
    """The trap the stage-3 plan named, in the form it actually takes.

    limits_for has TWO production callers: the handshake and the PREFS
    handler. The page limits go through both, deliberately -- page size IS
    a preference, scaled by the user's own multiplier. toolmax is not: it
    is how much of a tool result this MACHINE can usefully show, which the
    client declares at the handshake and the user's settings have no
    business touching.

    So it is set beside the page limits at the handshake and nowhere else.
    The obvious tidy-up is to move all three into one place; all three
    would then be reset by the next PREFS frame, silently, and a client
    that asked for the whole of every tool result would quietly start
    getting 1,200 characters again on its first settings change.

    Structural, because exercising on_prefs means standing up a model list,
    a permission desk and a prompts table to assert the absence of one
    line.
    """
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[3] / "bridge"
    opts = (root / "services" / "options.py").read_text(encoding="utf-8")
    conn = (root / "services" / "connection.py").read_text(encoding="utf-8")

    assert "toolmax" not in opts, (
        "the PREFS handler now touches toolmax; the user's settings must "
        "not overwrite what the client declared it can render")
    assert "guest.view.toolmax = caps.toolmax" in conn, (
        "the handshake no longer applies the declared tool cap, so every "
        "client silently gets the bridge's own number")


@pytest.mark.asyncio
async def test_the_declared_cap_reaches_the_block_that_is_built(svc):
    """End to end: the number on the view is the number the record carries,
    because nothing else would notice if the plumbing were cut."""
    from bridge.view import html as H

    _s, guest, _conn = svc
    guest.view.toolmax = 50
    block = H.tool_result("x" * 500, cap=guest.view.toolmax)
    assert block.attrs["clipped"] == 450
    assert len("".join(block.texts())) == 50
