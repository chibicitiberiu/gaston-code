"""Sync over the real wire protocol, with a fake guest.

This exercises the frame handling, the manifest parse and the transfer state
machine. It does NOT prove anything about Windows 98 -- CPython cannot model
case-insensitivity, 8.3 aliasing, reserved device names or FAT granularity, and
a green run here is evidence about framing only. The guest's own behaviour is
covered by test_guest_agreement.py, which runs the shipping C.
"""

import asyncio
import pathlib
import os
import zlib

import pytest

from bridge.files import ignores as I
from bridge.files import sync as S
from bridge.files import paths as P
from bridge.wire.protocol import Frame
from bridge.files.sync import Baseline, Entry, Type
from bridge.files.syncrun import Pass, SyncError
from bridge.files.synctransport import SyncTransport

import sys as _sys, os as _os
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
from test_server_integration import _recv, harness  # noqa: E402,F401


def manifest_line(rel: str, data: bytes, typ=b"f", v=1) -> bytes:
    """Exactly the format win/syncrpc.c emits, for either version.

    v1 carries the checksum the CLIENT's own classification selected; v2
    carries the normalised and the raw one side by side, which is what lets
    `.gitattributes` re-classify the row here.
    """
    cls = P.classify(data)
    norm = zlib.crc32(P.normalise(data, P.CLASS_TEXT))
    raw_crc = zlib.crc32(data)
    rb = rel.encode("cp1252")
    if v >= 2:
        return (b"%s %s %d %d %d %d %d %s\n"
                % (typ, cls.encode(), len(data), 0, norm, raw_crc,
                   len(rb), rb))
    crc = norm if cls == P.CLASS_TEXT else raw_crc
    return (b"%s %s %d %d %d %d %s\n" % (typ, cls.encode(), len(data), 0, crc,
                                         len(rb), rb))


async def fake_scan(harness, reader, writer, files: dict, *, tree_id="VOL-1",
                    status="ok", claim=None, v=1, root="C:\\WORK"):
    """Answer one SYNCSCAN the way the guest would."""
    req = await _recv(harness, reader, "SYNCSCAN")
    sid = req.headers["id"]
    body = b"".join(manifest_line(r, d, v=v) for r, d in files.items())
    if body:
        await harness.send(writer, Frame("SYNCENT", {"id": sid,
                                                     "n": str(len(files))}, body))
    await harness.send(writer, Frame("SYNCEND", {
        "id": sid, "status": status,
        "files": str(len(files) if claim is None else claim),
        "tree_id": tree_id,
        # The real guest always sends this; omitting it here meant the root
        # guard was never exercised by any test that used this helper.
        "root": root}))
    return sid


async def fake_get(harness, reader, writer, data: bytes):
    """Answer one SYNCGET the way the guest would.

    Conflicts need this because resolution is copy -> PULL -> advance -> notify:
    guest-wins is not a decision the bridge can apply without fetching the
    guest's bytes. A conflict test whose fake guest only serves the manifest is
    testing the staging and nothing else.
    """
    req = await _recv(harness, reader, "SYNCGET")
    sid = req.headers["id"]
    cls = P.classify(data)
    crc = zlib.crc32(P.normalise(data, cls))
    wire = P.to_guest(data, cls)
    await harness.send(writer, Frame("SYNCDAT", {
        "id": sid, "total": str(len(wire)), "off": "0"}, wire))
    await harness.send(writer, Frame("SYNCDATEND", {
        "id": sid, "status": "ok", "crc": str(crc), "cls": cls}))
    return sid


def a_pass(harness, tmp_path, root=None):
    root = root or str(tmp_path / "tree")
    os.makedirs(root, exist_ok=True)
    state = str(tmp_path / "state")
    os.makedirs(state, exist_ok=True)
    # Some callers build a Pass before harness.connect(), when there is no
    # guest yet -- SyncTransport tolerates that (nothing dereferences it
    # until a transport method is actually awaited) the same way the old
    # `getattr(guest, "caps", None) or C.Caps()` tolerated a None guest.
    guest = harness.bridge._guest
    caps = guest.caps if guest is not None else None
    return Pass(SyncTransport(guest), caps, root, state)


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


async def test_too_many_names_the_actual_limit(harness, tmp_path):
    """03.24: SYNCEND only ever carries status=too_many -- the count itself
    never crosses the wire, because the scan that would have counted past it
    is the one that gave up instead of finishing. Before this, the message
    was just "guest scan returned too_many", and nobody reading it could tell
    how many files is too many."""
    from bridge.files.syncrun import SYNC_MAX_FILES
    reader, writer = await harness.connect()
    await _recv(harness, reader, "OPTIONSEND")
    p = a_pass(harness, tmp_path)
    task = asyncio.create_task(p.scan_guest())
    await fake_scan(harness, reader, writer, {}, status="too_many")
    with pytest.raises(SyncError, match=str(SYNC_MAX_FILES)):
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

    task = asyncio.create_task(p.push("a.txt", None))
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

    task = asyncio.create_task(p.push("b.dat", None))
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
        p.push("a.txt", Entry(type=Type.FILE, crc=1234)))
    put = await _recv(harness, reader, "SYNCPUT")
    end = await _recv(harness, reader, "SYNCPUTEND")
    assert end.headers["if_crc"] == "1234"

    await harness.send(writer, Frame("SYNCPUTACK", {
        "id": put.headers["id"], "status": "changed", "crc": "0"}))
    assert await asyncio.wait_for(task, 5) is False
    assert any("changed" in r for r in p.reports)
    await harness.close(writer)


async def test_a_too_long_push_refusal_names_the_actual_limit(harness, tmp_path):
    """03.41: `too_long` used to reach the agent as a bare "push X refused:
    too_long" with no number in it, unlike exec's own too_long, which names
    its limit. The bridge already has the number -- the same `paths.check`
    the pre-push scan ran -- so this re-derives it purely to explain a
    refusal the guest has already made.

    The push here is of an ordinary short path, so the bridge's own check
    agrees nothing is wrong with it -- which is itself worth reporting: the
    guest and the bridge disagreeing about the limit is news, not silence.
    """
    reader, writer = await harness.connect()
    await _recv(harness, reader, "OPTIONSEND")
    p = a_pass(harness, tmp_path)
    open(os.path.join(p.root, "a.txt"), "wb").write(b"x\n")

    task = asyncio.create_task(p.push("a.txt", None))
    put = await _recv(harness, reader, "SYNCPUT")
    await _recv(harness, reader, "SYNCPUTEND")
    await harness.send(writer, Frame("SYNCPUTACK", {
        "id": put.headers["id"], "status": "too_long", "crc": "0"}))
    assert await asyncio.wait_for(task, 5) is False
    assert any("too_long" in r and "disagrees" in r for r in p.reports), \
        p.reports
    await harness.close(writer)


async def test_an_ack_with_the_wrong_crc_is_reported_not_retried(
        harness, tmp_path):
    """Invariant 2. Retrying a transfer that cannot establish
    crc(guest) == crc(bridge) is how a livelock starts."""
    reader, writer = await harness.connect()
    await _recv(harness, reader, "OPTIONSEND")
    p = a_pass(harness, tmp_path)
    open(os.path.join(p.root, "a.txt"), "wb").write(b"x\n")

    task = asyncio.create_task(p.push("a.txt", None))
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
    assert await p.push("big.bin", None) is False
    assert any("exceeds" in r for r in p.reports)
    await harness.close(writer)


async def test_sync_frames_are_never_re_delivered_on_reconnect(harness, tmp_path):
    """There is no wire replay any more (docs/BACKLOG.md #5) -- nothing
    resends a frame from a log. The one thing that DOES re-send state after
    a reconnect is the explicit reissue path, and a re-delivered SYNCPUT
    would rewrite a file that has already finished writing -- so SYNC*
    frames must never be among what a reconnect sends. See
    test_server_integration.py::test_exec_is_never_re_delivered for the
    same property on the exec side.
    """
    reader, writer = await harness.connect()
    await _recv(harness, reader, "OPTIONSEND")
    p = a_pass(harness, tmp_path)
    open(os.path.join(p.root, "a.txt"), "wb").write(b"x\n")

    task = asyncio.create_task(p.push("a.txt", None))
    put = await _recv(harness, reader, "SYNCPUT")
    await _recv(harness, reader, "SYNCPUTEND")
    await harness.send(writer, Frame("SYNCPUTACK", {
        "id": put.headers["id"], "status": "ok", "crc": "0"}))
    await asyncio.wait_for(task, 5)
    await harness.close(writer)

    reader2, writer2 = await harness.connect()
    got = await harness.recv_until(reader2, {"OPTIONSEND"}, timeout=3)
    types = {f.type for f in got}
    assert not any(t.startswith("SYNC") for t in types)
    await harness.close(writer2)


async def test_a_disconnect_mid_transfer_fails_it_rather_than_hanging(
        harness, tmp_path):
    reader, writer = await harness.connect()
    await _recv(harness, reader, "OPTIONSEND")
    p = a_pass(harness, tmp_path)
    open(os.path.join(p.root, "a.txt"), "wb").write(b"x\n")

    task = asyncio.create_task(p.push("a.txt", None))
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


def test_the_bridge_scan_filters_DIRECTORY_names_and_prunes(tmp_path):
    r"""A directory name goes through the same filter as a file name.

    This was file-only, and the hole showed up on the real machine: `con/x.c`
    was correctly refused as a file while `con` itself was sent as a
    SYNCMKDIR -- so the bridge asked Windows 98 to create `C:\WORK\con`, the
    `\con\` shape that can take a 9x machine down. The guest happened to
    refuse it. A filter that relies on the receiver refusing is not a filter.

    Refusing prunes: walking into it would report every legal child separately
    when there is one cause and nowhere for any of them to go.
    """
    root = tmp_path / "tree"
    (root / "con").mkdir(parents=True)
    (root / "con" / "x.c").write_bytes(b"x\n")
    (root / "aux").mkdir()
    (root / "aux" / "y.c").write_bytes(b"y\n")
    (root / "src").mkdir()
    (root / "src" / "ok.c").write_bytes(b"ok\n")

    class FakePass(Pass):
        def __init__(self, root):
            self.root = str(root)
            self.reports = []

    p = FakePass(root)
    rows = Pass.scan_bridge(p)
    assert "src" in rows and "src/ok.c" in rows
    assert "con" not in rows and "aux" not in rows
    assert "con/x.c" not in rows and "aux/y.c" not in rows
    assert any("con/" in r and "reserved" in r for r in p.reports)
    # One report per refused directory, not one per file underneath it.
    assert len([r for r in p.reports if "con" in r]) == 1


