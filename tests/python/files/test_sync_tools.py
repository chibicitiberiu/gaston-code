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

from bridge.files.sync import Baseline, Entry, Tomb, Type
from bridge.server import Bridge


def _committed_guest(local: str, guest_root: str):
    """A GuestSession that has (or has not) committed to a work folder.

    A real one rather than a stub: `sync_status_text` is not the only thing
    these tests reach, and the paths around it want `sync_lock` and the rest
    of the machinery a namespace does not have.
    """
    from bridge.server import GuestSession, Transcript, TranscriptView
    g = GuestSession(view=TranscriptView(pages=Transcript()))
    g.work_dir = local
    g.work_guest = guest_root
    return g


@pytest.fixture
def b(tmp_path):
    bridge = Bridge("s", host="127.0.0.1", port=0)
    cwd = tmp_path / "work"
    (cwd / "machine").mkdir(parents=True)
    bridge.config.cwd = str(cwd)
    bridge.config.sync_dir = str(cwd / "machine")
    bridge.config.sync_state = str(tmp_path / "state")
    os.makedirs(bridge.config.sync_state, exist_ok=True)
    # A conversation that has COMMITTED to a folder, because that is what
    # makes there be a mirror to report on at all. Nothing is mirrored until
    # a chat's first tool call, and `sync_status` says exactly that while
    # there is not -- see test_status_says_so_before_a_folder_exists.
    bridge.connection.guest = _committed_guest(bridge.config.sync_dir, "C:\\WORK")
    bridge.connection.guest.guest_sync_root = "C:\\WORK"
    Baseline().save(os.path.join(bridge.config.sync_state, "base.json"))
    return bridge


def test_status_says_so_before_a_folder_exists(b):
    """An agent told "mirrored: <shared root> <-> <configured root>" while
    the baseline named an older tree concluded synchronisation was broken and
    refused to write the file it had been asked for. Nothing being mirrored
    yet is the normal state of a conversation, and it has to read that way."""
    b.connection.guest = _committed_guest("", "")
    out = b.sync.sync_status_text()
    assert "does not have a folder on the user's machine yet" in out
    assert "not a fault" in out
    assert "Mirrored:" not in out


def test_status_says_so_plainly_when_sync_is_off(tmp_path):
    """And NAMES A DESTINATION. It used to end "there is no tool that would
    copy it", which was stale (`put_file` copies one) and was the shape of
    answer that left an agent with nowhere to write and a drive root to write
    it in -- 2026-10-01, on real hardware. With no guest attached there is no
    work root to name either, so what it says here is ask."""
    bridge = Bridge("s", host="127.0.0.1", port=0)
    out = bridge.sync.sync_status_text()
    assert "not switched on" in out
    assert "ask the user where a file should go" in out
    assert "not a fault" in out


def test_status_names_the_folder_on_the_machine_when_sync_is_off(b):
    """The machine declared a work root, so this conversation has a folder
    under it -- made by the client, with nothing mirrored on this side."""
    b.config.sync_dir = None
    g = _committed_guest("", "")
    g.hello = {"sync_root": "C:\\WORK"}
    g.first_prompt = "Sky colour"
    g.pending_resume = "a1b2c3d4"   # what `conv_id` reads for an unopened chat
    b.connection.guest = g
    out = b.sync.sync_status_text()
    assert "C:\\WORK\\SKYCOLOU" in out
    assert "put_file" in out


def test_sync_state_defaults_to_none_like_sync_dir(tmp_path):
    """02.36: sync_state used to default to "", the one value that makes
    os.path.join("", "base.json") silently resolve to a path relative to
    the process's cwd instead of failing loudly. sync_dir already uses None
    as its sentinel for "sync is off"; sync_state must match it."""
    bridge = Bridge("s", host="127.0.0.1", port=0)
    assert bridge.config.sync_dir is None
    assert bridge.config.sync_state is None


def test_stray_scan_is_bounded_and_says_so(b, monkeypatch):
    """02.34: _edits_outside_the_mirror used to os.walk the whole working
    directory with no ceiling, on every sync_status call, while tools.py
    tells the model the tool is "free to call". Cost has to be bounded
    regardless of how large cwd has grown, and a truncated scan must say so
    rather than silently look exhaustive."""
    monkeypatch.setattr(type(b.sync), "STRAY_SCAN_BUDGET", 3)
    for i in range(6):
        open(os.path.join(b.config.cwd, f"stray{i}.txt"), "w").write("x")

    stray, truncated = b.sync.edits_outside_the_mirror()
    assert truncated
    assert len(stray) <= 3

    out = b.sync.sync_status_text()
    assert "stopped after 3 files" in out


