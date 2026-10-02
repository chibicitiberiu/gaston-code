"""SessionCoordinator: guest lifecycle, turn quiescing, session transitions, and state recovery.

Owns the coordination of guest session transitions against in-flight turns and sync states:
quiescing running turns (quiesce_turn), session switching and creation with quiescing
(switch, new_session, set_effort), syncing guest UI state on reconnect (reissue_halt,
reissue_turn, sync_state_to_guest), and inbound frame routing for turn-affecting messages
(on_input, on_resume, on_interrupt).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Any

from ..guest import GuestSession
from ..status import GuestState
from ..wire import fold
from ..wire.protocol import Frame, error, halt, haltend, turn_start

log = logging.getLogger("bridge.coordinator")


class SessionCoordinator:
    """Coordinates guest lifecycle transitions with active turn and sync states."""

    def __init__(
        self,
        connection: Any,
        turn_runner: Any,
        sync: Any,
        permission: Any,
        session: Any,
        workspace: Any = None,
        guest_exec: Any = None,
        attachment: Any = None,
        spawn: Callable[[Any], None] | None = None,
    ) -> None:
        self.connection = connection
        self.turn_runner = turn_runner
        self.sync = sync
        self.permission = permission
        self.session = session
        self.workspace = workspace
        self.guest_exec = guest_exec
        self.attachment = attachment
        # Defaulted, not Optional: every caller supplies Bridge._spawn,
        # and a None here would only ever be a TypeError at the first
        # turn rather than at construction.
        self.spawn: Callable[[Any], Any] = spawn or asyncio.create_task


    def _spawn_coro(self, coro: Any) -> None:
        self.spawn(coro)

    async def quiesce_turn(self, guest: GuestSession) -> None:
        """Stop whatever turn is running, so turn_lock can be had."""
        if not guest.turn_lock.locked():
            return
        log.info("interrupting the live turn before the conversation changes")
        for exec_id in list(guest.exec_pending):
            self.guest_exec.kill_exec(guest, exec_id)
        if guest.claude:
            try:
                await guest.claude.interrupt()
            except Exception:
                log.exception("could not interrupt the running turn")

    async def set_effort(self, guest: GuestSession, effort: str) -> None:
        """Quiesce live turn before switching effort mode under lock."""
        await self.quiesce_turn(guest)
        async with guest.turn_lock:
            await self.session.set_effort_locked(guest, effort)

    async def new_session(self, guest: GuestSession, folder: str = "") -> None:
        """Quiesce live turn before creating new session under lock."""
        await self.quiesce_turn(guest)
        async with guest.turn_lock:
            await self.session.new_session_locked(guest, folder=folder)

    async def switch(self, guest: GuestSession, session_id: str) -> None:
        """Quiesce live turn before switching conversation under lock."""
        await self.quiesce_turn(guest)
        async with guest.turn_lock:
            await self.session.switch_locked(guest, session_id)

    async def reissue_halt(self, guest: GuestSession) -> None:
        """After a reconnect, put the Continue bar back."""
        sc = getattr(guest, "synccoord", None) or getattr(
            self.connection, "synccoord", None
        )
        if not sc or not sc.halt_id:
            return
        log.info("re-issuing halt %s after reconnect", sc.halt_id)
        await self.connection.send(guest, halt(sc.halt_id, sc.halt_text))

    async def reissue_turn(self, guest: GuestSession) -> None:
        """After a reconnect, restore the busy UI if a turn is still running."""
        session = guest.claude
        if session is None or not getattr(session, "busy", False):
            return
        log.info("re-issuing turn state after reconnect")
        guest.turn_produced = False
        guest.turn_degraded = ""
        guest.turn_stopped = False
        await self.connection.send(guest, turn_start())

    async def sync_state_to_guest(self, guest: GuestSession) -> None:
        """Sync guest UI state (permissions, sync halt bar, turn status) after reconnect."""
        guest.view.view_page = None
        if guest.perms:
            await guest.perms.reissue()
        await self.reissue_halt(guest)
        await self.reissue_turn(guest)

    async def on_switch(self, guest: GuestSession, f: Frame) -> None:
        await self.switch(guest, f.headers.get("id", ""))

    async def on_newsession(self, guest: GuestSession, f: Frame) -> None:
        await self.new_session(guest, f.headers.get("folder", ""))

    async def on_input(self, guest: GuestSession, f: Frame) -> None:
        """Handle inbound INPUT frame."""
        # `guest.state` directly, NOT getattr with a default. The property
        # derives from four fields; when one of them was renamed it began
        # raising AttributeError, `getattr(..., None)` swallowed that, and
        # every gate below silently stopped firing. A broken projection has
        # to be an error, not "no state".
        state = guest.state
        if state == GuestState.RUNNING_TURN:
            log.warning(
                "rejected INPUT while turn is active (state=%s)",
                getattr(guest, "state_detail", "turn-running"),
            )
            # NOT `busy`: that code is the handshake's "another client holds
            # this session", which closes the link. This one keeps it, and a
            # guest that cannot tell them apart would report the wrong thing.
            await self.connection.send(
                guest, error("turn_busy", "A prompt is already running."))
            return
        if state == GuestState.HALTED:
            log.warning(
                "rejected INPUT while session is halted (state=%s)",
                getattr(guest, "state_detail", "halted"),
            )
            await self.connection.send(
                guest, error("halted", "Session is halted awaiting resolution.")
            )
            return
        if state == GuestState.DISCONNECTED:
            log.warning("rejected INPUT while disconnected")
            return

        text = fold.decode(f.body)
        log.info("input: %s", text[:120])
        attach = f.headers.get("attach")
        attach_id = f.headers.get("attach_id")
        attach_name = f.headers.get("attach_name")
        if not guest.work_guest and not guest.first_prompt:
            guest.first_prompt = text
        # What the Auto-mode reviewer judges a machine call against
        # (agent/review.py): the person's own recent words.
        guest.recent_prompts = [*guest.recent_prompts[-4:], text]
        if attach:
            await self.workspace.ensure_work_dir(guest)
            for part in attach.split("|"):
                part = part.strip()
                if not part:
                    continue
                target = self.attachment.validate_attach_target(guest, part)
                if not target:
                    log.warning("rejected unsafe attachment path: %s", part)
                    await self.connection.send(
                        guest,
                        error(
                            "bad_attach",
                            f"Attachment outside work directory: {part}",
                        ),
                    )
                    return
        self._spawn_coro(
            self.turn_runner.run_turn(
                guest,
                text,
                attach=attach,
                attach_id=attach_id,
                attach_name=attach_name,
            )
        )

    async def on_resume(self, guest: GuestSession, f: Frame) -> None:
        """Handle inbound RESUME frame (user pressed Continue on sync halt)."""
        sc = getattr(guest, "synccoord", None) or getattr(
            self.connection, "synccoord", None
        )
        if not sc or not (f.headers.get("id", "") == sc.halt_id and sc.halt_id):
            return
        hid, sc.halt_id = sc.halt_id, ""
        log.info("user pressed Continue on the sync halt")
        await self.connection.send(guest, haltend(hid))
        answered, sc.halt_paths = sc.halt_paths, frozenset()
        self._spawn_coro(self.turn_runner.resume_turn(guest, answered))

    async def on_interrupt(
        self, guest: GuestSession, f: Frame | None = None
    ) -> None:
        """Handle inbound INTERRUPT frame (user pressed Stop)."""
        guest.turn_stopped = True
        for exec_id in list(guest.exec_pending):
            self.guest_exec.kill_exec(guest, exec_id)
        if guest.claude:
            await guest.claude.interrupt()

