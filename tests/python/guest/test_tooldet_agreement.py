r"""Does the guest's detector produce what the bridge's parser reads?

The same property `test_guest_agreement.py` establishes for the sync
classifier, for the same reason: two implementations of one format, in two
languages, that agree today and have no way of noticing when they stop. Here
the consequence is quieter than a file that never converges -- the inventory
simply comes up empty, the agent goes exploring, and nothing anywhere errors.
A silent false negative is the expensive direction for this whole feature.

So `toolcheck.exe` is the REAL `win/app/tooldet.cpp` -- the code that ships --
run under Wine against a synthetic tree, and its bytes go straight into
`toolrep.parse_rows`. Wine cannot model a FAT directory or Win9x's INI
caching, but `GetPrivateProfileStringA` and `GetFileAttributesA` are exactly
what it does model well, which is all this needs. The things it cannot check
are on the guest: `docs/PROGRESS.md` has the run.
"""

import os
import subprocess

import pytest

from bridge.agent.toolrep import parse_rows
from bridge.agent.toolrules import inventory

import conftest
from conftest import wine_env

pytestmark = conftest.needs_guest

EXE_NAME = "toolcheck.exe"


def run(tmp_path, ini: str, *, env=None, user_ini: str | None = None) -> bytes:
    """Write an INI into a directory and ask the guest what it sees there."""
    (tmp_path / "tools_detect.ini").write_text(ini, encoding="latin-1")
    if user_ini is not None:
        (tmp_path / "tools.ini").write_text(user_ini, encoding="latin-1")
    e = wine_env()
    e.update(env or {})
    out = subprocess.run(
        [conftest.WINE, conftest.guest_exe(EXE_NAME), str(tmp_path)],
        capture_output=True, env=e, timeout=120)
    return out.stdout


def facts(body: bytes) -> list:
    """Parsed rows, minus the ones reported unconditionally.

    PATH always comes back under `[machine]` whatever the INI says -- every
    rule on the bridge wants it and no section should have to ask. Most
    tests here are about one section, so they say so rather than each
    remembering to skip it."""
    return [f for f in parse_rows(body) if f.name != "machine"]


def make(tmp_path, *parts) -> str:
    """A file at a DOS path Wine can see, returned as that DOS path."""
    p = tmp_path.joinpath(*parts)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"MZ")
    return "Z:" + str(p).replace("/", "\\")


def test_a_file_that_exists_round_trips_through_both_sides(tmp_path):
    exe = make(tmp_path, "WATCOM", "BINNT", "WCC386.EXE")
    body = run(tmp_path, f"[watcom]\nfile1={exe}\n")
    assert [(f.name, f.kind, f.value) for f in facts(body)] == [
        ("watcom", "file", exe)]


def test_a_file_that_does_not_exist_is_not_reported(tmp_path):
    r"""Silence, not a row saying "no". The bridge has no way to tell a
    guessed absence from a measured one, and neither has the agent."""
    body = run(tmp_path, "[watcom]\nfile1=Z:\\nowhere\\NOPE.EXE\n")
    assert facts(body) == []


def test_a_path_with_spaces_survives_the_wire(tmp_path):
    """The reason the row format carries a byte count at all."""
    exe = make(tmp_path, "Program Files", "DevStudio", "VC", "BIN", "CL.EXE")
    assert facts(run(tmp_path, f"[msvc]\nfile1={exe}\n"))[0].value == exe


def test_an_environment_variable_is_reported_with_its_value(tmp_path):
    body = run(tmp_path, "[watcom]\nenv1=WATCOM\n",
               env={"WATCOM": "C:\\WATCOM"})
    f = facts(body)[0]
    assert (f.kind, f.var, f.contents) == ("env", "WATCOM", "C:\\WATCOM")


def test_an_unset_variable_is_not_reported(tmp_path):
    body = run(tmp_path, "[watcom]\nenv1=NOSUCHVARIABLE\n")
    assert facts(body) == []


