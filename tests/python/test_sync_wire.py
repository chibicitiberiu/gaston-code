"""Sync over the real wire protocol, with a fake guest.

This exercises the frame handling, the manifest parse and the transfer state
machine. It does NOT prove anything about Windows 98 -- CPython cannot model
case-insensitivity, 8.3 aliasing, reserved device names or FAT granularity, and
a green run here is evidence about framing only. The guest's own behaviour is
covered by test_guest_agreement.py, which runs the shipping C.
"""

import asyncio
import os
import zlib

import pytest

from bridge import paths as P
from bridge.protocol import Frame
from bridge.sync import Baseline, Entry, Type
from bridge.syncrun import Pass, SyncError

import sys as _sys, os as _os
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
from test_server_integration import _recv, harness  # noqa: E402,F401


def manifest_line(rel: str, data: bytes, typ=b"f") -> bytes:
    """Exactly the format win/syncrpc.c emits."""
    cls = P.classify(data)
    crc = zlib.crc32(P.normalise(data, cls))
    raw = rel.encode("cp1252")
    return (b"%s %s %d %d %d %d %s\n" % (typ, cls.encode(), len(data), 0, crc,
                                         len(raw), raw))


async def fake_scan(harness, reader, writer, files: dict, *, tree_id="VOL-1",
                    status="ok", claim=None):
    """Answer one SYNCSCAN the way the guest would."""
    req = await _recv(harness, reader, "SYNCSCAN")
    sid = req.headers["id"]
    body = b"".join(manifest_line(r, d) for r, d in files.items())
    if body:
        await harness.send(writer, Frame("SYNCENT", {"id": sid,
                                                     "n": str(len(files))}, body))
    await harness.send(writer, Frame("SYNCEND", {
        "id": sid, "status": status,
        "files": str(len(files) if claim is None else claim),
        "tree_id": tree_id}))
    return sid


def a_pass(harness, tmp_path, root=None):
    root = root or str(tmp_path / "tree")
    os.makedirs(root, exist_ok=True)
    state = str(tmp_path / "state")
    os.makedirs(state, exist_ok=True)
    return Pass(harness.bridge, harness.bridge._guest, root, state)


# --- the manifest -----------------------------------------------------------

async def test_a_manifest_round_trips_including_a_binary(harness, tmp_path):
    reader, writer = await harness.connect()
    await _recv(harness, reader, "OPTIONSEND")
    p = a_pass(harness, tmp_path)

    files = {"a.txt": b"hello\r\nworld\r\n", "img.dat": b"\x00\x01\x02"}
    task = asyncio.create_task(p.scan_guest())
    await fake_scan(harness, reader, writer, files)
    rows = await asyncio.wait_for(task, 5)

    assert set(rows) == {"a.txt", "img.dat"}
    assert rows["a.txt"].cls == P.CLASS_TEXT
    assert rows["img.dat"].cls == P.CLASS_EXACT
    # The CRLF file's CRC is over the NORMALISED bytes, so its LF twin matches.
    assert rows["a.txt"].crc == zlib.crc32(b"hello\nworld\n")
    await harness.close(writer)


async def test_a_short_manifest_aborts_rather_than_reading_as_deletions(
        harness, tmp_path):
    """The failure the too_many fix exists for, arriving through another door:
    if fewer entries arrive than SYNCEND claims, the missing paths look deleted
    and the bridge pushes its whole tree at the guest."""
    reader, writer = await harness.connect()
    await _recv(harness, reader, "OPTIONSEND")
    p = a_pass(harness, tmp_path)

    task = asyncio.create_task(p.scan_guest())
    await fake_scan(harness, reader, writer, {"a.txt": b"x"}, claim=99)
    with pytest.raises(SyncError, match="arrived short"):
        await asyncio.wait_for(task, 5)
    await harness.close(writer)


async def test_too_many_aborts_the_whole_pass(harness, tmp_path):
    reader, writer = await harness.connect()
    await _recv(harness, reader, "OPTIONSEND")
    p = a_pass(harness, tmp_path)
    task = asyncio.create_task(p.scan_guest())
    await fake_scan(harness, reader, writer, {}, status="too_many")
    with pytest.raises(SyncError, match="too_many"):
        await asyncio.wait_for(task, 5)
    await harness.close(writer)