def test_stray_scan_under_budget_is_not_flagged_truncated(b):
    open(os.path.join(b.config.cwd, "stray.txt"), "w").write("x")
    stray, truncated = b.sync.edits_outside_the_mirror()
    assert stray == ["stray.txt"]
    assert not truncated
    assert "stopped after" not in b.sync.sync_status_text()


def test_status_names_both_ends(b):
    out = b.sync.sync_status_text()
    assert b.config.sync_dir in out
    assert "C:\\WORK" in out


def test_status_is_quiet_when_there_is_nothing_to_say(b):
    assert "Nothing outstanding" in b.sync.sync_status_text()


def test_status_reports_a_withheld_path_and_how_to_clear_it(b):
    base = b.sync.load_baseline()
    base.latched["src/main.c"] = "both sides moved"
    base.save(os.path.join(b.config.sync_state, "base.json"))

    out = b.sync.sync_status_text()
    assert "REFUSING YOUR WRITES" in out
    assert "src/main.c" in out
    assert "sync_resolve" in out


def test_status_repeats_the_last_passs_skips(b):
    """A skipped name never reaches the user however many times it is written,
    and after the first pass nothing says so."""
    b.connection.guest.last_sync_reports = ["skipped aux.c: reserved device name"]
    out = b.sync.sync_status_text()
    assert "aux.c" in out
    assert "NEVER" in out


def test_status_lists_edits_that_missed_the_mirror(b):
    """The quietest way to achieve nothing: the agent is confined to cwd, the
    mirror is a directory inside it, and everything else stays here."""
    open(os.path.join(b.config.cwd, "notes.md"), "wb").write(b"lost work\n")
    open(os.path.join(b.config.sync_dir, "real.c"), "wb").write(b"int x;\n")

    out = b.sync.sync_status_text()
    assert "OUTSIDE THE MIRRORED FOLDER" in out
    assert "notes.md" in out
    assert "real.c" not in out


async def test_resolve_release_lifts_the_refusal(b):
    path = os.path.join(b.config.sync_state, "base.json")
    base = b.sync.load_baseline()
    base.latched["src/main.c"] = "both sides moved"
    base.save(path)

    out = await b.sync.sync_resolve("src/main.c", "release")
    assert "Released" in out
    assert not b.sync.load_baseline().latched


async def test_resolve_release_is_honest_when_there_was_no_latch(b):
    """Otherwise the agent 'fixes' a missing file by releasing a path that was
    never withheld, and the real reason -- a skipped name -- goes unread."""
    out = await b.sync.sync_resolve("src/main.c", "release")
    assert "not withheld" in out
    assert "sync_status" in out


async def test_resolve_matches_case_insensitively_like_the_guest_does(b):
    path = os.path.join(b.config.sync_state, "base.json")
    base = b.sync.load_baseline()
    base.latched["src/main.c"] = "both sides moved"
    base.save(path)

    assert "Released" in await b.sync.sync_resolve("SRC/MAIN.C", "release")


async def test_resolve_forget_drops_a_tombstone(b):
    path = os.path.join(b.config.sync_state, "base.json")
    base = b.sync.load_baseline()
    base.tombs["gone.c"] = Tomb(cls="t", crc=7)
    base.save(path)

    out = await b.sync.sync_resolve("gone.c", "forget")
    assert "Stopped tracking" in out
    assert not b.sync.load_baseline().tombs


async def test_resolve_forget_on_an_untracked_path_says_so(b):
    assert "not tracked" in await b.sync.sync_resolve("never-existed.c", "forget")


async def test_resolve_accept_revert_drops_the_prev_that_blocked_the_pull(b):
    """`diff` refuses to pull when the guest holds exactly what the last push
    replaced, because a stale editor buffer writing an old copy back looks
    identical to an edit. It cannot tell that from a person who genuinely
    undid the change, so this is the escape hatch -- and dropping `prev` is
    the whole mechanism: with no record of what the push replaced, the guest
    just differs from the ancestor and the ordinary row pulls it."""
    path = os.path.join(b.config.sync_state, "base.json")
    base = b.sync.load_baseline()
    base.prev["src/main.c"] = Entry(type=Type.FILE, cls="t", crc=11)
    base.latched["src/main.c"] = "the guest reverted a push"
    base.save(path)

    out = await b.sync.sync_resolve("src/main.c", "accept-revert")
    assert "Accepted the machine's version" in out
    after = b.sync.load_baseline()
    assert "src/main.c" not in after.prev
    assert not after.latched


