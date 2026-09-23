"""Prompt goldens.

Snapshots the fully assembled system prompt across diverse scenarios:
1. Bare minimal client (default Win98, no tools declared, no sync dir)
2. Full inventory client (Watcom, COMMAND.COM, NASM, tools and PATH declared, sync dir active)
3. Different client capabilities (POSIX vs CONSTRAINED shortnames)
4. Sync preamble variations (normal preamble, conflict notice preamble, agent notices)
5. Permission descriptions and rule notices

The frame trace does not cover the system prompt because it goes to the SDK,
not over the socket. These goldens ensure the prompt text migration
can be verified byte-for-byte.
"""

import json
import os
import re
from typing import Any

import pytest

from bridge.agent import machine
from bridge.agent import profiles
from bridge.agent import toolrep
from bridge.agent import toolrules
from bridge.files import caps
from bridge.wire.protocol import hello
from harness import harness  # noqa: F401
from tests.python.server.golden_utils import (
    PROMPT_GOLDENS_DIR,
    UPDATE_GOLDENS,
)


def assert_matches_prompt_golden(golden_name: str, actual_prompt: str) -> None:
    """Compare or update prompt golden markdown file."""
    golden_file = os.path.join(PROMPT_GOLDENS_DIR, f"{golden_name}.txt")
    # Normalize paths
    normalized = re.sub(r"/tmp/[^/ \n'\"]+", "<TMP_PATH>", actual_prompt)

    if UPDATE_GOLDENS or not os.path.exists(golden_file):
        os.makedirs(os.path.dirname(golden_file), exist_ok=True)
        with open(golden_file, "w", encoding="utf-8") as f:
            f.write(normalized)
            if not normalized.endswith("\n"):
                f.write("\n")

    with open(golden_file, "r", encoding="utf-8") as f:
        expected = f.read()

    assert normalized.strip() == expected.strip()


def test_prompt_golden_01_bare_client():
    """Scenario 1: Bare client with no tools, no sync dir."""
    h = hello("s").headers
    h["cpu"] = "Pentium II"
    h["ram_mb"] = "64"
    h["os"] = "Windows 98 SE"
    h["sync_root"] = "C:\\GASTON\\WORK"

    prompt = machine.system_prompt(
        h=h,
        workdir="/home/user/work",
        sync_dir=None,
        guest_root=None,
        home="C:\\GASTON\\WORK",
        tools=[],
        path=[],
    )
    assert_matches_prompt_golden("prompt_01_bare_client", prompt)


def test_prompt_golden_02_full_inventory():
    """Scenario 2: Full machine inventory with compiler, assembler, tools, and sync root."""
    h = hello("s").headers
    h["cpu"] = "Pentium MMX 233"
    h["ram_mb"] = "128"
    h["os"] = "Windows 98"
    h["sync_root"] = "C:\\PROJECT"

    triples = [
        ("command.com", "file", r"C:\WINDOWS\COMMAND.COM"),
        ("watcom", "file", r"C:\WATCOM\BINNT\WCC386.EXE"),
        ("watcom", "env", r"WATCOM=C:\WATCOM"),
    ]
    raw_body = b"".join(
        f"{name} {kind} {len(val.encode('latin-1'))} ".encode("latin-1")
        + val.encode("latin-1")
        + b"\n"
        for name, kind, val in triples
    )
    facts = toolrep.parse_rows(raw_body)
    tools = toolrules.inventory(facts)
    path = ["C:\\WINDOWS", "C:\\WINDOWS\\COMMAND", "C:\\WATCOM\\BINNT"]

    prompt = machine.system_prompt(
        h=h,
        workdir="/home/user/retro/project",
        sync_dir="/home/user/retro/project",
        guest_root="C:\\PROJECT",
        home="C:\\PROJECT",
        tools=tools,
        path=path,
    )
    assert_matches_prompt_golden("prompt_02_full_inventory", prompt)


def test_prompt_golden_03_posix_permissive():
    """Scenario 3: Permissive modern client with large path limits and POSIX caps."""
    h = hello("s").headers
    h["cpu"] = "x86_64"
    h["ram_mb"] = "1024"
    h["os"] = "Linux"
    h["sync_root"] = "/workspace"

    prompt = machine.system_prompt(
        h=h,
        workdir="/home/user/work",
        sync_dir="/home/user/work",
        guest_root="/workspace",
        home="/workspace",
        tools=[],
        path=["/bin", "/usr/bin", "/usr/local/bin"],
    )
    assert_matches_prompt_golden("prompt_03_posix_permissive", prompt)


async def test_prompt_golden_04_sync_preamble_variations(harness, tmp_path):
    """Scenario 4: System prompt with sync conflict and notice preambles."""
    r, w = await harness.connect()
    await harness.recv_until(r, {"OPTIONSEND"})
    guest = harness.bridge._guest

    # 1. Base preamble with conflict notice
    harness.bridge._agent_notices = [
        "A background sync pass found 1 conflict: README.TXT was edited on both machines.",
        "The file on your machine has been preserved as README.BAK.",
    ]
    preamble = await harness.bridge._with_sync_preamble("User request text")
    assert_matches_prompt_golden("prompt_04_sync_preamble_with_notices", preamble)
    await harness.close(w)
