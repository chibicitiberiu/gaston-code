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


def _local_lists(build_fn) -> dict:
    """Every `name = <expr>` inside build(), by name.

    Needed because the registration list is no longer one literal. Since
    BACKLOG.md 19 the five screen tools are a separate list that is empty for
    a client declaring no automation, and it is spliced in:

        screen = [machine_ui, ...] if automation else []
        tools=[machine_exec, ...] + screen + [machine_ps, ...]

    A reader of the old version of this function saw `tools=` was not a plain
    list, returned the empty set, and the test then reported EVERY tool as
    unregistered -- which is the failure mode of a source-level check that
    pattern-matches one spelling. Resolving the names instead keeps it
    answering the question it was written to answer.
    """
    out = {}
    for node in ast.walk(build_fn):
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    out[t.id] = node.value
    return out


def _registered_names(src: str) -> set[str]:
    """Every tool identifier that can reach create_sdk_mcp_server's tools=.

    The union over every branch, deliberately: this check is about a tool
    function that is defined and then wired up NOWHERE, which is a mistake in
    the source. Whether a particular client gets a particular tool is a
    different question with a different answer per connection, and it is
    tested by calling build() -- see test_tools_surface.py.
    """
    tree = ast.parse(src)
    build_fn = tree.body[0]
    locals_ = _local_lists(build_fn)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fname = getattr(node.func, "id", None) or \
            getattr(node.func, "attr", None)
        if fname != "create_sdk_mcp_server":
            continue
        for kw in node.keywords:
            if kw.arg != "tools":
                continue
            found, seen = set(), set()
            todo = [kw.value]
            while todo:
                expr = todo.pop()
                # ELEMENTS OF LIST LITERALS ONLY. Walking every Name in the
                # expression would also collect the condition a branch is
                # taken on (`automation`, and the `guest`/`C` inside it),
                # which are not tools and would make the inverse check --
                # "nothing registered that build() does not define" -- fail
                # on its own helper variables.
                for sub in ast.walk(expr):
                    if isinstance(sub, ast.List):
                        found |= {e.id for e in sub.elts
                                  if isinstance(e, ast.Name)}
                    elif isinstance(sub, ast.Name) and sub.id in locals_ \
                            and sub.id not in seen:
                        seen.add(sub.id)
                        todo.append(locals_[sub.id])
            return found
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
    result = T.build(bridge.guest_exec, bridge.sync, bridge.workspace,
                     guest=guest)
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


# --- toolnote: the measured bodies, fetched rather than resident -----------
#
# The system prompt carried one toolchain note and dropped the rest in
# alphabetical order, so the user's machine -- a Sun JDK and Visual C++ 6 --
# lost the C++ note and with it `cl` printing nothing at all when a
# directory is missing from PATH. The bodies moved behind this tool; these
# are about it answering for the machine whose conversation it is.

from bridge.agent.toolrules import Found


class _Guest:
    def __init__(self, tools):
        self.tools = tools


def _text_of(out: dict) -> str:
    return "".join(b.get("text", "") for b in out["content"])


def _toolnote(monkeypatch, found):
    captured = {}

    def fake_create_sdk_mcp_server(*, name, version, tools):
        captured["by_name"] = {t.name: t.handler for t in tools}
        return {"type": "sdk", "name": name, "instance": object()}

    monkeypatch.setattr(T, "create_sdk_mcp_server", fake_create_sdk_mcp_server)
    bridge = _FakeHost()
    T.build(bridge.guest_exec, bridge.sync, bridge.workspace,
            guest=_Guest(found))
    return captured["by_name"]["toolnote"]


JDK = Found("jdk", "toolchain", r"C:\Java-Workshop20\JDK", True, version="1.1.3")
MSVC = Found("msvc", "toolchain", r"C:\VC98", True, version="6.0")


async def test_both_toolchains_are_reachable_not_just_the_first(monkeypatch):
    """THE BUG, from the other side. Alphabetical order decided which note
    existed; now it decides nothing."""
    h = _toolnote(monkeypatch, [JDK, MSVC])
    jdk = _text_of(await h({"name": "jdk"}))
    msvc = _text_of(await h({"name": "msvc"}))
    assert "Sun JDK 1.1" in jdk
    assert "Visual C++" in msvc
    # The fact alphabetical order was throwing away: `cl` with MSPDB60.DLL's
    # directory off PATH prints nothing on either stream and only the exit
    # code says it failed.
    assert "prints NOTHING AT ALL" in " ".join(msvc.split())


