"""Finding the user's Claude Code, and refusing clearly when there is none.

Once the release stopped carrying a CLI, "where is it and is it new
enough" became the most likely failure in the product -- and the old
failure mode was not a bad message but an exception out of the attach
path, because the agent session is built in `Bridge._post_attach` with no
`try` there or at the caller.

The two behaviours worth pinning are both measured rather than reasoned:

FIRST PASS WINS. The author's box carries /usr/local/bin/claude at 1.0.33
from an npm install of June 2025 and ~/.local/bin/claude at 2.1.310, and
/usr/local/bin sorts FIRST in the SDK's order. `claude -v` is 10 ms on the
native binary and 820 ms on the npm one, which is a Node script -- and
that one is not read-only either: it rewrites ~/.claude.json. Probing
every candidate would be seconds of startup AND would stamp on the user's
live Claude Code config on every bridge start.

EVERY CANDIDATE IS REPORTED WHEN NONE WORKS. Same box, same reason: run
off PATH, a faithful search reaches 1.0.33 first. A bare refusal would
name it and stop, while a working copy sat on the same disk.
"""

from __future__ import annotations

import stat

import pytest

from bridge import claudecli


def _fake_cli(tmp_path, name, version):
    """An executable that answers `-v` the way Claude Code does."""
    p = tmp_path / name
    p.write_text("#!/bin/sh\necho '%s (Claude Code)'\n" % version)
    p.chmod(p.stat().st_mode | stat.S_IXUSR)
    return str(p)


@pytest.fixture
def nowhere(monkeypatch):
    """No bundled copy, nothing on PATH, no known location: a bare machine."""
    monkeypatch.setattr(claudecli, "_bundled", lambda: None)
    monkeypatch.setattr(claudecli.shutil, "which", lambda _n: None)
    monkeypatch.setattr(claudecli, "_known_locations", list)


# --- order ------------------------------------------------------------------

def test_the_bundled_copy_is_preferred(tmp_path, nowhere, monkeypatch):
    """The documented fat install keeps working unchanged: somebody who
    wants one artefact and no prerequisite still gets the SDK's own copy."""
    bundled = _fake_cli(tmp_path, "bundled", "2.1.300")
    onpath = _fake_cli(tmp_path, "onpath", "2.1.310")
    monkeypatch.setattr(claudecli, "_bundled", lambda: bundled)
    monkeypatch.setattr(claudecli.shutil, "which", lambda _n: onpath)
    assert claudecli.resolve().path == bundled


def test_path_beats_a_known_location(tmp_path, nowhere, monkeypatch):
    onpath = _fake_cli(tmp_path, "onpath", "2.1.310")
    known = _fake_cli(tmp_path, "known", "2.1.310")
    monkeypatch.setattr(claudecli.shutil, "which", lambda _n: onpath)
    monkeypatch.setattr(claudecli, "_known_locations",
                        lambda: [type(tmp_path)(known)])
    found = claudecli.resolve()
    assert found.path == onpath
    assert found.source == "on PATH"


def test_one_file_found_twice_is_probed_once(tmp_path, nowhere, monkeypatch):
    """`which` and the location list find the same file on this box. The
    FIRST spelling is kept -- the symlink, not its target -- because a
    native install repoints ~/.local/bin/claude at every self-update, and
    pinning the resolved file would freeze a long-running bridge to a
    version the user has since replaced."""
    cli = _fake_cli(tmp_path, "claude", "2.1.310")
    monkeypatch.setattr(claudecli.shutil, "which", lambda _n: cli)
    monkeypatch.setattr(claudecli, "_known_locations",
                        lambda: [type(tmp_path)(cli)])
    assert claudecli.candidates() == [("on PATH", cli)]


# --- first pass wins --------------------------------------------------------

def test_a_later_candidate_is_never_probed(tmp_path, nowhere, monkeypatch):
    """Not a micro-optimisation. The second candidate on the author's box
    takes 820 ms and rewrites ~/.claude.json as a side effect."""
    good = _fake_cli(tmp_path, "good", "2.1.310")
    later = _fake_cli(tmp_path, "later", "2.1.311")
    monkeypatch.setattr(claudecli, "_bundled", lambda: good)
    monkeypatch.setattr(claudecli.shutil, "which", lambda _n: later)

    probed = []
    real = claudecli.probe
    monkeypatch.setattr(claudecli, "probe",
                        lambda p: (probed.append(p), real(p))[1])

    assert claudecli.resolve().path == good
    assert probed == [good]


def test_an_old_first_candidate_does_not_stop_the_search(tmp_path, nowhere,
                                                         monkeypatch):
    """The author's box, exactly: /usr/local/bin/claude is 1.0.33 and sorts
    ahead of the 2.1.310 in ~/.local/bin. The old one is skipped, not
    refused on."""
    old = _fake_cli(tmp_path, "old", "1.0.33")
    new = _fake_cli(tmp_path, "new", "2.1.310")
    monkeypatch.setattr(claudecli, "_bundled", lambda: old)
    monkeypatch.setattr(claudecli.shutil, "which", lambda _n: new)
    assert claudecli.resolve().path == new


