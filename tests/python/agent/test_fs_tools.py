"""The agent's view of get_file, put_file and drives.

What these pin is the surface, not the transport: the tools exist, they name
no operating system, they refuse clearly when there is no machine, and the two
decisions that could quietly hurt somebody -- where a fetched binary lands,
and what `from_path` can reach -- behave as intended.
"""
from __future__ import annotations

import asyncio
import pathlib

from bridge.agent import tools as machine_tools
from bridge.services.guest_fs import FsError


class FakeFs:
    def __init__(self, *, data=b"", raise_status="", drives=()):
        self.data = data
        self.raise_status = raise_status
        self._drives = list(drives)
        self.put_calls: list = []
        self.probe_calls: list = []

    async def get(self, guest, path, *, max_bytes=0):
        if self.raise_status:
            raise FsError(self.raise_status)
        import zlib

        from bridge.services.guest_fs import FileBytes
        return FileBytes(data=self.data,
                         crc=zlib.crc32(self.data) & 0xFFFFFFFF,
                         truncated=False)

    async def put(self, guest, path, data):
        if self.raise_status:
            raise FsError(self.raise_status)
        self.put_calls.append((path, data))
        return len(data)

    async def drives(self, guest, *, probe=False):
        if self.raise_status:
            raise FsError(self.raise_status)
        self.probe_calls.append(probe)
        return self._drives


class FakeGuest:
    #: A client that declares the operations added with `input` -- list_dir
    #: is gated on it (an older client hung it for four minutes).
    from bridge.files import caps as _C
    caps = _C.Caps(ops=1)
    work_dir = ""
    work_guest = ""


def _server(fs=None, workdir=""):
    return machine_tools.build(None, None, None, FakeGuest() if fs else None,
                               fs=fs, workdir=workdir)["machine"]["instance"]


class _Params:
    """What the SDK's tools/call handler reads off its params object."""

    def __init__(self, name, arguments):
        self.name = name
        self.arguments = arguments


def _call(srv, name, args):
    handler = srv._request_handlers["tools/call"].handler
    return asyncio.run(handler(None, _Params(name, args)))


def _body(res) -> str:
    content = res["content"] if isinstance(res, dict) else res.content
    out = []
    for c in content:
        out.append(c["text"] if isinstance(c, dict) else c.text)
    return "\n".join(out)


def _descriptions() -> dict:
    srv = machine_tools.build(None, None, None, None)["machine"]["instance"]
    listed = asyncio.run(srv._request_handlers["tools/list"].handler(None, None))
    return {t.name: (t.description or "") for t in listed.tools}


def test_the_three_tools_are_offered():
    d = _descriptions()
    for name in ("get_file", "put_file", "drives"):
        assert name in d, f"{name} is not on the machine server"


def test_no_tool_names_one_operating_system():
    """The schema goes to every machine. Which one is attached is a fact the
    system prompt carries, not the tool descriptions."""
    d = _descriptions()
    for name in ("get_file", "put_file", "drives"):
        for leak in ("Windows", "Win98", "DOS", "C:\\", "COMMAND.COM",
                     "cmd.exe", "Linux"):
            assert leak not in d[name], f"{name} names {leak}"


def test_a_directory_listing_IS_offered_and_here_is_why():
    """WAS `test_ls_is_not_offered`, whose reasoning was "the machine's own
    shell lists a directory perfectly well". It does not, and the numbers that
    settled it were measured on real Windows 98 on 2026-10-01
    (docs/measurements/batch-eol-and-dir-b-p200-20261001.txt) -- twelve files
    in one directory:

        dir /b <path>    131 bytes -- names, no sizes, no dates
        dir    <path>    about 1,100 -- volume label, serial number, a
                         directory header, `.` and `..`, a free-space footer,
                         and BOTH the 8.3 and the long name on every row

    So `exec` gives names cheaply or details expensively and never both;
    neither answer is machine-readable; both arrive in the CONSOLE codepage
    while long names are stored in the ANSI one; and a path containing a space
    has to be quoted for a shell that splits on whitespace. The session that
    produced those numbers ran plain `dir` six times to check single files it
    had just compiled.

    CLAUDE.md's bar is unchanged -- this clears it rather than lowering it.
    `stat` does not: `list_dir` answers the same question for a whole
    directory, and a single file's size and date come back in the listing of
    the folder it is in.
    """
    d = _descriptions()
    assert "list_dir" in d
    assert "ls" not in d           # OS-neutral naming, not the Unix verb
    assert "stat" not in d


