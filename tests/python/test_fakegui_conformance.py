"""bridge/devtools/fakegui.py had zero test coverage (03.54) -- nothing
under tests/ imported it before test_server_integration.py's single
deflate-decoding test. fakegui's whole value is described in its own module
docstring: "if fakegui works and the guest does not, the bug is in the C,
not the bridge." That claim only holds for the paths fakegui actually
exercises, and several were silent gaps until now:

  * 03.28 -- fakegui's HELLO carried only ver/secret/max_frame, so every
    fakegui-driven test ran the bridge's UNDECLARED defaults
    (Caps(declared=False), manifest=1) and never the DECLARED path the real
    guest takes on every connection (win/net/net.cpp's rc_net_start sends 22
    more headers).
  * 03.29 -- fakegui read the `waiting` header on PERMREQ/PERMSTATE, which
    the guest's PERMREQ/PERMSTATE handling never reads (only PERMWAIT does
    -- win/app/bar.cpp's rc_bar_frame, PERMREQ/PERMSTATE and PERMWAIT are
    two of the six frame types it owns), so fakegui exercised a header
    interpretation the shipping guest does not share.
  * 03.31 -- fakegui had no way to answer a HALT except a human typing
    /continue at a live terminal prompt, so the one path the project's own
    standing rule is about ("the person at the keyboard never loses a byte,
    and is never asked twice") was untestable through the fake client.
  * 03.32 -- fakegui never answered an EXEC frame or any SYNC* frame through
    an automated test, so the tool-call path and the sync-transfer path's
    honest-refusal answers were only ever exercised by hand, if at all.

These tests drive the real `FakeGui` class -- not a re-implementation of it
-- against a real `Bridge`, the same pattern test_server_integration.py's
`test_fakegui_can_decode_a_real_deflated_text_frame` already established.
"""
from __future__ import annotations

import asyncio

import pytest

from bridge.devtools.fakegui import FakeGui
from bridge.wire.protocol import Frame, FrameReader
from bridge.files import caps as C

# The `harness` fixture (a real Bridge on a real port) and `_recv` are
# defined in test_server_integration.py, not duplicated here -- importing
# them keeps this file honest against the one Harness the rest of the suite
# already trusts, rather than a second implementation that could drift from
# it.
from test_server_integration import harness, _recv   # noqa: F401


async def _wait_attached(harness, timeout=5.0):
    """Poll until the bridge has processed HELLO and attached a guest.

    connect() only writes bytes; _read_hello runs on the bridge's own task,
    scheduled after the TCP accept. Nothing here reads fakegui's own socket,
    so this is safe to use whether or not a caller is about to start pump().
    """
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        guest = harness.bridge._guest
        if guest is not None and guest.connected:
            return guest
        await asyncio.sleep(0.02)
    pytest.fail("bridge never attached the fakegui client")


# --- 03.28: the handshake must exercise the DECLARED capability path -------


async def test_fakegui_hello_declares_capabilities_like_the_real_guest(
        harness):
    """Would fail before the fix: the old 3-header HELLO left
    guest.caps.declared False and guest.caps.manifest == 1, the exact
    "undeclared Win9x default" path -- never the declared path the real
    guest's rc_net_start takes on every connection."""
    gui = FakeGui("127.0.0.1", harness.port, "s")
    await gui.connect()
    try:
        guest = await _wait_attached(harness)
        assert guest.caps.declared is True, (
            "fakegui's HELLO must declare capabilities, like the real guest "
            "does on every connection")
        assert guest.caps.manifest == 2
        assert guest.caps.case == C.CASE_FOLD
        assert guest.caps.devices == C.DEVICES_DOS
        assert guest.caps.eol == C.EOL_CRLF
        assert guest.caps.maxpath == 260
        # And the identity fields a real HELLO also carries, read by
        # bridge/view/pages.py's limits_for and bridge/machine.py's prompt.
        assert guest.hello.get("mem_kb") == "32768"
        assert guest.hello.get("os", "").startswith("Windows")
    finally:
        await gui.close()


# --- 03.29: PERMREQ/PERMSTATE must not read `waiting` ----------------------


def test_permreq_does_not_surface_the_queued_count(capsys):
    """The guest's PERMREQ/PERMSTATE handling (win/app/bar.cpp's rc_bar_frame,
    which pushes a bar entry with only id/body/allows) reads `waiting`
    nowhere but the PERMWAIT case. Would fail before the fix: fakegui used
    to print "(+3 queued)" straight off the first PERMREQ, a header
    interpretation the shipping guest does not share."""
    gui = FakeGui("h", 0, "s")
    gui.render(Frame("PERMREQ", {"id": "t1", "tool": "Bash", "allows": "0",
                                 "waiting": "3"}, b"do a thing"))
    out = capsys.readouterr().out
    assert "queued" not in out
    assert "do a thing" in out


def test_permstate_also_does_not_surface_the_queued_count(capsys):
    gui = FakeGui("h", 0, "s")
    gui.render(Frame("PERMSTATE", {"id": "t1", "tool": "Bash", "allows": "1",
                                   "waiting": "5"}, b"re-issued"))
    out = capsys.readouterr().out
    assert "queued" not in out


def test_permwait_still_surfaces_the_queued_count(capsys):
    """The other half: PERMWAIT is the one frame that actually carries a
    queue-depth update on the real guest, and fakegui must still show it."""
    gui = FakeGui("h", 0, "s")
    gui.render(Frame("PERMWAIT", {"id": "t1", "waiting": "2"}))
    out = capsys.readouterr().out
    assert "2" in out and "queued" in out


