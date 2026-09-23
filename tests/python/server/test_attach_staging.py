"""Tests for Phase 5.2 and 5.3: Out-of-band streaming, 90s image conversion, and codepage decoding.
"""

import base64
import io
import os
import pytest
from PIL import Image

from bridge.wire.protocol import Frame
from harness import harness  # noqa: F401


def make_test_bmp(width=4, height=4, color=(255, 0, 0)) -> bytes:
    """Create a minimal 24bpp BMP in memory."""
    img = Image.new("RGB", (width, height), color=color)
    buf = io.BytesIO()
    img.save(buf, format="BMP")
    return buf.getvalue()


def make_test_pcx(width=4, height=4) -> bytes:
    """Create a minimal PCX image in memory."""
    img = Image.new("P", (width, height))
    buf = io.BytesIO()
    img.save(buf, format="PCX")
    return buf.getvalue()


@pytest.mark.asyncio
async def test_attach_id_attget_streaming(harness, tmp_path):
    """External file attached via attach_id triggers ATTGET and streams SYNCDAT."""
    workdir = str(tmp_path / "work")
    os.makedirs(workdir, exist_ok=True)
    b = harness.bridge
    b.cwd = workdir

    r, w = await harness.connect()
    await harness.recv_until(r, {"READY"})

    guest = harness.bridge._guest
    guest.work_dir = workdir

    # Send INPUT with attach_id
    await harness.send(w, Frame("INPUT", {"attach_id": "att_1", "attach_name": "WIN.INI"}, b"Check config"))

    # Guest should receive ATTGET
    frames = await harness.recv_until(r, {"ATTGET"}, timeout=5)
    attget = [f for f in frames if f.type == "ATTGET"]
    assert attget, "Expected ATTGET frame from bridge"
    att_sid = attget[-1].headers.get("id")
    assert attget[-1].headers.get("att") == "att_1"

    # Guest responds with SYNCDAT chunks and SYNCDATEND
    chunk1 = b"[windows]\r\nrun=retro.exe\r\n"
    await harness.send(w, Frame("SYNCDAT", {"id": att_sid, "off": "0", "total": str(len(chunk1))}, chunk1))
    await harness.send(w, Frame("SYNCDATEND", {"id": att_sid, "status": "ok", "crc": "1234"}, b""))

    # Wait for turn completion
    await harness.recv_until(r, {"TURN"}, timeout=5)
    await harness.recv_until(r, {"TURN"}, timeout=5)

    claude = guest.claude
    assert claude is not None
    assert isinstance(claude.last_prompt, str)
    assert "[Attached file: WIN.INI]" in claude.last_prompt
    assert "[windows]" in claude.last_prompt
    assert "run=retro.exe" in claude.last_prompt
    assert "Check config" in claude.last_prompt

    await harness.close(w)


@pytest.mark.asyncio
async def test_attach_bmp_converted_to_png(harness, tmp_path):
    """BMP image (e.g. screenshot) attached via attach_id is converted to PNG image block."""
    workdir = str(tmp_path / "work")
    os.makedirs(workdir, exist_ok=True)
    b = harness.bridge
    b.cwd = workdir

    r, w = await harness.connect()
    await harness.recv_until(r, {"READY"})

    guest = harness.bridge._guest
    guest.work_dir = workdir

    bmp_bytes = make_test_bmp(width=8, height=8, color=(0, 255, 0))

    await harness.send(w, Frame("INPUT", {"attach_id": "att_shot", "attach_name": "Screenshot (Desktop)"}, b"What is on screen?"))

    frames = await harness.recv_until(r, {"ATTGET"}, timeout=5)
    attget = [f for f in frames if f.type == "ATTGET"]
    assert attget
    att_sid = attget[-1].headers.get("id")

    # Stream the BMP
    await harness.send(w, Frame("SYNCDAT", {"id": att_sid, "off": "0", "total": str(len(bmp_bytes))}, bmp_bytes))
    await harness.send(w, Frame("SYNCDATEND", {"id": att_sid, "status": "ok", "crc": "5678"}, b""))

    await harness.recv_until(r, {"TURN"}, timeout=5)
    await harness.recv_until(r, {"TURN"}, timeout=5)

    claude = guest.claude
    assert claude is not None
    assert claude.last_prompt_messages, "Expected multimodal messages"
    first_msg = claude.last_prompt_messages[0]
    content = first_msg.get("message", {}).get("content", [])
    img_block = [c for c in content if c.get("type") == "image"]
    assert img_block, "Expected image content block"
    assert img_block[0]["source"]["media_type"] == "image/png"
    # Verify the image data is valid PNG
    raw_png = base64.b64decode(img_block[0]["source"]["data"])
    assert raw_png.startswith(b"\x89PNG\r\n\x1a\n")

    text_block = [c for c in content if c.get("type") == "text"]
    assert text_block and "What is on screen?" in text_block[0]["text"]

    await harness.close(w)


