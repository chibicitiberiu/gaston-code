"""SessionService: session lifecycle, deferred start, switching, and history export.

Owns the locked inner transitions (new_session_locked, switch_locked, set_effort_locked),
session creation with tool bindings and event callbacks (make_session), deferred resume
(resume_pending), session list presentation (send_sessions), and transcript export (on_export).
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as _dt
import logging
import os
import sys
from collections.abc import Callable
from typing import Any

from ..agent import machine, policy
from ..agent import permissions as perms
from ..agent import sessions as sesslist
from ..agent import tools as machine_tools
from ..agent.prompts import text as policy_text
from ..agent.session import TOOLS, Session
from ..guest import GuestSession
from ..view import html as H
from ..view.blocks import Block, encode
from ..wire.protocol import (
    Frame,
    error,
    notice,
    session_row,
    sessions_end,
    sessions_start,
    switched,
)
from .options import EFFORT_LABEL

log = logging.getLogger("bridge.session")

#: How many conversations the chat list sends, applied AFTER the filter.
#:
#: The guest's list is capped at `MAX_CHATS` (64) rows in `win/app/chatlist.h`
#: and the age-group headers count against that cap -- `sessions._age_group`
#: has five buckets -- so 59 conversations is the most that can arrive without
#: the guest dropping the tail in silence. Cutting here as well keeps the wire
#: honest: a row past the cap is a frame the list would refuse.
#:
#: 59 -> 58 on 2026-10-02, AND THE ROW IT GIVES UP IS THE POINT. The list now
#: ends with "N older not shown" when the cap bit, which is a row like any
#: other and counts against MAX_CHATS like any other. Measured on the real box
#: the moment it shipped: 59 conversations plus four age groups plus the notice
#: is exactly 64, so it fitted by one -- and on a day whose conversations span
#: all five buckets it would not have, and the notice would have been the row
#: that got dropped. The thing that tells you rows are missing must not be the
#: thing that goes missing.
#:
#: The trade is one conversation, and only for someone who has more than 58:
#: below that nothing is hidden, so there is no notice and nothing is given up.
#:
#: Pinned against the header by tests/python/meta/test_chatlist_cap.py,
#: including the row the notice needs.
DISPLAY_LIMIT = 58


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
        guest_fs: Any = None,
    ) -> None:
        self.connection = connection
        self.presentation = presentation
        self.workspace = workspace
        self.options = options
        self.guest_exec = guest_exec
        self.sync = sync
        self.ui = ui
        self.proc = proc
        self.guest_fs = guest_fs
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
        heading = Block("exportheading", {},
                        [("h", title or "Conversation"),
                         ("t", "Exported from Gaston Code, " + when)])
        records = [encode(heading), *guest.view.pages.all_records()]
        log.info("export: %d records, %d bytes for %r",
                 len(records), sum(len(r) for r in records), title)
        await guest.link.send_export_records(guest, records)

    def _homes(self) -> list[str]:
        """The directories this bridge runs conversations under: --cwd and
        --sync-dir, resolved. Empty when neither is known (a test double)."""
        cfg = getattr(self.workspace, "config", None)
        out = []
        for d in (getattr(cfg, "cwd", None), getattr(cfg, "sync_dir", None)):
            if isinstance(d, str) and d:
                with contextlib.suppress(OSError, ValueError):
                    out.append(os.path.realpath(d))
        return out

    def ours(self, rows: list[dict], guest: GuestSession) -> list[dict]:
        """The listing narrowed to what THIS guest should be offered here.

        Three questions. Started by a bridge, for this machine -- the store's
        `filter`. And started by THIS bridge: the store is one file in $HOME
        shared by every bridge on the host, so a second bridge with its own
        --cwd and --sync-dir listed the first one's conversations, and
        opening one resumed it in the other bridge's folders (measured
        2026-10-03: 87 rows on a bridge that had started none). Answered
        from where each session RAN, which every row already carries -- so
        conversations from before this check are sorted correctly too. A
        row with no cwd is kept: unknown is not foreign.
        """
        conv_store = self.conv if self.conv is not None else getattr(self.connection, "conv", None)
        if conv_store is not None:
            rows = conv_store.filter(rows, guest.machine)
        homes = self._homes()
        if not homes:
            return rows

        def inside(cwd: str) -> bool:
            if not cwd:
                return True
            try:
                c = os.path.realpath(cwd)
            except (OSError, ValueError):
                return True
            for h in homes:
                with contextlib.suppress(ValueError):
                    if os.path.commonpath([h, c]) == h:
                        return True
            return False

        return [r for r in rows if inside(r.get("cwd", ""))]

    async def send_sessions(self, guest: GuestSession) -> None:
        """The chat list. Only conversations this bridge started -- the rest
        belong to a machine with a different way of working.

        Extracted so the list can be pushed when the conversation id CHANGES
        as well as when the guest asks. The effort fork changes it, and until
        now nothing told the guest.

        FETCH WIDE, FILTER, THEN CAP -- in that order, and the cap is last.
        The listing is the whole host's and the filter throws most of it
        away, so a limit applied first is a limit on OTHER PEOPLE'S work:
        the guest's chats fell off the end of a 50-row fetch as sessions in
        unrelated checkouts turned up newer, and nothing said so. See
        `sesslist.FETCH_LIMIT` for the measurement, and `DISPLAY_LIMIT` for
        the bound that survives.

        The filter is BOTH halves of "ours" -- this bridge and this machine.
        `mine` is written on the bridge host and cannot tell one guest from
        another, so a second Win98 box, or the real XP 2000+ beside the QEMU
        one, saw the other's conversations with the other's drive letters in
        every path (BACKLOG.md §21). A guest that sends no `machine` header
        gets the old, wider list rather than an empty one.
        """
        rows = self.ours(self._resolve_sesslist().listing(), guest)
        hidden = 0
        if len(rows) > DISPLAY_LIMIT:
            hidden = len(rows) - DISPLAY_LIMIT
            log.info("chat list: %d conversations, showing the newest %d",
                     len(rows), DISPLAY_LIMIT)
            rows = rows[:DISPLAY_LIMIT]
        await self.connection.send(guest, sessions_start(len(rows)))
        for i, r in enumerate(rows):
            await self.connection.send(guest, session_row(
                i, r["id"], r["title"], r["cwd"], r.get("group", ""),
                bool(guest.conv_id and guest.conv_id == r["id"])))
        await self.connection.send(guest, sessions_end(hidden))

    async def on_getsessions(self, guest: GuestSession, f: Frame) -> None:
        await self.send_sessions(guest)

    def reset_chat_policy(self, guest: GuestSession) -> None:
        """The client's default mode, and no grants -- and its default model
        and effort, when it has declared any (Options > General). Without a
        declared default a new chat keeps what is selected, as before."""
        guest.grants = set()
        guest.mode_before_plan = ""
        guest.mode_id = policy.default_mode(guest)
        prefs = getattr(guest, "prefs", None)
        model = str(getattr(prefs, "general_model", "") or "")
        effort = str(getattr(prefs, "general_effort", "") or "")
        if model:
            guest.model = model
        if effort:
            guest.effort = effort

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
        # The execution mode first; a row from before 2026-10-04 has only
        # the CLI's name for it, mapped on read and never rewritten here.
        mode_id = saved.get("mode_id") or policy.LEGACY.get(
            str(saved.get("mode") or ""), "")
        user = getattr(getattr(guest, "prefs", None), "modes", None)
        if mode_id and policy.lookup(mode_id, user) is not None:
            guest.mode_id = mode_id
            if not saved.get("mode_id") and mode_id == "auto":
                guest.legacy_auto = True
        # Grants only from the SAME machine, and only the kinds a grant may
        # name today -- a stored grant is data from disk, not a licence.
        if (saved.get("grants")
                and saved.get("grants_machine", "") == (guest.machine or "")):
            guest.grants = {g for g in saved["grants"]
                            if str(g).partition(":")[0] in policy.GRANTABLE}
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
            await self.presentation.write_block(
                guest, H.tool_result(body, error=is_err,
                                     cap=guest.view.toolmax))

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
        mode = policy.mode_of(guest)
        # What this session is told it has. Recorded so a turn after a mode
        # change (or a reconnect that changed the caps) can tell the session
        # it is about to use was built for a different surface.
        guest.surface = policy.surface(mode, getattr(guest, "caps", None))
        s = session_cls(
            tools=[t for t in TOOLS if policy.offered(mode, t)],
            cwd=work_dir,
            system_prompt={
                "type": "preset",
                "preset": "claude_code",
                "append": machine.system_prompt(
                    guest.hello, work_dir,
                    sync_dir=work_dir if sync_dir_opt else None,
                    guest_root=guest.work_guest,
                    home=(guest.hello or {}).get("sync_root") or "",
                    # WHERE THE FOLDER IS GOING TO BE, for the sync-off
                    # branch. The folder is committed at the first tool call
                    # and this prompt is built before it, so `guest_root` is
                    # empty on a conversation's first turn -- and with no
                    # mirror that path is the only destination the agent will
                    # ever be given. Separate from `guest_root` on purpose:
                    # the mirrored branch must keep telling the difference
                    # between a folder that exists and one that is coming.
                    planned=self.workspace.planned_guest_dir(guest)
                    if hasattr(self.workspace, "planned_guest_dir") else "",
                    tools=guest.tools, path=guest.path),
            },
            model=self.options.effective_model(guest),
            effort=self.options.effective_effort(guest),
            permission_mode=mode.sdk,
            resume=resume,
            fork=fork,
            can_use_tool=can_use_tool_cb,
            mcp_servers=machine_tools.build(
                self.guest_exec, self.sync, self.workspace, guest,
                ui=self.ui, proc=self.proc, fs=self.guest_fs,
                workdir=work_dir, mode=mode),
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
            conv_store.add(s.session_id, guest.machine)
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
        self.reset_chat_policy(guest)
        guest.work_dir = ""
        guest.work_guest = ""
        guest.work_planned = ""
        guest.work_told = ""
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
        # A CHAT STARTS FROM THE CLIENT'S DEFAULT MODE AND NO GRANTS, and only
        # then takes what it saved: "allow programs here, this chat" from the
        # last conversation must not follow the person into the next one,
        # and `restore_settings` returns early for a chat that saved nothing.
        self.reset_chat_policy(guest)
        self.restore_settings(guest, session_id)
        if getattr(guest, "legacy_auto", False):
            guest.legacy_auto = False
            await self.presentation.write_block(
                guest, H.notice(policy_text("policy.legacy_auto")))

        conv_store = self.conv if self.conv is not None else getattr(self.connection, "conv", None)
        name = conv_store.folder(session_id) if conv_store is not None else None
        sync_dir_opt = (
            getattr(self.workspace.config, "sync_dir", None)
            if hasattr(self.workspace, "config")
            else getattr(self.connection, "sync_dir", None)
        )
        # THE FOLDER ON THE MACHINE IS RESTORED WHETHER OR NOT THERE IS A
        # MIRROR. It was gated on `sync_dir` with the rest, so reopening a
        # conversation on a bridge with no `--sync-dir` forgot which folder it
        # had been working in and the first tool call named a second one --
        # same conversation, two folders on the user's disk, and the files
        # split between them. The guest path is what the store holds; the
        # local tree is still the mirror's business.
        if name:
            guest.work_planned = name
            guest.work_guest = (
                (conv_store.guest_folder(session_id)
                 if conv_store is not None else None)
                or self.workspace.guest_path(guest, name)
            )
        else:
            guest.work_planned = ""
            guest.work_guest = ""
        guest.work_told = ""
        if name and sync_dir_opt:
            guest.work_dir = os.path.join(sync_dir_opt, name)
        else:
            guest.work_dir = self.workspace.stored_work_dir(session_id)
        guest.first_prompt = ""
        self.workspace.retarget_perms(guest)

        await guest.view.reset()

        for block in self._resolve_sesslist().history_blocks(session_id):
            await self.presentation.write_block(guest, block, stream=False, notify=False)
            if guest.view.pages.live.over_soft_cap():
                guest.view.pages.message_boundary()

        await self.connection.send(guest, switched(session_id, guest.view.pages.count))
        await self.presentation.send_page(guest, "live")
        await self.options.send_options(guest)

    async def match_surface_locked(self, guest: GuestSession) -> None:
        """Rebuild the session if what it was told it has is no longer what
        this conversation's mode offers -- a tool turned Off, or back on, or
        the screen tools after a reconnect that changed the caps.

        RESUMED, NOT FORKED: the conversation is the same one, only the tool
        list changes, and a fork would print a new session id and a notice
        for something the person did not ask about. Before a turn, under the
        turn lock, so nothing is mid-call when the session is replaced.
        """
        assert guest.turn_lock.locked()
        if guest.claude is None:
            return
        want = policy.surface(policy.mode_of(guest),
                              getattr(guest, "caps", None))
        if getattr(guest, "surface", want) == want:
            return
        old = guest.claude.session_id
        log.info("tool surface changed (%s -> %s); reopening %s",
                 getattr(guest, "surface", None), want, old)
        self.abandon_pending(guest)
        await guest.claude.close()
        guest.claude = None
        try:
            guest.claude = await self.make_session(guest, resume=old)
        except Exception as exc:
            log.exception("reopening for a new tool surface failed")
            await self.presentation.write_block(guest, H.error(
                "Could not reopen the session: " + str(exc)[:160]))

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