def test_the_bridge_scan_refuses_a_directory_path_over_max_path(tmp_path):
    """The limit is on the FULL guest path, so it bites at a depth that is
    perfectly legal on ext4."""
    root = tmp_path / "tree"
    root.mkdir()
    deep = root
    for _ in range(9):
        deep = deep / ("d" * 30)
    deep.mkdir(parents=True)
    (deep / "f.txt").write_bytes(b"q\n")

    class FakePass(Pass):
        def __init__(self, root):
            self.root = str(root)
            self.reports = []

    p = FakePass(root)
    rows = Pass.scan_bridge(p)
    assert not any(k.count("/") >= 8 for k in rows), \
        "a directory past MAX_PATH was offered to the guest"
    assert any("too long" in r for r in p.reports)


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

    # A real directory outside the tree, so os.walk sees a symlinked DIR and
    # the dirnames branch runs; a dangling link is a file to os.walk and takes
    # the other branch, which is why both are here.
    (tmp_path / "elsewhere").mkdir()
    (tmp_path / "elsewhere" / "inside.txt").write_bytes(b"not ours\n")
    os.symlink(str(tmp_path / "elsewhere"), str(root / "linkdir"))
    os.symlink(str(tmp_path / "nothing-here"), str(root / "dangling.txt"))

    p = FakePass(root)
    rows = Pass.scan_bridge(p)
    assert "link.txt" not in rows
    assert "linkdir" not in rows
    # Containment was never the whole job. An agent that writes into a
    # symlinked directory is told the write succeeded and, without this,
    # never learns that none of it is going to the machine.
    assert any("link.txt" in r and "symbolic link" in r for r in p.reports)
    assert any("linkdir/" in r and "symbolic link" in r for r in p.reports)
    assert "linkdir/inside.txt" not in rows
    assert any("dangling.txt" in r and "symbolic link" in r for r in p.reports)


# --- conflicts and the full pass -------------------------------------------

async def test_a_conflict_preserves_the_agents_version_and_latches(
        harness, tmp_path):
    """Guest wins, but the agent's work is moved rather than destroyed -- and
    the path latches so the agent's in-flight turn cannot push over the user."""
    reader, writer = await harness.connect()
    await _recv(harness, reader, "OPTIONSEND")
    p = a_pass(harness, tmp_path)
    open(os.path.join(p.root, "a.txt"), "wb").write(b"agent version\n")
    p.base.rows["a.txt"] = Entry(type=Type.FILE, crc=111, display="a.txt")

    task = asyncio.create_task(p.run())
    await fake_scan(harness, reader, writer, {"a.txt": b"user version\n"})
    await fake_get(harness, reader, writer, b"user version\n")
    got = await asyncio.wait_for(task, 10)

    assert got["conflicts"] == 1
    mine = os.path.join(p.conflict_dir, "a.txt", "mine.1")
    assert open(mine, "rb").read() == b"agent version\n"
    assert "a.txt" in p.base.latched
    # The notice has to name the preserved copy AND the ancestor: a merge
    # against "whatever the file is now" is not a three-way merge, and the
    # ancestor is the only thing that says which side changed what.
    note = next(n for n in got["notices"] if "a.txt" in n)
    assert "mine.1" in note and "ancestor" in note
    # And it must NOT send the agent back to the user for a confirmation they
    # have already given by pressing Continue.
    assert "do not ask them again" in note

    # Guest wins means the guest's bytes are HERE afterwards. Staging the
    # agent's copy and stopping is only half of it: for a while this branch did
    # exactly that, the divergence survived the pass, and the same conflict was
    # re-detected and re-staged every thirty seconds forever.
    assert open(os.path.join(p.root, "a.txt"), "rb").read() == b"user version\n"
    assert got["pulled"] == 1
    assert p.base.rows["a.txt"].crc == zlib.crc32(b"user version\n")
    await harness.close(writer)


async def test_a_conflict_is_resolved_once_not_every_pass(harness, tmp_path):
    """The second pass over the same state finds nothing to do.

    This is the property that makes the baseline advance at conflict
    resolution worth the lost ancestor: without it the user gets a notice, and
    another staged copy of the agent's work, every time the timer fires while
    they are still typing.
    """
    reader, writer = await harness.connect()
    await _recv(harness, reader, "OPTIONSEND")
    p = a_pass(harness, tmp_path)
    open(os.path.join(p.root, "a.txt"), "wb").write(b"agent version\n")
    p.base.rows["a.txt"] = Entry(type=Type.FILE, crc=111, display="a.txt")

    task = asyncio.create_task(p.run())
    await fake_scan(harness, reader, writer, {"a.txt": b"user version\n"})
    await fake_get(harness, reader, writer, b"user version\n")
    first = await asyncio.wait_for(task, 10)
    assert first["conflicts"] == 1

    # Same baseline, same tree, same guest -- a second pass.
    p2 = a_pass(harness, tmp_path, root=p.root)
    task = asyncio.create_task(p2.run())
    await fake_scan(harness, reader, writer, {"a.txt": b"user version\n"})
    second = await asyncio.wait_for(task, 10)

    assert second["conflicts"] == 0, "the conflict was re-detected"
    assert second["pulled"] == 0 and second["pushed"] == 0
    assert not os.path.exists(os.path.join(p.conflict_dir, "a.txt", "mine.2"))
    await harness.close(writer)


async def test_a_latched_path_refuses_pushes(harness, tmp_path):
    """The agent's turn keeps writing after a conflict it has not been told
    about. Without this the next pass pushes over the user's version.

    The state is the one that follows a resolved conflict: the baseline now
    matches what the GUEST holds (it advanced at resolution), and only the
    bridge has moved since -- because the agent's in-flight turn wrote again
    before the preamble reached it. That is an ordinary PUSH by every rule in
    the table, and pushing it would put the agent's unconsidered write on the
    user's machine. The latch is what stops it, mechanically.
    """
    reader, writer = await harness.connect()
    await _recv(harness, reader, "OPTIONSEND")
    p = a_pass(harness, tmp_path)
    user = b"user version\n"
    open(os.path.join(p.root, "a.txt"), "wb").write(b"agent wrote again\n")
    p.base.rows["a.txt"] = Entry(
        type=Type.FILE, cls=P.classify(user), crc=zlib.crc32(user),
        display="a.txt")
    p.base.latched["a.txt"] = "earlier conflict"

    task = asyncio.create_task(p.run())
    await fake_scan(harness, reader, writer, {"a.txt": user})
    got = await asyncio.wait_for(task, 10)

    assert got["pushed"] == 0, "a latched path must not push"
    assert got["latched"] >= 1
    assert any("latched" in r for r in got["reports"])
    await harness.close(writer)


async def test_a_second_conflict_does_not_overwrite_the_first_copy(
        harness, tmp_path):
    """Versioned, or the mechanism meant to preserve the agent's work destroys
    it on the second conflict."""
    p = a_pass(harness, tmp_path)
    open(os.path.join(p.root, "a.txt"), "wb").write(b"first\n")
    one = p.stage_conflict("a.txt")
    open(os.path.join(p.root, "a.txt"), "wb").write(b"second\n")
    two = p.stage_conflict("a.txt")
    assert one != two
    assert open(one, "rb").read() == b"first\n"
    assert open(two, "rb").read() == b"second\n"


async def test_a_full_pass_pushes_a_new_file_and_advances_the_baseline(
        harness, tmp_path):
    reader, writer = await harness.connect()
    await _recv(harness, reader, "OPTIONSEND")
    p = a_pass(harness, tmp_path)
    open(os.path.join(p.root, "new.txt"), "wb").write(b"fresh\n")

    async def guest():
        await fake_scan(harness, reader, writer, {})
        put = await _recv(harness, reader, "SYNCPUT")
        end = await _recv(harness, reader, "SYNCPUTEND")
        await harness.send(writer, Frame("SYNCPUTACK", {
            "id": put.headers["id"], "status": "ok",
            "crc": end.headers["crc"]}))

    task = asyncio.create_task(p.run())
    await asyncio.wait_for(guest(), 10)
    got = await asyncio.wait_for(task, 10)

    assert got["pushed"] == 1
    assert "new.txt" in p.base.rows
    # The ancestor is kept as CONTENT, which is what makes a reverted push
    # detectable and a three-way merge possible.
    assert open(os.path.join(p.base_dir, "new.txt"), "rb").read() == b"fresh\n"
    await harness.close(writer)


async def test_a_deletion_on_both_sides_tombstones_and_does_not_resurrect(
        harness, tmp_path):
    reader, writer = await harness.connect()
    await _recv(harness, reader, "OPTIONSEND")
    p = a_pass(harness, tmp_path)
    p.base.rows["gone.txt"] = Entry(type=Type.FILE, crc=5, display="gone.txt")

    task = asyncio.create_task(p.run())
    await fake_scan(harness, reader, writer, {})
    await asyncio.wait_for(task, 10)

    assert "gone.txt" in p.base.tombs
    assert "gone.txt" not in p.base.rows
    assert p.base.tombs["gone.txt"].crc == 5     # the CRC, not a bare marker
    await harness.close(writer)


async def test_a_failed_mkdir_withholds_the_pushes_under_it_without_spam(
        harness, tmp_path):
    """03.45: a directory whose MKDIR the guest refuses used to still get
    every file under it pushed anyway, each one individually refused by the
    guest as `no_parent` -- one cause, reported once per file, every single
    pass. Now the whole subtree is withheld after the one refusal, with one
    report, and the guest is never even asked for a transfer that can only
    fail.

    Proof this is real and not cosmetic: without the fix, `run()` goes on to
    send SYNCPUT for `sub/file.txt` and this fake guest never answers it, so
    the pass hangs until the outer `wait_for` below times out.
    """
    reader, writer = await harness.connect()
    await _recv(harness, reader, "OPTIONSEND")
    p = a_pass(harness, tmp_path)
    os.makedirs(os.path.join(p.root, "sub"))
    open(os.path.join(p.root, "sub", "file.txt"), "wb").write(b"x\n")

    task = asyncio.create_task(p.run())
    await fake_scan(harness, reader, writer, {})
    mk = await _recv(harness, reader, "SYNCMKDIR")
    assert mk.body == b"sub"
    await harness.send(writer, Frame("SYNCMKDIRACK", {
        "id": mk.headers["id"], "status": "no_parent"}))

    done = await asyncio.wait_for(task, 10)
    assert done["dirs"] == 0
    assert "sub" not in p.base.rows
    assert "sub/file.txt" not in p.base.rows
    assert any("withheld" in r and "sub" in r for r in p.reports), p.reports
    # The mkdir failure itself is still reported and explained -- withholding
    # the child is on top of that, not instead of it. `no_parent` gets a
    # plain-English note rather than the bare status word.
    assert any("own parent directory" in n for n in p.base.pending_notice), \
        p.base.pending_notice
    await harness.close(writer)


async def test_the_baseline_survives_a_restart_mid_pass(harness, tmp_path):
    """The record distinguishing 'here but not there' from 'deleted there' must
    not be destroyed by one badly timed interruption."""
    p = a_pass(harness, tmp_path)
    p.base.rows["a.txt"] = Entry(type=Type.FILE, crc=7, display="a.txt")
    p.base.tree_id = "VOL-1"
    p.base.save(p.state)

    fresh = a_pass(harness, tmp_path, root=p.root)
    assert fresh.base.rows["a.txt"].crc == 7
    assert fresh.base.tree_id == "VOL-1"