async def test_the_note_is_rendered_against_the_root_that_was_found(monkeypatch):
    r"""A standalone 1.1 install is nowhere near Java WorkShop's layout. A
    note quoting one while the inventory names the other is two
    authoritative sources disagreeing, the more detailed one wrong."""
    h = _toolnote(monkeypatch, [Found("jdk", "toolchain", r"C:\JDK1.1.8",
                                      True, version="1.1.8")])
    out = _text_of(await h({"name": "jdk"}))
    assert r"C:\JDK1.1.8\bin\javac -d ." in out
    assert "JDK 1.1.8" in out
    assert "{root}" not in out and "{version}" not in out


async def test_the_note_says_where_it_was_measured(monkeypatch):
    """The agent is being told to act on this without checking, so it is
    entitled to know what it was measured on and when."""
    h = _toolnote(monkeypatch, [JDK])
    assert "Measured:" in _text_of(await h({"name": "jdk"}))


async def test_a_tool_that_is_not_on_that_machine_is_refused(monkeypatch):
    """And refused by naming what IS there. A bare "unknown" invites a
    second guess; the list ends the exchange in one call."""
    h = _toolnote(monkeypatch, [JDK])
    out = await h({"name": "watcom"})
    assert out["isError"] is True
    assert "jdk" in _text_of(out)


async def test_a_detected_tool_nobody_measured_says_so(monkeypatch):
    """Detection may guess; notes may not. Inventing one here would be the
    exact failure the profiles' `measured` field exists to prevent."""
    h = _toolnote(monkeypatch, [JDK, Found("pkzip", "tool", r"C:\PKZIP", True)])
    out = await h({"name": "pkzip"})
    assert out["isError"] is True
    assert "nobody has measured" in _text_of(out)


async def test_the_name_is_matched_as_the_prompt_prints_it(monkeypatch):
    """The model retypes the name out of a table in its own prompt. A
    refusal over capitalisation costs a round trip and teaches it nothing."""
    h = _toolnote(monkeypatch, [MSVC])
    assert "Visual C++" in _text_of(await h({"name": "MSVC"}))


async def test_a_machine_that_reported_nothing_is_not_a_traceback(monkeypatch):
    h = _toolnote(monkeypatch, [])
    out = await h({"name": "jdk"})
    assert out["isError"] is True


# --- toolnote takes a version, and says when it could not match it ----------
#
# A note is about a RELEASE, not about a product, and until now there was
# nowhere to say so -- so the only defence against describing the wrong one
# was to refuse to detect it. `jdk.toml` lists 1.1-era paths and nothing
# later because its body is a list of what Java 1.1 lacks, and that list is
# "confidently wrong in front of a 1.2+ install where half of it came back".
# A JDK 1.3 on the machine was therefore reported as no JDK at all.
#
# Now the caller names a version, the nearest measured body answers, and an
# inexact match SAYS SO WITH BOTH NUMBERS IN IT. That last part is the
# feature: an approximate note the agent cannot distinguish from an exact one
# is worse than no note, because it is believed the same amount.

from bridge.agent import toolnotes as _notes


def _fake_note(*, base="", base_version="", variants=()):
    return _notes.Note(
        title="Fake Compiler", measured="a machine, 2026-10-01",
        body=base, for_version=base_version,
        variants=tuple(_notes.Body(version=v, title=f"Fake Compiler {v}",
                                   measured=f"a {v} box, 2026-10-01",
                                   body=f"Fake {{version}} at {{root}}: "
                                        f"the {v} facts.")
                       for v in variants))


FAKE = Found("faketool", "toolchain", r"D:\FAKE", True, version="1.1")


def _with_fake(monkeypatch, note, found=(FAKE,)):
    monkeypatch.setitem(_notes.NOTES, "faketool", note)
    return _toolnote(monkeypatch, list(found))


def _toolnote_schema(monkeypatch) -> dict:
    captured = {}

    def fake_create_sdk_mcp_server(*, name, version, tools):
        captured["by_name"] = {t.name: t for t in tools}
        return {"type": "sdk", "name": name, "instance": object()}

    monkeypatch.setattr(T, "create_sdk_mcp_server", fake_create_sdk_mcp_server)
    bridge = _FakeHost()
    T.build(bridge.guest_exec, bridge.sync, bridge.workspace)
    return captured["by_name"]["toolnote"].input_schema


