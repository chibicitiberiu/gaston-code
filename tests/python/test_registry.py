"""The session registry.

The chat list used to show every conversation on the machine, most of which
came from a desktop with a different set of tools and a different idea of what
a path looks like. The registry is what narrows it to conversations this bridge
actually started.
"""

import json

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
