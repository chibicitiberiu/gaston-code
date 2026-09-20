"""PermissionDesk: one guest's permission-prompt bar, and the queue behind it.

Before this, `Bridge` held roughly 250 lines split across the callback the
SDK calls into (`_can_use_tool`), the queue (`_enqueue_prompt`/
`_show_prompt`/`_send_waiting`/`_pump_queue`), reconnect recovery
(`_reissue_prompt`), the two ways a prompt ends without an answer
(`_finish_prompt` for a timeout or a cancellation, `_perm_reply` for a real
one), and the denial-on-teardown path (`_resolve_all`). All of it read or
wrote exactly two fields, `guest.perm_pending` and `guest.perm_queue` -- the
audit's "Prompts" `GuestSession` group (docs/audit/02-bridge-python.md #2,
#1.3) -- and nothing else in `Bridge` has a reason to reach into them
directly any more.

`_finish_prompt` (here, `_finish`) and `_resolve_all` (here, `deny_all`)
must stay synchronous: both run inside a cancelled scope -- the SDK's own
cancellation of an in-flight tool call, and `Bridge._on_client`'s teardown
before its first `await` -- where an `await` would simply never complete.
`_finish` schedules its continuation with `spawn` rather than awaiting it,
which is exactly what `Bridge._spawn` already existed for.

Built once per `GuestSession`, in `Bridge._attach`, and held there as
`guest.perms` -- the same lifetime as the guest itself, because a prompt on
screen must survive a reconnect. `write_block`, `send` and `spawn` are the
only things this needs from `Bridge`: writing the prompt's description into
the transcript, putting a frame on the wire, and scheduling a continuation
from a cancelled scope. The policy -- what is off-limits, which suggestions
imply "Always", how a request renders -- stays in `permissions.py`; this is
the state machine that sits on top of it.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections import deque

from . import doscmd
from . import permissions as perms
from .permissions import PendingPrompt, render_request
from ..status import PromptEndReason
from ..wire.protocol import permend, permreq, permstate, permwait
from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny

log = logging.getLogger("bridge.permdesk")


class PermissionDesk:
    """What is on the guest's screen right now, and what is queued behind it.

    `write_block(html)`, `send(frame)` and `spawn(coro)` are bound closures
    over one `GuestSession`, handed in at construction (`Bridge._attach`)
    rather than reached for through a stored `Bridge` reference -- the same
    reason `SyncTransport` (bridge/files/synctransport.py) holds a `guest`
    rather than a `bridge`.
    """

    def __init__(self, guest, *, cwd: str | None, write_block, send,
                spawn, ensure_work=None) -> None:
        self._guest = guest
        self._cwd = cwd
        self._write_block = write_block
        self._send = send
        self._spawn = spawn
        #: Commit this conversation to a work folder, awaited before the
        #: guard runs. A tool call is what commits one, and the PERMISSION
        #: REQUEST is the earliest moment of a tool call -- earlier than
        #: `on_tool` and earlier than the tool body. Without it the guard
        #: would measure containment against a folder that does not exist
        #: yet and announce "no folder is mirrored" for a command that is
        #: about to run in one.
        self._ensure_work = ensure_work
        #: The prompt on screen, or None. `GuestSession.perm_pending`
        #: forwards here, the same pattern `link: GuestLink` established for
        #: `writer`/`send_lock`/`zip_out`/`link_bps`.
        self.pending: PendingPrompt | None = None
        #: Prompts waiting behind it. A guest can show one bar; the model can
        #: request tools in parallel, so the rest wait rather than racing for
        #: it. `GuestSession.perm_queue` forwards here for the same reason.
        self.queue: deque = deque()

    @property
    def busy(self) -> bool:
        """Is there a prompt on screen right now."""
        return self.pending is not None

    # --- the callback claude_agent_sdk calls into ---------------------------

    def callback(self):
        """Build the can_use_tool callback for this guest.

        Everything that reaches here gets a bar. There is deliberately no
        auto-allow branch: measured against the real CLI, a Read or Grep
        inside cwd never reaches this callback at all, and a Bash command its
        sandbox can contain is approved without asking us either. The policy
        the plan carried would have been dead code.
        """

        async def can_use_tool(tool_name: str, args: dict, ctx):
            tool_use_id = getattr(ctx, "tool_use_id", None)
            if not tool_use_id:
                # types.py promises this is always a non-empty string here.
                # If it is not, we have no way to correlate a reply, and
                # guessing would approve the wrong call.
                log.error("permission request with no tool_use_id: %s",
                          tool_name)
                return PermissionResultDeny(
                    message="Internal error: the request had no id.",
                    interrupt=False)

            # The proxy is infrastructure, not the subject of the
            # conversation. A path tool reaching outside the working copy is
            # refused without troubling the user: they asked about THEIR
            # machine, and answering with the proxy's files would be wrong
            # even when the file is real.
            refuse = perms.off_limits(tool_name, args,
                                      self._cwd or os.getcwd())
            if refuse is not None:
                log.info("auto-refused %s outside the working copy: %s",
                         tool_name, str(args)[:120])
                return PermissionResultDeny(message=refuse, interrupt=False)

            # What this command reaches outside the mirrored folder, if
            # anything. It ONLY EVER ADDS A WARNING.
            #
            # That asymmetry is the whole design. The bar clips its line at
            # 160 characters and "Run on your machine: " eats 21 of them, so
            # roughly 139 bytes of a command that may be ~1000 are visible --
            # and attaching a reassuring "stays in the mirrored folder" to a
            # truncated line would be manufactured reassurance, strictly less
            # honest than today's bare command. So a contained command is
            # annotated with nothing at all, and the user reads the command
            # exactly as they do now.
            #
            # It gates nothing yet either. The verdict is displayed and
            # nothing acts on it, which is deliberate: it earns its way onto
            # real traffic before anything depends on it.
            warning = None
            if tool_name.startswith("mcp__machine__"):
                if self._ensure_work is not None:
                    await self._ensure_work()
                # THE LIVE FOLDER, not HELLO's `sync_root`. That header
                # carries the machine's CONFIGURED root -- where folders get
                # created -- and containment has to be measured against the
                # tree actually being mirrored for THIS conversation, which
                # is narrower. Measuring against the parent would call a
                # write into a sibling conversation's folder "inside the
                # mirror", which is the check inverted rather than loose.
                # An empty root means nothing is mirrored, and doscmd.reason
                # refuses outright on one rather than letting
                # `"".startswith("")` call every path contained.
                warning = doscmd.reason(
                    str((args or {}).get("command") or ""),
                    cwd=str((args or {}).get("cwd") or ""),
                    guest_root=str(
                        getattr(self._guest, "work_guest", "") or ""))
                if warning:
                    log.info("guard: %s -- %s", warning, str(args)[:80])

            loop = asyncio.get_running_loop()
            prompt = PendingPrompt(
                tool_use_id=tool_use_id,
                tool_name=tool_name,
                future=loop.create_future(),
                suggestions=list(getattr(ctx, "suggestions", None) or []),
                body=render_request(tool_name, args, ctx),
                allows=perms.allows_always(
                    tool_name, getattr(ctx, "suggestions", None) or []),
                summary=(("! %s -- %s"
                          % (warning, perms.summary(tool_name, ctx, args)))
                         if warning
                         else perms.summary(tool_name, ctx, args)),
            )

            try:
                await self._enqueue(prompt)
                return await asyncio.wait_for(prompt.future,
                                              timeout=perms.DEADLINE_S)
            except asyncio.TimeoutError:
                self._finish(prompt, PromptEndReason.TIMEOUT)
                return PermissionResultDeny(
                    message=("Nobody answered the permission request on the "
                             "Windows machine within ten minutes, so it was "
                             "denied. Ask again if you still need it."),
                    interrupt=False)
            except asyncio.CancelledError:
                # The CLI cancelled us -- the turn was interrupted, or another
                # client answered. We are inside a cancelled scope, so we may
                # NOT await here: the send would abort before it happened and
                # the guest would keep a bar it can never dismiss. Schedule it
                # instead; create_task is synchronous and the new task is not
                # in the cancelled scope.
                self._finish(prompt, PromptEndReason.CANCELLED)
                raise

        return can_use_tool

    # --- the queue -----------------------------------------------------------

    async def _enqueue(self, prompt: PendingPrompt) -> None:
        """Show a prompt, or queue it behind the one already on screen."""
        if self.pending is not None:
            self.queue.append(prompt)
            await self._send_waiting()
            return
        self.pending = prompt
        await self._show(prompt)

    async def _show(self, prompt: PendingPrompt) -> None:
        # The description goes into the transcript as a normal block so it
        # paginates and survives paging away and back. The bar itself carries
        # only the summary, because a guest in HISTORY mode never sees this.
        await self._write_block(prompt.body)
        await self._send(permreq(
            prompt.tool_use_id, prompt.tool_name, prompt.allows,
            len(self.queue), prompt.summary))

    async def _send_waiting(self) -> None:
        """Refresh the queue depth shown on the bar."""
        prompt = self.pending
        if prompt is None:
            return
        await self._send(permwait(prompt.tool_use_id, len(self.queue)))

    async def _pump(self) -> None:
        """Show the next queued prompt, discarding any that died waiting."""
        while self.pending is None and self.queue:
            nxt = self.queue.popleft()
            if nxt.future.done():
                continue
            self.pending = nxt
            await self._show(nxt)

    # --- ending a prompt ------------------------------------------------------

    def _finish(self, prompt: PendingPrompt, reason: PromptEndReason) -> None:
        """Synchronous bookkeeping plus a scheduled PERMEND.

        Deliberately not a coroutine. It is called from a cancelled context,
        where any await would be aborted before it ran.
        """
        if self.pending is not prompt:
            # Already resolved by another path; nothing owed.
            return
        self.pending = None
        self._spawn(self._after(prompt.tool_use_id, reason))

    async def _after(self, tool_use_id: str, reason: PromptEndReason) -> None:
        # .value, not the member itself -- Frame.encode() calls str() on
        # every header value, and str() on a bare (str, Enum) member is
        # "PromptEndReason.X", not the wire token "x" (bridge/status.py).
        await self._send(permend(tool_use_id, reason.value))
        await self._pump()

    async def reply(self, want: str, action: str) -> None:
        """Answer from the guest's own prompt bar -- a PERMREPLY frame."""
        prompt = self.pending
        if prompt is None or prompt.tool_use_id != want:
            # Not a bug on its own: the prompt may have been resolved by the
            # deadline, by a disconnect, or by a session rebuild while the
            # reply was in flight. Ignoring is right; crashing is not.
            log.info("PERMREPLY for unknown id %s (action=%s)", want, action)
            return

        if prompt.future.done():
            self.pending = None
            await self._pump()
            return

        if action == "allow":
            result = PermissionResultAllow()
        elif action == "always":
            result = PermissionResultAllow(
                updated_permissions=perms.session_scoped(prompt.suggestions))
        else:
            result = PermissionResultDeny(
                message=("The user declined this on the Windows machine. "
                         "Do not retry it; consider another approach, or ask "
                         "them what they would prefer."),
                interrupt=False)

        prompt.future.set_result(result)
        self.pending = None
        log.info("permission %s for %s (%s)", action, prompt.tool_name, want)
        await self._send(permend(want, PromptEndReason.ANSWERED.value))
        await self._pump()

    async def reissue(self) -> None:
        """P1: after a reconnect, put the bar back."""
        prompt = self.pending
        if prompt is None:
            return
        log.info("re-issuing pending prompt %s after reconnect",
                 prompt.tool_use_id)
        await self._send(permstate(
            prompt.tool_use_id, prompt.tool_name, prompt.allows,
            len(self.queue), prompt.summary))

    def deny_all(self, reason: str, message: str) -> None:
        """Deny everything outstanding, synchronously.

        Called from `Bridge._on_client`'s finally before its first await, and
        from the session-rebuild paths. Denying rather than leaving them
        pending is the point: a future nobody will ever complete hangs the
        turn until the deadline, on a machine that has already gone away.
        """
        pending = []
        if self.pending is not None:
            pending.append(self.pending)
            self.pending = None
        while self.queue:
            pending.append(self.queue.popleft())
        for prompt in pending:
            if not prompt.future.done():
                prompt.future.set_result(
                    PermissionResultDeny(message=message, interrupt=False))
        if pending:
            log.info("denied %d pending prompt(s): %s", len(pending), reason)
