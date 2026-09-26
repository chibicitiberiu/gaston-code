"""SessionService: session lifecycle, deferred start, switching, and history export.

Owns the locked inner transitions (new_session_locked, switch_locked, set_effort_locked),
session creation with tool bindings and event callbacks (make_session), deferred resume
(resume_pending), session list presentation (send_sessions), and transcript export (on_export).
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import logging
import os
import sys
from collections.abc import Callable
from typing import Any

from ..agent import machine
from ..agent import permissions as perms
from ..agent import sessions as sesslist
from ..agent import tools as machine_tools
from ..agent.session import Session
from ..guest import GuestSession
from ..view import html as H
from ..wire import fold
from ..wire.protocol import (
    Frame,
    error,
    notice,
    session_row,
    sessions_end,
    sessions_start,
    switched,
)
from .options import EFFORT_LABEL, MODE_ORDER

log = logging.getLogger("bridge.session")


def shows_tool_result(is_err: bool, stopped: bool) -> bool:
    """Does the user see this tool result?

    Not the error a Stop cuts a tool call short with: that body is the SDK's
    note to the MODEL ("The user doesn't want to proceed with this tool
    use ... STOP what you are doing"), and on the guest it rendered as a red
    error block right above the turn's own "Stopped." -- the user reading an
    instruction meant for the assistant, about an action they took."""
    return not (is_err and stopped)


def _summarise(args: dict) -> str:
    """One readable line for the tool-activity pane.

    perms.telling_argument is the one place "which argument matters" is
    decided now -- this used to be a fourth, independently-ordered copy of
    the same list. See docs/audit/02-bridge-python.md 02.15.
    """
    told = perms.telling_argument(args)
    if told:
        key, value = told
        return f"{key}: {value[:160]}"
    return ", ".join(f"{k}={str(v)[:40]}" for k, v in list(args.items())[:3])


class SessionService:
    """Session lifecycle, switching, session listing and export service."""

    def __init__(
        self,
        connection: Any,
        presentation: Any,
        workspace: Any,
        options: Any,
        guest_exec: Any,
        conv: Any,
        spawn: Callable[..., Any] | None = None,
        sync: Any = None,
        ui: Any = None,
        proc: Any = None,
    ) -> None:
        self.connection = connection
        self.presentation = presentation
        self.workspace = workspace
        self.options = options
        self.guest_exec = guest_exec
        self.sync = sync
        self.ui = ui
        self.proc = proc
        self.conv = conv
        # Defaulted, not Optional -- see SessionCoordinator.__init__.
        self.spawn: Callable[[Any], Any] = spawn or asyncio.create_task


    def _resolve_session_cls(self) -> Any:
        mod = sys.modules.get("bridge.server")
        if mod and hasattr(mod, "Session"):
            return mod.Session
        return Session

    def _resolve_sesslist(self) -> Any:
        mod = sys.modules.get("bridge.server")
        if mod and hasattr(mod, "sesslist"):
            return mod.sesslist
        return sesslist

    def conv_title(self, guest: GuestSession) -> str:
        """What this conversation is called, for the export's heading.

        From the chat list rather than from anything the bridge invents: the
        title the user is looking at in the list is the one they expect on the
        file. Falls back to the id, and then to a flat noun, because an
        untitled conversation is still exportable.
        """
        cid = guest.conv_id
        if cid:
            for r in self._resolve_sesslist().listing():
                if r["id"] == cid:
                    return r["title"]
        return cid or "Conversation"

    async def on_export(self, guest: GuestSession, f: Frame) -> None:
        """Assemble the whole conversation into one file and stream it.

        Assembled HERE and not on the guest, like everything else: the guest
        has tens of kilobytes free and a transcript is hundreds. It never
        holds the document -- each chunk goes straight from the socket to a
        file -- which is the same rule `do_put` already follows for sync.
        """
        title = self.conv_title(guest)
        when = _dt.datetime.now().strftime("%Y-%m-%d %H:%M")
        doc = H.export_document(title, guest.view.pages.all_text(),
                                "Exported from Gaston Code, " + when)
        # CP1252 on the way out, because the file is read on the machine that
        # saved it, by a browser that will be told windows-1252 in the meta
        # tag the document carries.
        data = fold.encode(doc)
        log.info("export: %d bytes for %r", len(data), title)
        await guest.link.send_export(guest, data)

    async def send_sessions(self, guest: GuestSession) -> None:
        """The chat list. Only conversations this bridge started -- the rest
        belong to a machine with a different way of working.

        Extracted so the list can be pushed when the conversation id CHANGES
        as well as when the guest asks. The effort fork changes it, and until
        now nothing told the guest.
        """
        conv_store = self.conv if self.conv is not None else getattr(self.connection, "conv", None)
        listing = self._resolve_sesslist().listing()
        rows = conv_store.filter(listing) if conv_store is not None else listing
        await self.connection.send(guest, sessions_start(len(rows)))
        for i, r in enumerate(rows):
            await self.connection.send(guest, session_row(
                i, r["id"], r["title"], r["cwd"], r.get("group", ""),
                bool(guest.conv_id and guest.conv_id == r["id"])))
        await self.connection.send(guest, sessions_end())

    async def on_getsessions(self, guest: GuestSession, f: Frame) -> None:
        await self.send_sessions(guest)

    def restore_settings(self, guest: GuestSession, session_id: str) -> None:
        """Put back what this conversation was last set to.

        ONLY WHAT WAS RECORDED. A conversation with nothing saved -- one from
        before this existed, or one opened on another machine -- keeps
        whatever is currently selected rather than being reset to the
        process defaults, because "I have not chosen for this chat" and "I
        chose the default for this chat" are different and only the second
        one is written down.
        """
        conv_store = self.conv if self.conv is not None else getattr(self.connection, "conv", None)
        saved = conv_store.settings(session_id) if conv_store is not None else None
        if not saved:
            return
        if "model" in saved:
            guest.model = saved["model"]
        if "effort" in saved:
            guest.effort = saved["effort"]
        mode = saved.get("mode")
        if mode in MODE_ORDER:
            guest.perm_mode = mode
        log.info("restored settings for %s: %s", session_id, saved)


    def abandon_pending(self, guest: GuestSession) -> None:
        """Orphan everything the conversation being left behind still owes.

        A prompt belongs to the session that raised it, and so does a running
        EXEC. Leaving either pending against a conversation the user has
        walked away from is the unanswerable state `SessionCoordinator.
        reissue_halt` exists to prevent -- worse now than when this lived
        inside `make_session`, because a switch no longer rebuilds anything
        and would otherwise clean up nothing at all. The guest hides its bar
        on the strength of the deny, so the deny has to happen whether or
        not a new client is built.
        """
        if guest.perms:
            guest.perms.deny_all(
                "rebuild", "The conversation changed before this was answered.")
        for exec_id in list(guest.exec_pending):
            self.guest_exec.kill_exec(guest, exec_id)
        self.guest_exec.fail_execs(
            guest, "The conversation changed while this command was running.")

    async def make_session(
        self,
        guest: GuestSession,
        resume: str | None = None,
        fork: bool = False,
    ) -> Session:
        # The model list is whatever the CLI reported when the CLIENT
        # connected -- get_server_info() hands back the cached init result --
        # so a CLI upgraded under a running bridge cannot reach the menu
        # until a new client exists. This is that moment, and it is the only
        # one: re-fetching at any other point re-reads the same init result.
        self.options.clear_cache()

        async def on_text(t: str) -> None:
            guest.turn_produced = True
            await self.presentation.emit_text(guest, t, style="plain")

        async def on_thinking(t: str) -> None:
            await self.presentation.emit_text(guest, t, style="thinking")

        async def on_tool(name: str, args: dict) -> None:
            guest.turn_produced = True
            # THE FIRST TOOL IS THE COMMITMENT: this conversation gets its
            # folder here, and file sharing begins. Before the block is
            # written, so the notice naming the folder comes first and the
            # tool line lands under it rather than above it.
            await self.workspace.ensure_work_dir(guest)
            # Flush buffered text first: a tool line that jumps ahead of the
            # prose that introduced it reads as a non sequitur.
            await self.presentation.flush_text(guest)
            await self.presentation.write_block(guest, H.tool_call(name, _summarise(args)))

        async def on_tool_result(tool_id: str, body: str, is_err: bool) -> None:
            # A finished block, like every other thing the user reads.
            await self.presentation.flush_text(guest)
            if not shows_tool_result(is_err, guest.turn_stopped):
                return
            await self.presentation.write_block(guest, H.tool_result(body, error=is_err))

        async def on_system(subtype: str, data: dict) -> None:
            if subtype == "compact_boundary":
                await self.presentation.write_block(guest, H.notice("History condensed."))
            elif subtype == "rate_limit":
                status = str(data.get("status") or "")
                if status == "rejected":
                    guest.turn_degraded = "rate_limited"
                    await self.presentation.write_block(guest, H.notice(
                        "The model provider refused this request: the "
                        "account is over its rate limit. Nothing ran -- try "
                        "again shortly."))
            elif subtype == "conversation_reset":
                self.options.remember_settings(
                    guest, str(data.get("new_conversation_id") or ""))
                await self.presentation.write_block(guest, H.notice("Conversation reset."))
            elif subtype == "sdk_event":
                event = str(data.get("event", "")) or "an unknown event"
                guest.turn_degraded = event
                await self.presentation.write_block(guest, H.notice(
                    f"The agent reported {event}, which this bridge does "
                    f"not handle yet. If nothing follows, the request did "
                    f"not run."))
            elif subtype == "init":
                sid = str(data.get("session_id") or "")
                if sid:
                    if guest.work_guest:
                        raw_folder = guest.work_guest.replace("\\", "/").rsplit("/", 1)[-1]
                        name = (
                            self.workspace.folder_name(raw_folder, guest.caps)
                            if hasattr(self.workspace, "folder_name")
                            else (getattr(self.connection, "folder_name", lambda f, c: f)(raw_folder, guest.caps))
                        ) or "WORK"
                        conv_store = self.conv if self.conv is not None else getattr(self.connection, "conv", None)
                        if conv_store is not None:
                            conv_store.set_folder(sid, name, guest.work_guest)
                    self.options.remember_settings(guest, sid)
                    self.spawn(self.send_sessions(guest))

        work_dir = (
            self.workspace.work_dir(guest)
            if hasattr(self.workspace, "work_dir")
            else getattr(self.connection, "_work_dir", lambda g: "")(guest)
        )
        sync_dir_opt = (
            getattr(self.workspace.config, "sync_dir", None)
            if hasattr(self.workspace, "config")
            else getattr(self.connection, "sync_dir", None)
        )
        can_use_tool_cb = guest.perms.callback() if guest.perms else None

        session_cls = self._resolve_session_cls()
        s = session_cls(
            cwd=work_dir,
            system_prompt={
                "type": "preset",
                "preset": "claude_code",
                "append": machine.system_prompt(
                    guest.hello, work_dir,
                    sync_dir=work_dir if sync_dir_opt else None,
                    guest_root=guest.work_guest,
                    home=(guest.hello or {}).get("sync_root") or "",
                    tools=guest.tools, path=guest.path),
            },
            model=self.options.effective_model(guest),
            effort=self.options.effective_effort(guest),
            permission_mode=guest.perm_mode,
            resume=resume,
            fork=fork,
            can_use_tool=can_use_tool_cb,
            mcp_servers=machine_tools.build(
                self.guest_exec, self.sync, self.workspace, guest,
                ui=self.ui, proc=self.proc),
            on_text=on_text,
            on_thinking=on_thinking,
            on_tool=on_tool,
            on_tool_result=on_tool_result,
            on_system=on_system,
        )
        await s.start()
        log.info("session in %s for guest machine:\n%s",
                 work_dir, machine.describe(guest.hello))
        conv_store = self.conv if self.conv is not None else getattr(self.connection, "conv", None)
        if conv_store is not None:
            conv_store.add(s.session_id)
        return s

    async def resume_pending(self, guest: GuestSession) -> bool:
        """Build the client for a conversation that has not been spoken in.

        NEITHER opening a chat from the list NOR pressing New chat spawns
        anything: both are pure bookkeeping, and this is where the process
        finally starts -- at the first message, which is also the first
        moment it could do anything. Resumes a stored conversation when one
        is pending and starts a fresh one otherwise.

        Returns False and tells the user if it fails; the caller must not
        proceed. A message that vanishes because there was no client is the
        one outcome worse than an error.
        """
        if guest.claude:
            return True
        resuming = bool(guest.pending_resume)
        try:
            guest.claude = await self.make_session(
                guest, resume=guest.pending_resume or None)
        except Exception as exc:
            log.exception("deferred session start failed")
            await self.presentation.write_block(guest, H.error(
                ("Could not reopen the conversation: " if resuming
                 else "Could not start a new session: ") + str(exc)[:160]))
            return False
        guest.pending_resume = ""
        if not resuming:
            await self.connection.send(guest, switched(guest.claude.session_id or "",
                                             guest.view.pages.count))
        return True

    async def new_session_locked(
        self, guest: GuestSession, folder: str = ""
    ) -> None:
        """Start a fresh conversation."""
        assert guest.turn_lock.locked()
        log.info("new session requested")
        self.abandon_pending(guest)
        if guest.claude:
            await guest.claude.close()
        guest.pending_resume = ""
        guest.work_dir = ""
        guest.work_guest = ""
        guest.first_prompt = ""
        self.workspace.retarget_perms(guest)
        await guest.view.reset()
        guest.claude = None
        await self.connection.send(guest, switched("", guest.view.pages.count))
        await self.presentation.send_page(guest, "live")
        await self.options.send_options(guest)

    async def switch_locked(
        self, guest: GuestSession, session_id: str
    ) -> None:
        """Close the live client and reopen on a stored session."""
        assert guest.turn_lock.locked()
        if not session_id:
            await self.connection.send(guest, error("bad_session", "no id"))
            return

        log.info("switching to session %s", session_id)
        await self.connection.send(guest, notice("switching", "Loading conversation..."))

        self.abandon_pending(guest)
        if guest.claude:
            await guest.claude.close()
        guest.claude = None
        guest.pending_resume = session_id
        self.restore_settings(guest, session_id)

        conv_store = self.conv if self.conv is not None else getattr(self.connection, "conv", None)
        name = conv_store.folder(session_id) if conv_store is not None else None
        sync_dir_opt = (
            getattr(self.workspace.config, "sync_dir", None)
            if hasattr(self.workspace, "config")
            else getattr(self.connection, "sync_dir", None)
        )
        if name and sync_dir_opt:
            guest.work_dir = os.path.join(sync_dir_opt, name)
            guest.work_guest = (
                (conv_store.guest_folder(session_id) if conv_store is not None else None)
                or self.workspace.guest_path(guest, name)
            )
        else:
            guest.work_dir = self.workspace.stored_work_dir(session_id)
            guest.work_guest = ""
        guest.first_prompt = ""
        self.workspace.retarget_perms(guest)

        await guest.view.reset()

        for block in self._resolve_sesslist().history_html(session_id):
            await self.presentation.write_block(guest, block, stream=False, notify=False)
            if guest.view.pages.live.over_soft_cap():
                guest.view.pages.message_boundary()

        await self.connection.send(guest, switched(session_id, guest.view.pages.count))
        await self.presentation.send_page(guest, "live")
        await self.options.send_options(guest)

    async def set_effort_locked(
        self, guest: GuestSession, effort: str
    ) -> None:
        """Change effort, which means reopening the session."""
        assert guest.turn_lock.locked()
        label = EFFORT_LABEL.get(effort, effort or "default")

        if not guest.claude:
            guest.effort = effort
            self.options.remember_settings(guest)
            log.info("effort -> %s (no client yet)", effort or "(default)")
            await self.presentation.write_block(guest, H.notice(
                "Effort set to " + label + "."))
            await self.options.send_options(guest)
            return

        old = guest.claude.session_id
        self.abandon_pending(guest)
        guest.effort = effort
        self.options.remember_settings(guest)
        log.info("effort -> %s (forking %s)", effort or "(default)", old)
        await self.presentation.write_block(guest, H.notice(
            "Effort set to " + label
            + ". Effort cannot be changed on a running session, so this "
              "conversation has been forked -- the history is intact and "
              "replies from here use the new setting."))

        await guest.claude.close()
        try:
            guest.claude = await self.make_session(guest, resume=old, fork=True)
        except Exception as exc:
            log.exception("effort change failed")
            await self.presentation.write_block(guest, H.error(
                "Could not reopen the session: " + str(exc)[:160]))
            await self.options.send_options(guest)
            return
        await self.options.send_options(guest)