def test_neither_scanner_reports_its_own_working_files(tmp_path):
    """`.gaston-tmp` and `.gaston-old` are ours and must never be manifest rows.

    A put that fails partway leaves a temp behind. If the scanner reports it,
    the other side has never seen that path, so it transfers it -- and now both
    machines hold a permanent `x.md.gaston-tmp` that syncs forever beside
    `x.md`. That happened on the real guest: a push failed on a path bug and
    the next pass pulled the leftover onto the bridge as ordinary content.

    The guest half of this lives in `win/sync.c`'s `walk` and is covered by
    `test_guest_agreement.py`; this is the bridge half.
    """
    root = tmp_path / "tree"
    root.mkdir()
    (root / "real.md").write_bytes(b"content\n")
    (root / "real.md.gaston-tmp").write_bytes(b"half a write\n")
    (root / "real.md.gaston-old").write_bytes(b"the previous version\n")

    class FakePass(Pass):
        def __init__(self, root):
            self.root = str(root)
            self.reports = []

    p = FakePass(root)
    rows = Pass.scan_bridge(p)
    assert "real.md" in rows
    assert not any(".gaston-" in k for k in rows), sorted(rows)


def test_adding_a_gitignore_line_freezes_rows_rather_than_deleting_them(
        tmp_path):
    """The failure this prevents is a mass deletion nobody asked for.

    Editing `.gitignore` is routine, and one line can remove a thousand paths
    from the scan. Read as "present last pass, absent now" that is a thousand
    deletions, and under guest-wins the bridge would report every one of them
    -- or, with the tombstone rule applied naively, make them permanently
    unsyncable. The row has to FREEZE at its last agreed content instead, and
    come back with that same ancestor when the line is removed.
    """
    root = tmp_path / "tree"
    root.mkdir()
    (root / "keep.c").write_bytes(b"int x;\n")
    (root / "out.log").write_bytes(b"noise\n")
    state = tmp_path / "state"
    state.mkdir()

    p = Pass.__new__(Pass)
    p.root = str(root)
    p.state = str(state / "base.json")
    p.base = S.Baseline()
    p.reports = []
    p.base.rows["keep.c"] = Entry(type=Type.FILE, crc=1, display="keep.c")
    p.base.rows["out.log"] = Entry(type=Type.FILE, crc=2, display="out.log")

    (root / ".gitignore").write_text("*.log\n")
    p._ignores(None)
    assert "out.log" not in p.base.rows
    assert p.base.ignored["out.log"].crc == 2, "the ancestor was lost"
    assert "keep.c" in p.base.rows
    assert any("scope changed" in r and "frozen" in r for r in p.reports)

    # And back again: un-ignoring resumes from the stored ancestor rather than
    # looking like a first sync, which would conflict the whole set.
    (root / ".gitignore").write_text("")
    p.reports = []
    p._ignores(None)
    assert p.base.rows["out.log"].crc == 2
    assert not p.base.ignored
    assert any("no longer ignored" in r for r in p.reports)


def test_a_default_ignore_keeps_a_venv_off_the_guest(tmp_path):
    """--sync-dir is mandatory for this reason, and the defaults are the
    second line of it."""
    root = tmp_path / "tree"
    (root / ".venv" / "lib").mkdir(parents=True)
    (root / ".venv" / "lib" / "big.py").write_bytes(b"x" * 100)
    (root / "__pycache__").mkdir()
    (root / "__pycache__" / "m.pyc").write_bytes(b"\x00\x01")
    (root / "main.py").write_bytes(b"print(1)\n")

    class FakePass(Pass):
        def __init__(self, root):
            self.root = str(root)
            self.reports = []

    p = FakePass(root)
    rows = Pass.scan_bridge(p, I.load(str(root)))
    assert "main.py" in rows
    assert not any(k.startswith(".venv") or k.startswith("__pycache__")
                   for k in rows), sorted(rows)


async def test_an_unresolvable_divergence_is_announced_once(harness, tmp_path):
    """Not every pass, and the pass runs every thirty seconds.

    The agent deletes `old.c` as half of a `git mv`; the user still has it.
    There is no delete primitive, so resurrecting it here or removing it there
    would both lose work -- the honest answer is to report and change nothing,
    which means the condition STANDS. Repeating it forever trains the agent to
    ignore the channel that also carries conflicts.
    """
    reader, writer = await harness.connect()
    await _recv(harness, reader, "OPTIONSEND")
    user = b"still here\n"
    p = a_pass(harness, tmp_path)
    p.base.rows["old.c"] = Entry(type=Type.FILE, cls="t",
                                 crc=zlib.crc32(user), display="old.c")

    task = asyncio.create_task(p.run())
    await fake_scan(harness, reader, writer, {"old.c": user})
    first = await asyncio.wait_for(task, 10)
    assert first["reported"] == 1
    assert any("old.c" in n for n in first["notices"])
    assert any("old.c" in r for r in first["new_reports"])
    # What `run_sync` does once the notices are actually on the wire.
    p.notices_delivered(first["notices"])

    p2 = a_pass(harness, tmp_path, root=p.root)
    task = asyncio.create_task(p2.run())
    await fake_scan(harness, reader, writer, {"old.c": user})
    second = await asyncio.wait_for(task, 10)

    assert second["reported"] == 1, "the condition still stands"
    assert not second["notices"], "but the agent was told twice"
    assert not second["new_reports"], "and the log would repeat it"
    # sync_status still wants the whole standing set, every time it is asked.
    assert any("old.c" in r for r in second["reports"])
    await harness.close(writer)


async def test_the_same_path_changing_is_news_again(harness, tmp_path):
    """The fingerprint carries the content, so 'you deleted main.c and the
    user still has it' is said again once the user EDITS it -- a different
    fact about a different file."""
    reader, writer = await harness.connect()
    await _recv(harness, reader, "OPTIONSEND")
    v1, v2 = b"first\n", b"the user changed it\n"
    p = a_pass(harness, tmp_path)
    p.base.rows["old.c"] = Entry(type=Type.FILE, cls="t",
                                 crc=zlib.crc32(v1), display="old.c")

    task = asyncio.create_task(p.run())
    await fake_scan(harness, reader, writer, {"old.c": v1})
    await asyncio.wait_for(task, 10)

    p2 = a_pass(harness, tmp_path, root=p.root)
    task = asyncio.create_task(p2.run())
    await fake_scan(harness, reader, writer, {"old.c": v2})
    second = await asyncio.wait_for(task, 10)
    assert any("old.c" in n for n in second["notices"])
    await harness.close(writer)


async def test_a_notice_is_delivered_once_and_then_cleared(harness, tmp_path):
    """`pending_notice` is a queue of work, not a log.

    Nothing cleared it, so every notice ever raised was re-sent on every pass.
    The user would have seen the same conflict message twice a minute -- worse
    than not reporting it, because it buries the next real one. It is still
    written BEFORE the baseline advances, so a crash between the two leaves the
    notice to be delivered on startup rather than lost.
    """
    reader, writer = await harness.connect()
    await _recv(harness, reader, "OPTIONSEND")
    p = a_pass(harness, tmp_path)
    p.note("SYNC: something happened")
    assert p.base.pending_notice == ["SYNC: something happened"]

    # Survives a reload, which is the point of writing it before acting.
    p2 = a_pass(harness, tmp_path, root=p.root)
    assert p2.base.pending_notice == ["SYNC: something happened"]

    p2.notices_delivered(["SYNC: something happened"])
    p3 = a_pass(harness, tmp_path, root=p.root)
    assert p3.base.pending_notice == []
    await harness.close(writer)


async def test_clearing_does_not_drop_a_notice_raised_meanwhile(
        harness, tmp_path):
    """Cleared by value, not by emptying the list.

    A pass can raise a new notice while the previous batch is being written to
    the wire; truncating would discard it with nobody told, which is the exact
    failure the durable queue exists to prevent.
    """
    p = a_pass(harness, tmp_path)
    p.note("first")
    delivering = list(p.base.pending_notice)
    p.note("raised while the first was being sent")
    p.notices_delivered(delivering)
    assert p.base.pending_notice == ["raised while the first was being sent"]


# --- 02.20: the agent's copy of a notice is durable too ---------------------


def test_note_writes_both_durable_queues(harness, tmp_path):
    """The user's and the agent's copies are two separate durable queues on
    the SAME baseline, not the same list read twice -- so one can be
    delivered without the other."""
    p = a_pass(harness, tmp_path)
    p.note("SYNC: conflict on x.c")
    assert p.base.pending_notice == ["SYNC: conflict on x.c"]
    assert p.base.pending_notice_agent == ["SYNC: conflict on x.c"]

    reloaded = Baseline.load(p.state)
    assert reloaded.pending_notice_agent == ["SYNC: conflict on x.c"]


async def test_a_bridge_restart_does_not_lose_the_agents_copy_of_a_notice(
        harness, tmp_path):
    """The scenario 02.20 names: a pass raises a conflict notice, it reaches
    the user's screen (and is cleared from the user's durable queue), and the
    bridge is replaced -- a crash, a redeploy -- before the agent's next
    turn. Before this, Bridge._agent_notices (now Bridge.synccoord's
    agent_notices, docs/audit/02-bridge-python.md 02.43) was memory-only, so
    the news the agent most needed (a conflict that latched its writes) was
    the one that did not survive.
    """
    from bridge.server import Bridge

    p = a_pass(harness, tmp_path)
    p.note("SYNC: conflict on x.c -- your copy was kept")
    # What run_sync does once the notice is actually on the user's screen:
    # clear the USER's copy. The agent's has not been touched.
    p.notices_delivered(["SYNC: conflict on x.c -- your copy was kept"])
    assert p.base.pending_notice == [], "test setup: user's copy delivered"
    assert p.base.pending_notice_agent == [
        "SYNC: conflict on x.c -- your copy was kept"], (
        "test setup: agent's copy still outstanding")

    # A fresh Bridge, standing in for the process that replaced the one
    # that ran the pass above -- nothing in memory survives a restart. Its
    # SyncCoordinator is built fresh right alongside it, the same way.
    fresh = Bridge("s", host="127.0.0.1", port=0)
    fresh.sync_dir = p.root
    fresh.sync_state = os.path.dirname(p.state)
    assert fresh.synccoord.agent_notices == []

    fresh._seed_agent_notices()
    assert fresh.synccoord.agent_notices == [
        "SYNC: conflict on x.c -- your copy was kept"], (
        "the notice the old process never got to hand over must be picked "
        "up from disk, not lost with the process that wrote it"
    )

    out = await fresh._with_sync_preamble("carry on")
    assert "x.c" in out
    assert fresh.synccoord.agent_notices == []
    # And the durable half actually cleared, not just the in-memory cache --
    # or the NEXT restart would hand the same notice over a third time.
    assert Baseline.load(p.state).pending_notice_agent == []


