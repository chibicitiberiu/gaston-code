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
import contextlib
import logging
import os
import secrets
import time
import uuid
from collections import deque
from dataclasses import dataclass, field

from .files import caps as C
from .files import manifest
from .wire import fold
from .agent import machine
from .agent.registry import Registry
from .wire.coalesce import Coalescer
from .wire.compress import StreamCompressor
from .view import html as H
from .view.html import APPEND, LIVE, HtmlStream
from .view.pages import Paginator, limits_for
from .files import paths as P
from .agent import permissions as perms
from .agent import tools as machine_tools
from .agent.permissions import PendingPrompt, render_request
from .wire.protocol import (
                            MAX_FRAME,
                            MAX_HEADER_VALUE,
                            PROTO_VERSION,
                            Frame,
                            FrameReader,
                            ProtocolError,
                            anchor_frame,
                            doc,
                            error,
                            exec_cmd,
                            exec_kill,
                            halt,
                            haltend,
                            notice,
                            option,
                            optionsend,
                            pageend,
                            pagesealed,
                            pagestart,
                            permend,
                            permreq,
                            permstate,
                            permwait,
                            pong,
                            ready,
                            resync,
                            session_row,
                            sessions_end,
                            sessions_start,
                            switched,
                            turn_end,
                            turn_error,
                            turn_start,
                            )
from .wire.link import GuestLink
from .wire.replay import Replay
from .agent import sessions as sesslist
from .files.sync import Entry as SyncEntry, Type as SyncType
from .files.syncrun import SyncError
from .agent.session import Session
from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny

log = logging.getLogger("bridge.server")

DEFAULT_PORT = 7890


