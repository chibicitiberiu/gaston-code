"""Sync is on unless it was switched off.

The design, in the user's words (2026-10-01): "Auto sync is always on once
work dir is established. Work dir can be established by user manually, or
will be auto-assigned by client if user doesn't assign it. Bridge sets up a
sync dir automatically in temp if one is not provided."

What these pin is the half that was missing. The client already made
`C:\\WORK\\<title>` and named it in the prompt whether or not this side was
mirroring, so a bridge started without `--sync-dir` produced the worst of the
two arrangements: a work directory on the guest, nothing converging into it,
and every file travelling by `put_file` -- which writes bytes with no
opinion about line endings, so a batch file written here did not run there
(docs/measurements/batch-eol-and-dir-b-p200-20261001.txt).
"""

from __future__ import annotations

import os
import tempfile

from bridge.server import default_sync_root, load_config, parse_args


def _cfg(argv: list[str]):
    return load_config(parse_args(argv))


def test_no_arguments_mirrors_a_temp_tree():
    c = _cfg([])
    assert c.sync_dir == default_sync_root()
    assert c.sync_dir.startswith(tempfile.gettempdir() + os.sep)


def test_the_agent_edits_the_tree_that_is_mirrored():
    """cwd IS the mirror when nobody named either. Anything else leaves the
    agent writing beside the mirror, which succeeds and never arrives."""
    c = _cfg([])
    assert c.cwd == c.sync_dir


def test_a_named_cwd_is_what_gets_mirrored():
    """Not a sibling of it, and not a temp tree somewhere else: the agent's
    file tools operate on --cwd, so that is the tree that has to converge."""
    c = _cfg(["--cwd", os.path.join(tempfile.gettempdir(), "gaston-test-cwd")])
    assert c.sync_dir == c.cwd


def test_a_named_sync_dir_still_wins():
    c = _cfg(["--sync-dir", os.path.join(tempfile.gettempdir(), "gt", "mine")])
    assert c.sync_dir == os.path.join(tempfile.gettempdir(), "gt", "mine")


def test_the_baseline_lands_outside_the_mirror():
    """validate() refuses a baseline inside the tree it describes; the
    default has to satisfy that without anyone passing --sync-state."""
    c = _cfg([])
    assert os.path.commonpath([c.sync_state, c.sync_dir]) != c.sync_dir


def test_off_has_to_be_asked_for():
    """The no-sync path still exists -- several prompt branches and their
    tests depend on it -- but it is now a flag rather than the default."""
    c = _cfg(["--no-sync"])
    assert c.sync_dir is None
    assert c.sync_state is None


def test_the_default_is_stable_across_restarts():
    """mkdtemp would throw the baseline away on every restart and re-sync
    every conversation from nothing."""
    assert default_sync_root() == default_sync_root()


# --- two things that were silent -------------------------------------------

def test_the_chat_list_says_how_many_did_not_fit():
    """The cap is the CLIENT's table (MAX_CHATS less the age-group headers), so
    the bridge cuts to match it. What it did not do was say so: the user
    crossed 59 conversations and the older ones stopped appearing, with the
    count in the bridge's log and nowhere on their screen."""
    from bridge.wire.protocol import sessions_end
    assert sessions_end(29).headers.get("hidden") == "29"
    # Zero omits it, so a client that ignores the header is unaffected.
    assert "hidden" not in sessions_end(0).headers
    assert "hidden" not in sessions_end().headers


def test_the_prompt_names_which_machine_this_is():
    """The agent's own memory directory is keyed on the working directory, not
    on the machine, so a note about "the connected machine" loses its subject
    the moment a second one dials in -- which is how a CRLF fact learned on the
    Athlon box got filed where the QEMU guest would read it."""
    from bridge.agent.machine import describe
    out = describe({"mem_kb": "261120", "machine": "087dfc60-bde9-11f1"})
    assert "087dfc60-bde9-11f1" in out
    assert "name this id" in out


def test_a_machine_that_declares_no_id_is_not_lectured():
    """An older client sends no `machine` header. Telling it to name an id it
    does not have would be an instruction it cannot follow."""
    from bridge.agent.machine import describe
    assert "name this id" not in describe({"mem_kb": "261120"})