def test_path_is_reported_without_being_asked_for(tmp_path):
    """Every rule on the bridge wants it, so no section has to ask -- and a
    toolchain wired up only through PATH is otherwise invisible.

    The CONTENTS are Wine's here, not this test's: Wine builds the Windows
    PATH from its own prefix and ignores the Unix one it was launched with.
    So the property being checked is that the row arrives at all from an INI
    that never mentions it, which is the part the guest is responsible for.
    """
    body = run(tmp_path, "[watcom]\nfile1=Z:\\nope\n")
    paths = [f for f in parse_rows(body) if f.var == "PATH"]
    assert len(paths) == 1
    assert paths[0].name == "machine"
    assert paths[0].contents.strip()


def test_the_users_ini_is_read_after_the_shipped_one(tmp_path):
    """`tools.ini` is never shipped and never overwritten by a deploy. It is
    read second so the user's answer is the one the bridge sees last."""
    mine = make(tmp_path, "OW", "BINNT", "WCC386.EXE")
    theirs = make(tmp_path, "WATCOM", "BINNT", "WCC386.EXE")
    body = run(tmp_path, f"[watcom]\nfile1={theirs}\n",
               user_ini=f"[watcom]\nfile1={mine}\n")
    values = [f.value for f in facts(body)]
    assert values == [theirs, mine]


def test_a_missing_shipped_ini_is_not_an_error(tmp_path):
    """An install that did not copy it, or a user who deleted it. The client
    still connects and the machine simply reports nothing."""
    out = subprocess.run(
        [conftest.WINE, conftest.guest_exe(EXE_NAME), str(tmp_path)],
        capture_output=True, env=wine_env(), timeout=120)
    assert out.returncode == 0
    # PATH still comes back; nothing else does, because there is nothing to
    # look at.
    assert facts(out.stdout) == []


def test_a_directory_counts_as_existing(tmp_path):
    r"""Some markers are directories (`{root}\H`), and
    `GetFileAttributesA` does not distinguish. Asserting the behaviour
    rather than assuming it, because a rule author will rely on it."""
    d = tmp_path / "WATCOM" / "H"
    d.mkdir(parents=True)
    dos = "Z:" + str(d).replace("/", "\\")
    assert len(facts(run(tmp_path, f"[watcom]\nfile1={dos}\n"))) == 1


def test_the_whole_chain_produces_an_inventory(tmp_path):
    """End to end: what the guest sees, through the wire format, through the
    rules, to the rows that go in the prompt."""
    com = make(tmp_path, "WINDOWS", "COMMAND.COM")
    wat = make(tmp_path, "WATCOM", "BINNT", "WCC386.EXE")
    body = run(tmp_path,
               f"[command.com]\nfile1={com}\n[watcom]\nfile1={wat}\n")
    found = inventory(facts(body))
    assert [(f.name, f.kind) for f in found] == [
        ("command.com", "shell"), ("watcom", "toolchain")]
    assert found[1].path.endswith("WATCOM")
    assert all(f.verified for f in found)


def test_more_sections_than_the_cap_does_not_produce_a_truncated_row(tmp_path):
    """A partial list is honest; half a path presented as measured is not."""
    lines = []
    for i in range(80):
        lines.append(f"[t{i}]\nfile1={make(tmp_path, f'T{i}.EXE')}\n")
    body = run(tmp_path, "".join(lines))
    # Whatever survived must be well-formed -- that is the property. The
    # parser is the judge: it drops any row whose stated length disagrees.
    assert body.count(b"\n") == len(parse_rows(body))


def test_the_output_is_the_bytes_that_go_on_the_wire(tmp_path):
    r"""`toolcheck` prints `rc_tooldet_cached` raw. If it ever reformatted --
    trimmed a line, translated a `\n` -- this suite would be testing a second
    implementation that happens to agree, which is the one thing it exists to
    rule out."""
    exe = make(tmp_path, "WATCOM", "BINNT", "WCC386.EXE")
    body = run(tmp_path, f"[watcom]\nfile1={exe}\n")
    assert body.endswith(b"\n")
    assert b"\r\n" not in body
    assert body.split(b"\n")[-2].startswith(b"watcom file ")
