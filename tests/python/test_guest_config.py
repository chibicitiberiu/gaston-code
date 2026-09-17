"""Every field of rc_config survives save -> load, checked by the guest's own code.

Two bugs of this shape have shipped. `font_size` was read by `rc_config_load`
and never written by `rc_config_save`, so it reset on every launch. The fix
added a comment predicting that a sync root would go the same way -- and it
did, in the mirror image: `save` wrote `[sync] root` and `load` never read it
back, so the guest ran with an empty sync root and the entire sync layer was
silently off. No error, no log line, nothing to grep for; the bridge just never
received a `SYNCDIRTY`.

Reading the two functions side by side did not catch either one. So this runs
the shipping `ini.c` under Wine against the real `GetPrivateProfileStringA` --
a Python reimplementation would only be testing the reimplementation, and the
profile API has its own quirks about defaults and missing sections, which is
where both bugs lived.
"""
from __future__ import annotations

import os
import subprocess

import pytest

HERE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BUILD = os.environ.get("BUILD", os.path.join(HERE, "build", "win"))
EXE = os.path.join(BUILD, "inicheck.exe")

WINE = os.environ.get(
    "WINE",
    os.path.expanduser(
        "~/.local/share/lutris/runners/wine/wine-ge-8-26-x86_64/bin/wine"),
)
PREFIX = os.path.join(HERE, "spike", ".wineprefix")

pytestmark = pytest.mark.skipif(
    not os.path.exists(EXE) or not os.path.exists(WINE),
    reason="needs build/win/inicheck.exe (make -C win inicheck) and the Lutris wine-ge runner",
)


def _run(workdir: str) -> subprocess.CompletedProcess:
    env = dict(os.environ, WINEPREFIX=PREFIX, WINEDEBUG="-all")
    win_dir = "Z:" + workdir.replace("/", "\\")
    return subprocess.run([WINE, EXE, win_dir], env=env,
                          capture_output=True, text=True, timeout=120)


def test_every_config_field_round_trips(tmp_path):
    out = _run(str(tmp_path))
    assert "FAILURES" not in out.stdout, out.stdout
    assert out.returncode == 0, out.stdout + out.stderr
    assert "all config fields round-trip" in out.stdout


def test_sync_root_specifically(tmp_path):
    """Named on its own, because this is the one that turned sync off."""
    out = _run(str(tmp_path))
    assert "ok   sync_root    C:\\WORK\\SUB" in out.stdout, out.stdout
