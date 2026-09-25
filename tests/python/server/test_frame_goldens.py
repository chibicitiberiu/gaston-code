"""Frame-trace goldens, bridge side.

Records and verifies the exact outbound frame sequences across 12 scenarios:
1. Connect and handshake through to READY and OPTIONSEND
2. INPUT -> streamed TEXT -> tool call -> PERMREQ -> PERMREPLY -> EXEC -> EXECRESULT -> TURN end
3. A sync conflict raising HALT, then RESUME
4. A reconnect mid-turn (_reissue_turn)
5. A reconnect while halted (_reissue_halt)
6. A page request and a seal
7. A conversation switch, settings saved and restored
8. An attachment staged and sent (ATTGET path included)
9. A second client rejected with ERROR busy while the first stays live
10. A ghost guest reaped on idle timeout, then reconnecting without being refused as busy
11. A turn that hits an SDK error and aborts (STATUS error and coalescer flush ordering)
12. A disconnect mid-turn, then reconnect (turn reissued and completed cleanly)

The frame trace is the contract:
- Characterisation records it.
- Structural refactoring must not change it. A diff is a bug.
- Behavioural changes alter it deliberately, with the diff as the review artefact.
"""

import asyncio
import os
import time
import zlib
from claude_agent_sdk import PermissionResultAllow
from bridge.agent.session import Turn
from bridge.wire.protocol import Frame, hello
from bridge.view import html as H
from bridge.files.sync import Baseline
from harness import harness, FakeCtx  # noqa: F401
from tests.python.server.golden_utils import (
    FRAME_GOLDENS_DIR,
    assert_matches_golden,
    serialize_frame_sequence,
)


class FrameCollector:
    """Collects frames from a reader, keeping track of order and decompressor."""

    def __init__(self, reader, harness_inst):
        self.reader = reader
        self.harness = harness_inst
        self.frames = []
        self.unzip = harness_inst.unzip

    async def recv_until(self, types, timeout=5.0):
        got = await self.harness.recv_until(self.reader, types, timeout=timeout)
        self.frames.extend(got)
        return got

    def serialized(self):
        return serialize_frame_sequence(self.frames, self.unzip)


async def test_golden_01_handshake(harness):
    """Scenario 1: Connect and handshake through to READY and OPTIONSEND."""
    r, w = await harness.connect()
    collector = FrameCollector(r, harness)
    await collector.recv_until({"OPTIONSEND"})
    golden_file = os.path.join(FRAME_GOLDENS_DIR, "scenario_01_handshake.json")
    assert_matches_golden(golden_file, collector.serialized())
    await harness.close(w)


async def test_golden_02_full_turn_with_perm_and_exec(harness):
    """Scenario 2: INPUT -> streamed TEXT -> tool call -> PERMREQ -> PERMREPLY -> EXEC -> EXECRESULT -> TURN end."""
    r, w = await harness.connect()
    await harness.recv_until(r, {"OPTIONSEND"})

    claude = harness.bridge._guest.claude
    orig_send = claude.send

    async def turn_with_tool(prompt):
        await claude.start_turn()
        if claude.on_text:
            await claude.on_text("Executing command...\n")
        if claude.on_tool:
            await claude.on_tool("Bash", {"command": "vol c:"})
        ctx = FakeCtx(
            tool_use_id="toolu_test_1", display_name="Bash", description="vol c:"
        )
        perm = await claude.can_use_tool("Bash", {"command": "vol c:"}, ctx)
        if isinstance(perm, PermissionResultAllow):
            res = await harness.bridge.run_on_guest(
                "vol c:",
                timeout_ms=5000,
                shell="",
                cwd="",
                guest=harness.bridge._guest,
            )
            if claude.on_text:
                await claude.on_text(f"Output: {res['content'][0]['text']}\n")
        return Turn(
            ok=True, subtype="success", cost_usd=0.01, num_turns=1, duration_ms=10
        )

    claude.send = turn_with_tool

    collector = FrameCollector(r, harness)

    # 1. Send INPUT
    await harness.send(w, Frame("INPUT", {}, b"Run vol c:"))

    # 2. Wait for PERMREQ
    await collector.recv_until({"PERMREQ"})

    # 3. Send PERMREPLY allow
    await harness.send(
        w, Frame("PERMREPLY", {"id": "toolu_test_1", "action": "allow"})
    )

    # 4. Wait for EXEC frame
    await collector.recv_until({"EXEC"})
    f_exec = next(f for f in collector.frames if f.type == "EXEC")
    exec_id = f_exec.headers["id"]

    # 5. Send EXECOUT and EXECRESULT
    await harness.send(w, Frame("EXECOUT", {"id": exec_id}, b"Volume in drive C is DOS\n"))
    await harness.send(
        w, Frame("EXECRESULT", {"id": exec_id, "status": "ok", "exit": "0"})
    )

    # 6. Wait for TURN end
    while not any(f.type == "TURN" and f.headers.get("state") == "end" for f in collector.frames):
        await collector.recv_until({"TURN"})

    golden_file = os.path.join(
        FRAME_GOLDENS_DIR, "scenario_02_full_turn_with_perm_and_exec.json"
    )
    assert_matches_golden(golden_file, collector.serialized())
    await harness.close(w)


