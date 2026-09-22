"""The per-conversation store.

One row per conversation, holding three things that used to be three files:
whether this bridge started it, what model/effort/mode it was set to, and
which folder it works in. The merge is the subject of half of these -- an
older bridge may still be writing the old files, so they are read on every
load rather than converted once.
"""

import json
import pytest

from bridge.agent.conversations import Conversations


# --- whose conversation it is ----------------------------------------------


def test_empty_when_there_is_no_file(tmp_path):
    c = Conversations(tmp_path / "none.json")
    assert len(c) == 0
    assert c.filter([{"id": "a"}]) == []


def test_added_ids_survive_a_restart(tmp_path):
    """In memory would be enough until the bridge is restarted, which is
    exactly when the chat list would go mysteriously empty."""
    path = tmp_path / "c.json"
    Conversations(path).add("abc")
    assert "abc" in Conversations(path)


def test_filter_keeps_only_known_sessions(tmp_path):
    c = Conversations(tmp_path / "c.json")
    c.add("mine")
    rows = [{"id": "mine", "title": "x"}, {"id": "someone-elses", "title": "y"}]
    assert [x["id"] for x in c.filter(rows)] == ["mine"]


def test_a_row_is_not_a_claim_that_the_bridge_started_it(tmp_path):
    """`filter` keys on `mine`, not on the row existing.

    A conversation gets a row the moment anything is known about it, and
    being given a folder or a model while it was open is not the same claim
    as this bridge having started it. Keying the chat list on row presence
    would put other machines' conversations back in the list, which is the
    exact thing the registry existed to stop.
    """
    c = Conversations(tmp_path / "c.json")
    c.set_folder("not-ours", "NOTES")
    c.remember("not-ours", model="opus")
    assert len(c) == 1
    assert "not-ours" not in c
    assert c.filter([{"id": "not-ours"}]) == []


def test_adding_twice_is_harmless(tmp_path):
    c = Conversations(tmp_path / "c.json")
    c.add("a")
    c.add("a")
    assert len(c) == 1


def test_none_and_empty_are_ignored(tmp_path):
    c = Conversations(tmp_path / "c.json")
    c.add(None)
    c.add("")
    assert len(c) == 0


def test_ids_that_appear_later_can_still_be_registered(tmp_path):
    """The SDK assigns a session id when the first query goes out, not when
    the session starts, so registering at start() records nothing and the
    conversation never shows up in the chat list."""
    c = Conversations(tmp_path / "c.json")
    c.add(None)                 # what start() sees
    assert len(c) == 0
    c.add("assigned-later")     # what the first turn sees
    assert c.filter([{"id": "assigned-later"}]) == [{"id": "assigned-later"}]


# --- model, effort, permission mode ----------------------------------------


def test_settings_round_trip_through_the_file(tmp_path):
    path = tmp_path / "c.json"
    c = Conversations(path)
    c.remember("conv-1", model="opus", effort="high", mode="plan")
    c.remember("conv-2", model="haiku")
    assert Conversations(path).settings("conv-1") == {
        "model": "opus", "effort": "high", "mode": "plan"}
    assert Conversations(path).settings("conv-2") == {"model": "haiku"}
    assert Conversations(path).settings("never-seen") == {}


def test_a_later_change_merges_rather_than_replaces(tmp_path):
    """Picking a model must not forget the mode chosen a minute earlier."""
    path = tmp_path / "c.json"
    c = Conversations(path)
    c.remember("x", mode="plan")
    c.remember("x", model="opus")
    assert Conversations(path).settings("x") == {"model": "opus",
                                                 "mode": "plan"}


def test_the_empty_string_is_a_real_value(tmp_path):
    """"" means the bridge's own default, which is a CHOICE -- distinct from
    never having chosen, which is what an absent key means. A caller has to
    be able to tell them apart or "restore what this chat used" cannot know
    whether to leave the current selection alone."""
    path = tmp_path / "c.json"
    Conversations(path).remember("x", model="")
    assert Conversations(path).settings("x") == {"model": ""}
    assert "model" in Conversations(path).settings("x")


def test_a_field_nobody_declared_is_refused(tmp_path):
    c = Conversations(tmp_path / "c.json")
    with pytest.raises(KeyError):
        c.remember("x", colour="red")


def test_no_conversation_id_writes_nothing(tmp_path):
    """Before the first message there is no id. Writing under "" would give
    every unspoken chat on the machine the same saved settings."""
    path = tmp_path / "c.json"
    c = Conversations(path)
    c.remember("", model="opus")
    c.remember(None, model="opus")
    c.set_folder("", "NOTES")
    assert not path.exists()
    assert len(c) == 0