def test_get_file_returns_text_as_text():
    fs = FakeFs(data=b"hello from the machine\r\n")
    res = _call(_server(fs), "get_file", {"path": "X"})
    assert "hello from the machine" in _body(res)


def test_get_file_saves_a_binary_beside_the_folder_and_never_inside_it(
        tmp_path):
    """The working directory IS the mirror.

    A fetched binary written into it is reported by the next scan as a new
    file and pushed BACK to the machine it came from. Conflict staging already
    sits one level above the working directory for exactly this reason, and
    this follows it.
    """
    workdir = tmp_path / "conv"
    workdir.mkdir()
    fs = FakeFs(data=b"MZ\x00\x00binary")
    res = _call(_server(fs, str(workdir)), "get_file",
                {"path": "Q:\\PROG.EXE"})
    body = _body(res)

    # One subdirectory per SOURCE PATH since 2026-10-02, keyed on a hash of
    # it: the name used to be the basename alone, so two `CONFIG.SYS` from
    # different directories silently overwrote each other and the agent was
    # handed a path that no longer held what it thought.
    fetched = tmp_path / ".gaston" / "fetched"
    saved = [q for q in fetched.rglob("PROG.EXE")]
    assert len(saved) == 1, body
    assert saved[0].read_bytes() == b"MZ\x00\x00binary"
    assert str(saved[0]) in body
    # The load-bearing half: nothing landed under the mirrored folder.
    assert not any(p.name == "PROG.EXE"
                   for p in workdir.rglob("*")), "a fetched file is in the mirror"


def test_two_files_with_one_name_do_not_overwrite_each_other(tmp_path):
    """`C:\\A\\CONFIG.SYS` then `C:\\B\\CONFIG.SYS` used to land on the same
    local path, so the second replaced the first and the agent was told a
    location that no longer held what it had just fetched."""
    workdir = tmp_path / "conv"
    workdir.mkdir()
    a = _body(_call(_server(FakeFs(data=b"\x00first"), str(workdir)),
                    "get_file", {"path": "C:\\A\\CONFIG.SYS"}))
    b = _body(_call(_server(FakeFs(data=b"\x00second"), str(workdir)),
                    "get_file", {"path": "C:\\B\\CONFIG.SYS"}))
    pa = a.strip().splitlines()[-1].strip()
    pb = b.strip().splitlines()[-1].strip()
    assert pa != pb, (pa, pb)
    assert pathlib.Path(pa).read_bytes() == b"\x00first"
    assert pathlib.Path(pb).read_bytes() == b"\x00second"


def test_a_refusal_is_reported_in_the_machines_own_word():
    fs = FakeFs(raise_status="missing")
    res = _call(_server(fs), "get_file", {"path": "NOPE"})
    assert "nothing at" in _body(res)


def test_an_unknown_status_still_says_something_useful():
    fs = FakeFs(raise_status="wat")
    body = _body(_call(_server(fs), "get_file", {"path": "P"}))
    assert "wat" in body


def test_put_file_refuses_both_sources_and_neither():
    fs = FakeFs()
    for args in ({"path": "P"},
                 {"path": "P", "content": "x", "from_path": "y"}):
        body = _body(_call(_server(fs), "put_file", args))
        assert "either" in body.lower()
    assert fs.put_calls == []