def test_the_version_is_optional_and_name_is_still_the_only_requirement(
        monkeypatch):
    """REQUIRED WOULD HAVE BEEN THE WRONG CHOICE. The machine already
    reports a version for most toolchains, so making the model name one is
    asking it to retype a number out of its own prompt -- and a fat-fingered
    digit there returns a note about a release nobody is running. Every call
    shaped the way yesterday's calls were shaped still works."""
    schema = _toolnote_schema(monkeypatch)
    assert set(schema["properties"]) == {"name", "version"}
    assert schema["required"] == ["name"]
    assert schema["properties"]["version"]["type"] == "string"
    assert schema["properties"]["version"]["description"].strip()


async def test_an_exact_version_reads_exactly_as_it_did_before(monkeypatch):
    h = _with_fake(monkeypatch, _fake_note(variants=("1.1", "1.2")))
    out = _text_of(await h({"name": "faketool", "version": "1.2"}))
    assert "the 1.2 facts" in out
    assert "NOT THE VERSION" not in out


async def test_a_point_release_matches_the_line_that_documents_it(monkeypatch):
    """1.1 measured, 1.1.8 asked. No warning: a disclosure on a note that
    does fit is how an agent learns to skim the ones that do not."""
    h = _with_fake(monkeypatch, _fake_note(variants=("1.1", "1.2")))
    out = _text_of(await h({"name": "faketool", "version": "1.1.8"}))
    assert "the 1.1 facts" in out
    assert "NOT THE VERSION" not in out
    # And the body still renders the version the CALLER is on.
    assert "Fake 1.1.8 at D:\\FAKE" in out


async def test_a_newer_machine_than_anything_measured_is_told_so(monkeypatch):
    """THE CASE THE JDK PROFILE EXISTS AS A REFUSAL BECAUSE OF. The note
    comes back, and it comes back labelled -- naming the version measured
    and the version asked for, because "may not match your version" is a
    disclaimer and not something a model can act on."""
    h = _with_fake(monkeypatch, _fake_note(variants=("1.1", "1.2")))
    out = _text_of(await h({"name": "faketool", "version": "1.3"}))
    assert "the 1.2 facts" in out
    assert "NOT THE VERSION IN FRONT OF YOU" in out
    assert "1.3" in out and "1.2" in out
    # The direction, named: the machine is AHEAD, so an absence in the note
    # is an absence in 1.2 and not an absence on that machine.
    assert "came AFTER" in out
    assert "absent on this machine" in out


async def test_the_warning_sits_above_the_body_it_qualifies(monkeypatch):
    """An agent that reads "no collections framework" and then a caveat has
    already decided what to write. The caveat is an instruction about how to
    read what follows, so it goes where instructions go."""
    h = _with_fake(monkeypatch, _fake_note(variants=("1.1",)))
    out = _text_of(await h({"name": "faketool", "version": "9.0"}))
    assert out.index("NOT THE VERSION") < out.index("the 1.1 facts")


async def test_an_older_machine_gets_the_other_warning(monkeypatch):
    h = _with_fake(monkeypatch, _fake_note(variants=("1.1", "1.2")))
    out = _text_of(await h({"name": "faketool", "version": "1.0"}))
    assert "the 1.1 facts" in out
    assert "NOT THE VERSION IN FRONT OF YOU" in out
    assert "may not exist yet" in out


async def test_an_inexact_body_renders_the_version_it_was_measured_on(
        monkeypatch):
    """`Fake 1.3 at D:\\FAKE` over 1.2 facts is the single sentence the
    whole disclosure above it is there to prevent -- a header stating a
    version nobody checked, in the authoritative voice of a measured note."""
    h = _with_fake(monkeypatch, _fake_note(variants=("1.1", "1.2")))
    out = _text_of(await h({"name": "faketool", "version": "1.3"}))
    assert "Fake 1.2 at D:\\FAKE" in out
    assert "Fake 1.3 at" not in out