@pytest.mark.asyncio
async def test_attach_pcx_converted_to_png(harness, tmp_path):
    """90s PCX image in workspace is converted by Pillow into PNG image block."""
    workdir = str(tmp_path / "work")
    os.makedirs(workdir, exist_ok=True)
    b = harness.bridge
    b.cwd = workdir

    r, w = await harness.connect()
    await harness.recv_until(r, {"READY"})

    guest = harness.bridge._guest
    guest.work_dir = workdir

    pcx_file = os.path.join(workdir, "art.pcx")
    with open(pcx_file, "wb") as f:
        f.write(make_test_pcx(8, 8))

    await harness.send(w, Frame("INPUT", {"attach": "art.pcx"}, b"Look at this retro drawing"))

    await harness.recv_until(r, {"TURN"}, timeout=5)
    await harness.recv_until(r, {"TURN"}, timeout=5)

    claude = guest.claude
    assert claude is not None
    assert claude.last_prompt_messages, "Expected multimodal messages"
    first_msg = claude.last_prompt_messages[0]
    content = first_msg.get("message", {}).get("content", [])
    img_block = [c for c in content if c.get("type") == "image"]
    assert img_block, "Expected PCX to be converted to image block"
    assert img_block[0]["source"]["media_type"] == "image/png"
    raw_png = base64.b64decode(img_block[0]["source"]["data"])
    assert raw_png.startswith(b"\x89PNG\r\n\x1a\n")

    await harness.close(w)


@pytest.mark.asyncio
async def test_attach_text_cp1252_encoding(harness, tmp_path):
    """ANSI Windows CP1252 text files are decoded cleanly with accents preserved."""
    workdir = str(tmp_path / "work")
    os.makedirs(workdir, exist_ok=True)
    b = harness.bridge
    b.cwd = workdir

    r, w = await harness.connect()
    await harness.recv_until(r, {"READY"})

    guest = harness.bridge._guest
    guest.work_dir = workdir

    # Text containing CP1252 accented characters (not valid UTF-8 alone)
    accented_text = "Édition spéciale: coût élevé été café\r\n"
    cp1252_bytes = accented_text.encode("cp1252")

    test_file = os.path.join(workdir, "notes_fr.txt")
    with open(test_file, "wb") as f:
        f.write(cp1252_bytes)

    await harness.send(w, Frame("INPUT", {"attach": "notes_fr.txt"}, b"Traduire ce texte"))

    await harness.recv_until(r, {"TURN"}, timeout=5)
    await harness.recv_until(r, {"TURN"}, timeout=5)

    claude = guest.claude
    assert claude is not None
    assert isinstance(claude.last_prompt, str)
    assert "Édition spéciale: coût élevé été café" in claude.last_prompt
    assert "Traduire ce texte" in claude.last_prompt

    await harness.close(w)


@pytest.mark.asyncio
async def test_attach_binary_file_reference(harness, tmp_path):
    """Unknown binary file (e.g. .zip or .exe) is referenced gracefully without inlining binary garbage."""
    workdir = str(tmp_path / "work")
    os.makedirs(workdir, exist_ok=True)
    b = harness.bridge
    b.cwd = workdir

    r, w = await harness.connect()
    await harness.recv_until(r, {"READY"})

    guest = harness.bridge._guest
    guest.work_dir = workdir

    exe_bytes = b"MZ\x90\x00\x03\x00\x00\x00\x04\x00\x00\x00\xff\xff\x00\x00"

    await harness.send(w, Frame("INPUT", {"attach_id": "att_bin", "attach_name": "app.exe"}, b"What binary is this?"))

    frames = await harness.recv_until(r, {"ATTGET"}, timeout=5)
    attget = [f for f in frames if f.type == "ATTGET"]
    assert attget
    att_sid = attget[-1].headers.get("id")

    await harness.send(w, Frame("SYNCDAT", {"id": att_sid, "off": "0", "total": str(len(exe_bytes))}, exe_bytes))
    await harness.send(w, Frame("SYNCDATEND", {"id": att_sid, "status": "ok", "crc": "9999"}, b""))

    await harness.recv_until(r, {"TURN"}, timeout=5)
    await harness.recv_until(r, {"TURN"}, timeout=5)

    claude = guest.claude
    assert claude is not None
    assert isinstance(claude.last_prompt, str)
    assert "[Referenced file: app.exe (16 bytes, binary file)]" in claude.last_prompt
    assert "What binary is this?" in claude.last_prompt

    await harness.close(w)


