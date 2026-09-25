"""Unit tests for AttachmentService."""

import os
from unittest.mock import AsyncMock, MagicMock

import pytest

from bridge.services.attachment import AttachmentService


@pytest.fixture
def mock_conn():
    conn = MagicMock()
    conn.sync_dir = "/sync"
    return conn


@pytest.fixture
def mock_ws(tmp_path):
    ws = MagicMock()
    work = str(tmp_path / "work")
    os.makedirs(work, exist_ok=True)
    ws.work_dir = MagicMock(return_value=work)
    return ws


@pytest.fixture
def mock_sync():
    sync = MagicMock()
    sync.run_sync = AsyncMock()
    return sync


@pytest.fixture
def mock_guest(mock_ws):
    guest = MagicMock()
    guest.work_dir = mock_ws.work_dir(guest)
    guest.link.connected = True
    return guest


def test_validate_attach_target(mock_conn, mock_ws, mock_sync, mock_guest):
    svc = AttachmentService(mock_conn, mock_ws, mock_sync)

    work_dir = mock_ws.work_dir(mock_guest)
    # Valid relative file
    res = svc.validate_attach_target(mock_guest, "notes.txt")
    assert res == os.path.join(work_dir, "notes.txt")

    # Subdirectory
    res = svc.validate_attach_target(mock_guest, "sub\\file.c")
    assert res == os.path.join(work_dir, "sub", "file.c")

    # Rejection of traversal
    assert svc.validate_attach_target(mock_guest, "../secret.txt") is None
    assert svc.validate_attach_target(mock_guest, "..\\secret.txt") is None

    # Rejection of absolute path
    assert svc.validate_attach_target(mock_guest, "/etc/passwd") is None
    assert svc.validate_attach_target(mock_guest, "C:\\autoexec.bat") is None

    # Rejection of null byte
    assert svc.validate_attach_target(mock_guest, "file\0.txt") is None

    # Rejection of empty
    assert svc.validate_attach_target(mock_guest, "") is None


def test_parse_display_names(mock_conn, mock_ws, mock_sync):
    svc = AttachmentService(mock_conn, mock_ws, mock_sync)

    names = svc.parse_display_names(
        attach="sub\\file1.c | file2.h",
        attach_name="image1.png | image2.bmp",
    )
    assert "image1.png" in names
    assert "image2.bmp" in names
    assert "file1.c" in names
    assert "file2.h" in names


def test_format_display_text(mock_conn, mock_ws, mock_sync):
    svc = AttachmentService(mock_conn, mock_ws, mock_sync)

    out = svc.format_display_text("Hello", ["foo.c", "bar.h"])
    assert out == "[Attached: foo.c, bar.h]\nHello"

    # Does not duplicate if already present
    out2 = svc.format_display_text("[Attached: foo.c]\nHello", ["foo.c"])
    assert out2 == "[Attached: foo.c]\nHello"


@pytest.mark.asyncio
async def test_assemble_prompt_text_inline(
    mock_conn, mock_ws, mock_sync, mock_guest
):
    svc = AttachmentService(mock_conn, mock_ws, mock_sync)
    work = mock_ws.work_dir(mock_guest)
    file_path = os.path.join(work, "hello.c")
    with open(file_path, "w", encoding="utf-8") as f:
        f.write('int main() { printf("hi\\n"); return 0; }\n')

    payload = await svc.assemble_prompt(
        mock_guest,
        "Please review this [Attached: hello.c]",
        attach="hello.c",
    )
    assert isinstance(payload, str)
    assert "Contents of hello.c:" in payload
    assert 'printf("hi\\n")' in payload
    assert "Please review this" in payload
    assert "[Attached: hello.c]" not in payload  # Tag cleaned from body