@dataclass
class GuestSession:
    """Bridge-side state for one guest, surviving across reconnects."""

    sess_id: str
    replay: Replay = field(default_factory=Replay)
    claude: Session | None = None
    #: Everything about writing bytes to this guest -- the socket writer, the
    #: send lock, the persistent compressor and the measured link speed. See
    #: bridge/wire/link.py. `writer`/`send_lock`/`zip_out`/`link_bps` below
    #: forward here so the rest of the bridge did not have to change shape.
    link: GuestLink = field(default_factory=GuestLink)
    # Chosen from the Model and Effort menus. Held per guest rather than per
    # process so a reconnect or an effort fork keeps the choice.
    model: str = ""
    effort: str = ""
    #: The halt currently on the user's screen, if any. A halt is the bridge
    #: stopping the agent and handing the decision to the person at the
    #: keyboard; it clears when they press Continue, and never on a timer.
    halt_id: str = ""
    #: The paths the bar on screen was raised FOR. Continue answers those and
    #: nothing else: a conflict found while the bar is already up raises no new
    #: bar (there is only one), so the user is never shown it -- and releasing
    #: everything meant their press lifted the write refusal on a path they had
    #: not seen. The agent then writes to it, having been told nothing, and the
    #: write goes to the user's machine.
    halt_paths: frozenset = frozenset()
    #: What the bar says, kept so it can be put back after a reconnect. The id
    #: alone is not enough to redisplay it.
    halt_text: str = ""
    # Page caps for THIS guest, scaled to the RAM it reported in HELLO.
    page_bytes: int = 0
    page_blocks: int = 0
    # The machine facts from HELLO, kept so every session this guest opens
    # gets the same description of the computer it is acting for.
    hello: dict = field(default_factory=dict)
    # C.from_hello(hello), parsed once in _attach() rather than re-parsed at
    # every site that needs a path rule -- a re-parse cannot raise (already
    # validated at handshake) but silently re-derives what is already known,
    # and one prior re-parse site used to fall back to {} when this guest was
    # merely unset, guessing Win9x defaults instead of reading what the
    # client actually declared. See docs/audit/02-bridge-python.md #02.22.
    caps: "C.Caps | None" = None

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
    # In-flight EXEC calls, keyed by our own id. Separate namespace from
    # tool_use_id so a stray frame cannot cross-resolve against TOOLRESULT.
    exec_pending: dict = field(default_factory=dict)
    exec_seq: int = 0
    # One worker thread on the guest means one command at a time. The model
    # can request tools in parallel, and an "Always" rule would bypass the
    # prompt queue that was implicitly serialising them.
    exec_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    # Sync transfers in flight, keyed by our own s<n> id -- a namespace the
    # guest enforces, so an exec frame can never resolve a sync future.
    sync_pending: dict = field(default_factory=dict)
    sync_seq: int = 0
    # One pass at a time. Three triggers (timer, watcher, sync_status) and no
    # lock would let two passes both read the baseline and both advance it.
    sync_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    #: One turn at a time, per guest.
    #:
    #: `_handle` spawned a task per INPUT with no guard, and the RESUME handler
    #: spawned a second -- reachable through the DESIGNED conflict flow, not a
    #: race: the Continue button is not gated on g_turn_active, a halt is
    #: raised by a sync pass while the agent works, and the agent writing files
    #: is what moves the tree in the first place.
    #:
    #: Two turns on one session is not "two prompt bars". The SDK stream has a
    #: single consumer, so two iterators get a DISJOINT split of messages: one
    #: turn can consume the other's ResultMessage and return with its cost
    #: while the other's text lands in the wrong callbacks. The transcript
    #: itself is corrupted, and Session._busy is a flag rather than a depth, so
    #: the first `finally` clears it and Stop stops nothing.
    turn_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    sync_active: bool = False

    # --- forwarded to self.link, so the rest of the bridge can keep saying
    # guest.writer / guest.send_lock / guest.zip_out / guest.link_bps rather
    # than reaching through guest.link for every one of them. ---

    @property
    def writer(self) -> asyncio.StreamWriter | None:
        return self.link.writer

    @writer.setter
    def writer(self, w: asyncio.StreamWriter | None) -> None:
        self.link.writer = w

    @property
    def send_lock(self) -> asyncio.Lock:
        return self.link.send_lock

    @property
    def zip_out(self) -> StreamCompressor:
        return self.link.zip_out

    @property
    def link_bps(self) -> float | None:
        return self.link.link_bps

    @link_bps.setter
    def link_bps(self, bps: float | None) -> None:
        self.link.link_bps = bps

    @property
    def connected(self) -> bool:
        return self.link.connected


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
        self._reaper: asyncio.Task | None = None
        self._sweeper: asyncio.Task | None = None
        #: How often the bridge checks the tree itself. Longer than the
        #: guest's own 30s sweep because it is a backstop, not the mechanism:
        #: a pass it starts is one the guest failed to ask for.
        self.sync_sweep_s = 120.0
        # The guest pings every few seconds, so silence much longer than that
        # means it is gone rather than quiet. Generous, because a 200MHz box
        # under load genuinely does pause.
        self.idle_timeout_s = 90.0
        self.idle_check_s = 15.0
        #: Sync is OFF unless a root is named. Defaulting to the bridge's own
        #: working directory would point it at .venv and vm/*.qcow2.
        self.sync_dir: str | None = None
        # None, matching sync_dir's sentinel (02.36). Every caller of
        # _load_baseline is guarded by `if not self.sync_dir`, and this used
        # to default to "" instead -- so if that invariant ever broke,
        # os.path.join("", "base.json") would silently read and WRITE
        # ./base.json relative to the bridge PROCESS's cwd rather than
        # failing loudly.
        self.sync_state: str | None = None
        #: The guest's own path for the mirror, learned from SYNCEND. Held here
        #: so the system prompt can NAME the folder rather than say "the sync
        #: folder" -- an agent told only that its edits reach the user has no
        #: way to know which of its edits do.
        self.guest_sync_root: str | None = None
        self._last_sync_reports: list[str] = []
        #: Notices waiting to ride in on the agent's next turn. Separate from
        #: the baseline's `pending_notice`, which is the USER's copy and is
        #: cleared when it reaches their screen: the two audiences need the
        #: same news and neither delivery implies the other.
        self._agent_notices: list[str] = []
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
        if self._sweeper is None and self.sync_dir:
            self._sweeper = asyncio.create_task(self._sweep_sync())

        try:
            # The frames pipelined behind HELLO travel WITH it rather than
            # through an attribute on the Bridge. They used to live in
            # self._early, which is per-process: a second client's HELLO
            # overwrote the first client's pipelined frames before _pump had
            # consumed them -- and it did so BEFORE being rejected as a
            # duplicate, so the rejected connection corrupted the live one.
            hello, early = await self._read_hello(reader, frames, writer)
            if hello is None:
                return

            guest = await self._attach(hello, writer)
            if guest is None:
                return

            await self._pump(guest, reader, frames, early)

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
                # An in-flight command is a different matter from a pending
                # prompt: the prompt can wait out a blip, but the command has
                # already started over there and its output is gone with the
                # link. Fail it now rather than holding the model for the
                # whole MCP budget.
                self._fail_syncs(self._guest,
                                 "The connected machine disconnected mid-sync.")
                self._fail_execs(self._guest,
                                 "The connected machine disconnected while "
                                 "this command was running. It may or may not "
                                 "have completed.")
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
                        return None, [], []
                    batch = frames.feed(data)
                    for i, f in enumerate(batch):
                        if f.type != "HELLO":
                            await self._try_send_raw(
                                writer, error("expected_hello",
                                              f"first frame was {f.type}")
                            )
                            return None, [], []
                        # Anything pipelined behind HELLO in the same read has
                        # already been parsed out of the FrameReader's buffer,
                        # so returning here would drop it silently. Hand the
                        # rest to _pump instead.
                        return f, batch[i + 1:]
        except TimeoutError:
            await self._try_send_raw(writer, error("hello_timeout",
                                                   "no HELLO within 10s"))
            return None, [], []

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

        # What the bridge now believes the machine IS. Logged at connect
        # because "why did that file not sync" starts here, and because a
        # client that declares nothing is indistinguishable from one that
        # declares the defaults until someone can see which happened.
        try:
            caps = C.from_hello(h)
        except C.CapsError as exc:
            log.warning("client declared rules this bridge cannot honour: %s",
                        exc)
            await self._try_send_raw(writer, error("bad_caps", str(exc)[:200]))
            return None
        log.info("client rules: %s", caps.summary())

        # A fresh connection means a fresh inflater on the guest, so the
        # compressor must restart in lockstep or the first frame is garbage.
        #
        # And the replay buffer dies with it. _send stamps the frame it is
        # about to write, by which point _write_block has already run the body
        # through the PERSISTENT compressor -- so every deflated frame in that
        # buffer is output from a stream that no longer exists. Replaying one
        # into the guest's fresh inflater produces garbage, and net.c drops
        # the link on a malformed frame. Latent only because the guest has
        # never sent `from=`; BACKLOG records that as dead code and did not
        # record this consequence of bringing it back.
        if self._guest is not None:
            self._guest.zip_out.reset()
            lost = self._guest.replay.invalidate()
            if lost:
                log.debug("replay: dropped %d frame(s) the new inflater "
                          "could not decode", lost)

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
        guest.caps = caps

        guest.link_bps = self.link_bps
        for c in guest.coalescers.values():
            c.set_link_speed(guest.link_bps)

        if guest.claude is None:
            guest.claude = await self._make_session(guest)

        await self._send(guest, ready(
            guest.sess_id, guest.claude.model or "(default)",
            guest.replay.next_seq, guest.pages.live.number,
            guest.pages.count), stamp=False)

        # Replay anything missed while disconnected.
        want = h.get("from")
        if want is not None:
            try:
                missed = guest.replay.since(int(want))
            except LookupError as exc:
                await self._send(guest, resync(str(exc)), stamp=False)
            else:
                resumed = True
                log.info("replaying %d frames from seq %s", len(missed), want)
                for f in missed:
                    await self._send(guest, f, stamp=False)

        # The stylesheet and the document skeleton live here rather than in
        # the guest, so the look can be changed by restarting the bridge
        # instead of rebuilding and re-deploying a binary.
        await self._send(guest, doc(H.document()))
        await self._send_options(guest)
        # P1: the guest never sends `from=`, so the replay path above is dead
        # code in production and a reconnect would otherwise arrive with the
        # bar gone and the future still pending bridge-side. Re-issue the
        # prompt explicitly instead of relying on replay.
        guest.lost_at = None
        # A fresh connection shows the fresh DOC skeleton just sent above --
        # the guest has no memory of which page it was reading before it
        # dropped (BACKLOG: it never re-requests page content on reconnect
        # either), so it is looking at LIVE now regardless of what it was
        # doing before. Leaving view_page on its old HISTORY value here made
        # _write_block (02.25) keep silently buffering into the paginator
        # forever -- the guest sees nothing new and the bridge believes that
        # is correct, because nothing ever reset the flag that says so.
        guest.view_page = None
        await self._reissue_prompt(guest)
        await self._reissue_halt(guest)
        await self._reissue_turn(guest)
        # A guest that was away may have been edited while it was away, and
        # nothing reconciled that until its watcher next happened to fire.
        if self.sync_dir:
            self._spawn(self.run_sync("the machine reconnected"))

        log.info("attached sess=%s (resumed=%s)", guest.sess_id, resumed)
        return guest

    # --- Claude session ----------------------------------------------------

    def _effective_model(self, guest: GuestSession) -> str:
        """What model this guest's session actually runs on.

        `guest.model` is empty until the Model menu is used, so the CLI's
        `--model` (`self.model`) is the fallback -- the same rule
        `_make_session` applies. `_send_options` reads this too (02.23): it
        used to read `guest.model` alone, so a bridge started with a CLI
        default ticked "default" in the menu while the session actually ran
        on the CLI's choice.
        """
        return guest.model or self.model or ""

    def _effective_effort(self, guest: GuestSession) -> str:
        """Effort's counterpart to `_effective_model` -- see there."""
        return guest.effort or self.effort or ""

    async def _make_session(self, guest: GuestSession,
                            resume: str | None = None,
                            fork: bool = False) -> Session:
        # A prompt belongs to the session that raised it. Rebuilding the
        # session -- switch, new, or the effort fork -- orphans its future, so
        # deny first rather than leaving one pending against a client that no
        # longer exists.
        self._resolve_all(guest, "rebuild",
                          "The conversation changed before this was answered.")
        self._fail_execs(guest, "The conversation changed while this command "
                                "was running.")
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
            # A finished block, like every other thing the user reads.
            #
            # This was a raw TOOLRESULT frame carrying plain text, and the
            # guest has no branch for that frame type -- so it fell through to
            # "unhandled frame type" and tool results have never appeared on
            # Windows 98 at all. The user saw "Bash: ls", then nothing, then
            # the model discussing output they could not see.
            #
            # Through _write_block it also enters the paginator, so it survives
            # a reconnect and is still there when paging back. A raw frame
            # never was. The tool_id is dropped: nothing correlated it, and the
            # result already follows its own call in the transcript.
            await self._flush_text(guest)
            await self._write_block(guest, H.tool_result(body, error=is_err))

        async def on_system(subtype: str, data: dict) -> None:
            if subtype == "compact_boundary":
                await self._write_block(guest, H.notice("History condensed."))
            elif subtype == "init":
                # The conversation id becomes known HERE, not at start().
                #
                # Session._session_id is only set from this message, which
                # arrives during the first turn -- so `guest.sess_id =
                # guest.claude.session_id or guest.sess_id` immediately after
                # the effort fork was a no-op against None, and the chat list
                # went on naming the PRE-FORK conversation until some later
                # turn happened to write a file. PLAN.md Phase 4 records the
                # symptom ("the transcript says the conversation was forked;
                # the list does not"); this is the cause.
                new = data.get("session_id")
                if new and new != guest.sess_id:
                    log.info("session id settled: %s -> %s", guest.sess_id, new)
                    guest.sess_id = new
                    self._spawn(self._send_sessions(guest))

        s = Session(
            cwd=self.cwd,
            # APPENDED to Claude Code's own prompt, never substituted. Without
            # it the model answers as though it were on this machine, and the
            # machine that matters is the 200MHz one on the other end.
            system_prompt={
                "type": "preset",
                "preset": "claude_code",
                # The sync root and the guest's own path for it, because the
                # prompt has to say WHICH directory is mirrored. Saying "your
                # edits reach the user" without naming the folder is how an
                # agent writes outside it and reports success.
                "append": machine.system_prompt(
                    guest.hello, self.cwd or os.getcwd(),
                    sync_dir=self.sync_dir,
                    guest_root=guest.hello.get("sync_root")
                    or self.guest_sync_root),
            },
            # The per-guest values, not the process defaults: the menu changes
            # these and a later reconnect or fork has to keep the choice.
            model=self._effective_model(guest),
            effort=self._effective_effort(guest),
            resume=resume,
            fork=fork,
            # Every tool call the CLI does not resolve locally comes back
            # through here as a prompt on the guest's screen.
            can_use_tool=self._can_use_tool(guest),
            # The key here becomes the mcp__<key>__<tool> prefix.
            mcp_servers=machine_tools.build(self, guest),
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
            prompt.summary = perms.summary(tool_name, ctx, args)

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
        await self._send(guest, permreq(
            prompt.tool_use_id, prompt.tool_name, prompt.allows,
            len(guest.perm_queue),
            getattr(prompt, "summary", prompt.tool_name)))

    async def _reissue_prompt(self, guest: GuestSession) -> None:
        """P1: after a reconnect, put the bar back."""
        prompt = guest.perm_pending
        if prompt is None:
            return
        log.info("re-issuing pending prompt %s after reconnect",
                 prompt.tool_use_id)
        await self._send(guest, permstate(
            prompt.tool_use_id, prompt.tool_name, prompt.allows,
            len(guest.perm_queue),
            getattr(prompt, "summary", prompt.tool_name)))

    async def _send_sessions(self, guest: GuestSession) -> None:
        """The chat list. Only conversations this bridge started -- the rest
        belong to a machine with a different way of working.

        Extracted so the list can be pushed when the conversation id CHANGES
        as well as when the guest asks. The effort fork changes it, and until
        now nothing told the guest.
        """
        rows = self.registry.filter(sesslist.listing(cwd=self.cwd))
        await self._send(guest, sessions_start(len(rows)))
        for i, r in enumerate(rows):
            await self._send(guest, session_row(
                i, r["id"], r["title"], r["cwd"], r.get("group", ""),
                bool(guest.claude and guest.claude.session_id == r["id"])))
        await self._send(guest, sessions_end())

    async def _reissue_halt(self, guest: GuestSession) -> None:
        """After a reconnect, put the Continue bar back.

        The third leg of the same idea as _reissue_prompt, and the one whose
        absence was worst. `halt_id` lives on the GuestSession and survives a
        reconnect; `latched` lives on disk. So a guest that restarted while a
        halt was up came back with NO BAR, a bridge still holding the halt, and
        every latched path refusing the agent's writes -- unanswerable, because
        RESUME is only accepted against a matching id and raising is idempotent
        while one is outstanding. Nothing short of restarting the bridge or
        hand-editing the baseline would have recovered it.

        No new frame type: the guest's HALT handler is idempotent, so re-sending
        the same one restores the bar.
        """
        if not guest.halt_id:
            return
        log.info("re-issuing halt %s after reconnect", guest.halt_id)
        await self._send(guest, halt(guest.halt_id, guest.halt_text),
                         stamp=False)

    async def _reissue_turn(self, guest: GuestSession) -> None:
        """After a reconnect, restore the busy UI if a turn is still running.

        Nothing re-sent TURN either, so a guest that blipped mid-turn came back
        with Send enabled and `g_turn_active` clear while the bridge-side turn
        was still streaming -- which is one of the ways two turns end up on one
        session, on a stream that can only have one consumer.

        Again no new frame: the guest's `TURN state=start` branch already sets
        the busy UI and the thinking indicator, so re-sending it is the whole
        fix. It restarts the elapsed timer, which is cosmetic.
        """
        session = guest.claude
        if session is None or not getattr(session, "busy", False):
            return
        log.info("re-issuing turn state after reconnect")
        await self._send(guest, turn_start(), stamp=False)

    async def _send_waiting(self, guest: GuestSession) -> None:
        """Refresh the queue depth shown on the bar."""
        prompt = guest.perm_pending
        if prompt is None:
            return
        await self._send(guest, permwait(prompt.tool_use_id,
                                         len(guest.perm_queue)))

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
        await self._send(guest, permend(tool_use_id, reason))
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
            await self._send(guest, pagesealed(pg.number, guest.pages.count))

    # --- frame pump --------------------------------------------------------

    async def _pump(
        self,
        guest: GuestSession,
        reader: asyncio.StreamReader,
        frames: FrameReader,
        early: list,
    ) -> None:
        # Frames that arrived in the same read as HELLO. See _read_hello.
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
            self._spawn(self._run_turn(guest, text))

        elif f.type == "RESUME":
            # The user pressed Continue on the halt bar. THAT is the signal the
            # design has been waiting for -- not an INPUT, and not a timer.
            #
            # "The agent stops until the user confirms they have finished with
            # the file" needs a confirmation, and until now the only inbound
            # channels were INPUT and PERMREPLY. Reading it off the next INPUT
            # was worse than useless: the most natural reaction to the notice
            # is to type "wait, stop", which is precisely the message that
            # would have restarted it.
            if f.headers.get("id", "") == guest.halt_id and guest.halt_id:
                hid, guest.halt_id = guest.halt_id, ""
                log.info("user pressed Continue on the sync halt")
                # Acknowledge, because the guest no longer takes its own bar
                # down on the click. A press that never reached us used to look
                # answered on their screen while we still held the halt -- and
                # since raising is idempotent, no later bar could appear and
                # every latched path refused the agent for good.
                await self._send(guest, haltend(hid), stamp=False)
                # Pressing Continue IS the confirmation, so it lifts the
                # refusal here. Making the agent ask again -- a permission
                # prompt for sync_resolve -- would put a second dialog in front
                # of a user who has just answered the question, on a 640x480
                # screen, and the second one carries no information the first
                # did not. `sync_resolve` stays for the case the user never
                # sees: the agent finishing a merge on its own.
                # The release travels WITH the turn that explains it, and
                # that ordering is the whole of M1.
                #
                # It used to run here, synchronously, and the informing turn
                # was spawned after it -- so an in-flight turn that had been
                # told nothing found the path no longer refusing its writes,
                # wrote, and the write went to the user's machine. Queueing the
                # turn (above) makes that window arbitrarily long, so the two
                # have to move together: _run_turn takes the lock, waits out
                # any turn ahead of it, and only then lifts the refusals it is
                # about to explain.
                answered, guest.halt_paths = guest.halt_paths, frozenset()
                self._spawn(self._resume_turn(guest, answered))

        elif f.type == "SYNCDIRTY":
            # The guest noticed its tree move. It never reconciles anything
            # itself -- the bridge owns the baseline and decides.
            self._spawn(self.run_sync("the guest reported a change"))

        elif f.type in ("SYNCENT", "SYNCEND", "SYNCPUTACK", "SYNCMKDIRACK",
                        "SYNCDAT", "SYNCDATEND"):
            await self._sync_frame(guest, f)

        elif f.type == "EXECOUT":
            await self._exec_out(guest, f)

        elif f.type == "EXECRESULT":
            await self._exec_result(guest, f)

        elif f.type == "PERMREPLY":
            await self._perm_reply(guest, f)

        elif f.type == "INTERRUPT":
            # Stop must reach the command too, not just the model.
            #
            # The CancelledError path in run_on_guest turns out to be largely
            # unreachable: on mcp 1.x the SDK does NOT cancel an in-flight
            # tool call when a turn is interrupted (types.py:653-662), so the
            # coroutine is never cancelled and never sends EXECKILL. Measured
            # on the guest -- `pause` kept running for its full 90s clamp
            # after Stop was pressed. Killing from here is the trigger that
            # actually fires, and it is the one the user meant: they pressed
            # Stop, so stop.
            for exec_id in list(guest.exec_pending):
                self._kill_exec(guest, exec_id)
            if guest.claude:
                await guest.claude.interrupt()

        elif f.type == "GETSESSIONS":
            await self._send_sessions(guest)

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
            await self._send(guest, pong(), stamp=False)

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

    def _with_sync_preamble(self, text: str) -> str:
        """Prepend anything sync needs the agent to know, and mark it delivered.

        Marked delivered when it is handed to the model, not when the model
        acts on it -- there is no signal for the latter, and re-sending on every
        turn until the agent happens to mention it would bury the next one.
        """
        if not self.sync_dir or not self._agent_notices:
            return text
        pending, self._agent_notices = self._agent_notices, []
        lines = ["[Automatic message from the file synchronisation layer, not "
                 "from the user.]", ""]
        lines.extend(pending)
        lines.append("")
        lines.append(
            "Deal with this before continuing with whatever was asked. A "
            "conflicted path REFUSES your writes until you call "
            "mcp__machine__sync_resolve on it, so writing to it again is not "
            "something you can do -- read the preserved copy and the ancestor "
            "beside it, work out the merge, and ask the user whether they have "
            "finished with the file. They approve that call on their own "
            "screen, which is how they tell you they are done.")
        lines.append("")
        lines.append("--- the user's message follows ---")
        lines.append("")
        return "\n".join(lines) + text

    async def _resume_turn(self, guest: GuestSession, answered) -> None:
        """Continue: wait for the wire to be ours, THEN unlatch, THEN explain.

        Never unlatch before the lock. A turn already running has not been told
        about this conflict -- that is why the latch exists -- and lifting the
        refusal while it runs is exactly the loss the latch was added to stop.
        """
        async with guest.turn_lock:
            freed = await self._release_all_latches(only=answered)
            if freed:
                log.info("sync: released %d latched path(s) on Continue", freed)
            await self._run_turn_locked(
                guest,
                "Resolve the sync conflict, then continue with what you "
                "were doing.", echo=False)

    async def _run_turn(self, guest: GuestSession, text: str,
                        *, echo: bool = True) -> None:
        if not guest.claude:
            return
        # QUEUED, not rejected. The user pressed Continue, or typed something;
        # dropping either on the floor is the one thing worse than running it
        # late. The wait is bounded by the turn ahead of it finishing.
        async with guest.turn_lock:
            await self._run_turn_locked(guest, text, echo=echo)

    async def _run_turn_locked(self, guest: GuestSession, text: str,
                               *, echo: bool = True) -> None:
        if not guest.claude:
            return
        # User turns are the landmarks a reader navigates by.
        anchor = guest.pages.mark_user_turn(text)
        # The guest echoed this line itself the moment Send was pressed, so it
        # is recorded but not streamed. USER_BLOCK is shared with the guest so
        # the two renderings are the same string, not merely similar.
        #
        # `echo=False` for a turn the bridge started on the user's behalf --
        # pressing Continue on a halt. Rendering it as though they had typed
        # "Resolve the sync conflict" would put words in their mouth in a
        # transcript they can scroll back through later.
        if echo:
            await self._write_block(guest, H.user_turn(text), stream=False)
        await self._send(guest, anchor_frame(
            anchor.page, anchor.offset, anchor.preview))
        await self._send(guest, turn_start())
        # Anything sync found while the agent was idle rides in on this turn.
        #
        # `_run_turn` is the only path into a session, and it fires only on
        # INPUT. A conflict found by the timer between turns therefore reaches
        # the USER's screen -- which is right, they are the one who can act on
        # it -- and reaches the agent nowhere at all. So it would never read
        # the preserved copy, never merge, and never call sync_resolve, and the
        # path would stay latched for the rest of the session with the agent
        # quietly unable to write to it and no idea why.
        text = self._with_sync_preamble(text)
        try:
            turn = await guest.claude.send(text)
        except Exception as exc:
            log.exception("turn failed")
            await self._flush_text(guest)
            await self._send(guest, turn_error(str(exc)))
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
            await self._send(guest, pagesealed(sealed.number,
                                               guest.pages.count))
        raw, comp = guest.zip_out.stats
        if comp:
            log.info("deflate so far: %d -> %d bytes (%.2f:1)",
                     raw, comp, guest.zip_out.ratio)
        await self._send(guest, turn_end(turn.ok, turn.subtype,
                                         turn.cost_usd, turn.duration_ms))

    # --- running commands on the guest -------------------------------------

    async def run_on_guest(self, command: str, *, timeout_ms: int,
                           shell: bool, cwd: str,
                           guest: "GuestSession | None" = None) -> dict:
        """Body of mcp__machine__exec. Returns an MCP tool result.

        `guest` is the session this tool belongs to. It defaults to the only
        one there can be today, but passing it is what makes the tool address
        A MACHINE rather than whichever machine happens to be attached when
        the model calls it: the MCP closure captured the Bridge, so with two
        clients this would run a command on the WRONG machine, with the right
        person's approval on their own screen. Three call sites today, eleven
        after Phase 6.
        """
        guest = guest or self._guest
        if guest is None or not guest.connected:
            # Structured error, never a hang. This is the cost of the
            # single-binary design and it was accepted on condition it is
            # handled here rather than left to the MCP timeout.
            return {"content": [{"type": "text", "text":
                    "The connected machine is not reachable right now -- it is "
                    "not connected to this proxy. Nothing was run."}],
                    "isError": True}

        # One worker thread over there means one command at a time.
        async with guest.exec_lock:
            # Re-check: acquiring the lock can take as long as the command
            # ahead of us, and _fail_execs in the disconnect path only covers
            # entries that existed when it ran -- ours would be registered
            # after it, and would then wait out the whole budget for a frame
            # that was never transmitted.
            if not guest.connected:
                return {"content": [{"type": "text", "text":
                        "The connected machine went away while this command "
                        "was waiting to run. Nothing was run."}],
                        "isError": True}
            guest.exec_seq += 1
            exec_id = f"x{guest.exec_seq}"
            fut = asyncio.get_running_loop().create_future()
            guest.exec_pending[exec_id] = {"future": fut, "out": bytearray()}

            if cwd:
                # Refuse rather than let the guest drop the frame.
                #
                # `cwd` travels as a header, and the guest's parser caps a
                # header value at 255 bytes. It used to CLIP there, silently,
                # and hand the short path to CreateProcessA -- which names a
                # different, existing directory, so the command ran somewhere
                # nobody chose. The guest now drops the frame instead, which
                # is loud but useless on its own: the bridge is the side that
                # can say something intelligible about it.
                if len(cwd.encode("utf-8", "surrogateescape")) > MAX_HEADER_VALUE:
                    return {
                        "content": [{"type": "text", "text": (
                            f"That working directory is too long to send to "
                            f"the machine ({len(cwd)} characters; the limit "
                            f"is {MAX_HEADER_VALUE}). Nothing was run. Use a "
                            f"shorter path, or cd to it inside the command.")}],
                        "isError": True,
                    }
            # stamp=False: an EXEC must NEVER enter the replay buffer.
            # Replay exists to redraw a transcript, and re-delivering this on
            # reconnect would RE-RUN the command -- `del c:\temp\*` twice is
            # not a redraw. Inert today only because the guest never sends
            # `from=`; this is here so implementing that later cannot arm it.
            await self._send(guest, exec_cmd(exec_id, command,
                                             timeout_ms=timeout_ms, shell=shell,
                                             cwd=cwd), stamp=False)
            log.info("exec %s: %s", exec_id, command[:160])

            # Above the guest's own limit, so a slow command produces a
            # "timed out" result FROM the guest rather than the bridge
            # abandoning it with the child still running over there.
            budget = (timeout_ms / 1000.0) + 30.0
            try:
                return await asyncio.wait_for(fut, timeout=budget)
            except asyncio.TimeoutError:
                self._kill_exec(guest, exec_id)
                return {"content": [{"type": "text", "text":
                        f"No reply from the connected machine after "
                        f"{budget:.0f}s; it has been asked to stop the "
                        f"command."}], "isError": True}
            except asyncio.CancelledError:
                # The turn was interrupted. Without telling the guest, the
                # command keeps running over there and the NEXT call is
                # answered `busy` -- so one interrupted turn poisons the
                # following one for a reason the model cannot see or act on.
                # Scheduled rather than awaited: we are inside a cancelled
                # scope, where an await aborts before it happens.
                self._kill_exec(guest, exec_id)
                raise
            finally:
                guest.exec_pending.pop(exec_id, None)

    def _kill_exec(self, guest: GuestSession, exec_id: str) -> None:
        """Ask the guest to stop a command we are no longer waiting for.

        Synchronous on purpose: the cancellation path cannot await.

        Deduplicated because both triggers can fire for one Stop -- the
        INTERRUPT handler always, and run_on_guest's CancelledError path when
        the SDK happens to cancel the tool as well. One kill is enough.
        """
        entry = guest.exec_pending.get(exec_id)
        if entry is not None:
            if entry.get("killed"):
                return
            entry["killed"] = True
        log.info("asking the guest to kill exec %s", exec_id)
        self._spawn(self._send(guest, exec_kill(exec_id), stamp=False))

    async def run_sync(self, why: str) -> dict:
        """One pass, serialised against every other trigger.

        Without the lock two passes would both read the baseline and both
        advance it, the second carrying a generation the first is about to
        invalidate.

        THE TRIGGERS, and this comment used to name three that did not exist.
        For most of Phase 3 there was exactly one caller -- the SYNCDIRTY
        handler -- so the entire 30-second cadence that the say-it-once
        machinery (first_report, reported_lines, aborted) is designed around
        lived in win/rpc.c, on the guest. If the guest's watcher thread died,
        or the guest was built without one, nothing on this side would ever
        have noticed:

          * the guest's watcher, via SYNCDIRTY -- still the primary one, and
            the only one that reacts to an actual change rather than a clock;
          * ON ATTACH, because a reconnecting guest may have been edited while
            it was away and nothing reconciled that;
          * a bridge-side sweep, as the backstop for a guest that has stopped
            telling us anything.
        """
        guest = self._guest
        if guest is None or not guest.connected or not self.sync_dir:
            return {}
        if guest.sync_lock.locked():
            log.debug("sync already running; coalescing (%s)", why)
            return {}

        async with guest.sync_lock:
            guest.sync_active = True
            try:
                from .files.syncrun import Pass
                p = Pass(self, guest, self.sync_dir, self.sync_state)
                got = await p.run()
                self.guest_sync_root = p.guest_root
                # Kept so sync_status can answer without re-running a pass --
                # which would be a scan of the whole tree on a 200MHz machine
                # every time the agent wondered whether its file had arrived.
                self._last_sync_reports = list(got.get("reports", []))
                log.info("sync (%s): %s", why,
                         {k: v for k, v in got.items()
                          if k not in ("reports", "notices")})
                # Reports are the only record of a path that did NOT move,
                # and a counter of zero says nothing about why. Logging them is
                # how "six of seven files transferred" stops looking like
                # success -- but only what CHANGED, because this pass runs
                # every thirty seconds and an unfixable name would otherwise
                # print until nobody reads the warnings.
                for line in got.get("new_reports", []):
                    log.warning("sync: %s", line)
                delivered = []
                for note in got.get("notices", []):
                    await self._write_block(guest, H.notice(note))
                    delivered.append(note)
                    # The agent needs the same news, and it has no other way to
                    # get it: nothing reaches a session except an INPUT.
                    self._agent_notices.append(note)

                # A conflict STOPS the agent, and stopping it means saying so
                # on the user's screen with a way to start it again. The
                # transcript already carries the explanation; this is the
                # button. Without it "wait for the user to confirm they have
                # finished with the file" has no confirmation -- the latch
                # would sit there until the agent happened to call
                # sync_resolve, which it has no reason to do unprompted.
                # NEW conflicts, not the running total. An unapplyable conflict
                # -- a file on the guest where the bridge holds a directory --
                # is re-decided on every pass because nothing can advance, so
                # raising on the total put the bar back up thirty seconds after
                # the user had answered it, spawned a "resolve the conflict"
                # turn with an empty preamble because the notice had already
                # been delivered, and did it again a minute later. Asked twice,
                # in the purest form the design has.
                if got.get("new_conflicts"):
                    # Short, because the bar at 640x480 gives the label about
                    # seventy characters before SS_ENDELLIPSIS cuts it. The
                    # first version ended "...Press Continue when you have
                    # finished with the file(s)" and was truncated at "you
                    # have" -- losing the half that said what the button does,
                    # on the one control the user needs. The button is labelled
                    # Continue and the whole explanation is in the transcript
                    # immediately above; the bar only has to say why it is up.
                    # The new count too, not the total: a standing conflict the
                    # user has already been told about should not inflate the
                    # number they are being stopped for.
                    n = got["new_conflicts"]
                    await self._raise_halt(
                        guest,
                        f"Stopped: {n} conflicting edit"
                        f"{'' if n == 1 else 's'}. Your copy was kept.",
                        paths=got.get("conflict_paths", ()))
                # Only after they are on the wire. The queue is pending work,
                # not a log: leaving them on it re-delivered every notice on
                # every pass, which buries the next real one.
                p.notices_delivered(delivered)
                return got
            except SyncError as exc:
                # A DELIBERATE refusal: a tree_id mismatch, a re-keyed
                # codepage, a manifest that arrived short. The whole pass
                # stops, which is right -- and used to be a log warning and
                # nothing else, so sync could be dead for an hour with the
                # agent and the user both believing it was working.
                await self._report_abort(guest, str(exc))
                return {"error": str(exc)}
            except Exception as exc:
                # NOT a refusal: a bug. The bare `except` here used to catch
                # both and describe both as "aborted", which is how the
                # directory-versus-file crash hid -- a total stop wearing the
                # words of a considered decision. Log the traceback and say
                # plainly that it was not deliberate.
                log.exception("sync pass FAILED (not a refusal): %s", exc)
                await self._report_abort(
                    guest, f"an internal error in the sync layer: {exc}",
                    bug=True)
                return {"error": str(exc)}
            finally:
                guest.sync_active = False

    # --- what the agent can ask about sync ---------------------------------

    async def _report_abort(self, guest: GuestSession, reason: str, *,
                            bug: bool = False) -> None:
        """Say ONCE that a whole pass refused to run.

        Once is the hard part. A `tree_id` mismatch aborts every pass, and a
        pass runs every thirty seconds, so the obvious "stop swallowing the
        exception" would have re-announced the same thing twice a minute --
        exactly the bug that `first_report` exists to prevent for a single
        path. `first_report` cannot be reused: an abort has no path key, and it
        raises BEFORE the pass saves anything, so there is no write to hang the
        fingerprint on. Hence a field on the baseline, written here.

        Keyed on the reason, so a DIFFERENT abort is still news -- a second
        machine connecting after a codepage change is not the same event as
        the codepage change.
        """
        log.warning("sync pass aborted: %s", reason)
        state = os.path.join(self.sync_state, "base.json")
        try:
            base = self._load_baseline()
        except Exception:
            # The baseline itself is what could not be read. Nothing to
            # remember it with, so say it every time rather than never: this
            # is the one abort that cannot be fixed by waiting.
            base = None
        if base is not None:
            # Anything already said since the last successful pass, not just
            # the last thing said. Two alternating reasons defeat a
            # last-reason check entirely, and two guests sharing a baseline
            # produce exactly that.
            if reason in base.announced:
                base.aborted = reason
                return
            base.aborted = reason
            base.announced.append(reason)
            try:
                base.save(state)
            except Exception as exc:          # pragma: no cover - disk failure
                log.warning("could not record the abort: %s", exc)

        what = ("File synchronisation stopped because of a fault in the "
                "bridge, not because of anything on your machine."
                if bug else
                "File synchronisation has stopped and is not copying "
                "anything in either direction.")
        text = (f"{what}\n\n{reason}\n\nNothing has been changed on either "
                f"side. Files edited here and there will both be kept, and "
                f"will reconcile once this is resolved.")
        await self._write_block(guest, H.notice(text))
        # The agent needs it too, and for the same reason the conflict notices
        # go this way: it will otherwise report a file as delivered when the
        # layer that delivers it has not run for an hour.
        self._agent_notices.append(
            f"SYNC HAS STOPPED -- {reason} No file is being copied in either "
            f"direction until this is resolved. Do not tell the user a file "
            f"has reached their machine; it has not.")

    def _load_baseline(self):
        from .files.sync import Baseline
        return Baseline.load(os.path.join(self.sync_state, "base.json"))

    def sync_status_text(self, guest: "GuestSession | None" = None) -> str:
        # `guest` is which machine is being asked about. Unused beyond this
        # note today because there is one; taking it means the tool addresses
        # a machine rather than "whichever is attached", which is what R7
        # needs and what makes the single-client assumption visible here
        # rather than buried in a closure.
        """Everything the agent cannot otherwise find out.

        Writing a file on the bridge always succeeds. Whether it reached the
        user's machine is a completely separate question, and without this
        there is no way to ask it -- so the agent reports a file as delivered
        because the write returned, which is true and useless.
        """
        if not self.sync_dir:
            return ("File synchronisation is not switched on for this session. "
                    "Nothing you write here reaches the user's machine, and "
                    "there is no tool that would copy it.")
        try:
            base = self._load_baseline()
        except Exception as exc:
            return (f"The synchronisation baseline could not be read ({exc}). "
                    f"No pass will run until that is fixed; tell the user.")

        out = [f"Mirrored: {self.sync_dir} here <-> "
               f"{self.guest_sync_root or '(not reported yet)'} on the "
               f"machine.",
               f"Last pass: generation {base.generation}."
               + (f"  Bound to tree {base.tree_id}." if base.tree_id else "")]

        # First, and in capitals, because it makes everything below it stale.
        # A pass-level abort stops the entire layer -- no file moves in either
        # direction -- and it used to be a log line, so the honest answer to
        # "did my file arrive" was unavailable to the agent AND the user.
        if base.aborted:
            out.append("")
            out.append("SYNCHRONISATION IS STOPPED. No file is being copied "
                       "in either direction, and nothing below this line has "
                       "moved since it stopped.")
            out.append(f"  Reason: {base.aborted}")
            out.append("  Tell the user. Do not report any file as having "
                       "reached their machine while this is the case.")

        # Why a file is treated the way it is. Without this the agent can see
        # that a path is byte-exact and has no way to learn that the tree ASKED
        # for that -- so a deliberate `-text` reads as a misclassification and
        # invites it to "fix" something that is already correct.
        try:
            from .files import attrs as A
            at = A.load(self.sync_dir)
        except Exception:
            at = None
        if at is not None and len(at):
            where = ", ".join(sorted(
                os.path.relpath(f, self.sync_dir) for f in at.sources)) or "?"
            out.append(
                f"\nLine-ending and text rules: {len(at)} in force, from "
                f"{where}. `-text`/`binary` means the two sides hold identical "
                f"bytes and nothing is converted; `eol=lf` or `eol=crlf` sets "
                f"what lands on the machine for that path. These are the "
                f"tree's own instructions -- do not override them to make a "
                f"file look tidier.")

        if base.latched:
            out.append("\nREFUSING YOUR WRITES -- these conflicted and the "
                       "user's copy won. Your version is preserved under "
                       f"{self.sync_dir.rstrip('/')}/../.gaston/conflict/ with "
                       "the common ancestor beside it. Read it, merge, ask the "
                       "user whether they have finished with the file, and "
                       "only then call sync_resolve:")
            for k, why in sorted(base.latched.items()):
                out.append(f"  {k} -- {why}")

        if self._last_sync_reports:
            out.append("\nFrom the last pass (skips are permanent until the "
                       "name or the path changes -- a skipped file NEVER "
                       "reaches the user, however many times you write it):")
            for line in self._last_sync_reports:
                out.append(f"  {line}")

        if base.tombs:
            out.append(f"\nDeleted on both sides and not tracked: "
                       f"{len(base.tombs)} path(s).")
        if base.ignored:
            out.append(f"Out of scope (.gitignore/.gastonignore): "
                       f"{len(base.ignored)} path(s), frozen at their last "
                       f"agreed content.")

        stray, stray_truncated = self._edits_outside_the_mirror()
        if stray:
            out.append("\nEDITED HERE BUT OUTSIDE THE MIRRORED FOLDER, so "
                       "these have not reached the user and never will where "
                       "they are:")
            out.extend(f"  {p}" for p in stray)
            if stray_truncated:
                out.append(f"  (stopped after {self.STRAY_SCAN_BUDGET} files "
                          f"outside the mirror; there may be more)")

        if len(out) == 2:
            out.append("\nNothing outstanding: no conflicts, no skips, "
                       "nothing withheld.")
        return "\n".join(out)

    #: Upper bound on how many files under cwd _edits_outside_the_mirror will
    #: stat in one call (02.34). It is called from sync_status, which tools.py
    #: advertises to the model as "free to call" -- but an unbounded os.walk
    #: of the whole working directory costs work that scales with a tree the
    #: agent itself grows, on every call, which is not free at all. Bounded
    #: like everything else here rather than assumed small.
    STRAY_SCAN_BUDGET = 4000

    def _edits_outside_the_mirror(
        self, limit: int = 20
    ) -> tuple[list[str], bool]:
        """Files under cwd but outside the sync root, newest first.

        The other way an agent achieves nothing and reports success:
        `off_limits` confines it to cwd, the mirror is a directory inside cwd,
        and an edit anywhere else succeeds and stays here forever.

        Returns `(paths, truncated)`: `truncated` is True when the walk hit
        STRAY_SCAN_BUDGET before finishing, so the caller can say the list may
        be incomplete rather than imply it is exhaustive.
        """
        cwd = self.cwd or os.getcwd()
        if not self.sync_dir or os.path.realpath(cwd) == os.path.realpath(
                self.sync_dir):
            return [], False
        mirror = os.path.realpath(self.sync_dir)
        found: list[tuple[float, str]] = []
        seen = 0
        truncated = False
        for dirpath, dirnames, filenames in os.walk(cwd):
            real = os.path.realpath(dirpath)
            if real == mirror or real.startswith(mirror + os.sep):
                dirnames[:] = []
                continue
            dirnames[:] = [d for d in dirnames
                           if not d.startswith(".") and d != "__pycache__"]
            for name in filenames:
                seen += 1
                if seen > self.STRAY_SCAN_BUDGET:
                    truncated = True
                    break
                full = os.path.join(dirpath, name)
                try:
                    found.append((os.stat(full).st_mtime,
                                  os.path.relpath(full, cwd)))
                except OSError:
                    continue
            if truncated:
                break
        found.sort(reverse=True)
        return [p for _, p in found[:limit]], truncated

    async def sync_resolve(self, path: str, action: str,
                           guest: "GuestSession | None" = None) -> str:
        """Lift one halt. Recorded in the baseline BEFORE it takes effect.

        Under sync_lock for the same reason as _release_all_latches: a Pass
        holds the baseline in memory and rewrites the whole of it at the end,
        so a release landing mid-pass was silently undone. This is the route
        the design tells the agent to use, which made it the likelier of the
        two to be lost -- the agent calls it while a pass is in flight because
        a pass is in flight most of the time.
        """
        if not self.sync_dir:
            return "File synchronisation is not switched on for this session."
        async with self._baseline_lock(guest):
            return self._sync_resolve_locked(path, action, guest or self._guest)

    def _sync_resolve_locked(
        self, path: str, action: str, guest: "GuestSession | None" = None
    ) -> str:
        try:
            base = self._load_baseline()
        except Exception as exc:
            return f"The baseline could not be read ({exc}); nothing changed."

        from .files import paths as P
        # guest.caps was parsed once at handshake (02.22). No guest at all
        # (never attached this process) is the one case with nothing to read
        # -- from_hello({}) is the documented restrictive Win9x default for
        # "no declaration", which is the honest answer here too.
        caps = guest.caps if guest is not None and guest.caps is not None \
            else C.from_hello({})
        key = P.canonical(path.replace("\\", "/").strip("/"), caps=caps)
        state = os.path.join(self.sync_state, "base.json")

        if action == "release":
            if key not in base.latched:
                return (f"{path} is not withheld. Nothing to release; if a "
                        f"write is not arriving, call sync_status -- it is "
                        f"more likely a skipped name than a conflict.")
            why = base.latched.pop(key)
            base.save(state)
            log.info("sync: %s released by the agent (was: %s)", key, why)
            # If nothing else is withheld, the reason for the halt is gone and
            # the bar must come down -- leaving it up would ask the user to
            # press Continue for a conflict that no longer exists.
            if not base.latched and self._guest is not None:
                self._spawn(self._clear_halt(self._guest))
            return (f"Released {path}. Your next write to it will be copied to "
                    f"the user's machine on the next pass. If they change it "
                    f"again in the meantime, it will conflict again -- which "
                    f"is correct, not a failure.")

        # forget
        removed = (base.tombs.pop(key, None) is not None
                   or base.rows.pop(key, None) is not None)
        base.latched.pop(key, None)
        base.save(state)
        if not removed:
            return f"{path} was not tracked; nothing to forget."
        log.info("sync: %s forgotten by the agent", key)
        return (f"Stopped tracking {path}. If it reappears on either side it "
                f"will be treated as new.")

    def _baseline_lock(self, guest: "GuestSession | None" = None):
        """The lock that serialises writers to base.json.

        It lives on the guest because that is where `Pass` takes it, and a pass
        cannot run without a guest -- so with no guest there is nothing to
        serialise against and a null context is not a shortcut, it is the
        accurate answer.
        """
        guest = guest or self._guest
        return guest.sync_lock if guest is not None else contextlib.nullcontext()

    async def _release_all_latches(self, only=None) -> int:
        """Lift the write refusals the user has just answered.

        All the ones the BAR named, not one: the bar says "N conflicting
        edits" and the user answers it once. Releasing a single path would
        leave the rest withheld with no bar up to say so, and the agent
        silently unable to write to files nobody has mentioned.

        But not paths the bar never named either. `_raise_halt` is idempotent
        while a bar is showing, so a conflict found in the meantime puts
        nothing on screen -- and releasing it on a press that predates it lifts
        the refusal on a path the agent has not been told about. It writes, and
        that write reaches the user's machine. `only` is the set the bar was
        raised for; None means everything, which is right when there is no bar
        to have named a subset.

        UNDER sync_lock, and that is the whole point of it being a coroutine.
        A Pass loads the baseline into memory when it starts and writes the
        WHOLE object back when it finishes -- so a release that lands in
        between was simply overwritten a moment later: latches restored, no
        bar up to say so, and no notice. The user pressed Continue and nothing
        happened, which is the one outcome the halt bar exists to prevent.
        And the press almost always lands mid-pass, because the pass that
        raised the bar is usually still running.
        """
        if not self.sync_dir:
            return 0
        async with self._baseline_lock():
            try:
                base = self._load_baseline()
            except Exception as exc:
                log.warning("could not release latches: %s", exc)
                return 0
            # Only what the bar was raised for. Anything latched since then
            # has never been on screen, so the press cannot be an answer to it.
            answered = set(only) if only is not None else set(base.latched)
            n = 0
            for key in answered:
                if base.latched.pop(key, None) is not None:
                    n += 1
            if n:
                base.save(os.path.join(self.sync_state, "base.json"))
            left = len(base.latched)
            if left:
                log.info("sync: %d path(s) latched after the release; they "
                         "were raised after the bar went up and will raise "
                         "their own", left)
            return n

    async def _raise_halt(self, guest: GuestSession, line: str,
                          paths=()) -> None:
        """Put the Continue bar up. Idempotent while one is already showing.

        `paths` is what this bar is ANSWERING FOR. Continue releases those and
        leaves anything latched later alone, because the idempotence above
        means a later conflict puts nothing on screen and the user cannot have
        answered a question they were never asked.
        """
        if guest.halt_id:
            return
        guest.halt_id = f"h{uuid.uuid4().hex[:8]}"
        guest.halt_paths = frozenset(paths)
        guest.halt_text = line
        await self._send(guest, halt(guest.halt_id, line), stamp=False)

    async def _clear_halt(self, guest: GuestSession) -> None:
        """Take it down because the reason went away, not because it was
        answered -- the agent called sync_resolve itself, say."""
        if not guest.halt_id:
            return
        hid, guest.halt_id = guest.halt_id, ""
        guest.halt_paths = frozenset()
        guest.halt_text = ""
        await self._send(guest, haltend(hid), stamp=False)

    async def _sync_frame(self, guest: GuestSession, f: Frame) -> None:
        """Route one SYNC* reply to whoever is waiting for it."""
        sid = f.headers.get("id", "")
        entry = guest.sync_pending.get(sid)
        if entry is None:
            # Expected after an abort: the caller gave up and cleaned up, and
            # this is the guest finishing what it had already started.
            log.debug("sync frame %s for %s, which nobody awaits", f.type, sid)
            return

        if f.type == "SYNCENT":
            # A manifest row against an id that is not a scan means the guest
            # is confused. Say so and carry on: this used to index a key that
            # only a scan entry has, and the KeyError is caught nowhere
            # between here and the read loop, so a malformed frame took the
            # whole connection down with it.
            if entry.kind != "scan":
                log.warning("SYNCENT for %s, which is a %s -- ignoring",
                            sid, entry.kind)
                return
            # The guest's own codepage, never an assumption. Decoding a
            # manifest with the wrong table produces names that look right and
            # cannot be encoded back to ask for the file. Parsed once at
            # handshake (02.22) rather than re-derived here.
            rows, display = manifest.parse_rows(f.body, guest.caps)
            entry.rows.update(rows)
            entry.display.update(display)

        elif f.type == "SYNCEND":
            fut = entry.future
            if not fut.done():
                fut.set_result((f.headers.get("status", "ok"),
                                int(f.headers.get("files", "0")),
                                f.headers.get("tree_id", ""),
                                f.headers.get("root", ""),
                                entry.rows, entry.display))

        elif f.type in ("SYNCPUTACK", "SYNCMKDIRACK"):
            fut = entry.future
            if not fut.done():
                fut.set_result((f.headers.get("status", "ok"),
                                int(f.headers.get("crc", "0"))))

        elif f.type == "SYNCDAT":
            if entry.kind != "get":
                log.warning("SYNCDAT for %s, which is a %s -- ignoring",
                            sid, entry.kind)
                return
            entry.data.extend(f.body)

        elif f.type == "SYNCDATEND":
            fut = entry.future
            if not fut.done():
                fut.set_result((f.headers.get("status", "ok"),
                                int(f.headers.get("crc", "0")),
                                f.headers.get("cls", P.CLASS_TEXT),
                                bytes(entry.data or b"")))

    def _fail_syncs(self, guest: GuestSession | None, why: str) -> None:
        """Resolve every in-flight sync transfer, synchronously."""
        if guest is None:
            return
        for sid, entry in list(guest.sync_pending.items()):
            fut = entry.get("future")
            if fut is not None and not fut.done():
                fut.set_exception(SyncError(why))
        guest.sync_pending.clear()
        guest.sync_active = False

    #: The bridge's own ceiling on captured output. The guest caps at 32KB, and
    #: this is the same number with room to spare -- it exists because the cap
    #: that mattered lived entirely in the OTHER binary. Nothing else here
    #: trusts a length a guest supplies, and a client that lied, or a regression
    #: in exec.c, could grow this buffer for as long as the call has left to
    #: run: 64KB a frame for up to 150 seconds.
    EXEC_OUT_MAX = 64 * 1024

    #: Prose restatement of the guest's own EXEC_MAX_CMD (win/rpc/exec.cpp,
    #: currently 1024) for the message shown to the model (03.17). Named
    #: rather than a bare literal in the string so a change on the guest side
    #: is at least a grep away from here instead of two independent numbers
    #: nothing ties together. Not enforced bridge-side before sending: the
    #: guest prepends its own shell prefix (`command.com /c ` vs `cmd.exe
    #: /c `, chosen by probe_exec from GetVersion()) whose length this side
    #: does not know, so a bridge-side pre-check would either reject commands
    #: that would have fit or pass some that would not -- the guest's own
    #: too_long refusal is the only place that number is authoritative.
    EXEC_MAX_CMD = 1024

    async def _exec_out(self, guest: GuestSession, f: Frame) -> None:
        entry = guest.exec_pending.get(f.headers.get("id", ""))
        if entry is None:
            return          # late chunk for a call we gave up on
        room = self.EXEC_OUT_MAX - len(entry["out"])
        if room <= 0:
            entry["capped"] = True
            return
        if len(f.body) > room:
            entry["out"].extend(f.body[:room])
            entry["capped"] = True
            return
        entry["out"].extend(f.body)

    async def _exec_result(self, guest: GuestSession, f: Frame) -> None:
        exec_id = f.headers.get("id", "")
        entry = guest.exec_pending.get(exec_id)
        if entry is None:
            # Expected after a kill: the caller has already given up and
            # cleaned up, and this is the guest confirming it stopped. Not a
            # problem, so do not log it like one.
            log.debug("EXECRESULT for %s, which nobody is waiting for "
                      "(usually a kill that has already been handled)",
                      exec_id)
            return
        fut = entry["future"]
        if fut.done():
            return

        status = f.headers.get("status", "ok")
        code = f.headers.get("code", "")
        # Either side may have cut it: the guest at its 32KB cap, or us at
        # ours. Both mean the same thing to the model -- what arrived is not
        # the whole of it -- so they are reported the same way.
        truncated = (f.headers.get("truncated", "0") == "1"
                     or bool(entry.get("capped")))

        # COMMAND.COM writes the OEM codepage, not the ANSI one. Using
        # fold.decode here instead is the classic Win9x encoding bug and turns
        # a `dir` listing into mojibake.
        text = fold.decode_oem(bytes(entry["out"]))

        parts = []
        if status == "spawn_failed":
            parts.append("The command could not be started on the connected "
                         "machine.")
        elif status == "too_long":
            # Was reported as spawn_failed, which reads as an environment
            # problem and invites a retry. It is a deterministic size
            # rejection, so retrying the same command never succeeds -- and
            # the model has no way to guess the limit unless it is told.
            parts.append(
                "The command line was too long for the connected machine and "
                f"was refused before it ran. The limit is about "
                f"{self.EXEC_MAX_CMD} characters including the shell prefix; "
                f"shorten it, or write the long part to a file and run that.")
        elif status == "timeout":
            parts.append(f"Timed out and was killed after the limit; partial "
                         f"output follows.")
        elif status == "busy":
            parts.append("The connected machine is already running a command.")
        elif status == "cancelled":
            parts.append("Cancelled -- the machine is shutting down.")
        if code not in ("", "0") and status == "ok":
            parts.append(f"Exit code {code}.")
        if truncated:
            # Whichever side actually cut it (03.16). entry["capped"] means
            # OUR OWN EXEC_OUT_MAX bit first; the guest's own 32KB cap is the
            # ordinary case and is what f.headers["truncated"] reports on its
            # own. Blaming the guest's cap unconditionally was wrong whenever
            # ours was the one that actually fired.
            if entry.get("capped"):
                parts.append(f"Output was capped at "
                            f"{self.EXEC_OUT_MAX // 1024}KB by this bridge "
                            f"and truncated.")
            else:
                parts.append("Output was capped at 32KB by the connected "
                            "machine and truncated.")

        body = text if text.strip() else "(no output)"
        head = (" ".join(parts) + "\n\n") if parts else ""
        fut.set_result({
            "content": [{"type": "text", "text": head + body}],
            "isError": status in ("spawn_failed", "too_long", "busy",
                                  "cancelled", "timeout"),
        })
        log.info("exec %s finished: status=%s code=%s bytes=%d",
                 exec_id, status, code, len(entry["out"]))

    def _fail_execs(self, guest: GuestSession | None, why: str) -> None:
        """Resolve every in-flight EXEC, synchronously.

        Called where the guest goes away or the session is rebuilt. A future
        nobody will complete holds the model for the whole MCP budget.
        """
        if guest is None:
            return
        for exec_id, entry in list(guest.exec_pending.items()):
            fut = entry["future"]
            if not fut.done():
                fut.set_result({"content": [{"type": "text", "text": why}],
                                "isError": True})
        guest.exec_pending.clear()

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
        await self._send(guest, permend(want, "answered"))
        await self._pump_queue(guest)

    async def _sweep_sync(self) -> None:
        """The backstop trigger. See run_sync for why it exists.

        Coalesced by run_sync's own lock, so a sweep that lands during a pass
        the guest asked for costs a debug line and nothing else.
        """
        while True:
            await asyncio.sleep(self.sync_sweep_s)
            if self.sync_dir and self._guest is not None:
                await self.run_sync("the bridge's own sweep")

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
            if guest.exec_pending or guest.sync_active:
                # A command can run quietly for up to two minutes -- longer
                # than the idle timeout -- and the guest has no keepalive of
                # its own, so `last_seen` only moves when output happens to
                # arrive. Reaping here would tear down the link mid-command,
                # which then drives the guest into its reconnect path. A
                # chatty command masks this; the first quiet one would not.
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

        in_use = resolved_of(self._effective_model(guest) or "default")

        chosen = None
        for m in models:
            value = m.get("value") or ""
            if value == "default":
                continue        # an alias, not a model
            label = m.get("displayName") or value
            current = (m.get("resolvedModel") or value) == in_use
            if current and chosen is None:
                chosen = m
            await self._send(guest, option("model", value, label, current))

        # Effort is a property of the model: Haiku has none at all, so an
        # Effort menu offering five levels would be lying.
        levels = (chosen or {}).get("supportedEffortLevels") or []
        if not levels:
            await self._send(guest, option(
                "effort", "", "(not available for this model)", False,
                disabled=True))
        else:
            for lvl in EFFORT_ORDER:
                if lvl not in levels:
                    continue
                await self._send(guest, option(
                    "effort", lvl, EFFORT_LABEL.get(lvl, lvl),
                    lvl == self._effective_effort(guest)))
        await self._send(guest, optionsend())

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
        await self._send(guest, switched(guest.claude.session_id or "",
                                         guest.pages.count))
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
        await self._send(guest, notice("switching", "Loading conversation..."))

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
        await self._send(guest, switched(session_id, guest.pages.count))
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

        await self._send(guest, pagestart(
            page.number, guest.pages.count, guest.view_page is None,
            page.continued))
        for run in page.runs:
            await self._send_html(guest, run.text, APPEND, page.number)
            # Between blocks, not just between chunks inside one (04.12): a
            # page can hold many runs, and this loop had no await point that
            # actually suspends any more than send_html's did on its own.
            await self.wire_yield(guest)
        await self._send(guest, pageend(page.number))

    # --- sending -------------------------------------------------------
    #
    # The transport itself -- the writer, the send lock, the compressor, the
    # chunking -- lives in GuestLink (bridge/wire/link.py) now. These three
    # stay on Bridge, as thin delegations to guest.link, because both
    # syncrun.py's Pass and tests/python reach them by these names.

    async def _send(self, guest: GuestSession, frame: Frame,
                    *, stamp: bool = True) -> None:
        await guest.link.send(guest, frame, stamp=stamp)

    async def _send_html(self, guest: GuestSession, html: str, mode: str,
                         page_no: int) -> None:
        await guest.link.send_html(guest, html, mode, page_no)

    async def wire_yield(self, guest: GuestSession) -> None:
        await guest.link.wire_yield()

    async def _try_send_raw(self, writer: asyncio.StreamWriter,
                            frame: Frame) -> None:
        await GuestLink.send_raw(writer, frame)


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
    # Deliberately separate from --cwd and never defaulted. Defaulting to the
    # launch directory would point sync at the whole repo, .venv and
    # vm/*.qcow2 included.
    ap.add_argument("--sync-dir", default=None,
                    help="the tree mirrored to the guest; sync is off without it")
    ap.add_argument("--sync-state", default=None,
                    help="where the baseline lives; MUST be outside --sync-dir, "
                         "or it is mirrored to the guest and recurses")
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
    if args.sync_dir:
        bridge.sync_dir = os.path.abspath(args.sync_dir)
        bridge.sync_state = os.path.abspath(
            args.sync_state
            or os.path.join(bridge.sync_dir, os.pardir, ".gaston-state"))
        # The baseline must not live inside the tree it describes, or a full
        # second copy of every file is mirrored to the guest, then scanned,
        # then copied again.
        if os.path.commonpath([bridge.sync_state, bridge.sync_dir]) \
                == bridge.sync_dir:
            raise SystemExit(
                "--sync-state must be OUTSIDE --sync-dir: a baseline inside "
                "the tree it describes gets mirrored to the guest and recurses")
        # And it must not live inside the WORKING DIRECTORY either, which is a
        # different requirement that happened to hold by default. off_limits
        # refuses paths outside cwd; it does not cover Bash, and nothing else
        # guards the baseline. A state directory under cwd is therefore one
        # `sed -i` away from a baseline that still parses and is wrong -- and a
        # three-way diff reaches confident conclusions from it, in whichever
        # direction the corruption points.
        if bridge.cwd:
            wd = os.path.realpath(bridge.cwd)
            try:
                under_cwd = os.path.commonpath(
                    [os.path.realpath(bridge.sync_state), wd]) == wd
            except ValueError:
                under_cwd = False
            if under_cwd:
                raise SystemExit(
                    "--sync-state must be OUTSIDE --cwd: the agent can reach "
                    "anything under the working directory with Bash, and a "
                    "baseline that has been edited still parses")
        os.makedirs(bridge.sync_state, exist_ok=True)
        log.info("sync: %s <-> guest, baseline in %s",
                 bridge.sync_dir, bridge.sync_state)
    try:
        asyncio.run(bridge.serve_forever())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