async def test_the_version_defaults_to_what_the_machine_reported(monkeypatch):
    """Asking the model to retype a number out of its own prompt is asking
    for the wrong note when it fat-fingers one."""
    h = _with_fake(monkeypatch, _fake_note(variants=("1.1", "1.2")),
                   found=(Found("faketool", "toolchain", r"D:\FAKE", True,
                                version="1.2"),))
    out = _text_of(await h({"name": "faketool"}))
    assert "the 1.2 facts" in out
    assert "NOT THE VERSION" not in out


async def test_a_detected_version_nobody_measured_is_disclosed_unprompted(
        monkeypatch):
    """THE PAYOFF, and the reason detection can stop refusing to find newer
    toolchains. Nobody asked about 1.3; the machine said 1.3, the newest
    note is 1.2, and the mismatch is reported without the agent having
    thought to ask. The warning is attributed to the machine rather than to
    the caller, because the caller named nothing."""
    h = _with_fake(monkeypatch, _fake_note(variants=("1.1", "1.2")),
                   found=(Found("faketool", "toolchain", r"D:\FAKE", True,
                                version="1.3"),))
    out = _text_of(await h({"name": "faketool"}))
    assert "NOT THE VERSION IN FRONT OF YOU" in out
    assert "what the machine reports" in out
    assert "what you asked about" not in out


async def test_an_asked_mismatch_is_attributed_to_the_caller(monkeypatch):
    h = _with_fake(monkeypatch, _fake_note(variants=("1.1", "1.2")))
    out = _text_of(await h({"name": "faketool", "version": "1.3"}))
    assert "what you asked about" in out
    assert "what the machine reports" not in out


async def test_a_version_that_is_not_a_version_is_not_an_error(monkeypatch):
    """It is ignored, out loud, and the note still arrives -- with the
    versions that DO exist named, so the next call is right. Refusing would
    cost a round trip to deliver text the agent could have had."""
    h = _with_fake(monkeypatch, _fake_note(base="The base facts.",
                                           variants=("1.1", "1.2")))
    out = await h({"name": "faketool", "version": "latest"})
    assert out.get("isError") is not True
    text = _text_of(out)
    assert "not a version number" in text
    assert "1.1, 1.2" in text
    assert "The base facts." in text


async def test_a_note_that_is_not_filed_by_version_says_that_too(monkeypatch):
    """Which is every shipped profile today. The agent is told the argument
    went nowhere AND that there was nowhere for it to go, rather than being
    left to wonder whether it named the version wrongly."""
    h = _with_fake(monkeypatch, _fake_note(base="The only facts."))
    text = _text_of(await h({"name": "faketool", "version": "???"}))
    assert "not filed by version" in text


async def test_a_variant_shows_its_own_provenance_not_the_bases(monkeypatch):
    """The header's `Measured:` line is the agent's whole basis for acting
    without checking. A variant showing the base's would attribute 1.2 facts
    to a 1.1 machine."""
    h = _with_fake(monkeypatch, _fake_note(base="Base.", base_version="1.1",
                                           variants=("1.2",)))
    out = _text_of(await h({"name": "faketool", "version": "1.2"}))
    assert "a 1.2 box, 2026-10-01" in out
    assert "Fake Compiler 1.2" in out


async def test_a_shipped_profile_resolves_or_discloses(monkeypatch):
    """Resolution against a SHIPPED profile, not an injected one. Succeeds
    test_a_shipped_profile_behaves_exactly_as_it_did, which asserted that a
    version argument had nothing to resolve against -- true only while the
    Python side waited for a profile to declare `for_version`. jdk.toml
    declared "1.1" on 2026-10-01, so the live path is the one worth pinning:
    an exact match stays silent, an inexact one names both numbers, and a
    version that will not parse changes nothing."""
    h = _toolnote(monkeypatch, [JDK])
    plain = _text_of(await h({"name": "jdk"}))

    # 1.1 and 1.1.3 are one claim written two ways: a note measured on "1.1"
    # is claiming the 1.1 line, which is what the profile's title already says
    # in prose, so neither may warn.
    for exact in ("1.1", "1.1.3"):
        out = _text_of(await h({"name": "jdk", "version": exact}))
        assert "NOT THE VERSION" not in out, exact
        assert "no collections framework" in out, exact

    # 1.3 is AHEAD of anything measured. That is the direction that costs a
    # failed build rather than a missed opportunity, so it must name BOTH
    # numbers -- a bare "may not match" is a disclaimer a model cannot act on.
    ahead = _text_of(await h({"name": "jdk", "version": "1.3"}))
    assert "NOT THE VERSION" in ahead
    assert "1.3" in ahead and "1.1" in ahead
    assert "no collections framework" in ahead

    # Junk is ignored, not refused: refusing spends a round trip to withhold
    # text the agent could have had.
    junk = _text_of(await h({"name": "jdk", "version": "junk"}))
    assert "no collections framework" in junk

    # The bare call still renders what it did before the parameter existed.
    assert "JDK 1.1.3, measured on this machine" in plain