async def test_golden_03_sync_conflict_halt_and_resume(harness, tmp_path):
    """Scenario 3: A sync conflict raising HALT, then RESUME."""
    r, w = await harness.connect()
    await harness.recv_until(r, {"OPTIONSEND"})

    harness.bridge.sync_dir = str(tmp_path / "tree")
    harness.bridge.sync_state = str(tmp_path / "state")
    os.makedirs(harness.bridge.sync_dir, exist_ok=True)
    os.makedirs(harness.bridge.sync_state, exist_ok=True)
    Baseline().save(os.path.join(harness.bridge.sync_state, "base.json"))

    collector = FrameCollector(r, harness)

    # 1. Raise HALT
    await harness.bridge.sync.raise_halt(
        harness.bridge._guest, "Stopped: 1 conflicting edit."
    )
    await collector.recv_until({"HALT"})
    halt = next(f for f in collector.frames if f.type == "HALT")

    # 2. Send RESUME
    await harness.send(w, Frame("RESUME", {"id": halt.headers["id"]}))

    # 3. Wait for HALTEND and the spawned turn
    await collector.recv_until({"TURN"})

    golden_file = os.path.join(
        FRAME_GOLDENS_DIR, "scenario_03_sync_conflict_halt_and_resume.json"
    )
    assert_matches_golden(golden_file, collector.serialized())
    await harness.close(w)


async def test_golden_04_reconnect_mid_turn(harness):
    """Scenario 4: A reconnect mid-turn (_reissue_turn)."""
    r1, w1 = await harness.connect()
    await harness.recv_until(r1, {"OPTIONSEND"})
    sess_id = harness.bridge._guest.sess_id

    turn_barrier = asyncio.Event()

    claude = harness.bridge._guest.claude

    async def slow_send(prompt):
        claude.busy = True
        try:
            await claude.start_turn()
            if claude.on_text:
                await claude.on_text("Starting before reconnect...\n")
            await turn_barrier.wait()
            if claude.on_text:
                await claude.on_text("Finished after reconnect.\n")
            return Turn(
                ok=True, subtype="success", cost_usd=0.01, num_turns=1, duration_ms=10
            )
        finally:
            claude.busy = False

    claude.send = slow_send

    # Start turn
    await harness.send(w1, Frame("INPUT", {}, b"Slow turn"))
    await harness.recv_until(r1, {"TEXT"})

    # Drop connection 1
    await harness.close(w1)

    # Reconnect as same guest session
    r2, w2 = await harness.connect(sess=sess_id)
    collector = FrameCollector(r2, harness)

    # Receive handshake and reissued turn frame
    await collector.recv_until({"TURN"})
    assert any(
        f.type == "TURN" and f.headers.get("state") == "start"
        for f in collector.frames
    )

    # Release turn barrier and let turn finish
    turn_barrier.set()
    while not any(f.type == "TURN" and f.headers.get("state") == "end" for f in collector.frames):
        await collector.recv_until({"TURN"})

    golden_file = os.path.join(
        FRAME_GOLDENS_DIR, "scenario_04_reconnect_mid_turn.json"
    )
    assert_matches_golden(golden_file, collector.serialized())
    await harness.close(w2)


