"""The notes the agent is told about the machine's toolchains.

Detection decides WHEN a note is shown; these decide what it SAYS. The second
is the dangerous one: a detection rule that guesses wrong costs one unused
inventory line, and a note that is wrong costs the agent acting confidently
on folklore -- under an instruction not to go and check.

So the invariants here are about provenance and about not hardcoding a path
that detection exists to discover.
"""

import pytest

from bridge.agent.toolnotes import NOTES, Note


def test_every_note_says_where_it_was_measured():
    """The day this becomes optional is the day the first unmeasured note
    arrives, and nothing downstream can tell the difference."""
    for name, note in NOTES.items():
        assert note.measured.strip(), name
        assert any(ch.isdigit() for ch in note.measured), (
            f"{name}: `measured` must name a version or a date, got "
            f"{note.measured!r}")


def test_no_note_hardcodes_a_path_that_detection_exists_to_find():
    """A note saying C:\\WATCOM contradicts the inventory line above it the
    moment someone installs to D:\\OW -- two authoritative sources
    disagreeing, and the more detailed one wrong."""
    for name, note in NOTES.items():
        assert "C:\\WATCOM" not in note.body, name
        assert "C:\\BC5" not in note.body, name


def test_a_body_with_a_root_placeholder_substitutes_it():
    out = NOTES["watcom"].render("D:\\OW")
    assert "D:\\OW\\SETVARS.BAT" in out
    assert "{root}" not in out


def test_a_body_without_a_placeholder_is_unharmed():
    """The shell note has no root -- rendering must not mangle it."""
    out = NOTES["command.com"].render("")
    assert "Too many parameters" in out
    assert "{root}" not in out


def test_the_shell_note_carries_the_facts_the_exec_description_should_not():
    """`exec`'s description is unconditional -- every machine gets it,
    including a 4DOS box where `&&` works and the NT 4 target CLAUDE.md says
    the tool surface must survive. Shell syntax belongs to the detected
    shell, which is what makes this design portable rather than a Win98
    special case."""
    body = NOTES["command.com"].body
    for fact in ("&&", "2>&1", "Syntax error"):
        assert fact in body, fact


def test_render_is_stripped_so_blocks_compose():
    for name, note in NOTES.items():
        out = note.render("X:\\T")
        assert not out.startswith("\n"), name
        assert not out.endswith("\n"), name


def test_a_note_is_immutable():
    """Shared, rendered per session. Nothing should be able to edit one in
    place and have it leak into the next conversation."""
    with pytest.raises(Exception):
        NOTES["watcom"].title = "something else"