def test_put_file_from_path_cannot_reach_outside_the_folder(tmp_path):
    """Writing anywhere on the GUEST is the user's call, made per approval.

    Reading anywhere on the BRIDGE is not the same decision and they cannot
    see it, so `from_path` is confined to the conversation's own folder.
    """
    workdir = tmp_path / "conv"
    workdir.mkdir()
    outside = tmp_path / "secret.txt"
    outside.write_text("not yours")

    fs = FakeFs()
    body = _body(_call(_server(fs, str(workdir)), "put_file",
                       {"path": "Q:\\X.TXT", "from_path": "../secret.txt"}))
    assert "own folder" in body
    assert fs.put_calls == []


def test_put_file_from_path_sends_a_file_in_the_folder_byte_for_byte(tmp_path):
    workdir = tmp_path / "conv"
    workdir.mkdir()
    (workdir / "payload.bin").write_bytes(b"\x00\x01\x02binary")

    fs = FakeFs()
    body = _body(_call(_server(fs, str(workdir)), "put_file",
                       {"path": "Q:\\X.BIN", "from_path": "payload.bin"}))
    assert fs.put_calls == [("Q:\\X.BIN", b"\x00\x01\x02binary")], body


def test_drives_names_the_kinds_and_says_when_a_drive_is_empty():
    from bridge.services.guest_fs import Drive
    fs = FakeFs(drives=[
        Drive(letter="A", type=2, label="", serial="0", free_kb=0,
              has_media=False),
        Drive(letter="C", type=3, label="WIN98", serial="1",
              free_kb=2097152, total_kb=4194304, has_media=True),
    ])
    body = _body(_call(_server(fs), "drives", {}))
    assert "removable" in body and "fixed" in body
    assert "empty" in body            # a floppy drive with no disk in it
    assert "2048 MB" in body
    assert "WIN98" in body


def test_a_mounted_cd_is_not_an_empty_drive():
    """Zero bytes free is not the same as no disk, and the first guest run
    got this wrong: D: had a label, a serial and a readable filesystem, and
    the table called it empty because a CD-ROM has nothing free by
    definition. The guest answers the question directly now."""
    from bridge.services.guest_fs import Drive
    fs = FakeFs(drives=[
        Drive(letter="D", type=5, label="GASTONTOOLS", serial="9",
              free_kb=0, total_kb=2482, has_media=True),
    ])
    body = _body(_call(_server(fs), "drives", {}))
    assert "empty" not in body, body
    assert "GASTONTOOLS" in body
    assert "0 MB" in body


def test_drives_says_not_checked_when_the_guest_never_probed():
    """has_media=None is a removable or a CD-ROM the guest did not ask about
    (BACKLOG 20) -- distinct from False, which means it asked and there is
    nothing there. Collapsing the two would report an unprobed floppy as
    known to be empty, which is the guess the guest change exists to avoid.
    """
    from bridge.services.guest_fs import Drive
    fs = FakeFs(drives=[
        Drive(letter="A", type=2, label="", serial="0", free_kb=0,
              has_media=None),
    ])
    body = _body(_call(_server(fs), "drives", {}))
    assert "not checked" in body, body
    assert "empty" not in body, body


def test_drives_forwards_probe_to_the_guest():
    fs = FakeFs(drives=[])
    _body(_call(_server(fs), "drives", {}))
    assert fs.probe_calls == [False], fs.probe_calls
    _body(_call(_server(fs), "drives", {"probe": True}))
    assert fs.probe_calls == [False, True], fs.probe_calls


def test_every_tool_refuses_clearly_with_no_machine_attached():
    srv = machine_tools.build(None, None, None, None)["machine"]["instance"]
    for name, args in (("get_file", {"path": "X"}),
                       ("put_file", {"path": "X", "content": "y"}),
                       ("drives", {})):
        body = _body(_call(srv, name, args))
        assert "No machine is connected" in body, name


