r"""Facts to inventory: the layer that decides what the machine HAS.

This is where a guess becomes an assertion the agent acts on, so the tests
are mostly about restraint. The expensive failure is not a missing row -- the
agent goes looking, which is what it did before this existed. It is a WRONG
row: the inventory is the only place a toolchain is named, so it is the one
the agent reaches for, and `turboc toolchain C:\TC` on a machine without
Turbo C gets `tcc hello.c` for a Win32 GUI app that Turbo C 2.01 cannot build
at all -- followed by a failure the agent has been told not to explore.

The other property here is that the rules and the shipped INI cannot drift.
The INI is what the guest looks at; the rules are what the bridge does with
the answers. A rule keyed on a section nobody ships is dead code, and a
section nobody has a rule for is a scan the machine pays for and nothing
reads.
"""

import configparser
import pathlib

import pytest

from bridge.agent.toolrep import Fact
from bridge.agent.toolrules import RULES, Found, inventory, path_dirs, root_of

INI = pathlib.Path(__file__).resolve().parents[2] / "win" / "tools_detect.ini"


def test_a_marker_file_walks_up_to_the_root():
    r"""BINNT\WCC386.EXE is what exists; C:\WATCOM is what the agent needs,
    because INCLUDE and LIB are relative to it."""
    got = inventory([Fact("watcom", "file", r"C:\WATCOM\BINNT\WCC386.EXE")])
    assert got == [Found("watcom", "toolchain", r"C:\WATCOM", True)]


def test_a_root_with_spaces_survives_the_walk():
    got = inventory([Fact("msvc", "file",
                          r"C:\Program Files\DevStudio\VC\BIN\CL.EXE")])
    assert got[0].path == r"C:\Program Files\DevStudio\VC"


def test_walking_up_never_produces_a_bare_drive_letter():
    r"""`C:` is not a directory on DOS, it is whatever that drive's current
    directory happens to be -- so a root written that way means something
    different every time the agent uses it."""
    got = inventory([Fact("watcom", "file", r"C:\W.EXE")])
    assert got[0].path == "C:\\"


def test_a_path_on_linux_is_not_used_to_split_a_windows_one():
    r"""`os.path.dirname(r"C:\A\B")` is the whole string on Linux, because
    the separator is `/`. bridge/files/paths.py exists because that mistake
    was made once already."""
    got = inventory([Fact("turboc", "file", r"C:\TC\BIN\TCC.EXE")])
    assert got[0].path == r"C:\TC"


def test_an_env_variable_alone_is_reported_but_not_claimed_as_verified():
    got = inventory([Fact("watcom", "env", r"WATCOM=C:\WATCOM")])
    assert got == [Found("watcom", "toolchain", r"C:\WATCOM", False)]
    assert "not confirmed" in got[0].basis


def test_a_seen_file_beats_a_stated_variable():
    """Both name the same tool and they disagree. The file was looked at;
    the variable was merely asserted by the machine."""
    got = inventory([
        Fact("watcom", "env", r"WATCOM=D:\STALE"),
        Fact("watcom", "file", r"C:\WATCOM\BINNT\WCC386.EXE"),
    ])
    assert got == [Found("watcom", "toolchain", r"C:\WATCOM", True)]


def test_a_variable_that_is_not_this_rules_variable_is_ignored():
    """INCLUDE is set by SETVARS.BAT and points at `h`, not at the root.
    Treating any env fact in a section as the root would name the wrong
    directory with full confidence."""
    assert inventory([Fact("watcom", "env", r"INCLUDE=C:\WATCOM\H")]) == []


def test_an_empty_variable_names_nothing():
    assert inventory([Fact("watcom", "env", "WATCOM=")]) == []


def test_a_section_with_no_rule_is_dropped_rather_than_guessed_at():
    """`tools.ini` is user-editable, so a section name is a LABEL and never
    an assertion about what the tool is. An unknown one gets no kind, no
    root-walk and no row."""
    assert inventory([Fact("norton", "file", r"C:\NU\NDD.EXE")]) == []


def test_nothing_found_says_nothing():
    """Not a row reading "not found". That is a line the agent reads on
    every request to learn nothing."""
    assert inventory([]) == []


def test_the_first_marker_wins():
    """The INI lists markers most-likely-first. Two Watcoms is a situation
    the agent should discover, not one this silently picks between."""
    got = inventory([
        Fact("watcom", "file", r"C:\WATCOM\BINNT\WCC386.EXE"),
        Fact("watcom", "file", r"D:\OW\BINNT\WCC386.EXE"),
    ])
    assert got[0].path == r"C:\WATCOM"


