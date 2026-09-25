"""TurnRunner: turn orchestration, sync preamble, multimodal turns, and stream lifecycle.

Owns the execution of turns on GuestSession (_run_turn, _run_turn_locked, _resume_turn),
injection of synchronization preambles and delivery tracking (_with_sync_preamble,
_seed_agent_notices, _clear_agent_notices), transcript anchor placement, and turn verdict
calculation (turn_verdict).
"""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING, Any, AsyncIterable, Callable

from ..guest import GuestSession
from ..view import html as H
from ..wire.protocol import (
    Frame,
    anchor_frame,
    pagesealed,
    turn_end,
    turn_error,
    turn_start,
)

if TYPE_CHECKING:
    pass

log = logging.getLogger("bridge.turn")


def turn_verdict(
    ok: bool, degraded: str, produced: bool, subtype: str
) -> tuple[bool, str]:
    """What the status bar should say when a turn ends.

    A turn the SDK says succeeded can still be a failure from the user's
    point of view if it degraded -- rate limited, an unhandled SDK event, or
    interrupted before producing anything -- AND produced nothing. A turn that
    degraded but STILL ANSWERED is reported as success: the user can see the
    answer, and telling them it failed is worse than saying nothing.

    Returns (ok, subtype). `subtype` is the raw string for the TURN frame;
    the guest maps it to a localized line.
    """
    if degraded and not produced:
        return False, degraded
    return ok, subtype


