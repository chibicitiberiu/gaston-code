"""The three-way state table, exhaustively.

Every row of the documented table, plus the cases five review rounds found
missing from it. Where a test's name says "silently", that is the failure it
prevents: the sync layer losing a real person's work with nothing reported.
"""

import json
import os
import time

import pytest

from bridge import sync as S
from bridge import paths as P
from bridge.sync import (Act, Baseline, Decision, Entry, Tomb, Type, diff,
                         order)


def f(crc, cls=P.CLASS_TEXT, display="", mtime=0.0):
    return Entry(type=Type.FILE, cls=cls, crc=crc, size=10, mtime=mtime,
                 display=display)


def d():
    return Entry(type=Type.DIR)


def only(guest, bridge, base=None):
    base = base or Baseline()
    got = diff(guest, bridge, base)
    assert len(got) == 1, got
    return got[0]


# --- the ordinary rows ------------------------------------------------------

def test_unchanged_does_nothing():
    b = Baseline(rows={"a": f(1)})
    assert only({"a": f(1)}, {"a": f(1)}, b).act is Act.NOTHING


def test_guest_moved_pulls():
    b = Baseline(rows={"a": f(1)})
    assert only({"a": f(2)}, {"a": f(1)}, b).act is Act.PULL


def test_bridge_moved_pushes():
    b = Baseline(rows={"a": f(1)})
    assert only({"a": f(1)}, {"a": f(2)}, b).act is Act.PUSH


def test_both_moved_the_same_way_adopts_without_transferring():
    b = Baseline(rows={"a": f(1)})
    assert only({"a": f(2)}, {"a": f(2)}, b).act is Act.ADOPT


def test_both_moved_differently_conflicts():
    b = Baseline(rows={"a": f(1)})
    assert only({"a": f(2)}, {"a": f(3)}, b).act is Act.CONFLICT


def test_new_on_one_side_transfers():
    assert only({"a": f(1)}, {}).act is Act.PULL
    assert only({}, {"a": f(1)}).act is Act.PUSH


def test_first_sync_collision_is_a_conflict_not_a_coin_flip():
    """Both present, different, no ancestor. There is genuinely no direction to
    derive, so it must not be guessed."""
    dec = only({"a": f(1)}, {"a": f(2)})
    assert dec.act is Act.CONFLICT
    assert "no common ancestor" in dec.reason


def test_same_content_no_ancestor_just_adopts():
    assert only({"a": f(1)}, {"a": f(1)}).act is Act.ADOPT


# --- the stale editor buffer: the likeliest real loss ----------------------

def test_a_reverted_push_is_a_conflict_not_a_pull():
    """Notepad does not notice external changes. The user has main.c open, the
    agent's edit is pushed, then the user saves their STALE buffer.

    That reads as an ordinary guest edit -- pull, no conflict -- and the agent's
    work vanishes from guest, bridge, baseline AND base/, with nobody told.
    `if_crc` cannot help: the file genuinely had not changed when the push
    landed.
    """
    b = Baseline(rows={"a": f(2)}, prev={"a": f(1)})   # push replaced crc 1 with 2
    dec = only({"a": f(1)}, {"a": f(2)}, b)            # guest is back at 1
    assert dec.act is Act.CONFLICT
    assert "reverted a push" in dec.reason


def test_a_genuine_guest_edit_after_a_push_still_pulls():
    """The detector must not fire on ordinary work."""
    b = Baseline(rows={"a": f(2)}, prev={"a": f(1)})
    assert only({"a": f(3)}, {"a": f(2)}, b).act is Act.PULL


# --- deletions --------------------------------------------------------------

def test_deleted_on_both_sides_tombstones():
    b = Baseline(rows={"a": f(1)})
    assert only({}, {}, b).act is Act.TOMBSTONE


def test_guest_deleted_but_bridge_still_has_it_reports_and_does_NOT_tombstone():
    """A tombstone here would make crc(B) == T.crc read as 'untouched',
    quietly freezing a live file the agent keeps editing."""
    b = Baseline(rows={"a": f(1)})
    dec = only({}, {"a": f(1)}, b)
    assert dec.act is Act.REPORT
    assert dec.act is not Act.TOMBSTONE


def test_bridge_deleted_half_of_a_rename_never_resurrects():
    """An agent's `git mv` produces this. Resurrecting on the bridge undoes the
    delete; deleting on the guest destroys the user's file. Reporting is the
    only honest answer."""
    b = Baseline(rows={"a": f(1)})
    dec = only({"a": f(1)}, {}, b)
    assert dec.act is Act.REPORT
    assert "rename" in dec.reason


