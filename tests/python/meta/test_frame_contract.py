"""Contract test: every frame type the bridge sends to the guest is handled by the guest.

Audit 04.4: 03.5 found that three frame types (TOOLRESULT, RESYNC, PONG) sent by the
bridge had no handler on the guest and fell into render_frame's unhandled log line.
While those instances were resolved, finding 04.4 noted that the defect class remained
open without an automated contract test verifying that the bridge's outbound vocabulary
is completely covered by the guest's dispatch tables.

This test parses the guest's dispatch tables out of win/app/mainwindow.cpp, win/net/frameproto.cpp,
and win/sync/syncrpc.cpp, and diffs them against the bridge's outbound Frame() constructors
in bridge/wire/protocol.py and bridge/files/synctransport.py.
"""

import ast
import os
import re

import conftest

REPO = conftest.REPO
MAIN_CPP = os.path.join(REPO, "win", "app", "mainwindow.cpp")
FRAMEPROTO_CPP = os.path.join(REPO, "win", "net", "frameproto.cpp")
SYNCRPC_CPP = os.path.join(REPO, "win", "sync", "syncrpc.cpp")
PROTOCOL_PY = os.path.join(REPO, "bridge", "wire", "protocol.py")
SYNCTRANSPORT_PY = os.path.join(REPO, "bridge", "files", "synctransport.py")


def guest_handled_types() -> set[str]:
    """Parse all frame types the guest handles across UI, RPC worker, and sync lanes."""
    handled = set()

    # 1. UI thread dispatch table: g_dispatch[] in win/app/mainwindow.cpp
    main_src = open(MAIN_CPP, encoding="utf-8").read()
    m = re.search(r'g_dispatch\[\]\s*=\s*\{([^;]+)\};', main_src, re.S)
    assert m, f"Could not find g_dispatch[] in {MAIN_CPP}"
    for row in re.finditer(r'\{\s*"([A-Z0-9_]+)"\s*,', m.group(1)):
        handled.add(row.group(1))

    # 2. Worker/Immediate lane table: RC_LANE_TABLE in win/net/frameproto.cpp
    proto_src = open(FRAMEPROTO_CPP, encoding="utf-8").read()
    m2 = re.search(r'RC_LANE_TABLE\[\]\s*=\s*\{([^;]+)\};', proto_src, re.S)
    assert m2, f"Could not find RC_LANE_TABLE[] in {FRAMEPROTO_CPP}"
    for row in re.finditer(r'\{\s*"([A-Z0-9_]+)"\s*,', m2.group(1)):
        handled.add(row.group(1))

    # 3. Sync lane handlers: rc_syncrpc_handle in win/sync/syncrpc.cpp
    sync_src = open(SYNCRPC_CPP, encoding="utf-8").read()
    for row in re.finditer(r'strcmp\(f->type,\s*"([A-Z0-9_]+)"\)', sync_src):
        handled.add(row.group(1))

    return handled


def bridge_outbound_types() -> set[str]:
    """Parse all frame types the bridge sends to the guest."""
    types = set()

    for path in (PROTOCOL_PY, SYNCTRANSPORT_PY):
        tree = ast.parse(open(path, encoding="utf-8").read(), path)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "Frame":
                if node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
                    types.add(node.args[0].value)

    # HELLO is sent by both ends at handshake; bridge sends it as initial greeting
    # which guest consumes during connection handshake (before entering render loop).
    return types


def test_every_frame_type_bridge_sends_is_handled_by_guest():
    """Every frame the bridge emits towards the guest must be explicitly handled."""
    guest_types = guest_handled_types()
    bridge_types = bridge_outbound_types()

    # HELLO is consumed during handshake in net.cpp do_connect/rc_net_start, not in dispatch loop
    bridge_to_guest_runtime = bridge_types - {"HELLO"}

    unhandled = bridge_to_guest_runtime - guest_types
    assert not unhandled, (
        f"The bridge sends frame types that the guest has no dispatch row or handler for: "
        f"{sorted(unhandled)}. The guest would drop these or log 'unhandled frame type'."
    )


def test_guest_handles_expected_core_vocabulary():
    """Sanity check that the guest handles all standard frames."""
    guest_types = guest_handled_types()
    required = {
        "READY", "DOC", "TEXT", "TURN", "OPTION", "OPTIONSEND", "ERROR", "NOTICE",
        "EXEC", "EXECKILL", "SYNCSCAN", "SYNCPUT", "SYNCPUTEND", "SYNCMKDIR", "SYNCGET"
    }
    missing = required - guest_types
    assert not missing, f"Guest missing handlers for core frames: {missing}"
