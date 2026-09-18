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
from bridge.files.sync import Baseline, Entry, Tomb, Type


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


def test_sync_state_defaults_to_none_like_sync_dir(tmp_path):
    """02.36: sync_state used to default to "", the one value that makes
    os.path.join("", "base.json") silently resolve to a path relative to
    the process's cwd instead of failing loudly. sync_dir already uses None
    as its sentinel for "sync is off"; sync_state must match it."""
    bridge = Bridge("s", host="127.0.0.1", port=0)
    assert bridge.sync_dir is None
    assert bridge.sync_state is None


def test_stray_scan_is_bounded_and_says_so(b, monkeypatch):
    """02.34: _edits_outside_the_mirror used to os.walk the whole working
    directory with no ceiling, on every sync_status call, while tools.py
    tells the model the tool is "free to call". Cost has to be bounded
    regardless of how large cwd has grown, and a truncated scan must say so
    rather than silently look exhaustive."""
    monkeypatch.setattr(type(b), "STRAY_SCAN_BUDGET", 3)
    for i in range(6):
        open(os.path.join(b.cwd, f"stray{i}.txt"), "w").write("x")

    stray, truncated = b._edits_outside_the_mirror()
    assert truncated
    assert len(stray) <= 3

    out = b.sync_status_text()
    assert "stopped after 3 files" in out


def test_stray_scan_under_budget_is_not_flagged_truncated(b):
    open(os.path.join(b.cwd, "stray.txt"), "w").write("x")
    stray, truncated = b._edits_outside_the_mirror()
    assert stray == ["stray.txt"]
    assert not truncated
    assert "stopped after" not in b.sync_status_text()


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


async def test_resolve_release_lifts_the_refusal(b):
    path = os.path.join(b.sync_state, "base.json")
    base = b._load_baseline()
    base.latched["src/main.c"] = "both sides moved"
    base.save(path)

    out = await b.sync_resolve("src/main.c", "release")
    assert "Released" in out
    assert not b._load_baseline().latched


async def test_resolve_release_is_honest_when_there_was_no_latch(b):
    """Otherwise the agent 'fixes' a missing file by releasing a path that was
    never withheld, and the real reason -- a skipped name -- goes unread."""
    out = await b.sync_resolve("src/main.c", "release")
    assert "not withheld" in out
    assert "sync_status" in out


async def test_resolve_matches_case_insensitively_like_the_guest_does(b):
    path = os.path.join(b.sync_state, "base.json")
    base = b._load_baseline()
    base.latched["src/main.c"] = "both sides moved"
    base.save(path)

    assert "Released" in await b.sync_resolve("SRC/MAIN.C", "release")


async def test_resolve_forget_drops_a_tombstone(b):
    path = os.path.join(b.sync_state, "base.json")
    base = b._load_baseline()
    base.tombs["gone.c"] = Tomb(cls="t", crc=7)
    base.save(path)

    out = await b.sync_resolve("gone.c", "forget")
    assert "Stopped tracking" in out
    assert not b._load_baseline().tombs


async def test_resolve_forget_on_an_untracked_path_says_so(b):
    assert "not tracked" in await b.sync_resolve("never-existed.c", "forget")


async def test_resolve_survives_a_baseline_it_cannot_read(b):
    """Refuse, never treat an unreadable baseline as an empty one -- that
    would make every file new on both sides."""
    open(os.path.join(b.sync_state, "base.json"), "w").write("{ not json")
    out = await b.sync_resolve("a.c", "release")
    assert "could not be read" in out
    assert "nothing changed" in out


# --- the preamble ----------------------------------------------------------
#
# The conflict flow only closes if the agent is actually told. `_run_turn` is
# the only path into a session and fires only on INPUT, so a conflict found by
# the timer between turns reaches the user's screen -- right, they are the one
# who can act on it -- and reaches the agent nowhere.

async def test_no_preamble_when_there_is_nothing_to_say(b):
    assert await b._with_sync_preamble("hello") == "hello"


async def test_a_pending_notice_rides_in_on_the_next_turn(b):
    b._agent_notices = ["SYNC CONFLICT src/main.c -- both sides moved."]
    out = await b._with_sync_preamble("carry on with the parser")

    assert "src/main.c" in out
    assert "carry on with the parser" in out
    # Attributed, or the model reads a machine message as the user speaking.
    assert "not \nfrom the user" in out or "not from the user" in out.replace("\n", " ")
    assert "the user's message follows" in out


async def test_the_preamble_says_writing_again_is_not_an_option(b):
    """The agent's natural move on being told about a conflict is to rewrite
    the file. It cannot -- the path refuses pushes -- and the preamble has to
    say so, or it will try, succeed locally, and report success."""
    b._agent_notices = ["SYNC CONFLICT a.c"]
    out = await b._with_sync_preamble("go on")
    assert "REFUSES your writes" in out
    assert "sync_resolve" in out
    assert "finished with the file" in out


async def test_a_notice_is_not_repeated_on_every_turn(b):
    b._agent_notices = ["SYNC CONFLICT a.c"]
    first = await b._with_sync_preamble("one")
    second = await b._with_sync_preamble("two")
    assert "a.c" in first
    assert second == "two"


async def test_the_preamble_is_skipped_when_sync_is_off(tmp_path):
    bridge = Bridge("s", host="127.0.0.1", port=0)
    bridge._agent_notices = ["stale"]
    assert await bridge._with_sync_preamble("hello") == "hello"


async def test_continue_lifts_every_withheld_path(b):
    """Pressing Continue is the confirmation, so it clears the refusal.

    All of them, not one. The bar says "N conflicting edits" and the user
    answers it once; releasing a single path would leave the rest withheld with
    no bar up to say so, and the agent silently unable to write to files nobody
    has mentioned.
    """
    path = os.path.join(b.sync_state, "base.json")
    base = b._load_baseline()
    base.latched["src/main.c"] = "both sides moved"
    base.latched["src/util.c"] = "both sides moved"
    base.save(path)

    assert await b._release_all_latches() == 2
    assert not b._load_baseline().latched


async def test_releasing_when_nothing_is_withheld_is_harmless(b):
    assert await b._release_all_latches() == 0


async def test_releasing_survives_an_unreadable_baseline(b):
    """Refuse to act, never treat it as empty -- and never raise into the
    RESUME handler, which would leave the user pressing a dead button."""
    open(os.path.join(b.sync_state, "base.json"), "w").write("{ not json")
    assert await b._release_all_latches() == 0