# --- 03.31: fakegui must be able to answer a HALT ---------------------------


async def test_answer_halt_is_false_with_nothing_pending():
    """The same guard input_loop applied inline before there was a method
    here to call."""
    gui = FakeGui("h", 0, "s")
    assert await gui.answer_halt() is False


async def test_fakegui_answers_a_halt_end_to_end(harness):
    """Drives a real HALT from the real bridge (_raise_halt -- the same call
    a sync Pass makes) all the way to a HALTEND, through the real FakeGui
    object and answer_halt(), the method /continue now calls. Would hang (or
    time out) before 03.31's fix: there was nothing to call that sent RESUME
    outside of a human typing at a live prompt."""
    gui = FakeGui("127.0.0.1", harness.port, "s")
    await gui.connect()
    pump = asyncio.create_task(gui.pump())
    try:
        guest = await _wait_attached(harness)
        assert harness.bridge.synccoord.halt_id == "", (
            "test setup: no halt should be up yet")

        await harness.bridge._raise_halt(guest, "conflict on notes.txt")

        for _ in range(200):
            if gui.halt_id:
                break
            await asyncio.sleep(0.02)
        else:
            pytest.fail("fakegui never rendered the HALT frame")
        assert gui.halt_id == harness.bridge.synccoord.halt_id

        assert await gui.answer_halt() is True

        for _ in range(200):
            if not harness.bridge.synccoord.halt_id and gui.halt_id is None:
                break
            await asyncio.sleep(0.02)
        else:
            pytest.fail("halt was never cleared on both sides")
    finally:
        pump.cancel()
        await gui.close()


# --- 03.32: fakegui must exercise EXEC and every SYNC* reply ---------------


async def test_fakegui_runs_exec_end_to_end(harness):
    """Drives a real EXEC through bridge.run_on_guest and lets the real
    FakeGui's own RPC handling answer it by actually running a subprocess
    and streaming EXECOUT/EXECRESULT back -- the whole tool-call path,
    through the fake client, for the first time."""
    gui = FakeGui("127.0.0.1", harness.port, "s")
    await gui.connect()
    pump = asyncio.create_task(gui.pump())
    try:
        await _wait_attached(harness)
        result = await asyncio.wait_for(
            harness.bridge.run_on_guest("echo hello-from-fakegui",
                                        timeout_ms=5000, shell=True, cwd=""),
            10)
        text = result["content"][0]["text"]
        assert "hello-from-fakegui" in text
        assert result.get("isError") is not True
    finally:
        pump.cancel()
        await gui.close()


async def test_fakegui_answers_busy_for_a_second_concurrent_exec():
    """win/rpc/rpc.c: one job at a time -- a second EXEC while one runs is
    answered busy, never queued. Exercised directly against _handle_rpc,
    because Bridge.run_on_guest already serialises calls with its own lock
    (measured: two overlapping run_on_guest() calls never put two EXEC
    frames on the wire at once), so this branch in fakegui's own code was
    unreachable through the one caller tried first and had no test at all."""
    gui = FakeGui("h", 0, "s")
    gui.writer = _CapturingWriter()
    gui._exec_task = asyncio.create_task(asyncio.sleep(10))
    gui._exec_id = "already-running"
    try:
        await gui._handle_rpc(Frame("EXEC", {"id": "second"}, b"echo hi"))
        frames = _sent_frames(gui.writer)
        assert len(frames) == 1
        assert frames[0].type == "EXECRESULT"
        assert frames[0].headers.get("id") == "second"
        assert frames[0].headers.get("status") == "busy"
    finally:
        gui._exec_task.cancel()
        try:
            await gui._exec_task
        except asyncio.CancelledError:
            pass


class _CapturingWriter:
    """Enough of a StreamWriter for FakeGui.send(): write() and drain()."""

    def __init__(self):
        self.chunks: list[bytes] = []

    def write(self, data: bytes) -> None:
        self.chunks.append(data)

    async def drain(self) -> None:
        pass


def _sent_frames(writer: _CapturingWriter) -> list[Frame]:
    fr = FrameReader()
    frames: list[Frame] = []
    for chunk in writer.chunks:
        frames.extend(fr.feed(chunk))
    return frames


@pytest.mark.parametrize("frame_type,expect_type,expect_status", [
    ("SYNCSCAN", "SYNCEND", "no_root"),
    ("SYNCPUT", "SYNCPUTACK", "readonly"),
    ("SYNCPUTEND", "SYNCPUTACK", "readonly"),
    ("SYNCMKDIR", "SYNCMKDIRACK", "bad_path"),
    ("SYNCGET", "SYNCDATEND", "missing"),
])
async def test_fakegui_answers_every_sync_frame_honestly(
        frame_type, expect_type, expect_status):
    """fakegui cannot mirror the manifest-and-CRC machinery in
    win/sync/syncrpc.c (see its own module docstring), but it must answer
    every SYNC* frame the way a guest with no configured sync root
    genuinely would -- which bridge/files/syncrun.py's scan_guest treats as
    the honest, terminal answer for the whole pass. Would fail before
    03.32's coverage: this path existed in the source but no test ever
    called it, so a regression here would have shipped silently."""
    gui = FakeGui("h", 0, "s")
    gui.writer = _CapturingWriter()
    await gui._handle_rpc(Frame(frame_type, {"id": "s1"}))
    frames = _sent_frames(gui.writer)
    assert len(frames) == 1, frames
    assert frames[0].type == expect_type
    assert frames[0].headers.get("id") == "s1"
    assert frames[0].headers.get("status") == expect_status
