"""Tests for ConnectionService."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bridge.services.config import BridgeConfig
from bridge.services.connection import ConnectionService
from bridge.wire.protocol import Frame, error


@pytest.fixture
def conn_service():
    config = BridgeConfig(
        secret="test-secret",
        hello_timeout_s=0.5,
        idle_timeout_s=1.0,
        idle_check_s=0.1,
        perm_grace_s=0.5,
    )
    on_attach = AsyncMock()
    handle_frame = AsyncMock()
    on_lost = MagicMock()
    ensure_background_tasks = MagicMock()
    spawn = MagicMock()

    guest_mock = MagicMock()
    guest_mock.connected = False
    guest_mock.lost_at = None
    guest_mock.perm_pending = None
    guest_mock.exec_pending = set()
    guest_mock.sync_active = False
    guest_mock.last_seen = 0.0
    guest_mock.caps = MagicMock()
    guest_mock.caps.acp = "1252"
    guest_mock.caps.oemcp = "437"
    guest_mock.tools = []
    guest_mock.link = MagicMock()
    guest_mock.link.send = AsyncMock(return_value=True)
    guest_mock.link.zip_in = MagicMock()
    guest_mock.link.zip_out = MagicMock()

    create_guest = MagicMock(return_value=guest_mock)

    svc = ConnectionService(
        config=config,
        create_guest=create_guest,
        on_attach=on_attach,
        handle_frame=handle_frame,
        on_lost=on_lost,
        ensure_background_tasks=ensure_background_tasks,
        spawn=spawn,
    )

    return {
        "service": svc,
        "config": config,
        "guest": guest_mock,
        "on_attach": on_attach,
        "handle_frame": handle_frame,
        "on_lost": on_lost,
        "ensure_background_tasks": ensure_background_tasks,
    }


def test_oem_for_and_bind_codepages(conn_service):
    svc = conn_service["service"]
    guest = conn_service["guest"]

    oem = svc.oem_for(guest)
    assert oem == "437"

    with patch("bridge.wire.fold.use") as mock_use:
        svc.bind_codepages(guest)
        assert mock_use.call_count >= 1


async def test_send(conn_service):
    svc = conn_service["service"]
    guest = conn_service["guest"]
    frame = Frame("TEST", {}, b"")

    res = await svc.send(guest, frame)
    assert res is True
    guest.link.send.assert_called_once_with(guest, frame)


async def test_try_send_raw(conn_service):
    svc = conn_service["service"]
    writer = MagicMock()
    frame = Frame("TEST", {}, b"")

    with patch("bridge.wire.link.GuestLink.send_raw", new_callable=AsyncMock) as mock_send_raw:
        await svc.try_send_raw(writer, frame)
        mock_send_raw.assert_called_once_with(writer, frame)


async def test_read_hello_valid(conn_service):
    svc = conn_service["service"]
    reader = AsyncMock()
    hello_frame = Frame("HELLO", {"secret": "test-secret"}, b"")
    early_frame = Frame("STAT", {}, b"")
    reader.read.side_effect = [hello_frame.encode() + early_frame.encode(), b""]

    frames = MagicMock()
    frames.feed.return_value = [hello_frame, early_frame]
    writer = MagicMock()

    h, early = await svc.read_hello(reader, frames, writer)
    assert h == hello_frame
    assert early == [early_frame]


async def test_read_hello_invalid_first_frame(conn_service):
    svc = conn_service["service"]
    reader = AsyncMock()
    bad_frame = Frame("STAT", {}, b"")
    reader.read.side_effect = [bad_frame.encode(), b""]

    frames = MagicMock()
    frames.feed.return_value = [bad_frame]
    writer = MagicMock()

    with patch.object(svc, "try_send_raw", new_callable=AsyncMock) as mock_raw:
        h, early = await svc.read_hello(reader, frames, writer)
        assert h is None
        assert early == []
        mock_raw.assert_called_once()
        sent_frame = mock_raw.call_args[0][1]
        assert sent_frame.type == "ERROR"
        assert sent_frame.headers["code"] == "expected_hello"


async def test_read_hello_timeout(conn_service):
    svc = conn_service["service"]
    reader = AsyncMock()
    async def slow_read(*args):
        await asyncio.sleep(1.0)
        return b""
    reader.read.side_effect = slow_read

    frames = MagicMock()
    writer = MagicMock()

    with patch.object(svc, "try_send_raw", new_callable=AsyncMock) as mock_raw:
        h, early = await svc.read_hello(reader, frames, writer)
        assert h is None
        assert early == []
        mock_raw.assert_called_once()
        sent_frame = mock_raw.call_args[0][1]
        assert sent_frame.type == "ERROR"
        assert sent_frame.headers["code"] == "hello_timeout"


async def test_attach_handshake_refused(conn_service):
    svc = conn_service["service"]
    writer = MagicMock()
    writer.get_extra_info.return_value = "127.0.0.1:1234"
    hello = Frame("HELLO", {"secret": "wrong-secret"}, b"")

    with patch("bridge.handshake.negotiate") as mock_neg:
        from bridge.handshake import Refused
        mock_neg.side_effect = Refused("bad_secret", "secret does not match")
        with patch.object(svc, "try_send_raw", new_callable=AsyncMock) as mock_raw:
            guest = await svc.attach(hello, writer)
            assert guest is None
            mock_raw.assert_called_once()
            err_frame = mock_raw.call_args[0][1]
            assert err_frame.headers["code"] == "bad_secret"


async def test_attach_busy(conn_service):
    svc = conn_service["service"]
    guest = conn_service["guest"]
    guest.link.connected = True
    svc.guest = guest

    writer = MagicMock()
    hello = Frame("HELLO", {"secret": "test-secret"}, b"")

    with patch("bridge.handshake.negotiate"):
        with patch.object(svc, "try_send_raw", new_callable=AsyncMock) as mock_raw:
            res = await svc.attach(hello, writer)
            assert res is None
            mock_raw.assert_called_once()
            err_frame = mock_raw.call_args[0][1]
            assert err_frame.headers["code"] == "busy"


async def test_attach_success(conn_service):
    svc = conn_service["service"]
    writer = MagicMock()
    hello = Frame("HELLO", {"secret": "test-secret"}, b"")

    neg_mock = MagicMock()
    neg_mock.page_bytes = 4096
    neg_mock.page_blocks = 32
    neg_mock.caps = MagicMock()
    neg_mock.caps.acp = "1252"
    neg_mock.caps.oemcp = "437"
    neg_mock.caps.summary.return_value = "caps"
    neg_mock.mem_kb = 65536

    with patch("bridge.handshake.negotiate", return_value=neg_mock):
        guest = await svc.attach(hello, writer)
        assert guest is not None
        assert svc.guest is guest
        conn_service["on_attach"].assert_called_once_with(guest, False)


def test_mark_lost(conn_service):
    svc = conn_service["service"]
    guest = conn_service["guest"]

    svc.mark_lost(guest)
    assert guest.lost_at is not None
    conn_service["on_lost"].assert_called_once_with(guest)


async def test_handle_frame(conn_service):
    svc = conn_service["service"]
    guest = conn_service["guest"]
    f = Frame("INPUT", {}, b"hello")

    await svc.handle(guest, f)
    conn_service["handle_frame"].assert_called_once_with(guest, f)


async def test_handle_deflate_frame(conn_service):
    svc = conn_service["service"]
    guest = conn_service["guest"]
    guest.link.zip_in.decompress.return_value = b"decompressed"

    f = Frame("INPUT", {"enc": "deflate"}, b"compressed")
    await svc.handle(guest, f)

    guest.link.zip_in.decompress.assert_called_once_with(b"compressed")
    called_frame = conn_service["handle_frame"].call_args[0][1]
    assert called_frame.body == b"decompressed"
    assert "enc" not in called_frame.headers
