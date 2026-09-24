"""SessionCoordinator: guest lifecycle, turn quiescing, session transitions, and state recovery.

Owns the coordination of guest session transitions against in-flight turns and sync states:
quiescing running turns (_quiesce_turn), session switching and creation with quiescing
(_switch, _new_session, _set_effort), syncing guest UI state on reconnect (_reissue_halt,
_reissue_turn, sync_state_to_guest), and inbound frame routing for turn-affecting messages
(_on_input, _on_resume, _on_interrupt).
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any, Callable

from ..status import GuestState
from ..wire import fold
from ..wire.protocol import Frame, error, halt, haltend, turn_start

if TYPE_CHECKING:
    from ..server import GuestSession

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
        spawn: Callable[[Any], None] | None = None,
    ) -> None:
        self.connection = connection
        self.turn_runner = turn_runner
        self.sync = sync
        self.permission = permission
        self.session = session
        self.workspace = workspace
        self.guest_exec = guest_exec
        self.spawn = spawn

    async def _send(self, guest: GuestSession, frame: Frame) -> bool:
        if hasattr(self.connection, "send"):
            return await self.connection.send(guest, frame)
        return await self.connection._send(guest, frame)

    def _spawn_coro(self, coro: Any) -> None:
        if self.spawn:
            self.spawn(coro)
        elif hasattr(self.connection, "_spawn"):
            self.connection._spawn(coro)
        else:
            asyncio.create_task(coro)

    async def quiesce_turn(self, guest: GuestSession) -> None:
        """Stop whatever turn is running, so turn_lock can be had."""
        if not guest.turn_lock.locked():
            return
        log.info("interrupting the live turn before the conversation changes")
        for exec_id in list(guest.exec_pending):
            if self.guest_exec:
                self.guest_exec.kill_exec(guest, exec_id)
            elif hasattr(self.connection, "_kill_exec"):
                self.connection._kill_exec(guest, exec_id)
        if guest.claude:
            try:
                await guest.claude.interrupt()
            except Exception:
                log.exception("could not interrupt the running turn")

    async def set_effort(self, guest: GuestSession, effort: str) -> None:
        """Quiesce live turn before switching effort mode under lock."""
        await self.quiesce_turn(guest)
        async with guest.turn_lock:
            if hasattr(self.session, "set_effort_locked"):
                await self.session.set_effort_locked(guest, effort)
            elif hasattr(self.connection, "_set_effort_locked"):
                await self.connection._set_effort_locked(guest, effort)

    async def new_session(self, guest: GuestSession, folder: str = "") -> None:
        """Quiesce live turn before creating new session under lock."""
        await self.quiesce_turn(guest)
        async with guest.turn_lock:
            if hasattr(self.session, "new_session_locked"):
                await self.session.new_session_locked(guest, folder=folder)
            elif hasattr(self.connection, "_new_session_locked"):
                await self.connection._new_session_locked(guest, folder=folder)

    async def switch(self, guest: GuestSession, session_id: str) -> None:
        """Quiesce live turn before switching conversation under lock."""
        await self.quiesce_turn(guest)
        async with guest.turn_lock:
            if hasattr(self.session, "switch_locked"):
                await self.session.switch_locked(guest, session_id)
            elif hasattr(self.connection, "_switch_locked"):
                await self.connection._switch_locked(guest, session_id)

    async def reissue_halt(self, guest: GuestSession) -> None:
        """After a reconnect, put the Continue bar back."""
        sc = getattr(guest, "synccoord", None) or getattr(
            self.connection, "synccoord", None
        )
        if not sc or not sc.halt_id:
            return
        log.info("re-issuing halt %s after reconnect", sc.halt_id)
        await self._send(guest, halt(sc.halt_id, sc.halt_text))

    async def reissue_turn(self, guest: GuestSession) -> None:
        """After a reconnect, restore the busy UI if a turn is still running."""
        session = guest.claude
        if session is None or not getattr(session, "busy", False):
            return
        log.info("re-issuing turn state after reconnect")
        guest.turn_produced = False
        guest.turn_degraded = ""
        await self._send(guest, turn_start())

    async def sync_state_to_guest(self, guest: GuestSession) -> None:
        """Sync guest UI state (permissions, sync halt bar, turn status) after reconnect."""
        guest.view_page = None
        if guest.perms:
            await guest.perms.reissue()
        await self.reissue_halt(guest)
        await self.reissue_turn(guest)

    async def on_input(self, guest: GuestSession, f: Frame) -> None:
        """Handle inbound INPUT frame."""
        state = getattr(guest, "state", None)
        if state == GuestState.RUNNING_TURN:
            log.warning(
                "rejected INPUT while turn is active (state=%s)",
                getattr(guest, "state_detail", "turn-running"),
            )
            await self._send(guest, error("busy", "A prompt is already running."))
            return
        if state == GuestState.HALTED:
            log.warning(
                "rejected INPUT while session is halted (state=%s)",
                getattr(guest, "state_detail", "halted"),
            )
            await self._send(
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
        if attach:
            ensure_fn = getattr(self.workspace, "ensure_work_dir", None) or getattr(
                self.connection, "_ensure_work_dir", None
            )
            if ensure_fn:
                await ensure_fn(guest)
            for part in attach.split("|"):
                part = part.strip()
                if not part:
                    continue
                validate_fn = getattr(
                    self.workspace, "validate_attach_target", None
                ) or getattr(self.connection, "_validate_attach_target", None)
                target = validate_fn(guest, part) if validate_fn else part
                if not target:
                    log.warning("rejected unsafe attachment path: %s", part)
                    await self._send(
                        guest,
                        error(
                            "bad_attach",
                            f"Attachment outside work directory: {part}",
                        ),
                    )
                    return
        run_fn = getattr(self.turn_runner, "run_turn", None) or getattr(
            self.connection, "_run_turn", None
        )
        self._spawn_coro(
            run_fn(
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
        await self._send(guest, haltend(hid))
        answered, sc.halt_paths = sc.halt_paths, frozenset()
        resume_fn = getattr(self.turn_runner, "resume_turn", None) or getattr(
            self.connection, "_resume_turn", None
        )
        self._spawn_coro(resume_fn(guest, answered))

    async def on_interrupt(
        self, guest: GuestSession, f: Frame | None = None
    ) -> None:
        """Handle inbound INTERRUPT frame (user pressed Stop)."""
        for exec_id in list(guest.exec_pending):
            if self.guest_exec:
                self.guest_exec.kill_exec(guest, exec_id)
            elif hasattr(self.connection, "_kill_exec"):
                self.connection._kill_exec(guest, exec_id)
        if guest.claude:
            await guest.claude.interrupt()

    # Aliases
    _quiesce_turn = quiesce_turn
    _set_effort = set_effort
    _new_session = new_session
    _switch = switch
    _reissue_halt = reissue_halt
    _reissue_turn = reissue_turn
    _on_input = on_input
    _on_resume = on_resume
    _on_interrupt = on_interrupt
