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


class _FakeHost:
    """Records what each tool called on it, and with which guest.

    NESTED, like the real host: the tools reach `host.guest_exec`,
    `host.sync` and `host.workspace`, not a flat surface. It used to be flat,
    because `build()` used to be handed the Bridge and the Bridge forwarded
    everything. When the services stopped being reached through the Bridge,
    this fake kept answering and the real object stopped -- audit 04.8's shape
    exactly, a fake pinned to a hand-copy rather than to the thing it stands
    in for. `test_the_fake_host_matches_the_real_services` is what makes that
    a failing test rather than a smoke-test surprise.
    """

    class _Workspace:
        def __init__(self, outer): self._o = outer
        def guest_work_root(self, guest):
            return self._o.work_root

    class _GuestExec:
        def __init__(self, outer): self._o = outer
        async def run_on_guest(self, command, *, timeout_ms, shell, cwd, guest):
            self._o.exec_calls.append(dict(
                command=command, timeout_ms=timeout_ms, shell=shell,
                cwd=cwd, guest=guest))
            return {"content": [{"type": "text", "text": "ran"}]}

    class _Sync:
        def __init__(self, outer): self._o = outer
        def sync_status_text(self, guest):
            self._o.status_calls.append(guest)
            return "nothing pending"
        async def sync_resolve(self, path, action, guest):
            self._o.resolve_calls.append((path, action, guest))
            return f"resolved {path} {action}"

    def __init__(self, work_root="C:\\WORK"):
        self.exec_calls: list[dict] = []
        self.status_calls: list = []
        self.resolve_calls: list[tuple] = []
        self.work_root = work_root
        self.workspace = self._Workspace(self)
        self.guest_exec = self._GuestExec(self)
        self.sync = self._Sync(self)


def test_the_fake_host_matches_the_real_services():
    """Every method the fake answers must exist on the service it stands for.

    The fake diverging from the real object is how `guest_work_root` went
    missing and nothing failed until a command ran on the actual Win98 box.
    """
    from bridge.services.guest_exec import GuestExecService
    from bridge.services.sync import SyncService
    from bridge.services.workspace import WorkspaceService

    host = _FakeHost()
    for attr, real in (("workspace", WorkspaceService),
                       ("guest_exec", GuestExecService),
                       ("sync", SyncService)):
        fake = getattr(host, attr)
        for name in dir(fake):
            if name.startswith("_"):
                continue
            assert hasattr(real, name), (
                f"_FakeHost.{attr} answers {name!r} and "
                f"{real.__name__} does not -- the fake has drifted from the "
                f"service it stands in for")


def _build_tools(monkeypatch, bridge, guest="guest-1"):
    """Call the real build() with create_sdk_mcp_server stubbed out, and
    hand back its handlers by MCP tool name so the actual closures under
    test run -- only the SDK's own server plumbing is replaced."""
    captured = {}

    def fake_create_sdk_mcp_server(*, name, version, tools):
        captured["by_name"] = {t.name: t.handler for t in tools}
        captured["defs"] = {t.name: t for t in tools}
        return {"type": "sdk", "name": name, "instance": object()}

    monkeypatch.setattr(T, "create_sdk_mcp_server", fake_create_sdk_mcp_server)
    result = T.build(bridge, guest=guest)
    _build_tools.last_defs = captured["defs"]
    return result, captured["by_name"]


async def test_build_wraps_the_server_with_the_outermost_timeout(monkeypatch):
    """300_000ms, and larger than the guest's own 120s ceiling and the
    bridge's own budget -- see the comment above the return in tools.py for
    why the ordering matters."""
    result, _ = _build_tools(monkeypatch, _FakeHost())
    assert result["machine"]["timeout"] == 300_000
    assert result["machine"]["name"] == "machine"


async def test_exec_rejects_an_empty_command(monkeypatch):
    _, handlers = _build_tools(monkeypatch, _FakeHost())
    out = await handlers["exec"]({"command": "   "})
    assert out["isError"] is True


async def test_exec_defaults_timeout_and_shell_and_binds_its_own_guest(
        monkeypatch):
    bridge = _FakeHost()
    _, handlers = _build_tools(monkeypatch, bridge, guest="machine-a")
    await handlers["exec"]({"command": "dir"})
    call = bridge.exec_calls[0]
    assert call["timeout_ms"] == T.DEFAULT_TIMEOUT_MS
    # EMPTY, meaning "whichever shell that machine reported first". Naming a
    # shell here would be this file deciding for a machine it has never
    # seen, which is the whole of what 02.5 took out.
    assert call["shell"] == ""
    assert call["cwd"] == "C:\\WORK", (
        "no cwd means this conversation's own folder on the user's machine, "
        "not empty -- an empty one makes the guest pass NULL to "
        "CreateProcess and the child inherits a directory the bridge has "
        "never been told"
    )
    assert call["guest"] == "machine-a"


