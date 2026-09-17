"""Bridge listener: the one TCP port the Win98 guest dials out to.

The guest is a single executable with a single connection. It knows one config
value (this host:port) and needs no stable LAN address, no inbound listener and
no discovery.

Security posture, stated rather than inherited: the shared secret travels in
plaintext and is an accepted risk on a trusted LAN segment. Bind to that
segment's interface, not 0.0.0.0. The secret grants everything at once --
approving permission prompts and, from Phase 3, feeding tool results to Claude.
"""

from __future__ import annotations

import asyncio
import logging
import os
import secrets
import time
import uuid
from collections import deque
from dataclasses import dataclass, field

from . import fold
from . import machine
from .registry import Registry
from .coalesce import Coalescer
from .compress import ENC_DEFLATE, ENC_RAW, StreamCompressor, should_compress
from . import html as H
from .html import APPEND, LIVE, HtmlStream
from .pages import Paginator, limits_for
from . import permissions as perms
from .permissions import PendingPrompt, render_request
from .protocol import (
    MAX_FRAME,
    PROTO_VERSION,
    Frame,
    FrameReader,
    ProtocolError,
    error,
)
from .replay import Replay
from . import sessions as sesslist
from .session import Session
from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny

log = logging.getLogger("bridge.server")

DEFAULT_PORT = 7890


@dataclass
class GuestSession:
    """Bridge-side state for one guest, surviving across reconnects."""

    sess_id: str
    replay: Replay = field(default_factory=Replay)
    claude: Session | None = None
    writer: asyncio.StreamWriter | None = None
    send_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    # One persistent deflate stream for the whole connection. Resetting it
    # mid-connection would desynchronise the guest's inflater, so it is only
    # reset on reconnect -- see _attach.
    zip_out: StreamCompressor = field(default_factory=StreamCompressor)
    link_bps: float | None = None
    # Chosen from the Model and Effort menus. Held per guest rather than per
    # process so a reconnect or an effort fork keeps the choice.
    model: str = ""
    effort: str = ""
    # Page caps for THIS guest, scaled to the RAM it reported in HELLO.
    page_bytes: int = 0
    page_blocks: int = 0
    # The machine facts from HELLO, kept so every session this guest opens
    # gets the same description of the computer it is acting for.
    hello: dict = field(default_factory=dict)

    # Which page the guest is showing. None means LIVE (follow the last page).
    # In HISTORY mode the bridge keeps buffering silently; the page counter
    # ticking up in the status bar is the new-content notification.
    view_page: int | None = None

    # One coalescer per source stream (assistant text, thinking). Coalescing
    # happens BEFORE rendering, so the renderer receives whole lines in ~2KB
    # batches rather than single tokens -- which is what lets it close blocks
    # properly and keeps the number of DOM updates on the guest down.
    coalescers: dict = field(default_factory=dict)
    render: HtmlStream = field(default_factory=HtmlStream)

    # The transcript lives here, in full. The guest only ever holds one page,
    # which is what bounds its memory by page size rather than by conversation
    # length.
    pages: Paginator = field(default_factory=Paginator)

    # The permission prompt currently on the guest's screen, and the ones
    # queued behind it. The model can request tools in parallel; a single
    # guest can show one bar, so the rest wait rather than racing for it.
    perm_pending: "PendingPrompt | None" = None
    perm_queue: deque = field(default_factory=deque)
    # When the guest was last heard from, for the idle reaper. A guest that
    # GPFs leaves no FIN behind, so without this _on_client parks forever and
    # `connected` stays True -- which rejects every reconnect as "busy".
    last_seen: float = field(default_factory=time.monotonic)
    # When the link dropped, or None while connected. A prompt outlives a
    # brief disconnect so a reconnect can restore the bar.
    lost_at: float | None = None

    @property
    def connected(self) -> bool:
        return self.writer is not None and not self.writer.is_closing()


