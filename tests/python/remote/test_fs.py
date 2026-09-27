"""The fs lane over libfs: LS, STAT, DRIVES.

libfs has been moving real files on a real 98 box since REMOTE.md step 5a, so
what is new here is the wire in front of it, and that is what these settle:
the record formats, the refusals, and that the distinctions libfs draws
survive the trip. The one that matters most is `missing` against `locked` --
a caller told "unreadable" about a file that simply is not there retries
forever, which is the bug the two answers exist to prevent.

Wine gives real FindFirstFile and real GetVolumeInformation over its Z: drive
mapping, which settles the formats. It does not settle FAT's 8.3 aliasing or
what an empty floppy does to the critical-error handler; those are the
guest's.
"""

import conftest
import pytest

from gremote import Refused, Remote

pytestmark = conftest.needs_guest

SECRET = "not-the-default"


def ini(port: int) -> str:
    return (
        "[server]\n"
        "bind=127.0.0.1\n"
        f"port={port}\n"
        f"secret={SECRET}\n"
        "allow=\n"
        "heartbeat_timeout_s=30\n"
    )


@pytest.fixture(scope="module")
def tree(tmp_path_factory):
    """A small directory the guest can see through Wine's Z: drive."""
    d = tmp_path_factory.mktemp("tree")
    (d / "one.txt").write_bytes(b"hello\n")
    (d / "two.bin").write_bytes(bytes(range(256)) * 4)
    (d / "a name with spaces.txt").write_bytes(b"x")
    (d / "sub").mkdir()
    (d / "sub" / "deep.txt").write_bytes(b"deeper\n")
    return d


@pytest.fixture(scope="module")
def remote(tmp_path_factory):
    port = conftest.free_port()
    d = tmp_path_factory.mktemp("fs")
    with conftest.GuestServer("gremote.exe", d, ini(port), port) as server:
        with Remote("127.0.0.1", SECRET, port=port) as r:
            r.server = server
            yield r


def test_ls_lists_one_directory(remote, tree):
    entries = {e.name: e for e in remote.ls(conftest.wine_path(tree))}
    assert set(entries) == {"one.txt", "two.bin", "a name with spaces.txt",
                            "sub"}, sorted(entries)
    assert entries["one.txt"].size == 6, entries["one.txt"]
    assert entries["sub"].is_dir, entries["sub"]
    assert not entries["one.txt"].is_dir


def test_a_name_with_spaces_survives_the_line_format(remote, tree):
    """The name is the last field for exactly this reason. It is also why a
    line format is safe at all: a Windows path cannot contain a newline."""
    names = [e.name for e in remote.ls(conftest.wine_path(tree))]
    assert "a name with spaces.txt" in names, names


def test_ls_does_not_recurse(remote, tree):
    names = [e.name for e in remote.ls(conftest.wine_path(tree))]
    assert "deep.txt" not in names, names


def test_dot_and_dotdot_are_never_listed(remote, tree):
    names = [e.name for e in remote.ls(conftest.wine_path(tree))]
    assert "." not in names and ".." not in names, names


def test_ls_of_a_missing_directory_says_missing(remote, tree):
    with pytest.raises(Refused) as e:
        remote.ls(conftest.wine_path(tree / "nowhere"))
    assert e.value.code == "missing", e.value.code


def test_ls_of_a_file_is_not_an_empty_listing(remote, tree):
    """An empty listing would be a lie that reads like a fact."""
    with pytest.raises(Refused) as e:
        remote.ls(conftest.wine_path(tree / "one.txt"))
    assert e.value.code == "not_a_directory", e.value.code


def test_ls_stops_at_the_cap_and_admits_it(remote, tmp_path_factory):
    d = tmp_path_factory.mktemp("many")
    for i in range(12):
        (d / f"f{i:02d}.txt").write_bytes(b"x")
    entries = remote.ls(conftest.wine_path(d), max_entries=5)
    assert len(entries) == 5, len(entries)


def test_stat_reports_a_file(remote, tree):
    st = remote.stat(conftest.wine_path(tree / "one.txt"))
    assert st.type == "f", st
    assert st.size == 6, st
    assert st.mtime > 0, st


def test_stat_reports_a_directory(remote, tree):
    st = remote.stat(conftest.wine_path(tree / "sub"))
    assert st.is_dir, st


def test_stat_of_nothing_is_missing_not_locked(remote, tree):
    """The distinction libfs draws, carried onto the wire. A caller told
    "locked" about a file that is not there retries until its budget runs
    out; told "missing", it stops."""
    with pytest.raises(Refused) as e:
        remote.stat(conftest.wine_path(tree / "no-such-file"))
    assert e.value.code == "missing", e.value.code


def test_an_empty_path_is_refused(remote):
    with pytest.raises(Refused) as e:
        remote.request("STAT", {"path": ""})
    assert e.value.code == "bad_path", e.value.code


def test_drives_says_which_letters_exist(remote):
    """`dir` can list one volume. It cannot say which letters there are, or
    which of them is a CD -- which is the whole reason this op exists."""
    found = remote.drives()
    assert found, remote.server.log()
    letters = {d.letter for d in found}
    assert "C" in letters, letters
    assert all(len(d.letter) == 1 for d in found), found


def test_a_drive_with_media_is_not_confused_with_an_empty_one(remote):
    """has_media comes from GetVolumeInformation succeeding, never from
    free bytes. A mounted CD has none by definition, and inferring
    emptiness that way reported every disc as an empty drive -- a bug found
    by running it on the guest, not by reading it."""
    c = [d for d in remote.drives() if d.letter == "C"]
    assert c, "no C: at all"
    assert c[0].has_media is True, c[0]
    assert c[0].total_kb > 0, c[0]
