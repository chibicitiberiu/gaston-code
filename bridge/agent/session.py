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
import contextlib
import logging
from collections.abc import AsyncIterable, Awaitable, Callable
from dataclasses import dataclass
from typing import get_args

from claude_agent_sdk import (
                              AssistantMessage,
                              ClaudeAgentOptions,
                              ClaudeSDKClient,
                              ConversationResetMessage,
                              EffortLevel,
                              PermissionMode,
                              RateLimitEvent,
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

#: The SDK's own admissible values (claude_agent_sdk.types), not
#: services/options.py's EFFORT_ORDER/MODE_ORDER: those are the GUEST
#: MENU's presentation order, a subset a particular model may support, and
#: MODE_ORDER in particular never lists "bypassPermissions" or "dontAsk" --
#: real SDK modes the menu just does not offer. Importing them here would
#: also cycle: services/options.py -> ..guest -> .agent.session.
_EFFORT_LEVELS: tuple[EffortLevel, ...] = get_args(EffortLevel)
_PERMISSION_MODES: tuple[PermissionMode, ...] = get_args(PermissionMode)


def _as_effort(effort: str | None) -> EffortLevel | None:
    """A caller's plain string, narrowed to the SDK's Literal, or None.

    `effort` reaches here untyped -- guest.effort, restored settings, the
    CLI's own default -- and nothing upstream validates it against the SDK's
    five known levels the way OptionsService.set_mode does for permission
    modes. Anything unrecognised is folded into None, same as "": both mean
    "no preference" to ClaudeAgentOptions.
    """
    if effort in _EFFORT_LEVELS:
        return effort
    return None


def _as_permission_mode(mode: str) -> PermissionMode:
    """A caller's plain string, narrowed to the SDK's Literal.

    Falls back to "default", the same value ClaudeAgentOptions itself
    defaults to. OptionsService.set_mode already refuses an unknown mode
    before guest.perm_mode is ever set to it, so in practice this never
    trips -- it exists so the boundary in front of the SDK call is a real
    check rather than an assumption about every caller.
    """
    if mode in _PERMISSION_MODES:
        return mode
    return "default"

# The tool surface this product offers, declared explicitly -- there is no
# list_models() equivalent in the SDK to read it from (checked 2026-09-16).
#
# Without this the session inherits whatever the OPERATOR's machine happens to
# be configured with: CronCreate, Workflow, Monitor, SendMessage,
# PushNotification, RemoteTrigger, ScheduleWakeup and live claude.ai
# connectors for Gmail, Drive and Calendar were all measured present. A
# Windows 98 client session has no business holding a tool that reads the
# operator's email, and none of that is reproducible between machines.
#
# It also fixes a real gap: Glob and Grep were not in the default set at all
# -- they sit behind ToolSearch as deferred tools, so the model had to go
# looking for them and frequently did not, which looked like "tool calls are
# failing" on the guest.
#
# `tools` is the AVAILABILITY list, distinct from `allowed_tools` (the
# auto-approve list, which stays empty).
TOOLS = [
    "Read", "Write", "Edit",      # the staging working copy
    "Glob", "Grep",               # deferred by default; named here so they exist
    "Bash",
    "WebFetch", "WebSearch",      # finding period drivers and documentation
    # Plan first's way out. Offered in every mode -- the tool list is fixed
    # when a session is built and the mode is not -- and harmless outside
    # Plan: the desk asks, and approving it changes nothing there. Measured
    # 2026-10-04: in plan mode it reaches `can_use_tool` with the plan text,
    # which is what makes the bar the approval.
    "ExitPlanMode",
]

# Deliberately NOT offered, and why -- so the next person does not "restore"
# them without knowing what they cost:
#
#   Task            parallel subagents raise parallel permission prompts, and
#                   the guest has one bar on a 640x480 screen
#   NotebookEdit    there is no Jupyter on Windows 98
#   AskUserQuestion } need a UI the guest does not have; the Mode menu is how
#   EnterPlanMode   } Plan first is entered
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
        cli_path: str | None = None,
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
        # EMPTY allow list, deliberately: leaving this unset means Claude runs
        # Bash unrestricted on the *bridge host*, exactly the threat model
        # FEASIBILITY condemns in claudewin9x. Tools arrive later, behind a
        # permission prompt, not before.
        self._opts = ClaudeAgentOptions(
            cwd=cwd,
            model=model,
            # Which Claude Code, decided once at startup by
            # bridge.claudecli and carried here so the version in the
            # startup line is the version that runs. None hands the
            # question back to the SDK, which is what every test does.
            cli_path=cli_path,
            # None, never "". OptionsService.effective_effort ends in `or ""`
            # so a per-guest setting can fall back to the CLI default, but an
            # empty string handed straight through made the CLI print
            # `Warning: Unknown --effort value '' -- ignoring it` on every
            # start -- harmless, and exactly the kind of warning people learn
            # to read past. Empty means "no preference", said by not saying
            # anything; `_as_effort` folds any other unrecognised value into
            # the same None, which the SDK's own Literal type now requires.
            effort=_as_effort(effort),
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
            permission_mode=_as_permission_mode(permission_mode),
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
            with contextlib.suppress(asyncio.CancelledError):
                await self._pump
        if self._client:
            try:
                await self._client.disconnect()
            finally:
                self._client = None

    # --- driving -----------------------------------------------------------

    async def context_usage(self) -> int | None:
        """How full the context window is, 0-100, or None.

        `get_context_usage()` is a LIVE control request -- the same data the
        CLI's /context shows -- and `percentage` is already computed against
        the real ceiling.

        The obvious alternative is wrong and would ship visibly broken:
        ResultMessage.model_usage carries `contextWindow` and the token
        counts, but those are CUMULATIVE OVER THE TURN. A turn with twelve
        tool calls re-sends the context each time, so summing inputTokens +
        cacheReadInputTokens + cacheCreationInputTokens exceeds the window
        several times over and the gauge would read past 100% on any real
        turn.

        Never raises: this is decoration on a status bar, and an exception
        here would abort a turn that has already finished.
        """
        if not self._client:
            return None
        try:
            usage = await self._client.get_context_usage()
        except Exception as exc:
            log.debug("context usage unavailable: %s", exc)
            return None
        if not usage:
            return None
        try:
            pct = float(usage.get("percentage") or 0.0)
        except (TypeError, ValueError):
            return None
        if pct < 0:
            return 0
        return int(pct + 0.5) if pct <= 100 else 100

    async def send(self, prompt: str | AsyncIterable[dict]) -> Turn:
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

    async def server_commands(self) -> list[dict]:
        """The slash commands the CLI offers, as {name, description,
        argumentHint}.

        Free, unlike it looks: get_server_info() returns the initialization
        result the SDK already holds from connect(), so this shells out to
        nothing.

        There is NO companion list saying which of these a remote UI should
        hide. `terminal_slash_commands` was assumed to exist and does not --
        measured against this SDK, get_server_info() returns account, agents,
        commands, current_permission_mode, models, output_style, session_state
        and a dozen more, and no key like it. 53 commands came back, most of
        them meaningless on a Win98 client (plugins, cloud workflows, a heap
        dump to the operator's Desktop). Whatever picks a subset has to be
        ours.
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
        cmds = info.get("commands")
        return cmds if isinstance(cmds, list) else []

    async def set_model(self, model: str) -> None:
        """Live, no fork needed -- verified against SDK 0.2.153."""
        if self._client:
            await self._client.set_model(model)
        self._opts.model = model

    async def set_permission_mode(self, mode: str) -> None:
        """Live, like set_model, and for the same reason: streaming.

        The SDK's set_permission_mode is documented as working only in
        streaming mode, which is what this class always uses
        (`include_partial_messages=streaming`, default True). `_opts` is
        updated as well so a session rebuilt from these options -- the effort
        fork does exactly that -- does not silently drop back to "default".
        """
        pm = _as_permission_mode(mode)
        if self._client:
            await self._client.set_permission_mode(pm)
        self._opts.permission_mode = pm

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
                    # Deltas already carried this text while streaming.
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

        # RateLimitEvent is INFORMATIONAL: the CLI emits it on every
        # rate-limit status TRANSITION, including into "allowed", and only
        # `status == "rejected"` means anything was actually refused. Falling
        # through to the catch-all below would mark a fine turn degraded (and
        # on an output-less turn, e.g. /compact, turn_verdict would call it
        # failed). Note the nesting -- msg.rate_limit_info.status, not
        # msg.status.
        if isinstance(msg, RateLimitEvent):
            info = getattr(msg, "rate_limit_info", None)
            status = str(getattr(info, "status", "") or "")
            log.info("rate limit: status=%s utilization=%s", status,
                     getattr(info, "utilization", None))
            if self.on_system:
                await self.on_system("rate_limit", {
                    "status": status,
                    "utilization": getattr(info, "utilization", None),
                    "resets_at": getattr(info, "resets_at", None),
                })
            return

        # Emitted after /clear. The other live fall-through, and it would
        # have misfired exactly the same way: a reset produces no text and no
        # tool call, so it looked like a degraded turn that answered nothing.
        if isinstance(msg, ConversationResetMessage):
            new_id = getattr(msg, "new_conversation_id", "") or ""
            log.info("conversation reset: new id %s", new_id or "(none)")
            if new_id:
                self._session_id = new_id
            if self.on_system:
                await self.on_system("conversation_reset",
                                     {"new_conversation_id": new_id})
            return

        # NOT log.debug: an unrecognised SDK message type ending a turn with
        # no text and no tool call (RateLimitEvent was one, before the branch
        # above) used to leave the user staring at an idle app with nothing
        # on screen to say why -- silently breaking CLAUDE.md's "the person at
        # the keyboard never loses a byte". No test in this suite can catch
        # this class of bug; it needs the live SDK in a state nothing here
        # fakes. Loud in the log AND handed to on_system so the bridge can put
        # it in the transcript -- a type we don't understand is something the
        # user should be told about, not protected from.
        name = type(msg).__name__
        log.warning("unhandled SDK message type: %s", name)
        if self.on_system:
            await self.on_system("sdk_event", {"event": name})
