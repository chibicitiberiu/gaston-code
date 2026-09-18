"""The sync layer's own directory is not the agent's to rewrite.

`.gaston/` lives INSIDE the working directory on purpose: a conflict copy and
the ancestor beside it have to be readable, or a three-way merge has nothing to
merge from. `off_limits` refuses what is outside cwd, so that same placement
left the staged copies writable -- and a staged copy is the last remaining
version of work the user's edit displaced.
"""
import os

from bridge.agent.permissions import off_limits


def test_reading_a_conflict_copy_is_allowed(tmp_path):
    """The reason it is inside cwd at all."""
    wd = str(tmp_path)
    mine = os.path.join(wd, ".gaston", "conflict", "a.txt", "mine.1")
    assert off_limits("Read", {"file_path": mine}, wd) is None
    assert off_limits("Grep", {"path": os.path.join(wd, ".gaston")}, wd) is None


def test_writing_into_gaston_is_refused(tmp_path):
    wd = str(tmp_path)
    mine = os.path.join(wd, ".gaston", "conflict", "a.txt", "mine.1")
    for tool, key in (("Write", "file_path"), ("Edit", "file_path")):
        why = off_limits(tool, {key: mine}, wd)
        assert why is not None, f"{tool} into .gaston must be refused"
        # It must say where the merge DOES belong, or the agent retries.
        assert "sync root" in why


def test_an_ordinary_write_inside_cwd_is_still_allowed(tmp_path):
    wd = str(tmp_path)
    assert off_limits(
        "Write", {"file_path": os.path.join(wd, "machine", "a.txt")}, wd) is None


def test_a_name_merely_starting_with_gaston_is_not_refused(tmp_path):
    """commonpath, not startswith: `.gaston-notes` is an ordinary file."""
    wd = str(tmp_path)
    assert off_limits(
        "Write", {"file_path": os.path.join(wd, ".gaston-notes.md")}, wd) is None


def test_outside_cwd_is_still_refused_and_says_so_differently(tmp_path):
    wd = str(tmp_path / "work")
    os.makedirs(wd)
    why = off_limits("Read", {"file_path": "/etc/passwd"}, wd)
    assert why is not None and "on the proxy" in why