# --- translation: the lane has no opinion, the TOOL does --------------------
#
# `bridge/services/guest_fs.py` is "byte for byte, with no baseline and no
# opinion about line endings", and that is right for the lane. It was wrong as
# the only behaviour the agent could get: the sync layer classifies text and
# converts, put_file did not, so the SAME file was runnable through one path
# and silently broken through the other.
#
# Measured on real Windows 98 the day this was found
# (docs/measurements/batch-eol-and-dir-b-p200-20261001.txt):
#
#   LF.BAT    "@echo off\necho one\nset FOO=bar\necho [%FOO%]\n"  -> "OFF"
#   CRLF.BAT  the same with \r\n                                  -> "one" "[bar]"
#
# Both exit 0. The session that hit it spent eleven tool calls on it, and
# `get_file` -- called specifically to diagnose it -- showed text that looked
# perfect, because LF and CRLF render identically.

LF_BAT = "@echo off\necho one\nset FOO=bar\n"


def test_text_is_converted_to_the_machines_endings_by_default():
    fs = FakeFs()
    _call(_server(fs), "put_file", {"path": r"C:\W\B.BAT", "content": LF_BAT})
    sent = fs.put_calls[0][1]
    assert b"\r\n" in sent
    assert b"\n" not in sent.replace(b"\r\n", b"")


def test_the_reply_says_the_bytes_are_not_identical_any_more():
    """Otherwise a later `get_file` showing CRLF is a mystery, and the agent
    has no way to know the two copies differ."""
    fs = FakeFs()
    out = _body(_call(_server(fs), "put_file",
                      {"path": r"C:\W\B.BAT", "content": LF_BAT}))
    assert "converted" in out.lower()


def test_translation_false_sends_the_bytes_as_they_are():
    fs = FakeFs()
    _call(_server(fs), "put_file",
          {"path": r"C:\W\B.BAT", "content": LF_BAT, "translation": False})
    assert fs.put_calls[0][1] == LF_BAT.encode("cp1252")


def test_a_file_with_nul_bytes_is_never_converted(tmp_path):
    """`P.classify` is the sync layer's own classifier and this is its first
    rule: a NUL ANYWHERE means byte-exact. Normalising a PDF shifts every
    xref offset, and a PDF can run kilobytes of ASCII before its first
    stream."""
    blob = b"%PDF-1.2\nstream\n\x00\x01\n\nmore\n"
    (tmp_path / "f.bin").write_bytes(blob)
    fs = FakeFs()
    _call(_server(fs, workdir=str(tmp_path)), "put_file",
          {"path": r"C:\W\F.BIN", "from_path": "f.bin"})
    assert fs.put_calls[0][1] == blob


def test_mixed_endings_are_left_alone(tmp_path):
    """Not fussiness: we cannot know which the user wanted, and converting
    would rewrite their file while the normalised CRC stayed identical, so
    nothing would report it. See `P.classify`."""
    blob = b"one\r\ntwo\nthree\r\n"
    (tmp_path / "m.txt").write_bytes(blob)
    fs = FakeFs()
    _call(_server(fs, workdir=str(tmp_path)), "put_file",
          {"path": r"C:\W\M.TXT", "from_path": "m.txt"})
    assert fs.put_calls[0][1] == blob


def test_an_unencodable_character_is_a_refusal_not_a_traceback():
    """Until 2026-10-02 `fold.encode_exact` raised straight out of the tool:
    the handler logged and re-raised, so one box-drawing character in a README
    reached the agent as an exception with no advice in it. Measured the same
    day: cp1252 holds the euro sign and does not hold U+2500."""
    fs = FakeFs()
    out = _body(_call(_server(fs), "put_file",
                      {"path": r"C:\W\R.TXT", "content": "a \u2500 b"}))
    assert fs.put_calls == []            # nothing was written
    assert "U+2500" in out               # which character
    assert "cp1252" in out               # and why it cannot hold it


def test_a_character_the_codepage_does_hold_goes_through():
    fs = FakeFs()
    _call(_server(fs), "put_file", {"path": r"C:\W\E.TXT",
                                    "content": "10 \u20ac"})
    assert fs.put_calls[0][1] == b"10 \x80"