def test_bridge_deleted_AND_guest_edited_is_reported_distinctly():
    b = Baseline(rows={"a": f(1)})
    dec = only({"a": f(9)}, {}, b)
    assert dec.act is Act.REPORT
    assert "edited on the guest" in dec.reason


# --- tombstones -------------------------------------------------------------

def test_tombstone_stays_put_when_nothing_returns():
    b = Baseline(tombs={"a": Tomb(cls=P.CLASS_TEXT, crc=1)})
    assert only({}, {}, b).act is Act.NOTHING


def test_edited_after_deletion_is_visible_thanks_to_the_tombstone_crc():
    """Without the CRC, 'untouched since the deletion' and 'edited since the
    deletion' are identical, and the edit is silently never synced."""
    b = Baseline(tombs={"a": Tomb(cls=P.CLASS_TEXT, crc=1)})
    assert only({"a": f(1)}, {}, b).act is Act.NOTHING     # the deleted content
    assert only({"a": f(2)}, {}, b).act is Act.PULL        # someone edited it
    assert only({}, {"a": f(2)}, b).act is Act.PUSH        # on the other side


def test_recreated_identically_on_both_clears_the_tombstone():
    b = Baseline(tombs={"a": Tomb(cls=P.CLASS_TEXT, crc=1)})
    assert only({"a": f(5)}, {"a": f(5)}, b).act is Act.CLEAR_TOMBSTONE


def test_recreated_differently_on_both_conflicts():
    b = Baseline(tombs={"a": Tomb(cls=P.CLASS_TEXT, crc=1)})
    assert only({"a": f(5)}, {"a": f(6)}, b).act is Act.CONFLICT


# --- types ------------------------------------------------------------------

def test_file_becoming_a_directory_is_a_conflict_not_an_overwrite():
    """Without a type field this compares as 'differs' and tries to overwrite a
    directory with a file."""
    b = Baseline(rows={"a": f(1)})
    dec = only({"a": d()}, {"a": f(1)}, b)
    assert dec.act is Act.CONFLICT
    assert "file <-> directory" in dec.reason


def test_a_directory_only_on_the_bridge_is_created():
    assert only({}, {"a": d()}).act is Act.MKDIR


def test_an_unreadable_file_is_skipped_not_treated_as_absent():
    """Treating a locked file as absent makes it a tombstone, and then the
    user's later edits are invisible forever."""
    b = Baseline(rows={"a": f(1)})
    dec = only({"a": Entry(type=Type.UNREADABLE)}, {"a": f(1)}, b)
    assert dec.act is Act.SKIP
    # Not "locked": that is one cause and the only transient one. The machine
    # also reports `u` for a name it can enumerate but never open, and naming
    # a lock sends the reader after a process that does not exist.
    assert dec.reason is S.UNREADABLE
    assert dec.reason is not S.IGNORED


def test_ignored_paths_freeze_rather_than_looking_deleted():
    """Editing .gitignore is routine. A path leaving the scan set must not read
    as a mass deletion."""
    b = Baseline(ignored={"a": f(1)})
    assert only({}, {}, b).act is Act.SKIP


# --- class changes ----------------------------------------------------------

def test_a_class_change_is_a_content_change_not_a_freeze():
    """Save As > Unicode. The class differs, so (type, class, crc) differs, so
    it resolves by the ordinary rules instead of freezing the path forever."""
    b = Baseline(rows={"a": f(1, cls=P.CLASS_TEXT)})
    dec = only({"a": f(1, cls=P.CLASS_EXACT)}, {"a": f(1, cls=P.CLASS_TEXT)}, b)
    assert dec.act is Act.PULL


def test_same_crc_different_class_is_not_the_same_content():
    assert not f(1, cls=P.CLASS_TEXT).same_content(f(1, cls=P.CLASS_EXACT))


def test_size_and_mtime_never_affect_cross_side_equality():
    """A uniform text file has different sizes on the two sides by construction;
    comparing them reads every text file as changed, every pass."""
    assert f(1, display="a").same_content(
        Entry(type=Type.FILE, cls=P.CLASS_TEXT, crc=1, size=999, mtime=12345))


# --- the settle window ------------------------------------------------------

def test_a_freshly_written_file_is_not_cacheable():
    """Gating the cache LOOKUP is not enough. A read taken while the 2-second
    FAT tick is still open can be superseded by a second save in that tick at
    the same size, so the entry must never be STORED."""
    assert not f(1, mtime=time.time()).cacheable
    assert not f(1, mtime=time.time() - 1.0).cacheable
    assert f(1, mtime=time.time() - 60).cacheable


