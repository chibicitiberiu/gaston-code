"""The guide and the things it describes must say the same numbers.

THE GUIDE IS THE NINTH STATEMENT of how to install this. The others are
README.md, the two generated README.TXTs, the two generated ini files and
their comments, release.yml's release body, and pyproject.toml's metadata.
`grep -rl mkrelease tests/` returned nothing before this file: not one of
them was pinned to any other, and THREE were already wrong in the same
way -- each said claude-agent-sdk carried Claude Code so nobody had to
install it, which stopped being true and stayed written down.

That is the failure this file exists for. It does not check prose; it
checks the handful of facts a reader will type in and be wrong about if
two documents disagree: the port, the install directory, the placeholder
in the shipped ini, and the client's own filename.

Modelled on test_guest_css.py, which pins a string in one file against a
string in another for the same reason.
"""

from __future__ import annotations

import os
import re

import conftest
import pytest

GUIDE = os.path.join(conftest.REPO, "docs", "GUIDE.md")
MKRELEASE = os.path.join(conftest.REPO, "tools", "gen", "mkrelease.sh")
CLIENT_CFG = os.path.join(conftest.REPO, "win", "app", "config.cpp")
BRIDGE_CFG = os.path.join(conftest.REPO, "bridge", "services", "config.py")


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


@pytest.fixture(scope="module")
def guide():
    return _read(GUIDE)


def test_the_port_is_the_same_number_in_all_four_places(guide):
    """A reader who types the guide's port into gaston.ini and finds the
    bridge on another one has no way to tell which is wrong."""
    bridge = re.search(r"^DEFAULT_PORT = (\d+)", _read(BRIDGE_CFG), re.M)
    client = re.search(r"DEFAULT_PORT = (\d+)", _read(CLIENT_CFG))
    shipped = re.search(r"^port=(\d+)", _read(MKRELEASE), re.M)
    assert bridge and client and shipped
    port = bridge.group(1)
    assert client.group(1) == port, "win/app/config.cpp disagrees"
    assert shipped.group(1) == port, "the ini mkrelease.sh writes disagrees"
    assert "port=%s" % port in guide, "docs/GUIDE.md disagrees"


def test_the_install_directory_is_the_same_in_both_documents(guide):
    r"""The guide says to make C:\GASTON and the generated README.TXT says
    to make C:\GASTON, or one of them sends somebody to an empty folder."""
    assert r"MD C:\GASTON" in guide
    assert r"MD C:\GASTON" in _read(MKRELEASE)


def test_the_guide_names_the_placeholder_the_ini_actually_ships(guide):
    """`secret=change-me` is what mkrelease.sh writes. A guide that named a
    different placeholder would have people editing a line that is not
    there."""
    shipped = re.search(r"^secret=(\S+)", _read(MKRELEASE), re.M)
    assert shipped, "mkrelease.sh no longer writes a secret= line"
    assert shipped.group(1) in guide


def test_the_guide_names_the_files_that_must_keep_long_names(guide):
    """tools_detect.ini has no 8.3 form at all, and a transfer that
    shortens it breaks tool detection silently. Both documents warn; this
    keeps the list from drifting apart."""
    for name in ("view.css", "view.tpl", "tools_detect.ini"):
        assert name in guide, name
        assert name in _read(MKRELEASE), name


def test_the_guide_does_not_promise_a_bundled_claude_code(guide):
    """The exact prose failure this file was written for. Claude Code is a
    prerequisite now; anything that says otherwise is the old design
    outliving the code again."""
    lowered = guide.lower()
    for claim in ("bundled", "nothing extra to install", "carries claude code"):
        assert claim not in lowered, claim


def test_every_image_the_guide_shows_is_tracked(guide):
    """docs/images/ is tracked precisely because tmp/ was not and
    `make clean` would have taken them."""
    for name in re.findall(r"!\[[^\]]*\]\(images/([^)]+)\)", guide):
        path = os.path.join(conftest.REPO, "docs", "images", name)
        assert os.path.isfile(path), name


def test_the_guide_names_the_version_that_is_being_released(guide):
    """The guide writes asset filenames out in full -- `pipx install
    gaston_code_bridge-0.4.1-py3-none-any.whl` -- because a reader types
    them. That makes it the ninth place the version lives, and the one
    nobody thinks to bump: a guide telling people to install a file the
    release page does not have is worse than no guide.
    """
    version = re.search(r'^version = "([^"]+)"',
                        _read(os.path.join(conftest.REPO, "pyproject.toml")),
                        re.M)
    assert version, "pyproject.toml has no version"
    stale = re.findall(r"gaston[-_][a-z_]*-(\d+\.\d+\.\d+)", guide)
    assert stale, "the guide names no versioned asset at all any more"
    wrong = sorted({v for v in stale if v != version.group(1)})
    assert not wrong, (
        "docs/GUIDE.md names version(s) %s but pyproject.toml says %s"
        % (wrong, version.group(1)))


def test_the_guide_install_line_asks_for_the_source_sdk(guide):
    """Without the variable the user downloads 219 MB of Claude Code they
    already have, and the SDK runs that copy instead of theirs -- which is
    the entire point of the release this guide documents."""
    assert "PIP_NO_BINARY=claude-agent-sdk" in guide
    assert "UV_NO_BINARY_PACKAGE" in guide
