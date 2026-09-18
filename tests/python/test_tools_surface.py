"""The tool surface is declared, not inherited.

Without an explicit `tools` list a session picks up whatever the OPERATOR's
machine is configured with. Measured on one developer box that was: CronCreate,
CronDelete, CronList, Workflow, Monitor, SendMessage, PushNotification,
RemoteTrigger, ScheduleWakeup, ListAgents, TaskOutput, TaskStop,
EnterWorktree, ExitWorktree, DesignSync, ReportFindings, Skill -- plus live
claude.ai connectors for Gmail, Google Drive and Calendar.

Two problems with that, and the second is the one that was reported as a bug:

  * a Windows 98 client session should not hold a tool that reads the
    operator's email, and nothing about that is reproducible between machines;
  * Glob and Grep were NOT in the default set. They sit behind ToolSearch as
    deferred tools, so the model had to go looking for them and often just gave
    up -- which on the guest looked like "tool calls are failing".
"""

from claude_agent_sdk import ClaudeAgentOptions

from bridge.agent.session import TOOLS, Session


def _opts(**kw) -> ClaudeAgentOptions:
    return Session(**kw)._opts


def test_the_basics_are_present():
    """The gap that was actually reported. Glob and Grep are deferred by
    default, so naming them here is what makes them exist."""
    for name in ("Read", "Write", "Edit", "Glob", "Grep", "Bash"):
        assert name in TOOLS, f"{name} must be offered"


def test_tools_is_passed_as_availability():
    assert _opts().tools == TOOLS


def test_allowed_tools_stays_empty():
    """Availability and permission are different lists. Everything the CLI does
    not resolve locally must still reach the prompt bar."""
    assert _opts().allowed_tools == []


def test_operator_settings_are_not_inherited():
    """setting_sources=[] -- the tool set must not depend on whose laptop is
    running the bridge."""
    assert _opts().setting_sources == []


def test_only_our_own_mcp_servers():
    """strict_mcp_config drops the claude.ai connectors while keeping the
    in-process server Stage B adds. Verified against the real CLI: with it set,
    mcp__machine__exec is present and the connectors are gone."""
    assert _opts().strict_mcp_config is True


def test_expensive_and_impossible_tools_are_not_offered():
    """Each of these costs something concrete on this target."""
    # Parallel subagents raise parallel prompts; the guest has one bar.
    assert "Task" not in TOOLS
    # There is no Jupyter on Windows 98.
    assert "NotebookEdit" not in TOOLS
    # Need a UI the guest does not have.
    assert "AskUserQuestion" not in TOOLS
    # Operator-environment features, not part of this product.
    for name in ("CronCreate", "Workflow", "Monitor", "SendMessage",
                 "PushNotification", "RemoteTrigger", "ScheduleWakeup",
                 "DesignSync", "Skill"):
        assert name not in TOOLS


def test_no_connector_tools_leak_into_the_list():
    assert not [t for t in TOOLS if t.startswith("mcp__claude_ai_")]


def test_caller_can_still_override():
    """Stage B needs to add mcp__machine__* without editing this module."""
    assert _opts(tools=["Read"]).tools == ["Read"]


def test_the_tools_are_bound_to_a_guest_not_just_the_bridge():
    """The MCP closures captured only the Bridge and resolved `_guest` at call
    time, so a tool addressed "whichever machine is attached right now".

    With one client that is the same machine. With two it is not, and the
    failure is the worst shape available: the command runs on the WRONG
    machine while the permission prompt asking for it appears on the RIGHT
    person's screen -- so it looks approved because it was approved, for
    something else. Three call sites today, eleven after Phase 6.
    """
    import inspect
    from bridge.agent import tools

    sig = inspect.signature(tools.build)
    assert "guest" in sig.parameters, \
        "tools.build must take the guest its tools address"

    from bridge.server import Bridge
    for name in ("run_on_guest", "sync_status_text", "sync_resolve"):
        params = inspect.signature(getattr(Bridge, name)).parameters
        assert "guest" in params, (
            f"Bridge.{name} still resolves the guest itself, so the tool "
            f"cannot say which machine it means")