def test_a_future_mtime_is_never_cacheable():
    """A Win98 box with a dead CMOS battery has a skewed clock; a forward skew
    would otherwise make every file look settled and restore the bug."""
    assert not f(1, mtime=time.time() + 3600).cacheable


# --- persistence ------------------------------------------------------------

def test_baseline_round_trips(tmp_path):
    p = str(tmp_path / "state" / "base.json")
    b = Baseline(rows={"a": f(1, display="A.txt")},
                 tombs={"g": Tomb(cls="t", crc=7, reported=True)},
                 prev={"a": f(9)}, tree_id="VOL-1234", generation=5)
    b.save(p)
    back = Baseline.load(p)
    assert back.rows["a"].crc == 1 and back.rows["a"].display == "A.txt"
    assert back.tombs["g"].crc == 7 and back.tombs["g"].reported
    assert back.prev["a"].crc == 9
    assert back.tree_id == "VOL-1234" and back.generation == 5


def test_save_keeps_the_previous_generation(tmp_path):
    p = str(tmp_path / "base.json")
    Baseline(generation=1).save(p)
    Baseline(generation=2).save(p)
    assert Baseline.load(p).generation == 2
    assert Baseline.load(p + ".prev").generation == 1


def test_a_corrupt_baseline_raises_rather_than_reading_as_empty(tmp_path):
    """'No baseline' is a first sync, which conflicts the entire tree. Refusing
    to run is the safe failure; guessing is not."""
    p = str(tmp_path / "base.json")
    Baseline(rows={"a": f(1)}).save(p)
    with open(p, "w") as fh:
        fh.write('{"rows": {"a": ')          # truncated mid-write
    with pytest.raises(json.JSONDecodeError):
        Baseline.load(p)


def test_a_missing_baseline_is_simply_empty(tmp_path):
    assert Baseline.load(str(tmp_path / "nope.json")).rows == {}


# --- ordering ---------------------------------------------------------------

def test_conflicts_are_staged_before_transfers_touch_the_path():
    got = order([Decision("z", Act.PUSH), Decision("a", Act.CONFLICT)])
    assert got[0].act is Act.CONFLICT


def test_directories_are_created_before_files_land_in_them():
    """CreateFileA into a missing directory fails on Win98, so a pass that
    pushes children first fails identically every time."""
    got = order([Decision("a/b/c.txt", Act.PUSH), Decision("a/b", Act.MKDIR)])
    assert got[0].act is Act.MKDIR


def test_parents_are_created_before_children():
    got = order([Decision("a/b/c", Act.MKDIR), Decision("a", Act.MKDIR),
                 Decision("a/b", Act.MKDIR)])
    assert [g.key for g in got] == ["a", "a/b", "a/b/c"]


def test_an_unreadable_directory_does_not_delete_its_whole_subtree():
    """A directory the machine cannot list has no descendants in that manifest
    -- it could not enumerate what is inside it either.

    Read naively, every file under it is absent, which is row 12 once per
    file: "deleted on the guest, kept here". One directory whose search
    pattern will not fit would announce its entire subtree as deletions, and
    the agent would be told a hundred files had been removed by a user who
    touched nothing.
    """
    b = Baseline(rows={"deep": d(), "deep/a.c": f(1), "deep/b/c.c": f(2)})
    got = {dec.key: dec for dec in diff({"deep": Entry(type=Type.UNREADABLE)},
                                        {"deep": d(),
                                         "deep/a.c": f(1),
                                         "deep/b/c.c": f(2)}, b)}
    assert got["deep"].act is Act.SKIP
    assert got["deep"].reason is S.UNREADABLE
    for child in ("deep/a.c", "deep/b/c.c"):
        assert got[child].act is Act.SKIP, got[child]
        assert got[child].act is not Act.REPORT
        # Silent: the directory is announced once, not once per file inside it.
        assert got[child].reason is S.UNDER_UNREADABLE


def test_a_path_merely_sharing_a_prefix_is_not_swept_up():
    """`deep` must not silence `deeper.c`. Prefix matching on a bare string
    would, and that is a file quietly ceasing to sync."""
    b = Baseline(rows={"deep": d(), "deeper.c": f(1)})
    got = {dec.key: dec for dec in diff({"deep": Entry(type=Type.UNREADABLE),
                                         "deeper.c": f(1)},
                                        {"deep": d(), "deeper.c": f(2)}, b)}
    assert got["deeper.c"].act is Act.PUSH
    assert got["deeper.c"].reason is not S.UNDER_UNREADABLE
