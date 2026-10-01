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


# NOTE: this test is VACUOUS UNDER WINE and passes with or without the fix.
# listDirectory used to build "C:\\*" for a drive root, and Wine's
# FindFirstFileA normalises the doubled separator away; real Windows 98 does
# not. Measured 2026-10-01 on an Athlon XP running 98 SE: 0 entries before
# the fix, 29 after, while Wine said 4 either way. It is kept because it
# documents the contract and would catch a regression that broke roots
# everywhere -- but the thing that actually guards this is running fscheck on
# the guest. See the comment on caseList in win/checks/fscheck.cpp.
def test_ls_of_a_drive_root_lists_it(remote):
    """Found on a real 98 machine: every root answered 0 entries while
    C:\\GASTON answered 7, and `dir C:\\ /B` answered 21. A root already
    ends in its separator, so the search pattern came out "C:\\\\*" and
    FindFirstFile refused it."""
    entries = remote.ls("C:\\")
    assert entries, remote.server.log()


def test_a_listing_that_fails_is_not_an_empty_listing(remote,
                                                      tmp_path_factory):
    """The root bug was only invisible because LS threw listDirectory's
    return away, so any failure at all came back as `END count=0`. Here the
    directory stats as a directory and then cannot be opened: the answer has
    to be a refusal, because a count of zero is a lie the caller cannot tell
    from an empty directory."""
    d = tmp_path_factory.mktemp("shut")
    closed = d / "no-entry"
    closed.mkdir()
    (closed / "in-here.txt").write_bytes(b"x")
    closed.chmod(0o000)
    try:
        assert remote.stat(conftest.wine_path(closed)).is_dir, "it stats"
        with pytest.raises(Refused) as e:
            remote.ls(conftest.wine_path(closed))
        assert e.value.code == "locked", e.value.code
    finally:
        closed.chmod(0o700)


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


# --- GET -------------------------------------------------------------------
#
# The transfer is libfs's ChunkSource with a CRC tee'd off the same pass, so
# the checksum covers what actually went out rather than a second read of the
# file. Byte-exact by design: this is not the sync layer, and nothing here
# classifies text, folds line endings or consults .gitattributes.

def test_get_returns_a_file_byte_for_byte(remote, tree):
    got = remote.get(conftest.wine_path(tree / "one.txt"))
    assert got.data == b"hello\n", got.data
    assert got.truncated is False
    assert got.verify(), hex(got.crc)


def test_get_of_a_binary_changes_nothing(remote, tree):
    """Including the bytes a text transfer would have folded. A CRLF pass
    over 0x0d 0x0a in the middle of a PNG is how that bug looks."""
    want = bytes(range(256)) * 4
    got = remote.get(conftest.wine_path(tree / "two.bin"))
    assert got.data == want, (len(got.data), len(want))
    assert got.verify()


def test_get_honours_max_bytes_on_the_guest_and_says_it_stopped(remote, tree):
    """The cap is the guest's. Applied here it would still have the machine
    read the whole thing and send it."""
    got = remote.get(conftest.wine_path(tree / "two.bin"), max_bytes=100)
    assert len(got.data) == 100, len(got.data)
    assert got.truncated is True
    # The CRC covers what was SENT -- for a truncated read, the prefix, which
    # is the only thing there is to check.
    assert got.verify()


def test_get_of_an_empty_file_is_not_an_error(remote, tmp_path_factory):
    d = tmp_path_factory.mktemp("empty")
    (d / "nothing.txt").write_bytes(b"")
    got = remote.get(conftest.wine_path(d / "nothing.txt"))
    assert got.data == b""
    assert got.truncated is False


def test_get_of_a_directory_says_so(remote, tree):
    with pytest.raises(Refused) as e:
        remote.get(conftest.wine_path(tree / "sub"))
    assert e.value.code == "is_directory", e.value.code


def test_get_of_a_missing_file_is_missing(remote, tree):
    with pytest.raises(Refused) as e:
        remote.get(conftest.wine_path(tree / "not-here.txt"))
    assert e.value.code == "missing", e.value.code