class _FakeClaude:
    """Just enough session for the turn to start and be observed.

    `close()` is here because the harness tears the bridge down after every
    test and would otherwise fail on the fake rather than on the assertion --
    which turns a passing test into an error and a failing one into a mystery.
    """

    session_id = "s"

    def __init__(self, sink: list):
        self.sink = sink

    async def send(self, text):
        self.sink.append(text)
        raise RuntimeError("stop here; the turn machinery is not the point")

    async def close(self):
        pass


async def test_a_conflict_puts_a_continue_bar_on_the_users_screen(
        harness, tmp_path):
    """"Wait for the user to confirm they have finished" needs a confirmation.

    Until this existed the only inbound channels were INPUT and PERMREPLY, and
    reading the confirmation off the next INPUT was worse than useless: the
    most natural reaction to being told the agent stopped is to type "wait,
    stop", which would have been the message that restarted it.
    """
    reader, writer = await harness.connect()
    await _recv(harness, reader, "OPTIONSEND")
    p = a_pass(harness, tmp_path)
    harness.bridge.sync_dir = p.root
    harness.bridge.sync_state = os.path.dirname(p.state)
    _commit(harness, p.root)
    open(os.path.join(p.root, "a.txt"), "wb").write(b"agent version\n")
    p.base.rows["a.txt"] = Entry(type=Type.FILE, crc=111, display="a.txt")
    p.base.save(p.state)

    task = asyncio.create_task(harness.bridge.run_sync("test"))
    await fake_scan(harness, reader, writer, {"a.txt": b"user version\n"})
    await fake_get(harness, reader, writer, b"user version\n")
    await asyncio.wait_for(task, 10)

    halt = await _recv(harness, reader, "HALT")
    assert halt.headers["id"]
    assert b"conflicting edit" in halt.body
    # The bar's label is clipped at roughly seventy characters on a 640x480
    # screen, and a line that runs past that loses its end silently.
    assert len(halt.body) <= 70, halt.body
    assert harness.bridge.synccoord.halt_id == halt.headers["id"]
    await harness.close(writer)


async def test_continue_starts_the_agent_with_an_instruction_not_an_echo(
        harness, tmp_path):
    """The turn is the bridge's, not the user's.

    Rendering "Resolve the sync conflict" as though they had typed it would put
    words in their mouth in a transcript they can scroll back through later.
    """
    reader, writer = await harness.connect()
    await _recv(harness, reader, "OPTIONSEND")
    guest = harness.bridge._guest
    harness.bridge.synccoord.halt_id = "h123"

    sent = []
    guest.claude = _FakeClaude(sent)
    await harness.send(writer, Frame("RESUME", {"id": "h123"}))
    for _ in range(40):
        if sent:
            break
        await asyncio.sleep(0.05)

    assert sent and "Resolve the sync conflict" in sent[0]
    assert harness.bridge.synccoord.halt_id == "", (
        "the halt must clear when it is answered")
    await harness.close(writer)


async def test_a_stale_resume_is_ignored(harness, tmp_path):
    """A RESUME can lose a race with a halt the bridge cleared itself -- the
    agent called sync_resolve, say. Acting on it would start a turn nobody
    asked for."""
    reader, writer = await harness.connect()
    await _recv(harness, reader, "OPTIONSEND")
    guest = harness.bridge._guest
    harness.bridge.synccoord.halt_id = "h999"

    sent = []
    guest.claude = _FakeClaude(sent)
    await harness.send(writer, Frame("RESUME", {"id": "h123"}))
    await asyncio.sleep(0.3)
    assert not sent
    assert harness.bridge.synccoord.halt_id == "h999"
    await harness.close(writer)


async def test_a_file_where_we_hold_a_directory_does_not_abort_the_pass(
        harness, tmp_path):
    """The guest puts a file at a path we hold as a directory.

    Two bugs lived here. `stage_conflict` did `open(src, "rb")` on what was a
    directory, and IsADirectoryError is caught nowhere below `run_sync` -- so
    one such collision aborted the WHOLE pass, taking every unrelated file with
    it, on every pass, forever. And because a type change resolves to nothing
    (applying it would mean deleting a subtree, and there is no delete
    primitive), the conflict was re-decided every pass: a fresh mine.N and a
    fresh notice every thirty seconds.
    """
    reader, writer = await harness.connect()
    await _recv(harness, reader, "OPTIONSEND")
    p = a_pass(harness, tmp_path)

    # We hold notes/ as a directory with a child in it; the child is the thing
    # row 3 says must be preserved before anything is resolved.
    os.makedirs(os.path.join(p.root, "notes"))
    open(os.path.join(p.root, "notes", "kept.txt"), "wb").write(b"agent work\n")
    open(os.path.join(p.root, "other.txt"), "wb").write(b"unrelated\n")
    p.base.rows["notes"] = Entry(type=Type.DIR, display="notes")
    p.base.rows["notes/kept.txt"] = Entry(
        type=Type.FILE, crc=zlib.crc32(b"agent work\n"), display="notes/kept.txt")

    # other.txt already agrees on both sides, so it needs no transfer -- it is
    # here to prove an unrelated path survives the collision, not to move.
    p.base.rows["other.txt"] = Entry(
        type=Type.FILE, crc=zlib.crc32(b"unrelated\n"), display="other.txt")

    task = asyncio.create_task(p.run())
    await fake_scan(harness, reader, writer, {
        "notes": b"user made this a file\n",
        "other.txt": b"unrelated\n"})
    got = await asyncio.wait_for(task, 10)

    # The pass completed rather than raising -- that is the half that made this
    # severe, because the abort took every unrelated path with it.
    assert "error" not in got
    assert "other.txt" in p.base.rows

    # The subtree is preserved, descendants included.
    mine = os.path.join(p.conflict_dir, "notes", "mine.1")
    assert os.path.isdir(mine)
    assert open(os.path.join(mine, "kept.txt"), "rb").read() == b"agent work\n"

    # Nothing was deleted here and nothing was applied.
    assert os.path.isdir(os.path.join(p.root, "notes"))

    # The notice says what is actually wrong, and does not tell the agent to
    # merge a directory against a file.
    note = next(n for n in got["notices"] if "notes" in n)
    assert "directory" in note and "Merge the two" not in note

    # Said once. The server delivers the batch and clears it, exactly as
    # run_sync does, and a second pass over the same unchanged collision
    # re-decides it -- it must NOT stage mine.2 or say it again.
    p.notices_delivered(got["notices"])
    p2 = a_pass(harness, tmp_path, root=p.root)
    task = asyncio.create_task(p2.run())
    await fake_scan(harness, reader, writer, {
        "notes": b"user made this a file\n",
        "other.txt": b"unrelated\n"})
    got2 = await asyncio.wait_for(task, 10)
    assert not os.path.exists(os.path.join(p.conflict_dir, "notes", "mine.2"))
    assert not [n for n in got2["notices"] if "SYNC CONFLICT notes" in n]
    await harness.close(writer)


# --- .gitattributes over the wire -------------------------------------------

async def test_a_minus_text_file_is_pushed_without_conversion(
        harness, tmp_path):
    """`-text` means the two sides hold the same bytes. No CRLF expansion, and
    the checksum the client is asked to match is over the raw bytes."""
    reader, writer = await harness.connect(hdrs={"manifest": 2})
    await _recv(harness, reader, "OPTIONSEND")
    p = a_pass(harness, tmp_path)
    open(os.path.join(p.root, ".gitattributes"), "wb").write(b"*.mk -text\n")
    body = b"all:\n\tmake\n"
    open(os.path.join(p.root, "rules.mk"), "wb").write(body)

    sent = {}

    async def guest():
        await fake_scan(harness, reader, writer, {}, v=2)
        for _ in range(2):          # .gitattributes and rules.mk, either order
            put = await _recv(harness, reader, "SYNCPUT")
            end = await _recv(harness, reader, "SYNCPUTEND")
            name = put.body.split(b" ", 1)[1][:int(put.body.split(b" ", 1)[0])]
            sent[name.decode()] = (put.body[len(put.body.split(b" ", 1)[0]) + 1
                                            + len(name):],
                                   end.headers["cls"], end.headers["crc"])
            await harness.send(writer, Frame("SYNCPUTACK", {
                "id": put.headers["id"], "status": "ok",
                "crc": end.headers["crc"]}))

    task = asyncio.create_task(p.run())
    await asyncio.wait_for(guest(), 10)
    got = await asyncio.wait_for(task, 10)
    assert got["pushed"] == 2

    wire, cls, crc = sent["rules.mk"]
    assert cls == P.CLASS_EXACT
    assert wire == body, "a -text file must cross unconverted"
    assert int(crc) == zlib.crc32(body)
    await harness.close(writer)


async def test_eol_lf_keeps_one_path_lf_on_a_crlf_client(harness, tmp_path):
    """The Makefile a cross toolchain has to read. Everything else still gets
    the client's own convention, which is what makes this worth a per-path
    rule rather than a global switch."""
    reader, writer = await harness.connect(hdrs={"manifest": 2})
    await _recv(harness, reader, "OPTIONSEND")
    p = a_pass(harness, tmp_path)
    open(os.path.join(p.root, ".gitattributes"), "wb").write(b"Makefile eol=lf\n")
    open(os.path.join(p.root, "Makefile"), "wb").write(b"all:\n\tcc x.c\n")
    open(os.path.join(p.root, "notes.txt"), "wb").write(b"one\ntwo\n")

    sent = {}

    async def guest():
        await fake_scan(harness, reader, writer, {}, v=2)
        for _ in range(3):
            put = await _recv(harness, reader, "SYNCPUT")
            end = await _recv(harness, reader, "SYNCPUTEND")
            plen, rest = put.body.split(b" ", 1)
            name = rest[:int(plen)].decode()
            sent[name] = rest[int(plen):]
            await harness.send(writer, Frame("SYNCPUTACK", {
                "id": put.headers["id"], "status": "ok",
                "crc": end.headers["crc"]}))

    task = asyncio.create_task(p.run())
    await asyncio.wait_for(guest(), 10)
    await asyncio.wait_for(task, 10)

    assert sent["Makefile"] == b"all:\n\tcc x.c\n", "eol=lf must not expand"
    assert sent["notes.txt"] == b"one\r\ntwo\r\n", "the client's own default"
    await harness.close(writer)


