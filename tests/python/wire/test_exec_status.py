"""Unit tests for ExecStatus in bridge/status.py."""

import pathlib
import re

import pytest

from bridge.status import ExecStatus, EXEC_STATUS_PROSE
import conftest


def test_exec_status_parse_known():
    assert ExecStatus.parse("ok") == ExecStatus.OK
    assert ExecStatus.parse("timeout") == ExecStatus.TIMEOUT
    assert ExecStatus.parse("spawn_failed") == ExecStatus.SPAWN_FAILED
    assert ExecStatus.parse("cancelled") == ExecStatus.CANCELLED
    assert ExecStatus.parse("too_long") == ExecStatus.TOO_LONG
    assert ExecStatus.parse("busy") == ExecStatus.BUSY
    assert ExecStatus.parse("unknown") == ExecStatus.UNKNOWN


def test_exec_status_parse_unknown_falls_back_to_unknown():
    # Falling back to OK would misclassify errors as successes
    assert ExecStatus.parse("completely_unheard_of_status") == ExecStatus.UNKNOWN
    assert ExecStatus.parse("") == ExecStatus.UNKNOWN


def test_exec_status_is_error():
    assert not ExecStatus.OK.is_error
    assert ExecStatus.UNKNOWN.is_error
    assert ExecStatus.TIMEOUT.is_error
    assert ExecStatus.SPAWN_FAILED.is_error
    assert ExecStatus.CANCELLED.is_error
    assert ExecStatus.TOO_LONG.is_error
    assert ExecStatus.BUSY.is_error


def test_exec_status_prose_for_unknown():
    assert ExecStatus.UNKNOWN in EXEC_STATUS_PROSE
    assert "unrecognised" in EXEC_STATUS_PROSE[ExecStatus.UNKNOWN]


def test_exec_status_vocabulary_agreement():
    src = pathlib.Path(conftest.HERE, "win", "rpc", "rpc.cpp").read_text()

    found: set[str] = set()
    for m in re.finditer(r'\bstatus\s*=\s*"([a-z_]+)"', src):
        found.add(m.group(1))
    for m in re.finditer(r'(?:k|keys)\[1\]\s*=\s*"status";\s*(?:v|vals)\[1\]\s*=\s*"([a-z_]+)"',
                         src):
        found.add(m.group(1))
    assert found

    bridge_side = {s.value for s in ExecStatus}
    missing = found - bridge_side
    assert not missing, f"win/rpc/rpc.cpp sends unrecognised statuses: {missing}"
    extra = bridge_side - found
    assert not extra, f"ExecStatus defines unused statuses: {extra}"
