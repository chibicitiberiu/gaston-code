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
from bridge.view.htmlrender import render
from bridge.view.transcript import TranscriptView


@pytest.fixture
def svc():
    conn = MagicMock()
    conn.send_html = AsyncMock()
    conn.send = AsyncMock()
    conn.wire_yield = AsyncMock()
    guest = MagicMock()
    guest.view = TranscriptView()
    return PresentationService(conn), guest, conn


PROSE = B.Block("para", {}, [("t", B.runs("**bold** and `code`"))])
EXPECTED = "<p><b>bold</b> and <code>code</code></p>"


@pytest.mark.asyncio
async def test_write_block_takes_a_block(svc):
    s, guest, conn = svc
    await s.write_block(guest, PROSE)
    conn.send_html.assert_awaited_once()
    assert conn.send_html.await_args.args[1] == EXPECTED
    assert conn.send_html.await_args.args[2] == APPEND


@pytest.mark.asyncio
async def test_write_block_still_takes_markup(svc):
    """A6 adds a type, it does not replace one. Thirty-three call sites are
    still passing strings at this commit."""
    s, guest, conn = svc
    await s.write_block(guest, EXPECTED)
    assert conn.send_html.await_args.args[1] == EXPECTED


@pytest.mark.asyncio
async def test_the_block_and_the_markup_reach_the_paginator_identically(svc):
    s1, g1, _ = svc
    await s1.write_block(g1, PROSE)
    conn2 = MagicMock()
    conn2.send_html = AsyncMock()
    conn2.send = AsyncMock()
    g2 = MagicMock()
    g2.view = TranscriptView()
    await PresentationService(conn2).write_block(g2, EXPECTED)

    assert g1.view.pages.all_text() == g2.view.pages.all_text()
    assert g1.view.pages.all_text() == EXPECTED


@pytest.mark.asyncio
async def test_the_live_arm_bypasses_write_block_and_still_renders(svc):
    """This is the path that is easy to miss: write_updates sends LIVE
    straight to the connection, so a change made only in write_block leaves
    the streaming half unconverted."""
    s, guest, conn = svc
    from bridge.view.html import Update

    await s.write_updates(guest, [Update(LIVE, render(PROSE))])
    assert conn.send_html.await_args.args[1] == EXPECTED
    assert conn.send_html.await_args.args[2] == LIVE


@pytest.mark.asyncio
async def test_send_page_is_the_third_producer(svc):
    s, guest, conn = svc
    await s.write_block(guest, PROSE)
    conn.send_html.reset_mock()

    await s.send_page(guest, "live")
    sent = [c.args[1] for c in conn.send_html.await_args_list]
    assert EXPECTED in "".join(sent)


@pytest.mark.asyncio
async def test_a_block_with_no_payload_sends_nothing(svc):
    """A kind that DECLARES payload tags and carries none renders as "", the
    same as the old _render's `if not lines: return ""`. write_block's
    existing empty guard then has to run AFTER the render, not before it.

    Not the same as a payload-less KIND: `rule` has no tags to carry and is
    still an <hr>."""
    s, guest, conn = svc
    await s.write_block(guest, B.Block("para", {}, []))
    conn.send_html.assert_not_awaited()

    await s.write_block(guest, B.Block("rule"))
    assert conn.send_html.await_args.args[1] == "<hr>"
