"""Every field of ClientConfig survives save -> load, checked by the guest's own code.

Two bugs of this shape have shipped. `font_size` was read by the loader
and never written by the saver, so it reset on every launch. The fix
added a comment predicting that a sync root would go the same way -- and it
did, in the mirror image: `save` wrote `[sync] root` and `load` never read it
back, so the guest ran with an empty sync root and the entire sync layer was
silently off. No error, no log line, nothing to grep for; the bridge just never
received a `SYNCDIRTY`.

Reading the two functions side by side did not catch either one. So this runs
the shipping `app/config.cpp` (over libcore's `IniFile`) under Wine against the real `GetPrivateProfileStringA` --
a Python reimplementation would only be testing the reimplementation, and the
profile API has its own quirks about defaults and missing sections, which is
where both bugs lived.
"""

import subprocess

import conftest

EXE_NAME = "inicheck.exe"

# Only the genuinely-unsupported machine skips. A missing binary with
# the toolchain present is a build omission and fails loudly -- see
# conftest.guest_exe.
pytestmark = conftest.needs_guest


def _run(workdir: str) -> subprocess.CompletedProcess:
    return conftest.run_guest(EXE_NAME, conftest.wine_path(workdir),
                              text=True, timeout=120)


def test_every_config_field_round_trips(tmp_path):
    out = _run(str(tmp_path))
    assert "FAILURES" not in out.stdout, out.stdout
    assert out.returncode == 0, out.stdout + out.stderr
    assert "all config fields round-trip" in out.stdout


def test_sync_root_specifically(tmp_path):
    """Named on its own, because this is the one that turned sync off."""
    out = _run(str(tmp_path))
    assert "ok   sync_root    C:\\WORK\\SUB" in out.stdout, out.stdout


def test_a_value_present_but_wrong_refuses_startup(tmp_path):
    """GetPrivateProfileIntA read `port=78g0` as 78 and connected there.
    ConfigStore refuses, naming the key, and so does a typo'd key."""
    out = _run(str(tmp_path))
    assert "ok   bad-port" in out.stdout, out.stdout
    assert "ok   typo-key" in out.stdout, out.stdout


def test_an_old_ini_with_a_retired_key_still_loads(tmp_path):
    """font_size was written by older clients and real INIs still carry it."""
    out = _run(str(tmp_path))
    assert "ok   retired-key" in out.stdout, out.stdout


def test_screen_control_is_a_setting_and_defaults_to_on(tmp_path):
    """BACKLOG.md 19. ON for a missing key: the five screen ops have shipped
    since Phase 6, so defaulting it off would take them away from every
    install that upgrades, with nothing anywhere saying why the agent had
    stopped being able to see the screen."""
    out = _run(str(tmp_path))
    assert "ok   automation" in out.stdout, out.stdout
    assert "ok   automation-off" in out.stdout, out.stdout


def test_an_unusable_automation_value_refuses_startup(tmp_path):
    """CLAUDE.md: an invalid value refuses startup. Falling back to the
    default on `automation=maybe` would be the permissive reading arrived at
    in silence, for the one setting whose entire purpose is to say no."""
    out = _run(str(tmp_path))
    assert "ok   automation-invalid" in out.stdout, out.stdout


def test_the_options_settings_round_trip_and_refuse(tmp_path):
    """The Options dialog's settings, the same mechanical way: lists as
    numbered keys (the profile API cannot hold two of one name), a mode as
    its own section, a shortened list leaving no tail and a deleted mode no
    orphan section, and the bad values refused by name."""
    out = _run(str(tmp_path))
    for line in ("ok   ignore2", "ok   protect+", "ok   danger+",
                 "ok   trust-", "ok   mode.values", "ok   shrunk",
                 "ok   mode-gone", "ok   no-orphan", "ok   unnumbered",
                 "ok   bad-mode-value"):
        assert line in out.stdout, (line, out.stdout)