def test_get_file_says_what_endings_the_file_has():
    """The line that was missing. An agent read a batch file back to find out
    why it would not run, got text that looked right, and learned nothing."""
    out = _body(_call(_server(FakeFs(data=b"one\ntwo\n")), "get_file",
                      {"path": r"C:\W\B.BAT"}))
    assert "LF" in out
    assert "NOT the CRLF this machine uses" in out
    # 2026-10-02: moved to the rule (foreign endings named, with the ones this
    # machine wants); the consequence sentence was cut from the line and lives
    # in the shell's note.


def test_native_endings_are_reported_as_fine():
    out = _body(_call(_server(FakeFs(data=b"one\r\ntwo\r\n")), "get_file",
                      {"path": r"C:\W\B.BAT"}))
    assert "CRLF" in out and "this machine's own" in out
    assert "NOT" not in out
    # 2026-10-02: native endings are still reported as fine; the phrase
    # changed with the trim, the absence of "NOT" is the rule.


def test_get_file_hands_back_text_with_local_endings():
    out = _body(_call(_server(FakeFs(data=b"one\r\ntwo\r\n")), "get_file",
                      {"path": r"C:\W\B.TXT"}))
    assert "one\ntwo" in out


def test_get_file_untranslated_goes_to_a_file_like_a_binary(tmp_path):
    """The agent cannot hold bytes, so byte-exact has to mean a local file --
    the same route a binary already took."""
    wd = tmp_path / "work"
    wd.mkdir()
    out = _body(_call(_server(FakeFs(data=b"one\r\ntwo\r\n"),
                              workdir=str(wd)), "get_file",
                      {"path": r"C:\W\B.TXT", "translation": False}))
    assert "one\ntwo" not in out
    assert "saved" in out.lower() or ".gaston" in out


def test_the_endings_question_is_asked_of_the_machine_not_of_an_os():
    """CLAUDE.md: the tool surface should survive being pointed at MS-DOS,
    Windows 3.1 or NT 4. A client that declares `eol=lf` must be told LF is
    its native ending, not lectured about COMMAND.COM."""
    from bridge.files import caps as _C

    class LfGuest:
        caps = _C.POSIX
    srv = machine_tools.build(None, None, None, LfGuest(),
                              fs=FakeFs(data=b"one\ntwo\n"),
                              workdir="")["machine"]["instance"]
    out = _body(_call(srv, "get_file", {"path": "/tmp/x"}))
    assert "NOT what this machine uses" not in out
    assert "COMMAND.COM" not in out


# --- list_dir ---------------------------------------------------------------

def _fs_listing(entries, truncated=False):
    from bridge.services.guest_fs import Listing

    class LsFs(FakeFs):
        async def ls(self, guest, path, *, max_entries=0):
            if self.raise_status:
                raise FsError(self.raise_status)
            self.ls_calls.append((path, max_entries))
            return Listing(entries=list(entries), truncated=truncated)

    fs = LsFs()
    fs.ls_calls = []
    return fs


def _entry(name, type="f", size=0, mtime=0, attrs=0):
    from bridge.services.guest_fs import Entry
    return Entry(name=name, type=type, size=size, mtime=mtime, attrs=attrs)


def test_folders_sort_first_then_names():
    """The guest emits whatever order the filesystem hands over, which on FAT
    is creation order -- useful to nobody reading a listing, and not even
    stable between two calls on the same directory."""
    fs = _fs_listing([_entry("zebra.txt"), _entry("Beta", "d"),
                      _entry("alpha.txt"), _entry("Alpha", "d")])
    out = _body(_call(_server(fs), "list_dir", {"path": r"C:\W"}))
    order = [ln.split()[0] for ln in out.splitlines() if ln.startswith("  ")]
    assert order == ["Alpha", "Beta", "alpha.txt", "zebra.txt"]


