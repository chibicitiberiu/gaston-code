"""PermissionService: guest tool-permission prompts and queue management.

Coordinates the PermissionDesk lifecycle for guest sessions, including desk
creation/attachment, reply routing (PERMREPLY), prompt reissuance on reconnect,
and bulk prompt denial on teardown or session switch.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from ..agent import categories, policy, review
from ..agent import permissions as perms
from ..agent.permdesk import PermissionDesk
from ..guest import GuestSession
from ..wire.protocol import Frame

log = logging.getLogger("bridge.permission")


class PermissionService:
    """Manages guest permission desk creation, reply routing, reissue, and denial."""

    def __init__(
        self,
        connection: Any,
        *,
        presentation: Any = None,
        workspace: Any = None,
        spawn: Any = None,
        guest_fs: Any = None,
        on_mode: Any = None,
    ) -> None:
        self.connection = connection
        #: `async (guest) -> None` when the desk changes a chat's mode.
        self.on_mode = on_mode
        #: For the bar's preview of what a delete, copy or move touches.
        self.guest_fs = guest_fs
        self.presentation = presentation
        self.workspace = workspace
        self.spawn = spawn or asyncio.create_task

    def attach(
        self, guest: GuestSession, *, cwd: str | None = None
    ) -> PermissionDesk:
        """Create or return the PermissionDesk for a guest session."""
        if guest.perms is None:
            guest.perms = PermissionDesk(
                guest,
                cwd=cwd
                or (self.workspace.work_dir(guest) if self.workspace else None),
                write_block=lambda html: (
                    self.presentation.write_block(guest, html)
                    if self.presentation
                    else None
                ),
                send=lambda frame: self.connection.send(guest, frame),
                spawn=self.spawn,
                ensure_work=(
                    (lambda: self.workspace.ensure_work_dir(guest))
                    if self.workspace
                    else None
                ),
                preview=lambda tool, args: review.preview(
                    self.guest_fs, guest, tool, args),
                fetch=lambda kind, path: self._fetch(guest, kind, path),
                on_mode=((lambda: self.on_mode(guest))
                         if self.on_mode else None),
            )
        return guest.perms


    async def _fetch(self, guest: GuestSession, kind: str, path: str):
        """What the classifier asked to see: a folder's names, or a script's
        bytes. None for anything that cannot be had -- which the classifier
        treats as doubt, never as a pass."""
        if self.guest_fs is None:
            return None
        try:
            if kind == "ls":
                listing = await self.guest_fs.ls(guest, path)
                return [e.name for e in listing.entries]
            got = await self.guest_fs.get(guest, path,
                                          max_bytes=categories.MAX_SCRIPT)
            return None if got.truncated else got.data
        except Exception as exc:
            log.info("classifier could not %s %s: %s", kind, path, exc)
            return None

    async def on_rules(self, guest: GuestSession, f: Frame) -> None:
        """What can run without asking, as a transcript block.

        A block rather than a dialog: it is prose with a table in it, the
        guest already renders both, and a dialog on a 640x480 screen would
        need scrolling it does not have. It also belongs in the transcript --
        the answer depends on the mode, and the mode changes.
        """
        prefs = getattr(guest, "prefs", None)
        await self.presentation.write_block(
            guest, perms.rules_view(policy.mode_of(guest),
                                    getattr(prefs, "lists", None),
                                    guest.work_guest or ""))

    async def reply(self, guest: GuestSession, f: Frame) -> None:
        """Answer from the guest's prompt bar."""
        if guest.perms is not None:
            await guest.perms.reply(
                f.headers.get("id", ""), f.headers.get("action", "deny")
            )

    async def reissue(self, guest: GuestSession) -> None:
        """Reissue an active prompt bar after a reconnect."""
        if guest.perms is not None:
            await guest.perms.reissue()

    def deny_all(
        self, guest: GuestSession | None, reason: str, message: str
    ) -> None:
        """Resolve every pending and queued prompt as denied.

        Takes the None-guard that `perms: PermissionDesk | None` needs, so
        the callers do not each repeat it.
        """
        if guest is not None and guest.perms is not None:
            guest.perms.deny_all(reason, message)

    def callback(self, guest: GuestSession) -> Any:
        """Get the can_use_tool callback for the SDK turn."""
        if guest.perms is not None:
            return guest.perms.callback()
        return None

    def retarget(self, guest: GuestSession, cwd: str) -> None:
        """Point the guest's permission desk at a new directory."""
        if guest is not None and guest.perms is not None:
            guest.perms._cwd = cwd

