"""Unit tests for OptionsService."""

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
