"""PermissionService: guest tool-permission prompts and queue management.

Coordinates the PermissionDesk lifecycle for guest sessions, including desk
creation/attachment, reply routing (PERMREPLY), prompt reissuance on reconnect,
and bulk prompt denial on teardown or session switch.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from ..guest import GuestSession
from ..agent import permissions as perms
from ..agent.permdesk import PermissionDesk
from ..agent.session import TOOLS
from ..wire.protocol import Frame

if TYPE_CHECKING:
    pass

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
    ) -> None:
        self.connection = connection
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
            )
        return guest.perms

    create_desk = attach

    async def on_rules(self, guest: GuestSession, f: Frame) -> None:
        """What can run without asking, as a transcript block.

        A block rather than a dialog: it is prose with a table in it, the
        guest already renders both, and a dialog on a 640x480 screen would
        need scrolling it does not have. It also belongs in the transcript --
        the answer depends on the mode, and the mode changes.
        """
        tools = list(TOOLS) + ["mcp__machine__exec"]
        root = (guest.hello or {}).get("sync_root", "") or ""
        await self.presentation.write_block(
            guest, perms.rules_view(guest.perm_mode, tools, root))

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
        self, guest: GuestSession | None, why: str = "disconnected"
    ) -> None:
        """Resolve every pending and queued prompt as denied."""
        if guest is not None and guest.perms is not None:
            guest.perms.deny_all(why)

    def callback(self, guest: GuestSession) -> Any:
        """Get the can_use_tool callback for the SDK turn."""
        if guest.perms is not None:
            return guest.perms.callback()
        return None

    def retarget(self, guest: GuestSession, cwd: str) -> None:
        """Point the guest's permission desk at a new directory."""
        if guest is not None and guest.perms is not None:
            guest.perms._cwd = cwd

    # Private method aliases for bridge facade
    _perm_reply = reply