@pytest.mark.asyncio
async def test_attach_multiple_files(harness, tmp_path):
    """Multiple files (workspace + external) are processed and inlined in prompt."""
    workdir = str(tmp_path / "work")
    os.makedirs(workdir, exist_ok=True)
    b = harness.bridge
    b.cwd = workdir

    r, w = await harness.connect()
    await harness.recv_until(r, {"READY"})

    guest = harness.bridge._guest
    guest.work_dir = workdir

    # Workspace file
    with open(os.path.join(workdir, "local.txt"), "w") as f:
        f.write("Local file contents\n")

    # Send INPUT with both workspace file and external file
    await harness.send(w, Frame("INPUT", {
        "attach": "local.txt",
        "attach_id": "att_ext1",
        "attach_name": "WIN.INI"
    }, b"Compare these files"))

    # Should request att_ext1
    frames = await harness.recv_until(r, {"ATTGET"}, timeout=5)
    attget = [f for f in frames if f.type == "ATTGET"]
    assert attget
    att_sid = attget[-1].headers.get("id")

    ext_data = b"[win]\r\nsetting=1\r\n"
    await harness.send(w, Frame("SYNCDAT", {"id": att_sid, "off": "0", "total": str(len(ext_data))}, ext_data))
    await harness.send(w, Frame("SYNCDATEND", {"id": att_sid, "status": "ok", "crc": "1111"}, b""))

    await harness.recv_until(r, {"TURN"}, timeout=5)
    await harness.recv_until(r, {"TURN"}, timeout=5)

    claude = guest.claude
    assert claude is not None
    assert isinstance(claude.last_prompt, str)
    assert "[Attached workspace file: local.txt]" in claude.last_prompt
    assert "Local file contents" in claude.last_prompt
    assert "[Attached file: WIN.INI]" in claude.last_prompt
    assert "[win]" in claude.last_prompt
    assert "Compare these files" in claude.last_prompt

    await harness.close(w)


@pytest.mark.asyncio
async def test_attach_multiple_images(harness, tmp_path):
    """Multiple images attached in one INPUT become multiple image blocks in multimodal prompt."""
    workdir = str(tmp_path / "work")
    os.makedirs(workdir, exist_ok=True)
    b = harness.bridge
    b.cwd = workdir

    r, w = await harness.connect()
    await harness.recv_until(r, {"READY"})

    guest = harness.bridge._guest
    guest.work_dir = workdir

    # Workspace PCX image
    pcx_file = os.path.join(workdir, "art.pcx")
    with open(pcx_file, "wb") as f:
        f.write(make_test_pcx(8, 8))

    # Send INPUT with workspace image and external BMP screenshot
    await harness.send(w, Frame("INPUT", {
        "attach": "art.pcx",
        "attach_id": "shot_1",
        "attach_name": "Screenshot #1"
    }, b"Compare these images"))

    frames = await harness.recv_until(r, {"ATTGET"}, timeout=5)
    attget = [f for f in frames if f.type == "ATTGET"]
    assert attget
    att_sid = attget[-1].headers.get("id")

    bmp_bytes = make_test_bmp(8, 8, (255, 255, 0))
    await harness.send(w, Frame("SYNCDAT", {"id": att_sid, "off": "0", "total": str(len(bmp_bytes))}, bmp_bytes))
    await harness.send(w, Frame("SYNCDATEND", {"id": att_sid, "status": "ok", "crc": "2222"}, b""))

    await harness.recv_until(r, {"TURN"}, timeout=5)
    await harness.recv_until(r, {"TURN"}, timeout=5)

    claude = guest.claude
    assert claude is not None
    assert claude.last_prompt_messages, "Expected multimodal messages"
    first_msg = claude.last_prompt_messages[0]
    content = first_msg.get("message", {}).get("content", [])
    img_blocks = [c for c in content if c.get("type") == "image"]
    assert len(img_blocks) == 2, f"Expected 2 image blocks, got {len(img_blocks)}"
    for b in img_blocks:
        assert b["source"]["media_type"] == "image/png"
        raw_png = base64.b64decode(b["source"]["data"])
        assert raw_png.startswith(b"\x89PNG\r\n\x1a\n")

    text_blocks = [c for c in content if c.get("type") == "text"]
    assert text_blocks and "Compare these images" in text_blocks[0]["text"]

    await harness.close(w)