def test_a_type_the_guest_could_not_classify_is_not_called_a_file():
    """'u' is a real answer on a 1998 filesystem. Folding it into "not a
    directory" is how one gets opened as a file."""
    fs = _fs_listing([_entry("odd", "u"), _entry("real.txt", "f", 42)])
    out = _body(_call(_server(fs), "list_dir", {"path": r"C:\W"}))
    assert "?" in out
    assert "42" in out


def test_an_empty_directory_says_so():
    """Rather than a header with nothing under it, which reads as a failure."""
    out = _body(_call(_server(_fs_listing([])), "list_dir",
                      {"path": r"C:\W"}))
    assert "empty" in out.lower()


def test_a_capped_listing_says_it_is_not_the_whole_directory():
    """The failure this prevents: an agent reads a capped listing as complete
    and reports that the rest of the files are gone."""
    fs = _fs_listing([_entry("a.txt")], truncated=True)
    out = _body(_call(_server(fs), "list_dir", {"path": r"C:\W"}))
    assert "NOT the whole" in out


def test_max_entries_reaches_the_machine():
    """Enforced there, not by counting what arrives: a cap applied on this
    side still has the guest read and send everything."""
    fs = _fs_listing([_entry("a.txt")])
    _call(_server(fs), "list_dir", {"path": r"C:\W", "max_entries": 50})
    assert fs.ls_calls[0] == (r"C:\W", 50)


def test_a_refusal_comes_back_as_a_refusal():
    fs = _fs_listing([])
    fs.raise_status = "missing"
    res = _call(_server(fs), "list_dir", {"path": r"C:\W\F.TXT"})
    bad = res["isError"] if isinstance(res, dict) else res.is_error
    assert bad
    assert "There is nothing at" in _body(res)


def test_a_name_with_spaces_survives_the_wire_format():
    """Rows are `<type> <size> <mtime> <attrs> <name>` and most long names on
    a Windows machine contain a space, so the name is everything after the
    four fixed fields and nothing else."""
    from bridge.services.guest_fs import _parse_entries
    rows = _parse_entries(b"d 0 1 00000010 Program Files\n"
                          b"f 9 2 00000020 My Notes v2.txt\n")
    assert [r.name for r in rows] == ["Program Files", "My Notes v2.txt"]


def test_an_unparseable_row_is_dropped_not_guessed():
    """A row this cannot parse is a row whose name it does not know, and
    inventing one puts a wrong path in front of the agent."""
    from bridge.services.guest_fs import _parse_entries
    rows = _parse_entries(b"f 1 2 3 ok.txt\nnonsense\nf x y z bad.txt\n")
    assert [r.name for r in rows] == ["ok.txt"]


# --- what comes back inline, and what does not -----------------------------
#
# Until 2026-10-02 EVERY text file came back in full, with the only ceiling the
# guest's 4MB -- about a million tokens for one call, on a machine whose logs
# reach that size. The only thing in the way was the agent choosing to pass
# `max_bytes`, whose description reads like an optimisation.

def test_short_text_still_comes_back_inline():
    out = _body(_call(_server(FakeFs(data=b"host=10.0.0.45\r\n")), "get_file",
                      {"path": r"C:\G\gaston.ini"}))
    assert "host=10.0.0.45" in out


def test_long_text_is_downloaded_instead_of_returned(tmp_path):
    from bridge.agent.tools import GET_INLINE_MAX
    workdir = tmp_path / "conv"
    workdir.mkdir()
    big = b"a line of perfectly ordinary text\r\n" * 2000
    assert len(big) > GET_INLINE_MAX
    out = _body(_call(_server(FakeFs(data=big), str(workdir)), "get_file",
                      {"path": r"C:\BIG.LOG"}))
    assert "a line of perfectly ordinary text" not in out    # not in context
    assert "Downloaded to" in out
    assert "text" in out                                      # what it is
    assert "KB" in out or "MB" in out                         # how big
    saved = list((tmp_path / ".gaston" / "fetched").rglob("BIG.LOG"))
    assert len(saved) == 1 and saved[0].read_bytes() == big


