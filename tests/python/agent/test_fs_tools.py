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
