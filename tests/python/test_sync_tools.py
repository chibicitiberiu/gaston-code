"""`sync_status` and `sync_resolve`, the agent's half of the conflict flow.

Both were named in the plan and defined nowhere, which left the design with a
halt and no way out of it. A conflicted path refuses the agent's writes -- that
is deliberate, because an in-flight turn cannot react to news it has not
received -- and without `sync_resolve` the only thing that ever lifted it was
the user typing again, which is one turn too late and can be the message
"wait, stop".

`sync_status` matters for a quieter reason. Writing a file here always
succeeds. Whether it reached the user's machine is a separate question, and
without this there is no way to ask it, so the agent reports a file as
delivered because the write returned -- which is true, and useless.
"""
from __future__ import annotations

import os

import pytest

from bridge.server import Bridge
from bridge.sync import Baseline, Entry, Tomb, Type


@pytest.fixture
def b(tmp_path):
    bridge = Bridge("s", host="127.0.0.1", port=0)
    cwd = tmp_path / "work"
    (cwd / "machine").mkdir(parents=True)
    bridge.cwd = str(cwd)
    bridge.sync_dir = str(cwd / "machine")
    bridge.sync_state = str(tmp_path / "state")
    os.makedirs(bridge.sync_state, exist_ok=True)
    bridge.guest_sync_root = "C:\\WORK"
    Baseline().save(os.path.join(bridge.sync_state, "base.json"))
    return bridge


def test_status_says_so_plainly_when_sync_is_off(tmp_path):
    bridge = Bridge("s", host="127.0.0.1", port=0)
    out = bridge.sync_status_text()
    assert "not switched on" in out
    assert "no tool that would copy it" in out


def test_status_names_both_ends(b):
    out = b.sync_status_text()
    assert b.sync_dir in out
    assert "C:\\WORK" in out


def test_status_is_quiet_when_there_is_nothing_to_say(b):
    assert "Nothing outstanding" in b.sync_status_text()


def test_status_reports_a_withheld_path_and_how_to_clear_it(b):
    base = b._load_baseline()
    base.latched["src/main.c"] = "both sides moved"
    base.save(os.path.join(b.sync_state, "base.json"))

    out = b.sync_status_text()
    assert "REFUSING YOUR WRITES" in out
    assert "src/main.c" in out
    assert "sync_resolve" in out


def test_status_repeats_the_last_passs_skips(b):
    """A skipped name never reaches the user however many times it is written,
    and after the first pass nothing says so."""
    b._last_sync_reports = ["skipped aux.c: reserved device name"]
    out = b.sync_status_text()
    assert "aux.c" in out
    assert "NEVER" in out


def test_status_lists_edits_that_missed_the_mirror(b):
    """The quietest way to achieve nothing: the agent is confined to cwd, the
    mirror is a directory inside it, and everything else stays here."""
    open(os.path.join(b.cwd, "notes.md"), "wb").write(b"lost work\n")
    open(os.path.join(b.sync_dir, "real.c"), "wb").write(b"int x;\n")

    out = b.sync_status_text()
    assert "OUTSIDE THE MIRRORED FOLDER" in out
    assert "notes.md" in out
    assert "real.c" not in out


def test_resolve_release_lifts_the_refusal(b):
    path = os.path.join(b.sync_state, "base.json")
    base = b._load_baseline()
    base.latched["src/main.c"] = "both sides moved"
    base.save(path)

    out = b.sync_resolve("src/main.c", "release")
    assert "Released" in out
    assert not b._load_baseline().latched


def test_resolve_release_is_honest_when_there_was_no_latch(b):
    """Otherwise the agent 'fixes' a missing file by releasing a path that was
    never withheld, and the real reason -- a skipped name -- goes unread."""
    out = b.sync_resolve("src/main.c", "release")
    assert "not withheld" in out
    assert "sync_status" in out


def test_resolve_matches_case_insensitively_like_the_guest_does(b):
    path = os.path.join(b.sync_state, "base.json")
    base = b._load_baseline()
    base.latched["src/main.c"] = "both sides moved"
    base.save(path)

    assert "Released" in b.sync_resolve("SRC/MAIN.C", "release")


def test_resolve_forget_drops_a_tombstone(b):
    path = os.path.join(b.sync_state, "base.json")
    base = b._load_baseline()
    base.tombs["gone.c"] = Tomb(cls="t", crc=7)
    base.save(path)

    out = b.sync_resolve("gone.c", "forget")
    assert "Stopped tracking" in out
    assert not b._load_baseline().tombs


def test_resolve_forget_on_an_untracked_path_says_so(b):
    assert "not tracked" in b.sync_resolve("never-existed.c", "forget")


def test_resolve_survives_a_baseline_it_cannot_read(b):
    """Refuse, never treat an unreadable baseline as an empty one -- that
    would make every file new on both sides."""
    open(os.path.join(b.sync_state, "base.json"), "w").write("{ not json")
    out = b.sync_resolve("a.c", "release")
    assert "could not be read" in out
    assert "nothing changed" in out