async def test_golden_05_reconnect_while_halted(harness, tmp_path):
    """Scenario 5: A reconnect while halted (_reissue_halt)."""
    r1, w1 = await harness.connect()
    await harness.recv_until(r1, {"OPTIONSEND"})
    sess_id = harness.bridge._guest.sess_id

    harness.bridge.sync_dir = str(tmp_path / "tree")
    harness.bridge.sync_state = str(tmp_path / "state")
    os.makedirs(harness.bridge.sync_dir, exist_ok=True)
    os.makedirs(harness.bridge.sync_state, exist_ok=True)
    Baseline().save(os.path.join(harness.bridge.sync_state, "base.json"))

    await harness.bridge.sync.raise_halt(
        harness.bridge._guest, "Stopped: 1 conflicting edit."
    )
    await harness.recv_until(r1, {"HALT"})

    # Drop connection 1
    await harness.close(w1)

    # Reconnect with same session handle
    r2, w2 = await harness.connect(sess=sess_id)
    collector = FrameCollector(r2, harness)

    # Reissue halt must arrive after handshake
    await collector.recv_until({"HALT"})

    golden_file = os.path.join(
        FRAME_GOLDENS_DIR, "scenario_05_reconnect_while_halted.json"
    )
    assert_matches_golden(golden_file, collector.serialized())
    await harness.close(w2)


async def test_golden_06_page_request_and_seal(harness):
    """Scenario 6: A page request and a seal."""
    r, w = await harness.connect()
    await harness.recv_until(r, {"OPTIONSEND"})

    # Fill several pages directly into transcript
    guest = harness.bridge._guest
    for i in range(600):
        guest.pages.add("plain", f"line {i:04d} padding padding\n")
        if i % 100 == 0:
            guest.pages.message_boundary()

    collector = FrameCollector(r, harness)

    # Request page 1
    await harness.send(w, Frame("GETPAGE", {"page": "1"}))
    await collector.recv_until({"PAGEEND"})

    golden_file = os.path.join(
        FRAME_GOLDENS_DIR, "scenario_06_page_request_and_seal.json"
    )
    assert_matches_golden(golden_file, collector.serialized())
    await harness.close(w)


async def test_golden_07_conversation_switch(harness):
    """Scenario 7: A conversation switch, settings saved and restored."""
    r, w = await harness.connect()
    await harness.recv_until(r, {"OPTIONSEND"})

    # Change model and effort
    await harness.send(w, Frame("SETMODEL", {"model": "haiku"}))
    await harness.recv_until(r, {"OPTIONSEND"})
    await harness.send(w, Frame("SETEFFORT", {"effort": "low"}))
    await harness.recv_until(r, {"OPTIONSEND"})

    collector = FrameCollector(r, harness)

    # Request fresh session
    await harness.send(w, Frame("NEW", {}))
    await collector.recv_until({"OPTIONSEND"})

    golden_file = os.path.join(
        FRAME_GOLDENS_DIR, "scenario_07_conversation_switch.json"
    )
    assert_matches_golden(golden_file, collector.serialized())
    await harness.close(w)


async def test_golden_08_attachment_staged_and_sent(harness, tmp_path):
    """Scenario 8: An attachment staged and sent (ATTGET path included)."""
    workdir = str(tmp_path / "work")
    os.makedirs(workdir, exist_ok=True)
    harness.bridge.cwd = workdir

    r, w = await harness.connect()
    await harness.recv_until(r, {"OPTIONSEND"})

    collector = FrameCollector(r, harness)

    # Send INPUT with attachment
    await harness.send(
        w,
        Frame(
            "INPUT",
            {"attach_id": "att_1", "attach_name": "SAMPLE.TXT"},
            b"Inspect this file",
        ),
    )

    # Wait for ATTGET
    await collector.recv_until({"ATTGET"})
    attget = next(f for f in collector.frames if f.type == "ATTGET")
    att_sid = attget.headers["id"]

    # Stream file data and finish
    chunk = b"Hello from Windows 98\r\n"
    await harness.send(
        w,
        Frame(
            "SYNCDAT",
            {"id": att_sid, "off": "0", "total": str(len(chunk))},
            chunk,
        ),
    )
    await harness.send(
        w, Frame("SYNCDATEND", {"id": att_sid, "status": "ok", "crc": "1234"}, b"")
    )

    # Wait for turn completion
    await collector.recv_until({"TURN"})

    golden_file = os.path.join(
        FRAME_GOLDENS_DIR, "scenario_08_attachment_staged_and_sent.json"
    )
    assert_matches_golden(golden_file, collector.serialized())
    await harness.close(w)


