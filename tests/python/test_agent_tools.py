"""bridge/agent/tools.py had zero test coverage (03.54): nothing under
tests/ imported it, and test_tools_surface.py -- despite its name -- tests
bridge.session.TOOLS, the CLI's built-in tool list, never this module.

Two things needed covering:

  * 04.26 -- the three @tool-decorated functions inside build() are listed
    by hand a few lines later in a separate tools=[...] list. Nothing
    asserted the two stayed in sync, which is the same shape as the
    Act.PULL bug: a case handled correctly and then silently absent from
    the list that actually wires it up.
  * what each handler actually does when called: empty-input rejection,
    timeout clamping, and -- the module docstring's central concern -- that
    a tool built for one guest keeps addressing THAT guest.
"""
from __future__ import annotations

import ast
import inspect

import pytest

from bridge.agent import tools as T


# --- 04.26: every @tool-decorated function in build() must be registered --


def _tool_decorated_names(src: str) -> set[str]:
    """Every async def inside build() carrying an @tool(...) decorator."""
    tree = ast.parse(src)
    build_fn = tree.body[0]
    assert isinstance(build_fn, ast.FunctionDef) and build_fn.name == "build"
    names = set()
    for node in ast.walk(build_fn):
        if isinstance(node, ast.AsyncFunctionDef):
            for dec in node.decorator_list:
                if isinstance(dec, ast.Call) and \
                        getattr(dec.func, "id", None) == "tool":
                    names.add(node.name)
    return names


def _registered_names(src: str) -> set[str]:
    """The identifiers passed as tools=[...] to create_sdk_mcp_server."""
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fname = getattr(node.func, "id", None) or \
            getattr(node.func, "attr", None)
        if fname != "create_sdk_mcp_server":
            continue
        for kw in node.keywords:
            if kw.arg == "tools" and isinstance(kw.value, ast.List):
                return {e.id for e in kw.value.elts if isinstance(e, ast.Name)}
    return set()


def test_every_tool_decorated_function_is_registered():
    """Would fail if a fourth @tool function were added and not passed to
    create_sdk_mcp_server -- the exact omission this exists to catch. Proven
    to actually catch it: temporarily removing `machine_sync_resolve` from
    the tools=[...] list in bridge/agent/tools.py made this test report
    {'machine_sync_resolve'} as missing; restored before committing. See the
    report for the transcript."""
    src = inspect.getsource(T.build)
    missing = _tool_decorated_names(src) - _registered_names(src)
    assert not missing, (
        f"{missing} are @tool-decorated inside build() but never passed to "
        f"create_sdk_mcp_server's tools=[...] -- silently absent from the "
        f"machine server")


def test_registration_list_names_nothing_undeclared():
    """The inverse direction: a name in tools=[...] that build() never
    defines as an @tool would be a NameError at import time, but keeping
    both directions asserted here documents that this is a completeness
    check, not a one-way whitelist."""
    src = inspect.getsource(T.build)
    assert _registered_names(src) <= _tool_decorated_names(src)


# --- what the handlers actually do ------------------------------------


class _FakeBridge:
    """Records what each tool called on it, and with which guest."""

    def __init__(self):
        self.exec_calls: list[dict] = []
        self.status_calls: list = []
        self.resolve_calls: list[tuple] = []

    async def run_on_guest(self, command, *, timeout_ms, shell, cwd, guest):
        self.exec_calls.append(dict(command=command, timeout_ms=timeout_ms,
                                    shell=shell, cwd=cwd, guest=guest))
        return {"content": [{"type": "text", "text": "ran"}]}

    def sync_status_text(self, guest):
        self.status_calls.append(guest)
        return "nothing pending"

    async def sync_resolve(self, path, action, guest):
        self.resolve_calls.append((path, action, guest))
        return f"resolved {path} {action}"


def _build_tools(monkeypatch, bridge, guest="guest-1"):
    """Call the real build() with create_sdk_mcp_server stubbed out, and
    hand back its handlers by MCP tool name so the actual closures under
    test run -- only the SDK's own server plumbing is replaced."""
    captured = {}

    def fake_create_sdk_mcp_server(*, name, version, tools):
        captured["by_name"] = {t.name: t.handler for t in tools}
        return {"type": "sdk", "name": name, "instance": object()}

    monkeypatch.setattr(T, "create_sdk_mcp_server", fake_create_sdk_mcp_server)
    result = T.build(bridge, guest=guest)
    return result, captured["by_name"]