async def test_resolve_accept_revert_says_the_old_content_is_not_kept(b):
    """The agent's version is discarded by this action and nothing stages it.
    Saying so is the difference between the agent re-applying its change and
    the agent believing the bridge kept a copy it can ask for."""
    path = os.path.join(b.config.sync_state, "base.json")
    base = b.sync.load_baseline()
    base.prev["src/main.c"] = Entry(type=Type.FILE, cls="t", crc=11)
    base.save(path)

    out = await b.sync.sync_resolve("src/main.c", "accept-revert")
    assert "not kept" in out


async def test_resolve_accept_revert_is_honest_when_nothing_was_reverted(b):
    assert "not being held as a reverted push" in await b.sync.sync_resolve(
        "src/main.c", "accept-revert")


async def test_resolve_refuses_an_unknown_action_instead_of_forgetting(b):
    """Until a third action existed, "not release" meant "forget", and the
    only guard was in tools.py -- a different file. An unrecognised action
    reaching the bridge dropped the row, which is the DESTRUCTIVE half of the
    pair and the wrong way round for a default."""
    path = os.path.join(b.config.sync_state, "base.json")
    base = b.sync.load_baseline()
    base.tombs["gone.c"] = Tomb(cls="t", crc=7)
    base.save(path)

    out = await b.sync.sync_resolve("gone.c", "acccept-revert")   # typo, deliberate
    assert "Unknown action" in out
    assert b.sync.load_baseline().tombs, "an unknown action must not forget the row"


async def test_resolve_survives_a_baseline_it_cannot_read(b):
    """Refuse, never treat an unreadable baseline as an empty one -- that
    would make every file new on both sides."""
    open(os.path.join(b.config.sync_state, "base.json"), "w").write("{ not json")
    out = await b.sync.sync_resolve("a.c", "release")
    assert "could not be read" in out
    assert "nothing changed" in out


# --- the preamble ----------------------------------------------------------
#
# The conflict flow only closes if the agent is actually told. `_run_turn` is
# the only path into a session and fires only on INPUT, so a conflict found by
# the timer between turns reaches the user's screen -- right, they are the one
# who can act on it -- and reaches the agent nowhere.

async def test_no_preamble_when_there_is_nothing_to_say(b):
    assert await b.turn_runner.with_sync_preamble("hello") == "hello"


async def test_a_pending_notice_rides_in_on_the_next_turn(b):
    b.connection.synccoord.agent_notices = ["SYNC CONFLICT src/main.c -- both sides moved."]
    out = await b.turn_runner.with_sync_preamble("carry on with the parser")

    assert "src/main.c" in out
    assert "carry on with the parser" in out
    # Attributed, or the model reads a machine message as the user speaking.
    assert "not \nfrom the user" in out or "not from the user" in out.replace("\n", " ")
    assert "the user's message follows" in out


async def test_the_preamble_says_writing_again_is_not_an_option(b):
    """The agent's natural move on being told about a conflict is to rewrite
    the file. It cannot -- the path refuses pushes -- and the preamble has to
    say so, or it will try, succeed locally, and report success."""
    b.connection.synccoord.agent_notices = ["SYNC CONFLICT a.c"]
    out = await b.turn_runner.with_sync_preamble("go on")
    assert "REFUSES your writes" in out
    assert "sync_resolve" in out
    assert "finished with the file" in out


async def test_a_notice_is_not_repeated_on_every_turn(b):
    b.connection.synccoord.agent_notices = ["SYNC CONFLICT a.c"]
    first = await b.turn_runner.with_sync_preamble("one")
    second = await b.turn_runner.with_sync_preamble("two")
    assert "a.c" in first
    assert second == "two"


async def test_the_preamble_is_skipped_when_sync_is_off(tmp_path):
    bridge = Bridge("s", host="127.0.0.1", port=0)
    bridge.connection.synccoord.agent_notices = ["stale"]
    assert await bridge.turn_runner.with_sync_preamble("hello") == "hello"


async def test_continue_lifts_every_withheld_path(b):
    """Pressing Continue is the confirmation, so it clears the refusal.

    All of them, not one. The bar says "N conflicting edits" and the user
    answers it once; releasing a single path would leave the rest withheld with
    no bar up to say so, and the agent silently unable to write to files nobody
    has mentioned.
    """
    path = os.path.join(b.config.sync_state, "base.json")
    base = b.sync.load_baseline()
    base.latched["src/main.c"] = "both sides moved"
    base.latched["src/util.c"] = "both sides moved"
    base.save(path)

    assert await b.sync.release_all_latches() == 2
    assert not b.sync.load_baseline().latched


async def test_releasing_when_nothing_is_withheld_is_harmless(b):
    assert await b.sync.release_all_latches() == 0


async def test_releasing_survives_an_unreadable_baseline(b):
    """Refuse to act, never treat it as empty -- and never raise into the
    RESUME handler, which would leave the user pressing a dead button."""
    open(os.path.join(b.config.sync_state, "base.json"), "w").write("{ not json")
    assert await b.sync.release_all_latches() == 0