# --- the version floor ------------------------------------------------------

def test_versions_compare_as_numbers_not_strings(tmp_path, nowhere,
                                                 monkeypatch):
    """"2.1.310" < "2.1.49" lexically, so a string comparison would refuse a
    current CLI as too old."""
    assert claudecli.probe(_fake_cli(tmp_path, "a", "2.1.310")) == (2, 1, 310)
    assert claudecli.probe(_fake_cli(tmp_path, "b", "2.1.49")) == (2, 1, 49)
    assert (2, 1, 310) > (2, 1, 49)


def test_everything_too_old_reports_every_candidate(tmp_path, nowhere,
                                                    monkeypatch):
    old = _fake_cli(tmp_path, "old", "1.0.33")
    older = _fake_cli(tmp_path, "older", "1.9.9")
    monkeypatch.setattr(claudecli, "_bundled", lambda: old)
    monkeypatch.setattr(claudecli.shutil, "which", lambda _n: older)
    with pytest.raises(claudecli.ClaudeCliError) as e:
        claudecli.resolve()
    message = str(e.value)
    assert old in message and older in message
    assert "1.0.33" in message and "1.9.9" in message


def test_nothing_at_all_says_so_and_says_how_to_install(nowhere):
    with pytest.raises(claudecli.ClaudeCliError) as e:
        claudecli.resolve()
    assert "install.sh" in str(e.value) or "install.ps1" in str(e.value)


# --- an explicit setting is never a fallback --------------------------------

def test_a_configured_path_that_does_not_exist_is_an_error(tmp_path, nowhere):
    with pytest.raises(claudecli.ClaudeCliError):
        claudecli.resolve(str(tmp_path / "nope"))


def test_a_configured_path_that_is_a_directory_is_an_error(tmp_path, nowhere):
    """PermissionError, not CalledProcessError -- which is why `probe`
    catches OSError. The SDK's own location list filters on `is_file()`
    with no X_OK check, so it hands back things that cannot be run."""
    with pytest.raises(claudecli.ClaudeCliError):
        claudecli.resolve(str(tmp_path))


def test_a_configured_path_that_is_not_executable_is_an_error(tmp_path,
                                                              nowhere):
    p = tmp_path / "notexec"
    p.write_text("#!/bin/sh\necho '2.1.310 (Claude Code)'\n")
    p.chmod(p.stat().st_mode & ~stat.S_IXUSR & ~stat.S_IXGRP & ~stat.S_IXOTH)
    with pytest.raises(claudecli.ClaudeCliError):
        claudecli.resolve(str(p))


def test_a_configured_path_too_old_is_an_error_not_a_search(tmp_path, nowhere,
                                                            monkeypatch):
    """Searching past a wrong explicit setting would run a CLI the user did
    not name, which is the failure --claude-cli exists to rule out."""
    good = _fake_cli(tmp_path, "good", "2.1.310")
    monkeypatch.setattr(claudecli, "_bundled", lambda: good)
    with pytest.raises(claudecli.ClaudeCliError) as e:
        claudecli.resolve(_fake_cli(tmp_path, "old", "1.0.33"))
    assert good not in str(e.value)


def test_a_good_configured_path_is_taken(tmp_path, nowhere):
    cli = _fake_cli(tmp_path, "mine", "2.1.310")
    found = claudecli.resolve(cli)
    assert (found.path, found.version, found.source) == (
        cli, (2, 1, 310), "--claude-cli")


# --- what the probe will not accept -----------------------------------------

@pytest.mark.parametrize("output", ["", "not a version", "claude 2.1.310"])
def test_output_that_is_not_a_version_is_not_a_version(tmp_path, output):
    p = tmp_path / "odd"
    p.write_text("#!/bin/sh\necho %s\n" % (output or "''"))
    p.chmod(p.stat().st_mode | stat.S_IXUSR)
    assert claudecli.probe(str(p)) is None


def test_the_probe_does_not_hang_on_a_cli_that_waits(tmp_path, monkeypatch):
    """stdin is closed, so a CLI that decides to prompt gets EOF rather than
    holding the bridge until the timeout."""
    p = tmp_path / "reader"
    p.write_text("#!/bin/sh\nread _line\necho '2.1.310 (Claude Code)'\n")
    p.chmod(p.stat().st_mode | stat.S_IXUSR)
    assert claudecli.probe(str(p)) == (2, 1, 310)


def test_the_floor_matches_what_the_sdk_documents():
    """Hardcoded here on purpose -- MINIMUM_CLAUDE_CODE_VERSION lives three
    levels into _internal, in a 1,200-line module importing drags in at
    startup -- so something has to notice if it moves."""
    from claude_agent_sdk._internal.transport import subprocess_cli as sdk
    parts = tuple(int(x) for x in sdk.MINIMUM_CLAUDE_CODE_VERSION.split("."))
    assert parts == claudecli.MINIMUM


def test_the_sdk_reports_what_it_was_built_against():
    assert claudecli.sdk_cli_version() is not None
