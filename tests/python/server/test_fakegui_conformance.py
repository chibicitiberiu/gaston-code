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
import contextlib
import os

import pytest

# The `harness` fixture (a real Bridge on a real port) and `_recv` are
# defined in test_server_integration.py, not duplicated here -- importing
# them keeps this file honest against the one Harness the rest of the suite
# already trusts, rather than a second implementation that could drift from
# it.
from harness import _recv, harness  # noqa: F401

from bridge.devtools.fakegui import FakeGui
from bridge.files import caps as C
from bridge.wire.protocol import Frame, FrameReader


async def _wait_attached(harness, timeout=5.0):
    """Poll until the bridge has processed HELLO and attached a guest.

    connect() only writes bytes; _read_hello runs on the bridge's own task,
    scheduled after the TCP accept. Nothing here reads fakegui's own socket,
    so this is safe to use whether or not a caller is about to start pump().
    """
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        guest = harness.bridge.connection.guest
        if guest is not None and guest.link.connected:
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
        assert guest.caps.maxdepth == 8
        assert guest.caps.deflate == 1
    finally:
        await gui.close()


def test_fakegui_hello_headers_match_guest_net_cpp():
    """Audit 04.8: FakeGui must not snapshot HELLO headers by hand.

    Parse the HDR() macro calls in win/net/net.cpp and assert that
    fakegui's HELLO frame declares the same keys.
    """
    import re

    import conftest

    from bridge.devtools.fakegui import FAKE_CAPS, FAKE_IDENTITY
    from bridge.wire.protocol import hello

    net_cpp = os.path.join(conftest.REPO, "win", "net", "net.cpp")
    src = open(net_cpp, encoding="utf-8").read()
    hdr_keys = set(re.findall(r'HDR\("([a-z0-9_]+)"', src))
    assert hdr_keys, "no HDR() calls found in net.cpp"

    fake_headers = set(hello("test-secret").headers.keys()) | set(FAKE_CAPS.keys()) | set(FAKE_IDENTITY.keys())

    # Optional headers that depend on runtime configuration/state:
    # sync_root (only if configured), sess (only on resume), conv (only once
    # a conversation has been named).
    # `prefs` only with a PREFS body to follow it (net.cpp): the fake has
    # no settings to send, and declaring it would make the bridge wait.
    optional_runtime = {"sync_root", "sess", "conv", "prefs"}
    required_keys = hdr_keys - optional_runtime

    missing = required_keys - fake_headers
    assert not missing, (
        f"FakeGui's HELLO headers are missing keys declared by win/net/net.cpp: "
        f"{sorted(missing)}. Update FAKE_CAPS or FAKE_IDENTITY."
    )


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
        assert harness.bridge.connection.synccoord.halt_id == "", (
            "test setup: no halt should be up yet")

        await harness.bridge.sync.raise_halt(guest, "conflict on notes.txt")

        for _ in range(200):
            if gui.halt_id:
                break
            await asyncio.sleep(0.02)
        else:
            pytest.fail("fakegui never rendered the HALT frame")
        assert gui.halt_id == harness.bridge.connection.synccoord.halt_id

        assert await gui.answer_halt() is True

        for _ in range(200):
            if not harness.bridge.connection.synccoord.halt_id and gui.halt_id is None:
                break
            await asyncio.sleep(0.02)
        else:
            pytest.fail("halt was never cleared on both sides")
    finally:
        pump.cancel()
        await gui.close()


# --- 03.32: fakegui must exercise EXEC and every SYNC* reply ---------------


