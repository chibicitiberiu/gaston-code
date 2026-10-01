"""The agent's view of get_file, put_file and drives.

What these pin is the surface, not the transport: the tools exist, they name
no operating system, they refuse clearly when there is no machine, and the two
decisions that could quietly hurt somebody -- where a fetched binary lands,
and what `from_path` can reach -- behave as intended.
"""
from __future__ import annotations

import asyncio

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
    caps = None


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


def test_ls_is_not_offered():
    """CLAUDE.md's bar: a tool earns its place only if `exec` does the job
    badly. The machine's own shell lists a directory perfectly well, and a
    tool that duplicates it costs context on every single request."""
    assert "ls" not in _descriptions()
    assert "stat" not in _descriptions()


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

    saved = tmp_path / ".gaston" / "fetched" / "PROG.EXE"
    assert saved.exists(), body
    assert saved.read_bytes() == b"MZ\x00\x00binary"
    assert str(saved) in body
    # The load-bearing half: nothing landed under the mirrored folder.
    assert not any(p.name == "PROG.EXE"
                   for p in workdir.rglob("*")), "a fetched file is in the mirror"


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
    assert "NOT what this machine uses" in out


def test_native_endings_are_reported_as_fine():
    out = _body(_call(_server(FakeFs(data=b"one\r\ntwo\r\n")), "get_file",
                      {"path": r"C:\W\B.BAT"}))
    assert "what this machine uses" in out
    assert "NOT what this machine uses" not in out


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
