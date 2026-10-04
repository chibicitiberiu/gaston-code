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


# --- the Options inspector: what decides, and where it is written ------------

#: Rows per list the inspector is sent; past this, `hidden` says how many.
LIST_CAP = 256
#: Rows per LISTENT frame, so no frame comes near MAX_FRAME.
ROWS_PER_FRAME = 40


def _shell_rows(guest) -> list:
    """The shell grammar's lists -- the bridge's own data (bridge/profiles)
    -- as `entry\\treason\\tsource` rows."""
    from ..agent import cmdguard, profiles, toolrules
    names = [getattr(f, "name", "") for f in
             toolrules.shells_of(getattr(guest, "tools", None))] or \
        profiles.shells()
    rows = []
    for name in names:
        g = cmdguard.grammar_for(name)
        src = f"bridge: profiles/{name}.toml"
        for verb in sorted(g.always_ask):
            rows.append(f"{verb}\t{g.always_ask[verb]}\t{src}")
        for verb in sorted(g.unbounded):
            rows.append(f"{verb}\truns whatever it is given\t{src}")
        for verb in sorted(g.write):
            rows.append(f"{verb}\twrites what it names\t{src}")
        for verb in sorted(g.read):
            rows.append(f"{verb}\tonly reads\t{src}")
    return rows


def _ignore_rows(guest) -> list:
    from ..files import ignores
    rows = [f"{p}\tnever copied\tbridge: files/ignores.py"
            for p in ignores.DEFAULTS + ignores.ALWAYS]
    for p in getattr(getattr(guest, "prefs", None), "sync_ignore", ()) or ():
        rows.append(f"{p}\tyour pattern\tgaston.ini [ignore]")
    return rows


def _profile_rows() -> list:
    from ..agent import profiles
    rows = []
    for name in profiles.names():
        about = " ".join(profiles.about(name).split())[:120]
        rows.append(f"{name}\t{about}\tbridge: profiles/{name}.toml")
    return rows



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


    async def on_getlists(self, guest: GuestSession, f: Frame) -> None:
        """One list, or the answer to a test line (Options > Lists...).

        A TEST CLASSIFIES WITH THE PENDING LISTS AND MODE -- the sheet's copy,
        sent in the body after the line itself -- so "Try it" answers for what
        the person is about to save, not for what is in force: a test that
        ignored their last edit would be the dialog lying to them.
        """
        from .. import prefs as P
        from ..agent import policy
        from ..wire.protocol import listend, listent
        h = f.headers
        acp = getattr(getattr(guest, "caps", None), "acp", "") or "cp1252"
        if h.get("kind"):
            text = (f.body or b"").decode(acp, "replace")
            line, _, rest = text.partition("\n")
            pending = guest.prefs or P.DEFAULT
            if rest.strip():
                try:
                    pending = P.parse(rest.encode(acp, "replace"), acp)
                except P.PrefsError as exc:
                    log.info("inspector: pending settings refused (%s); "
                             "testing against the saved ones", exc)
            mode = (policy.lookup(h.get("mode", ""), pending.modes)
                    or policy.mode_of(guest))
            if h["kind"] == "path":
                tool, args = "mcp__machine__put_file", {"path": line.strip()}
            else:
                tool, args = "mcp__machine__exec", {"command": line.strip()}
            if guest.perms is None or not line.strip():
                await self.connection.send(guest, listend("test", value="ask",
                                                          mode=mode.label))
                return
            verdict = await guest.perms.classify(tool, args, lists=pending.lists)
            d = policy.decide(mode, verdict)
            rows = [f"{x.cat}\t{x.rule}\t{x.source}\t{'doubt' if x.doubt else ''}"
                    for x in verdict.hits]
            if verdict.refuse:
                rows.insert(0, f"refused\t{verdict.refuse}\t\t")
            await self.connection.send(guest, listend(
                "test", value=d.value, mode=mode.label,
                cat=d.hit.cat if d.hit else "",
                body="".join(r + "\n" for r in rows).encode(acp, "replace")))
            return

        list_id = h.get("list", "")
        rows = {"shell": lambda: _shell_rows(guest),
                "ignore": lambda: _ignore_rows(guest),
                "profiles": _profile_rows}.get(list_id, list)()
        hidden = max(0, len(rows) - LIST_CAP)
        rows = rows[:LIST_CAP]
        for i in range(0, len(rows), ROWS_PER_FRAME):
            await self.connection.send(guest,
                                       listent(list_id,
                                               rows[i:i + ROWS_PER_FRAME],
                                               acp))
        await self.connection.send(guest, listend(list_id, hidden))