async def test_resolve_accept_tree_takes_no_path(b):
    """It answers a WHOLE-PASS refusal, so requiring a path would mean fifty
    calls to lift one refusal -- and there is no single file it is about."""
    out = await b.sync.sync_resolve("", "accept-tree")
    assert "next pass" in out
    assert b.sync.load_baseline().accept_tree is True


async def test_resolve_accept_tree_changes_no_rows(b):
    """All it does is let the next pass past the check. If it edited the
    baseline it would be deciding the outcome instead of permitting it, and
    the per-path rows are what should decide."""
    path = os.path.join(b.config.sync_state, "base.json")
    base = b.sync.load_baseline()
    base.rows["src/main.c"] = Entry(type=Type.FILE, cls="t", crc=3)
    base.save(path)

    await b.sync.sync_resolve("", "accept-tree")
    after = b.sync.load_baseline()
    assert after.rows["src/main.c"].crc == 3
    assert not after.tombs


# --- one baseline each, stage 3c D3 -----------------------------------------


def _guest_on(machine: str, guest_root: str):
    from bridge.server import GuestSession, Transcript, TranscriptView
    g = GuestSession(view=TranscriptView(pages=Transcript()))
    g.hello = {"machine": machine}
    g.work_guest = guest_root
    return g


def test_two_machines_on_one_root_get_one_baseline_each(b):
    """THE DEADLOCK THIS EXISTS TO PREVENT. `base_path` keyed on the guest
    root alone, and both of this project's machines mirror `C:\\WORK` -- so
    they landed on one file with two different `tree_id`s, and `scan_guest`
    refuses a baseline whose tree id is not the machine's. Each refused the
    other's, every pass, forever: neither syncing, and the reason only in a
    log (BACKLOG 7).
    """
    a = _guest_on("BOX-A", "C:\\WORK\\NOTES")
    c = _guest_on("BOX-B", "C:\\WORK\\NOTES")
    pa, pc = b.sync.base_path(a), b.sync.base_path(c)
    assert pa and pc and pa != pc, (pa, pc)


def test_the_machine_already_using_base_json_keeps_it(b, tmp_path):
    """Nothing is migrated and nothing is deleted -- the same rule 11c set
    when this file first grew a second name. A baseline written before
    stage 3c records a root and no machine, and belongs to whichever
    machine comes back for it; it is only the SECOND that is rehoused, so
    the live one keeps its rows and does not re-read the whole tree."""
    import json
    import os

    a = _guest_on("BOX-A", "C:\\WORK\\NOTES")
    legacy = b.sync.base_path(a)
    assert os.path.basename(legacy) == "base.json", legacy
    os.makedirs(os.path.dirname(legacy), exist_ok=True)
    with open(legacy, "w", encoding="utf-8") as fh:
        json.dump({"guest_root": "C:\\WORK\\NOTES", "machine": ""}, fh)

    b.sync.base_paths.clear()
    assert os.path.basename(b.sync.base_path(a)) == "base.json"
    b.sync.base_paths.clear()
    other = b.sync.base_path(_guest_on("BOX-B", "C:\\WORK\\NOTES"))
    assert os.path.basename(other) != "base.json", other


def test_a_claimed_baseline_is_not_handed_to_another_machine(b, tmp_path):
    """Once a machine has stamped itself, the question has an answer that
    survives a restart -- which the in-process memo alone would not give."""
    import json
    import os

    a = _guest_on("BOX-A", "C:\\WORK\\NOTES")
    legacy = b.sync.base_path(a)
    os.makedirs(os.path.dirname(legacy), exist_ok=True)
    with open(legacy, "w", encoding="utf-8") as fh:
        json.dump({"guest_root": "C:\\WORK\\NOTES", "machine": "BOX-A"}, fh)

    b.sync.base_paths.clear()
    assert os.path.basename(b.sync.base_path(a)) == "base.json"
    b.sync.base_paths.clear()
    assert os.path.basename(
        b.sync.base_path(_guest_on("BOX-B", "C:\\WORK\\NOTES"))) != "base.json"


def test_one_machine_two_conversations_still_get_one_each(b):
    """The conversation dimension was already there and stays: the key is
    the folder as the guest spells it, and a conversation has its own
    folder under the root (`C:\\WORK\\<name>`, BACKLOG 7's one mirror per
    chat). Stage 3c added the machine beside it, not instead of it."""
    one = b.sync.base_path(_guest_on("BOX-A", "C:\\WORK\\NOTES"))
    two = b.sync.base_path(_guest_on("BOX-A", "C:\\WORK\\OTHER"))
    assert one != two, (one, two)
