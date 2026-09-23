"""Tests for Phase 5.1: Mirrored file attachment and path security containment.
"""

import base64
import os
import pytest

from bridge.wire.protocol import Frame
from harness import harness  # noqa: F401


# 1x1 transparent PNG bytes
TINY_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII="
)


@pytest.mark.asyncio
async def test_attach_traversal_rejected(harness, tmp_path):
    """Path traversal outside work directory must be rejected with bad_attach ERROR."""
    b = harness.bridge
    b.cwd = str(tmp_path / "work")
    os.makedirs(b.cwd, exist_ok=True)

    r, w = await harness.connect()
    await harness.recv_until(r, {"READY"})

    # Traversal with ..
    await harness.send(w, Frame("INPUT", {"attach": "../../etc/shadow"}, b"look at this"))
    frames = await harness.recv_until(r, {"ERROR"}, timeout=5)
    err = [f for f in frames if f.type == "ERROR"]
    assert err, "Expected ERROR frame"
    assert err[-1].headers.get("code") == "bad_attach"
    assert "Attachment outside work directory" in err[-1].headers.get("msg", "")

    # Absolute path
    await harness.send(w, Frame("INPUT", {"attach": "/etc/shadow"}, b"look at this"))
    frames = await harness.recv_until(r, {"ERROR"}, timeout=5)
    err = [f for f in frames if f.type == "ERROR"]
    assert err, "Expected ERROR frame"
    assert err[-1].headers.get("code") == "bad_attach"

    # Drive letter path
    await harness.send(w, Frame("INPUT", {"attach": "C:\\boot.ini"}, b"look at this"))
    frames = await harness.recv_until(r, {"ERROR"}, timeout=5)
    err = [f for f in frames if f.type == "ERROR"]
    assert err, "Expected ERROR frame"
    assert err[-1].headers.get("code") == "bad_attach"

    await harness.close(w)


@pytest.mark.asyncio
async def test_attach_symlink_escape_rejected(harness, tmp_path):
    """Symlink pointing outside the work directory must be rejected."""
    workdir = str(tmp_path / "work")
    os.makedirs(workdir, exist_ok=True)
    b = harness.bridge
    b.cwd = workdir

    r, w = await harness.connect()
    await harness.recv_until(r, {"READY"})

    guest = harness.bridge._guest
    guest.work_dir = workdir

    outside = tmp_path / "secret.txt"
    outside.write_text("secret data")

    symlink_path = os.path.join(workdir, "sym_escape.txt")
    if os.path.exists(symlink_path):
        os.remove(symlink_path)
    os.symlink(str(outside), symlink_path)

    await harness.send(w, Frame("INPUT", {"attach": "sym_escape.txt"}, b"steal it"))
    frames = await harness.recv_until(r, {"ERROR"}, timeout=5)
    err = [f for f in frames if f.type == "ERROR"]
    assert err, "Expected ERROR frame for symlink traversal"
    assert err[-1].headers.get("code") == "bad_attach"

    await harness.close(w)


@pytest.mark.asyncio
async def test_attach_text_file(harness, tmp_path):
    """Attaching a text or code file instructs Claude to inspect the workspace file."""
    workdir = str(tmp_path / "work")
    os.makedirs(workdir, exist_ok=True)
    b = harness.bridge
    b.cwd = workdir

    r, w = await harness.connect()
    await harness.recv_until(r, {"READY"})

    guest = harness.bridge._guest
    guest.work_dir = workdir

    test_file = os.path.join(workdir, "notes.txt")
    with open(test_file, "w", encoding="utf-8") as f:
        f.write("Line 1: important note\nLine 2: review this\n")

    await harness.send(w, Frame("INPUT", {"attach": "notes.txt"}, b"Please review notes"))
    await harness.recv_until(r, {"TURN"}, timeout=5)
    await harness.recv_until(r, {"TURN"}, timeout=5)

    claude = guest.claude
    assert claude is not None
    assert "[Attached workspace file: notes.txt]" in claude.last_prompt
    assert "Contents of notes.txt:" in claude.last_prompt
    assert "Line 1: important note" in claude.last_prompt
    assert "Please review notes" in claude.last_prompt

    await harness.close(w)


@pytest.mark.asyncio
async def test_attach_empty_file_and_ui_tag_cleanup(harness, tmp_path):
    """Empty attached file is marked (empty file) and client [Attached: ...] tags are cleaned."""
    workdir = str(tmp_path / "work")
    os.makedirs(workdir, exist_ok=True)
    b = harness.bridge
    b.cwd = workdir

    r, w = await harness.connect()
    await harness.recv_until(r, {"READY"})

    guest = harness.bridge._guest
    guest.work_dir = workdir

    empty_file = os.path.join(workdir, "empty.txt")
    with open(empty_file, "wb") as f:
        pass

    # Windows client sends [Attached: empty.txt]\r\nWhat is this?
    prompt = b"[Attached: empty.txt]\r\nWhat is this?"
    await harness.send(w, Frame("INPUT", {"attach": "empty.txt"}, prompt))
    await harness.recv_until(r, {"TURN"}, timeout=5)
    await harness.recv_until(r, {"TURN"}, timeout=5)

    claude = guest.claude
    assert claude is not None
    assert isinstance(claude.last_prompt, str)
    assert "[Attached workspace file: empty.txt (empty file)]" in claude.last_prompt
    assert "[Attached: empty.txt]" not in claude.last_prompt
    assert "What is this?" in claude.last_prompt

    await harness.close(w)


