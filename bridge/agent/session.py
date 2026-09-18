"""Claude Code session, wrapped for the bridge.

One live ClaudeSDKClient at a time. Switching chats closes it and reopens with
resume=<id>; the chat list is read-only history. A warm client pool is roughly
three times the work and nobody notices on a single-user box.

With streaming=True (the default since Phase 2a) on_text is called with token
deltas from StreamEvent; with streaming=False it is called once per completed
text block. Same callback either way, so the bridge does not care.

The one trap: with include_partial_messages the SDK emits BOTH the deltas and
the completed AssistantMessage blocks. Rendering both duplicates every word on
screen, so the completed blocks are skipped while streaming.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from claude_agent_sdk import (
                              AssistantMessage,
                              ClaudeAgentOptions,
                              ClaudeSDKClient,
                              ResultMessage,
                              StreamEvent,
                              SystemMessage,
                              TextBlock,
                              ThinkingBlock,
                              ToolResultBlock,
                              ToolUseBlock,
                              UserMessage,
                              )

log = logging.getLogger("bridge.session")

# Hand-maintained: there is no list_models() in the SDK. Checked 2026-09-16.
# The tool surface this product offers, declared explicitly.
#
# Without this the session inherits whatever the OPERATOR's machine happens to
# be configured with, which was measured to include CronCreate, Workflow,
# Monitor, SendMessage, PushNotification, RemoteTrigger, ScheduleWakeup and
# live claude.ai connectors for Gmail, Google Drive and Calendar. A Windows 98
# client session has no business holding a tool that reads the operator's
# email, and none of that is reproducible between machines.
#
# It also FIXES a real gap rather than only removing things: Glob and Grep were
# not in the default set at all -- they sit behind ToolSearch as deferred tools,
# so the model had to go looking for them and frequently did not. On the guest
# this looked like "tool calls are failing".
#
# `tools` is the AVAILABILITY list. It is a different thing from
# `allowed_tools`, which is the auto-approve list and stays empty.
TOOLS = [
    "Read", "Write", "Edit",      # the staging working copy
    "Glob", "Grep",               # deferred by default; named here so they exist
    "Bash",
    "WebFetch", "WebSearch",      # finding period drivers and documentation
]

# Deliberately NOT offered, and why -- so the next person does not "restore"
# them without knowing what they cost:
#
#   Task            parallel subagents raise parallel permission prompts, and
#                   the guest has one bar on a 640x480 screen
#   NotebookEdit    there is no Jupyter on Windows 98
#   AskUserQuestion } need a UI the guest does not have
#   EnterPlanMode   }
#   Skill, Workflow, Cron*, Monitor, SendMessage, PushNotification,
#   RemoteTrigger, ScheduleWakeup, ListAgents, Task{Output,Stop},
#   Enter/ExitWorktree, DesignSync, ReportFindings
#                   operator-environment features, not part of this product

@dataclass
class Turn:
    """What the UI needs to know when a turn finishes."""

    ok: bool
    subtype: str
    cost_usd: float | None
    num_turns: int
    duration_ms: int


class Session:
    """Wraps one ClaudeSDKClient and pushes events at the caller.

    Callbacks are awaited in the order the SDK produced them, so the bridge can
    stamp seq numbers without racing.
    """

    def __init__(
        self,
        *,
        cwd: str | None = None,
        model: str | None = None,
        effort: str | None = None,
        system_prompt=None,
        resume: str | None = None,
        fork: bool = False,
        allowed_tools: list[str] | None = None,
        tools: list[str] | None = None,
        permission_mode: str = "default",
        can_use_tool=None,
        mcp_servers: dict | None = None,
        streaming: bool = True,
        on_text: Callable[[str], Awaitable[None]] | None = None,
        on_tool: Callable[[str, dict], Awaitable[None]] | None = None,
        on_tool_result: Callable[[str, str, bool], Awaitable[None]] | None = None,
        on_thinking: Callable[[str], Awaitable[None]] | None = None,
        on_system: Callable[[str, dict], Awaitable[None]] | None = None,
    ) -> None:
        # Phase 1 default is an EMPTY allow list, deliberately.
        #
        # The plan review caught that leaving this unset means Claude runs Bash
        # unrestricted on the *bridge host* from the very first demo -- exactly
        # the threat model FEASIBILITY condemns in claudewin9x. Tools arrive in
        # Phase 3, behind a permission prompt, not before.
        self._opts = ClaudeAgentOptions(
            cwd=cwd,
            model=model,
            effort=effort,
            # A preset dict with an "append" extends Claude Code's own prompt.
            # Passing a bare string here would REPLACE it, which would throw
            # away everything that makes Claude Code work.
            system_prompt=system_prompt,
            resume=resume,
            fork_session=fork,
            allowed_tools=allowed_tools if allowed_tools is not None else [],
            # Availability, not permission. See TOOLS above.
            tools=tools if tools is not None else list(TOOLS),
            # Do not inherit the operator's ~/.claude settings: the tool set a
            # Win98 client gets must not depend on whose laptop runs the
            # bridge.
            setting_sources=[],
            # Only the MCP servers passed here, never the ones configured on
            # the operator's account. Verified to keep our own in-process
            # server while dropping the claude.ai connectors.
            strict_mcp_config=True,
            permission_mode=permission_mode,
            can_use_tool=can_use_tool,
            mcp_servers=mcp_servers or {},
            include_partial_messages=streaming,
        )
        self._streaming = streaming
        self._client: ClaudeSDKClient | None = None
        self._pump: asyncio.Task | None = None
        self._session_id: str | None = None
        self._busy = asyncio.Event()

        self.on_text = on_text
        self.on_tool = on_tool
        self.on_tool_result = on_tool_result
        self.on_thinking = on_thinking
        self.on_system = on_system

    # --- lifecycle ---------------------------------------------------------

    @property
    def session_id(self) -> str | None:
        return self._session_id

    @property
    def model(self) -> str | None:
        return self._opts.model

    async def start(self) -> None:
        self._client = ClaudeSDKClient(options=self._opts)
        await self._client.connect()
        log.info("session connected (model=%s effort=%s)",
                 self._opts.model, self._opts.effort)

    async def close(self) -> None:
        if self._pump and not self._pump.done():
            self._pump.cancel()
            try:
                await self._pump
            except asyncio.CancelledError:
                pass
        if self._client:
            try:
                await self._client.disconnect()
            finally:
                self._client = None

    # --- driving -----------------------------------------------------------

    async def send(self, prompt: str) -> Turn:
        """Send one user turn and consume events until the turn ends."""
        if not self._client:
            raise RuntimeError("session not started")

        self._busy.set()
        try:
            await self._client.query(prompt)
            return await self._consume()
        finally:
            self._busy.clear()

    @property
    def busy(self) -> bool:
        """Is a turn running right now?

        Read on reconnect to decide whether the guest's Send button should
        come back disabled: nothing re-sent TURN, so a guest that blipped
        mid-turn returned with Send live while this was still streaming.
        """
        return self._busy.is_set()

    async def interrupt(self) -> None:
        """The Stop button. Safe to call when idle."""
        if self._client and self._busy.is_set():
            await self._client.interrupt()

    async def server_models(self) -> list[dict]:
        """The model list the CLI itself offers, with what each resolves to.

        Better than a hardcoded table in three ways: the names stay correct
        without a release, `resolvedModel` says what "default" actually is, and
        `supportedEffortLevels` says which effort settings a given model even
        has -- Haiku, for instance, has none.
        """
        if not self._client:
            return []
        try:
            info = await self._client.get_server_info()
        except Exception:
            log.exception("get_server_info failed")
            return []
        if not isinstance(info, dict):
            return []
        models = info.get("models")
        return models if isinstance(models, list) else []

    async def set_model(self, model: str) -> None:
        """Live, no fork needed -- verified against SDK 0.2.153."""
        if self._client:
            await self._client.set_model(model)
        self._opts.model = model

    # --- event pump --------------------------------------------------------

    async def _consume(self) -> Turn:
        assert self._client
        async for msg in self._client.receive_response():
            await self._dispatch(msg)
            if isinstance(msg, ResultMessage):
                return Turn(
                    ok=(msg.subtype == "success"),
                    subtype=msg.subtype,
                    cost_usd=getattr(msg, "total_cost_usd", None),
                    num_turns=getattr(msg, "num_turns", 0),
                    duration_ms=getattr(msg, "duration_ms", 0),
                )
        # receive_response() ended without a ResultMessage: the CLI died.
        return Turn(ok=False, subtype="error_stream_ended",
                    cost_usd=None, num_turns=0, duration_ms=0)

    async def _dispatch(self, msg) -> None:
        if isinstance(msg, SystemMessage):
            data = getattr(msg, "data", {}) or {}
            if getattr(msg, "subtype", "") == "init":
                self._session_id = data.get("session_id") or self._session_id
            if self.on_system:
                await self.on_system(getattr(msg, "subtype", ""), data)
            return

        if isinstance(msg, StreamEvent):
            # Token-level deltas. Only present with include_partial_messages.
            # StreamEvent.event is the raw Anthropic SSE event dict.
            ev = msg.event or {}
            if ev.get("type") == "content_block_delta":
                delta = ev.get("delta") or {}
                dtype = delta.get("type")
                if dtype == "text_delta" and self.on_text:
                    await self.on_text(delta.get("text", ""))
                elif dtype == "thinking_delta" and self.on_thinking:
                    await self.on_thinking(delta.get("thinking", ""))
            return

        if isinstance(msg, AssistantMessage):
            for block in msg.content:
                if isinstance(block, TextBlock):
                    # When streaming, the deltas already carried this text --
                    # rendering the completed block too would duplicate every
                    # word on screen.
                    if not self._streaming and self.on_text:
                        await self.on_text(block.text)
                elif isinstance(block, ThinkingBlock):
                    if not self._streaming and self.on_thinking:
                        await self.on_thinking(getattr(block, "thinking", ""))
                elif isinstance(block, ToolUseBlock) and self.on_tool:
                    await self.on_tool(block.name, block.input or {})
            return

        if isinstance(msg, UserMessage):
            # Tool results come back as UserMessages.
            content = msg.content if isinstance(msg.content, list) else []
            for block in content:
                if isinstance(block, ToolResultBlock) and self.on_tool_result:
                    body = block.content
                    if isinstance(body, list):
                        body = "".join(
                            b.get("text", "") for b in body if isinstance(b, dict)
                        )
                    await self.on_tool_result(
                        block.tool_use_id or "",
                        str(body or ""),
                        bool(getattr(block, "is_error", False)),
                    )
            return

        if isinstance(msg, ResultMessage):
            return

        log.debug("unhandled message type: %s", type(msg).__name__)
