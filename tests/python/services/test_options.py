"""Unit tests for OptionsService."""

import logging
from unittest.mock import AsyncMock, MagicMock

import pytest

from bridge.services.config import BridgeConfig
from bridge.services.options import OptionsService


@pytest.fixture
def mock_conn():
    conn = MagicMock()
    conn.send = AsyncMock(return_value=True)
    return conn


@pytest.fixture
def mock_guest():
    guest = MagicMock()
    guest.claude = None
    guest.model = "claude-3-5-sonnet"
    guest.effort = "high"
    guest.perm_mode = "default"
    guest.work_guest = "C:\\WORK\\TEST"
    guest.conv_id = "test-conv-id"
    return guest


def test_effective_model_and_effort(mock_conn, mock_guest):
    config = BridgeConfig(secret="sec", model="haiku", effort="low")
    options = OptionsService(connection=mock_conn, config=config)

    assert options.effective_model(mock_guest) == "claude-3-5-sonnet"
    assert options.effective_effort(mock_guest) == "high"

    # When guest model is unset, fallback to config
    mock_guest.model = ""
    mock_guest.effort = ""
    assert options.effective_model(mock_guest) == "haiku"
    assert options.effective_effort(mock_guest) == "low"


@pytest.mark.asyncio
async def test_set_mode_validates(mock_conn, mock_guest):
    options = OptionsService(connection=mock_conn)
    await options.set_mode(mock_guest, "invalid_mode")
    assert mock_guest.perm_mode == "default"

    await options.set_mode(mock_guest, "plan")
    assert mock_guest.perm_mode == "plan"


@pytest.mark.asyncio
async def test_set_mode_refusal_reverts_perm_mode(mock_conn, mock_guest):
    """BACKLOG 18: a mode the model cannot take must not stick client-side.

    Haiku refusing Auto used to leave `guest.perm_mode` at "auto" while the
    live session stayed in "default" -- the guest's menu and the bridge then
    disagreed about what was actually running. The fix puts `perm_mode` back
    to the mode that was in force before the refused change.
    """
    mock_guest.model = "claude-haiku-4-5"
    mock_guest.perm_mode = "default"
    mock_guest.claude = MagicMock()
    mock_guest.claude.set_permission_mode = AsyncMock(
        side_effect=Exception(
            "Cannot set permission mode to auto: auto mode unavailable for this model"
        )
    )
    mock_guest.claude.server_models = AsyncMock(return_value=[])
    mock_guest.claude.server_commands = AsyncMock(return_value=[])

    write_block = AsyncMock()
    options = OptionsService(connection=mock_conn, write_block=write_block)

    await options.set_mode(mock_guest, "auto")

    assert mock_guest.perm_mode == "default"
    write_block.assert_awaited_once()
    note = write_block.call_args[0][1]
    assert "Auto" in note

    # The guest is resent the (now-reverted) menu so it stops showing a tick
    # beside a mode that was never applied.
    assert mock_conn.send.await_count > 0


@pytest.mark.asyncio
async def test_set_mode_refusal_logs_one_line_no_traceback(mock_conn, mock_guest, caplog):
    """A model/mode mismatch is an expected outcome, not a crash: one
    warning line naming the model and the mode, no ERROR, no traceback."""
    mock_guest.model = "claude-haiku-4-5"
    mock_guest.perm_mode = "default"
    mock_guest.claude = MagicMock()
    mock_guest.claude.set_permission_mode = AsyncMock(
        side_effect=Exception(
            "Cannot set permission mode to auto: auto mode unavailable for this model"
        )
    )
    mock_guest.claude.server_models = AsyncMock(return_value=[])
    mock_guest.claude.server_commands = AsyncMock(return_value=[])

    options = OptionsService(connection=mock_conn, write_block=AsyncMock())

    with caplog.at_level(logging.WARNING, logger="bridge.options"):
        await options.set_mode(mock_guest, "auto")

    assert not any(r.levelno >= logging.ERROR for r in caplog.records)
    assert not any(r.exc_info for r in caplog.records)
    relevant = [r for r in caplog.records if "refused" in r.message]
    assert relevant, caplog.records
    assert "claude-haiku-4-5" in relevant[0].message
    assert "auto" in relevant[0].message
