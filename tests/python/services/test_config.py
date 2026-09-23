"""Unit tests for BridgeConfig and directory containment validation."""

import os
import pytest
from bridge.services.config import BridgeConfig, DEFAULT_PORT


def test_config_defaults():
    cfg = BridgeConfig(secret="test-secret")
    assert cfg.secret == "test-secret"
    assert cfg.host == "127.0.0.1"
    assert cfg.port == DEFAULT_PORT
    assert cfg.sync_sweep_s == 120.0
    assert cfg.idle_timeout_s == 90.0
    assert cfg.idle_check_s == 15.0
    assert cfg.perm_grace_s == 60.0
    assert cfg.sync_dir is None
    assert cfg.sync_state is None


def test_config_validates_sync_state_outside_sync_dir(tmp_path):
    sync_dir = tmp_path / "sync"
    sync_dir.mkdir()
    inside_state = sync_dir / "state"

    cfg = BridgeConfig(secret="sec", sync_dir=str(sync_dir), sync_state=str(inside_state))
    with pytest.raises(ValueError, match="OUTSIDE --sync-dir"):
        cfg.validate()


def test_config_validates_sync_state_outside_cwd(tmp_path):
    cwd = tmp_path / "work"
    cwd.mkdir()
    sync_dir = tmp_path / "sync"
    sync_dir.mkdir()
    inside_cwd_state = cwd / "state"

    cfg = BridgeConfig(
        secret="sec",
        cwd=str(cwd),
        sync_dir=str(sync_dir),
        sync_state=str(inside_cwd_state),
    )
    with pytest.raises(ValueError, match="OUTSIDE --cwd"):
        cfg.validate()


def test_config_validates_clean_paths(tmp_path):
    sync_dir = tmp_path / "sync"
    sync_dir.mkdir()
    sync_state = tmp_path / "state"
    cwd = tmp_path / "cwd"
    cwd.mkdir()

    cfg = BridgeConfig(
        secret="sec",
        cwd=str(cwd),
        sync_dir=str(sync_dir),
        sync_state=str(sync_state),
    )
    cfg.validate()
    assert cfg.sync_dir == str(sync_dir.resolve())
    assert cfg.sync_state == str(sync_state.resolve())