def test_the_shell_is_listed_first():
    """Which shell is present changes how every command must be written, so
    it is the fact with the widest blast radius."""
    got = inventory([
        Fact("watcom", "file", r"C:\WATCOM\BINNT\WCC386.EXE"),
        Fact("command.com", "file", r"C:\WINDOWS\COMMAND.COM"),
    ])
    assert [f.name for f in got] == ["command.com", "watcom"]


def test_a_trailing_backslash_is_not_carried_into_the_prompt():
    got = inventory([Fact("watcom", "env", "WATCOM=C:\\WATCOM\\")])
    assert got[0].path == r"C:\WATCOM"


def test_path_is_split_on_semicolons():
    facts = [Fact("machine", "env", r"PATH=C:\WINDOWS;C:\WINDOWS\COMMAND;")]
    assert path_dirs(facts) == [r"C:\WINDOWS", r"C:\WINDOWS\COMMAND"]


def test_path_is_empty_when_the_machine_did_not_report_it():
    assert path_dirs([]) == []


# --- the two halves must not drift -----------------------------------------

def _ini() -> configparser.ConfigParser:
    cp = configparser.ConfigParser()
    # Windows paths contain `%` (`%WINDIR%`) and `:`; neither interpolation
    # nor `:` as a delimiter belongs in a DOS INI.
    cp = configparser.ConfigParser(interpolation=None, delimiters=("=",))
    cp.optionxform = str.lower
    cp.read(INI, encoding="latin-1")
    return cp


def test_every_rule_has_a_section_that_ships():
    """A rule for a section nothing ships is dead code -- the guest will
    never report that name, so the rule can be wrong for years."""
    missing = sorted(set(RULES) - set(_ini().sections()))
    assert not missing, f"rules with no shipped section: {missing}"


def test_every_shipped_section_has_a_rule():
    """The other direction, and it is the one that costs the user. A section
    with no rule is a disk scan on a 200MHz machine whose answer the bridge
    then drops on the floor."""
    extra = sorted(set(_ini().sections()) - set(RULES))
    assert not extra, f"sections nothing interprets: {extra}"


@pytest.mark.parametrize("name", sorted(RULES))
def test_every_rule_declares_a_kind(name):
    assert RULES[name].kind in ("shell", "toolchain", "tool")


@pytest.mark.parametrize("name", sorted(RULES))
def test_a_rules_env_names_are_actually_asked_for(name):
    """A rule that reads `WATCOM` while the INI asks for `WATCOMDIR` is a
    tool that is never found. The bridge only ever sees variables the guest
    was told to look at."""
    rule = RULES[name]
    if not rule.env:
        return
    section = _ini()[name]
    asked = {section[k].strip().upper()
             for k in section if k.startswith("env")}
    for var in rule.env:
        assert var.upper() in asked, f"[{name}] never asks for {var}"


@pytest.mark.parametrize("name", sorted(RULES))
def test_every_shipped_marker_resolves_to_the_same_root_shape(name):
    r"""The reason `bins` replaced a hop count.

    `[turboc]` lists `C:\TC\BIN\TCC.EXE` AND `C:\TC\TCC.EXE`, because 2.01
    installs flat and 3.x does not, and whichever is on the disk is the one
    reported. "Drop two components" is right for one of them and turns the
    other into `C:` -- a wrong root presented as verified, which is the
    expensive failure this whole layer is trying not to produce.
    """
    rule = RULES[name]
    section = _ini()[name]
    roots = set()
    for key in (k for k in section if k.startswith("file")):
        value = section[key].strip()
        if not value:
            continue
        root = root_of(value, rule)
        assert root.rstrip("\\"), f"[{name}] {key}={value} resolved to nothing"
        assert len(root.rstrip("\\")) > 2, (
            f"[{name}] {key}={value} resolved to the bare drive {root!r}")
        if rule.bins is not None:
            last = root.rstrip("\\").rsplit("\\")[-1].upper()
            assert last not in tuple(b.upper() for b in rule.bins), (
                f"[{name}] {key}={value} left {last} in the root")
        roots.add(root.upper())
    # Not that they agree -- the INI lists alternative install locations on
    # purpose -- only that each is a directory and not a drive.
    assert roots or not rule.bins


@pytest.mark.parametrize("name", sorted(RULES))
def test_a_single_program_reports_the_program_and_not_its_folder(name):
    r"""`C:\WINDOWS\COMMAND` is where COMMAND.COM's friends live, and saying
    the shell is a directory would be a lie the agent then quotes."""
    rule = RULES[name]
    if rule.bins is not None:
        return
    section = _ini()[name]
    for key in (k for k in section if k.startswith("file")):
        value = section[key].strip()
        if value:
            assert root_of(value, rule) == value