def test_a_larger_file_arrives_in_order_and_whole(remote, tmp_path_factory):
    """Several chunks, so the offsets and the reassembly are exercised
    rather than assumed. 40KB is five frames at the 8KB chunk size."""
    d = tmp_path_factory.mktemp("big")
    want = bytes((i * 7 + 3) & 0xFF for i in range(40 * 1024))
    (d / "big.bin").write_bytes(want)
    got = remote.get(conftest.wine_path(d / "big.bin"))
    assert got.data == want, (len(got.data), len(want))
    assert got.verify()


# --- the write half --------------------------------------------------------
#
# PUT is the one with a rule that is not obvious and matters most: it never
# moves the old file aside. The sync layer does, so that no instant has no
# file at the path -- and that is safe only because rc_sync_recover sweeps
# the sync root at startup. Nothing will ever sweep C:\WINDOWS, so out here a
# failure has to leave the original in place or leave nothing at all.

def test_put_writes_a_file(remote, tmp_path_factory):
    d = tmp_path_factory.mktemp("put")
    target = d / "written.txt"
    n = remote.put(conftest.wine_path(target), b"from the other side\n")
    assert n == 20, n
    assert target.read_bytes() == b"from the other side\n"


def test_put_leaves_no_working_file_behind(remote, tmp_path_factory):
    """mem-audit F1's shape, checked on the way in rather than found later:
    the temp is gone whether the commit worked or not."""
    d = tmp_path_factory.mktemp("clean")
    remote.put(conftest.wine_path(d / "f.txt"), b"x" * 100)
    leftovers = [p.name for p in d.iterdir() if ".gaston" in p.name]
    assert not leftovers, leftovers


def test_put_replaces_an_existing_file(remote, tmp_path_factory):
    d = tmp_path_factory.mktemp("replace")
    target = d / "there.txt"
    target.write_bytes(b"the old contents")
    remote.put(conftest.wine_path(target), b"the new contents")
    assert target.read_bytes() == b"the new contents"
    assert not [p for p in d.iterdir() if ".gaston" in p.name]


def test_a_file_bigger_than_one_chunk_arrives_whole(remote, tmp_path_factory):
    """Several DATA frames through the lane's continuation queue, which is
    the part that would deadlock if they went through the single request
    slot and refused each other."""
    d = tmp_path_factory.mktemp("bigput")
    want = bytes((i * 11 + 5) & 0xFF for i in range(50 * 1024))
    n = remote.put(conftest.wine_path(d / "big.bin"), want)
    assert n == len(want), (n, len(want))
    assert (d / "big.bin").read_bytes() == want


def test_a_round_trip_is_byte_exact(remote, tmp_path_factory):
    d = tmp_path_factory.mktemp("roundtrip")
    want = bytes(range(256)) * 37
    remote.put(conftest.wine_path(d / "rt.bin"), want)
    got = remote.get(conftest.wine_path(d / "rt.bin"))
    assert got.data == want
    assert got.verify()


def test_a_put_into_a_directory_that_is_not_there_says_which(remote,
                                                             tmp_path_factory):
    d = tmp_path_factory.mktemp("noparent")
    with pytest.raises(Refused) as e:
        remote.put(conftest.wine_path(d / "nope" / "f.txt"), b"x")
    assert e.value.code == "no_parent", e.value.code


def test_a_chunk_that_does_not_continue_is_refused(remote, tmp_path_factory):
    """A gap would be a hole in somebody's file, so the transfer ends rather
    than committing something that is not what was sent."""
    d = tmp_path_factory.mktemp("gap")
    with pytest.raises(Refused) as e:
        remote.request("PUT", {"path": conftest.wine_path(d / "g.txt"),
                               "off": "4096"}, b"x")
    assert e.value.code == "out_of_order", e.value.code
    assert not list(d.iterdir()), [p.name for p in d.iterdir()]


def test_rm_removes_a_file(remote, tmp_path_factory):
    d = tmp_path_factory.mktemp("rm")
    (d / "gone.txt").write_bytes(b"x")
    remote.rm(conftest.wine_path(d / "gone.txt"))
    assert not (d / "gone.txt").exists()