class TurnRunner:
    """Orchestrates turns, sync preambles, and turn stream verdicts."""

    def __init__(
        self,
        config: Any,
        connection: Any,
        presentation: Any,
        attachment: Any,
        sync: Any,
        guest_exec: Any,
        permission: Any,
        options: Any,
        workspace: Any,
        session: Any = None,
        conv: Any = None,
    ) -> None:
        self.config = config
        self.connection = connection
        self.presentation = presentation
        self.attachment = attachment
        self.sync = sync
        self.guest_exec = guest_exec
        self.permission = permission
        self.options = options
        self.workspace = workspace
        self.session = session
        self.conv = conv

    async def _send(self, guest: GuestSession, frame: Frame) -> bool:
        return await self.connection.send(guest, frame)

    def seed_agent_notices(self, guest: GuestSession | None = None) -> None:
        """Fold whatever the baseline still owes the agent into memory."""
        g = guest or getattr(self.connection, "guest", None)
        try:
            load_fn = getattr(self.sync, "load_baseline", None) or getattr(
                self.connection, "_load_baseline", None
            )
            if not load_fn:
                return
            base = load_fn(g)
        except Exception:
            return
        sc = getattr(g, "synccoord", None) or getattr(
            self.connection, "synccoord", None
        )
        if sc is not None:
            for text in base.pending_notice_agent:
                if text not in sc.agent_notices:
                    sc.agent_notices.append(text)

    async def clear_agent_notices(
        self, delivered: list, guest: GuestSession | None = None
    ) -> None:
        """Durable half of with_sync_preamble."""
        sync_dir = (
            getattr(self.config, "sync_dir", None)
            or getattr(self.connection, "sync_dir", None)
        )
        if not delivered or not sync_dir:
            return
        g = guest or getattr(self.connection, "guest", None)
        lock_ctx = getattr(self.sync, "baseline_lock", None) or getattr(
            self.connection, "_baseline_lock", None
        )
        if not lock_ctx:
            return
        async with lock_ctx(g):
            try:
                load_fn = getattr(self.sync, "load_baseline", None) or getattr(
                    self.connection, "_load_baseline", None
                )
                if not load_fn:
                    return
                base = load_fn(g)
            except Exception:
                return
            gone = list(delivered)
            keep = []
            for t in base.pending_notice_agent:
                if t in gone:
                    gone.remove(t)
                else:
                    keep.append(t)
            base.pending_notice_agent = keep
            try:
                base_path_fn = getattr(self.sync, "base_path", None) or getattr(
                    self.connection, "_base_path", None
                )
                path = base_path_fn(g) if base_path_fn else ""
                if path:
                    base.save(path)
            except Exception as exc:
                log.warning("could not record delivered agent notices: %s", exc)

    async def with_sync_preamble(
        self, text: str, guest: GuestSession | None = None
    ) -> str:
        """Prepend anything sync needs the agent to know, and mark it delivered."""
        sync_dir = (
            getattr(self.config, "sync_dir", None)
            or getattr(self.connection, "sync_dir", None)
        )
        g = guest or getattr(self.connection, "guest", None)
        sc = getattr(g, "synccoord", None) or getattr(
            self.connection, "synccoord", None
        )
        if not sync_dir or sc is None or not sc.agent_notices:
            return text
        pending, sc.agent_notices = sc.agent_notices, []
        lines = [
            "[Automatic message from the file synchronisation layer, not "
            "from the user.]",
            "",
        ]
        lines.extend(pending)
        lines.append("")
        lines.append(
            "Deal with this before continuing with whatever was asked. A "
            "conflicted path REFUSES your writes until you call "
            "mcp__machine__sync_resolve on it, so writing to it again is not "
            "something you can do -- read the preserved copy and the ancestor "
            "beside it, work out the merge, and ask the user whether they have "
            "finished with the file. They approve that call on their own "
            "screen, which is how they tell you they are done."
        )
        lines.append("")
        lines.append("--- the user's message follows ---")
        lines.append("")
        await self.clear_agent_notices(pending, guest=g)
        return "\n".join(lines) + text

    async def resume_turn(self, guest: GuestSession, answered) -> None:
        """Continue: wait for the wire to be ours, THEN unlatch, THEN explain."""
        async with guest.turn_lock:
            release_fn = getattr(self.sync, "release_all_latches", None) or getattr(
                self.connection, "_release_all_latches", None
            )
            freed = await release_fn(only=answered) if release_fn else 0
            if freed:
                log.info("sync: released %d latched path(s) on Continue", freed)
            await self.run_turn_locked(
                guest,
                "Resolve the sync conflict, then continue with what you "
                "were doing.",
                echo=False,
            )

    async def run_turn(
        self,
        guest: GuestSession,
        text: str,
        *,
        echo: bool = True,
        attach: str | None = None,
        attach_id: str | None = None,
        attach_name: str | None = None,
    ) -> None:
        """Orchestrate turn start, deferred session resumption, and locking."""
        if not guest.claude:
            resume_fn = (
                getattr(self.session, "resume_pending", None)
                or getattr(self.connection, "_resume_pending", None)
            )
            if resume_fn and not await resume_fn(guest):
                return
        if not guest.claude:
            return
        async with guest.turn_lock:
            await self.run_turn_locked(
                guest,
                text,
                echo=echo,
                attach=attach,
                attach_id=attach_id,
                attach_name=attach_name,
            )

    async def run_turn_locked(
        self,
        guest: GuestSession,
        text: str,
        *,
        echo: bool = True,
        attach: str | None = None,
        attach_id: str | None = None,
        attach_name: str | None = None,
    ) -> None:
        """Locked turn body: multimodal prompt assembly, stream run, verdicts."""
        if not guest.claude:
            return
        display_names = self.attachment.parse_display_names(attach, attach_name)
        display_text = self.attachment.format_display_text(text, display_names)
        anchor = guest.view.pages.mark_user_turn(display_text)
        if echo:
            await self.presentation.write_block(
                guest, H.user_turn(display_text), stream=False
            )
        await self._send(
            guest, anchor_frame(anchor.page, anchor.offset, anchor.preview)
        )
        guest.turn_produced = False
        guest.turn_degraded = ""
        await self._send(guest, turn_start())
        was_command = text.lstrip().startswith("/")
        if not was_command:
            text = await self.with_sync_preamble(text, guest=guest)
        prompt_payload: str | AsyncIterable[dict] = text
        if (attach or attach_id) and not was_command:
            prompt_payload = await self.attachment.assemble_prompt(
                guest,
                text,
                attach=attach,
                attach_id=attach_id,
                attach_name=attach_name,
            )
        try:
            turn = await guest.claude.send(prompt_payload)
        except Exception as exc:
            log.exception("turn failed")
            await self.presentation.flush_text(guest)
            await self._send(guest, turn_error(str(exc)))
            return
        await self.presentation.flush_text(guest)
        if guest.claude:
            conv_store = self.conv or getattr(self.connection, "conv", None)
            if conv_store is not None:
                conv_store.add(guest.claude.session_id)
        sealed = guest.view.pages.message_boundary()
        if sealed:
            await self._send(
                guest, pagesealed(sealed.number, guest.view.pages.count)
            )
        raw, comp = guest.link.zip_out.stats
        if comp:
            log.info(
                "deflate so far: %d -> %d bytes (%.2f:1)",
                raw,
                comp,
                guest.link.zip_out.ratio,
            )
        degraded = guest.turn_degraded
        guest.turn_degraded = ""
        ok, subtype = turn_verdict(
            turn.ok, degraded, guest.turn_produced, turn.subtype
        )
        if ok and was_command and not guest.turn_produced:
            await self.presentation.write_block(
                guest,
                H.notice(
                    "That command finished without sending anything back. Some "
                    "commands only affect the session itself."
                ),
            )
        ctx_pct = await guest.claude.context_usage() if guest.claude else None
        await self._send(
            guest,
            turn_end(
                ok, subtype, turn.cost_usd, turn.duration_ms, ctx_pct
            ),
        )

    # Aliases
    _run_turn = run_turn
    _run_turn_locked = run_turn_locked
    _resume_turn = resume_turn
    _with_sync_preamble = with_sync_preamble
    _clear_agent_notices = clear_agent_notices
    _seed_agent_notices = seed_agent_notices