def test_the_reply_says_what_kind_of_file_it_is(tmp_path):
    """Type and size alongside the path: the path is what the agent acts on,
    the other two are what tell it whether to Read the thing, Grep it, or
    leave it alone."""
    workdir = tmp_path / "conv"
    workdir.mkdir()
    out = _body(_call(_server(FakeFs(data=b"MZ\x90\x00" + b"\x00" * 40),
                              str(workdir)), "get_file",
                      {"path": r"C:\HELLO.EXE"}))
    assert "executable program" in out
    assert "bytes" in out


def test_an_unrecognised_binary_is_still_described(tmp_path):
    workdir = tmp_path / "conv"
    workdir.mkdir()
    out = _body(_call(_server(FakeFs(data=b"\x00\x01\x02\x03nothing known"),
                              str(workdir)), "get_file", {"path": r"C:\ODD.DAT"}))
    assert "binary data" in out or "octet" in out


class _GuestWithAFolder(FakeGuest):
    work_guest = "C:\\GTWORK\\INYOURWO"

    def on_guest(self, path):
        from bridge.files.paths import on_guest
        return on_guest(path, self.work_guest)


def test_a_relative_put_lands_in_the_conversation_s_folder():
    """On the Athlon, 2026-10-03, `proj\\a.txt` went to the guest as written
    and resolved against the client's current directory -- gremote's install
    folder -- where the agent then moved and deleted files."""
    fs = FakeFs()
    srv = machine_tools.build(None, None, None, _GuestWithAFolder(),
                              fs=fs)["machine"]["instance"]
    _call(srv, "put_file", {"path": "proj\\a.txt", "content": "x"})
    assert fs.put_calls and fs.put_calls[0][0] == "C:\\GTWORK\\INYOURWO\\proj\\a.txt"


def test_a_relative_path_with_no_folder_is_refused_not_guessed():
    class NoFolder(_GuestWithAFolder):
        work_guest = ""
    fs = FakeFs()
    srv = machine_tools.build(None, None, None, NoFolder(),
                              fs=fs)["machine"]["instance"]
    res = _call(srv, "put_file", {"path": "proj\\a.txt", "content": "x"})
    assert not fs.put_calls
    assert "relative" in _body(res)


class _NoParentOnce(FakeFs):
    """The guest's answer to a put into a folder that is not there yet."""

    def __init__(self):
        super().__init__()
        self.made = []

    async def put(self, guest, path, data):
        if not self.made:
            raise FsError("no_parent")
        return await super().put(guest, path, data)

    async def op(self, guest, op, path, to="", **kw):
        self.made.append((op, path))
        return {"status": "ok"}


def test_put_file_makes_a_missing_folder_and_writes():
    """2026-10-03: a write into a new folder was refused, and the agent spent
    a turn and a permission prompt per level on mkdir before writing the
    same file again."""
    fs = _NoParentOnce()
    srv = machine_tools.build(None, None, None, _GuestWithAFolder(),
                              fs=fs)["machine"]["instance"]
    res = _call(srv, "put_file", {"path": "C:\\W\\proj\\src\\main.c",
                                  "content": "x"})
    assert fs.made == [("FSMKDIR", "C:\\W\\proj\\src")]
    assert fs.put_calls and fs.put_calls[0][0] == "C:\\W\\proj\\src\\main.c"
    assert "was made first" in _body(res)


def test_put_file_on_an_old_client_still_says_the_folder_is_missing():
    class Old(_GuestWithAFolder):
        from bridge.files import caps as _C
        caps = _C.Caps(ops=0)
    fs = _NoParentOnce()
    srv = machine_tools.build(None, None, None, Old(),
                              fs=fs)["machine"]["instance"]
    res = _call(srv, "put_file", {"path": "C:\\W\\new\\a.txt", "content": "x"})
    assert not fs.made and not fs.put_calls
    assert "does not exist" in _body(res)