async def test_a_minus_text_file_pulls_back_byte_for_byte(harness, tmp_path):
    """The user edits it on their machine and it arrives here EXACTLY.

    Without the override this file is uniform CRLF, so it would be folded to LF
    on the way in -- correct for source, wrong for a fixture whose endings are
    the thing being tested, and invisible either way because the normalised
    checksum does not change.
    """
    reader, writer = await harness.connect(hdrs={"manifest": 2})
    await _recv(harness, reader, "OPTIONSEND")
    p = a_pass(harness, tmp_path)
    open(os.path.join(p.root, ".gitattributes"), "wb").write(
        b"fixtures/* -text\n")
    os.makedirs(os.path.join(p.root, "fixtures"))
    open(os.path.join(p.root, "fixtures", "crlf.txt"), "wb").write(b"old\r\n")
    theirs = b"line one\r\nline two\r\n"
    p.base.rows["fixtures/crlf.txt"] = Entry(
        type=Type.FILE, cls=P.CLASS_EXACT, crc=zlib.crc32(b"old\r\n"),
        crc_raw=zlib.crc32(b"old\r\n"), dual=True, display="fixtures/crlf.txt")

    async def guest():
        await fake_scan(harness, reader, writer, {
            "fixtures/crlf.txt": theirs,
            ".gitattributes": b"fixtures/* -text\n",
            "fixtures": b""}, v=2)
        await fake_get(harness, reader, writer, theirs)

    task = asyncio.create_task(p.run())
    await asyncio.wait_for(guest(), 10)
    got = await asyncio.wait_for(task, 10)

    assert got["pulled"] == 1
    on_disk = open(os.path.join(p.root, "fixtures", "crlf.txt"), "rb").read()
    assert on_disk == theirs, "a -text pull must not fold CRLF"
    assert p.base.rows["fixtures/crlf.txt"].crc == zlib.crc32(theirs)
    await harness.close(writer)


async def test_a_v1_client_is_not_overridden_and_is_told_why(harness, tmp_path):
    """A client that sends one checksum computed it for its own reading of the
    bytes, and the other is not derivable -- a pure-LF file has them equal, so
    equality proves nothing. Honouring `-text` against such a client would
    compare a normalised checksum to a raw one on every pass, forever."""
    reader, writer = await harness.connect()          # no manifest header: v1
    await _recv(harness, reader, "OPTIONSEND")
    p = a_pass(harness, tmp_path)
    open(os.path.join(p.root, ".gitattributes"), "wb").write(b"*.txt -text\n")
    same = b"hello\r\nthere\r\n"
    open(os.path.join(p.root, "a.txt"), "wb").write(same)
    # Both already agreed, so nothing needs to transfer: the point of the test
    # is which class the rows are compared under, not the transfer.
    p.base.rows[".gitattributes"] = Entry(
        type=Type.FILE, crc=zlib.crc32(b"*.txt -text\n"),
        display=".gitattributes")
    p.base.rows["a.txt"] = Entry(
        type=Type.FILE, crc=zlib.crc32(b"hello\nthere\n"), display="a.txt")

    task = asyncio.create_task(p.run())
    await fake_scan(harness, reader, writer, {
        "a.txt": same, ".gitattributes": b"*.txt -text\n"})
    got = await asyncio.wait_for(task, 10)

    assert any("v1 manifest" in r for r in got["reports"]), got["reports"]
    # And it stayed in the class the client can actually speak, rather than
    # being forced into a comparison neither side can satisfy.
    assert got["conflicts"] == 0
    await harness.close(writer)


# --- sync must not hold the wire --------------------------------------------

async def test_a_big_push_does_not_starve_everything_else(harness, tmp_path):
    """A permission prompt must not wait for a megabyte to finish.

    The failure this guards is not lock contention -- it is that a chunk loop
    over an uncontended lock, with a drain() below the high-water mark, has no
    await point that actually suspends. Measured before the fix: the other task
    first ran after chunk 128 of 128, so the prompt was not queued behind sync,
    it had not been composed yet.

    Counted at the bridge's own send, because the client reads several frames
    per syscall and counting batches would measure the harness instead.
    """
    reader, writer = await harness.connect()
    await _recv(harness, reader, "OPTIONSEND")
    p = a_pass(harness, tmp_path)
    open(os.path.join(p.root, "big.bin"), "wb").write(b"x" * (64 * 1024))

    sent = 0
    ran_after = None
    # 02.19: Pass sends over its own SyncTransport now, which reaches
    # guest.link.send directly rather than through Bridge._send -- so this
    # counts at the link, the layer Pass actually calls.
    real_send = harness.bridge._guest.link.send

    async def counting_send(guest, frame, **kw):
        nonlocal sent
        if frame.type.startswith("SYNCPUT"):
            sent += 1
        return await real_send(guest, frame, **kw)

    harness.bridge._guest.link.send = counting_send

    seen = []
    watching = True

    async def other_work():
        nonlocal ran_after
        ran_after = sent
        # Until the pass is over, rather than a fixed number of turns. A fixed
        # count is a race: 200 sleep(0)s finish long before the pass reaches
        # the push, so every observation reads zero and the test fails for a
        # reason that has nothing to do with the property.
        while watching:
            seen.append(sent)
            await asyncio.sleep(0)

    async def guest():
        frames = await harness.recv_until(reader, {"SYNCPUTEND"}, 20)
        put = next(f for f in frames if f.type == "SYNCPUT")
        end = next(f for f in frames if f.type == "SYNCPUTEND")
        await harness.send(writer, Frame("SYNCPUTACK", {
            "id": put.headers["id"], "status": "ok",
            "crc": end.headers["crc"]}))

    try:
        task = asyncio.create_task(p.run())
        await fake_scan(harness, reader, writer, {})
        side = asyncio.create_task(other_work())
        await asyncio.wait_for(guest(), 20)
        await asyncio.wait_for(task, 20)
        watching = False
        await asyncio.wait_for(side, 5)
    finally:
        watching = False
        harness.bridge._guest.link.send = real_send

    assert sent > 2, "the test needs a multi-chunk push to mean anything"
    assert ran_after is not None and ran_after <= 1, (
        f"other work first ran after {ran_after} of {sent} sync frames; sync "
        f"is starving the event loop rather than sharing it")
    # The real property: a turn taken PART WAY THROUGH the file, not only
    # before it. Without the yield between chunks this set is {0, sent} -- the
    # loop runs to completion and everything else resumes afterwards.
    midway = [o for o in seen if 0 < o < sent]
    assert midway, (
        f"no turn was taken during the push; observed only {sorted(set(seen))} "
        f"of {sent} frames, so the chunk loop ran to completion uninterrupted")
    await harness.close(writer)


async def test_sync_stands_aside_while_the_user_has_a_prompt_open(
        harness, tmp_path):
    """The second half of the rule. A pass cut short is a consistent state --
    every path commits on its own ack -- so the honest thing is to stop and let
    the next pass finish, not to push a megabyte past someone's dialog."""
    reader, writer = await harness.connect()
    await _recv(harness, reader, "OPTIONSEND")
    p = a_pass(harness, tmp_path)
    for name in ("a.txt", "b.txt"):
        open(os.path.join(p.root, name), "wb").write(b"new\n")

    harness.bridge._guest.perm_pending = object()      # a dialog is on screen
    try:
        task = asyncio.create_task(p.run())
        await fake_scan(harness, reader, writer, {})
        got = await asyncio.wait_for(task, 10)
    finally:
        harness.bridge._guest.perm_pending = None

    assert got["pushed"] == 0
    assert any("permission prompt" in r for r in got["reports"])
    await harness.close(writer)


# --- unreadable paths, which used to go nowhere -----------------------------

async def test_an_unreadable_path_reaches_the_agent_once(harness, tmp_path):
    """`reports` is only read if the agent thinks to call sync_status.

    So a file the agent had just written and the machine could not open
    produced silence: the write succeeded here, never arrived there, and
    nothing said so. That is the exact failure sync_status exists to prevent,
    on the one path that never reached it.

    Once, not every pass. The machine reports `u` for a name it can enumerate
    but never open -- one carrying a byte with no form in its codepage -- and
    that condition never clears, so announcing it on a thirty-second timer
    would bury the conflicts that share the channel.
    """
    reader, writer = await harness.connect()
    await _recv(harness, reader, "OPTIONSEND")

    p = a_pass(harness, tmp_path)
    (pathlib.Path(p.root) / "held.c").write_bytes(b"int main(void){}\n")

    task = asyncio.create_task(p.run())
    req = await _recv(harness, reader, "SYNCSCAN")
    sid = req.headers["id"]
    body = manifest_line("held.c", b"", typ=b"u")
    await harness.send(writer, Frame("SYNCENT", {"id": sid, "n": "1"}, body))
    await harness.send(writer, Frame("SYNCEND", {
        "id": sid, "status": "ok", "files": "1", "tree_id": "VOL-1"}))
    got = await asyncio.wait_for(task, 10)

    assert any("held.c" in n for n in got["notices"]), got
    # And the reason must not blame a lock, which is one cause of `u` and the
    # only transient one. Naming it sends the reader after a process that may
    # not exist.
    assert any("cannot open it" in n for n in got["notices"]), got

    # Second pass, same condition: said once.
    #
    # The queue is emptied first because marking a notice DELIVERED happens in
    # run_sync, not in Pass.run, and this test drives Pass directly. Without
    # this the pass-1 notice is simply still pending and would be mistaken for
    # a second announcement -- which would make the test pass for the wrong
    # reason if first_report ever stopped working.
    p2 = a_pass(harness, tmp_path, root=p.root)
    p2.base.pending_notice.clear()
    task = asyncio.create_task(p2.run())
    req = await _recv(harness, reader, "SYNCSCAN")
    sid = req.headers["id"]
    await harness.send(writer, Frame("SYNCENT", {"id": sid, "n": "1"},
                                     manifest_line("held.c", b"", typ=b"u")))
    await harness.send(writer, Frame("SYNCEND", {
        "id": sid, "status": "ok", "files": "1", "tree_id": "VOL-1"}))
    second = await asyncio.wait_for(task, 10)
    assert not any("held.c" in n for n in second["notices"]), second
    await harness.close(writer)


async def test_an_ignored_path_is_never_announced(harness, tmp_path):
    """Most of a tree is ignored. Announcing that would bury everything else."""
    reader, writer = await harness.connect()
    await _recv(harness, reader, "OPTIONSEND")
    p = a_pass(harness, tmp_path)
    p.base.ignored["build/x.o"] = S.Entry(type=S.Type.FILE, crc=1)
    task = asyncio.create_task(p.run())
    await fake_scan(harness, reader, writer, {})
    got = await asyncio.wait_for(task, 10)
    assert not any("build/x.o" in n for n in got["notices"]), got
    await harness.close(writer)


async def test_continue_waits_for_the_pass_rather_than_racing_it(harness,
                                                                 tmp_path):
    """The user presses Continue while a pass is running.

    A Pass loads the baseline when it starts and writes the WHOLE object back
    when it finishes, and run_sync holds sync_lock across all of it. A release
    that did not take that lock was therefore read-modify-written underneath:
    the pass's save restored the latches a moment later, with no bar up to say
    so and no notice. The user answered and nothing happened.

    That is the likely case rather than the unlucky one -- the pass that
    raised the bar is usually the one still running when the user reacts.
    """
    reader, writer = await harness.connect()
    await _recv(harness, reader, "OPTIONSEND")
    guest = harness.bridge._guest

    p = a_pass(harness, tmp_path)
    harness.bridge.sync_dir = p.root
    harness.bridge.sync_state = os.path.dirname(p.state)
    p.base.latched["src/main.c"] = "both sides moved"
    p.base.save(p.state)

    # Stand in for a pass in flight: run_sync holds this for its whole body.
    await guest.sync_lock.acquire()
    task = asyncio.create_task(harness.bridge._release_all_latches())
    await asyncio.sleep(0.05)
    assert not task.done(), "the release did not wait for the pass"

    guest.sync_lock.release()
    assert await asyncio.wait_for(task, 5) == 1
    assert not harness.bridge._load_baseline().latched
    await harness.close(writer)


