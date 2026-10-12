"""Service layering and topological dependency guard.

Ensures that:
1. Every service in bridge/services belongs to an explicit layer (L1..L8).
2. Dependencies flow through constructors in strict topological order (acyclic).
3. No service class holds instance attributes other than its declared constructor
   dependencies, maintaining singleton discipline and scoped state.
   The only allowlisted exception is ConnectionService.guest.
4. Bridge.__init__ constructs services in topological order.
"""

import ast
import importlib
import inspect
import pkgutil

import bridge.services
from bridge.server import Bridge

SERVICE_LAYERS = {
    "BridgeConfig": 1,
    "ConnectionService": 2,
    "PresentationService": 3,
    "WorkspaceService": 3,
    "OptionsService": 3,
    "GuestExecService": 4,
    "PermissionService": 4,
    "SyncService": 4,
    "AttachmentService": 5,
    "SessionService": 6,
    "TurnRunner": 7,
    "SessionCoordinator": 8,
    "Bridge": 9,
}

# Parameter name to layer mapping for constructor dependency inspection
PARAM_TO_SERVICE = {
    "config": "BridgeConfig",
    "connection": "ConnectionService",
    "conn": "ConnectionService",
    "presentation": "PresentationService",
    "workspace": "WorkspaceService",
    "options": "OptionsService",
    "guest_exec": "GuestExecService",
    "permission": "PermissionService",
    "permissions": "PermissionService",
    "sync": "SyncService",
    "attachment": "AttachmentService",
    "session": "SessionService",
    "turn_runner": "TurnRunner",
    "coordinator": "SessionCoordinator",
}

INSTANCE_ATTR_ALLOWLIST = {
    # THE CONNECTION REGISTRY. A service holding state is the thing this
    # test exists to refuse, and this is the one place it is right: the set
    # of attached guests is not state ABOUT a guest, it is the index of
    # them, and it has nowhere else to live. Stage 3c turned the single
    # `guest` slot into a dict keyed on the machine id.
    "ConnectionService._guests",
    # Attachment order, so the temporary `guest` property has a defined
    # answer with more than one. It goes when the property does, in D6.
    "ConnectionService._order",
    "ConnectionService._warned_ambiguous",
    # What exists before a guest does. D5 deletes it.
    "ConnectionService._preattach_synccoord",
    # Which (machine, root) holds the legacy base.json this run. A fact
    # about the STATE DIRECTORY rather than about a guest -- two guests is
    # exactly what makes it a question -- and it has nowhere else to live.
    "SyncService._legacy_owner",
}


def _discover_service_classes():
    """Discover all service classes defined under bridge/services/."""
    classes = {}
    for module_info in pkgutil.iter_modules(bridge.services.__path__):
        mod = importlib.import_module(f"bridge.services.{module_info.name}")
        for attr_name in dir(mod):
            obj = getattr(mod, attr_name)
            if inspect.isclass(obj) and obj.__module__ == mod.__name__:
                if attr_name in SERVICE_LAYERS:
                    classes[attr_name] = obj
    return classes


def _get_init_assigned_attributes(cls):
    """Parse AST of cls.__init__ and return set of self.<attr> assigned."""
    if not hasattr(cls, "__init__"):
        return set()
    try:
        source = inspect.getsource(cls.__init__)
    except (OSError, TypeError):
        return set()
    import textwrap
    tree = ast.parse(textwrap.dedent(source))
    attrs = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Attribute) and isinstance(t.value, ast.Name) and t.value.id == "self":
                    attrs.add(t.attr)
        elif isinstance(node, ast.AnnAssign):
            if isinstance(node.target, ast.Attribute) and isinstance(node.target.value, ast.Name) and node.target.value.id == "self":
                attrs.add(node.target.attr)
    return attrs


def test_service_layers_acyclic():
    """Verify that every discovered service only depends on strictly lower layers."""
    services = _discover_service_classes()
    for name, cls in services.items():
        layer = SERVICE_LAYERS[name]
        sig = inspect.signature(cls.__init__)
        for param_name, param in sig.parameters.items():
            if param_name in ("self", "args", "kwargs"):
                continue
            # Check either parameter annotation or parameter name
            dep_name = None
            if param.annotation != inspect.Parameter.empty:
                ann = param.annotation
                ann_str = ann.__name__ if hasattr(ann, "__name__") else str(ann)
                if ann_str in SERVICE_LAYERS:
                    dep_name = ann_str
            if dep_name is None and param_name in PARAM_TO_SERVICE:
                dep_name = PARAM_TO_SERVICE[param_name]

            if dep_name:
                dep_layer = SERVICE_LAYERS[dep_name]
                assert dep_layer < layer, (
                    f"{name} (L{layer}) depends on {dep_name} (L{dep_layer}), "
                    f"violating topological layering (must depend only on lower layers)"
                )


def test_service_scoped_state_discipline():
    """Verify that services only hold constructor-injected dependencies on self.

    No service is allowed to hold guest or runtime state on instance attributes,
    with the sole documented exception of ConnectionService.guest.
    """
    services = _discover_service_classes()
    for name, cls in services.items():
        if name == "BridgeConfig":
            continue  # Config holds configuration fields
        assigned = _get_init_assigned_attributes(cls)
        sig = inspect.signature(cls.__init__)
        params = set(sig.parameters.keys()) - {"self", "args", "kwargs"}
        for attr in assigned:
            full_name = f"{name}.{attr}"
            if full_name in INSTANCE_ATTR_ALLOWLIST:
                continue
            # Strip leading underscores for private mirrors of injected params (e.g. self._config)
            clean_attr = attr.lstrip("_")
            assert (attr in params or clean_attr in params), (
                f"Scoped state violation: {name}.{attr} is assigned in __init__ but is not "
                f"a declared constructor dependency. Services must be stateless singletons; "
                f"state belongs on GuestSession."
            )


def test_bridge_builds_in_topological_order():
    """Verify that Bridge.__init__ instantiates/assigns services in topological order."""
    source = inspect.getsource(Bridge.__init__)
    import textwrap
    tree = ast.parse(textwrap.dedent(source))

    # Find the order in which services or service attributes are assigned in Bridge.__init__
    service_order = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            # Check target attribute name on self
            target_attrs = []
            if isinstance(node, ast.Assign):
                for t in node.targets:
                    if isinstance(t, ast.Attribute) and isinstance(t.value, ast.Name) and t.value.id == "self":
                        target_attrs.append(t.attr)
            elif isinstance(node, ast.AnnAssign):
                if isinstance(node.target, ast.Attribute) and isinstance(node.target.value, ast.Name) and node.target.value.id == "self":
                    target_attrs.append(node.target.attr)

            for attr in target_attrs:
                clean = attr.lstrip("_")
                if clean in PARAM_TO_SERVICE:
                    svc = PARAM_TO_SERVICE[clean]
                    if svc in SERVICE_LAYERS:
                        service_order.append((svc, SERVICE_LAYERS[svc]))

    # Verify non-decreasing layer order
    current_max_layer = 0
    for svc, layer in service_order:
        assert layer >= current_max_layer, (
            f"Bridge.__init__ assigned {svc} (L{layer}) after a higher-layer service (L{current_max_layer}), "
            f"violating topological construction order."
        )
        current_max_layer = max(current_max_layer, layer)
