"""03.15: the three-timeout ordering PLAN.md demands is held by comments on
both sides of the wire, not enforced anywhere -- "guest ceiling < bridge
default < MCP tool timeout 300000ms" (PLAN.md, section 3.1) is a rule about
two files that have never been compared to each other by a test.

win/rpc/rpc.cpp clamps an EXEC's timeout_ms to
[RPC_EXEC_TIMEOUT_FLOOR_MS, RPC_EXEC_TIMEOUT_CEIL_MS], defaulting to
RPC_EXEC_TIMEOUT_DEFAULT_MS when none is given. bridge/agent/tools.py
hand-mirrors the same three numbers as DEFAULT_TIMEOUT_MS/MAX_TIMEOUT_MS (plus
a hard-coded 1000ms floor) so the bridge clamps before it ever asks the guest
for anything -- which is *why* the guest's own silent clamp (03.15's other
half, fixed separately in rpc.cpp/rpccheck.c) is not normally reachable. If
either side's numbers ever drift apart, that stops being true with nothing to
say so.

This test reads both sources as text -- no Wine, no build -- and fails the
moment they disagree.
"""

from __future__ import annotations

import inspect
import re
from pathlib import Path

from bridge.agent import tools as bridge_tools

import conftest

RPC_CPP = Path(conftest.HERE) / "win" / "rpc" / "rpc.cpp"


def _guest_constant(name: str) -> int:
    src = RPC_CPP.read_text()
    m = re.search(r"#define\s+%s\s+(\d+)" % re.escape(name), src)
    assert m, "no #define %s in %s" % (name, RPC_CPP)
    return int(m.group(1))


def _bridge_mcp_tool_timeout_ms() -> int:
    """The outermost of the three timeouts: the literal `"timeout": N` the
    SDK server config is built with in tools.build(). Read from source,
    rather than calling build() itself, which needs a live bridge and guest
    to construct the MCP server around."""
    src = inspect.getsource(bridge_tools.build)
    m = re.search(r'"timeout":\s*([\d_]+)', src)
    assert m, "no MCP server \"timeout\" literal in tools.build()"
    return int(m.group(1).replace("_", ""))


def test_guest_and_bridge_agree_on_the_exec_timeout_floor():
    assert _guest_constant("RPC_EXEC_TIMEOUT_FLOOR_MS") == 1000
    # The bridge's floor is inline (`max(1000, min(...))`) rather than a named
    # constant; 1000 is asserted directly on both sides rather than parsed
    # out of an expression, which would be fragile for no benefit.
    src = inspect.getsource(bridge_tools)
    assert re.search(r"max\(1000,\s*min\(", src), \
        "bridge/agent/tools.py's floor is no longer the literal 1000"


def test_guest_and_bridge_agree_on_the_exec_timeout_default():
    assert _guest_constant("RPC_EXEC_TIMEOUT_DEFAULT_MS") == \
        bridge_tools.DEFAULT_TIMEOUT_MS


def test_guest_and_bridge_agree_on_the_exec_timeout_ceiling():
    assert _guest_constant("RPC_EXEC_TIMEOUT_CEIL_MS") == \
        bridge_tools.MAX_TIMEOUT_MS


def test_the_three_timeouts_are_ordered():
    """PLAN.md 3.1: guest ceiling < bridge budget < MCP tool timeout. The
    bridge's own asyncio.wait_for budget (server.py) is
    `timeout_ms / 1000.0 + 30.0` seconds, so at the guest's own ceiling that
    budget is (120000/1000 + 30) = 150s -- still well under the MCP tool's
    300s, which is the inequality that actually has to hold to avoid the
    abandoned-call failure PLAN.md 3.1 describes."""
    floor = _guest_constant("RPC_EXEC_TIMEOUT_FLOOR_MS")
    default = _guest_constant("RPC_EXEC_TIMEOUT_DEFAULT_MS")
    ceiling = _guest_constant("RPC_EXEC_TIMEOUT_CEIL_MS")
    mcp_timeout = _bridge_mcp_tool_timeout_ms()

    assert floor < default < ceiling
    bridge_budget_at_ceiling_ms = ceiling + 30_000
    assert bridge_budget_at_ceiling_ms < mcp_timeout