async def test_the_agents_resolve_waits_for_the_pass_too(harness, tmp_path):
    """Same race on the route the design tells the agent to use, which makes
    it the likelier of the two to be lost."""
    reader, writer = await harness.connect()
    await _recv(harness, reader, "OPTIONSEND")
    guest = harness.bridge._guest

    p = a_pass(harness, tmp_path)
    harness.bridge.sync_dir = p.root
    harness.bridge.sync_state = os.path.dirname(p.state)
    p.base.latched["src/main.c"] = "both sides moved"
    p.base.save(p.state)

    await guest.sync_lock.acquire()
    task = asyncio.create_task(
        harness.bridge.sync_resolve("src/main.c", "release"))
    await asyncio.sleep(0.05)
    assert not task.done(), "sync_resolve did not wait for the pass"

    guest.sync_lock.release()
    assert "Released" in await asyncio.wait_for(task, 5)
    assert not harness.bridge._load_baseline().latched
    await harness.close(writer)


async def test_a_standing_conflict_does_not_re_raise_the_halt(harness,
                                                              tmp_path):
    """A file on the guest where the bridge holds a directory cannot be
    applied -- a pull writes a file and cannot replace a directory without a
    delete primitive we do not have. Nothing advances, so every pass decides
    it again.

    Counting those towards the halt put the bar back up thirty seconds after
    the user answered it, and spawned a "resolve the sync conflict" turn whose
    preamble was EMPTY, because the notice had already been delivered. Then
    again a minute later. Paid turns, and the purest form of asking twice.

    Driven through run_sync, not through Pass.run: the halt is raised by the
    wiring, so a test that reads the counter straight off a Pass passes with
    the fix reverted. It did, when this was written that way.
    """
    reader, writer = await harness.connect()
    await _recv(harness, reader, "OPTIONSEND")
    root = str(tmp_path / "tree")
    os.makedirs(os.path.join(root, "notes"))
    harness.bridge.sync_dir = root
    harness.bridge.sync_state = str(tmp_path / "state")
    os.makedirs(harness.bridge.sync_state, exist_ok=True)
    _commit(harness, root)

    task = asyncio.create_task(harness.bridge.run_sync("test"))
    await fake_scan(harness, reader, writer, {"notes": b"a file now\n"})
    first = await asyncio.wait_for(task, 10)
    assert first["new_conflicts"] == 1
    assert (await _recv(harness, reader, "HALT")) is not None

    # Answer it, the way the user does.
    await harness.send(writer,
                       Frame("RESUME", {"id": harness.bridge.synccoord.halt_id}))
    await asyncio.sleep(0.1)

    # Same unresolvable conflict, next pass. No second bar.
    task = asyncio.create_task(harness.bridge.run_sync("test"))
    await fake_scan(harness, reader, writer, {"notes": b"a file now\n"})
    second = await asyncio.wait_for(task, 10)
    assert second["conflicts"] == 1, "it is still a conflict, and still true"
    assert second["new_conflicts"] == 0, "but it is not news, so no new bar"
    assert not harness.bridge.synccoord.halt_id, (
        "the bar went back up on a standing conflict")
    await harness.close(writer)


async def test_continue_does_not_answer_for_a_conflict_never_shown(harness,
                                                                   tmp_path):
    """_raise_halt is idempotent while a bar is up, so a conflict found in the
    meantime puts nothing on screen. Releasing everything on the press lifted
    the write refusal on a path the user had never seen and the agent had
    never been told about -- so the agent writes, and it reaches their machine.

    Driven through the RESUME frame for the same reason as above: the subset
    is chosen by the handler.
    """
    reader, writer = await harness.connect()
    await _recv(harness, reader, "OPTIONSEND")
    guest = harness.bridge._guest
    p = a_pass(harness, tmp_path)
    harness.bridge.sync_dir = p.root
    harness.bridge.sync_state = os.path.dirname(p.state)
    state = os.path.join(harness.bridge.sync_state, "base.json")

    base = harness.bridge._load_baseline()
    base.latched["seen.c"] = "both sides moved"
    base.save(state)
    await harness.bridge._raise_halt(guest, "Stopped: 1 conflicting edit.",
                                     paths=["seen.c"])
    await _recv(harness, reader, "HALT")

    # A second conflict lands while the bar is up. No new bar: there is one.
    base = harness.bridge._load_baseline()
    base.latched["unseen.c"] = "both sides moved"
    base.save(state)
    await harness.bridge._raise_halt(guest, "Stopped: 1 more.",
                                     paths=["unseen.c"])

    await harness.send(writer,
                       Frame("RESUME", {"id": harness.bridge.synccoord.halt_id}))
    await asyncio.sleep(0.15)

    left = harness.bridge._load_baseline().latched
    assert "unseen.c" in left, "released a path the user was never shown"
    assert "seen.c" not in left
    await harness.close(writer)


async def test_continue_is_acknowledged_rather_than_assumed(harness, tmp_path):
    """The guest no longer takes its own bar down on the click.

    rc_net_send returns -1 on a closed link, a full queue or an oversized
    frame. Hiding regardless meant a press that never left the machine looked
    answered: the bridge still held the halt, its raise is idempotent so no
    later bar could appear, and every latched path refused the agent's writes
    for good. So the bridge must answer RESUME with HALTEND, or the bar the
    guest is now keeping up would never come down.
    """
    reader, writer = await harness.connect()
    await _recv(harness, reader, "OPTIONSEND")
    guest = harness.bridge._guest
    harness.bridge.sync_dir = str(tmp_path / "tree")
    harness.bridge.sync_state = str(tmp_path / "state")
    os.makedirs(harness.bridge.sync_dir, exist_ok=True)
    os.makedirs(harness.bridge.sync_state, exist_ok=True)
    Baseline().save(os.path.join(harness.bridge.sync_state, "base.json"))

    await harness.bridge._raise_halt(guest, "Stopped: 1 conflicting edit.")
    halt = await _recv(harness, reader, "HALT")

    await harness.send(writer, Frame("RESUME", {"id": halt.headers["id"]}))
    end = await _recv(harness, reader, "HALTEND")
    assert end.headers["id"] == halt.headers["id"]
    assert not harness.bridge.synccoord.halt_id
    await harness.close(writer)


async def test_a_halt_survives_a_reconnect(harness, tmp_path):
    """The third leg beside PERMSTATE, and the one whose absence was worst.

    halt_id lives on the SyncCoordinator (Bridge-lifetime) and survives a
    reconnect; `latched` lives on disk. So a guest that restarted while a
    halt was up came back with NO BAR, a bridge still holding the halt, and
    every latched path refusing the agent's writes -- unanswerable, because
    RESUME is only accepted against a matching id and raising is idempotent
    while one is outstanding. Nothing short of restarting the bridge would
    recover it.
    """
    reader, writer = await harness.connect()
    await _recv(harness, reader, "OPTIONSEND")
    guest = harness.bridge._guest
    await harness.bridge._raise_halt(guest, "Stopped: 1 conflicting edit.")
    halt = await _recv(harness, reader, "HALT")
    await harness.close(writer)
    await asyncio.sleep(0.1)

    # The client comes back, as it does after a GPF or a cable pull.
    reader2, writer2 = await harness.connect()
    again = await _recv(harness, reader2, "HALT")
    assert again.headers["id"] == halt.headers["id"], "a NEW halt id would " \
        "never match the RESUME the user is about to send"
    assert again.body.decode("cp1252") == "Stopped: 1 conflicting edit."

    # And it is answerable, which is the whole point.
    await harness.send(writer2, Frame("RESUME", {"id": again.headers["id"]}))
    await _recv(harness, reader2, "HALTEND")
    assert not harness.bridge.synccoord.halt_id
    await harness.close(writer2)


async def test_a_bridge_restart_with_latched_paths_raises_the_bar(
        harness, tmp_path):
    """02.21: the same hole as test_a_halt_survives_a_reconnect, but across a
    BRIDGE restart rather than a guest reconnect.

    A guest reconnect keeps the same Bridge (and its SyncCoordinator), so
    synccoord.halt_id survives it and _reissue_halt puts the bar back. A
    bridge restart does not: the new process's SyncCoordinator starts with
    halt_id empty, while base.latched -- written by the OLD process -- is
    still sitting on disk. Before 02.21 nothing at startup or attach ever
    read it, so the agent's writes stayed refused with no bar on the user's
    screen to explain why or let them clear it, and the only way out was
    the agent noticing on its own via sync_status.

    Here the connecting guest IS the first one this Bridge object has ever
    seen -- `harness.bridge.synccoord` has never had a chance to observe
    this pass's `base.latched`, the same as a freshly constructed one would
    not have -- which is exactly what "a fresh process" looks like from
    _attach's side.
    """
    harness.bridge.sync_dir = str(tmp_path / "tree")
    harness.bridge.sync_state = str(tmp_path / "state")
    os.makedirs(harness.bridge.sync_dir, exist_ok=True)
    os.makedirs(harness.bridge.sync_state, exist_ok=True)
    base = Baseline()
    base.latched = {"a.txt": "conflicting edit"}
    base.save(os.path.join(harness.bridge.sync_state, "base.json"))
    assert harness.bridge._guest is None, "test setup: no prior connection"

    reader, writer = await harness.connect()
    await _recv(harness, reader, "OPTIONSEND")
    halt = await _recv(harness, reader, "HALT")

    assert halt.type == "HALT", (
        "no HALT arrived at all -- the restart left a latched path with no "
        f"bar (got {halt.type} instead, after recv_until's timeout)")
    assert halt.headers["id"]
    assert harness.bridge.synccoord.halt_id == halt.headers["id"]
    await harness.close(writer)


async def test_reconcile_halt_clears_a_bar_nothing_is_latched_for(
        harness, tmp_path):
    """The other direction: a bar up with base.latched already empty (the
    agent released the last path through some route that did not itself
    clear synccoord.halt_id) must come down, not stay stuck."""
    harness.bridge.sync_dir = str(tmp_path / "tree")
    harness.bridge.sync_state = str(tmp_path / "state")
    os.makedirs(harness.bridge.sync_dir, exist_ok=True)
    os.makedirs(harness.bridge.sync_state, exist_ok=True)
    Baseline().save(os.path.join(harness.bridge.sync_state, "base.json"))

    reader, writer = await harness.connect()
    await _recv(harness, reader, "OPTIONSEND")
    guest = harness.bridge._guest
    harness.bridge.synccoord.halt_id = "h-stuck"

    await harness.bridge._reconcile_halt(guest)
    assert harness.bridge.synccoord.halt_id == "", (
        "a bar with nothing latched must come down")
    await harness.close(writer)