async def test_fakegui_runs_exec_end_to_end(harness):
    """Drives a real EXEC through bridge.guest_exec.run_on_guest and lets the real
    FakeGui's own RPC handling answer it by actually running a subprocess
    and streaming EXECOUT/EXECRESULT back -- the whole tool-call path,
    through the fake client, for the first time."""
    gui = FakeGui("127.0.0.1", harness.port, "s")
    await gui.connect()
    pump = asyncio.create_task(gui.pump())
    try:
        await _wait_attached(harness)
        result = await asyncio.wait_for(
            harness.bridge.guest_exec.run_on_guest("echo hello-from-fakegui",
                                        timeout_ms=5000, shell="", cwd="",
        guest=harness.bridge.connection.guest),
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


# --- 02.16: the keepalive and the export sink ------------------------------


async def test_fakegui_sweeps_a_keepalive_like_the_guest_does(harness,
                                                              monkeypatch):
    """fakegui sent PING only from the typed `/ping`.

    The real guest sweeps every RPC_SWEEP_MS (30s) and sends SYNCDIRTY when
    it has a sync root or PING when it does not -- win/rpc/rpc.cpp's
    `rpc_maybe_nudge`, added because the bridge reaps a guest silent for 90
    seconds while the documented deadline for answering a permission prompt
    is 600. So a break-glass fakegui session left idle while somebody reads a
    prompt was hung up on, and the conformance harness never exercised the
    keepalive at all.

    The sweep interval is monkeypatched so the test takes milliseconds; what
    is under test is that connect() starts it and that it puts a real PING on
    the wire, not the constant.
    """
    monkeypatch.setattr(FakeGui, "SWEEP_S", 0.05)
    gui = FakeGui("127.0.0.1", harness.port, "s")
    await gui.connect()
    pump = asyncio.create_task(gui.pump())
    try:
        guest = await _wait_attached(harness)
        assert gui._sweep is not None and not gui._sweep.done()
        # last_seen only moves when a frame arrives, which is the whole point.
        guest.last_seen = 0.0
        for _ in range(200):
            if guest.last_seen > 0.0:
                break
            await asyncio.sleep(0.02)
        else:
            pytest.fail("no keepalive reached the bridge")
    finally:
        pump.cancel()
        await gui.close()
    assert gui._sweep is None


async def test_fakegui_writes_a_real_export_to_a_file(harness, monkeypatch):
    """`render` had no case for EXPORTSTART/EXPORTDAT/EXPORTEND at all.

    They fell into the catch-all and printed `[EXPORTDAT {...}] <200 chars of
    deflated body>` per 63 KB chunk -- so the one transfer path with a real
    defect in it (02.3: the idle reaper reading a long export as a dead
    guest) could not be driven from the conformance tool, and there was no
    export test here to hang off.
    """
    gui = FakeGui("127.0.0.1", harness.port, "s")
    gui.raw = True          # the transcript is fixture, not the subject
    await gui.connect()
    pump = asyncio.create_task(gui.pump())
    try:
        guest = await _wait_attached(harness)
        # Something long enough to be chunked, so the multi-frame path and
        # the `off` continuity check are both exercised.
        from bridge.view import blocks as B

        for i in range(400):
            await harness.bridge.presentation.write_block(
                guest, B.para("paragraph %04d of an exported transcript" % i))

        await harness.bridge.session.on_export(guest, Frame("EXPORT", {}))

        for _ in range(400):
            if gui.export_path:
                break
            await asyncio.sleep(0.02)
        else:
            pytest.fail("the export never finished")

        assert gui._exp is None, "the transfer state was not cleared"
        with open(gui.export_path, "rb") as fh:
            got = fh.read()
        assert b"paragraph 0000" in got and b"paragraph 0399" in got
    finally:
        pump.cancel()
        await gui.close()
        if gui.export_path:
            with contextlib.suppress(OSError):
                os.unlink(gui.export_path)


async def test_fakegui_throws_away_an_export_that_did_not_finish(harness):
    """A shortened export is not a smaller export, it is a broken file --
    which is what `send_export_records` refuses to produce and what the guest
    refuses to keep. The fake client has to refuse it too, or the tool would
    report success for the one failure the END frame exists to signal."""
    gui = FakeGui("127.0.0.1", harness.port, "s")
    gui._export(Frame("EXPORTSTART", {"total": "100"}))
    path = gui._exp["path"]
    gui._export(Frame("EXPORTDAT", {"off": "0", "total": "100", "enc": "raw"},
                      b"x" * 40))
    gui._export(Frame("EXPORTEND", {"status": "error", "bytes": "40",
                                    "reason": "the connection dropped"}))
    assert gui._exp is None
    assert not os.path.exists(path)


async def test_fakegui_refuses_an_export_chunk_that_skips_bytes(harness):
    """`off` is in decompressed bytes and the guest checks it, because a
    chunk that does not continue where the last one stopped writes a file
    with a hole in it -- and a hole is indistinguishable from a complete file
    afterwards."""
    gui = FakeGui("127.0.0.1", harness.port, "s")
    gui._export(Frame("EXPORTSTART", {"total": "100"}))
    path = gui._exp["path"]
    gui._export(Frame("EXPORTDAT", {"off": "0", "total": "100", "enc": "raw"},
                      b"x" * 40))
    gui._export(Frame("EXPORTDAT", {"off": "60", "total": "100",
                                    "enc": "raw"}, b"y" * 40))
    assert gui._exp is None
    assert not os.path.exists(path)
