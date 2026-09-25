"""Nothing under `bridge/` reaches the composition root by name.

`Bridge` builds the services and owns the process lifecycle. Everything else
takes what it needs through its constructor, so no module below it should be
naming `bridge` and calling a method on it: a service that does is coupled to
the whole object graph, and a method deleted from Bridge breaks it at runtime
rather than at import.

WHY THIS TEST EXISTS. `agent/tools.py` used to be handed the Bridge and call
`bridge.workspace.guest_work_root(...)`, `bridge.guest_exec.run_on_guest(...)` and two more. That
worked only because Bridge forwarded each one to a service. When the services
stopped being reached through it, the closure was left calling methods the
object it had been handed no longer had -- and nothing failed. The unit tests
passed, because they hand `build()` a fake; the goldens passed, because a
prompt does not run a command. It surfaced as the model telling the user
"there appears to be a connection issue with the guest machine setup", on a
640x480 screen, twenty minutes after the suite went green.
"""

import ast
import pathlib

import conftest

ROOT = pathlib.Path(conftest.REPO) / "bridge"

#: `bridge` is also an ordinary local name for a mapping in a couple of
#: places. Anything `dict` answers is not a claim about the composition root.
DICT_ATTRS = set(dir(dict))


def reaches_for_bridge():
    """(file, line, attribute) for every `bridge.X` / `_bridge.X` access."""
    for path in sorted(ROOT.rglob("*.py")):
        if "__pycache__" in path.parts or path.name == "server.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for n in ast.walk(tree):
            if (isinstance(n, ast.Attribute)
                    and isinstance(n.value, ast.Name)
                    and n.value.id in {"bridge", "_bridge"}
                    and n.attr not in DICT_ATTRS):
                yield str(path.relative_to(ROOT)), n.lineno, n.attr


def test_no_module_below_the_composition_root_calls_into_it():
    found = [f"{f}:{l} calls bridge.{a}" for f, l, a in reaches_for_bridge()]
    assert not found, (
        "These reach the composition root by name. Take the service through "
        "the constructor instead -- Bridge is where services are built, not "
        "a directory of them:\n  " + "\n  ".join(found))


def test_the_services_are_reachable_without_a_bridge():
    """The other half. If the rule above held because nothing was wired at
    all, this would notice."""
    from bridge.services.guest_exec import GuestExecService
    from bridge.services.sync import SyncService
    from bridge.services.workspace import WorkspaceService

    for svc, name in ((GuestExecService, "run_on_guest"),
                      (SyncService, "sync_status_text"),
                      (WorkspaceService, "guest_work_root")):
        assert hasattr(svc, name), f"{svc.__name__} lost {name}"
