"""PermissionDesk: one guest's permission-prompt bar, and the queue behind it.

This used to be ~250 lines spread across `Bridge`, all of it reading or
writing exactly two fields, `guest.perms.pending` and `guest.perms.queue` --
the audit's "Prompts" `GuestSession` group (docs/audit/02-bridge-python.md
#2, #1.3). Nothing else in `Bridge` has a reason to reach into them directly
any more.

`_finish` and `deny_all` must stay synchronous: both run inside a cancelled
scope -- the SDK's own cancellation of an in-flight tool call, and
`ConnectionService.on_client`'s teardown before its first `await` -- where an
`await` would simply never complete. `_finish` schedules its continuation
with `spawn` rather than awaiting it, which is exactly what `Bridge._spawn`
already existed for.

Built once per `GuestSession`, in `ConnectionService.attach`, and held there
as `guest.perms` -- the same lifetime as the guest itself, because a prompt
on screen must survive a reconnect. `write_block`, `send` and `spawn` are the
only things this needs from `Bridge`: writing the prompt's description into
the transcript, putting a frame on the wire, and scheduling a continuation
from a cancelled scope. The policy -- what is off-limits, which suggestions
imply "Always", how a request renders -- stays in `permissions.py`; this is
the state machine that sits on top of it.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections import deque

from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny

from ..status import PromptEndReason
from ..view import html as H
from ..wire.protocol import permend, permreq, permstate, permwait
from . import categories, policy, prompts, review, toolrules
from . import permissions as perms
from .permissions import PendingPrompt, render_request

log = logging.getLogger("bridge.permdesk")

#: What the guest's bar can hold. `rc_bar::base_text` is `char[224]`, and the
#: control it draws into is one `SS_ENDELLIPSIS` line -- so roughly the first
#: seventy characters are what a person actually reads, and everything past
#: 223 is not stored at all.
BAR_BYTES = 216
#: How much of that the COMMAND is entitled to before the warning may have
#: any. `perms.summary` clips itself to 160; this is the floor underneath it.
BAR_SUMMARY_SHARE = 140


def _bar_line(summary: str, warning: str) -> str:
    """The one line on the guest's prompt bar.

    THE COMMAND COMES FIRST, and that is half of 02.4. It used to read
    `"! %s -- %s" % (warning, summary)` -- warning first, and the
    concatenation was not re-clipped, so on a 640x480 bar the user saw

        ! no folder is mirrored from this machine, so nothing on it can be re...

    and nothing whatever of the command they were being asked to approve. The
    other half of that fix was saying the deployment gap once rather than on
    every prompt; the classifier (categories.py) now says what a call
    touches instead of what it might reach.

    The marker stays at the front because two characters cost nothing and are
    what tells a reader there is something to read; the warning itself goes
    at the tail, where it survives for anything that can show more than one
    line, and into the transcript block in full, which is where it can
    actually be read.
    """
    if not warning:
        return summary
    head = summary[:BAR_SUMMARY_SHARE]
    tail = warning[:BAR_BYTES - BAR_SUMMARY_SHARE - 8]
    return "! " + head + "  -- " + tail


def _grant_words(key: str) -> str:
    """What one grant key lets happen, for the request block above the
    bar -- "This chat" on the button is only as good as this sentence."""
    cat, _, subject = key.partition(":")
    if subject:
        return prompts.text("policy.grant_subject", path=subject)
    return prompts.text(f"policy.cat.{cat}")


def _wait_words(seconds: float) -> str:
    """"10 minutes", "1 minute", "45 seconds" -- the deadline as the
    agent is told it, now that the client can choose it."""
    if seconds < 120:
        n = max(1, round(seconds))
        return f"{n} second{'s' if n != 1 else ''}"
    n = round(seconds / 60)
    return f"{n} minute{'s' if n != 1 else ''}"


#: Categories that stay inside the work folder, and so carry no warning.
_CONTAINED = (categories.READ, categories.FILES_WORK, categories.RUN_WORK,
              categories.RUN_BUILT)


class PermissionDesk:
    """What is on the guest's screen right now, and what is queued behind it.

    `write_block(html)`, `send(frame)` and `spawn(coro)` are bound closures
    over one `GuestSession`, handed in at construction (`ConnectionService.attach`)
    rather than reached for through a stored `Bridge` reference -- the same
    reason `SyncTransport` (bridge/files/synctransport.py) holds a `guest`
    rather than a `bridge`.
    """

    def __init__(self, guest, *, cwd: str | None, write_block, send,
                spawn, ensure_work=None, preview=None, fetch=None,
                on_mode=None) -> None:
        self._guest = guest
        #: `async (kind, path) -> names | bytes | None`: what the classifier
        #: asks to see before it can judge a program ("ls" a folder, "get"
        #: a script). None: anything it asks for is doubt.
        self._fetch = fetch
        #: `async () -> None`: the mode changed from here (Plan first was
        #: approved), so remember it and resend the menu.
        self._on_mode = on_mode
        #: `async (tool_name, args) -> str`: what a delete, copy or move is
        #: about to touch, counted on the guest (agent/review.py). Optional:
        #: a desk without one shows the bar without the count.
        self._preview = preview
        #: Calls this desk approved, (key, when), for `gate`.
        self._approved: deque = deque(maxlen=64)
        self._gate_seq = 0
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
        #: Where the agent's session actually runs, which is not always
        #: `_cwd`. A session's cwd is fixed when it is built and a folder is
        #: committed at the first tool call, so for a new conversation the
        #: agent runs here while the mirror is a folder underneath. Only used
        #: to tell a near miss from a genuinely foreign path.
        self._shared = cwd
        #: The prompt on screen, or None. `GuestSession.perm_pending`
        #: forwards here, the same pattern `link: GuestLink` established for
        #: `writer`/`send_lock`/`zip_out`/`link_bps`.
        self.pending: PendingPrompt | None = None
        #: Prompts waiting behind it. A guest can show one bar; the model can
        #: request tools in parallel, so the rest wait rather than racing for
        #: it.
        self.queue: deque = deque()

    @property
    def busy(self) -> bool:
        """Is there a prompt on screen right now."""
        return self.pending is not None

    # --- the second gate, for calls that never reached the first ---------
    #
    # MEASURED on the VM (2026-10-02): in Auto, with Sonnet, the CLI approved
    # mkdir, put_file and a whole-folder delete ITSELF and never called
    # `can_use_tool` -- so the preview and the reviewer this desk runs were
    # skipped for exactly the mode they were built for. The dangerous machine
    # tools therefore ask again from inside the tool: a call the desk already
    # approved passes at once; one that came round it is decided here, by the
    # same code, and a reviewer that is unsure puts the bar up from in there.

    #: How long an approval from the desk stands for the tool that follows.
    APPROVAL_S = 300.0

    @staticmethod
    def _key(tool_name: str, args) -> str:
        return tool_name + "\0" + json.dumps(args or {}, sort_keys=True,
                                             default=str)

    async def gate(self, tool_name: str, args: dict) -> str | None:
        """None when the call may go ahead; otherwise why it may not, in
        words for the agent."""
        key = self._key(tool_name, args)
        now = time.monotonic()
        while self._approved and now - self._approved[0][1] > self.APPROVAL_S:
            self._approved.popleft()
        for i, (k, _) in enumerate(self._approved):
            if k == key:
                del self._approved[i]
                return None
        self._gate_seq += 1

        class _Ctx:
            tool_use_id = f"gate-{self._gate_seq}"
            suggestions: tuple = ()
            title = description = display_name = None

        log.info("second gate for %s: it did not come through the desk",
                 tool_name)
        result = await self.callback()(tool_name, args, _Ctx())
        self._approved_take(key)
        if isinstance(result, PermissionResultAllow):
            return None
        return getattr(result, "message", "") or "Refused."

    def _approved_take(self, key: str) -> None:
        for i, (k, _) in enumerate(self._approved):
            if k == key:
                del self._approved[i]
                return

    def _deadline(self) -> float:
        """How long a bar waits for an answer: the client's choice (Options
        > Permissions), else the bridge's ten minutes."""
        want = getattr(getattr(self._guest, "prefs", None),
                       "perm_deadline_s", 0)
        return float(want) if want else perms.DEADLINE_S

    # --- classification ----------------------------------------------------

    def _ctx(self, lists=None) -> categories.Ctx:
        g = self._guest
        prefs = getattr(g, "prefs", None)
        return categories.Ctx(
            caps=getattr(g, "caps", None),
            shells=tuple(getattr(f, "name", "") for f in
                         toolrules.shells_of(getattr(g, "tools", None))),
            work_root=str(getattr(g, "work_guest", "") or ""),
            bridge_dir=str(getattr(g, "work_dir", "") or ""),
            lists=(lists if lists is not None
                   else getattr(prefs, "lists", None) or categories.Lists()),
            resolve=getattr(g, "on_guest", None))

    async def classify(self, tool_name: str, args: dict,
                       lists=None) -> categories.Verdict:
        """`categories.classify`, answering what it asks for from the guest.

        FROM THE GUEST, not the mirror: a script an exec wrote a moment ago
        is on the machine before it is in the mirror, and judging the
        mirror's older copy would judge a file that is not the one about to
        run. Anything that cannot be fetched is doubt, never a pass.
        """
        ctx = self._ctx(lists)
        v = categories.classify(ctx, tool_name, args)
        for _ in range(categories.MAX_DEPTH + 2):
            if not v.wants:
                return v
            got: dict = {}
            for kind, path, _key in v.wants:
                got[(kind, path)] = (await self._fetch(kind, path)
                                     if self._fetch is not None else None)
            categories.fill(ctx, v.wants, got)
            v = categories.classify(ctx, tool_name, args)
        if v.wants:
            v.add(categories.RUN_ELSE, prompts.text(
                "policy.rule.unread", program=v.wants[0][1]), doubt=True)
            v.wants = []
        return v

    async def _leave_plan(self):
        """The plan was approved: back to the mode the chat was in before
        Plan first, and tell the CLI it is no longer planning. The setMode
        update is the ONLY `updated_permissions` this bridge forwards."""
        from claude_agent_sdk.types import PermissionUpdate
        g = self._guest
        back = str(getattr(g, "mode_before_plan", "") or "")
        if policy.lookup(back, getattr(getattr(g, "prefs", None), "modes",
                                       None)) is None or back == "plan":
            back = policy.DEFAULT
        g.mode_id = back
        if self._on_mode is not None:
            await self._on_mode()
        return PermissionResultAllow(updated_permissions=[PermissionUpdate(
            type="setMode", mode="default", destination="session")])

    # --- the callback claude_agent_sdk calls into ---------------------------

    def callback(self):
        inner = self._callback()

        async def recording(tool_name: str, args: dict, ctx):
            result = await inner(tool_name, args, ctx)
            if isinstance(result, PermissionResultAllow):
                self._approved.append((self._key(tool_name, args),
                                       time.monotonic()))
            return result

        return recording

    def _callback(self):
        """Build the can_use_tool callback for this guest.

        What reaches here is decided by the conversation's execution mode
        (policy.py) on what the call touches (categories.py). Measured
        against the real CLI, a Read or Grep inside cwd never reaches this
        callback at all, and a Bash command its sandbox can contain is
        approved without asking us either -- which is why the bridge's own
        reads sit outside the matrix and `bridge_shell` only ever sees the
        commands the sandbox could NOT contain.
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
                                      self._cwd or os.getcwd(),
                                      shared=self._shared)
            if refuse is not None:
                log.info("auto-refused %s outside the working copy: %s",
                         tool_name, str(args)[:120])
                return PermissionResultDeny(message=refuse, interrupt=False)

            # A step list the tool would refuse is refused HERE, before the
            # bar: asking the person to approve a list that cannot run costs
            # them a click per mistake and tells them nothing.
            if tool_name == "mcp__machine__input":
                from .steps import precheck
                why = precheck(args)
                if why is not None:
                    log.info("auto-refused a malformed input list: %s", why)
                    return PermissionResultDeny(
                        message=prompts.text("tools.input.refused", why=why),
                        interrupt=False)

            # WHAT IT TOUCHES, AND WHAT THIS CONVERSATION'S MODE SAYS. One
            # analysis (categories.py), one table (policy.py): the bar, the
            # checker and the log all explain the same finding.
            if tool_name.startswith("mcp__machine__") and \
                    self._ensure_work is not None:
                await self._ensure_work()
            verdict = await self.classify(tool_name, args)
            mode = policy.mode_of(self._guest)
            decision = policy.decide(mode, verdict,
                                     getattr(self._guest, "grants", ()))
            log.info("%s under %s: %s (%s)", tool_name, mode.id,
                     decision.value, decision.why or "-")
            if decision.value == policy.REFUSE:
                return PermissionResultDeny(message=decision.why,
                                            interrupt=False)
            if decision.value == policy.OFF:
                return PermissionResultDeny(
                    message=prompts.text(
                        "policy.off", mode=mode.label,
                        why=decision.why or tool_name),
                    interrupt=False)
            if decision.value == policy.ALLOW:
                return PermissionResultAllow()
            # A WARNING ONLY FOR WHAT LEAVES THE WORK FOLDER. A contained
            # call reads on the bar exactly as its command does: a "stays in
            # the work folder" tag would be reassurance, and the bar clips --
            # a truncated reassurance is less honest than none.
            warning = (decision.why if decision.hit is not None
                       and (decision.hit.doubt
                            or decision.hit.cat not in _CONTAINED) else "")

            # WHAT IT TOUCHES, counted on the guest, for a delete, copy or
            # move: the person approving "delete C:\\WORK\\OLD" needs to know
            # that is 132 files.
            bar_args = args
            preview_text = ""
            if tool_name.startswith("mcp__machine__") and self._preview:
                preview_text = await self._preview(tool_name, args)
                if preview_text:
                    bar_args = {**(args or {}), "_preview": preview_text}

            # CHECK: A SECOND OPINION INSTEAD OF A BAR. The checker can
            # approve; it cannot refuse. Unsure, unreachable or slow, it hands
            # the question to the person with its reason.
            if decision.value == policy.CHECK:
                cat = decision.hit.cat if decision.hit is not None else ""
                approved, why = await review.review(
                    self._guest, tool_name, args, preview_text,
                    finding="; ".join(h.rule for h in verdict.hits),
                    category=(prompts.text(f"policy.cat.{cat}")
                              if cat else ""))
                if approved:
                    log.info("checker approved %s: %s", tool_name, why)
                    await self._write_block(H.notice(prompts.text(
                        "review.approved",
                        what=perms.summary(tool_name, ctx, bar_args),
                        why=why)))
                    return PermissionResultAllow()
                log.info("checker asks the person about %s: %s",
                         tool_name, why)
                # The checker's reason FIRST: the bar has room for one
                # clause after the command, and "why the checker could not
                # decide" is what the person needs to decide themselves.
                # The classifier's finding follows, and is in full in the
                # transcript block.
                note = prompts.text("review.asks", why=why)
                warning = f"{note} ({warning})" if warning else note

            loop = asyncio.get_running_loop()
            keys = policy.grant_keys(decision)
            grant_note = ""
            if keys:
                grant_note = prompts.text(
                    "policy.grant_note", what="; ".join(
                        _grant_words(k) for k in keys))
            prompt = PendingPrompt(
                tool_use_id=tool_use_id,
                tool_name=tool_name,
                future=loop.create_future(),
                suggestions=list(getattr(ctx, "suggestions", None) or []),
                body=render_request(tool_name, bar_args, ctx,
                                    warning=warning or "",
                                    note=grant_note),
                # "Allow for this chat", not the CLI's Always: a grant of
                # this call's CATEGORY, kept with the conversation, offered
                # only where policy.GRANTABLE says one click may buy it.
                grants=keys,
                allows=bool(keys),
                summary=_bar_line(perms.summary(tool_name, ctx, bar_args),
                                  warning or ""),
            )

            try:
                await self._enqueue(prompt)
                result = await asyncio.wait_for(prompt.future,
                                                timeout=self._deadline())
                if verdict.plan_exit and isinstance(result,
                                                    PermissionResultAllow):
                    return await self._leave_plan()
                return result
            except TimeoutError:
                self._finish(prompt, PromptEndReason.TIMEOUT)
                return PermissionResultDeny(
                    message=prompts.text(
                        "policy.timed_out", wait=_wait_words(self._deadline())),
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

        # Coalescers must be flushed before the bar is shown, or buffered text
        # arrives out of order behind the prompt.
        assert not any(c._buf for c in self._guest.view.coalescers.values())

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

        result: PermissionResultAllow | PermissionResultDeny
        granted = False
        if action == "allow":
            result = PermissionResultAllow()
        elif action == "always" and prompt.grants:
            # NOTHING IS FORWARDED TO THE CLI. Its own rules would keep
            # approving after the mode changed, whatever the new table said
            # (the plan's review, finding 6); a grant lives here, beside the
            # mode it was given under, and goes when that mode does.
            self._guest.grants = set(getattr(self._guest, "grants", ())
                                     or ()) | set(prompt.grants)
            granted = True
            result = PermissionResultAllow()
        elif action == "always":
            result = PermissionResultAllow()
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
        if granted:
            self._settle_granted()
            if self._on_mode is not None:
                await self._on_mode()          # remember it with the chat
        await self._pump()

    def _covered(self, prompt: PendingPrompt) -> bool:
        have = set(getattr(self._guest, "grants", ()) or ())
        return bool(prompt.grants) and all(
            k in have or k.partition(":")[0] in have for k in prompt.grants)

    def _settle_granted(self) -> None:
        """Queued bars the grant just given covers are answered with it --
        the person said "this kind, for this chat", and making them press
        it again for the next one in line is asking twice."""
        keep: deque = deque()
        while self.queue:
            p = self.queue.popleft()
            if not p.future.done() and self._covered(p):
                p.future.set_result(PermissionResultAllow())
                log.info("granted for this chat: %s", p.tool_name)
            else:
                keep.append(p)
        self.queue = keep

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

        Called from `ConnectionService.on_client`'s finally before its first await, and
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