# --- the tiers, as the agent reads them -------------------------------------
#
# Three outcomes, three different amounts of noise. The resolution order is
# the user's, 2026-10-01: same version, same minor version, closest with the
# lower one first. What the agent SEES is the half that matters here --
# whether it gets silence, one line, or the banner -- because an agent that
# cannot tell an exact note from a near one believes both the same amount.


def _patch_note():
    """A note documenting two patch releases on different lines."""
    return _notes.Note(
        title="Fake Compiler", measured="a machine, 2026-10-01", body="",
        variants=(_notes.Body("1.1.3", "Fake Compiler 1.1.3",
                              "a 1.1.3 box, 2026-10-01",
                              "Fake {version} at {root}: the 1.1.3 facts."),
                  _notes.Body("1.2.0", "Fake Compiler 1.2.0",
                              "a 1.2.0 box, 2026-10-01",
                              "Fake {version} at {root}: the 1.2.0 facts.")))


async def test_a_patch_release_apart_gets_one_line_and_not_the_banner(
        monkeypatch):
    """THE POINT OF TIER 2. 1.1.3 answering 1.1.8 is the same generation of
    the tool, and shouting "THIS IS NOT THE VERSION IN FRONT OF YOU" at it
    spends the warning that the 1.1-note-on-a-1.3-machine case needs. Crying
    wolf in the cheap case is how the expensive one stops being read."""
    h = _with_fake(monkeypatch, _patch_note(),
                   found=(Found("faketool", "toolchain", r"D:\FAKE", True,
                                version="1.1.8"),))
    out = _text_of(await h({"name": "faketool"}))
    assert "NOT THE VERSION IN FRONT OF YOU" not in out
    assert "A PATCH RELEASE APART" in out
    assert "the 1.1.3 facts" in out


async def test_the_quiet_line_still_names_both_numbers_and_the_line(
        monkeypatch):
    """Quieter is not vaguer. `Measured:` is the agent's whole basis for
    acting without checking, so the gap is stated precisely -- and the claim
    is kept narrow: same line, same era, nobody ran it on your version."""
    h = _with_fake(monkeypatch, _patch_note())
    out = _text_of(await h({"name": "faketool", "version": "1.1.8"}))
    assert "measured on 1.1.3" in out
    assert "1.1.8 is what you asked about" in out
    assert "Both are 1.1" in out
    assert "nobody has run these commands on 1.1.8" in out


async def test_a_patch_match_still_renders_the_version_it_was_measured_on(
        monkeypatch):
    """Softer warning, same discipline about the body. "Fake 1.1.8 ... the
    1.1.3 facts" would be a version claim nobody checked, stated in the
    authoritative voice of a measured note."""
    h = _with_fake(monkeypatch, _patch_note())
    out = _text_of(await h({"name": "faketool", "version": "1.1.8"}))
    assert "Fake 1.1.3 at D:\\FAKE" in out
    assert "Fake 1.1.8 at" not in out


async def test_a_different_line_still_gets_the_banner(monkeypatch):
    """Tier 3 is untouched. 2.0 asked against 1.1.3 and 1.2.0 crosses a
    release line, which is the thing that breaks a build."""
    h = _with_fake(monkeypatch, _patch_note())
    out = _text_of(await h({"name": "faketool", "version": "2.0"}))
    assert "NOT THE VERSION IN FRONT OF YOU" in out
    assert "A PATCH RELEASE APART" not in out
    assert "the 1.2.0 facts" in out


async def test_the_same_line_wins_the_body_and_the_warning_together(
        monkeypatch):
    """Both halves of tier 2 in one call: 1.1.3 answers rather than 1.2.0,
    AND the agent is told it is a patch gap rather than a generation gap. A
    resolution that got the body right and the warning wrong would still be
    the failure this exists to prevent, so both are asserted."""
    h = _with_fake(monkeypatch, _patch_note())
    out = _text_of(await h({"name": "faketool", "version": "1.1.9"}))
    assert "the 1.1.3 facts" in out
    assert "A PATCH RELEASE APART" in out
    assert "NOT THE VERSION IN FRONT OF YOU" not in out