async def test_a_refused_pass_is_announced_once_not_never(harness, tmp_path):
    """A tree_id mismatch stops the WHOLE layer -- no file moves in either
    direction -- and it used to be a log warning and nothing else. Sync could
    be dead for an hour with the agent still reporting files as delivered and
    the user seeing no reason to doubt it.

    Once, though. A refusal that aborts every pass would otherwise announce
    itself twice a minute, which is the bug first_report exists to stop for a
    single path. first_report cannot be reused here: an abort has no path key
    and raises before the pass saves anything.
    """
    reader, writer = await harness.connect()
    await _recv(harness, reader, "OPTIONSEND")
    harness.bridge.sync_dir = str(tmp_path / "tree")
    harness.bridge.sync_state = str(tmp_path / "state")
    os.makedirs(harness.bridge.sync_dir, exist_ok=True)
    os.makedirs(harness.bridge.sync_state, exist_ok=True)
    _commit(harness, harness.bridge.sync_dir)
    base = Baseline()
    base.tree_id = "VOL-ORIGINAL"
    base.save(os.path.join(harness.bridge.sync_state, "base.json"))

    task = asyncio.create_task(harness.bridge.run_sync("test"))
    await fake_scan(harness, reader, writer, {}, tree_id="VOL-OTHER")
    got = await asyncio.wait_for(task, 10)
    assert "error" in got

    # The user is told, on their own screen. The block is deflated on the
    # wire, so decompress rather than assert on ciphertext.
    said = await _recv(harness, reader, "TEXT")
    body = said.body
    if said.headers.get("enc") == "deflate":
        body = zlib.decompressobj().decompress(body)
    assert b"stopped" in body.lower(), body[:200]
    # And the agent, which would otherwise keep reporting files as delivered.
    assert any("SYNC HAS STOPPED" in n
               for n in harness.bridge.synccoord.agent_notices)
    assert harness.bridge._load_baseline().aborted

    # sync_status leads with it, because it makes everything else stale.
    status = harness.bridge.sync_status_text()
    assert "SYNCHRONISATION IS STOPPED" in status

    # Second pass, same refusal: silent.
    harness.bridge.synccoord.agent_notices.clear()
    task = asyncio.create_task(harness.bridge.run_sync("test"))
    await fake_scan(harness, reader, writer, {}, tree_id="VOL-OTHER")
    await asyncio.wait_for(task, 10)
    assert not harness.bridge.synccoord.agent_notices, (
        "announced the same abort twice")
    await harness.close(writer)


async def test_recovery_is_announced_too(harness, tmp_path):
    """"Sync has stopped" with no matching "sync is working again" leaves the
    user believing it is still down."""
    reader, writer = await harness.connect()
    await _recv(harness, reader, "OPTIONSEND")
    harness.bridge.sync_dir = str(tmp_path / "tree")
    harness.bridge.sync_state = str(tmp_path / "state")
    os.makedirs(harness.bridge.sync_dir, exist_ok=True)
    os.makedirs(harness.bridge.sync_state, exist_ok=True)
    _commit(harness, harness.bridge.sync_dir)
    base = Baseline()
    base.aborted = "something stopped it earlier"
    base.save(os.path.join(harness.bridge.sync_state, "base.json"))

    task = asyncio.create_task(harness.bridge.run_sync("test"))
    await fake_scan(harness, reader, writer, {})
    got = await asyncio.wait_for(task, 10)
    assert got.get("recovered") == "something stopped it earlier"
    assert not harness.bridge._load_baseline().aborted
    assert any("working again" in n for n in got["notices"]), got["notices"]
    await harness.close(writer)


async def test_two_roots_sharing_a_long_prefix_are_not_one_tree(harness,
                                                                tmp_path):
    """tree_id is "%08lX-%s" % (volume serial, root) built into 64 bytes on
    the guest, so only the first 54 characters of the root survive. Two roots
    on one volume sharing that prefix produce the SAME id, and the mismatch
    guard -- the thing standing between a baseline and the wrong tree -- passes.

    Fixed by comparing `root=`, which has always been on the wire in full and
    was never compared to anything. Not by lengthening the guest's buffer:
    that changes the id for every existing baseline with a long root, which
    would then refuse every pass with no way to say "this is the same tree".
    """
    reader, writer = await harness.connect()
    await _recv(harness, reader, "OPTIONSEND")
    p = a_pass(harness, tmp_path)

    long_a = "C:\\" + "W" * 51 + "\\ALPHA"
    long_b = "C:\\" + "W" * 51 + "\\BETA"
    same_id = "1A2B3C4D-" + (long_a[:54])
    assert same_id == "1A2B3C4D-" + long_b[:54], "the premise: ids collide"

    task = asyncio.create_task(p.scan_guest())
    req = await _recv(harness, reader, "SYNCSCAN")
    await harness.send(writer, Frame("SYNCEND", {
        "id": req.headers["id"], "status": "ok", "files": "0",
        "tree_id": same_id, "root": long_a}))
    await asyncio.wait_for(task, 5)
    p.base.save(p.state)

    # A different root, an identical tree_id. The old guard saw nothing.
    p2 = a_pass(harness, tmp_path, root=p.root)
    task = asyncio.create_task(p2.scan_guest())
    req = await _recv(harness, reader, "SYNCSCAN")
    await harness.send(writer, Frame("SYNCEND", {
        "id": req.headers["id"], "status": "ok", "files": "0",
        "tree_id": same_id, "root": long_b}))
    with pytest.raises(SyncError, match="relative to the root"):
        await asyncio.wait_for(task, 5)
    await harness.close(writer)


async def test_the_same_root_is_not_refused(harness, tmp_path):
    """The guard must not fire on the ordinary case, or sync stops for good."""
    reader, writer = await harness.connect()
    await _recv(harness, reader, "OPTIONSEND")
    p = a_pass(harness, tmp_path)
    task = asyncio.create_task(p.scan_guest())
    await fake_scan(harness, reader, writer, {})
    await asyncio.wait_for(task, 5)
    assert p.base.guest_root == "C:\\WORK"
    p.base.save(p.state)

    p2 = a_pass(harness, tmp_path, root=p.root)
    task = asyncio.create_task(p2.scan_guest())
    await fake_scan(harness, reader, writer, {})
    await asyncio.wait_for(task, 5)
    await harness.close(writer)


async def test_two_turns_never_run_at_once(harness, tmp_path):
    """The SDK stream has ONE consumer. Two iterators get a disjoint split of
    messages -- one turn consumes the other's ResultMessage and returns with
    its cost while the other's text lands in the wrong callbacks -- so the
    transcript itself is corrupted, not merely the prompt bar.

    Reachable through the DESIGNED conflict flow, not a race: Continue is not
    gated on g_turn_active, and a sync pass raises the halt while the agent
    works because the agent writing files is what moves the tree.
    """
    reader, writer = await harness.connect()
    await _recv(harness, reader, "OPTIONSEND")
    guest = harness.bridge._guest

    live = asyncio.Event()
    order = []

    async def slow(text, **kw):
        from bridge.agent.session import Turn
        order.append(("start", text))
        await live.wait()
        order.append(("end", text))
        return Turn(ok=True, subtype="success", cost_usd=0.0,
                    num_turns=1, duration_ms=1)

    guest.claude.send = slow
    first = asyncio.create_task(harness.bridge._run_turn(guest, "one"))
    await asyncio.sleep(0.05)
    second = asyncio.create_task(harness.bridge._run_turn(guest, "two"))
    await asyncio.sleep(0.05)

    assert [o for o in order if o[0] == "start"] == [("start", "one")], \
        "the second turn started while the first was still running"
    live.set()
    await asyncio.wait_for(asyncio.gather(first, second), 5)
    assert order[0] == ("start", "one") and order[1] == ("end", "one")
    await harness.close(writer)


async def test_continue_unlatches_only_once_the_wire_is_free(harness, tmp_path):
    """M1: the release must travel with the turn that explains it.

    It ran synchronously in the RESUME handler with the informing turn spawned
    after, so a turn already in flight -- which had been told nothing, that
    being why the latch exists -- found the path no longer refusing its writes
    and wrote. Queueing the turn makes that window arbitrarily long, so the two
    have to move together.
    """
    reader, writer = await harness.connect()
    await _recv(harness, reader, "OPTIONSEND")
    guest = harness.bridge._guest
    harness.bridge.sync_dir = str(tmp_path / "tree")
    harness.bridge.sync_state = str(tmp_path / "state")
    os.makedirs(harness.bridge.sync_dir, exist_ok=True)
    os.makedirs(harness.bridge.sync_state, exist_ok=True)
    base = Baseline()
    base.latched["src/main.c"] = "both sides moved"
    base.save(os.path.join(harness.bridge.sync_state, "base.json"))

    await harness.bridge._raise_halt(guest, "Stopped: 1 conflicting edit.",
                                     paths=["src/main.c"])
    await _recv(harness, reader, "HALT")

    # A turn is already running and has heard nothing about this conflict.
    live = asyncio.Event()

    async def slow(text, **kw):
        from bridge.agent.session import Turn
        await live.wait()
        return Turn(ok=True, subtype="success", cost_usd=0.0,
                    num_turns=1, duration_ms=1)

    guest.claude.send = slow
    running = asyncio.create_task(harness.bridge._run_turn(guest, "in flight"))
    await asyncio.sleep(0.05)

    await harness.send(writer,
                       Frame("RESUME", {"id": harness.bridge.synccoord.halt_id}))
    await _recv(harness, reader, "HALTEND")
    await asyncio.sleep(0.1)

    assert harness.bridge._load_baseline().latched, (
        "unlatched while a turn that was never told about it is still running")

    live.set()
    await asyncio.wait_for(running, 5)
    await asyncio.sleep(0.2)
    assert not harness.bridge._load_baseline().latched, (
        "never unlatched once the wire was free")
    await harness.close(writer)


async def test_a_reconnecting_guest_reconciles(harness, tmp_path):
    """Nothing synced on attach.

    run_sync had exactly ONE production caller -- the SYNCDIRTY handler --
    although its docstring named three triggers. So the whole 30-second
    cadence the say-it-once machinery is designed around lived on the GUEST,
    in win/rpc.c, and a machine edited while it was disconnected reconciled
    only when its watcher next happened to fire.
    """
    harness.bridge.sync_dir = str(tmp_path / "tree")
    harness.bridge.sync_state = str(tmp_path / "state")
    os.makedirs(harness.bridge.sync_dir, exist_ok=True)
    os.makedirs(harness.bridge.sync_state, exist_ok=True)

    reader, writer = await harness.connect()
    await _recv(harness, reader, "OPTIONSEND")
    # A folder this conversation has committed to, because that is what makes
    # there be a tree to reconcile at all. A chat that never called a tool
    # mirrors nothing, and attaching one correctly does nothing.
    _commit(harness, harness.bridge.sync_dir)
    await harness.close(writer)

    reader, writer = await harness.connect()
    # The scan is asked for without the guest having reported anything.
    req = await _recv(harness, reader, "SYNCSCAN")
    assert req.type == "SYNCSCAN", "attaching did not reconcile"
    await harness.send(writer, Frame("SYNCEND", {
        "id": req.headers["id"], "status": "ok", "files": "0",
        "tree_id": "VOL-1", "root": "C:\\WORK"}))
    await harness.close(writer)