class Bridge:
    def __init__(
        self,
        secret: str,
        *,
        host: str = "127.0.0.1",
        port: int = DEFAULT_PORT,
        cwd: str | None = None,
        model: str | None = None,
        effort: str | None = None,
        link_bps: float | None = None,
    ) -> None:
        self.secret = secret
        # Which sessions this bridge started, so the chat list can show those
        # and not every conversation on the machine. See bridge/registry.py.
        self.registry = Registry()
        # Measured link speed. None means "unknown, assume it might be slow".
        # Set from the guest's STAT frames; overridable for testing so the
        # dialup path can be exercised without a modem.
        self.link_bps = link_bps
        self.host = host
        self.port = port
        self.cwd = cwd
        self.model = model
        self.effort = effort

        # One guest, one session. See PLAN.md "Multi-session".
        self._guest: GuestSession | None = None
        self._server: asyncio.Server | None = None
        # Strong references to fire-and-forget tasks. asyncio holds only a
        # weak one, so a bare create_task can be collected mid-flight.
        self._tasks: set = set()
        # Frames pipelined behind HELLO in a single read, handed to _pump.
        self._early: list = []
        self._reaper: asyncio.Task | None = None
        # The guest pings every few seconds, so silence much longer than that
        # means it is gone rather than quiet. Generous, because a 200MHz box
        # under load genuinely does pause.
        self.idle_timeout_s = 90.0
        self.idle_check_s = 15.0
        # How long a pending permission request survives a dropped link. Long
        # enough for the guest's 1/2/4/8s reconnect backoff to win, far short
        # of the 10 minute answer deadline.
        self.perm_grace_s = 60.0

    # --- lifecycle ---------------------------------------------------------

    async def serve_forever(self) -> None:
        self._server = await asyncio.start_server(
            self._on_client, self.host, self.port
        )
        addr = ", ".join(str(s.getsockname()) for s in self._server.sockets or [])
        log.info("listening on %s", addr)
        async with self._server:
            await self._server.serve_forever()

    async def close(self) -> None:
        if self._reaper is not None:
            self._reaper.cancel()
            self._reaper = None
        if self._server:
            self._server.close()
            await self._server.wait_closed()
        if self._guest and self._guest.claude:
            await self._guest.claude.close()

    # --- connection --------------------------------------------------------

    async def _on_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        peer = writer.get_extra_info("peername")
        log.info("connection from %s", peer)
        frames = FrameReader()
        # Started here rather than in serve_forever() because that is not the
        # only way a bridge gets clients -- the tests drive _on_client
        # directly, and a liveness mechanism that depends on which entry point
        # was used is a liveness mechanism you do not have.
        if self._reaper is None:
            self._reaper = asyncio.create_task(self._reap_idle())

        try:
            hello = await self._read_hello(reader, frames, writer)
            if hello is None:
                return

            guest = await self._attach(hello, writer)
            if guest is None:
                return

            await self._pump(guest, reader, frames)

        except ProtocolError as exc:
            # Framing is lost; never try to resync a byte stream.
            log.warning("protocol error from %s: %s", peer, exc)
            await self._try_send_raw(writer, error("protocol", str(exc)))
        except (ConnectionResetError, BrokenPipeError):
            log.info("%s dropped", peer)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("unhandled error serving %s", peer)
        finally:
            if self._guest and self._guest.writer is writer:
                self._guest.writer = None
                # A pending prompt is NOT denied here. Denying on any drop
                # tells the model "the user declined" for something the user
                # never saw -- and on a 1998 NE2000 over a flaky link, a blip
                # would become a refusal. Instead the prompt is held for a
                # short grace period: reconnect inside it and the bar comes
                # back (PERMSTATE), miss it and the reaper denies with a
                # message that says what actually happened.
                self._guest.lost_at = time.monotonic()
                if self._guest.perm_pending is not None:
                    log.info("link lost with a prompt pending; holding %.0fs",
                             self.perm_grace_s)
            # Graceful close. An abrupt RST is reported to crash Win98's
            # Winsock stack outright, taking the machine with it.
            try:
                if writer.can_write_eof():
                    writer.write_eof()
            except Exception:
                pass
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass
            log.info("connection to %s closed", peer)

    async def _read_hello(
        self,
        reader: asyncio.StreamReader,
        frames: FrameReader,
        writer: asyncio.StreamWriter,
    ) -> Frame | None:
        """First frame must be HELLO, within a short deadline."""
        try:
            async with asyncio.timeout(10):
                while True:
                    data = await reader.read(4096)
                    if not data:
                        return None
                    batch = frames.feed(data)
                    for i, f in enumerate(batch):
                        if f.type != "HELLO":
                            await self._try_send_raw(
                                writer, error("expected_hello",
                                              f"first frame was {f.type}")
                            )
                            return None
                        # Anything pipelined behind HELLO in the same read has
                        # already been parsed out of the FrameReader's buffer,
                        # so returning here would drop it silently. Hand the
                        # rest to _pump instead.
                        self._early = batch[i + 1:]
                        return f
        except TimeoutError:
            await self._try_send_raw(writer, error("hello_timeout",
                                                   "no HELLO within 10s"))
            return None

    async def _attach(
        self, hello: Frame, writer: asyncio.StreamWriter
    ) -> GuestSession | None:
        h = hello.headers

        # Version and frame cap are checked together: inheriting a peer's idea
        # of the cap is how you end up allocating 32MB on a 32MB machine.
        if h.get("ver") != str(PROTO_VERSION):
            await self._try_send_raw(writer, error(
                "bad_version",
                f"bridge speaks {PROTO_VERSION}, guest speaks {h.get('ver')}"))
            return None

        peer_cap = int(h.get("max_frame", "0") or 0)
        if peer_cap != MAX_FRAME:
            await self._try_send_raw(writer, error(
                "bad_frame_cap",
                f"bridge caps at {MAX_FRAME}, guest says {peer_cap}"))
            return None

        if not secrets.compare_digest(h.get("secret", ""), self.secret):
            log.warning("bad secret from %s", writer.get_extra_info("peername"))
            await self._try_send_raw(writer, error("bad_secret", "rejected"))
            return None

        # A second client is rejected, not evicted: whoever is already holding
        # the session may be mid-turn, and silently stealing it is worse than
        # refusing.
        if self._guest and self._guest.connected:
            await self._try_send_raw(writer, error(
                "busy", "another client holds this session"))
            return None

        # Page size is scaled to the guest's RAM. A 128MB machine can hold a
        # far bigger page than the 32MB floor the design is built around, and
        # the handshake is the only place the bridge learns which it has.
        mem_kb = int(h.get("mem_kb", "0") or 0)
        page_bytes, page_blocks = limits_for(mem_kb)
        log.info("guest has %d MB; pages capped at %d bytes / %d blocks",
                 mem_kb // 1024, page_bytes, page_blocks)

        # A fresh connection means a fresh inflater on the guest, so the
        # compressor must restart in lockstep or the first frame is garbage.
        if self._guest is not None:
            self._guest.zip_out.reset()

        resumed = False
        if self._guest is None:
            self._guest = GuestSession(
                sess_id=str(uuid.uuid4()),
                pages=Paginator(page_bytes, page_blocks))
        guest = self._guest
        guest.writer = writer
        guest.page_bytes = page_bytes
        guest.page_blocks = page_blocks
        guest.hello = dict(h)

        guest.link_bps = self.link_bps
        for c in guest.coalescers.values():
            c.set_link_speed(guest.link_bps)

        if guest.claude is None:
            guest.claude = await self._make_session(guest)

        await self._send(guest, Frame("READY", {
            "sess": guest.sess_id,
            "ver": str(PROTO_VERSION),
            "model": guest.claude.model or "(default)",
            "next_seq": str(guest.replay.next_seq),
            "page": str(guest.pages.live.number),
            "total": str(guest.pages.count),
        }), stamp=False)

        # Replay anything missed while disconnected.
        want = h.get("from")
        if want is not None:
            try:
                missed = guest.replay.since(int(want))
            except LookupError as exc:
                await self._send(guest, Frame("RESYNC", {"reason": str(exc)}),
                                 stamp=False)
            else:
                resumed = True
                log.info("replaying %d frames from seq %s", len(missed), want)
                for f in missed:
                    await self._send(guest, f, stamp=False)

        # The stylesheet and the document skeleton live here rather than in
        # the guest, so the look can be changed by restarting the bridge
        # instead of rebuilding and re-deploying a binary.
        await self._send(guest, Frame("DOC", {}, fold.encode(H.document())))
        await self._send_options(guest)
        # P1: the guest never sends `from=`, so the replay path above is dead
        # code in production and a reconnect would otherwise arrive with the
        # bar gone and the future still pending bridge-side. Re-issue the
        # prompt explicitly instead of relying on replay.
        guest.lost_at = None
        await self._reissue_prompt(guest)

        log.info("attached sess=%s (resumed=%s)", guest.sess_id, resumed)
        return guest

    # --- Claude session ----------------------------------------------------

    async def _make_session(self, guest: GuestSession,
                            resume: str | None = None,
                            fork: bool = False) -> Session:
        # A prompt belongs to the session that raised it. Rebuilding the
        # session -- switch, new, or the effort fork -- orphans its future, so
        # deny first rather than leaving one pending against a client that no
        # longer exists.
        self._resolve_all(guest, "rebuild",
                          "The conversation changed before this was answered.")
        async def on_text(t: str) -> None:
            await self._emit_text(guest, t, style="plain")

        async def on_thinking(t: str) -> None:
            await self._emit_text(guest, t, style="thinking")

        async def on_tool(name: str, args: dict) -> None:
            # Flush buffered text first: a tool line that jumps ahead of the
            # prose that introduced it reads as a non sequitur.
            await self._flush_text(guest)
            await self._write_block(guest, H.tool_call(name, _summarise(args)))

        async def on_tool_result(tool_id: str, body: str, is_err: bool) -> None:
            await self._send(guest, Frame(
                "TOOLRESULT",
                {"id": tool_id, "error": "1" if is_err else "0"},
                fold.encode(body[:2000])))

        async def on_system(subtype: str, data: dict) -> None:
            if subtype == "compact_boundary":
                await self._write_block(guest, H.notice("History condensed."))

        s = Session(
            cwd=self.cwd,
            # APPENDED to Claude Code's own prompt, never substituted. Without
            # it the model answers as though it were on this machine, and the
            # machine that matters is the 200MHz one on the other end.
            system_prompt={
                "type": "preset",
                "preset": "claude_code",
                "append": machine.system_prompt(guest.hello,
                                                self.cwd or os.getcwd()),
            },
            # The per-guest values, not the process defaults: the menu changes
            # these and a later reconnect or fork has to keep the choice.
            model=guest.model or self.model,
            effort=guest.effort or self.effort,
            resume=resume,
            fork=fork,
            # Every tool call the CLI does not resolve locally comes back
            # through here as a prompt on the guest's screen.
            can_use_tool=self._can_use_tool(guest),
            on_text=on_text,
            on_thinking=on_thinking,
            on_tool=on_tool,
            on_tool_result=on_tool_result,
            on_system=on_system,
        )
        await s.start()
        log.info("session for guest machine:\n%s",
                 machine.describe(guest.hello))
        # The id is usually not known yet -- the SDK assigns it when the first
        # query goes out -- so this is a best effort and _run_turn registers it
        # again once it exists.
        self.registry.add(s.session_id)
        return s

    # --- permissions -------------------------------------------------------

    def _can_use_tool(self, guest: GuestSession):
        """Build the can_use_tool callback for one guest.

        Everything that reaches here gets a bar. There is deliberately no
        auto-allow branch: measured against the real CLI, a Read or Grep inside
        cwd never reaches this callback at all, and a Bash command its sandbox
        can contain is approved without asking us either. The policy the plan
        carried would have been dead code.
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
                                      self.cwd or os.getcwd())
            if refuse is not None:
                log.info("auto-refused %s outside the working copy: %s",
                         tool_name, str(args)[:120])
                return PermissionResultDeny(message=refuse, interrupt=False)

            loop = asyncio.get_running_loop()
            prompt = PendingPrompt(
                tool_use_id=tool_use_id,
                tool_name=tool_name,
                future=loop.create_future(),
                suggestions=list(getattr(ctx, "suggestions", None) or []),
                body=render_request(tool_name, args, ctx),
                allows=perms.allows_always(
                    tool_name, getattr(ctx, "suggestions", None) or []),
            )
            prompt.summary = perms.summary(tool_name, ctx)

            try:
                await self._enqueue_prompt(guest, prompt)
                return await asyncio.wait_for(prompt.future,
                                              timeout=perms.DEADLINE_S)
            except asyncio.TimeoutError:
                self._finish_prompt(guest, prompt, "timeout")
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
                self._finish_prompt(guest, prompt, "cancelled")
                raise

        return can_use_tool

    async def _enqueue_prompt(self, guest: GuestSession,
                              prompt: PendingPrompt) -> None:
        """Show a prompt, or queue it behind the one already on screen."""
        if guest.perm_pending is not None:
            guest.perm_queue.append(prompt)
            await self._send_waiting(guest)
            return
        guest.perm_pending = prompt
        await self._show_prompt(guest, prompt)

    async def _show_prompt(self, guest: GuestSession,
                           prompt: PendingPrompt) -> None:
        # The description goes into the transcript as a normal block so it
        # paginates and survives paging away and back. The bar itself carries
        # only the summary, because a guest in HISTORY mode never sees this.
        await self._write_block(guest, prompt.body)
        await self._send(guest, Frame("PERMREQ", {
            "id": prompt.tool_use_id,
            "tool": prompt.tool_name,
            "allows": "1" if prompt.allows else "0",
            "waiting": str(len(guest.perm_queue)),
        }, fold.encode(getattr(prompt, "summary", prompt.tool_name))))

    async def _reissue_prompt(self, guest: GuestSession) -> None:
        """P1: after a reconnect, put the bar back."""
        prompt = guest.perm_pending
        if prompt is None:
            return
        log.info("re-issuing pending prompt %s after reconnect",
                 prompt.tool_use_id)
        await self._send(guest, Frame("PERMSTATE", {
            "id": prompt.tool_use_id,
            "tool": prompt.tool_name,
            "allows": "1" if prompt.allows else "0",
            "waiting": str(len(guest.perm_queue)),
        }, fold.encode(getattr(prompt, "summary", prompt.tool_name))))

    async def _send_waiting(self, guest: GuestSession) -> None:
        """Refresh the queue depth shown on the bar."""
        prompt = guest.perm_pending
        if prompt is None:
            return
        await self._send(guest, Frame("PERMWAIT", {
            "id": prompt.tool_use_id,
            "waiting": str(len(guest.perm_queue)),
        }))

    def _finish_prompt(self, guest: GuestSession, prompt: PendingPrompt,
                       reason: str) -> None:
        """Synchronous bookkeeping plus a scheduled PERMEND.

        Deliberately not a coroutine. It is called from a cancelled context,
        where any await would be aborted before it ran.
        """
        if guest.perm_pending is not prompt:
            # Already resolved by another path; nothing owed.
            return
        guest.perm_pending = None
        self._spawn(self._after_prompt(guest, prompt.tool_use_id, reason))

    async def _after_prompt(self, guest: GuestSession, tool_use_id: str,
                            reason: str) -> None:
        await self._send(guest, Frame("PERMEND",
                                      {"id": tool_use_id, "reason": reason}))
        await self._pump_queue(guest)

    async def _pump_queue(self, guest: GuestSession) -> None:
        """Show the next queued prompt, discarding any that died waiting."""
        while guest.perm_pending is None and guest.perm_queue:
            nxt = guest.perm_queue.popleft()
            if nxt.future.done():
                continue
            guest.perm_pending = nxt
            await self._show_prompt(guest, nxt)

    def _resolve_all(self, guest: GuestSession | None, reason: str,
                     message: str) -> None:
        """Deny everything outstanding, synchronously.

        Called from _on_client's finally before its first await, and from the
        session-rebuild paths. Denying rather than leaving them pending is the
        point: a future nobody will ever complete hangs the turn until the
        deadline, on a machine that has already gone away.
        """
        if guest is None:
            return
        pending = []
        if guest.perm_pending is not None:
            pending.append(guest.perm_pending)
            guest.perm_pending = None
        while guest.perm_queue:
            pending.append(guest.perm_queue.popleft())
        for prompt in pending:
            if not prompt.future.done():
                prompt.future.set_result(
                    PermissionResultDeny(message=message, interrupt=False))
        if pending:
            log.info("denied %d pending prompt(s): %s", len(pending), reason)

    def _spawn(self, coro) -> None:
        """Fire-and-forget, with a reference kept.

        asyncio only holds a weak reference to a running task, so a bare
        create_task can be garbage-collected mid-flight.
        """
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _coalescer(self, guest: GuestSession, style: str) -> Coalescer:
        c = guest.coalescers.get(style)
        if c is None:
            if style == "plain":
                # Assistant prose is markdown; the renderer turns it into
                # finished HTML blocks plus the block still being written.
                async def sink(text: str) -> None:
                    await self._write_updates(guest, guest.render.feed(text))
            else:
                # Thinking and anything else is not markdown; wrap it rather
                # than parsing it.
                async def sink(text: str, _s=style) -> None:
                    await self._write_block(
                        guest, f'<div class="{_s}">' + H.esc(text) + "</div>")
            c = Coalescer(sink)
            c.set_link_speed(guest.link_bps)
            c.start()
            guest.coalescers[style] = c
        return c

    async def _emit_text(self, guest: GuestSession, text: str,
                         *, style: str) -> None:
        """Buffer a delta. The coalescer decides when it goes out."""
        if not text:
            return
        await self._coalescer(guest, style).add(text)

    async def _flush_text(self, guest: GuestSession) -> None:
        for c in list(guest.coalescers.values()):
            await c.flush()
        # A model can stop mid-sentence; whatever the parser is holding for
        # lookahead has to come out or the last line is silently lost.
        await self._write_updates(guest, guest.render.flush())

    async def _write_updates(self, guest: GuestSession,
                             updates: list) -> None:
        """Send a batch of renderer updates.

        Only APPEND blocks are recorded: a LIVE block is the paragraph still
        being written, and it is replaced on every update, so storing it would
        put a dozen half-finished copies of the same sentence into the page.
        The final state of that block always arrives as an APPEND.
        """
        for u in updates:
            if u.mode == APPEND:
                await self._write_block(guest, u.html)
            else:
                await self._send_html(guest, u.html, LIVE,
                                      guest.pages.live.number)

    async def _write_block(self, guest: GuestSession, html: str,
                           *, stream: bool = True) -> None:
        """Record a finished block into the paginator, then send it.

        `stream=False` records without sending, for a block the guest has
        already put on screen itself -- the echo of the user's own line. It
        still has to reach the paginator, because a sealed page is re-sent in
        full when the reader pages back, and a transcript of answers with no
        questions in it is close to useless.
        """
        if not html:
            return

        page_no = guest.pages.live.number
        sealed = guest.pages.add("html", html)

        # Only stream when the guest is watching the live page. In HISTORY mode
        # the bridge keeps buffering silently and the page counter ticking up
        # is the notification.
        if stream and (guest.view_page is None or guest.view_page == page_no):
            await self._send_html(guest, html, APPEND, page_no)

        for pg in sealed:
            await self._send(guest, Frame("PAGESEALED", {
                "page": str(pg.number), "total": str(guest.pages.count)}))

    async def _send_html(self, guest: GuestSession, html: str, mode: str,
                         page_no: int) -> None:
        """Fold to CP1252, compress if worthwhile, chunk under the frame cap.

        Chunking keeps the send lock free between chunks, so a long block
        cannot delay a keystroke or a Stop. A LIVE update is never chunked --
        it replaces an element wholesale, so half of one is meaningless; an
        oversized one is simply dropped and the APPEND that follows carries the
        whole thing.
        """
        data = fold.encode(html)
        if not data:
            if mode == LIVE:
                await self._send(guest, Frame(
                    "TEXT", {"mode": LIVE, "enc": ENC_RAW,
                             "page": str(page_no)}, b""))
            return

        budget = MAX_FRAME - 1024   # headroom for headers and deflate overhead
        if mode == LIVE and len(data) > budget:
            return

        total = len(data)
        for i in range(0, total, budget):
            chunk = data[i:i + budget]
            enc = ENC_RAW
            if should_compress(chunk, guest.link_bps):
                chunk = guest.zip_out.compress(chunk)
                enc = ENC_DEFLATE
            # total/off are in DECOMPRESSED bytes and are on every chunk, not
            # just the first: the guest shows a progress figure from them, and
            # a reconnect mid-transfer would otherwise leave it with a
            # denominator it never received.
            await self._send(guest, Frame(
                "TEXT", {"mode": mode, "enc": enc, "page": str(page_no),
                         "total": str(total), "off": str(i)},
                chunk))

    # --- frame pump --------------------------------------------------------

    async def _pump(
        self,
        guest: GuestSession,
        reader: asyncio.StreamReader,
        frames: FrameReader,
    ) -> None:
        # Frames that arrived in the same read as HELLO. See _read_hello.
        early, self._early = self._early, []
        for f in early:
            await self._handle(guest, f)
        while True:
            data = await reader.read(8192)
            if not data:
                return
            for f in frames.feed(data):
                await self._handle(guest, f)

    async def _handle(self, guest: GuestSession, f: Frame) -> None:
        guest.last_seen = time.monotonic()
        if f.type == "INPUT":
            text = fold.decode(f.body)
            log.info("input: %s", text[:120])
            asyncio.create_task(self._run_turn(guest, text))

        elif f.type == "PERMREPLY":
            await self._perm_reply(guest, f)

        elif f.type == "INTERRUPT":
            if guest.claude:
                await guest.claude.interrupt()

        elif f.type == "GETSESSIONS":
            # Only conversations this bridge started: the rest belong to a
            # machine with a different way of working.
            rows = self.registry.filter(sesslist.listing(cwd=self.cwd))
            await self._send(guest, Frame("SESSIONS",
                                          {"count": str(len(rows))}))
            for i, r in enumerate(rows):
                await self._send(guest, Frame("SESSION", {
                    "i": str(i),
                    "id": r["id"],
                    "title": r["title"][:120],
                    "cwd": r["cwd"][:120],
                    "group": r.get("group", ""),
                    "current": "1" if (guest.claude and
                                       guest.claude.session_id == r["id"])
                               else "0",
                }))
            await self._send(guest, Frame("SESSIONSEND", {}))

        elif f.type == "SWITCH":
            await self._switch(guest, f.headers.get("id", ""))

        elif f.type == "GETPAGE":
            await self._send_page(guest, f.headers.get("page", ""))

        elif f.type == "STAT":
            # The guest measures its own throughput for the progress UI; the
            # sender uses it to pick a codec. This is what makes one build work
            # on both a 10Mbit LAN and a modem.
            try:
                bps = float(f.headers.get("bps", "0"))
            except ValueError:
                bps = 0.0
            if bps > 0:
                guest.link_bps = bps
                for c in guest.coalescers.values():
                    c.set_link_speed(bps)
                log.info("guest reports %.0f B/s", bps)

        elif f.type == "PING":
            await self._send(guest, Frame("PONG", {}), stamp=False)

        elif f.type == "NEWSESSION":
            await self._new_session(guest)

        elif f.type == "GETOPTIONS":
            await self._send_options(guest)

        elif f.type == "SETMODEL":
            model = f.headers.get("model", "")
            if guest.claude:
                await guest.claude.set_model(model)
                guest.model = model
                log.info("model -> %s", model or "(default)")
                await self._write_block(guest, H.notice("Model set to " + model))
            await self._send_options(guest)

        elif f.type == "SETEFFORT":
            await self._set_effort(guest, f.headers.get("effort", ""))

        else:
            log.warning("unknown frame type from guest: %s", f.type)
            await self._send(guest, error("unknown_type", f.type), stamp=False)

    async def _run_turn(self, guest: GuestSession, text: str) -> None:
        if not guest.claude:
            return
        # User turns are the landmarks a reader navigates by.
        anchor = guest.pages.mark_user_turn(text)
        # The guest echoed this line itself the moment Send was pressed, so it
        # is recorded but not streamed. USER_BLOCK is shared with the guest so
        # the two renderings are the same string, not merely similar.
        await self._write_block(guest, H.user_turn(text), stream=False)
        await self._send(guest, Frame("ANCHOR", {
            "page": str(anchor.page), "off": str(anchor.offset),
            "preview": anchor.preview}))
        await self._send(guest, Frame("TURN", {"state": "start"}))
        try:
            turn = await guest.claude.send(text)
        except Exception as exc:
            log.exception("turn failed")
            await self._flush_text(guest)
            await self._send(guest, Frame("TURN", {
                "state": "error", "msg": str(exc)[:200]}))
            return
        await self._flush_text(guest)
        # Register here, not at start(): the SDK assigns the session id when
        # the first query goes out, so at start() there is nothing to record
        # and the conversation would never appear in the chat list.
        self.registry.add(guest.claude.session_id)
        # Seal only at a message boundary, so a turn is never split
        # mid-sentence while the reader is watching it stream.
        sealed = guest.pages.message_boundary()
        if sealed:
            await self._send(guest, Frame("PAGESEALED", {
                "page": str(sealed.number), "total": str(guest.pages.count)}))
        raw, comp = guest.zip_out.stats
        if comp:
            log.info("deflate so far: %d -> %d bytes (%.2f:1)",
                     raw, comp, guest.zip_out.ratio)
        await self._send(guest, Frame("TURN", {
            "state": "end",
            "ok": "1" if turn.ok else "0",
            "subtype": turn.subtype,
            "cost_usd": f"{turn.cost_usd:.4f}" if turn.cost_usd else "",
            "ms": str(turn.duration_ms),
        }))

    async def _perm_reply(self, guest: GuestSession, f: Frame) -> None:
        """Answer from the guest's prompt bar."""
        want = f.headers.get("id", "")
        action = f.headers.get("action", "deny")
        prompt = guest.perm_pending

        if prompt is None or prompt.tool_use_id != want:
            # Not a bug on its own: the prompt may have been resolved by the
            # deadline, by a disconnect, or by a session rebuild while the
            # reply was in flight. Ignoring is right; crashing is not.
            log.info("PERMREPLY for unknown id %s (action=%s)", want, action)
            return

        if prompt.future.done():
            guest.perm_pending = None
            await self._pump_queue(guest)
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
        guest.perm_pending = None
        log.info("permission %s for %s (%s)", action, prompt.tool_name, want)
        await self._send(guest, Frame("PERMEND",
                                      {"id": want, "reason": "answered"}))
        await self._pump_queue(guest)

    async def _reap_idle(self) -> None:
        """P2: a guest that stops talking must eventually be let go.

        _pump blocks in read() with no timeout, so a guest that GPFs or is
        reset leaves no FIN behind and _on_client parks forever -- with
        `connected` still True, which rejects every reconnect as "busy". The
        machine crashing is the expected case here, not the exotic one.
        """
        while True:
            await asyncio.sleep(self.idle_check_s)
            guest = self._guest
            if guest is None:
                continue
            if not guest.connected:
                # Disconnected: expire any prompt whose grace has run out.
                if (guest.perm_pending is not None and guest.lost_at is not None
                        and time.monotonic() - guest.lost_at
                        > self.perm_grace_s):
                    self._resolve_all(
                        guest, "gone",
                        "The Windows machine disconnected before anyone could "
                        "answer, so this was not approved. It was not refused "
                        "on purpose -- ask again if you still need it.")
                continue
            idle = time.monotonic() - guest.last_seen
            if idle < self.idle_timeout_s:
                continue
            log.warning("guest silent for %.0fs; closing so it can reconnect",
                        idle)
            self._resolve_all(guest, "idle",
                              "The Windows machine stopped responding.")
            w, guest.writer = guest.writer, None
            try:
                if w is not None:
                    w.close()
            except Exception:
                pass

    async def _send_options(self, guest: GuestSession) -> None:
        """The model and effort menus, with the one in use marked.

        The list comes from the CLI rather than from a table here, so the names
        stay correct without a release. There is deliberately no "Default"
        entry: it is not a model, it is an alias, and a menu that says
        "Default" does not tell you what you are talking to. The alias is
        resolved and the real model it points at is the one ticked.
        """
        if not guest.claude:
            return
        models = await guest.claude.server_models()

        # What is actually in effect, as a concrete model name.
        def resolved_of(value: str) -> str:
            for m in models:
                if m.get("value") == value:
                    return m.get("resolvedModel") or value
            return value

        in_use = resolved_of(guest.model or "default")

        chosen = None
        for m in models:
            value = m.get("value") or ""
            if value == "default":
                continue        # an alias, not a model
            label = m.get("displayName") or value
            current = (m.get("resolvedModel") or value) == in_use
            if current and chosen is None:
                chosen = m
            await self._send(guest, Frame("OPTION", {
                "kind": "model", "id": value, "label": label,
                "current": "1" if current else "0",
            }))

        # Effort is a property of the model: Haiku has none at all, so an
        # Effort menu offering five levels would be lying.
        levels = (chosen or {}).get("supportedEffortLevels") or []
        if not levels:
            await self._send(guest, Frame("OPTION", {
                "kind": "effort", "id": "", "label": "(not available for this model)",
                "current": "0", "disabled": "1",
            }))
        else:
            for lvl in EFFORT_ORDER:
                if lvl not in levels:
                    continue
                await self._send(guest, Frame("OPTION", {
                    "kind": "effort", "id": lvl,
                    "label": EFFORT_LABEL.get(lvl, lvl),
                    "current": "1" if lvl == (guest.effort or "") else "0",
                }))
        await self._send(guest, Frame("OPTIONSEND", {}))

    async def _set_effort(self, guest: GuestSession, effort: str) -> None:
        """Change effort, which means reopening the session.

        There is no set_effort() on the client -- effort is fixed when the
        session starts -- so this closes the current client and resumes the
        same conversation with fork_session=True. The fork is why the history
        survives and why the session id changes.
        """
        if not guest.claude:
            return
        old = guest.claude.session_id
        guest.effort = effort
        label = EFFORT_LABEL.get(effort, effort or "default")
        log.info("effort -> %s (forking %s)", effort or "(default)", old)

        await self._write_block(guest, H.notice(
            "Effort set to " + label
            + ". Effort cannot be changed on a running session, so this "
              "conversation has been forked -- the history is intact and "
              "replies from here use the new setting."))

        await guest.claude.close()
        try:
            guest.claude = await self._make_session(guest, resume=old,
                                                    fork=True)
        except Exception as exc:
            log.exception("effort change failed")
            await self._write_block(guest, H.error(
                "Could not reopen the session: " + str(exc)[:160]))
            await self._send_options(guest)
            return
        guest.sess_id = guest.claude.session_id or guest.sess_id
        await self._send_options(guest)

    async def _new_session(self, guest: GuestSession) -> None:
        """Start a fresh conversation. There was no way to do this at all."""
        log.info("new session requested")
        if guest.claude:
            await guest.claude.close()

        guest.pages = Paginator(guest.page_bytes or 16384,
                                guest.page_blocks or 90)
        guest.render = HtmlStream()
        for c in guest.coalescers.values():
            await c.stop()
        guest.coalescers.clear()
        guest.view_page = None

        try:
            guest.claude = await self._make_session(guest)
        except Exception as exc:
            log.exception("new session failed")
            await self._send(guest, error("new_failed", str(exc)[:200]))
            return

        guest.sess_id = guest.claude.session_id or guest.sess_id
        await self._send(guest, Frame("SWITCHED", {
            "id": guest.claude.session_id or "",
            "total": str(guest.pages.count),
        }))
        await self._send_page(guest, "live")
        await self._send_options(guest)

    async def _switch(self, guest: GuestSession, session_id: str) -> None:
        """Close the live client and reopen on a stored session.

        One live ClaudeSDKClient at a time, so this is a close + resume rather
        than a pool lookup. The history is replayed into a fresh paginator so
        the guest can page back through it exactly as if it had streamed.
        """
        if not session_id:
            await self._send(guest, error("bad_session", "no id"))
            return

        log.info("switching to session %s", session_id)
        await self._send(guest, Frame("NOTICE", {"kind": "switching"},
                                      fold.encode("Loading conversation...")))

        if guest.claude:
            await guest.claude.close()

        # Fresh transcript state. Anything the old session had is on disk.
        guest.pages = Paginator(guest.page_bytes or None or 16384,
                                guest.page_blocks or 90)
        guest.render = HtmlStream()
        for c in guest.coalescers.values():
            await c.stop()
        guest.coalescers.clear()

        try:
            guest.claude = await self._make_session(guest, resume=session_id)
        except Exception as exc:
            log.exception("resume failed")
            await self._send(guest, error("resume_failed", str(exc)[:200]))
            return

        for block in sesslist.history_html(session_id):
            guest.pages.add("html", block)
            if guest.pages.live.over_soft_cap():
                guest.pages.message_boundary()

        guest.view_page = None
        await self._send(guest, Frame("SWITCHED", {
            "id": session_id,
            "total": str(guest.pages.count),
        }))
        await self._send_page(guest, "live")

    async def _send_page(self, guest: GuestSession, want: str) -> None:
        """Render one page to the guest. LIVE is requested as page=live."""
        if want == "live":
            number = guest.pages.live.number
            guest.view_page = None
        else:
            try:
                number = int(want)
            except ValueError:
                await self._send(guest, error("bad_page", want))
                return
            guest.view_page = (None if number == guest.pages.live.number
                               else number)

        page = guest.pages.page(number)
        if page is None:
            await self._send(guest, error("no_such_page", want))
            return

        await self._send(guest, Frame("PAGESTART", {
            "page": str(page.number),
            "total": str(guest.pages.count),
            "live": "1" if guest.view_page is None else "0",
            "continued": "1" if page.continued else "0",
        }))
        for run in page.runs:
            await self._send_html(guest, run.text, APPEND, page.number)
        await self._send(guest, Frame("PAGEEND", {"page": str(page.number)}))

    # --- sending -----------------------------------------------------------

    async def _send(self, guest: GuestSession, frame: Frame,
                    *, stamp: bool = True) -> None:
        # P4: check encodability BEFORE stamping. Frame.encode raises above
        # MAX_FRAME and the replay buffer is append-only, so stamping an
        # oversized frame makes every future replay raise -- permanently, and
        # the guest can then never reconnect.
        #
        # The check is on a copy because append() mutates headers["seq"], and
        # encoding the real frame first would produce a payload with no seq in
        # it. The placeholder is wider than any seq we will reach, so a copy
        # that encodes guarantees the stamped original does too.
        probe = Frame(frame.type, dict(frame.headers), frame.body)
        probe.headers["seq"] = "9999999999"
        try:
            probe.encode()
        except ProtocolError:
            log.error("refusing to send oversized %s frame (%d byte body); "
                      "it would poison the replay buffer",
                      frame.type, len(frame.body))
            return
        if stamp:
            guest.replay.append(frame)
        payload = frame.encode()
        async with guest.send_lock:
            w = guest.writer
            if w is None or w.is_closing():
                return  # buffered in replay; the guest will ask for it
            try:
                w.write(payload)
                await w.drain()
            except (ConnectionResetError, BrokenPipeError):
                guest.writer = None

    async def _try_send_raw(self, writer: asyncio.StreamWriter,
                            frame: Frame) -> None:
        try:
            writer.write(frame.encode())
            await writer.drain()
        except Exception:
            pass


# Effort levels, in the order they should appear. Which of these a given model
# actually offers comes from the CLI, not from here.
EFFORT_ORDER = ["low", "medium", "high", "xhigh", "max"]
EFFORT_LABEL = {"low": "Low", "medium": "Medium", "high": "High",
                "xhigh": "Extra high", "max": "Maximum"}


def _summarise(args: dict) -> str:
    """One readable line for the tool-activity pane."""
    for key in ("command", "file_path", "pattern", "path", "url", "prompt"):
        if key in args:
            return f"{key}: {str(args[key])[:160]}"
    return ", ".join(f"{k}={str(v)[:40]}" for k, v in list(args.items())[:3])


def main() -> None:
    import argparse

    ap = argparse.ArgumentParser(description="RetroClaude bridge")
    ap.add_argument("--host", default="127.0.0.1",
                    help="bind address; use the retro-segment interface, not 0.0.0.0")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--cwd", default=None, help="working directory for Claude")
    ap.add_argument("--model", default=None)
    ap.add_argument("--effort", default=None, choices=[None, "low", "medium",
                                                       "high", "xhigh", "max"])
    ap.add_argument("--link-bps", type=float, default=None,
                    help="assume this link speed in bytes/sec (testing: "
                         "5000 simulates dialup and forces the slow-link path)")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    secret = os.environ.get("RETROCLAUDE_SECRET")
    if not secret:
        secret = "dev-secret"
        log.warning("RETROCLAUDE_SECRET unset; using %r (development only)", secret)

    bridge = Bridge(secret, host=args.host, port=args.port,
                    cwd=args.cwd, model=args.model, effort=args.effort,
                    link_bps=args.link_bps)
    try:
        asyncio.run(bridge.serve_forever())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
