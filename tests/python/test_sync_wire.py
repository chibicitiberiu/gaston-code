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

from bridge import ignores as I
from bridge import sync as S
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

    p = FakePass(root)
    rows = Pass.scan_bridge(p)
    assert "link.txt" not in rows


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
    assert any("STOP editing" in n for n in got["notices"])

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
    p2 = Pass(harness.bridge, harness.bridge._guest, p.root,
              os.path.dirname(p.state))
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
    one = p.stage_conflict("a.txt", "a.txt")
    open(os.path.join(p.root, "a.txt"), "wb").write(b"second\n")
    two = p.stage_conflict("a.txt", "a.txt")
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


async def test_the_baseline_survives_a_restart_mid_pass(harness, tmp_path):
    """The record distinguishing 'here but not there' from 'deleted there' must
    not be destroyed by one badly timed interruption."""
    p = a_pass(harness, tmp_path)
    p.base.rows["a.txt"] = Entry(type=Type.FILE, crc=7, display="a.txt")
    p.base.tree_id = "VOL-1"
    p.base.save(p.state)

    fresh = Pass(harness.bridge, harness.bridge._guest, p.root,
                 os.path.dirname(p.state))
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