async def test_a_different_tree_is_refused_not_first_synced(harness, tmp_path):
    """Trace it as a first sync and a reconnect from the original guest
    overwrites the conflict copy holding the agent's work."""
    reader, writer = await harness.connect()
    await _recv(harness, reader, "OPTIONSEND")
    p = a_pass(harness, tmp_path)
    p.base.tree_id = "VOL-ORIGINAL"

    task = asyncio.create_task(p.scan_guest())
    await fake_scan(harness, reader, writer, {}, tree_id="VOL-OTHER")
    with pytest.raises(SyncError, match="Refusing"):
        await asyncio.wait_for(task, 5)
    await harness.close(writer)


# --- pushing ----------------------------------------------------------------

async def test_a_push_converts_endings_and_carries_its_precondition(
        harness, tmp_path):
    reader, writer = await harness.connect()
    await _recv(harness, reader, "OPTIONSEND")
    p = a_pass(harness, tmp_path)
    open(os.path.join(p.root, "a.txt"), "wb").write(b"one\ntwo\n")

    task = asyncio.create_task(p.push("a.txt", "a.txt", None))
    put = await _recv(harness, reader, "SYNCPUT")
    end = await _recv(harness, reader, "SYNCPUTEND")

    # The path is length-prefixed in the BODY -- a truncated header would write
    # the right content to the wrong file with the CRC still matching.
    assert put.body.startswith(b"5 a.txt")
    assert put.body[7:] == b"one\r\ntwo\r\n"          # converted for the guest
    assert end.headers["if_crc"] == "absent"
    assert end.headers["cls"] == P.CLASS_TEXT

    await harness.send(writer, Frame("SYNCPUTACK", {
        "id": put.headers["id"], "status": "ok",
        "crc": end.headers["crc"]}))
    assert await asyncio.wait_for(task, 5) is True
    await harness.close(writer)


async def test_a_binary_push_is_not_converted(harness, tmp_path):
    reader, writer = await harness.connect()
    await _recv(harness, reader, "OPTIONSEND")
    p = a_pass(harness, tmp_path)
    blob = b"\x00\r\n\x01\n\x02"
    open(os.path.join(p.root, "b.dat"), "wb").write(blob)

    task = asyncio.create_task(p.push("b.dat", "b.dat", None))
    put = await _recv(harness, reader, "SYNCPUT")
    end = await _recv(harness, reader, "SYNCPUTEND")
    assert put.body[len(b"5 b.dat"):] == blob, "a binary was line-ending mangled"
    assert end.headers["cls"] == P.CLASS_EXACT
    await harness.send(writer, Frame("SYNCPUTACK", {
        "id": put.headers["id"], "status": "ok", "crc": end.headers["crc"]}))
    assert await asyncio.wait_for(task, 5) is True
    await harness.close(writer)


async def test_a_changed_refusal_does_not_advance_the_baseline(harness, tmp_path):
    """The compare-and-swap. A save landing after the scanner walked past the
    file makes the guest refuse, and refusing must not look like success."""
    reader, writer = await harness.connect()
    await _recv(harness, reader, "OPTIONSEND")
    p = a_pass(harness, tmp_path)
    open(os.path.join(p.root, "a.txt"), "wb").write(b"x\n")

    task = asyncio.create_task(
        p.push("a.txt", "a.txt", Entry(type=Type.FILE, crc=1234)))
    put = await _recv(harness, reader, "SYNCPUT")
    end = await _recv(harness, reader, "SYNCPUTEND")
    assert end.headers["if_crc"] == "1234"

    await harness.send(writer, Frame("SYNCPUTACK", {
        "id": put.headers["id"], "status": "changed", "crc": "0"}))
    assert await asyncio.wait_for(task, 5) is False
    assert any("changed" in r for r in p.reports)
    await harness.close(writer)


async def test_an_ack_with_the_wrong_crc_is_reported_not_retried(
        harness, tmp_path):
    """Invariant 2. Retrying a transfer that cannot establish
    crc(guest) == crc(bridge) is how a livelock starts."""
    reader, writer = await harness.connect()
    await _recv(harness, reader, "OPTIONSEND")
    p = a_pass(harness, tmp_path)
    open(os.path.join(p.root, "a.txt"), "wb").write(b"x\n")

    task = asyncio.create_task(p.push("a.txt", "a.txt", None))
    put = await _recv(harness, reader, "SYNCPUT")
    await _recv(harness, reader, "SYNCPUTEND")
    await harness.send(writer, Frame("SYNCPUTACK", {
        "id": put.headers["id"], "status": "ok", "crc": "999999"}))
    assert await asyncio.wait_for(task, 5) is False
    assert any("acked crc" in r for r in p.reports)
    await harness.close(writer)


