"""The notes the agent is told about the machine's toolchains.

Detection decides WHEN a note is shown; these decide what it SAYS. The second
is the dangerous one: a detection rule that guesses wrong costs one unused
inventory line, and a note that is wrong costs the agent acting confidently
on folklore -- under an instruction not to go and check.

So the invariants here are about provenance and about not hardcoding a path
that detection exists to discover.
"""

import configparser
import pathlib

import conftest
import pytest

from bridge.agent.toolnotes import NOTES
from bridge.agent.toolrules import RULES

INI = pathlib.Path(conftest.REPO) / "win" / "tools_detect.ini"


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


# --- a note nothing can detect is a note nobody will ever read --------------

def test_every_note_is_keyed_on_something_the_bridge_can_detect():
    """The key is how a note gets selected. One that matches no rule is text
    that can never be shown, which is the quiet way for a measured fact to
    rot: nobody notices it is unreachable because nothing errors."""
    unreachable = sorted(set(NOTES) - set(RULES))
    assert not unreachable, f"notes with no detection rule: {unreachable}"


def test_every_note_is_keyed_on_something_the_guest_looks_for():
    """And the rule has to correspond to a section the SHIPPED INI asks
    about, or the guest never reports that name and the rule never fires."""
    cp = configparser.ConfigParser(interpolation=None, delimiters=("=",))
    cp.read(INI, encoding="latin-1")
    missing = sorted(set(NOTES) - set(cp.sections()))
    assert not missing, f"notes nothing is looked for: {missing}"


def test_cscript_note_renders_root_and_version():
    note = NOTES["cscript"]
    out = note.render(r"C:\WINDOWS\COMMAND\CSCRIPT.EXE", version="5.0")
    assert r"C:\WINDOWS\COMMAND\CSCRIPT.EXE" in out
    assert "Windows Script Host 5.0" in out
    assert "{root}" not in out
    assert "{version}" not in out


def test_cscript_note_warns_against_wscript():
    body = NOTES["cscript"].body
    assert "wscript" in body.lower()
    assert "modal dialogs" in body or "dialog" in body


def test_cscript_note_includes_batch_and_timeout_flags():
    body = NOTES["cscript"].body
    assert "//nologo" in body
    assert "//B" in body
    assert "//T:" in body


def test_the_jdk_note_names_the_1_1_absences_an_agent_reaches_for():
    """The version number alone does not help: a model writes `ArrayList`
    and `String.format` by reflex, javac 1.1 says `class ArrayList not
    found` or nothing useful, and the agent has been told not to go
    exploring. So the absences are named rather than implied."""
    body = NOTES["jdk"].body
    for absent in ("ArrayList", "HashMap", "Iterator", "generics",
                   "StringBuilder", "String.format", "regex", "Swing"):
        assert absent in body, absent
    # And what to use instead, which is the half that makes it actionable.
    for instead in ("Vector", "Hashtable", "Enumeration", "StringBuffer",
                    "AWT"):
        assert instead in body, instead


def test_the_jdk_note_separates_the_two_kinds_of_failure():
    """Both measured on the machine, 2026-10-01, and they differ -- which is
    worth more than blanket scepticism would have been.

    `javac` with no arguments prints its usage and exits 0; a real compile
    error sets errorlevel and prints `Bad.java:3: ';' expected.` with a
    caret and a count. So the agent is told the code IS trustworthy for the
    case it cares about, and told not to read a 0 from a rejected command
    line as a build.

    IT HAS TO NAME `if errorlevel 1`, not "the exit code": COMMAND.COM
    returns 0 for a failing child whatever happened, so the number that
    reaches this bridge through the shell cannot carry the distinction and
    the test pins the construct that can.
    """
    body = NOTES["jdk"].body
    assert "`if errorlevel 1`" in body
    assert "exits 0" in body


def test_the_jdk_note_renders_root_and_version():
    out = NOTES["jdk"].render(r"C:\JDK1.1.8", version="1.1.8")
    assert "JDK 1.1.8" in out
    assert r"C:\JDK1.1.8\bin\javac" in out
    assert "{root}" not in out and "{version}" not in out


def test_the_jdk_note_invokes_the_tools_by_full_path():
    r"""Measured 2026-10-01 on the real machine: PATH is
    `C:\WINDOWS;C:\WINDOWS\COMMAND` and nothing else, so the JDK is not on
    it and no variable points at it. A bare `javac hello.java` there is
    `Bad command or file name`, which names the program and not the
    environment -- and three sessions have already read that as "the
    compiler is not installed" and gone hunting the disk.
    """
    body = NOTES["jdk"].body
    assert r"{root}\bin\javac" in body
    assert r"{root}\bin\java Hello" in body
    assert "\n        javac " not in body, "a bare javac will not run there"
