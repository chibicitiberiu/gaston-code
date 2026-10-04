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
    guest.mode_id = "manual"
    guest.mode_before_plan = ""
    guest.grants = set()
    guest.prefs = None
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
    assert mock_guest.mode_id == "manual"

    await options.set_mode(mock_guest, "plan")
    assert mock_guest.mode_id == "plan"
    assert mock_guest.mode_before_plan == "manual"


@pytest.mark.asyncio
async def test_auto_asks_nothing_of_the_cli_so_haiku_can_have_it(
        mock_conn, mock_guest):
    """Execution modes are the desk's. The CLI's own `auto` refused Haiku
    (BACKLOG 18); this bridge's Auto never asks the CLI for anything, so
    there is nothing for a model to refuse."""
    mock_guest.model = "claude-haiku-4-5"
    mock_guest.claude = MagicMock()
    mock_guest.claude.set_permission_mode = AsyncMock(
        side_effect=Exception("auto mode unavailable for this model"))
    mock_guest.claude.server_models = AsyncMock(return_value=[])
    mock_guest.claude.server_commands = AsyncMock(return_value=[])
    options = OptionsService(connection=mock_conn, write_block=AsyncMock())

    await options.set_mode(mock_guest, "auto")

    assert mock_guest.mode_id == "auto"
    mock_guest.claude.set_permission_mode.assert_not_awaited()


@pytest.mark.asyncio
async def test_the_plan_boundary_is_the_one_place_the_cli_hears(
        mock_conn, mock_guest):
    mock_guest.claude = MagicMock()
    mock_guest.claude.set_permission_mode = AsyncMock()
    mock_guest.claude.server_models = AsyncMock(return_value=[])
    mock_guest.claude.server_commands = AsyncMock(return_value=[])
    options = OptionsService(connection=mock_conn, write_block=AsyncMock())

    await options.set_mode(mock_guest, "plan")
    mock_guest.claude.set_permission_mode.assert_awaited_once_with("plan")
    await options.set_mode(mock_guest, "auto")
    mock_guest.claude.set_permission_mode.assert_awaited_with("default")


@pytest.mark.asyncio
async def test_a_refused_plan_boundary_puts_the_mode_back(
        mock_conn, mock_guest, caplog):
    """BACKLOG 18's rule, at the one boundary left: a mode the CLI refuses
    must not stick here -- one warning line, the mode restored, the menu
    resent so the tick is where the mode really is."""
    mock_guest.model = "claude-haiku-4-5"
    mock_guest.claude = MagicMock()
    mock_guest.claude.set_permission_mode = AsyncMock(
        side_effect=Exception("plan mode unavailable"))
    mock_guest.claude.server_models = AsyncMock(return_value=[])
    mock_guest.claude.server_commands = AsyncMock(return_value=[])
    write_block = AsyncMock()
    options = OptionsService(connection=mock_conn, write_block=write_block)

    with caplog.at_level(logging.WARNING, logger="bridge.options"):
        await options.set_mode(mock_guest, "plan")

    assert mock_guest.mode_id == "manual"
    assert mock_guest.mode_before_plan == ""
    write_block.assert_awaited_once()
    assert mock_conn.send.await_count > 0
    assert not any(r.levelno >= logging.ERROR for r in caplog.records)
    relevant = [r for r in caplog.records if "refused" in r.message]
    assert relevant and "claude-haiku-4-5" in relevant[0].message


@pytest.mark.asyncio
async def test_changing_mode_drops_the_chats_grants(mock_conn, mock_guest):
    mock_guest.grants = {"run_work"}
    options = OptionsService(connection=mock_conn)
    await options.set_mode(mock_guest, "auto")
    assert mock_guest.grants == set()