async def test_an_oversized_file_is_skipped_with_a_reason(harness, tmp_path):
    """One file must never hold the wire: the guest has a single sender thread,
    so a huge transfer starves permission prompts behind it."""
    reader, writer = await harness.connect()
    await _recv(harness, reader, "OPTIONSEND")
    p = a_pass(harness, tmp_path)
    open(os.path.join(p.root, "big.bin"), "wb").write(b"\x00" * (2 * 1024 * 1024))
    assert await p.push("big.bin", "big.bin", None) is False
    assert any("exceeds" in r for r in p.reports)
    await harness.close(writer)


async def test_sync_frames_never_enter_the_replay_buffer(harness, tmp_path):
    """Replay redraws a transcript. Re-delivering a SYNCPUT REWRITES A FILE."""
    reader, writer = await harness.connect()
    await _recv(harness, reader, "OPTIONSEND")
    p = a_pass(harness, tmp_path)
    before = harness.bridge._guest.replay.next_seq
    open(os.path.join(p.root, "a.txt"), "wb").write(b"x\n")

    task = asyncio.create_task(p.push("a.txt", "a.txt", None))
    put = await _recv(harness, reader, "SYNCPUT")
    await _recv(harness, reader, "SYNCPUTEND")
    await harness.send(writer, Frame("SYNCPUTACK", {
        "id": put.headers["id"], "status": "ok", "crc": "0"}))
    await asyncio.wait_for(task, 5)

    types = [fr.type for fr in harness.bridge._guest.replay.since(before)]
    assert not [t for t in types if t.startswith("SYNC")]
    await harness.close(writer)


async def test_a_disconnect_mid_transfer_fails_it_rather_than_hanging(
        harness, tmp_path):
    reader, writer = await harness.connect()
    await _recv(harness, reader, "OPTIONSEND")
    p = a_pass(harness, tmp_path)
    open(os.path.join(p.root, "a.txt"), "wb").write(b"x\n")

    task = asyncio.create_task(p.push("a.txt", "a.txt", None))
    await _recv(harness, reader, "SYNCPUT")
    await harness.close(writer)
    with pytest.raises((SyncError, asyncio.TimeoutError)):
        await asyncio.wait_for(task, 5)


# --- the bridge's own scan --------------------------------------------------

def test_the_bridge_scan_skips_unsyncable_names_with_reasons(tmp_path):
    root = tmp_path / "tree"
    (root / "src").mkdir(parents=True)
    (root / "src" / "ok.c").write_bytes(b"int main(void){return 0;}\n")
    (root / "aux.c").write_bytes(b"device name\n")
    (root / "what?.txt").write_bytes(b"illegal\n")

    class FakePass(Pass):
        def __init__(self, root):
            self.root = str(root)
            self.reports = []

    p = FakePass(root)
    rows = Pass.scan_bridge(p)
    assert "src/ok.c" in rows
    assert "aux.c" not in rows and "what?.txt" not in rows
    assert any("aux.c" in r and "reserved" in r for r in p.reports)
    assert any("what?.txt" in r and "illegal" in r for r in p.reports)


def test_the_bridge_scan_refuses_a_case_collision_keeping_the_first(tmp_path):
    root = tmp_path / "tree"
    root.mkdir()
    (root / "README.md").write_bytes(b"one\n")
    (root / "readme.md").write_bytes(b"two\n")

    class FakePass(Pass):
        def __init__(self, root):
            self.root = str(root)
            self.reports = []

    p = FakePass(root)
    rows = Pass.scan_bridge(p)
    assert len([k for k in rows if k == "readme.md"]) == 1
    assert any("collides" in r for r in p.reports)


def test_the_bridge_scan_does_not_follow_symlinks_out_of_the_tree(tmp_path):
    """A symlink pointing outside would have its TARGET'S CONTENTS pushed,
    bypassing off_limits -- the confidentiality boundary sync never consults."""
    root = tmp_path / "tree"
    root.mkdir()
    secret = tmp_path / "secret.txt"
    secret.write_bytes(b"ssh key\n")
    os.symlink(str(secret), str(root / "link.txt"))

    class FakePass(Pass):
        def __init__(self, root):
            self.root = str(root)
            self.reports = []

    p = FakePass(root)
    rows = Pass.scan_bridge(p)
    assert "link.txt" not in rows