async def test_golden_09_duplicate_client_rejected(harness):
    """Scenario 9: A second client rejected with ERROR busy while the first stays live."""
    r1, w1 = await harness.connect()
    await harness.recv_until(r1, {"OPTIONSEND"})

    # Client 2 connects to the same bridge port with a fresh session request
    r2, w2 = await asyncio.open_connection("127.0.0.1", harness.port)
    collector2 = FrameCollector(r2, harness)

    w2.write(hello("s", sess="different-sess-id").encode())
    await w2.drain()

    await collector2.recv_until({"ERROR"})

    golden_file = os.path.join(
        FRAME_GOLDENS_DIR, "scenario_09_duplicate_client_rejected.json"
    )
    assert_matches_golden(golden_file, collector2.serialized())
    await harness.close(w2)

    # Verify Client 1 is still live
    await harness.send(w1, Frame("INPUT", {}, b"Ping"))
    frames = []
    while not any(f.type == "TURN" and f.headers.get("state") == "end" for f in frames):
        got = await harness.recv_until(r1, {"TURN"})
        frames.extend(got)
    await harness.close(w1)


async def test_golden_10_ghost_guest_reaped_and_reconnected(harness):
    """Scenario 10: A ghost guest reaped on idle timeout, then reconnecting without being refused as busy."""
    harness.bridge.idle_timeout_s = 0.2
    harness.bridge.idle_check_s = 0.05

    r1, w1 = await harness.connect()
    await harness.recv_until(r1, {"OPTIONSEND"})

    # Drop connection 1 abruptly
    await harness.close(w1)

    # Wait for background idle reaper to run
    await asyncio.sleep(0.35)

    # Reconnect client
    r2, w2 = await harness.connect()
    collector = FrameCollector(r2, harness)
    await collector.recv_until({"OPTIONSEND"})

    golden_file = os.path.join(
        FRAME_GOLDENS_DIR, "scenario_10_ghost_guest_reaped_and_reconnected.json"
    )
    assert_matches_golden(golden_file, collector.serialized())
    await harness.close(w2)


async def test_golden_11_turn_error_and_coalescer_flush(harness):
    """Scenario 11: A turn that hits an SDK error and aborts (STATUS error and coalescer flush ordering)."""
    r, w = await harness.connect()
    await harness.recv_until(r, {"OPTIONSEND"})

    claude = harness.bridge._guest.claude

    async def failing_send(prompt):
        await claude.start_turn()
        if claude.on_text:
            await claude.on_text("Partial output before error.\n")
        raise RuntimeError("API connection failure")

    claude.send = failing_send

    collector = FrameCollector(r, harness)

    # Send INPUT
    await harness.send(w, Frame("INPUT", {}, b"Fail this turn"))
    while not any(f.type == "TURN" and f.headers.get("state") == "error" for f in collector.frames):
        await collector.recv_until({"TURN"})

    # Ensure TEXT arrived before TURN error
    types = [f.type for f in collector.frames]
    assert "TEXT" in types
    text_idx = types.index("TEXT")
    turn_err_idx = next(
        i for i, f in enumerate(collector.frames)
        if f.type == "TURN" and f.headers.get("state") == "error"
    )
    assert text_idx < turn_err_idx

    golden_file = os.path.join(
        FRAME_GOLDENS_DIR, "scenario_11_turn_error_and_coalescer_flush.json"
    )
    assert_matches_golden(golden_file, collector.serialized())
    await harness.close(w)


async def test_golden_12_disconnect_mid_turn_and_resume_cleanly(harness):
    """Scenario 12: A disconnect mid-turn, then reconnect (turn reissued and completed cleanly)."""
    r1, w1 = await harness.connect()
    await harness.recv_until(r1, {"OPTIONSEND"})
    sess_id = harness.bridge._guest.sess_id

    turn_gate = asyncio.Event()
    claude = harness.bridge._guest.claude

    async def gated_send(prompt):
        await claude.start_turn()
        if claude.on_text:
            await claude.on_text("Pre-disconnect stream...\n")
        await turn_gate.wait()
        if claude.on_text:
            await claude.on_text("Post-reconnect stream...\n")
        return Turn(
            ok=True, subtype="success", cost_usd=0.01, num_turns=1, duration_ms=10
        )

    claude.send = gated_send

    # Start turn
    await harness.send(w1, Frame("INPUT", {}, b"Gated turn"))
    await harness.recv_until(r1, {"TEXT"})

    # Drop connection 1
    await harness.close(w1)

    # Reconnect
    r2, w2 = await harness.connect(sess=sess_id)
    collector = FrameCollector(r2, harness)

    # Handshake + reissue turn
    await collector.recv_until({"TURN"})

    # Let turn complete
    turn_gate.set()
    await collector.recv_until({"TURN"})

    golden_file = os.path.join(
        FRAME_GOLDENS_DIR,
        "scenario_12_disconnect_mid_turn_and_resume_cleanly.json",
    )
    assert_matches_golden(golden_file, collector.serialized())
    await harness.close(w2)