async def test_exec_still_honours_a_cwd_it_was_given(monkeypatch):
    bridge = _FakeHost()
    _, handlers = _build_tools(monkeypatch, bridge)
    await handlers["exec"]({"command": "dir", "cwd": "C:\\TEMP"})
    assert bridge.exec_calls[-1]["cwd"] == "C:\\TEMP"


def test_only_command_is_required(monkeypatch):
    """The shorthand schema marks EVERY property required, which made three
    documented-optional arguments mandatory. Measured on the guest: the model
    called exec without `cwd`, got `Input validation error: 'cwd' is a
    required property`, and every exec cost two round trips with a red error
    on the user's screen in between."""
    _build_tools(monkeypatch, _FakeHost())
    schema = _build_tools.last_defs["exec"].input_schema
    assert schema["required"] == ["command"]
    assert set(schema["properties"]) == {
        "command", "timeout_ms", "shell", "cwd"}


async def test_exec_clamps_timeout_ms_to_the_documented_range(monkeypatch):
    bridge = _FakeHost()
    _, handlers = _build_tools(monkeypatch, bridge)
    await handlers["exec"]({"command": "dir", "timeout_ms": 5})
    assert bridge.exec_calls[-1]["timeout_ms"] == 1000
    await handlers["exec"]({"command": "dir", "timeout_ms": 999_999_999})
    assert bridge.exec_calls[-1]["timeout_ms"] == T.MAX_TIMEOUT_MS


async def test_exec_runs_a_program_directly_when_asked(monkeypatch):
    bridge = _FakeHost()
    _, handlers = _build_tools(monkeypatch, bridge)
    await handlers["exec"]({"command": "dir", "shell": "none"})
    assert bridge.exec_calls[-1]["shell"] == "none"


async def test_exec_passes_a_named_shell_straight_through(monkeypatch):
    """A machine has as many shells as it has, and which one is a fact about
    that machine -- so this layer must not vet the name against a list it
    does not have. `run_on_guest` refuses one the machine did not report,
    and names what it did."""
    bridge = _FakeHost()
    _, handlers = _build_tools(monkeypatch, bridge)
    await handlers["exec"]({"command": "dir", "shell": "4dos"})
    assert bridge.exec_calls[-1]["shell"] == "4dos"


async def test_exec_still_understands_the_old_boolean(monkeypatch):
    """`shell` was a boolean for long enough that a model may have learned
    it. True meant "the shell somebody picked" and False meant "no shell",
    which is exactly what "" and "none" mean now."""
    bridge = _FakeHost()
    _, handlers = _build_tools(monkeypatch, bridge)
    await handlers["exec"]({"command": "dir", "shell": True})
    assert bridge.exec_calls[-1]["shell"] == ""
    await handlers["exec"]({"command": "dir", "shell": False})
    assert bridge.exec_calls[-1]["shell"] == "none"


async def test_two_builds_bind_two_different_guests(monkeypatch):
    """The module docstring's whole concern: a tool must address the
    machine ITS OWN conversation belongs to. Before the guest was threaded
    through, both closures would have captured only the Bridge and resolved
    "whichever machine is attached right now" at call time -- indistinguishable
    from correct with only one client connected, wrong with two."""
    bridge = _FakeHost()
    _, handlers_a = _build_tools(monkeypatch, bridge, guest="A")
    _, handlers_b = _build_tools(monkeypatch, bridge, guest="B")
    await handlers_a["exec"]({"command": "ver"})
    await handlers_b["exec"]({"command": "ver"})
    assert [c["guest"] for c in bridge.exec_calls] == ["A", "B"]


async def test_sync_status_is_read_only_and_reports_its_own_guest(
        monkeypatch):
    bridge = _FakeHost()
    _, handlers = _build_tools(monkeypatch, bridge, guest="machine-a")
    out = await handlers["sync_status"]({})
    assert bridge.status_calls == ["machine-a"]
    assert "nothing pending" in out["content"][0]["text"]
    assert "isError" not in out


async def test_sync_resolve_rejects_an_empty_path(monkeypatch):
    _, handlers = _build_tools(monkeypatch, _FakeHost())
    out = await handlers["sync_resolve"]({"path": ""})
    assert out["isError"] is True


async def test_sync_resolve_rejects_an_unknown_action(monkeypatch):
    _, handlers = _build_tools(monkeypatch, _FakeHost())
    out = await handlers["sync_resolve"]({"path": "notes.txt",
                                          "action": "yolo"})
    assert out["isError"] is True


async def test_sync_resolve_defaults_to_release(monkeypatch):
    bridge = _FakeHost()
    _, handlers = _build_tools(monkeypatch, bridge)
    await handlers["sync_resolve"]({"path": "notes.txt"})
    assert bridge.resolve_calls[-1][1] == "release"


async def test_sync_resolve_forget_is_passed_through_case_insensitively(
        monkeypatch):
    bridge = _FakeHost()
    _, handlers = _build_tools(monkeypatch, bridge, guest="machine-a")
    await handlers["sync_resolve"]({"path": "notes.txt", "action": "FORGET"})
    assert bridge.resolve_calls[-1] == ("notes.txt", "forget", "machine-a")
