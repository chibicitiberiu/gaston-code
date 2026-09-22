"""Model, effort and permission mode belong to the conversation, not the box.

`registry.Settings` was built for one complaint: "opening a chat you had set
to Plan and Opus gave you whatever the last chat happened to be using". The
store, the write and the read were all present and `settings.json` was still
never created, because every write ran at a moment when the conversation had
no id to be keyed on.

The reason this was not caught is in `harness.FakeSession`: it used to assign
`session_id` in `__init__`, so `guest.conv_id` was true from the instant a
client existed. The real `Session` learns its id from the SDK's `init`
message, which arrives during the FIRST TURN -- so the fake was true exactly
where production was false. The fake defers it now, and these tests are
written against that.
"""

from __future__ import annotations

import json

import pytest

from bridge.wire.protocol import Frame

from harness import harness, _recv   # noqa: F401


async def _connected(harness):
    r, w = await harness.connect()
    await _recv(harness, r, "OPTIONSEND")
    return r, w, harness.bridge._guest


def _saved(harness, session_id):
    return harness.bridge.settings.get(session_id)


# --- the write side ---------------------------------------------------------


async def test_a_model_chosen_before_the_first_message_is_remembered(harness):
    """The New chat case, and the one that left `settings.json` absent.

    Picking a model on a conversation that has not been spoken in is recorded
    on the guest and applied when the client is finally built -- but the
    write to disk needs an id, and there is none until the first turn. The
    old hook ran in `_resume_pending`, one line after `pending_resume` was
    cleared and before `send()` had produced an id, so it wrote nothing on
    either branch.
    """
    r, w, guest = await _connected(harness)
    await harness.bridge._new_session(guest, "")
    assert not guest.conv_id, "a fresh chat must not claim an id yet"

    await harness.bridge._on_setmodel(guest, Frame("SETMODEL",
                                                   {"model": "opus"}))
    assert guest.model == "opus"
    assert _saved(harness, "") == {}, "nothing may be keyed on the empty id"

    await harness.bridge._run_turn(guest, "hello")
    conv = guest.conv_id
    assert conv, "the turn should have produced a conversation id"
    assert _saved(harness, conv).get("model") == "opus"
    await harness.close(w)


async def test_the_effort_fork_carries_its_settings_to_the_new_id(harness):
    """Changing effort forks, and a fork is a NEW conversation id.

    `_set_effort` writes the settings before the fork, against the id being
    left behind. The forked conversation -- the one now on screen and in the
    chat list -- had no row at all, so the effort the user had just chosen
    was the first thing lost when they switched away and back.
    """
    r, w, guest = await _connected(harness)
    await harness.bridge._run_turn(guest, "first")
    before = guest.conv_id
    assert before

    await harness.bridge._set_effort(guest, "high")
    await harness.bridge._run_turn(guest, "second")
    after = guest.conv_id

    assert after != before, "the fork should have changed the id"
    assert _saved(harness, after).get("effort") == "high"
    await harness.close(w)


async def test_settings_reach_the_file_on_disk(harness):
    """Through `Settings`, not just the in-memory map.

    The store writes atomically to `$GASTON_STATE`'s directory. This asserts
    the file, because "the object remembers" and "the next bridge remembers"
    are different claims and only the second one is the complaint.
    """
    r, w, guest = await _connected(harness)
    await harness.bridge._on_setmodel(guest, Frame("SETMODEL",
                                                   {"model": "haiku"}))
    await harness.bridge._run_turn(guest, "hello")

    path = harness.bridge.settings.path
    assert path.exists(), "settings.json was never created"
    on_disk = json.loads(path.read_text())
    assert on_disk[guest.conv_id]["model"] == "haiku"
    await harness.close(w)


# --- the read side, which is what the user actually sees --------------------


async def test_switching_back_restores_that_chat_s_model(harness):
    """The whole complaint, end to end.

    Two conversations, each set to a different model, and the second one
    opened and then left. Coming back has to bring the first one's choice
    with it rather than whatever was last on screen.
    """
    bridge = harness.bridge
    r, w, guest = await _connected(harness)

    # Conversation A, on opus.
    await bridge._on_setmodel(guest, Frame("SETMODEL", {"model": "opus"}))
    await bridge._run_turn(guest, "in A")
    conv_a = guest.conv_id

    # Conversation B, on haiku.
    await bridge._new_session(guest, "")
    await bridge._on_setmodel(guest, Frame("SETMODEL", {"model": "haiku"}))
    await bridge._run_turn(guest, "in B")
    conv_b = guest.conv_id
    assert conv_b != conv_a

    # Back to A.
    await bridge._switch(guest, conv_a)
    assert guest.model == "opus", (
        "switching back gave the model the other chat was using")

    # And the session built for it actually runs on that model, rather than
    # merely being labelled with it.
    await bridge._run_turn(guest, "again in A")
    assert guest.claude.model == "opus"

    # B is untouched by the visit to A.
    assert _saved(harness, conv_b).get("model") == "haiku"
    await harness.close(w)


async def test_a_chat_with_nothing_saved_keeps_what_is_selected(harness):
    """The deliberate half, asserted so a later change has to argue with it.

    "I have not chosen for this chat" and "I chose the default for this chat"
    are different, and only the second is written down -- so a conversation
    from before this existed must not be reset to the process defaults.
    """
    r, w, guest = await _connected(harness)
    await harness.bridge._on_setmodel(guest, Frame("SETMODEL",
                                                   {"model": "opus"}))
    harness.bridge._restore_settings(guest, "a-conversation-never-seen")
    assert guest.model == "opus"
    await harness.close(w)


async def test_the_mode_and_effort_travel_with_the_model(harness):
    """All three fields, because `Settings.set` takes the whole trio."""
    bridge = harness.bridge
    r, w, guest = await _connected(harness)

    await bridge._set_mode(guest, "plan")
    await bridge._on_setmodel(guest, Frame("SETMODEL", {"model": "opus"}))
    await bridge._run_turn(guest, "hello")
    conv = guest.conv_id

    saved = _saved(harness, conv)
    assert saved.get("mode") == "plan"
    assert saved.get("model") == "opus"

    await bridge._new_session(guest, "")
    await bridge._set_mode(guest, "default")
    await bridge._run_turn(guest, "elsewhere")

    await bridge._switch(guest, conv)
    assert guest.perm_mode == "plan"
    assert guest.model == "opus"
    await harness.close(w)