def test_rm_refuses_a_directory(remote, tree):
    with pytest.raises(Refused) as e:
        remote.rm(conftest.wine_path(tree / "sub"))
    assert e.value.code == "is_directory", e.value.code


def test_rm_of_nothing_is_missing(remote, tmp_path_factory):
    d = tmp_path_factory.mktemp("rmnone")
    with pytest.raises(Refused) as e:
        remote.rm(conftest.wine_path(d / "never.txt"))
    assert e.value.code == "missing", e.value.code


def test_mkdir_creates_parents_and_all(remote, tmp_path_factory):
    d = tmp_path_factory.mktemp("mk")
    remote.mkdir(conftest.wine_path(d / "a" / "b" / "c"))
    assert (d / "a" / "b" / "c").is_dir()


def test_mkdir_of_something_already_there_says_so(remote, tree):
    with pytest.raises(Refused) as e:
        remote.mkdir(conftest.wine_path(tree / "sub"))
    assert e.value.code == "exists", e.value.code


def test_mkdir_where_a_file_sits_says_which(remote, tree):
    with pytest.raises(Refused) as e:
        remote.mkdir(conftest.wine_path(tree / "one.txt"))
    assert e.value.code == "is_file", e.value.code


def test_mv_renames(remote, tmp_path_factory):
    d = tmp_path_factory.mktemp("mv")
    (d / "before.txt").write_bytes(b"same bytes")
    remote.mv(conftest.wine_path(d / "before.txt"),
              conftest.wine_path(d / "after.txt"))
    assert not (d / "before.txt").exists()
    assert (d / "after.txt").read_bytes() == b"same bytes"


def test_mv_refuses_to_replace(remote, tmp_path_factory):
    """A move that silently overwrote something would be a delete nobody
    asked for."""
    d = tmp_path_factory.mktemp("mvover")
    (d / "a.txt").write_bytes(b"a")
    (d / "b.txt").write_bytes(b"b")
    with pytest.raises(Refused) as e:
        remote.mv(conftest.wine_path(d / "a.txt"),
                  conftest.wine_path(d / "b.txt"))
    assert e.value.code == "exists", e.value.code
    assert (d / "b.txt").read_bytes() == b"b"


def test_mv_of_nothing_is_missing(remote, tmp_path_factory):
    d = tmp_path_factory.mktemp("mvnone")
    with pytest.raises(Refused) as e:
        remote.mv(conftest.wine_path(d / "ghost.txt"),
                  conftest.wine_path(d / "elsewhere.txt"))
    assert e.value.code == "missing", e.value.code


def test_a_wrong_crc_refuses_the_commit_and_leaves_nothing(remote,
                                                           tmp_path_factory):
    """The verifier is what makes PUT safe to point at a binary: a truncated
    gaston.exe is a bricked client. The CRC is compared against what was
    accumulated on the way in, not by re-reading the temp -- the bytes went
    past once and the platter should not have to do it again.
    """
    from bridge.wire.frame import Frame
    from gremote.client import _Call

    d = tmp_path_factory.mktemp("badcrc")
    target = d / "never-arrives.bin"
    ident = "crc-test"
    # The same sequence put() sends, with a CRC that is not the data's.
    # Driven with raw frames because put() computes an honest one, and the
    # point here is what the guest does with a dishonest one.
    call = _Call()
    with remote._calls_lock:
        remote._calls[ident] = call
    try:
        remote._send(Frame("PUT", {"id": ident,
                                   "path": conftest.wine_path(target),
                                   "off": "0"}, b"these are the bytes"))
        remote._send(Frame("END", {"id": ident, "crc": "deadbeef"}))
        reply = call.reply if call.done.wait(30) else None
    finally:
        with remote._calls_lock:
            remote._calls.pop(ident, None)

    assert reply is not None, "no answer to a PUT with a bad CRC"
    assert reply.type == "ERR", reply.headers
    assert reply.headers.get("code") == "crc_mismatch", reply.headers
    assert not target.exists(), "a file that failed its CRC was committed"
    assert not list(d.iterdir()), [p.name for p in d.iterdir()]
