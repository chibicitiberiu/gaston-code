"""The session registry.

The chat list used to show every conversation on the machine, most of which
came from a desktop with a different set of tools and a different idea of what
a path looks like. The registry is what narrows it to conversations this bridge
actually started.
"""

import json
import pytest

from bridge.agent.registry import Registry


def test_empty_when_there_is_no_file(tmp_path):
    r = Registry(tmp_path / "none.json")
    assert len(r) == 0
    assert r.filter([{"id": "a"}]) == []


def test_added_ids_survive_a_restart(tmp_path):
    """In memory would be enough until the bridge is restarted, which is
    exactly when the list would go mysteriously empty."""
    path = tmp_path / "s.json"
    Registry(path).add("abc")
    assert "abc" in Registry(path)


def test_filter_keeps_only_known_sessions(tmp_path):
    r = Registry(tmp_path / "s.json")
    r.add("mine")
    rows = [{"id": "mine", "title": "x"}, {"id": "someone-elses", "title": "y"}]
    assert [x["id"] for x in r.filter(rows)] == ["mine"]


def test_adding_twice_is_harmless(tmp_path):
    r = Registry(tmp_path / "s.json")
    r.add("a")
    r.add("a")
    assert len(r) == 1


def test_none_and_empty_are_ignored(tmp_path):
    r = Registry(tmp_path / "s.json")
    r.add(None)
    r.add("")
    assert len(r) == 0


def test_a_corrupt_registry_does_not_stop_the_bridge(tmp_path):
    """Worst case is an empty chat list, which using the app repairs. Refusing
    to start would not be recoverable at all."""
    path = tmp_path / "s.json"
    path.write_text("{ this is not json")
    r = Registry(path)
    assert len(r) == 0
    r.add("fresh")
    assert "fresh" in Registry(path)


def test_the_write_is_atomic(tmp_path):
    """Written to a temp file and renamed, so a crash mid-write cannot leave a
    truncated registry behind."""
    path = tmp_path / "s.json"
    r = Registry(path)
    r.add("a")
    assert json.loads(path.read_text()) == ["a"]
    assert not (tmp_path / "s.tmp").exists()


def test_ids_that_appear_later_can_still_be_registered(tmp_path):
    """The SDK assigns a session id when the first query goes out, not when
    the session starts, so registering at start() records nothing and the
    conversation never shows up in the chat list."""
    r = Registry(tmp_path / "s.json")
    r.add(None)                 # what start() sees
    assert len(r) == 0
    r.add("assigned-later")     # what the first turn sees
    assert r.filter([{"id": "assigned-later"}]) == [{"id": "assigned-later"}]


# --- settings, per conversation ---------------------------------------------


def test_settings_round_trip_through_the_file(tmp_path):
    from bridge.agent.registry import Settings

    path = tmp_path / "settings.json"
    s = Settings(path)
    s.set("conv-1", model="opus", effort="high", mode="plan")
    s.set("conv-2", model="haiku")
    assert Settings(path).get("conv-1") == {
        "model": "opus", "effort": "high", "mode": "plan"}
    assert Settings(path).get("conv-2") == {"model": "haiku"}
    assert Settings(path).get("never-seen") == {}


def test_a_later_change_merges_rather_than_replaces(tmp_path):
    """Picking a model must not forget the mode chosen a minute earlier."""
    from bridge.agent.registry import Settings

    path = tmp_path / "settings.json"
    s = Settings(path)
    s.set("c", mode="plan")
    s.set("c", model="opus")
    assert Settings(path).get("c") == {"model": "opus", "mode": "plan"}


def test_the_empty_string_is_a_real_value(tmp_path):
    """"" means the bridge's own default, which is a CHOICE -- distinct from
    never having chosen, which is what an absent key means. A caller has to
    be able to tell them apart or "restore what this chat used" cannot know
    whether to leave the current selection alone."""
    from bridge.agent.registry import Settings

    path = tmp_path / "settings.json"
    s = Settings(path)
    s.set("c", model="")
    assert Settings(path).get("c") == {"model": ""}
    assert "model" in Settings(path).get("c")


def test_a_field_nobody_declared_is_refused(tmp_path):
    from bridge.agent.registry import Settings

    s = Settings(tmp_path / "settings.json")
    with pytest.raises(KeyError):
        s.set("c", colour="red")


def test_no_conversation_id_writes_nothing(tmp_path):
    """Before the first message there is no id. Writing under "" would give
    every unspoken chat on the machine the same saved settings."""
    from bridge.agent.registry import Settings

    path = tmp_path / "settings.json"
    s = Settings(path)
    s.set("", model="opus")
    s.set(None, model="opus")
    assert not path.exists()
    assert len(s) == 0


def test_a_corrupt_settings_file_is_not_fatal(tmp_path):
    """Losing these costs three menu selections. Refusing to start costs the
    session -- see Baseline for where the other posture belongs."""
    from bridge.agent.registry import Settings

    path = tmp_path / "settings.json"
    path.write_text("{not json")
    s = Settings(path)
    assert s.get("c") == {}
    s.set("c", model="opus")
    assert Settings(path).get("c") == {"model": "opus"}