async def test_an_exact_match_is_still_silent(monkeypatch):
    h = _with_fake(monkeypatch, _patch_note())
    out = _text_of(await h({"name": "faketool", "version": "1.2.0"}))
    assert "NOT THE VERSION" not in out
    assert "A PATCH RELEASE APART" not in out
    assert "the 1.2.0 facts" in out


async def test_the_shipped_jdk_line_claim_is_not_qualified(monkeypatch):
    """jdk.toml declares `for_version = "1.1"`, which claims the LINE. Every
    1.1.x must come back unqualified by either warning -- a caveat on the one
    shipped profile that uses the key would undo what the key was added for,
    which was to stop refusing to detect anything newer."""
    h = _toolnote(monkeypatch, [JDK])
    for asked in ("1.1", "1.1.3", "1.1.8"):
        out = _text_of(await h({"name": "jdk", "version": asked}))
        assert "NOT THE VERSION" not in out, asked
        assert "A PATCH RELEASE APART" not in out, asked
        assert "no collections framework" in out, asked


# --- the shell's note is fetched like every other --------------------------
#
# It was resident in the system prompt in full until 2026-10-01, on the
# argument that there is no look-it-up-first moment for a shell. The argument
# held; the remedy did not -- it shipped COMMAND.COM's `start`, `2>&1` and
# file-locking facts to every machine, including ones that may be running
# 4DOS. The fact that cannot wait is now the shell's resident headline and the
# rest arrives through here, so these are about the fetch actually working for
# a name with a dot in it and a `kind` of "shell".

SHELL = Found("command.com", "shell", r"C:\WINDOWS\COMMAND.COM", False)


async def test_the_shell_note_can_be_fetched_by_name(monkeypatch):
    """`command.com` is the name the inventory prints and the name the profile
    file is called. A dot in it is not a path and must not be treated as
    one."""
    h = _toolnote(monkeypatch, [SHELL, MSVC])
    out = _text_of(await h({"name": "command.com"}))
    assert "COMMAND.COM" in out
    # The four facts that stopped being resident. Each one measured, each one
    # now only reachable through this call.
    for fact in ("Too many parameters", "2>&1", "start prog.exe",
                 "Permission denied"):
        assert fact in out, fact


async def test_the_shell_note_is_matched_however_it_is_capitalised(monkeypatch):
    """The model reads `command.com` out of a table and the note itself calls
    it COMMAND.COM. Refusing over the case it picked costs a round trip."""
    h = _toolnote(monkeypatch, [SHELL])
    assert "Too many parameters" in _text_of(await h({"name": "COMMAND.COM"}))


async def test_the_shell_note_says_where_it_was_measured(monkeypatch):
    """Resident text had no room for provenance; fetched text does, and this
    note is the one whose facts were re-measured on real hardware after two
    wrong guesses."""
    out = _text_of(await _toolnote(monkeypatch, [SHELL])({"name":
                                                         "command.com"}))
    assert "Measured:" in out
    assert "Windows 98 SE" in out


async def test_a_refusal_names_the_shell_among_what_is_there(monkeypatch):
    """The refusal lists what the machine has so the next call lands. A shell
    that was excluded from that list would be unreachable in practice: the
    agent would be told the machine has nothing but `msvc`."""
    h = _toolnote(monkeypatch, [SHELL, MSVC])
    out = await h({"name": "bash"})
    assert out["isError"] is True
    assert "command.com" in _text_of(out)


async def test_the_shell_note_is_rendered_against_the_shell_that_was_found(
        monkeypatch):
    """This note has no `{root}` in it, so the risk is the opposite one:
    rendering must not mangle a body with no placeholder, and must not leave a
    stray placeholder behind if one is ever added."""
    h = _toolnote(monkeypatch, [Found("command.com", "shell",
                                      r"D:\DOS\COMMAND.COM", True)])
    out = _text_of(await h({"name": "command.com"}))
    assert r"D:\DOS\COMMAND.COM" in out      # the header names what was found
    assert "{root}" not in out and "{version}" not in out
