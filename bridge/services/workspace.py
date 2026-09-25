"""WorkspaceService: conversation work directories and guest path mapping.

Manages work directory naming, commitment at first tool call, path mapping
between host mirror and guest paths, and SETWORK frame handling.
"""

from __future__ import annotations

import contextlib
import logging
import os
from collections.abc import Callable
from typing import Any

from ..agent import sessions as sesslist
from ..files import paths as P
from ..guest import GuestSession
from ..view import html as H
from ..wire.protocol import Frame
from .config import BridgeConfig

log = logging.getLogger("bridge.workspace")


class WorkspaceService:
    """Manages workspace directories and path mapping for guest sessions."""

    def __init__(
        self,
        config: BridgeConfig,
        conv: Any | None = None,
        write_block: Callable[..., Any] | None = None,
        send_options: Callable[..., Any] | None = None,
        spawn_sync: Callable[..., Any] | None = None,
    ) -> None:
        self.config = config
        self.conv = conv
        self.write_block = write_block
        self.send_options = send_options
        self.spawn_sync = spawn_sync

    def stored_work_dir(self, session_id: str) -> str:
        """The folder a stored conversation was started in, if it is ours.

        Only a folder INSIDE the mirror is accepted.
        """
        base = self.config.sync_dir
        if not base:
            return ""
        for r in sesslist.listing():
            if r.get("id") != session_id:
                continue
            cwd = r.get("cwd") or ""
            if not cwd:
                return ""
            try:
                rel = os.path.relpath(cwd, base)
            except ValueError:
                return ""
            if rel in (".", "") or rel.startswith(".."):
                return ""
            return os.path.join(base, rel)
        return ""

    def mirror_dir(self, guest: GuestSession) -> str:
        """The directory on THIS side that pairs with the guest's work folder,
        or \"\" while this conversation has not committed to one.
        """
        return guest.work_dir or ""

    def work_dir(self, guest: GuestSession) -> str:
        """Where the agent works for THIS conversation."""
        return guest.work_dir or self.config.cwd or os.getcwd()

    def retarget_perms(self, guest: GuestSession) -> None:
        """Point the permission desk at this conversation's folder."""
        desk = getattr(guest, "perms", None)
        if desk is not None:
            desk._cwd = self.work_dir(guest)

    def guest_path(self, guest: GuestSession, name: str) -> str:
        r"""`<mirror root><sep><name>`, spelled the way the guest spells it."""
        root = (guest.hello or {}).get("sync_root") or guest.guest_sync_root
        return P.guest_path(root or "", name)

    def guest_work_root(self, guest: GuestSession) -> str:
        r"""Where commands run, spelled the way the guest spells it."""
        if guest.work_guest:
            return guest.work_guest
        return ((guest.hello or {}).get("sync_root")
                or guest.guest_sync_root or "")

    def sub_of(self, guest: GuestSession) -> str:
        """This conversation's folder name relative to the mirror, or \"\"."""
        # sync_dir is None when sync is switched off, and then nothing is
        # mirrored, so nothing is a subfolder of it.
        if not self.config.sync_dir:
            return ""
        return P.sub_of(guest.work_dir, self.config.sync_dir)

    async def ensure_work_dir(self, guest: GuestSession) -> None:
        """Commit this conversation to a folder, if it has not already."""
        if guest.work_guest:
            if guest.conv_id and self.conv is not None:
                name = (
                    P.folder_name(
                        guest.work_guest.replace("\\", "/").rsplit("/", 1)[-1],
                        guest.caps,
                    )
                    or "WORK"
                )
                self.conv.set_folder(guest.conv_id, name, guest.work_guest)
            return
        await self.name_work_dir(guest)
        if guest.work_guest and self.send_options:
            await self.send_options(guest)

    async def name_work_dir(self, guest: GuestSession) -> None:
        """Give this conversation its folder."""
        if guest.work_guest or not guest.conv_id:
            return
        base = self.config.sync_dir
        if not base:
            return
        if not self.guest_path(guest, "x"):
            return

        source = guest.first_prompt
        if not source:
            for r in sesslist.listing():
                if r.get("id") == guest.conv_id:
                    source = r.get("title") or ""
                    break
        name = P.title_folder(source, guest.caps)
        if not name:
            return

        taken = self.conv.taken() if self.conv is not None else set()
        stem, n = name, 1
        while os.path.exists(os.path.join(base, name)) or name in taken:
            n += 1
            if n > 99:
                return
            tail = str(n)
            name = stem[: P.folder_max(guest.caps) - len(tail)] + tail

        path = os.path.join(base, name)
        try:
            os.makedirs(path, exist_ok=True)
        except OSError:
            log.exception("could not create %s", path)
            return
        guest.work_dir = path
        guest.work_guest = self.guest_path(guest, name)
        if self.conv is not None:
            self.conv.set_folder(guest.conv_id, name, guest.work_guest)
        self.retarget_perms(guest)
        log.info(
            "work directory for %s: %s (%s)",
            guest.conv_id,
            path,
            guest.work_guest,
        )
        if self.write_block:
            await self.write_block(
                guest,
                H.notice(
                    "This conversation works in "
                    + guest.work_guest
                    + " on your machine."
                ),
            )
        note = (
            f"This conversation's folder now exists: write in {path} "
            f"here, which is mirrored to {guest.work_guest} on the "
            f"user's machine. Anything written outside it stays on this proxy."
        )
        if note not in guest.synccoord.agent_notices:
            guest.synccoord.agent_notices.append(note)

    async def on_setwork(self, guest: GuestSession, f: Frame) -> None:
        """The user picked a folder on their machine for this conversation."""
        path = (f.headers.get("path") or "").strip()
        if not path:
            return

        name = P.folder_name(
            path.replace("\\", "/").rsplit("/", 1)[-1], guest.caps
        )
        if not name:
            name = (
                P.folder_name(guest.conv_id[:8], guest.caps)
                if guest.conv_id
                else "WORK"
            )
        base = self.config.sync_dir
        if not base:
            if self.write_block:
                await self.write_block(
                    guest,
                    H.error(
                        "This bridge has no mirrored folder, so a work directory "
                        "would have nothing on this side to pair with."
                    ),
                )
            return
        local = os.path.join(base, name)
        try:
            os.makedirs(local, exist_ok=True)
        except OSError as exc:
            log.exception("could not create %s", local)
            if self.write_block:
                await self.write_block(
                    guest,
                    H.error(
                        "Could not create the folder on this side: "
                        + str(exc)[:160]
                    ),
                )
            return

        guest.work_dir = local
        guest.work_guest = path
        if guest.conv_id and self.conv is not None:
            self.conv.set_folder(guest.conv_id, name, path)
        self.retarget_perms(guest)
        log.info(
            "work directory for %s: %s (guest: %s)",
            guest.conv_id or "<initial>",
            local,
            path,
        )
        if self.write_block:
            await self.write_block(
                guest,
                H.notice(
                    "This conversation works in " + path + " on your machine."
                ),
            )

        if not guest.conv_id and guest.claude:
            with contextlib.suppress(Exception):
                await guest.claude.close()
            guest.claude = None

        if guest.conv_id:
            note = (
                f"THE WORK DIRECTORY HAS CHANGED. This conversation now "
                f"works in {local} here, mirrored to {path} on the user's "
                f"machine. The folder you were using before is no longer "
                f"mirrored -- anything written there from now on stays on "
                f"this proxy, however well the write appears to succeed."
            )
            if f.headers.get("staged") == "merge":
                note += (
                    f"\n\nThe user asked YOU to merge the files they brought "
                    f"across. Copies whose names clashed are in "
                    f"{os.path.join(local, 'Gaston-Incoming')}; the versions that "
                    f"were already here are in place beside it. Reconcile them "
                    f"file by file, say what you changed, and ask before "
                    f"discarding either side. Do not delete anything in "
                    f"Gaston-Incoming until the user agrees the merge is right."
                )
            if note not in guest.synccoord.agent_notices:
                guest.synccoord.agent_notices.append(note)
        elif f.headers.get("staged") == "merge":
            note = (
                f"The user asked YOU to merge the files they brought "
                f"across into {path}. Copies whose names clashed are in "
                f"{os.path.join(local, 'Gaston-Incoming')}; the versions that "
                f"were already here are in place beside it. Reconcile them "
                f"file by file, say what you changed, and ask before "
                f"discarding either side. Do not delete anything in "
                f"Gaston-Incoming until the user agrees the merge is right."
            )
            if note not in guest.synccoord.agent_notices:
                guest.synccoord.agent_notices.append(note)

        if self.send_options:
            await self.send_options(guest)
        if self.config.sync_dir and self.spawn_sync:
            self.spawn_sync("the work directory was set")

