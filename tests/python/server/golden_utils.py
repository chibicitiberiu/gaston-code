"""Utilities for recording and verifying frame-trace and prompt goldens.

The frame trace is the contract:
- Characterisation records it.
- Refactoring must not change it. A diff is a bug.
- Intentional protocol changes alter it deliberately, one small change at a time.
"""

import json
import os
import re
from typing import Any

from bridge.wire.compress import StreamDecompressor
from bridge.wire.protocol import Frame

GOLDEN_BASE_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "goldens")
)
FRAME_GOLDENS_DIR = os.path.join(GOLDEN_BASE_DIR, "frames")
PROMPT_GOLDENS_DIR = os.path.join(GOLDEN_BASE_DIR, "prompts")

UPDATE_GOLDENS = os.environ.get("UPDATE_GOLDENS") == "1"


def normalize_headers(headers: dict[str, str], frame_type: str) -> dict[str, str]:
    """Normalize non-deterministic fields in frame headers."""
    norm = {}
    for k, v in sorted(headers.items()):
        if k == "sess":
            norm[k] = "<SESS_ID>"
        elif k == "conv":
            norm[k] = "<CONV_ID>"
        elif k == "id" and frame_type in ("HALT", "HALTEND", "RESUME"):
            norm[k] = "<HALT_ID>"
        elif k == "id" and frame_type in ("ATTGET", "SYNCDAT", "SYNCDATEND"):
            norm[k] = "<SYNC_ID>"
        elif k == "ms":
            norm[k] = "<MS>"
        elif k == "cost_usd":
            norm[k] = "<COST>"
        elif k == "preview" and frame_type == "ANCHOR":
            # Mask dynamic session ids or paths in preview text
            norm[k] = re.sub(r"fake-session-\d+", "<CONV_ID>", v)
        else:
            norm[k] = v
    return norm


def normalize_body(body: bytes, frame: Frame, unzip: StreamDecompressor) -> str | None:
    """Decode and normalize frame body for clean diffs."""
    if not body:
        return None

    raw_bytes = body
    if frame.type == "TEXT" and frame.headers.get("enc") == "deflate":
        try:
            raw_bytes = unzip.decompress(body)
        except Exception:
            raw_bytes = body

    try:
        text = raw_bytes.decode("utf-8")
    except UnicodeDecodeError:
        try:
            text = raw_bytes.decode("cp1252")
        except UnicodeDecodeError:
            text = f"<binary {len(raw_bytes)} bytes>"

    # Normalize variable paths or session IDs in body
    text = re.sub(r"fake-session-\d+", "<CONV_ID>", text)
    text = re.sub(r"/tmp/[^/ \n'\"]+", "<TMP_PATH>", text)
    text = re.sub(r"att_[a-f0-9]+", "att_<ID>", text)
    return text


def serialize_frame(frame: Frame, unzip: StreamDecompressor) -> dict[str, Any]:
    """Serialize a single frame into a deterministic dictionary."""
    data: dict[str, Any] = {
        "type": frame.type,
        "headers": normalize_headers(frame.headers, frame.type),
    }
    body_text = normalize_body(frame.body, frame, unzip)
    if body_text is not None:
        data["body"] = body_text
    return data


def serialize_frame_sequence(
    frames: list[Frame], unzip: StreamDecompressor | None = None
) -> list[dict[str, Any]]:
    """Serialize a list of frames with persistent decompressor."""
    decompressor = unzip or StreamDecompressor()
    return [serialize_frame(f, decompressor) for f in frames]


def assert_matches_golden(golden_file: str, actual_data: Any) -> None:
    """Assert actual_data matches the golden file, writing it if UPDATE_GOLDENS=1."""
    if UPDATE_GOLDENS or not os.path.exists(golden_file):
        os.makedirs(os.path.dirname(golden_file), exist_ok=True)
        with open(golden_file, "w", encoding="utf-8") as f:
            json.dump(actual_data, f, indent=2, sort_keys=True)
            f.write("\n")

    with open(golden_file, encoding="utf-8") as f:
        expected = json.load(f)

    assert actual_data == expected