# --- the folder ------------------------------------------------------------


def test_the_folder_comes_back_with_both_spellings(tmp_path):
    path = tmp_path / "c.json"
    Conversations(path).set_folder("x", "NOTES", "C:\\Gaston\\NOTES")
    back = Conversations(path)
    assert back.folder("x") == "NOTES"
    assert back.guest_folder("x") == "C:\\Gaston\\NOTES"
    assert back.taken() == {"NOTES"}


def test_taken_ignores_rows_with_no_folder(tmp_path):
    """A row exists for anything known about a conversation, and most of
    them have no folder. "" was never a name anything could collide with."""
    c = Conversations(tmp_path / "c.json")
    c.add("spoken-in")
    c.set_folder("has-one", "NOTES")
    assert c.taken() == {"NOTES"}


# --- durability ------------------------------------------------------------


def test_a_corrupt_file_does_not_stop_the_bridge(tmp_path):
    """Worst case is an empty chat list and three menu selections, both of
    which using the app repairs. Refusing to start would not be recoverable
    at all -- see Baseline for where the other posture belongs."""
    path = tmp_path / "c.json"
    path.write_text("{ this is not json")
    c = Conversations(path)
    assert len(c) == 0
    c.add("fresh")
    assert "fresh" in Conversations(path)


def test_the_write_is_atomic(tmp_path):
    """Written to a temp file and renamed, so a crash mid-write cannot leave
    a truncated store behind."""
    path = tmp_path / "c.json"
    Conversations(path).add("a")
    assert json.loads(path.read_text()) == {"a": {"mine": True}}
    assert not (tmp_path / "c.tmp").exists()


# --- the three files this replaced -----------------------------------------


def _legacy(tmp_path):
    (tmp_path / "sessions.json").write_text(json.dumps(["a", "b"]))
    (tmp_path / "folders.json").write_text(json.dumps({
        "a": {"local": "NOTES", "guest": "C:\\Gaston\\NOTES"},
        "old": "PINEAPPL",          # folders.json's first format
    }))
    (tmp_path / "settings.json").write_text(json.dumps({
        "a": {"model": "opus", "effort": "", "mode": "plan"},
    }))


def test_the_old_three_files_are_read_into_one_row(tmp_path):
    _legacy(tmp_path)
    c = Conversations(tmp_path / "c.json")
    assert "a" in c and "b" in c
    assert c.settings("a") == {"model": "opus", "effort": "", "mode": "plan"}
    assert c.folder("a") == "NOTES"
    assert c.guest_folder("a") == "C:\\Gaston\\NOTES"
    assert c.folder("old") == "PINEAPPL", "the first folders.json format"
    assert "old" not in c, "a folder is not a claim that we started it"


def test_the_old_files_are_never_written_or_removed(tmp_path):
    """Rolling back is running the previous binary, which means the files it
    reads have to still be there and still be what it left."""
    _legacy(tmp_path)
    before = {n: (tmp_path / n).read_text()
              for n in ("sessions.json", "folders.json", "settings.json")}
    c = Conversations(tmp_path / "c.json")
    c.add("new-one")
    c.remember("a", model="haiku")
    assert {n: (tmp_path / n).read_text() for n in before} == before


def test_an_id_an_older_bridge_adds_later_is_not_lost(tmp_path):
    """The reason the old files are read on EVERY load rather than converted
    once. A bridge from before this change may still be running and still
    appending to sessions.json; a one-shot migration would leave that
    conversation permanently out of the chat list."""
    path = tmp_path / "c.json"
    Conversations(path).add("from-the-new-one")

    (tmp_path / "sessions.json").write_text(json.dumps(["added-by-the-old"]))
    back = Conversations(path)
    assert "added-by-the-old" in back
    assert "from-the-new-one" in back


def test_the_merged_file_wins_field_by_field(tmp_path):
    """A newer choice must beat the legacy one, without dropping fields the
    newer file has nothing to say about."""
    _legacy(tmp_path)
    path = tmp_path / "c.json"
    path.write_text(json.dumps({"a": {"model": "haiku"}}))
    c = Conversations(path)
    assert c.settings("a") == {"model": "haiku", "effort": "", "mode": "plan"}
    assert c.folder("a") == "NOTES", "not mentioned, so not dropped"
    assert "a" in c, "nor is being ours"


def test_a_corrupt_legacy_file_is_skipped_not_fatal(tmp_path):
    _legacy(tmp_path)
    (tmp_path / "folders.json").write_text("{ nope")
    c = Conversations(tmp_path / "c.json")
    assert "a" in c
    assert c.settings("a")["model"] == "opus"
    assert c.folder("a") == "", "the unreadable one contributes nothing"
