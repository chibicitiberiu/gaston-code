"""Unit tests for WorkspaceService."""

import pytest
from unittest.mock import MagicMock
from bridge.services.config import BridgeConfig
from bridge.services.workspace import WorkspaceService


@pytest.fixture
def mock_guest():
    guest = MagicMock()
    guest.hello = {"sync_root": "C:\\WORK"}
    guest.work_dir = "/tmp/mirror/NOTES"
    guest.work_guest = "C:\\WORK\\NOTES"
    guest.conv_id = "test-conv-id"
    return guest


def test_work_dir_fallback(mock_guest):
    cfg = BridgeConfig(secret="sec", cwd="/base/cwd")
    ws = WorkspaceService(config=cfg)

    # When guest has work_dir
    assert ws.work_dir(mock_guest) == "/tmp/mirror/NOTES"

    # When guest has no work_dir, fallback to config.cwd
    mock_guest.work_dir = ""
    assert ws.work_dir(mock_guest) == "/base/cwd"


def test_guest_path_mapping(mock_guest):
    cfg = BridgeConfig(secret="sec")
    ws = WorkspaceService(config=cfg)

    assert ws.guest_path(mock_guest, "SUB") == "C:\\WORK\\SUB"
    assert ws.guest_work_root(mock_guest) == "C:\\WORK\\NOTES"


def test_sub_of(mock_guest):
    cfg = BridgeConfig(secret="sec", sync_dir="/tmp/mirror")
    ws = WorkspaceService(config=cfg)

    assert ws.sub_of(mock_guest) == "NOTES"
