"""Unit tests for PresentationService."""

import pytest
from unittest.mock import AsyncMock, MagicMock
from bridge.services.presentation import PresentationService
from bridge.view.transcript import TranscriptView


@pytest.fixture
def mock_conn():
    conn = MagicMock()
    conn.send = AsyncMock(return_value=True)
    conn.send_html = AsyncMock()
    conn.wire_yield = AsyncMock()
    return conn


@pytest.fixture
def mock_guest():
    guest = MagicMock()
    guest.view = TranscriptView()
    guest.view.pages = guest.view.pages
    guest.view.render = guest.view.render
    guest.view.coalescers = guest.view.coalescers
    guest.view.view_page = None
    guest.link.link_bps = 5000
    return guest


@pytest.mark.asyncio
async def test_write_block_records_and_sends(mock_conn, mock_guest):
    ps = PresentationService(connection=mock_conn)

    await ps.write_block(mock_guest, "<p>Hello world</p>")
    assert mock_conn.send_html.called
    assert len(mock_guest.view.pages.live.runs) > 0


@pytest.mark.asyncio
async def test_reset_transcript(mock_conn, mock_guest):
    ps = PresentationService(connection=mock_conn)

    await ps.write_block(mock_guest, "<p>Before reset</p>")
    assert len(mock_guest.view.pages.live.runs) > 0

    await ps.reset_transcript(mock_guest)
    # Paginator reset
    assert len(mock_guest.view.pages.live.runs) == 0


@pytest.mark.asyncio
async def test_send_page_live_and_specific(mock_conn, mock_guest):
    ps = PresentationService(connection=mock_conn)

    # Put some content on live page
    await ps.write_block(mock_guest, "<p>Live content</p>")

    # send_page "live"
    await ps.send_page(mock_guest, "live")
    assert mock_guest.view.view_page is None
    assert mock_conn.send.called

    # send_page "1"
    mock_conn.send.reset_mock()
    await ps.send_page(mock_guest, "1")
    assert mock_conn.send.called

    # send_page invalid
    mock_conn.send.reset_mock()
    await ps.send_page(mock_guest, "invalid_num")
    assert mock_conn.send.called
    frame = mock_conn.send.call_args[0][1]
    assert frame.type == "ERROR"
    assert frame.headers.get("code") == "bad_page"


@pytest.mark.asyncio
async def test_emit_and_flush_text(mock_conn, mock_guest):
    ps = PresentationService(connection=mock_conn)

    await ps.emit_text(mock_guest, "hello from stream", style="plain")
    await ps.flush_text(mock_guest)
    assert len(mock_guest.view.pages.live.runs) > 0