async def test_build_wraps_the_server_with_the_outermost_timeout(monkeypatch):
    """300_000ms, and larger than the guest's own 120s ceiling and the
    bridge's own budget -- see the comment above the return in tools.py for
    why the ordering matters."""
    result, _ = _build_tools(monkeypatch, _FakeBridge())
    assert result["machine"]["timeout"] == 300_000
    assert result["machine"]["name"] == "machine"


async def test_exec_rejects_an_empty_command(monkeypatch):
    _, handlers = _build_tools(monkeypatch, _FakeBridge())
    out = await handlers["exec"]({"command": "   "})
    assert out["isError"] is True


async def test_exec_defaults_timeout_and_shell_and_binds_its_own_guest(
        monkeypatch):
    bridge = _FakeBridge()
    _, handlers = _build_tools(monkeypatch, bridge, guest="machine-a")
    await handlers["exec"]({"command": "dir"})
    call = bridge.exec_calls[0]
    assert call["timeout_ms"] == T.DEFAULT_TIMEOUT_MS
    assert call["shell"] is True
    assert call["cwd"] == ""
    assert call["guest"] == "machine-a"


async def test_exec_clamps_timeout_ms_to_the_documented_range(monkeypatch):
    bridge = _FakeBridge()
    _, handlers = _build_tools(monkeypatch, bridge)
    await handlers["exec"]({"command": "dir", "timeout_ms": 5})
    assert bridge.exec_calls[-1]["timeout_ms"] == 1000
    await handlers["exec"]({"command": "dir", "timeout_ms": 999_999_999})
    assert bridge.exec_calls[-1]["timeout_ms"] == T.MAX_TIMEOUT_MS


async def test_exec_shell_false_is_honoured(monkeypatch):
    bridge = _FakeBridge()
    _, handlers = _build_tools(monkeypatch, bridge)
    await handlers["exec"]({"command": "dir", "shell": False})
    assert bridge.exec_calls[-1]["shell"] is False


async def test_two_builds_bind_two_different_guests(monkeypatch):
    """The module docstring's whole concern: a tool must address the
    machine ITS OWN conversation belongs to. Before the guest was threaded
    through, both closures would have captured only the Bridge and resolved
    "whichever machine is attached right now" at call time -- indistinguishable
    from correct with only one client connected, wrong with two."""
    bridge = _FakeBridge()
    _, handlers_a = _build_tools(monkeypatch, bridge, guest="A")
    _, handlers_b = _build_tools(monkeypatch, bridge, guest="B")
    await handlers_a["exec"]({"command": "ver"})
    await handlers_b["exec"]({"command": "ver"})
    assert [c["guest"] for c in bridge.exec_calls] == ["A", "B"]


async def test_sync_status_is_read_only_and_reports_its_own_guest(
        monkeypatch):
    bridge = _FakeBridge()
    _, handlers = _build_tools(monkeypatch, bridge, guest="machine-a")
    out = await handlers["sync_status"]({})
    assert bridge.status_calls == ["machine-a"]
    assert "nothing pending" in out["content"][0]["text"]
    assert "isError" not in out


async def test_sync_resolve_rejects_an_empty_path(monkeypatch):
    _, handlers = _build_tools(monkeypatch, _FakeBridge())
    out = await handlers["sync_resolve"]({"path": ""})
    assert out["isError"] is True


async def test_sync_resolve_rejects_an_unknown_action(monkeypatch):
    _, handlers = _build_tools(monkeypatch, _FakeBridge())
    out = await handlers["sync_resolve"]({"path": "notes.txt",
                                          "action": "yolo"})
    assert out["isError"] is True


async def test_sync_resolve_defaults_to_release(monkeypatch):
    bridge = _FakeBridge()
    _, handlers = _build_tools(monkeypatch, bridge)
    await handlers["sync_resolve"]({"path": "notes.txt"})
    assert bridge.resolve_calls[-1][1] == "release"


async def test_sync_resolve_forget_is_passed_through_case_insensitively(
        monkeypatch):
    bridge = _FakeBridge()
    _, handlers = _build_tools(monkeypatch, bridge, guest="machine-a")
    await handlers["sync_resolve"]({"path": "notes.txt", "action": "FORGET"})
    assert bridge.resolve_calls[-1] == ("notes.txt", "forget", "machine-a")
