"""Parsing what the guest said about itself.

The body arrives DURING THE HANDSHAKE, before the bridge has decided whether
to accept this client, and it is written by a program the user can point at
an INI they edited themselves. So the property being tested here is not
"correct rows parse correctly" -- it is that nothing in this file can refuse
a machine. A malformed row costs one missing inventory line; an exception
here costs the user their computer, which is the trade `bridge/files/
manifest.py` makes for the same reason and `Baseline` deliberately does not.
"""

import logging

import pytest

from bridge.agent.toolrep import MAX_FACTS, Fact, parse_rows


def row(name: str, kind: str, value: str, length: int | None = None) -> bytes:
    raw = value.encode("latin-1")
    n = len(raw) if length is None else length
    return f"{name} {kind} {n} ".encode("latin-1") + raw + b"\n"


def test_a_path_with_spaces_survives():
    """The reason the format has a length prefix at all. `C:\\Program
    Files\\...` is the common case, not an exotic one."""
    body = row("msvc", "file", r"C:\Program Files\DevStudio\VC\BIN\CL.EXE")
    assert parse_rows(body) == [
        Fact("msvc", "file", r"C:\Program Files\DevStudio\VC\BIN\CL.EXE")]


def test_an_env_fact_splits_into_a_name_and_contents():
    f = parse_rows(row("watcom", "env", "WATCOM=C:\\WATCOM"))[0]
    assert f.var == "WATCOM"
    assert f.contents == "C:\\WATCOM"


def test_a_value_containing_an_equals_sign_splits_only_once():
    """A PATH entry can contain `=`; only the first one delimits."""
    f = parse_rows(row("machine", "env", "PATH=C:\\A;C:\\B=x"))[0]
    assert f.var == "PATH"
    assert f.contents == "C:\\A;C:\\B=x"


def test_a_file_fact_has_no_var_or_contents():
    f = parse_rows(row("watcom", "file", "C:\\WATCOM\\BINNT\\WCC386.EXE"))[0]
    assert f.var == ""
    assert f.contents == ""


def test_a_length_that_disagrees_is_skipped_not_truncated():
    """The prefix exists to catch exactly this. Taking a prefix of a row
    whose length cannot be trusted would put a half-path in the prompt and
    present it as measured -- worse than losing the row."""
    body = row("watcom", "file", "C:\\WATCOM\\BINNT\\WCC386.EXE", length=5)
    assert parse_rows(body) == []


def test_a_bad_row_does_not_take_its_neighbours_with_it():
    body = (row("command.com", "file", "C:\\WINDOWS\\COMMAND.COM")
            + b"this is not a row at all\n"
            + row("watcom", "env", "WATCOM=C:\\WATCOM"))
    got = parse_rows(body)
    assert [f.name for f in got] == ["command.com", "watcom"]


@pytest.mark.parametrize("body", [
    b"", b"\n\n\n", b"\xff\xfe\x00\x01", b"a b c d",
    b"watcom file notanumber x\n",
    b"watcom file 5",                      # no terminator, short value
    b"watcom\n", b"watcom file\n",
    b"x " * 40000,
])
def test_nothing_can_make_this_raise(body):
    """Every one of these is a real connection attempt if it happens. The
    machine must still attach."""
    assert isinstance(parse_rows(body), list)


def test_a_final_row_without_a_newline_is_still_read():
    """The guest always terminates, but a truncated send or a future writer
    might not, and dropping the last fact silently is the false negative this
    whole feature exists to avoid."""
    body = row("watcom", "file", "C:\\WATCOM\\W.EXE").rstrip(b"\n")
    assert len(parse_rows(body)) == 1


def test_an_unknown_kind_is_dropped_rather_than_guessed_at():
    assert parse_rows(row("watcom", "registry", "HKLM\\Software")) == []


def test_the_fact_count_is_capped():
    body = b"".join(row(f"t{i}", "file", f"C:\\T{i}") for i in range(MAX_FACTS * 2))
    assert len(parse_rows(body)) == MAX_FACTS


def test_hitting_the_cap_is_logged_rather_than_silent(caplog):
    body = b"".join(row(f"t{i}", "file", f"C:\\T{i}") for i in range(MAX_FACTS * 2))
    with caplog.at_level(logging.WARNING, logger="bridge.toolrep"):
        parse_rows(body)
    assert any("stopped at" in r.getMessage() for r in caplog.records)


def test_a_skipped_row_says_so_in_the_log(caplog):
    """Silence here is a false negative, and a false negative is the
    expensive direction -- the agent goes exploring for something that was
    detected and thrown away."""
    with caplog.at_level(logging.WARNING, logger="bridge.toolrep"):
        parse_rows(row("watcom", "file", "C:\\W", length=99))
    assert caplog.records


class _Caps:
    acp = "cp1252"


def test_the_guest_codepage_decodes_a_high_byte_path():
    """`acp` on the wire is a codepage NUMBER; the codec name comes from
    caps. Handing the raw header to `bytes.decode` is the mistake this
    argument exists to prevent."""
    raw = "C:\\Bücher".encode("cp1252")
    body = b"watcom file " + str(len(raw)).encode() + b" " + raw + b"\n"
    assert parse_rows(body, _Caps()).pop().value == "C:\\Bücher"


def test_an_unusable_codec_still_yields_facts():
    """A body can arrive before caps are settled. latin-1 round-trips every
    byte, which is survivable; an exception during the handshake is not."""
    class Broken:
        acp = None
    assert len(parse_rows(row("watcom", "file", "C:\\W"), Broken())) == 1