async def test_the_bridge_has_its_own_sweep(harness, tmp_path):
    """The backstop: if the guest's watcher thread dies, or a guest is built
    without one, nothing on this side would ever have noticed."""
    harness.bridge.sync_dir = str(tmp_path / "tree")
    harness.bridge.sync_state = str(tmp_path / "state")
    os.makedirs(harness.bridge.sync_dir, exist_ok=True)
    os.makedirs(harness.bridge.sync_state, exist_ok=True)
    harness.bridge.sync_sweep_s = 0.05

    reader, writer = await harness.connect()
    await _recv(harness, reader, "OPTIONSEND")
    # No attach pass to answer: a fresh conversation has committed to no
    # folder, so nothing is mirrored and attaching has nothing to reconcile.
    # The sweep is the only pass here, which is what this is about.
    _commit(harness, harness.bridge.sync_dir)

    req = await _recv(harness, reader, "SYNCSCAN")    # the sweep one
    assert req.type == "SYNCSCAN", "the bridge never swept on its own"
    await harness.close(writer)


async def test_a_sync_frame_for_the_wrong_kind_of_request_is_not_fatal(
        harness, tmp_path):
    """sync_pending held four different ad-hoc dict shapes in one id
    namespace -- a scan carried rows and display, a put carried neither --
    and _sync_frame indexed entry["rows"] on any SYNCENT.

    So a SYNCENT bearing a PUT's id raised KeyError, which nothing catches
    between there and the read loop, and the whole connection went down. A
    guest would have to be confused to send that; the point is that the
    bridge's answer to a confused guest should be a log line rather than a
    dropped link.

    SYNCDAT had the same shape through setdefault, which would silently
    attach a byte buffer to a scan.
    """
    reader, writer = await harness.connect()
    await _recv(harness, reader, "OPTIONSEND")
    p = a_pass(harness, tmp_path)
    (pathlib.Path(p.root) / "a.txt").write_bytes(b"hello\n")

    task = asyncio.create_task(p.push("a.txt", None))
    put = await _recv(harness, reader, "SYNCPUT")
    await _recv(harness, reader, "SYNCPUTEND")
    pid = put.headers["id"]

    # Manifest rows and file bytes, both against the PUT's id.
    await harness.send(writer, Frame("SYNCENT", {"id": pid, "n": "1"},
                                     manifest_line("x.c", b"int x;\n")))
    await harness.send(writer, Frame("SYNCDAT", {"id": pid, "total": "3",
                                                 "off": "0"}, b"abc"))
    await asyncio.sleep(0.05)

    # The link is still up and the put still completes normally.
    await harness.send(writer, Frame("SYNCPUTACK", {
        "id": pid, "status": "ok", "crc": "0"}))
    await asyncio.wait_for(task, 5)
    await harness.close(writer)


async def test_a_busy_refusal_answers_in_the_shape_the_caller_expects(
        harness, tmp_path):
    """The guest's reject_sync_busy answered EVERY full-queue sync frame with
    SYNCPUTACK, whatever had arrived.

    The bridge routes an ack by id, so a SYNCPUTACK against a SCAN reaches
    the right future carrying the wrong SHAPE -- two values where the
    awaiting code unpacks six. It surfaced as "sync pass aborted: not enough
    values to unpack", which names neither the frame nor the full queue.

    The busy path needs the worker's sync FIFO full, so it is rare -- which
    is the argument for getting it right rather than against it. A rare path
    that reports the wrong thing costs an investigation, not a retry.
    """
    reader, writer = await harness.connect()
    await _recv(harness, reader, "OPTIONSEND")
    p = a_pass(harness, tmp_path)

    task = asyncio.create_task(p.scan_guest())
    req = await _recv(harness, reader, "SYNCSCAN")
    # What the guest now sends when its queue is full: SYNCEND, not
    # SYNCPUTACK. The right shape, carrying a status the pass understands.
    await harness.send(writer, Frame("SYNCEND", {
        "id": req.headers["id"], "status": "busy", "files": "0"}))

    with pytest.raises(SyncError) as e:
        await asyncio.wait_for(task, 5)
    assert "busy" in str(e.value), (
        f"the refusal should name the reason, got {e.value}")
    assert "unpack" not in str(e.value), (
        "a shape mismatch reported instead of the actual refusal")
    await harness.close(writer)


async def test_two_alternating_abort_reasons_are_each_said_once(harness,
                                                                tmp_path):
    """The once-only guard compared against the LAST reason, which suppresses
    a repeat and nothing else.

    Two alternating reasons defeat it completely -- and that is not
    hypothetical: two guests sharing one --sync-state each refuse the other's
    tree_id, so the reasons alternate every pass and both users get
    "synchronisation has stopped" twice a minute, forever. The notice channel
    that also carries conflicts becomes noise.
    """
    reader, writer = await harness.connect()
    await _recv(harness, reader, "OPTIONSEND")
    guest = harness.bridge._guest
    harness.bridge.sync_dir = str(tmp_path / "tree")
    harness.bridge.sync_state = str(tmp_path / "state")
    os.makedirs(harness.bridge.sync_dir, exist_ok=True)
    os.makedirs(harness.bridge.sync_state, exist_ok=True)
    Baseline().save(os.path.join(harness.bridge.sync_state, "base.json"))

    said = []
    for reason in ("machine A is not machine B", "machine B is not machine A",
                   "machine A is not machine B", "machine B is not machine A"):
        harness.bridge.synccoord.agent_notices.clear()
        await harness.bridge._report_abort(guest, reason)
        said.append(bool(harness.bridge.synccoord.agent_notices))

    assert said == [True, True, False, False], (
        f"each distinct reason once, then silence -- got {said}")

    # And a pass that works clears the slate, so a later failure is news.
    base = harness.bridge._load_baseline()
    base.announced = []
    base.aborted = ""
    base.save(os.path.join(harness.bridge.sync_state, "base.json"))
    harness.bridge.synccoord.agent_notices.clear()
    await harness.bridge._report_abort(guest, "machine A is not machine B")
    assert harness.bridge.synccoord.agent_notices, (
        "recovery did not reset the slate")
    await harness.close(writer)


# --- the aggregate-revert refusal -------------------------------------------
#
# Every row it counts is individually correct, which is what makes the case
# hard and what makes it worth a refusal: applying fifty correct rows produces
# one wrong outcome.

def _commit(harness, local_root, guest_root="C:\\WORK"):
    """Give the open conversation a work folder.

    Nothing is mirrored until one exists -- a chat that is only being talked
    to shares no files, and `run_sync` returns immediately without one. In
    production the first TOOL CALL does this; a sync test has no tool, so it
    says the same thing directly.
    """
    harness.bridge._guest.work_dir = local_root
    harness.bridge._guest.work_guest = guest_root


def _revert_pass(rows, decisions, accept_tree=False):
    class FakePass(Pass):
        def __init__(self):
            self.base = Baseline(rows=rows)
            self.base.accept_tree = accept_tree
    p = FakePass()
    Pass._refuse_aggregate_revert(p, decisions)
    return p


def _dec(n, reason):
    return [S.Decision(f"f{i}.c", S.Act.CONFLICT, reason) for i in range(n)]


def _rows(n):
    return {f"f{i}.c": Entry(type=Type.FILE, cls="t", crc=i) for i in range(n)}


def test_a_restored_backup_refuses_the_whole_pass():
    """Twenty of forty paths back at the content a push replaced is not twenty
    edits, it is one event -- and the per-row handling would work through it
    faithfully, pulling old contents over work already done."""
    with pytest.raises(SyncError) as exc:
        _revert_pass(_rows(40), _dec(20, S.REVERTED_PUSH))
    assert "went backwards in one pass" in str(exc.value)
    assert "Nothing has been changed on either side" in str(exc.value)
    assert "accept-tree" in str(exc.value), "a refusal with no way out is a trap"


def test_paths_gone_from_the_machine_count_towards_it_too():
    """A restored backup predates files created after it, so they read as
    deletions -- the other half of the same event, and counting only reverts
    would miss a backup restored onto a tree that had mostly grown. They COUNT,
    but they cannot fire it on their own; see the test below."""
    decs = _dec(19, S.GONE_ON_GUEST) + [
        S.Decision("z.c", S.Act.CONFLICT, S.REVERTED_PUSH)]
    with pytest.raises(SyncError):
        _revert_pass(_rows(40), decs)


def test_deleting_files_on_your_own_machine_never_refuses_the_pass():
    """THE TRAP THIS AVOIDS, and it is worse than the bug it would guard.

    `REPORT` does not advance the baseline -- it counts and says so -- so a
    path that vanished is re-decided identically on every pass, forever. If
    absence alone could fire this, clearing a build directory would refuse the
    pass, then refuse it again thirty seconds later, with `accept-tree` buying
    exactly one pass each time. A guard that must be answered forever teaches
    the answer reflexively, and the one time it matters it gets the same
    reflex.

    Vanishing alone is deletion: already handled, already harmless, nothing
    applied and nothing lost. A RESTORED tree is distinguished by content
    coming BACK to an older state, so at least one of those must be present."""
    _revert_pass(_rows(40), _dec(30, S.GONE_ON_GUEST))


def test_an_ordinary_afternoon_is_not_a_restored_backup():
    """Four reverts is someone undoing their own work. The floor exists so a
    small tree, where one revert is already a large share, is never refused."""
    _revert_pass(_rows(40), _dec(4, S.REVERTED_PUSH))


def test_a_few_reverts_in_a_large_tree_pass():
    """Twelve reverts in three thousand tracked paths is work, not an event.
    The share exists so a flat count cannot stop a big tree."""
    _revert_pass(_rows(3000), _dec(12, S.REVERTED_PUSH))


def test_one_revert_in_a_tiny_tree_passes():
    """The case a bare percentage gets wrong: one of three is 33%."""
    _revert_pass(_rows(3), _dec(1, S.REVERTED_PUSH))


def test_a_first_sync_has_nothing_to_regress_from():
    _revert_pass({}, _dec(50, S.REVERTED_PUSH))


def test_accept_tree_lets_exactly_one_pass_through():
    """And then re-arms itself. A flag that stayed set would disarm the guard
    for every pass afterwards, which is the failure mode of every 'just this
    once' switch that is never cleared."""
    p = _revert_pass(_rows(40), _dec(20, S.REVERTED_PUSH), accept_tree=True)
    assert p.base.accept_tree is False

    with pytest.raises(SyncError):
        _revert_pass(_rows(40), _dec(20, S.REVERTED_PUSH),
                     accept_tree=p.base.accept_tree)


def test_the_accept_tree_flag_survives_a_baseline_round_trip(tmp_path):
    """It is set by the agent in one process and read by a pass in another, so
    a field that is written but not loaded would silently never work."""
    state = str(tmp_path / "base.json")
    b = Baseline(rows=_rows(2))
    b.accept_tree = True
    b.save(state)
    assert Baseline.load(state).accept_tree is True
