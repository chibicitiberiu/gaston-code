"""TurnRunner: turn orchestration, sync preamble, multimodal turns, and stream lifecycle.

Owns the execution of turns on GuestSession (run_turn, run_turn_locked, resume_turn),
injection of synchronization preambles and delivery tracking (with_sync_preamble,
seed_agent_notices, clear_agent_notices), and turn verdict
calculation (turn_verdict).
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterable
from typing import Any

from ..agent import prompts
from ..guest import GuestSession
from ..view import html as H
from ..wire.protocol import (
    turn_end,
    turn_error,
    turn_start,
)

log = logging.getLogger("bridge.turn")


def turn_verdict(
    ok: bool, degraded: str, produced: bool, subtype: str,
    stopped: bool = False, was_command: bool = False,
) -> tuple[bool, str]:
    """What the status bar should say when a turn ends.

    A turn the SDK says succeeded can still be a failure from the user's
    point of view if it degraded -- rate limited, an unhandled SDK event, or
    interrupted before producing anything -- AND produced nothing. A turn that
    degraded but STILL ANSWERED is reported as success: the user can see the
    answer, and telling them it failed is worse than saying nothing.

    A turn the user STOPPED is neither: it ends not-ok with subtype
    `stopped`, whatever the SDK called it (`error_during_execution` when a
    tool call was cut short), so the guest can say "stopped" rather than
    report a failure the user asked for.

    AND A TURN THAT SUCCEEDED WITHOUT SAYING ANYTHING IS NOT A SUCCESS.
    win/app/turn.cpp already carries the argument for the `ok=0` shape of
    this -- "a turn that came back not-ok is not a quiet success", written
    after a user reported "i'm just getting interrupted with no message
    nothing". This is the same failure wearing `ok=1`, and it was found by
    pointing a client at a bridge whose Claude Code was installed but NOT
    LOGGED IN (BACKLOG 46). `claude -v` exits 0 without credentials, so the
    startup preflight passes; the session CONNECTS, so the guard at the
    attach site never fires; and the turn then returns ok with no content
    at all. The Windows 98 client showed the user their own message and
    `done 0s $?` -- a turn reported complete, in no time, for no money.
    They would retype it, or decide the model had nothing to say.

    Reported not-ok, the guest already renders "The request did not
    complete: no_output. Nothing was returned -- it may not have run at
    all." That path has existed since 03.50, so this needs no protocol
    change and no caps bump, and an older client handles it too.

    EXCEPT FOR A SLASH COMMAND, which is output-less by design: `/compact`
    and `/rename` change the session and print nothing, and the caller
    below already explains that in a notice. Marking those failed would
    make the common case cry wolf, which is the mistake the comment at the
    foot of tests/python/agent/test_sdk_events.py was written about.

    Returns (ok, subtype). `subtype` is the raw string for the TURN frame;
    the guest maps it to a localized line.
    """
    if stopped:
        return False, "stopped"
    if degraded and not produced:
        return False, degraded
    if ok and not produced and not was_command:
        return False, "no_output"
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


    def seed_agent_notices(self, guest: GuestSession | None = None) -> None:
        """Fold whatever the baseline still owes the agent into memory."""
        # EXPLICIT OR NOTHING. These four fell back to
        # `connection.guest` when a caller passed none, which with one
        # machine was the same object and with two is a turn preamble built
        # from the wrong machine's sync state. Every production caller names
        # its guest; the parameter stays optional only because the tests
        # build these services without a connection at all.
        g = guest
        try:
            load_fn = getattr(self.sync, 'load_baseline', None)
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
            getattr(self.config, 'sync_dir', None)
        )
        if not delivered or not sync_dir:
            return
        # EXPLICIT OR NOTHING. These four fell back to
        # `connection.guest` when a caller passed none, which with one
        # machine was the same object and with two is a turn preamble built
        # from the wrong machine's sync state. Every production caller names
        # its guest; the parameter stays optional only because the tests
        # build these services without a connection at all.
        g = guest
        lock_ctx = getattr(self.sync, 'baseline_lock', None)
        if not lock_ctx:
            return
        async with lock_ctx(g):
            try:
                load_fn = getattr(self.sync, 'load_baseline', None)
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
                base_path_fn = getattr(self.sync, 'base_path', None)
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
            getattr(self.config, 'sync_dir', None)
        )
        # EXPLICIT OR NOTHING. These four fell back to
        # `connection.guest` when a caller passed none, which with one
        # machine was the same object and with two is a turn preamble built
        # from the wrong machine's sync state. Every production caller names
        # its guest; the parameter stays optional only because the tests
        # build these services without a connection at all.
        g = guest
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

    def with_workdir_preamble(self, text: str,
                              guest: GuestSession | None = None) -> str:
        r"""Name this conversation's folder on the machine, once.

        ONLY WHEN THERE IS NO MIRROR, and then it is the only channel that
        arrives in time. With a mirror the agent writes on this side and the
        mirror carries it, and the system prompt's mirrored paragraph says so
        already. Without one, every byte reaching the machine is a `put_file`
        to a path the agent types -- and on 2026-10-01 an agent with no path
        typed `C:\`, on somebody's real disk.

        The system prompt cannot do this job. It is built when the connection
        ATTACHES, before the user has said anything, and the folder is named
        from the conversation's title. By the time there is a title the prompt
        is frozen for the life of the session.

        So the folder is NAMED here, at the top of the turn (the title is the
        user's first message, which `coordinator.on_input` has already
        recorded), and CREATED at the first tool call by the machine itself.
        Naming makes no directory anywhere -- see
        `WorkspaceService.planned_guest_dir` -- so a conversation that is only
        talked to still touches nothing.
        """
        # EXPLICIT OR NOTHING. These four fell back to
        # `connection.guest` when a caller passed none, which with one
        # machine was the same object and with two is a turn preamble built
        # from the wrong machine's sync state. Every production caller names
        # its guest; the parameter stays optional only because the tests
        # build these services without a connection at all.
        g = guest
        if g is None or getattr(self.config, "sync_dir", None):
            return text
        if self.workspace is None:
            return text
        folder = self.workspace.planned_guest_dir(g)
        # Said once per FOLDER rather than once per conversation, so a folder
        # the user changes under the agent (SETWORK) is said again.
        if not folder or g.work_told == folder:
            return text
        g.work_told = folder
        return prompts.text("machine.sync.workdir_told", folder=folder) + text

    async def resume_turn(self, guest: GuestSession, answered) -> None:
        """Continue: wait for the wire to be ours, THEN unlatch, THEN explain."""
        async with guest.turn_lock:
            release_fn = getattr(self.sync, 'release_all_latches', None)
            freed = await release_fn(only=answered) if release_fn else 0
            if freed:
                log.info("sync: released %d latched path(s) on Continue", freed)
            await self.run_turn_locked(
                guest,
                prompts.text("machine.sync.resume_after_stop"
                             if guest.last_turn_stopped
                             else "machine.sync.resume"),
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
                getattr(self.session, 'resume_pending', None)
            )
            if resume_fn and not await resume_fn(guest):
                return
        if not guest.claude:
            return
        async with guest.turn_lock:
            match = getattr(self.session, "match_surface_locked", None)
            if match is not None:
                await match(guest)
            if not guest.claude:
                return
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
        if echo:
            await self.presentation.write_block(
                # STREAMED, like every other block. The guest echoed this
                # locally until stage 3b, which is why it was recorded and
                # not sent -- and that local copy was written into whatever
                # window happened to be on screen, including one the reader
                # had paged back to, where the record does not belong. It
                # also never quite matched: user_turn strips before escaping
                # and the guest's escaper did not, so a trailing blank line
                # rendered one line taller locally than in the copy shown
                # after paging back (audit 01.29). One source, one escaper,
                # one place it can land.
                guest, H.user_turn(display_text)
            )
        guest.turn_produced = False
        guest.turn_degraded = ""
        guest.turn_stopped = False
        await self.connection.send(guest, turn_start())
        was_command = text.lstrip().startswith("/")
        if not was_command:
            text = await self.with_sync_preamble(text, guest=guest)
            # AFTER the sync preamble, so it sits closest to the user's own
            # message: this is the instruction the next tool call acts on, and
            # the sync one is about a conflict that has already happened.
            text = self.with_workdir_preamble(text, guest=guest)
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
            await self.connection.send(guest, turn_error(str(exc)))
            return
        await self.presentation.flush_text(guest)
        if guest.claude:
            conv_store = self.conv
            if conv_store is not None:
                conv_store.add(guest.claude.session_id, guest.machine)
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
        stopped, guest.turn_stopped = guest.turn_stopped, False
        guest.last_turn_stopped = stopped
        ok, subtype = turn_verdict(
            turn.ok, degraded, guest.turn_produced, turn.subtype, stopped,
            was_command,
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
        await self.connection.send(
            guest,
            turn_end(
                ok, subtype, turn.cost_usd, turn.duration_ms, ctx_pct,
                conv=getattr(guest, "conv_id", ""),
            ),
        )