@pytest.mark.asyncio
async def test_attach_binary_file(harness, tmp_path):
    """Binary files (containing null bytes) are marked as binary file without inlining."""
    workdir = str(tmp_path / "work")
    os.makedirs(workdir, exist_ok=True)
    b = harness.bridge
    b.cwd = workdir

    r, w = await harness.connect()
    await harness.recv_until(r, {"READY"})

    guest = harness.bridge._guest
    guest.work_dir = workdir

    bin_file = os.path.join(workdir, "data.bin")
    with open(bin_file, "wb") as f:
        f.write(b"\x00\x01\x02\x03\x04\xff")

    await harness.send(w, Frame("INPUT", {"attach": "data.bin"}, b"Check binary data"))
    await harness.recv_until(r, {"TURN"}, timeout=5)
    await harness.recv_until(r, {"TURN"}, timeout=5)

    claude = guest.claude
    assert claude is not None
    assert isinstance(claude.last_prompt, str)
    assert "[Attached workspace file: data.bin (6 bytes, binary file)]" in claude.last_prompt
    assert "Check binary data" in claude.last_prompt

    await harness.close(w)


@pytest.mark.asyncio
async def test_attach_subdirectory_and_backslash(harness, tmp_path):
    """Relative subpaths and Windows backslashes in attachments are handled."""
    workdir = str(tmp_path / "work")
    os.makedirs(workdir, exist_ok=True)
    b = harness.bridge
    b.cwd = workdir

    r, w = await harness.connect()
    await harness.recv_until(r, {"READY"})

    guest = harness.bridge._guest
    guest.work_dir = workdir

    subdir = os.path.join(workdir, "sub", "dir")
    os.makedirs(subdir, exist_ok=True)

    c_file = os.path.join(subdir, "code.c")
    with open(c_file, "w", encoding="utf-8") as f:
        f.write("int main() { return 0; }\n")

    # Send with Windows-style backslashes
    await harness.send(w, Frame("INPUT", {"attach": "sub\\dir\\code.c"}, b"Check the code"))
    await harness.recv_until(r, {"TURN"}, timeout=5)
    await harness.recv_until(r, {"TURN"}, timeout=5)

    claude = guest.claude
    assert claude is not None
    assert isinstance(claude.last_prompt, str)
    assert "[Attached workspace file: sub/dir/code.c]" in claude.last_prompt
    assert "Check the code" in claude.last_prompt

    await harness.close(w)


@pytest.mark.asyncio
async def test_attach_image_multimodal(harness, tmp_path):
    """Attaching an image sends an image content block with base64 payload to Claude."""
    workdir = str(tmp_path / "work")
    os.makedirs(workdir, exist_ok=True)
    b = harness.bridge
    b.cwd = workdir

    r, w = await harness.connect()
    await harness.recv_until(r, {"READY"})

    guest = harness.bridge._guest
    guest.work_dir = workdir

    img_path = os.path.join(workdir, "diagram.png")
    with open(img_path, "wb") as f:
        f.write(TINY_PNG)

    await harness.send(w, Frame("INPUT", {"attach": "diagram.png"}, b"What is in this diagram?"))
    await harness.recv_until(r, {"TURN"}, timeout=5)
    await harness.recv_until(r, {"TURN"}, timeout=5)

    claude = guest.claude
    assert claude is not None
    assert claude.last_prompt_messages, "Expected multimodal messages yielded"

    first_msg = claude.last_prompt_messages[0]
    content = first_msg.get("message", {}).get("content", [])
    assert len(content) == 2

    # Verify image block
    img_block = content[0]
    assert img_block["type"] == "image"
    assert img_block["source"]["type"] == "base64"
    assert img_block["source"]["media_type"] == "image/png"
    assert img_block["source"]["data"] == base64.b64encode(TINY_PNG).decode("ascii")

    # Verify text block
    text_block = content[1]
    assert text_block["type"] == "text"
    assert "What is in this diagram?" in text_block["text"]

    await harness.close(w)


@pytest.mark.asyncio
async def test_attach_image_too_large_fallback(harness, tmp_path, monkeypatch):
    """An image exceeding 10MB limit falls back to workspace file prompt note."""
    workdir = str(tmp_path / "work")
    os.makedirs(workdir, exist_ok=True)
    b = harness.bridge
    b.cwd = workdir

    r, w = await harness.connect()
    await harness.recv_until(r, {"READY"})

    guest = harness.bridge._guest
    guest.work_dir = workdir

    img_path = os.path.join(workdir, "huge.png")
    with open(img_path, "wb") as f:
        f.write(TINY_PNG)

    # Mock getsize to simulate > 10MB
    orig_getsize = os.path.getsize
    monkeypatch.setattr(os.path, "getsize",
                        lambda p: 15 * 1024 * 1024 if "huge.png" in p else orig_getsize(p))

    await harness.send(w, Frame("INPUT", {"attach": "huge.png"}, b"Analyze huge image"))
    await harness.recv_until(r, {"TURN"}, timeout=5)
    await harness.recv_until(r, {"TURN"}, timeout=5)

    claude = guest.claude
    assert claude is not None
    assert isinstance(claude.last_prompt, str)
    assert "[Attached workspace file: huge.png (file too large for image block)]" in claude.last_prompt
    assert "Analyze huge image" in claude.last_prompt

    await harness.close(w)

