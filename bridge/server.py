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

import datetime as _dt
import asyncio
import contextlib
import logging
import json
import os
import time
from dataclasses import dataclass, field

from . import handshake
from .files import caps as C
from .files import manifest
from .status import EXEC_STATUS_PROSE, ExecStatus, SyncStatus
from .wire import fold
from .agent import machine
from .agent import profiles
from .agent.conversations import Conversations
from .wire.coalesce import Coalescer
from .wire.compress import StreamCompressor
from .view import html as H
from .view.html import APPEND, LIVE, HtmlStream
from .view.pages import Paginator, limits_for
from .files import paths as P
from .agent import permissions as perms
from .agent import tools as machine_tools
from .agent.permdesk import PermissionDesk
from .agent.identity import GuestIdentity
from .synccoord import SyncCoordinator
from .wire.protocol import (
                            MAX_HEADER_VALUE,
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
                            pong,
                            ready,
                            session_row,
                            sessions_end,
                            sessions_start,
                            switched,
                            turn_end,
                            turn_error,
                            turn_start,
                            )
from .wire.link import GuestLink
from .files.synctransport import GuestSyncState, SyncTransport
from .view.transcript import TranscriptView
from .agent import sessions as sesslist
from .files.sync import Entry as SyncEntry, Type as SyncType
from .files.sync import AckResult, GetResult, ScanResult
from .files.syncrun import SyncError
from .agent.session import TOOLS, Session
from .agent import toolrep, toolrules
from .agent.toolrules import Found

log = logging.getLogger("bridge.server")

DEFAULT_PORT = 7890


@dataclass
class ExecCall:
    """One outstanding EXEC, and what has happened to it so far.

    Was an ad-hoc `{"future", "out"}` dict grown post-hoc with `"killed"`
    (set by `_kill_exec`) and `"capped"` (set by `_exec_out`), so every read
    was a defensive `entry.get(...)` because the key might not exist yet. A
    dataclass makes both booleans False by construction -- the exec-side twin
    of `sync.Wait` (02.4). docs/audit/02-bridge-python.md 02.13.
    """

    future: "asyncio.Future"
    out: bytearray = field(default_factory=bytearray)
    killed: bool = False
    capped: bool = False


def turn_verdict(ok: bool, degraded: str, produced: bool,
                 subtype: str) -> tuple[bool, str]:
    """What the status bar should say when a turn ends.

    The SDK's own verdict, unless it told us something during the turn that
    contradicts it AND the turn has nothing to show for itself. Both halves
    are needed, and the second was learned the hard way on the real machine.

    A rate-limited turn can come back `ok=True` having produced nothing, and
    reporting that as a success puts "done" in the status bar directly beneath
    a notice saying the request may not have run. But the SAME event arrives on
    a turn the SDK then RETRIES AND COMPLETES -- measured on the guest: the
    notice appeared, the command ran, the answer rendered, and the status bar
    still said "failed". That is the same contradiction wearing the opposite
    sign, and it is the worse of the two: a user who reads "failed" under a
    full, correct answer learns to disbelieve the status bar, and then the one
    time it is right they will not believe it either.

    `produced` comes only from the SDK's own callbacks, so a notice the BRIDGE
    wrote -- the sync preamble, the rate-limit warning itself -- does not count
    as the model having answered.
    """
    failed_silently = bool(degraded) and not produced
    return (ok and not failed_silently,
            degraded if failed_silently else subtype)


@dataclass
class GuestSession:
    """Bridge-side state for one guest, surviving across reconnects."""

    #: Set when the SDK reports something during a turn that means the answer
    #: cannot be trusted as complete -- a RateLimitEvent today. Read and
    #: cleared by _run_turn_locked when it sends TURN end.
    #:
    #: It exists because the SDK reports ok=True for a rate-limited turn that
    #: streamed nothing, so the guest's status bar said "done $?" next to a
    #: transcript notice explaining that the request may not have run. The
    #: transcript and the status line disagreeing is worse than either being
    #: wrong on its own, and the bridge is the side that knows.
    turn_degraded: str = ""
    #: Did the model actually produce anything this turn -- text or a tool
    #: call? Set from the SDK's own callbacks, so a notice the BRIDGE wrote
    #: (the sync preamble, a rate-limit warning) does not count as output.
    #: Reset when a turn starts. See where turn_end weighs it against
    #: `turn_degraded`.
    turn_produced: bool = False

    #: Which machine this is and which conversation is live -- `sess_id`,
    #: `hello`, `claude`, `model`, `effort` -- the audit's "Identity"
    #: GuestSession group (02.2), moved onto its own type in
    #: bridge/agent/identity.py the same way `link: GuestLink` already did
    #: for the connection. The five names below forward here so nothing
    #: outside this file had to change shape. docs/audit/02-bridge-python.md
    #: 02.42.
    identity: GuestIdentity = field(default_factory=GuestIdentity)
    #: Everything about writing bytes to this guest -- the socket writer, the
    #: send lock, the persistent compressor and the measured link speed. See
    #: bridge/wire/link.py. `writer`/`send_lock`/`zip_out`/`link_bps` below
    #: forward here so the rest of the bridge did not have to change shape.
    link: GuestLink = field(default_factory=GuestLink)
    #: The halt bar's state and the agent's notice queue: the halt id, the
    #: paths it was raised for, the answered-path set. See
    #: bridge/synccoord.py.
    #:
    #: 02.42 moved the halt trio off this class onto a `SyncCoordinator`;
    #: 02.43 gave that type the derivation as well as the fields -- and left
    #: the object itself a field of `Bridge`, which is what audit-2's 02.18
    #: came back for. One coordinator per BRIDGE means one halt bar and one
    #: notice queue shared by every client, so the moment R7 lands, agent A
    #: resolving its own conflict takes the bar down on B's screen and
    #: drains B's notices into A's next turn. It is one per GUEST here, which
    #: is what `synccoord.py`'s own docstring says the shape should be.
    #:
    #: `Bridge.synccoord` still exists and forwards to the attached guest's,
    #: exactly as `writer`/`zip_out`/`pages` forward the other way -- the
    #: single-client shortcut, in one place, for R7 to delete.
    synccoord: SyncCoordinator = field(default_factory=SyncCoordinator)
    # C.from_hello(hello), parsed once in _attach() rather than re-parsed at
    # every site that needs a path rule -- a re-parse cannot raise (already
    # validated at handshake) but silently re-derives what is already known,
    # and one prior re-parse site used to fall back to {} when this guest was
    # merely unset, guessing Win9x defaults instead of reading what the
    # client actually declared. See docs/audit/02-bridge-python.md #02.22.
    caps: "C.Caps | None" = None

    #: What that machine has installed, as HELLO's body said and the rules in
    #: bridge/agent/toolrules.py read it. Set once per CONNECTION, not once
    #: per session: it comes off HELLO, and every conversation opened on one
    #: connection sees the same inventory. A guest built before this existed
    #: sends no body and lands here as [], which is the same thing the prompt
    #: does with "nothing was detected" -- says nothing at all.
    tools: "list[Found]" = field(default_factory=list)
    #: That machine's PATH, as it was at connect. Same lifetime as `tools`.
    path: "list[str]" = field(default_factory=list)

    #: What this guest is watching and how much of it fits: the paginator,
    #: the renderer, the per-stream coalescers, which page is on screen, and
    #: the two page-size limits scaled to this guest's RAM at handshake --
    #: the audit's "View" GuestSession group (02.2), moved onto its own type
    #: in bridge/view/transcript.py the same way `link: GuestLink` already
    #: did for the connection. `pages`/`render`/`coalescers`/`view_page`/
    #: `page_bytes`/`page_blocks` below forward here so nothing outside this
    #: file had to change shape.
    view: TranscriptView = field(default_factory=TranscriptView)

    #: The prompt currently on the guest's screen, the ones queued behind it,
    #: and the state machine that owns both -- the audit's "Prompts"
    #: GuestSession group (02.2), moved onto its own type in
    #: bridge/agent/permdesk.py the same way `link: GuestLink` already did
    #: for the connection. `perm_pending`/`perm_queue` below forward here so
    #: nothing outside this file had to change shape. Unlike `link`/`view`/
    #: `sync`, there is no bare default: a `PermissionDesk` needs its owning
    #: guest and three of Bridge's own operations, so it is built and
    #: assigned in `_attach` right after the `GuestSession` itself.
    #: docs/audit/02-bridge-python.md 02.41.
    perms: "PermissionDesk | None" = None
    # When the guest was last heard from, for the idle reaper. A guest that
    # GPFs leaves no FIN behind, so without this _on_client parks forever and
    # `connected` stays True -- which rejects every reconnect as "busy".
    last_seen: float = field(default_factory=time.monotonic)
    # When the link dropped, or None while connected. A prompt outlives a
    # brief disconnect so a reconnect can restore the bar.
    lost_at: float | None = None
    # In-flight EXEC calls, keyed by our own id. Separate namespace from
    # tool_use_id so a stray frame cannot cross-resolve against TOOLRESULT.
    exec_pending: dict[str, ExecCall] = field(default_factory=dict)
    exec_seq: int = 0
    # One worker thread on the guest means one command at a time. The model
    # can request tools in parallel, and an "Always" rule would bypass the
    # prompt queue that was implicitly serialising them.
    exec_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    #: The sync subprotocol's own id namespace, in-flight table, baseline
    #: lock and running flag -- the audit's "Sync RPC" GuestSession group
    #: (docs/audit/02-bridge-python.md 02.2), moved onto its own type in
    #: bridge/files/synctransport.py the same way `link: GuestLink` already
    #: did for the connection itself. `sync_pending`/`sync_seq`/`sync_lock`/
    #: `sync_active` below forward here so nothing outside this file had to
    #: change shape.
    sync: GuestSyncState = field(default_factory=GuestSyncState)
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

    # --- forwarded to self.identity, for the same reason as self.link below -

    @property
    def sess_id(self) -> str:
        return self.identity.sess_id

    @sess_id.setter
    def sess_id(self, v: str) -> None:
        self.identity.sess_id = v

    @property
    def hello(self) -> dict:
        return self.identity.hello

    @hello.setter
    def hello(self, h: dict) -> None:
        self.identity.hello = h

    @property
    def claude(self) -> "Session | None":
        return self.identity.claude

    @claude.setter
    def claude(self, s: "Session | None") -> None:
        self.identity.claude = s

    @property
    def model(self) -> str:
        return self.identity.model

    @model.setter
    def model(self, m: str) -> None:
        self.identity.model = m

    @property
    def effort(self) -> str:
        return self.identity.effort

    @effort.setter
    def effort(self, e: str) -> None:
        self.identity.effort = e

    @property
    def work_dir(self) -> str:
        return self.identity.work_dir

    @work_dir.setter
    def work_dir(self, d: str) -> None:
        self.identity.work_dir = d

    @property
    def first_prompt(self) -> str:
        return self.identity.first_prompt

    @first_prompt.setter
    def first_prompt(self, t: str) -> None:
        self.identity.first_prompt = t

    @property
    def work_guest(self) -> str:
        return self.identity.work_guest

    @work_guest.setter
    def work_guest(self, d: str) -> None:
        self.identity.work_guest = d

    @property
    def pending_resume(self) -> str:
        return self.identity.pending_resume

    @pending_resume.setter
    def pending_resume(self, s: str) -> None:
        self.identity.pending_resume = s

    @property
    def conv_id(self) -> str:
        """Which conversation this guest is looking at.

        Either the live client's id, or -- after a switch that has not been
        spoken in yet -- the id it will resume from. Everything that asks
        "which conversation" should ask here rather than reaching for
        `claude.session_id`, which is None for a conversation that is open on
        screen and simply has no process behind it.
        """
        if self.claude and self.claude.session_id:
            return self.claude.session_id
        return self.identity.pending_resume

    @property
    def perm_mode(self) -> str:
        return self.identity.perm_mode

    @perm_mode.setter
    def perm_mode(self, m: str) -> None:
        self.identity.perm_mode = m

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

    # --- forwarded to self.sync, for the same reason as self.link above -----

    @property
    def sync_pending(self) -> dict:
        return self.sync.pending

    @property
    def sync_seq(self) -> int:
        return self.sync.seq

    @property
    def sync_lock(self) -> asyncio.Lock:
        return self.sync.lock

    @property
    def sync_active(self) -> bool:
        return self.sync.active

    @sync_active.setter
    def sync_active(self, v: bool) -> None:
        self.sync.active = v

    # --- forwarded to self.perms, for the same reason as self.link above ----

    @property
    def perm_pending(self) -> "PendingPrompt | None":
        return self.perms.pending if self.perms else None

    @perm_pending.setter
    def perm_pending(self, prompt: "PendingPrompt | None") -> None:
        self.perms.pending = prompt

    @property
    def perm_queue(self):
        return self.perms.queue

    # --- forwarded to self.view, for the same reason as self.link above -----

    @property
    def pages(self) -> Paginator:
        return self.view.pages

    @pages.setter
    def pages(self, p: Paginator) -> None:
        self.view.pages = p

    @property
    def render(self) -> HtmlStream:
        return self.view.render

    @render.setter
    def render(self, r: HtmlStream) -> None:
        self.view.render = r

    @property
    def coalescers(self) -> dict:
        return self.view.coalescers

    @property
    def view_page(self) -> int | None:
        return self.view.view_page

    @view_page.setter
    def view_page(self, n: int | None) -> None:
        self.view.view_page = n

    @property
    def page_bytes(self) -> int:
        return self.view.page_bytes

    @page_bytes.setter
    def page_bytes(self, n: int) -> None:
        self.view.page_bytes = n

    @property
    def page_blocks(self) -> int:
        return self.view.page_blocks

    @page_blocks.setter
    def page_blocks(self, n: int) -> None:
        self.view.page_blocks = n

    # --- transcript housekeeping --------------------------------------------

    def set_link_speed(self, bps: float | None) -> None:
        """Propagate a measured (or CLI-default) link speed to every live
        coalescer, so a conversation already streaming picks it up too, not
        only ones started after. Was `guest.link_bps = bps; for c in
        guest.coalescers.values(): c.set_link_speed(bps)` at two call sites.

        Spans two of the audit's groups (Link's `link_bps`, View's
        `coalescers`) and stays here, on the composition root, rather than on
        either `GuestLink` or `TranscriptView` -- neither owns both halves of
        what it does.
        """
        self.link_bps = bps
        for c in self.view.coalescers.values():
            c.set_link_speed(bps)

    async def reset_transcript(self) -> None:
        """`TranscriptView.reset()`, kept as a same-named delegation so
        `_new_session`/`_switch` did not have to change shape (02.2)."""
        await self.view.reset()


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
        #: What this bridge knows about each conversation: whether it
        #: started it (so the chat list shows those and not every
        #: conversation on the machine), the model, effort and permission
        #: mode chosen for it, and the folder it works in on both sides.
        #: One row, one file -- see agent/conversations.py for why those
        #: three were not worth three stores.
        self.conv = Conversations()
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
        #: See _models(). None means "not asked yet", never "asked and empty".
        self._models_cache: list[dict] | None = None
        #: The CLI's slash-command list, cached beside the model list and for
        #: the same reasons: it is a property of the installed CLI, and a
        #: conversation opened from the chat list has no client to ask until
        #: the first message. Cleared wherever the model cache is.
        self._commands_cache: list[dict] | None = None
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
        #: Consecutive passes refused for a reason that fixes itself. See the
        #: `transient` branch in run_sync.
        self._transient_aborts = 0
        self._last_sync_reports: list[str] = []
        #: Resolved baseline file per guest root, so the lookup below is not
        #: repeated on every pass.
        self._base_paths: dict[str, str] = {}
        #: Where `self.synccoord` points until a guest exists. Handed to
        #: the `GuestSession` when it is built, so the object's identity
        #: survives the handover and anything recorded before the first
        #: attach is still there afterwards. See the property below.
        self._preattach_synccoord = SyncCoordinator()
        # How long a pending permission request survives a dropped link. Long
        # enough for the guest's 1/2/4/8s reconnect backoff to win, far short
        # of the 10 minute answer deadline.
        self.perm_grace_s = 60.0

    @property
    def synccoord(self) -> SyncCoordinator:
        """The attached guest's coordinator. 02.18.

        A FORWARD, not storage. The halt bar and the agent notice queue are
        per-machine state and now live on `GuestSession`; this keeps the
        thirty-odd `self.synccoord` call sites -- and every test that reads
        `bridge.synccoord` -- reading as they did, the same way
        `GuestSession.writer` forwards to `link.writer` so that "nothing
        outside this file had to change shape".

        It is also precisely the single-client assumption, named and in one
        place: R7 deletes this property and makes each caller say which guest
        it means. Several already can -- `sync_status_text`, `sync_resolve`
        and `_reconcile_halt` all take a guest today.
        """
        g = self._guest
        return g.synccoord if g is not None else self._preattach_synccoord

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
        # BOTH long-lived loops, and everything _spawn started.
        #
        # The sweeper was missed here, so a bridge shut down cleanly left
        # `_sweep_sync` looping every 120s against a guest whose writer was
        # gone and a Claude session already closed. `run_sync` returns early
        # on `not guest.connected`, so it was noise rather than damage -- but
        # in-process (the test suite, and any future embedding) it is a task
        # that outlives its owner, and R7 has to fix it here anyway.
        for name in ("_reaper", "_sweeper"):
            task = getattr(self, name, None)
            if task is not None:
                task.cancel()
                setattr(self, name, None)
        pending, self._tasks = list(self._tasks), set()
        for task in pending:
            task.cancel()
        if pending:
            # Gathered rather than merely cancelled: a cancel only REQUESTS
            # it, and a task suspended in a `finally` can still be running
            # when the loop is torn down underneath it.
            await asyncio.gather(*pending, return_exceptions=True)
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

            # STARTED AFTER _attach, not before it, and the ordering is the
            # whole of 02.6. `asyncio.create_task` copies the CURRENT
            # context, and `_attach` is where `fold.use` binds this
            # connection's codepages -- so a reaper and a sweeper created up
            # here ran forever in a context where `fold.codec()` was still
            # the cp1252 default. A client declaring acp=1251 then got
            # correctly-encoded sync notices for passes IT triggered and
            # mojibake for the ones the bridge's own 120s backstop started,
            # inside a document whose <meta charset> said windows-1251. The
            # two paths are indistinguishable from the outside, which is what
            # makes that undiagnosable rather than merely wrong.
            #
            # Started here rather than in serve_forever() because that is not
            # the only way a bridge gets clients -- the tests drive
            # _on_client directly, and a liveness mechanism that depends on
            # which entry point was used is a liveness mechanism you do not
            # have. Nothing needs reaping before a guest exists: _read_hello
            # has its own 10s deadline.
            if self._reaper is None:
                self._reaper = asyncio.create_task(self._reap_idle())
            if self._sweeper is None and self.sync_dir:
                self._sweeper = asyncio.create_task(self._sweep_sync())

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
            guest = self._guest
            # THE SECOND DISJUNCT IS 02.8. The identity check is what stops a
            # late-closing old connection tearing down a live one, and it is
            # right for that -- but the idle reaper nulls `writer` ITSELF
            # before closing the socket, so after a reap this test was False
            # and the whole block was skipped. `lost_at` then stayed None,
            # and a prompt raised by the still-running turn afterwards waited
            # the full 600s answer deadline instead of the 60s grace, because
            # `_reap_idle`'s expiry branch is gated on `lost_at is not None`.
            # A writer of None cannot belong to a newer connection: `_attach`
            # sets it before `_pump` can return.
            if guest and (guest.writer is writer or guest.writer is None):
                guest.writer = None
                self._mark_lost(guest)
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

    @staticmethod
    def _oem_for(guest: GuestSession) -> str:
        """Which codepage this machine's console children write.

        A different question from the ANSI one with a different answer:
        win/net/net.cpp says exactly that and then never sent an answer, so
        `decode_oem` defaulted to cp437 for every machine.

        THE MACHINE FIRST, then the shell profile. A profile can say that
        this shell's children write OEM; only the machine knows which OEM it
        is set to, and it now sends GetOEMCP() beside GetACP().
        """
        caps = guest.caps
        shell = toolrules.shell_of(guest.tools)
        return (getattr(caps, "oemcp", "") or
                (str(profiles.shell(shell).get("oem_codepage", ""))
                 if shell else ""))

    def _bind_codepages(self, guest: GuestSession) -> None:
        """Bind this guest's display and console codepages in THIS context.

        `fold` keeps both in `ContextVar`s and `asyncio.create_task` copies
        the context at creation, so a task that outlives the call that
        created it -- the sweeper, the reaper -- can only be right if it
        binds them itself before it writes anything. See 02.6, and _on_client
        for the ordering half of the same bug.
        """
        caps = guest.caps
        if caps is None:
            return
        fold.use(caps.acp)
        oem = self._oem_for(guest)
        if oem:
            fold.use(oem=oem)

    async def _read_hello(
        self,
        reader: asyncio.StreamReader,
        frames: FrameReader,
        writer: asyncio.StreamWriter,
    ) -> "tuple[Frame | None, list[Frame]]":
        """First frame must be HELLO, within a short deadline.

        Returns `(hello, pipelined)`. A TWO-tuple on every path, which it was
        not: the three refusals below each returned `None, [], []` while the
        one caller unpacks two (`hello, early = await self._read_hello(...)`).
        So an EOF, a non-HELLO first frame and a handshake timeout all raised
        `ValueError: too many values to unpack` into `_on_client`'s catch-all
        and were logged as an unhandled error with a traceback.

        Nobody noticed because the refusal frame is sent by `_try_send_raw`
        BEFORE the return, so the client saw the right thing and only the
        bridge's own log was wrong -- which is exactly the shape of bug that
        survives a test suite. The annotation said `Frame | None` and was
        wrong for both shapes.
        """
        try:
            async with asyncio.timeout(10):
                while True:
                    data = await reader.read(4096)
                    if not data:
                        return None, []
                    batch = frames.feed(data)
                    for i, f in enumerate(batch):
                        if f.type != "HELLO":
                            await self._try_send_raw(
                                writer, error("expected_hello",
                                              f"first frame was {f.type}")
                            )
                            return None, []
                        # Anything pipelined behind HELLO in the same read has
                        # already been parsed out of the FrameReader's buffer,
                        # so returning here would drop it silently. Hand the
                        # rest to _pump instead.
                        return f, batch[i + 1:]
        except TimeoutError:
            await self._try_send_raw(writer, error("hello_timeout",
                                                   "no HELLO within 10s"))
            return None, []

    async def _attach(
        self, hello: Frame, writer: asyncio.StreamWriter
    ) -> GuestSession | None:
        h = hello.headers

        try:
            neg = handshake.negotiate(h, self.secret)
        except handshake.Refused as exc:
            if exc.code == "bad_secret":
                log.warning("bad secret from %s",
                            writer.get_extra_info("peername"))
            elif exc.code == "bad_caps":
                log.warning("client declared rules this bridge cannot "
                            "honour: %s", exc.message)
            await self._try_send_raw(writer, error(exc.code, exc.message))
            return None

        # A second client is rejected, not evicted: whoever is already holding
        # the session may be mid-turn, and silently stealing it is worse than
        # refusing. Checked here rather than inside negotiate() because it is
        # live Bridge state, not a fact about the HELLO frame -- see
        # bridge/handshake.py.
        if self._guest and self._guest.connected:
            await self._try_send_raw(writer, error(
                "busy", "another client holds this session"))
            return None

        page_bytes, page_blocks, caps = neg.page_bytes, neg.page_blocks, neg.caps
        log.info("guest has %d MB; pages capped at %d bytes / %d blocks",
                 neg.mem_kb // 1024, page_bytes, page_blocks)
        # What the bridge now believes the machine IS. Logged at connect
        # because "why did that file not sync" starts here, and because a
        # client that declares nothing is indistinguishable from one that
        # declares the defaults until someone can see which happened.
        log.info("client rules: %s", caps.summary())
        # And which tree it says it is mirroring. The root is the
        # first thing that goes wrong when files do not move, and it
        # is the ONE fact the guest reports that the bridge cannot
        # derive -- a conversation folder applied before a bridge
        # restart outlives that restart on the guest's side, so a
        # HELLO can name a narrower tree than this process knows
        # anything about.
        log.info("guest mirrors %s", h.get("sync_root")
                 or "(file sharing is off)")

        # A fresh connection means a fresh inflater on the guest, so the
        # compressor must restart in lockstep or the first frame is garbage.
        # There used to be a second consequence: the (now-retired) replay
        # buffer had to be invalidated here too, because _write_block runs a
        # block through the PERSISTENT compressor before _send ever sees it,
        # so a deflated frame kept for replay was output from a stream that
        # no longer exists once the guest's inflater restarts. Nothing keeps
        # frames around to replay any more, so there is nothing left to
        # invalidate -- see docs/BACKLOG.md #5.
        is_reconnect = self._guest is not None
        if self._guest is not None:
            self._guest.zip_out.reset()

        if self._guest is None:
            # sess_id is not passed here: GuestIdentity's own default_factory
            # generates it (bridge/agent/identity.py), the same uuid4() this
            # used to construct inline -- sess_id stopped being a GuestSession
            # field directly when Identity moved onto its own type (02.42),
            # and it is not a GuestSession() keyword the way `view` below is
            # because there is nothing case-specific about it to pass.
            self._guest = GuestSession(
                view=TranscriptView(pages=Paginator(page_bytes, page_blocks)),
                # The one built in __init__, not a fresh one: `self.synccoord`
                # has been answering from it since the process started, and a
                # notice seeded before the first client connected has to
                # still be in the queue after it does.
                synccoord=self._preattach_synccoord)
            # PermissionDesk needs its own owning guest to reach _write_block
            # and _send with, so it cannot be built as a GuestSession()
            # keyword the way `view` above is -- it is assigned right after,
            # same lifetime as the guest itself (a prompt must survive a
            # reconnect).
            self._guest.perms = PermissionDesk(
                self._guest, cwd=self.cwd,
                write_block=lambda html: self._write_block(self._guest, html),
                send=lambda frame: self._send(self._guest, frame),
                spawn=self._spawn,
                ensure_work=lambda: self._ensure_work_dir(self._guest))
        guest = self._guest
        guest.writer = writer
        guest.page_bytes = page_bytes
        guest.page_blocks = page_blocks
        guest.hello = dict(h)
        guest.caps = caps
        # BIND THE DISPLAY CODEPAGE FOR THIS CONNECTION, here, because this is
        # the first moment it is known and everything below writes text.
        #
        # `caps.acp` has been negotiated since the handshake was written and
        # only the FILE layer read it. `fold` hardcoded cp1252 at six places
        # and `protocol.py` at two more, so a client reporting acp=1251
        # connected, synced its filenames perfectly, and saw every one of them
        # as `??????.txt` in the transcript. Set once per connection; asyncio
        # tasks spawned from here inherit it.
        fold.use(caps.acp)
        # THE INVENTORY, off HELLO's body rather than a frame of its own.
        #
        # It has to be here, above _make_session below, because that call
        # freezes the system prompt for the session -- and on a fresh
        # connection that is the session the user then talks to. A TOOLS
        # frame sent after connect would arrive after _pump starts reading,
        # which is after this, which is too late every time.
        #
        # Decoded through `caps`, not the raw `acp` header: the header is a
        # codepage number and the codec name comes from codec_for_acp. A path
        # under C:\Program Files is ASCII, but a German machine's is not.
        _facts = toolrep.parse_rows(hello.body or b"", caps)
        guest.tools = toolrules.inventory(_facts)
        # PATH separately from the inventory: it is not a tool, it is the
        # reason a tool that IS installed comes back "Bad command or file
        # name". Every exec is a fresh shell, so the machine's own start-up
        # scripts have not run.
        _shell = toolrules.shell_of(guest.tools)
        guest.path = toolrules.path_dirs(_facts, _shell)
        # The CONSOLE codepage, which is a different question from the ANSI
        # one with a different answer -- win/net/net.cpp says exactly that and
        # then never sent an answer, so `decode_oem` defaulted to cp437 for
        # every machine. The shell declares it, because it is the shell that
        # spawns the child whose bytes these are.
        # THE MACHINE FIRST, then the shell profile -- see _oem_for. Bound
        # through the same helper the sweeper uses, so there is one
        # derivation rather than two that can disagree (02.6).
        self._bind_codepages(guest)
        if guest.tools:
            log.info("machine has: %s", ", ".join(
                "%s (%s)" % (f.name, f.path) for f in guest.tools))

        guest.set_link_speed(self.link_bps)

        # Not when a conversation is open-but-unspoken: the guest reconnecting
        # must not start the process that switching to it deliberately did
        # not. This is the one place a fresh bridge DOES build eagerly, and it
        # is what warms the model cache the deferred path then reads.
        if guest.claude is None and not guest.pending_resume:
            guest.claude = await self._make_session(guest)

        await self._send(guest, ready(
            guest.sess_id,
            (guest.claude.model if guest.claude else guest.model)
            or "(default)",
            guest.pages.live.number, guest.pages.count,
            conv=guest.conv_id))

        # The stylesheet and the document skeleton live here rather than in
        # the guest, so the look can be changed by restarting the bridge
        # instead of rebuilding and re-deploying a binary.
        await self._send(guest, doc(H.document()))
        await self._send_options(guest)
        # There is no replay to rely on for anything a reconnect needs back
        # (docs/BACKLOG.md #5: the guest never sent `from=`, so that path was
        # always dead code) -- every piece of live state below is re-issued
        # explicitly instead, or a reconnect would arrive with the bar gone
        # and the future still pending bridge-side.
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
        await guest.perms.reissue()
        await self._reissue_halt(guest)
        await self._reissue_turn(guest)
        # _reissue_halt above handles a GUEST reconnect: the same Bridge (and
        # its synccoord) survives it, so synccoord.halt_id already agrees
        # with base.latched. It does NOT handle a BRIDGE restart, which
        # starts a brand new SyncCoordinator with halt_id empty while
        # base.latched is still whatever the old process left on disk
        # (02.21) -- so derive it instead of trusting whichever path last
        # had a chance to set it.
        await self._reconcile_halt(guest)
        # A guest that was away may have been edited while it was away, and
        # nothing reconciled that until its watcher next happened to fire.
        if self.sync_dir:
            self._seed_agent_notices()
            self._spawn(self.run_sync("the machine reconnected"))

        log.info("attached sess=%s (reconnect=%s)", guest.sess_id, is_reconnect)
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

    # --- the conversation's own folder -------------------------------------
    #
    # A SUBFOLDER OF THE MIRROR, never a second mirror. BACKLOG.md §7 settled
    # that a mirror belongs to a conversation; what it did not settle is
    # whether "the folder moves" means moving the sync root, and moving it
    # would mean a new baseline, a new `tree_id`, a watcher restart and a
    # recovery pass on the new tree -- four chances to report deletions
    # nobody made, in the one layer of this system that can lose a person's
    # work. Putting the conversation inside the existing mirror gets the same
    # property (each chat has its own folder, and two chats cannot overwrite
    # each other's files) with one mirror, one baseline and one watcher,
    # exactly as §7 says the decision requires.
    #
    # It also makes a nicer guarantee than the original shape: with the
    # agent's cwd INSIDE the mirror, everything it can write is mirrored, so
    # `_scan_strays` has nothing to find by construction.

    #: A folder name is read by a person, in a title bar and a status line, so
    #: it is capped for legibility as well as by the filesystem. A client with
    #: long names gets a readable slice of the conversation title rather than
    #: 255 characters of it.
    FOLDER_MAX_LONG = 32

    @staticmethod
    def folder_max(caps=None) -> int:
        """How long this client's conversation folder may be.

        `shortnames` has been on the wire since the handshake was written and
        was discarded on arrival -- it counted toward `declared` and had no
        field, so a client asking IN WRITING for the restrictive rule was
        answered with a hardcoded 8. Now the 8 is what a silent client is
        assumed to want, and a client that says otherwise is believed.
        """
        caps = caps if caps is not None else P.DEFAULT
        if caps.shortnames:
            return C.SHORTNAME_MAX
        return min(caps.maxcomp, Bridge.FOLDER_MAX_LONG)

    @staticmethod
    def folder_name(raw: str, caps=None) -> str:
        """A folder name that is safe on both sides, or "" if nothing is left.

        Refuses rather than repairs anything carrying a separator or a `..`:
        a name with one in it is not a typo to fix, it is a way out of the
        folder, and quietly rewriting it into something that works is how a
        guard becomes decoration. Everything else is reduced rather than
        refused, because this is usually fed a CONVERSATION TITLE -- prose
        with spaces and punctuation in it -- and refusing "List files in
        C:\\RETRO" would mean no folder at all.

        The illegal set comes from `caps`, not from a literal here. It used to
        be spelled out -- separators, `..`, and `* ? " < > |` -- which is
        COMMAND.COM's set written into the bridge while `caps.illegal` sat
        negotiated and unread, and `paths._component_ok` did the same job
        correctly two modules away.
        """
        raw = (raw or "").strip()
        if not raw:
            return ""
        caps = caps if caps is not None else P.DEFAULT
        # Separators and `..` are refused whatever the client says: they are
        # how a name leaves the folder, which is not a filesystem question.
        bad = P.illegal_chars(caps) | {"/", ":", ".."}
        if any(b in raw for b in bad):
            return ""
        # Uppercased only where the volume aliases, because the alias itself
        # is uppercase. Elsewhere the title's own case is worth keeping.
        text = raw.upper() if caps.shortnames else raw
        out = "".join(c for c in text
                      if c.isascii() and (c.isalnum() or c in "-_"))
        out = out.strip("-_")[:Bridge.folder_max(caps)]
        # Last word to the same check every synced path gets, so a folder
        # cannot be called CON on a machine where that is a device.
        return "" if out and P.component_ok(out, caps) else out

    @staticmethod
    def _title_folder(title: str, caps=None) -> str:
        """A folder name from a conversation title, reduced not refused.

        The title is prose and the folder is 8.3, so punctuation goes and the
        rest is truncated. "List files in C:\\RETRO" has a colon in it, which
        `folder_name` refuses on purpose -- so the separators are stripped
        HERE, where they are known to be part of a sentence rather than an
        attempt to leave the folder.
        """
        keep = []
        for ch in (title or ""):
            keep.append(ch if (ch.isascii() and (ch.isalnum() or ch in "-_"))
                        else " ")
        return Bridge.folder_name("".join(keep).replace(" ", ""), caps)

    def _stored_work_dir(self, session_id: str) -> str:
        """The folder a stored conversation was started in, if it is ours.

        Only a folder INSIDE the mirror is accepted. A session started by
        something else -- the CLI in a terminal, a different bridge -- can
        have any cwd at all, and resuming it must not point this bridge's
        agent at a directory the rest of the system knows nothing about.
        """
        base = self.sync_dir
        if not base:
            return ""
        for r in sesslist.listing():
            if r["id"] != session_id:
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

    def _retarget_perms(self, guest: GuestSession) -> None:
        """Point the permission desk at this conversation's folder.

        `off_limits` refuses a path tool reaching outside the working
        directory, and the desk was handed one at connect time and never told
        it had changed. Leaving it stale would refuse the conversation's OWN
        files while allowing the previous conversation's, which is the check
        inverted rather than merely wrong.
        """
        desk = getattr(guest, "perms", None)
        if desk is not None:
            desk._cwd = self._work_dir(guest)

    def _mirror_dir(self, guest: GuestSession) -> str:
        """The directory on THIS side that pairs with the guest's work folder,
        or "" while this conversation has not committed to one.

        NO FALLBACK, and that is the whole point. `_work_dir` falls back to
        `self.cwd` because the agent has to run somewhere; a mirror has no
        such obligation, and inventing one pairs this bridge's entire shared
        directory against whatever tree the guest happens to have open. That
        is not hypothetical: a restarted bridge, told by HELLO that the
        machine was mirroring ``C:\\Gaston\\OK``, offered its whole tree
        against it. Nothing is mirrored until both sides have been told the same
        folder.
        """
        return guest.work_dir or ""

    def _work_dir(self, guest: GuestSession) -> str:
        """Where the agent works for THIS conversation."""
        return guest.work_dir or self.cwd or os.getcwd()

    def _remember_settings(self, guest: GuestSession,
                           session_id: str = "") -> None:
        """Write this conversation's model, effort and mode to disk.

        Called after every change AND at the moment the conversation first
        gets an id, because a setting picked before the first message has
        nowhere to be written until then -- `conv_id` is empty for a chat
        that has been started but not spoken in. Cheap: three strings and an
        atomic replace, against a change the user made by hand.

        `session_id` names the conversation to write against, for the one
        caller that knows it before `conv_id` agrees: the SDK's `init`
        message carries the id in hand, and reading `guest.conv_id` there
        would be asking a property whose answer depends on which Session
        object happens to be attached at that instant.
        """
        conv = session_id or guest.conv_id
        if not conv:
            return
        self.conv.remember(conv,
                          model=guest.model,
                          effort=guest.effort,
                          mode=guest.perm_mode)

    def _restore_settings(self, guest: GuestSession, session_id: str) -> None:
        """Put back what this conversation was last set to.

        ONLY WHAT WAS RECORDED. A conversation with nothing saved -- one from
        before this existed, or one opened on another machine -- keeps
        whatever is currently selected rather than being reset to the
        process defaults, because "I have not chosen for this chat" and "I
        chose the default for this chat" are different and only the second
        one is written down.
        """
        saved = self.conv.settings(session_id)
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

    async def _ensure_work_dir(self, guest: GuestSession) -> None:
        """Commit this conversation to a folder, if it has not already.

        THE FIRST TOOL CALL IS THE COMMITMENT. Before it nothing is mirrored
        -- no folder exists, the guest's sync layer is pointed at nothing,
        and no pass runs -- because a chat that is only being talked to has
        no files to share and creating a directory for it is litter. At the
        first tool the folder is made, sharing turns on and the toolbar stops
        saying "<unset>".

        Idempotent and called from both the render path and the exec path,
        because which of the two arrives first is an ordering detail of the
        SDK rather than something to depend on.
        """
        if guest.work_guest:
            return
        await self._name_work_dir(guest)
        if guest.work_guest:
            await self._send_options(guest)

    async def _name_work_dir(self, guest: GuestSession) -> None:
        """Give this conversation its folder.

        NAMED FROM THE FIRST MESSAGE, not from the CLI's title. A folder is
        named after the conversation and the CLI does title a conversation
        from its first message -- but it does so at the end of the turn, and
        the folder is needed the moment a tool runs, which can be inside that
        same turn. The alternative was a placeholder renamed afterwards,
        which means a new `tree_id` and a new baseline for a tree sync has
        already scanned, and a window in which both sides have to move
        together. The title is the fallback, for a conversation resumed from
        the chat list that never got a folder.

        UNIQUE, by suffix. Two conversations about listing files both reduce
        to LISTFILE, and the second one taking the first one's folder would
        put two conversations' work in one place with no way to tell them
        apart afterwards.
        """
        if guest.work_guest or not guest.conv_id:
            return
        base = self.sync_dir
        if not base:
            return
        # No mirror on the guest means the folder would exist only here, and
        # the one thing it is for is telling the user where their files are.
        # Leave it unset and let the agent work in the shared root.
        if not self._guest_path(guest, "x"):
            return

        source = guest.first_prompt
        if not source:
            for r in sesslist.listing():
                if r["id"] == guest.conv_id:
                    source = r.get("title") or ""
                    break
        name = self._title_folder(source, guest.caps)
        if not name:
            return

        # Unique, and the suffix eats into the 8 characters rather than
        # extending past them -- the name has to stay 8.3 on the other side.
        taken = self.conv.taken()
        stem, n = name, 1
        while os.path.exists(os.path.join(base, name)) or name in taken:
            n += 1
            if n > 99:
                return
            tail = str(n)
            name = stem[:Bridge.folder_max(guest.caps) - len(tail)] + tail

        path = os.path.join(base, name)
        try:
            os.makedirs(path, exist_ok=True)
        except OSError:
            log.exception("could not create %s", path)
            return
        guest.work_dir = path
        guest.work_guest = self._guest_path(guest, name)
        self.conv.set_folder(guest.conv_id, name, guest.work_guest)
        self._retarget_perms(guest)
        log.info("work directory for %s: %s (%s)",
                 guest.conv_id, path, guest.work_guest)
        await self._write_block(guest, H.notice(
            "This conversation works in " + guest.work_guest
            + " on your machine."))
        # AND THE AGENT, which is the half that was missing. Its system
        # prompt was written before this folder existed and says so; without
        # this the only way it could learn the path was to call sync_status
        # and read it out of a status report, which is a tool call to
        # discover something we already knew.
        note = (f"This conversation's folder now exists: write in {path} "
                f"here, which is mirrored to {guest.work_guest} on the "
                f"user's machine. Anything written outside it stays on this "
                f"proxy.")
        if note not in self.synccoord.agent_notices:
            self.synccoord.agent_notices.append(note)

    @staticmethod
    def _sep_of(root: str) -> str:
        r"""The separator this machine spells its own paths with.

        Read off the root rather than declared, and that is the honest source:
        the guest SENT this string, so whatever it joins with is what it uses.
        `caps` has no separator field and adding one would ask the client to
        repeat something it has already demonstrated.

        Backslash only if the root actually contains one. This line used to be
        `root.rstrip("\\") + "\\" + name` unconditionally -- the one place the
        bridge spells a guest path, and it spelled it for exactly one family
        of machines while `paths.py` carefully kept "/" as the wire separator
        everywhere else.
        """
        return "\\" if "\\" in root else "/"

    def _guest_path(self, guest: GuestSession, name: str) -> str:
        r"""`<mirror root><sep><name>`, spelled the way the guest spells it."""
        root = (guest.hello or {}).get("sync_root") or self.guest_sync_root
        if not root:
            return ""
        if not name:
            return root
        sep = self._sep_of(root)
        return root.rstrip("\\/") + sep + name

    def guest_work_root(self, guest: GuestSession) -> str:
        r"""Where commands run, spelled the way the guest spells it.

        This conversation's own folder once it has one, and the mirror root
        until then -- a command has to run SOMEWHERE from the first message,
        and the root is where the agent is working at that point.
        """
        if guest.work_guest:
            return guest.work_guest
        return (guest.hello or {}).get("sync_root") or self.guest_sync_root

    def _sub_of(self, guest: GuestSession) -> str:
        """This conversation's folder name relative to the mirror, or ""."""
        if not guest.work_dir or not self.sync_dir:
            return ""
        try:
            rel = os.path.relpath(guest.work_dir, self.sync_dir)
        except ValueError:
            return ""
        return "" if rel in (".", "") or rel.startswith("..") else rel

    def _effective_effort(self, guest: GuestSession) -> str:
        """Effort's counterpart to `_effective_model` -- see there."""
        return guest.effort or self.effort or ""

    def _abandon_pending(self, guest: GuestSession) -> None:
        """Orphan everything the conversation being left behind still owes.

        A prompt belongs to the session that raised it, and so does a running
        EXEC. Leaving either pending against a conversation the user has
        walked away from is the unanswerable state `_reissue_halt` exists to
        prevent -- worse now than when this lived inside `_make_session`,
        because a switch no longer rebuilds anything and would otherwise
        clean up nothing at all. The guest hides its bar on the strength of
        the deny, so the deny has to happen whether or not a new client is
        built.
        """
        guest.perms.deny_all(
            "rebuild", "The conversation changed before this was answered.")
        # THE KILL COMES FIRST, and it is 02.9. `_fail_execs` resolves the
        # futures and clears the table; it sends nothing. So the command kept
        # running on the guest's single worker thread, and the first `exec`
        # in the NEW conversation was answered `busy` -- for a command the
        # model in that conversation never issued and cannot see. This is
        # what `_on_interrupt` already does, and for the reason it states
        # there: one abandoned turn must not poison the following one.
        for exec_id in list(guest.exec_pending):
            self._kill_exec(guest, exec_id)
        self._fail_execs(guest, "The conversation changed while this command "
                                "was running.")

    async def _resume_pending(self, guest: GuestSession) -> bool:
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
            guest.claude = await self._make_session(
                guest, resume=guest.pending_resume or None)
        except Exception as exc:
            log.exception("deferred session start failed")
            await self._write_block(guest, H.error(
                ("Could not reopen the conversation: " if resuming
                 else "Could not start a new session: ") + str(exc)[:160]))
            return False
        guest.pending_resume = ""
        # NO _remember_settings HERE, and the comment that used to say "the
        # conversation has an id at last" was wrong: `Session._session_id` is
        # set from the SDK's `init` message, which arrives during the first
        # TURN, and `pending_resume` has just been cleared on the line above
        # -- so `conv_id` is empty on both branches and the write was a
        # no-op every time. It lives in `on_system`'s init branch now, which
        # is the first instant an id exists.
        if not resuming:
            # The guest has been showing an empty id since New chat; tell it
            # which conversation this turned out to be, so the chat list can
            # mark it and a reconnect knows what to resume.
            await self._send(guest, switched(guest.claude.session_id or "",
                                             guest.pages.count))
        # The menus were filled from the cache while there was no client; now
        # there is one, and it is the thing that decides what `default`
        # resolves to.
        await self._send_options(guest)
        return True

    async def _make_session(self, guest: GuestSession,
                            resume: str | None = None,
                            fork: bool = False) -> Session:
        # The model list is whatever the CLI reported when the CLIENT
        # connected -- get_server_info() hands back the cached init result --
        # so a CLI upgraded under a running bridge cannot reach the menu
        # until a new client exists. This is that moment, and it is the only
        # one: re-fetching at any other point re-reads the same init result.
        self._models_cache = None
        self._commands_cache = None
        async def on_text(t: str) -> None:
            guest.turn_produced = True
            await self._emit_text(guest, t, style="plain")

        async def on_thinking(t: str) -> None:
            await self._emit_text(guest, t, style="thinking")

        async def on_tool(name: str, args: dict) -> None:
            guest.turn_produced = True
            # THE FIRST TOOL IS THE COMMITMENT: this conversation gets its
            # folder here, and file sharing begins. Before the block is
            # written, so the notice naming the folder comes first and the
            # tool line lands under it rather than above it.
            await self._ensure_work_dir(guest)
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
            elif subtype == "rate_limit":
                # Only "rejected" means anything was refused. The CLI emits
                # this on every status TRANSITION, including into "allowed",
                # and treating all of them as trouble is what put "the request
                # may not have run" under turns that ran perfectly -- and, on
                # an output-less turn, reported a completed one as failed.
                #
                # Deliberately NOT marking the turn degraded on a warning.
                # `allowed_warning` means the account is near a ceiling, which
                # is worth knowing and is not a reason to disbelieve an answer
                # the user can read on the same screen.
                status = str(data.get("status") or "")
                if status == "rejected":
                    guest.turn_degraded = "rate_limited"
                    await self._write_block(guest, H.notice(
                        "The model provider refused this request: the "
                        "account is over its rate limit. Nothing ran -- try "
                        "again shortly."))
            elif subtype == "conversation_reset":
                # /clear replaces the conversation id (session.py's
                # ConversationResetMessage branch), so the settings have to
                # follow it or the chat the user is now in has no row.
                self._remember_settings(
                    guest, str(data.get("new_conversation_id") or ""))
                await self._write_block(guest, H.notice("Conversation reset."))
            elif subtype == "sdk_event":
                # An SDK message the session layer does not handle. It used to
                # be a DEBUG line and nothing else, which meant a
                # RateLimitEvent ended the turn with the screen unchanged and
                # the user's prompt apparently ignored. Whatever it is, saying
                # so beats silence -- the turn may well produce nothing after
                # it, and an unexplained no-op is the worst possible answer.
                #
                # RateLimitEvent and ConversationResetMessage used to arrive
                # here; both have real branches in session.py now. What is
                # left is genuinely unknown, which is the only case where
                # "assume the worst" is the right reading.
                event = str(data.get("event", "")) or "an unknown event"
                guest.turn_degraded = event
                await self._write_block(guest, H.notice(
                    f"The agent reported {event}, which this bridge does "
                    f"not handle yet. If nothing follows, the request did "
                    f"not run."))
            elif subtype == "init":
                # The conversation id becomes known HERE, not at start().
                #
                # Session._session_id is only set from this message, which
                # arrives during the first turn. Refresh the chat list once
                # it does -- PLAN.md Phase 4 records the symptom ("the
                # transcript says the conversation was forked; the list does
                # not") from the time this used to be gated on `guest.sess_id`
                # changing: immediately after the effort fork guest.sess_id
                # was reassigned from guest.claude.session_id, which was
                # still None at that point, so the comparison saw no change
                # and never pushed. This fires once per Session object's
                # lifetime (the SDK's own first-turn system message), so no
                # dedup against a previous value is needed.
                #
                # guest.sess_id itself is NOT touched here, or anywhere a
                # session is rebuilt. It used to be re-pointed at whatever
                # session happened to be live -- "a bridge handle" on a fresh
                # connection, "the SDK session" after a new-session or a
                # fork, and simply stale after a switch, which never updated
                # it at all. Nothing reads it for anything but READY's header
                # and logging, which is exactly why it was free to drift.
                # It is now a stable per-connection handle, set once in
                # _attach and never overwritten -- the SDK's own id already
                # travels in SWITCHED and in this message, and R7's
                # per-client identity wants a handle that outlives every
                # session a client opens. docs/audit/02-bridge-python.md
                # 02.24.
                sid = str(data.get("session_id") or "")
                if sid:
                    # AND THIS IS WHERE THE SETTINGS LAND. Model, effort and
                    # mode are chosen against a conversation that does not
                    # have an id yet -- New chat has none until its first
                    # message, and the effort fork mints a new one on the
                    # turn after the fork -- so every write before this
                    # moment had nothing to be keyed on and silently did
                    # nothing. Back when these were three files, the
                    # settings one was never created at all on this machine
                    # while the folders one beside it filled up normally,
                    # because a folder is committed at the first TOOL CALL,
                    # by which time the id exists. That contrast is what
                    # made the bug legible, and why they are one store now.
                    #
                    # The consequence the user sees is the one the store was
                    # built to stop: open a chat you had set to Opus and Plan
                    # and you get whatever the last chat was using, because
                    # `_restore_settings` finds no row and deliberately keeps
                    # what is on screen.
                    #
                    # Every path arrives here -- new, resumed, forked and
                    # /clear -- which is why this is the only hook needed.
                    self._remember_settings(guest, sid)
                    self._spawn(self._send_sessions(guest))

        s = Session(
            cwd=self._work_dir(guest),
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
                # THIS CONVERSATION'S folder, which is usually empty here:
                # the prompt is built when the session is, and a folder is
                # committed at the first TOOL CALL. `guest_work_root` would
                # fall back to the configured root and name a mirror that
                # does not exist -- which is exactly what sent an agent off
                # to diagnose a sync fault instead of writing the file it
                # was asked for. `home` is what the folder will sit under,
                # so the prompt can describe what is about to happen.
                "append": machine.system_prompt(
                    guest.hello, self._work_dir(guest),
                    sync_dir=self._work_dir(guest) if self.sync_dir else None,
                    guest_root=guest.work_guest,
                    home=(guest.hello or {}).get("sync_root") or "",
                    # From HELLO, so it is fixed for the life of the
                    # connection and every conversation on it sees the same
                    # machine. Empty for any guest built before the body
                    # existed, which is every deployed one until it is
                    # reflashed -- and empty prints nothing at all.
                    tools=guest.tools, path=guest.path),
            },
            # The per-guest values, not the process defaults: the menu changes
            # these and a later reconnect or fork has to keep the choice.
            model=self._effective_model(guest),
            effort=self._effective_effort(guest),
            # Same reasoning: a rebuild (switch, new session, effort fork)
            # must not drop the user back into Manual without saying so.
            permission_mode=guest.perm_mode,
            resume=resume,
            fork=fork,
            # Every tool call the CLI does not resolve locally comes back
            # through here as a prompt on the guest's screen.
            can_use_tool=guest.perms.callback(),
            # The key here becomes the mcp__<key>__<tool> prefix.
            mcp_servers=machine_tools.build(self, guest),
            on_text=on_text,
            on_thinking=on_thinking,
            on_tool=on_tool,
            on_tool_result=on_tool_result,
            on_system=on_system,
        )
        await s.start()
        log.info("session in %s for guest machine:\n%s",
                 self._work_dir(guest), machine.describe(guest.hello))
        # The id is usually not known yet -- the SDK assigns it when the first
        # query goes out -- so this is a best effort and _run_turn registers it
        # again once it exists.
        self.conv.add(s.session_id)
        return s

    # --- permissions -------------------------------------------------------
    #
    # The prompt bar itself -- the can_use_tool callback, the queue, reconnect
    # recovery, and the two ways a prompt ends -- lives on `guest.perms`
    # (bridge/agent/permdesk.py) now. What is left here is what still
    # genuinely needs the whole Bridge: the chat list and the halt bar, which
    # are not part of the audit's "Prompts" group (docs/audit/02-bridge-python.md
    # 02.41).

    async def _send_sessions(self, guest: GuestSession) -> None:
        """The chat list. Only conversations this bridge started -- the rest
        belong to a machine with a different way of working.

        Extracted so the list can be pushed when the conversation id CHANGES
        as well as when the guest asks. The effort fork changes it, and until
        now nothing told the guest.
        """
        # NOT filtered by directory any more. Conversations live in their own
        # folders now, so `list_sessions(directory=self.cwd)` would list only
        # the ones sharing the default -- the chat list would lose a chat the
        # moment it was given a folder of its own. `Conversations.filter` was
        # always the real filter (only conversations this bridge started);
        # the directory was belt and braces and is now wrong.
        rows = self.conv.filter(sesslist.listing())
        await self._send(guest, sessions_start(len(rows)))
        for i, r in enumerate(rows):
            await self._send(guest, session_row(
                i, r["id"], r["title"], r["cwd"], r.get("group", ""),
                bool(guest.conv_id and guest.conv_id == r["id"])))
        await self._send(guest, sessions_end())

    async def _reissue_halt(self, guest: GuestSession) -> None:
        """After a reconnect, put the Continue bar back.

        The third leg of the same idea as `guest.perms.reissue()`, and the one
        whose
        absence was worst. `synccoord.halt_id` lives on the SyncCoordinator
        and survives a reconnect (same Bridge, same process); `latched` lives
        on disk. So a guest that reconnected while a halt was up came back
        with NO BAR, a bridge still holding the halt, and every latched path
        refusing the agent's writes -- unanswerable, because RESUME is only
        accepted against a matching id and raising is idempotent while one is
        outstanding. Nothing short of restarting the bridge or hand-editing
        the baseline would have recovered it.

        No new frame type: the guest's HALT handler is idempotent, so re-sending
        the same one restores the bar.
        """
        sc = self.synccoord
        if not sc.halt_id:
            return
        log.info("re-issuing halt %s after reconnect", sc.halt_id)
        await self._send(guest, halt(sc.halt_id, sc.halt_text))

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
        # Both halves, not one.
        #
        # This reset `turn_produced` alone, which meant a guest that blipped
        # AFTER the model had already streamed text came back looking like a
        # turn that had answered nothing -- and then any stale `turn_degraded`
        # turned that into a reported failure under a visible answer. The
        # verdict needs the two fields to agree about which turn they
        # describe, and the only honest answer here is "we stopped watching",
        # which is what clearing both says.
        guest.turn_produced = False
        guest.turn_degraded = ""
        await self._send(guest, turn_start())

    def _spawn(self, coro) -> None:
        """Fire-and-forget, with a reference kept AND the result looked at.

        asyncio only holds a weak reference to a running task, so a bare
        create_task can be garbage-collected mid-flight.

        Everything fire-and-forget on this bridge goes through here: turns,
        the Continue turn, sync passes, EXECKILL, the session list on `init`,
        `_clear_halt` from `sync_resolve`. Nothing used to retrieve the
        exception, so a raise inside any of them produced NO log line at the
        moment it happened -- it surfaced later, if at all, as asyncio's
        "Task exception was never retrieved" whenever the task object was
        collected. A turn dying mid-flight is not a quiet event: it is a
        TURN end frame that never goes out, and a guest whose Send button
        stays disabled until the link is re-established. See _done below.
        """
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._done)

    def _done(self, task: "asyncio.Task") -> None:
        """Drop a finished task and say so if it raised.

        `CancelledError` is not a failure: `close()` cancels these
        deliberately, and so does the SDK on an interrupt.
        """
        self._tasks.discard(task)
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            log.error("background task failed", exc_info=exc)

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
                           *, stream: bool = True,
                           notify: bool = True) -> None:
        """Record a finished block into the paginator, then send it.

        `stream=False` records without sending the APPEND, for a block the
        guest has already put on screen itself -- the echo of the user's own
        line. It still has to reach the paginator, because a sealed page is
        re-sent in full when the reader pages back, and a transcript of
        answers with no questions in it is close to useless.

        `notify=False` additionally withholds PAGESEALED, and exists for ONE
        caller: replaying a conversation's history in `_switch` (02.39).

        The two flags are separate because the echo case genuinely needs the
        seal notification -- the user's own message can be what fills a page,
        and the guest's page counter would go stale without it. History replay
        is the opposite: the guest has not been told it is looking at this
        conversation yet, SWITCHED carries a fresh total immediately
        afterwards, and `_send_page` then puts the live page on screen. Seals
        announced during that loop describe a session the guest does not think
        it is viewing, which is why routing replay through here was twice
        looked at and twice backed out before the flag existed.
        """
        if not html:
            return

        page_no = guest.pages.live.number
        sealed, chunks = guest.pages.write("html", html)

        # Only stream when the guest is watching the live page. In HISTORY mode
        # the bridge keeps buffering silently and the page counter ticking up
        # is the notification.
        #
        # A run this long can split across a seal (pages.write above), so only
        # the piece that actually landed on page_no belongs in this APPEND --
        # sending the whole block under page_no double-renders the tail once
        # the reader pages forward to where pages.write actually put it
        # (02.33). The tail reaches the guest when it is paged to.
        if stream and (guest.view_page is None or guest.view_page == page_no):
            on_this_page = "".join(text for pg, text in chunks if pg == page_no)
            if on_this_page:
                await self._send_html(guest, on_this_page, APPEND, page_no)

        if notify:
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
        name = self._DISPATCH.get(f.type)
        if name is None:
            log.warning("unknown frame type from guest: %s", f.type)
            await self._send(guest, error("unknown_type", f.type))
            return
        await getattr(self, name)(guest, f)

    async def _on_input(self, guest: GuestSession, f: Frame) -> None:
        text = fold.decode(f.body)
        log.info("input: %s", text[:120])
        # The first thing said in a conversation is what its folder is named
        # after, and by the time a tool runs the SDK has not titled the
        # conversation yet. Recorded only while the folder is still open --
        # once committed, the name is the name.
        if not guest.work_guest and not guest.first_prompt:
            guest.first_prompt = text
        self._spawn(self._run_turn(guest, text))

    async def _on_resume(self, guest: GuestSession, f: Frame) -> None:
        # The user pressed Continue on the halt bar. THAT is the signal the
        # design has been waiting for -- not an INPUT, and not a timer.
        #
        # "The agent stops until the user confirms they have finished with
        # the file" needs a confirmation, and until now the only inbound
        # channels were INPUT and PERMREPLY. Reading it off the next INPUT
        # was worse than useless: the most natural reaction to the notice
        # is to type "wait, stop", which is precisely the message that
        # would have restarted it.
        sc = self.synccoord
        if not (f.headers.get("id", "") == sc.halt_id and sc.halt_id):
            return
        hid, sc.halt_id = sc.halt_id, ""
        log.info("user pressed Continue on the sync halt")
        # Acknowledge, because the guest no longer takes its own bar
        # down on the click. A press that never reached us used to look
        # answered on their screen while we still held the halt -- and
        # since raising is idempotent, no later bar could appear and
        # every latched path refused the agent for good.
        await self._send(guest, haltend(hid))
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
        answered, sc.halt_paths = sc.halt_paths, frozenset()
        self._spawn(self._resume_turn(guest, answered))

    async def _on_syncdirty(self, guest: GuestSession, f: Frame) -> None:
        # The guest noticed its tree move. It never reconciles anything
        # itself -- the bridge owns the baseline and decides.
        self._spawn(self.run_sync("the guest reported a change"))

    async def _on_interrupt(self, guest: GuestSession, f: Frame) -> None:
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

    async def _on_getsessions(self, guest: GuestSession, f: Frame) -> None:
        await self._send_sessions(guest)

    async def _on_switch(self, guest: GuestSession, f: Frame) -> None:
        await self._switch(guest, f.headers.get("id", ""))

    async def _on_getpage(self, guest: GuestSession, f: Frame) -> None:
        await self._send_page(guest, f.headers.get("page", ""))

    async def _on_stat(self, guest: GuestSession, f: Frame) -> None:
        # The guest measures its own throughput for the progress UI; the
        # sender uses it to pick a codec. This is what makes one build work
        # on both a 10Mbit LAN and a modem.
        try:
            bps = float(f.headers.get("bps", "0"))
        except ValueError:
            bps = 0.0
        if bps > 0:
            guest.set_link_speed(bps)
            log.info("guest reports %.0f B/s", bps)

    async def _on_ping(self, guest: GuestSession, f: Frame) -> None:
        await self._send(guest, pong())

    async def _on_newsession(self, guest: GuestSession, f: Frame) -> None:
        await self._new_session(guest, f.headers.get("folder", ""))

    async def _on_setmodel(self, guest: GuestSession, f: Frame) -> None:
        model = f.headers.get("model", "")
        # RECORDED whether or not a client exists. A conversation opened from
        # the chat list has none until the first message, and picking a model
        # there used to be a silent no-op -- the menu re-ticked the old one
        # and nothing said why. `_make_session` reads `guest.model`, so the
        # choice is applied when the client is finally built.
        was = guest.model
        guest.model = model
        self._remember_settings(guest)
        log.info("model -> %s", model or "(default)")
        if guest.claude:
            # WRAPPED, like its two siblings. `_set_mode` and `_set_effort`
            # both catch and report; this was the one control path that let
            # the exception out -- through _handle, through _pump, into
            # _on_client's catch-all, and the connection went down.
            #
            # Not hypothetical, and the cause is measured a few hundred lines
            # below in _new_session's own comment: the CLI refuses control
            # requests against a client that exists but has never run a turn,
            # and `_attach` still builds exactly such a session eagerly on a
            # fresh connection. So opening the Model menu before typing
            # anything dropped the link -- and since the reconnect built
            # another never-run session, the next press did it again.
            try:
                await guest.claude.set_model(model)
            except Exception as exc:
                log.exception("model change failed")
                # PUT BACK, so the menu re-ticks what is actually in force
                # and the store does not carry a model the CLI
                # refused into the next session built.
                guest.model = was
                self._remember_settings(guest)
                await self._write_block(guest, H.error(
                    "Could not change the model: " + str(exc)[:160]))
                await self._send_options(guest)
                return
        await self._write_block(guest, H.notice("Model set to " + model))
        await self._send_options(guest)

    async def _on_seteffort(self, guest: GuestSession, f: Frame) -> None:
        await self._set_effort(guest, f.headers.get("effort", ""))

    async def _on_setmode(self, guest: GuestSession, f: Frame) -> None:
        await self._set_mode(guest, f.headers.get("mode", ""))

    async def _on_rules(self, guest: GuestSession, f: Frame) -> None:
        """What can run without asking, as a transcript block.

        A block rather than a dialog: it is prose with a table in it, the
        guest already renders both, and a dialog on a 640x480 screen would
        need scrolling it does not have. It also belongs in the transcript --
        the answer depends on the mode, and the mode changes.
        """
        tools = list(TOOLS) + ["mcp__machine__exec"]
        root = (guest.hello or {}).get("sync_root", "") or ""
        await self._write_block(guest, perms.rules_view(
            guest.perm_mode, tools, root))

    async def _on_setwork(self, guest: GuestSession, f: Frame) -> None:
        """The user picked a folder on their machine for this conversation.

        A PROPOSAL, recorded here and sent back in the options -- the guest
        applies what it is told rather than what it picked, so the two sides
        cannot end up disagreeing about which tree is mirrored.

        The path is the GUEST'S and is never touched with os.path: it is a
        DOS path and this process is on Linux, where `os.path.basename`
        of `C:\\Gaston\\NOTES` is the whole string. That mistake is what
        bridge/files/paths.py exists to have stopped happening.
        """
        path = (f.headers.get("path") or "").strip()
        if not path:
            return
        if not guest.conv_id:
            await self._write_block(guest, H.error(
                "Send a message first -- a work directory belongs to a "
                "conversation, and this one has not started yet."))
            return

        # The bridge-side half is still a folder under --sync-dir, because
        # that is the tree this process can actually write to. What changed is
        # the GUEST'S half, which is the one the user chose.
        name = self.folder_name(
            path.replace("\\", "/").rsplit("/", 1)[-1], guest.caps)
        if not name:
            name = self.folder_name(guest.conv_id[:8], guest.caps) or "WORK"
        base = self.sync_dir
        if not base:
            await self._write_block(guest, H.error(
                "This bridge has no mirrored folder, so a work directory "
                "would have nothing on this side to pair with."))
            return
        local = os.path.join(base, name)
        try:
            os.makedirs(local, exist_ok=True)
        except OSError as exc:
            log.exception("could not create %s", local)
            await self._write_block(guest, H.error(
                "Could not create the folder on this side: " + str(exc)[:160]))
            return

        guest.work_dir = local
        guest.work_guest = path
        self.conv.set_folder(guest.conv_id, name, path)
        self._retarget_perms(guest)
        log.info("work directory for %s: %s (guest: %s)",
                 guest.conv_id, local, path)
        await self._write_block(guest, H.notice(
            "This conversation works in " + path + " on your machine."))
        # AND THE AGENT. It had no idea this had happened: the notice above
        # goes to the transcript, `_send_options` goes to the toolbar, and
        # the system prompt was written before either. So the agent went on
        # writing into the folder it was told about when the session was
        # built -- which is no longer mirrored anywhere.
        #
        # Through agent_notices, which rides in on the next turn's preamble.
        # That is the right moment and the only one: nothing reaches a
        # session except an INPUT, and manufacturing a turn to deliver a
        # directory change would bill the user for an announcement.
        note = (f"THE WORK DIRECTORY HAS CHANGED. This conversation now "
                f"works in {local} here, mirrored to {path} on the user's "
                f"machine. The folder you were using before is no longer "
                f"mirrored -- anything written there from now on stays on "
                f"this proxy, however well the write appears to succeed.")
        if f.headers.get("staged") == "merge":
            # The user picked "let the assistant merge them" in the dialog,
            # which is an instruction to the assistant. Without this the
            # copies land in Gaston-Incoming and nothing ever asks anyone to
            # reconcile them, so that answer did half of what its label said.
            note += (
                f"\n\nThe user asked YOU to merge the files they brought "
                f"across. Copies whose names clashed are in "
                f"{os.path.join(local, 'Gaston-Incoming')}; the versions that "
                f"were already here are in place beside it. Reconcile them "
                f"file by file, say what you changed, and ask before "
                f"discarding either side. Do not delete anything in "
                f"Gaston-Incoming until the user agrees the merge is right.")
        if note not in self.synccoord.agent_notices:
            self.synccoord.agent_notices.append(note)
        await self._send_options(guest)

    async def _on_export(self, guest: GuestSession, f: Frame) -> None:
        """Assemble the whole conversation into one file and stream it.

        Assembled HERE and not on the guest, like everything else: the guest
        has tens of kilobytes free and a transcript is hundreds. It never
        holds the document -- each chunk goes straight from the socket to a
        file -- which is the same rule `do_put` already follows for sync.
        """
        title = self._conv_title(guest)
        when = _dt.datetime.now().strftime("%Y-%m-%d %H:%M")
        doc = H.export_document(title, guest.pages.all_text(),
                                "Exported from Gaston Code, " + when)
        # CP1252 on the way out, because the file is read on the machine that
        # saved it, by a browser that will be told windows-1252 in the meta
        # tag the document carries.
        data = fold.encode(doc)
        log.info("export: %d bytes for %r", len(data), title)
        await guest.link.send_export(guest, data)

    def _conv_title(self, guest: GuestSession) -> str:
        """What this conversation is called, for the export's heading.

        From the chat list rather than from anything the bridge invents: the
        title the user is looking at in the list is the one they expect on the
        file. Falls back to the id, and then to a flat noun, because an
        untitled conversation is still exportable.
        """
        cid = guest.conv_id
        if cid:
            for r in sesslist.listing():
                if r["id"] == cid:
                    return r["title"]
        return cid or "Conversation"

    #: One entry per inbound frame type, built once at class definition
    #: time -- 02.30: this used to be a 17-branch if/elif mixing one-line
    #: handlers (PING, STAT) with 25-line ones (RESUME, GETSESSIONS,
    #: INTERRUPT), and the previous audit pass named it the lowest-risk
    #: place to start: mechanical, no state of its own. Values are method
    #: NAMES rather than the bound methods themselves, and resolved with
    #: `getattr` in `_handle` -- `_sync_frame`/`_exec_out`/`_exec_result`/
    #: `_perm_reply` are defined later in this class body, so a dict of the
    #: bare names built here, at THIS point in class-body execution, would
    #: raise NameError before the class even finishes being defined. Every
    #: handler takes `(self, guest, f)`, including the six sync reply types
    #: and the two exec ones, which already had that shape and are reused
    #: as-is rather than wrapped. `_handle` becomes four lines plus the
    #: unknown-type fallback; `_perm_reply` (02.41) is now a one-line forward
    #: onto `guest.perms.reply`, kept as a named entry here rather than
    #: replaced by the desk directly registering into this dict, since
    #: nothing else in `_DISPATCH` holds anything but a bound Bridge method
    #: name. `ExecChannel`/`SyncCoordinator` (02.30's other two extractions)
    #: are unowned by any pass so far and can register the same way when they
    #: land.
    _DISPATCH: dict = {
        "INPUT": "_on_input",
        "RESUME": "_on_resume",
        "SYNCDIRTY": "_on_syncdirty",
        "SYNCENT": "_sync_frame",
        "SYNCEND": "_sync_frame",
        "SYNCPUTACK": "_sync_frame",
        "SYNCMKDIRACK": "_sync_frame",
        "SYNCDAT": "_sync_frame",
        "SYNCDATEND": "_sync_frame",
        "EXECOUT": "_exec_out",
        "EXECRESULT": "_exec_result",
        "PERMREPLY": "_perm_reply",
        "INTERRUPT": "_on_interrupt",
        "GETSESSIONS": "_on_getsessions",
        "SWITCH": "_on_switch",
        "GETPAGE": "_on_getpage",
        "STAT": "_on_stat",
        "PING": "_on_ping",
        "NEWSESSION": "_on_newsession",
        "SETMODEL": "_on_setmodel",
        "SETEFFORT": "_on_seteffort",
        "SETMODE": "_on_setmode",
        "EXPORT": "_on_export",
        "RULES": "_on_rules",
        "SETWORK": "_on_setwork",
    }

    def _seed_agent_notices(self) -> None:
        """Fold whatever the baseline still owes the agent into memory.

        `synccoord.agent_notices` is a Bridge-lifetime cache of
        `base.pending_notice_agent` (02.20): the durable queue is the source
        of truth, and this is what makes a bridge that just restarted catch
        up on a notice a PREVIOUS process wrote and delivered to the user's
        screen, but never got to hand to the model before it stopped. Called
        at every attach rather than once at startup, so it also catches a
        notice written by a pass that ran while nothing was connected --
        merged, not replaced, so it cannot drop one already waiting in
        memory for a turn that has not started yet.
        """
        try:
            base = self._load_baseline()
        except Exception:
            return
        for text in base.pending_notice_agent:
            if text not in self.synccoord.agent_notices:
                self.synccoord.agent_notices.append(text)

    async def _with_sync_preamble(self, text: str) -> str:
        """Prepend anything sync needs the agent to know, and mark it delivered.

        Marked delivered when it is handed to the model, not when the model
        acts on it -- there is no signal for the latter, and re-sending on every
        turn until the agent happens to mention it would bury the next one.

        The in-memory queue is cleared first, synchronously, so a notice
        raised while the durable clear below is in flight is not lost (the
        same reason `notices_delivered` clears by value rather than by
        emptying the list). The disk clear is best-effort: a crash in the
        narrow window between the model seeing these lines and the save below
        means they are handed over once more after a restart, which is the
        safe direction to err -- see 02.20.
        """
        if not self.sync_dir or not self.synccoord.agent_notices:
            return text
        pending, self.synccoord.agent_notices = self.synccoord.agent_notices, []
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
        await self._clear_agent_notices(pending)
        return "\n".join(lines) + text

    async def _clear_agent_notices(self, delivered: list) -> None:
        """Durable half of `_with_sync_preamble` -- see there and 02.20."""
        if not delivered or not self.sync_dir:
            return
        async with self._baseline_lock():
            try:
                base = self._load_baseline()
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
                base.save(self._base_path())
            except Exception as exc:      # pragma: no cover - disk failure
                log.warning("could not record delivered agent notices: %s",
                           exc)

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
        # The first message is what starts the process -- for a chat opened
        # from the list AND for one started with New chat, neither of which
        # spawns anything by itself. Before the lock, because building it is
        # slow and holding the turn lock across a subprocess start would make
        # a second message queue behind a client that does not exist yet.
        #
        # Unconditional now. It used to run only when `pending_resume` was
        # set, so once New chat stopped spawning eagerly the first message
        # into a new conversation would have fallen through to the `return`
        # below and vanished without a word.
        if not guest.claude:
            if not await self._resume_pending(guest):
                return
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
        # Cleared at the START of a turn, not only at its end.
        #
        # `turn_degraded` used to be cleared only after a verdict was taken,
        # so a turn whose `send()` RAISED returned early (see the except
        # below) and left the event name set -- and the NEXT turn, which had
        # nothing to do with it, was judged against it.
        guest.turn_produced = False
        guest.turn_degraded = ""
        await self._send(guest, turn_start())
        # Captured BEFORE _with_sync_preamble rewrites `text`: the preamble is
        # prepended, so the leading "/" this is testing for would no longer be
        # leading by the time the turn ends.
        was_command = text.lstrip().startswith("/")
        # Anything sync found while the agent was idle rides in on this turn.
        #
        # `_run_turn` is the only path into a session, and it fires only on
        # INPUT. A conflict found by the timer between turns therefore reaches
        # the USER's screen -- which is right, they are the one who can act on
        # it -- and reaches the agent nowhere at all. So it would never read
        # the preserved copy, never merge, and never call sync_resolve, and the
        # path would stay latched for the rest of the session with the agent
        # quietly unable to write to it and no idea why.
        #
        # NOT ONTO A SLASH COMMAND, and that is 02.7. The preamble is
        # PREPENDED, so `/compact` arrives at the CLI as twenty lines of
        # prose with `/compact` on the end -- which is not a slash command at
        # all. The context was not compacted, nothing said so, and the
        # "finished without sending anything back" notice below did not fire
        # either, because the model had dutifully answered the preamble. The
        # comment above already noticed this ordering for the VERDICT and
        # fixed that; nothing protected the command itself.
        #
        # HELD rather than sent separately: the queue is drained only when it
        # is delivered, so leaving it alone here costs one turn of delay and
        # matches the "say it once, on the next turn" discipline the notices
        # already follow. A command turn is also the one turn where the user
        # is not asking the model to do anything with files.
        if not was_command:
            text = await self._with_sync_preamble(text)
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
        #
        # GUARDED the way the `context_usage()` call eleven lines below
        # already is, and for the same reason: `guest.claude` can be None by
        # the time a turn comes back. The turn lock now stops the session
        # being swapped mid-turn (02.1), but this line is inside a coroutine
        # that can be resumed after an interrupt, and the tell that the guard
        # belongs here was that its neighbour has always had one.
        if guest.claude:
            self.conv.add(guest.claude.session_id)
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
        # The SDK's own verdict, unless it told us something during the turn
        # that contradicts it -- AND the turn has nothing to show for itself.
        #
        # Both halves are needed and the second was learned the hard way. A
        # rate-limited turn can come back ok=True having produced nothing, and
        # reporting that as a success puts "done" in the status bar directly
        # beneath a notice saying the request may not have run. But the SAME
        # event also arrives on a turn the SDK then RETRIES AND COMPLETES --
        # measured on the guest: the notice appeared, the tool ran, the answer
        # rendered, and the status bar still said "failed". That is the same
        # contradiction wearing the opposite sign, and it is worse, because a
        # user who sees a full answer under the word "failed" learns to
        # disbelieve the status bar.
        #
        # `turn_produced` is set only from the SDK's own callbacks, so a notice
        # the bridge wrote does not count as the model having answered.
        degraded = guest.turn_degraded
        guest.turn_degraded = ""
        ok, subtype = turn_verdict(turn.ok, degraded, guest.turn_produced,
                                   turn.subtype)
        # A slash command that answered nothing.
        #
        # The CLI resolves some commands entirely locally and sends no message
        # back at all. Measured on the guest: /recap and /usage each cost a
        # turn and left the transcript blank, and a /compact that declines
        # looks exactly like one that ran. Those two are off the Commands menu
        # now (COMMAND_MENU), but a command can also be TYPED, and the install
        # decides which ones exist -- so the silence is worth naming wherever
        # it happens. "Nothing happened" and "it worked" must not look alike.
        if ok and was_command and not guest.turn_produced:
            await self._write_block(guest, H.notice(
                "That command finished without sending anything back. Some "
                "commands only affect the session itself."))
        # How full the context window is, asked at the one moment it has
        # just changed and the guest is already listening. A live control
        # request, NOT ResultMessage.model_usage -- that is cumulative over
        # the turn and would read past 100% on any multi-tool turn. See
        # Session.context_usage.
        ctx_pct = await guest.claude.context_usage() if guest.claude else None
        await self._send(guest, turn_end(ok, subtype,
                                         turn.cost_usd, turn.duration_ms,
                                         ctx_pct))
        # The folder is NOT committed here. It is committed at the first tool
        # call (_ensure_work_dir), which is the first moment anything needs
        # one -- a conversation that is only being talked to shares no files
        # at all, and a turn that ran no tool has nothing to have shared.

    # --- running commands on the guest -------------------------------------

    async def run_on_guest(self, command: str, *, timeout_ms: int,
                           shell: str, cwd: str,
                           guest: "GuestSession | None" = None) -> dict:
        """Body of mcp__machine__exec. Returns an MCP tool result.

        `shell` NAMES one of the machine's shells, or is `"none"` to spawn
        the program directly, or is empty for the first shell the machine
        reported. It used to be a bool, which could only ever mean "the shell
        somebody else picked" -- and the somebody was the guest, choosing
        between `command.com` and `cmd.exe` from `GetVersion()` and telling
        nobody. A machine has as many shells as it has, so the machine
        reports the list, the system prompt prints it, and the model names
        one here.

        `guest` is the session this tool belongs to. It defaults to the only
        one there can be today, but passing it is what makes the tool address
        A MACHINE rather than whichever machine happens to be attached when
        the model calls it: the MCP closure captured the Bridge, so with two
        clients this would run a command on the WRONG machine, with the right
        person's approval on their own screen. Three call sites today, eleven
        after Phase 6.
        """
        guest = guest or self._guest
        if guest is not None:
            # Same commitment as `on_tool`, from the other side. Which of the
            # two the SDK reaches first is its business; a command must not
            # run in a folder that does not exist yet either way.
            await self._ensure_work_dir(guest)
        if guest is None or not guest.connected:
            # Structured error, never a hang. This is the cost of the
            # single-binary design and it was accepted on condition it is
            # handled here rather than left to the MCP timeout.
            return {"content": [{"type": "text", "text":
                    "The connected machine is not reachable right now -- it is "
                    "not connected to this proxy. Nothing was run."}],
                    "isError": True}

        # RESOLVED HERE, against what THIS machine reported, and refused by
        # name rather than silently falling back -- a command that was meant
        # for one shell and ran under another is not a smaller version of
        # what was asked for.
        prefix = ""
        want = (shell or "").strip()
        if want.lower() != "none":
            usable = toolrules.usable_shells(guest.tools)
            if want:
                prefix = toolrules.shell_command(guest.tools, want) or ""
                if not prefix:
                    have = ", ".join(f.name for f in usable)
                    return {"content": [{"type": "text", "text": (
                        f"There is no shell called {want!r} that this bridge "
                        f"can run commands through on that machine. "
                        + (f"It has: {have}." if have else
                           "It reported no shell this bridge knows how to "
                           "drive.")
                        + " Nothing was run.")}],
                        "isError": True}
            elif usable:
                prefix = toolrules.shell_command(guest.tools,
                                                 usable[0].name) or ""

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
            guest.exec_pending[exec_id] = ExecCall(future=fut)

            # Refuse a command the wire would REWRITE.
            #
            # Header values and, until this was fixed, the EXEC body went
            # through fold -- the display transliterator -- which turns
            # U+00B7 into '.', U+2502 into '|', U+00BB into '>>' and U+2044
            # into '/'. So a command could be inspected in one form and
            # executed in another: `del c:\work\<U+00B7><U+00B7>\..` arrives
            # at the guest as `del c:\work\..\..`, with a path separator and
            # a parent-directory hop invented between the two.
            #
            # exec_cmd now raises instead of folding, and `cwd` is checked
            # here because it travels as a header and _quote still folds --
            # correctly, for every other header, which carries prose.
            #
            # Refusing rather than stripping: the model did not type these by
            # accident, and a command with characters removed is no more the
            # command it wrote than one with characters added.
            for label, value in (("command", command), ("cwd", cwd)):
                bad = fold.unrepresentable(value)
                if bad is None:
                    continue
                return {"content": [{"type": "text", "text": (
                    f"That {label} contains {bad!r} (U+{ord(bad):04X}), which "
                    f"the machine's codepage cannot represent. Nothing was "
                    f"run. Rewrite it using plain ASCII -- do not assume a "
                    f"lookalike character will be understood, because the "
                    f"machine will read whatever byte it is given.")}],
                    "isError": True}

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
            # Nothing re-sends an EXEC on reconnect, and nothing must: a
            # redelivered EXEC would RE-RUN the command -- `del c:\temp\*`
            # twice is not a redraw. There is no wire replay any more (the
            # buffer that word used to mean is retired, docs/BACKLOG.md #5),
            # so the only re-delivery mechanism left is the explicit reissue
            # path after a reconnect (_reissue_halt, _reissue_turn,
            # PermissionDesk.reissue) -- and none of those touch
            # exec_pending or send an EXEC/EXECKILL. See
            # test_exec_is_never_re_delivered.
            await self._send(guest, exec_cmd(exec_id, command,
                                             timeout_ms=timeout_ms,
                                             shell=prefix, cwd=cwd))
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
            if entry.killed:
                return
            entry.killed = True
        log.info("asking the guest to kill exec %s", exec_id)
        self._spawn(self._send(guest, exec_kill(exec_id)))

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
        # NOTHING IS MIRRORED UNTIL A CONVERSATION COMMITS TO A FOLDER, so
        # there is no pass to run. A chat that is only being talked to shares
        # no files: sharing begins at the first tool call and not before.
        # Without this a pass would pair this process's whole shared
        # directory against whatever the guest had open, which is how a
        # restarted bridge came to push four conversations' folders at one
        # conversation's tree.
        if not self._mirror_dir(guest):
            log.debug("nothing mirrored yet; skipping sync (%s)", why)
            return {}
        if guest.sync_lock.locked():
            log.debug("sync already running; coalescing (%s)", why)
            return {}

        async with guest.sync_lock:
            guest.sync_active = True
            try:
                from .files.syncrun import Pass
                # 02.19: Pass takes the transport and the caps, nothing else
                # -- not this Bridge, not the whole GuestSession.
                transport = SyncTransport(guest)
                # THE CONVERSATION'S FOLDER, not the whole tree.
                #
                # One mirror per conversation means both ends narrow, not
                # one: the guest re-points its sync layer at the folder the
                # chat works in, and if this side kept offering
                # `~/gaston-work` against the guest's `C:\Gaston\OK` it would
                # push every other conversation's files into it.
                p = Pass(transport, guest.caps, self._mirror_dir(guest),
                         self._base_path())
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
                # get it: nothing reaches a session except an INPUT. Merged
                # rather than replaced or matched 1:1 against the user's set
                # above: pending_notice_agent is its OWN durable queue
                # (02.20), so it can carry an entry the user's copy already
                # dropped (delivered last pass, bridge restarted before the
                # agent's next turn) -- and a `not in` merge cannot drop one
                # already waiting in memory for a turn that has not started.
                for note in got.get("agent_notices", []):
                    if note not in self.synccoord.agent_notices:
                        self.synccoord.agent_notices.append(note)

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
                # Covers the same gap _reconcile_halt closes at attach, from
                # the other direction: latched paths this PASS did not just
                # raise for (a standing conflict from before a bridge
                # restart, discovered on the first pass a fresh process
                # runs) still need a bar, and one that WAS raised needs to
                # come down once nothing is latched any more.
                await self._reconcile_halt(guest, base=p.base)
                # Only after they are on the wire. The queue is pending work,
                # not a log: leaving them on it re-delivered every notice on
                # every pass, which buries the next real one.
                p.notices_delivered(delivered)
                self._transient_aborts = 0
                return got
            except SyncError as exc:
                # A DELIBERATE refusal: a tree_id mismatch, a changed guest
                # root, a manifest that arrived short, or too many paths
                # going backwards at once (a tree restored from a backup).
                # The whole pass stops, which is right -- and used to be a log warning and
                # nothing else, so sync could be dead for an hour with the
                # agent and the user both believing it was working.
                #
                # EXCEPT WHILE THE TWO SIDES ARE MID-HANDOVER. `no_root` means
                # the machine has no root at this instant, which is what every
                # work directory change looks like for about a second -- and
                # announcing it wrote a matched "synchronisation has stopped"
                # and "it is working again" into the transcript every time. It
                # still becomes news if it keeps happening, because then it is
                # not a handover, it is a folder that cannot be made.
                if getattr(exc, "transient", False):
                    self._transient_aborts += 1
                    if self._transient_aborts <= self.TRANSIENT_ABORT_GRACE:
                        log.info("sync: %s (retrying; %d of %d)", exc,
                                 self._transient_aborts,
                                 self.TRANSIENT_ABORT_GRACE)
                        return {"retry": str(exc)}
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
        state = self._base_path(guest)
        agent_text = (
            f"SYNC HAS STOPPED -- {reason} No file is being copied in either "
            f"direction until this is resolved. Do not tell the user a file "
            f"has reached their machine; it has not.")
        try:
            base = self._load_baseline(guest)
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
            # Durable, same as a Pass.note() (02.20): a bridge restart
            # between this and the agent's next turn must not silently lose
            # the one notice that tells it sync has stopped entirely.
            base.pending_notice_agent.append(agent_text)
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
        if agent_text not in self.synccoord.agent_notices:
            self.synccoord.agent_notices.append(agent_text)

    #: How many passes in a row may be refused for a self-fixing reason
    #: before it is announced anyway. Passes run about every thirty seconds
    #: and a handover resolves in one, so three is a minute and a half of
    #: patience -- long enough that no change announces itself, short enough
    #: that a root which genuinely cannot be made is still reported while the
    #: user remembers choosing it.
    TRANSIENT_ABORT_GRACE = 3

    def _base_path(self, guest: "GuestSession | None" = None) -> str:
        """The baseline file for the root `guest` is currently mirroring.

        `guest` IS NOW THREADED THROUGH, and 02.18 is why. `sync_status_text`
        and `sync_resolve` both already took a guest -- `tools.py` explains
        that the parameter is what makes an MCP tool address A MACHINE rather
        than whichever machine happens to be attached -- and then both called
        down into here, which read `self._guest` directly and undid it. With
        one guest that is invisible; the moment R7 lands, agent A's
        `sync_resolve` writes its release into whichever baseline
        `self._guest.work_guest` currently names, and the halt bar comes down
        on someone else's screen. Doing it now, while there is one guest and
        the tests are cheap, is the whole reason `run_on_guest` already takes
        the parameter.

        ONE BASELINE PER ROOT. A baseline records which files existed where,
        and it is bound to a `tree_id` and a `guest_root` -- change the root
        and `scan_guest` refuses, correctly, because treating a different
        directory as the same one would conflict the whole tree. That refusal
        is right and it is also what the user hits the moment they use
        "Change work directory...", which is not a corner case, it is the
        feature.

        So a root gets its own file instead of fighting over one. Nothing is
        migrated and nothing is deleted: the legacy `base.json` keeps
        belonging to whichever root it already records, so switching back to
        that root finds its history exactly as it left it.
        """
        legacy = os.path.join(self.sync_state, "base.json")
        root = self._current_guest_root(guest)
        if not root:
            return legacy
        # getattr, because sync_status_text is reachable on a Bridge built
        # with __new__ in the tests -- the same reason _current_guest_root
        # below guards _guest.
        cache = getattr(self, "_base_paths", None)
        if cache is None:
            cache = self._base_paths = {}
        hit = cache.get(root)
        if hit:
            return hit

        recorded = None
        try:
            with open(legacy, encoding="utf-8") as fh:
                recorded = json.load(fh).get("guest_root", "")
        except (OSError, ValueError):
            recorded = None

        if recorded is None or recorded == "" or recorded == root:
            # No legacy baseline, or it is already this root's.
            path = legacy
        else:
            import hashlib
            slug = hashlib.sha1(root.encode("utf-8", "replace")).hexdigest()
            path = os.path.join(self.sync_state, "base-%s.json" % slug[:10])
            log.info("baseline for %s is %s (%s belongs to %s)",
                     root, os.path.basename(path), "base.json", recorded)
        cache[root] = path
        return path

    def _current_guest_root(self, guest: "GuestSession | None" = None) -> str:
        """What `guest` is mirroring RIGHT NOW, or "" while it is mirroring
        nothing -- which is every conversation until its first tool call.

        THE COMMITTED FOLDER AND NOTHING ELSE. One mirror per conversation
        means the guest re-points its sync layer at the folder the chat works
        in, so `C:\\Gaston\\OK` and not `C:\\Gaston` is what its scan
        reports, and a baseline keyed on the latter refuses every pass for an
        identity mismatch.

        HELLO's `sync_root` used to be the fallback here and must not be: it
        now carries the machine's CONFIGURED root, which is where folders get
        created and never a tree anyone is pairing. Falling back to it would
        put this process's whole shared directory opposite the guest's, which
        is the arrangement per-conversation folders exist to end.
        """
        g = guest if guest is not None else getattr(self, "_guest", None)
        if g is not None and g.work_guest:
            return g.work_guest
        return ""

    def _load_baseline(self, guest: "GuestSession | None" = None):
        from .files.sync import Baseline
        return Baseline.load(self._base_path(guest))

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
        g = guest or self._guest
        mirror = self._mirror_dir(g) if g is not None else ""
        if not mirror:
            # NOT A FAULT, and it has to say so in as many words. An agent
            # told "mirrored: <shared root> <-> <configured root>" while the
            # baseline still named an older tree concluded synchronisation
            # was broken and refused to write the file it had been asked for,
            # which is the most expensive possible reading of "nothing has
            # been set up yet".
            return ("This conversation does not have a folder on the user's "
                    "machine yet, so nothing is being copied in either "
                    "direction. That is the normal state of a conversation "
                    "that has not used a tool -- it is not a fault and there "
                    "is nothing to diagnose. A folder is created the moment "
                    "you use one, and you will be told its path then.")
        try:
            base = self._load_baseline(g)
        except Exception as exc:
            return (f"The synchronisation baseline could not be read ({exc}). "
                    f"No pass will run until that is fixed; tell the user.")

        out = [f"Mirrored: {mirror} here <-> "
               f"{g.work_guest or self.guest_sync_root or '(not reported yet)'}"
               f" on the machine.",
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
            at = A.load(mirror)
        except Exception:
            at = None
        if at is not None and len(at):
            where = ", ".join(sorted(
                os.path.relpath(f, mirror) for f in at.sources)) or "?"
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
                       f"{mirror.rstrip('/')}/../.gaston/conflict/ with "
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

        stray, stray_truncated = self._edits_outside_the_mirror(g)
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
        self, guest: "GuestSession | None" = None, limit: int = 20
    ) -> tuple[list[str], bool]:
        """Files under cwd but outside the sync root, newest first.

        The other way an agent achieves nothing and reports success:
        `off_limits` confines it to cwd, the mirror is a directory inside cwd,
        and an edit anywhere else succeeds and stays here forever.

        Returns `(paths, truncated)`: `truncated` is True when the walk hit
        STRAY_SCAN_BUDGET before finishing, so the caller can say the list may
        be incomplete rather than imply it is exhaustive.
        """
        # WHERE THE AGENT ACTUALLY RUNS against WHAT IS ACTUALLY MIRRORED,
        # and the two are not the same question. A session's cwd is fixed
        # when the session is built, which for a new conversation is BEFORE
        # it has committed to a folder -- so the agent goes on running in
        # `self.cwd` while the mirror narrows to the folder underneath it,
        # and everything it writes beside that folder is a stray. That is
        # precisely the case this exists to catch, and reading the cwd back
        # off the conversation's folder would declare the two equal and find
        # nothing.
        g = guest if guest is not None else getattr(self, "_guest", None)
        cwd = self.cwd or os.getcwd()
        mirror_dir = (self._mirror_dir(g) if g else "") or self.sync_dir
        if not mirror_dir or os.path.realpath(cwd) == os.path.realpath(
                mirror_dir):
            return [], False
        mirror = os.path.realpath(mirror_dir)
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
            base = self._load_baseline(guest)
        except Exception as exc:
            return f"The baseline could not be read ({exc}); nothing changed."

        if action == "accept-tree":
            # PATH-LESS, and that is the point rather than an omission: the
            # aggregate-revert check refuses a WHOLE PASS, so answering it one
            # path at a time would mean fifty calls to lift one refusal, and
            # the thing being agreed to is a property of the tree rather than
            # of any file in it.
            #
            # It does not change a single row. All it does is let the next
            # pass past the check, after which the ordinary per-path rows
            # decide everything exactly as they always would.
            base.accept_tree = True
            base.save(self._base_path(guest))
            log.info("sync: whole-tree revert accepted by the agent")
            return ("The next pass will go ahead without the whole-tree "
                    "check. Its rows are unchanged, so the machine's version "
                    "of each path wins where it moved -- which is what was "
                    "being refused. Nothing has moved yet.")

        from .files import paths as P
        # guest.caps was parsed once at handshake (02.22). No guest at all
        # (never attached this process) is the one case with nothing to read
        # -- from_hello({}) is the documented restrictive Win9x default for
        # "no declaration", which is the honest answer here too.
        caps = guest.caps if guest is not None and guest.caps is not None \
            else C.from_hello({})
        key = P.canonical(path.replace("\\", "/").strip("/"), caps=caps)
        state = self._base_path(guest)

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
            # ON THE GUEST THIS CALL BELONGS TO, not on whichever one is
            # attached (02.18). The parameter was already here and this line
            # reached past it, so with two clients the bar would come down on
            # the wrong person's screen.
            if not base.latched and guest is not None:
                self._spawn(self._clear_halt(guest))
            return (f"Released {path}. Your next write to it will be copied to "
                    f"the user's machine on the next pass. If they change it "
                    f"again in the meantime, it will conflict again -- which "
                    f"is correct, not a failure.")

        if action == "accept-revert":
            # The guest holds exactly what the last push REPLACED, so `diff`
            # called it a reverted push and preserved the agent's version
            # rather than pulling (sync.py, row 15). That guard exists for a
            # stale editor buffer writing back an old copy, and it cannot tell
            # that from a person who genuinely undid the change -- which is
            # the whole reason this action exists rather than the agent being
            # told to "edit it again".
            #
            # Dropping `prev` is the entire mechanism: with no record of what
            # the push replaced, the guest simply differs from the ancestor,
            # and the ordinary "the guest moved" row pulls it. Nothing here
            # transfers anything or decides what the content should be.
            prev = base.prev.pop(key, None)
            was_latched = base.latched.pop(key, None) is not None
            if prev is None and not was_latched:
                return (f"{path} is not being held as a reverted push. "
                        f"Nothing changed -- call sync_status to see what is "
                        f"actually withheld.")
            base.save(state)
            log.info("sync: %s revert accepted by the agent", key)
            # ON THE GUEST THIS CALL BELONGS TO, not on whichever one is
            # attached (02.18). The parameter was already here and this line
            # reached past it, so with two clients the bar would come down on
            # the wrong person's screen.
            if not base.latched and guest is not None:
                self._spawn(self._clear_halt(guest))
            return (f"Accepted the machine's version of {path}. The next pass "
                    f"will copy it here, replacing what was written. The "
                    f"previous content is not kept -- if it is still wanted, "
                    f"read the file after the pull and re-apply the change.")

        if action != "forget":
            # A THIRD action made this fallthrough dangerous. Until now the
            # function had exactly two, so "not release" meant "forget" and
            # the only guard on that was in tools.py, a different file. An
            # unrecognised action reaching here dropped the row -- the
            # destructive one of the pair -- which is the wrong way round for
            # a default.
            return (f"Unknown action {action!r}; use release, accept-revert "
                    f"or forget.")

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
                base.save(self._base_path())
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

        The state lives on `self.synccoord` (a `SyncCoordinator`), not here:
        it decides and remembers, this method only sends the frame that
        follows. docs/audit/02-bridge-python.md 02.43.
        """
        hid = self.synccoord.raise_halt(line, paths)
        if hid is None:
            return
        await self._send(guest, halt(hid, line))

    async def _clear_halt(self, guest: GuestSession) -> None:
        """Take it down because the reason went away, not because it was
        answered -- the agent called sync_resolve itself, say."""
        hid = self.synccoord.clear_halt()
        if hid is None:
            return
        await self._send(guest, haltend(hid))

    async def _reconcile_halt(self, guest: GuestSession, base=None) -> None:
        """The halt is a VIEW of base.latched, not independent state.

        synccoord.halt_id is memory; base.latched is disk. Nothing but a
        fresh conflict (the new_conflicts branch above) or a reconnect
        (_reissue_halt) ever moved synccoord.halt_id to agree with it -- so a
        BRIDGE restart with paths still latched came back with no bar, a
        fresh SyncCoordinator whose halt_id starts empty, and every latched
        path going on refusing the agent's writes with nothing on the user's
        screen to explain why or let them clear it. The only way out used to
        be the agent proactively calling sync_status and sync_resolve.

        Called at attach (base loaded fresh) and at the end of every pass
        (base is the Pass's own, already current) so both directions --
        a bar that should be up and is not, one that is up and should not
        be -- self-correct instead of depending on whichever call site last
        had a chance to set synccoord.halt_id. docs/audit/02-bridge-python.md
        02.21.
        """
        if not self.sync_dir:
            return
        if base is None:
            try:
                base = self._load_baseline(guest)
            except Exception:
                return
        if base.latched and not self.synccoord.halt_id:
            n = len(base.latched)
            await self._raise_halt(
                guest,
                f"Stopped: {n} conflicting edit{'' if n == 1 else 's'}. "
                f"Your copy was kept.",
                paths=base.latched.keys())
        elif not base.latched and self.synccoord.halt_id:
            await self._clear_halt(guest)

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
                fut.set_result(ScanResult(
                    status=SyncStatus.parse(f.headers.get("status", "ok")),
                    files=int(f.headers.get("files", "0")),
                    tree_id=f.headers.get("tree_id", ""),
                    root=f.headers.get("root", ""),
                    rows=entry.rows, display=entry.display))

        elif f.type in ("SYNCPUTACK", "SYNCMKDIRACK"):
            fut = entry.future
            if not fut.done():
                fut.set_result(AckResult(
                    status=SyncStatus.parse(f.headers.get("status", "ok")),
                    crc=int(f.headers.get("crc", "0"))))

        elif f.type == "SYNCDAT":
            if entry.kind != "get":
                log.warning("SYNCDAT for %s, which is a %s -- ignoring",
                            sid, entry.kind)
                return
            entry.data.extend(f.body)

        elif f.type == "SYNCDATEND":
            fut = entry.future
            if not fut.done():
                fut.set_result(GetResult(
                    status=SyncStatus.parse(f.headers.get("status", "ok")),
                    crc=int(f.headers.get("crc", "0")),
                    cls=f.headers.get("cls", P.CLASS_TEXT),
                    data=bytes(entry.data or b"")))

    def _fail_syncs(self, guest: GuestSession | None, why: str) -> None:
        """Resolve every in-flight sync transfer, synchronously."""
        if guest is None:
            return
        for sid, entry in list(guest.sync_pending.items()):
            # entry.future is a required field on S.Wait, not an optional
            # one -- this used to be entry.get("future") from when
            # sync_pending held ad-hoc dicts (pre-02.4), and calling .get on
            # a Wait dataclass raises AttributeError instead of failing the
            # transfer, which is the one thing this method exists to do.
            fut = entry.future
            if not fut.done():
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
        room = self.EXEC_OUT_MAX - len(entry.out)
        if room <= 0:
            entry.capped = True
            return
        if len(f.body) > room:
            entry.out.extend(f.body[:room])
            entry.capped = True
            return
        entry.out.extend(f.body)

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
        fut = entry.future
        if fut.done():
            return

        status = ExecStatus.parse(f.headers.get("status", "ok"))
        code = f.headers.get("code", "")
        # Either side may have cut it: the guest at its 32KB cap, or us at
        # ours. Both mean the same thing to the model -- what arrived is not
        # the whole of it -- so they are reported the same way.
        truncated = f.headers.get("truncated", "0") == "1" or entry.capped

        # COMMAND.COM writes the OEM codepage, not the ANSI one. Using
        # fold.decode here instead is the classic Win9x encoding bug and turns
        # a `dir` listing into mojibake.
        text = fold.decode_oem(bytes(entry.out))

        parts = []
        prose = EXEC_STATUS_PROSE.get(status)
        if prose:
            parts.append(prose.format(max_cmd=self.EXEC_MAX_CMD))
        if code not in ("", "0") and status == ExecStatus.OK:
            parts.append(f"Exit code {code}.")
        if truncated:
            # Whichever side actually cut it (03.16). entry.capped means OUR
            # OWN EXEC_OUT_MAX bit first; the guest's own 32KB cap is the
            # ordinary case and is what f.headers["truncated"] reports on its
            # own. Blaming the guest's cap unconditionally was wrong whenever
            # ours was the one that actually fired.
            if entry.capped:
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
            "isError": status.is_error,
        })
        log.info("exec %s finished: status=%s code=%s bytes=%d",
                 exec_id, status.value, code, len(entry.out))

    def _mark_lost(self, guest: GuestSession) -> None:
        """Record that this guest's link is gone. Idempotent.

        Called from BOTH teardown paths -- `_on_client`'s `finally` and the
        idle reaper -- because each used to do only half of it and the reaper
        stopped the other half from running at all (02.8).

        A pending prompt is NOT denied here. Denying on any drop tells the
        model "the user declined" for something the user never saw -- and on
        a 1998 NE2000 over a flaky link, a blip would become a refusal.
        Instead the prompt is held for a short grace period: reconnect inside
        it and the bar comes back (PERMSTATE), miss it and the reaper denies
        with a message that says what actually happened. That expiry is gated
        on `lost_at`, which is why stamping it is the load-bearing line here.
        """
        if guest.lost_at is None:
            guest.lost_at = time.monotonic()
        # An in-flight command is a different matter from a pending prompt:
        # the prompt can wait out a blip, but the command has already started
        # over there and its output is gone with the link. Fail it now rather
        # than holding the model for the whole MCP budget.
        self._fail_syncs(guest, "The connected machine disconnected mid-sync.")
        self._fail_execs(guest,
                         "The connected machine disconnected while this "
                         "command was running. It may or may not have "
                         "completed.")
        if guest.perm_pending is not None:
            log.info("link lost with a prompt pending; holding %.0fs",
                     self.perm_grace_s)

    def _fail_execs(self, guest: GuestSession | None, why: str) -> None:
        """Resolve every in-flight EXEC, synchronously.

        Called where the guest goes away or the session is rebuilt. A future
        nobody will complete holds the model for the whole MCP budget.
        """
        if guest is None:
            return
        for exec_id, entry in list(guest.exec_pending.items()):
            fut = entry.future
            if not fut.done():
                fut.set_result({"content": [{"type": "text", "text": why}],
                                "isError": True})
        guest.exec_pending.clear()

    async def _perm_reply(self, guest: GuestSession, f: Frame) -> None:
        """Answer from the guest's prompt bar."""
        await guest.perms.reply(f.headers.get("id", ""),
                                f.headers.get("action", "deny"))

    async def _sweep_sync(self) -> None:
        """The backstop trigger. See run_sync for why it exists.

        Coalesced by run_sync's own lock, so a sweep that lands during a pass
        the guest asked for costs a debug line and nothing else.
        """
        while True:
            await asyncio.sleep(self.sync_sweep_s)
            guest = self._guest
            if self.sync_dir and guest is not None:
                # Bound HERE as well as inherited from _attach's context.
                # The inheritance is what 02.6 fixed; this is what keeps it
                # true once a task started for one connection is still
                # running during the next one -- and once R7 gives the bridge
                # two guests on two codepages, it is the only thing that can
                # be right.
                self._bind_codepages(guest)
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
                    guest.perms.deny_all(
                        "gone",
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
            guest.perms.deny_all("idle",
                                 "The Windows machine stopped responding.")
            w, guest.writer = guest.writer, None
            # BEFORE the close, and here rather than only in _on_client.
            # Nulling `writer` above is what made _on_client's `finally` skip
            # its own bookkeeping, so a reap left `lost_at` unset and every
            # sync and exec registered afterwards unfailed -- see _mark_lost
            # and 02.8. _on_client still calls it too; it is idempotent.
            self._mark_lost(guest)
            try:
                if w is not None:
                    w.close()
            except Exception:
                pass

    async def _models(self, guest: GuestSession) -> list[dict]:
        """The CLI's model list, fetched once per process (04.14).

        This was re-fetched from the CLI on every options refresh -- which
        happens on every attach, every model change and every effort change --
        and it shells out. The list is a property of the installed CLI, not of
        a session or a guest, so it cannot differ between the calls that were
        making it. Cached on the Bridge rather than on GuestSession for the
        same reason: a second client would get the same answer.

        Failure is not cached. A CLI that was briefly unavailable should be
        asked again rather than leaving the menu empty for the life of the
        process.
        """
        if self._models_cache is None:
            if not guest.claude:
                return []
            models = await guest.claude.server_models()
            if not models:
                return []
            self._models_cache = models
        return self._models_cache

    async def _commands(self, guest: GuestSession) -> list[dict]:
        """The CLI's slash-command list, cached like `_models`.

        Same lifetime and the same reason: it comes from the init result the
        SDK cached at connect, so it cannot differ between calls within one
        client, and a conversation with no client yet still has a Commands
        menu to fill. Failure is not cached.
        """
        if self._commands_cache is None:
            if not guest.claude:
                return []
            cmds = await guest.claude.server_commands()
            if not cmds:
                return []
            self._commands_cache = cmds
        return self._commands_cache

    async def _send_options(self, guest: GuestSession) -> None:
        """The model and effort menus, with the one in use marked.

        The list comes from the CLI rather than from a table here, so the names
        stay correct without a release. There is deliberately no "Default"
        entry: it is not a model, it is an alias, and a menu that says
        "Default" does not tell you what you are talking to. The alias is
        resolved and the real model it points at is the one ticked.
        """
        # A conversation opened from the chat list has no client until the
        # first message, and its menus must still be right: the model list is
        # a property of the installed CLI, and mode and effort are the
        # guest's own. `_models` answers from the cache in that state; only a
        # bridge that has never built a session at all has nothing to say.
        if not guest.claude and self._models_cache is None:
            return
        models = await self._models(guest)

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
            # `description` rides as its own header, not folded into the
            # label: the label is what the toolbar BUTTON shows, and "Haiku
            # 4.5 - Fastest for quick answers" does not fit on a 640px screen
            # while "Haiku" does. The menu shows both, which is where the
            # question "which of these is which?" is actually asked -- four
            # display names alone do not answer it.
            await self._send(guest, option("model", value, label, current,
                                           desc=m.get("description") or ""))

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

        # Where this conversation's files live, as the GUEST spells it. One
        # row, no tick -- it is a fact about the conversation rather than a
        # choice among alternatives, and the guest shows it on the toolbar so
        # that "where do my files go?" is answerable without asking.
        #
        # `id` is the full path and `label` is the short form the button
        # wears; the same split the model rows use, and for the same reason:
        # a 640px screen has no room for C:\WORK\SOMETHING on a button.
        await self._send(guest, option(
            "work", guest.work_guest, guest.work_guest or "<unset>", False,
            desc=guest.work_guest))

        # AFTER the models and efforts, not before: the guest clears all three
        # menus when it sees the first `model` row of a batch, which is how it
        # knows a batch has started without a frame type saying so. A mode row
        # sent ahead of that would be appended and then deleted.
        for mode in MODE_ORDER:
            await self._send(guest, option(
                "mode", mode, MODE_LABEL.get(mode, mode),
                mode == guest.perm_mode))

        # Intersected with what this CLI actually offers rather than sent from
        # the table alone: a menu entry for a command the install does not
        # have would fail as a typed prompt, several seconds later, with the
        # model apologising for it.
        have = {}
        for c in await self._commands(guest):
            n = c.get("name")
            if isinstance(n, str) and n:
                have["/" + n] = c.get("argumentHint") or ""
        for cmd in COMMAND_MENU:
            if cmd not in have:
                continue
            # The hint goes in the LABEL, because the menu is where the
            # question "what does this one take?" is actually asked. The id
            # stays the bare command -- it is what gets sent.
            hint = have[cmd]
            label = (cmd + " " + hint) if hint else cmd
            await self._send(guest, option("command", cmd, label, False,
                                           hint=hint))
        await self._send(guest, optionsend())

    async def _quiesce_turn(self, guest: GuestSession) -> None:
        """Stop whatever turn is running, so `turn_lock` can be had.

        The three methods below destroy and replace the object a turn may be
        running ON. Taking the lock without this would park `_pump` -- and
        therefore every inbound frame -- for as long as the model felt like
        streaming.

        The kill goes out first for the same reason `_on_interrupt` sends it:
        the command is running on the guest's single worker thread and
        forgetting about it bridge-side leaves the NEXT conversation's first
        `exec` answered `busy`.
        """
        if not guest.turn_lock.locked():
            return
        log.info("interrupting the live turn before the conversation changes")
        for exec_id in list(guest.exec_pending):
            self._kill_exec(guest, exec_id)
        if guest.claude:
            try:
                await guest.claude.interrupt()
            except Exception:
                # Best effort. A client that refuses to be interrupted is
                # still one we are about to close, and the wait below is
                # bounded by the turn ending one way or another.
                log.exception("could not interrupt the running turn")

    async def _set_effort(self, guest: GuestSession, effort: str) -> None:
        """See `_set_effort_locked`; this is the turn-lock half (02.1)."""
        await self._quiesce_turn(guest)
        async with guest.turn_lock:
            await self._set_effort_locked(guest, effort)

    async def _new_session(self, guest: GuestSession,
                           folder: str = "") -> None:
        """See `_new_session_locked`; this is the turn-lock half (02.1)."""
        await self._quiesce_turn(guest)
        async with guest.turn_lock:
            await self._new_session_locked(guest, folder)

    async def _switch(self, guest: GuestSession, session_id: str) -> None:
        """See `_switch_locked`; this is the turn-lock half.

        UNDER `guest.turn_lock`, all three of them, and this is 02.1. Each
        closes `guest.claude` and replaces the transcript, and each is
        reached from `_handle` -- which runs in `_pump`'s task -- while a
        turn spawned by `_on_input` may be suspended inside
        `guest.claude.send()`. They are concurrent by construction, not by
        race.

        What happened without the lock: `reset_transcript()` replaced
        `view.pages` and `view.render` and cleared the coalescers, the
        suspended turn's SDK callbacks kept firing, and the old
        conversation's remaining prose was appended to the conversation the
        user had just opened -- into its paginator, so also into its Export
        and onto its sealed pages. Then `close()` made `receive_response()`
        end without a `ResultMessage`, `_consume` returned an error Turn
        rather than raising, and `_run_turn_locked` walked into
        `self.conv.add(guest.claude.session_id)` with `guest.claude`
        already None. The AttributeError died inside a task nothing
        inspected, so no TURN end frame was ever sent and the guest's Send
        button stayed disabled until the link was re-established.
        """
        await self._quiesce_turn(guest)
        async with guest.turn_lock:
            await self._switch_locked(guest, session_id)

    async def _set_effort_locked(self, guest: GuestSession,
                                 effort: str) -> None:
        """Change effort, which means reopening the session.

        There is no set_effort() on the client -- effort is fixed when the
        session starts -- so this closes the current client and resumes the
        same conversation with fork_session=True. The fork is why the history
        survives and why the session id changes.
        """
        label = EFFORT_LABEL.get(effort, effort or "default")

        # No client means a conversation opened from the chat list and not yet
        # spoken in -- there is nothing to fork, and nothing needs forking.
        # Record it and let the deferred resume start with it. Picking an
        # effort there used to be a silent no-op.
        if not guest.claude:
            guest.effort = effort
            self._remember_settings(guest)
            log.info("effort -> %s (no client yet)", effort or "(default)")
            await self._write_block(guest, H.notice(
                "Effort set to " + label + "."))
            await self._send_options(guest)
            return

        old = guest.claude.session_id
        self._abandon_pending(guest)
        guest.effort = effort
        self._remember_settings(guest)
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
        # guest.sess_id is a stable per-connection handle now, not the SDK's
        # own id (02.24) -- nothing to update here.
        await self._send_options(guest)

    async def _set_mode(self, guest: GuestSession, mode: str) -> None:
        """Change the permission mode, live -- no fork, unlike effort.

        Refused rather than passed through if it is not one we offer: the
        header is model-free but it still arrives off a socket, and
        `bypassPermissions` would silently stop the permission bar from ever
        appearing again.
        """
        if mode not in MODE_ORDER:
            log.warning("ignoring unknown permission mode %r", mode)
            return
        guest.perm_mode = mode
        self._remember_settings(guest)
        label = MODE_LABEL.get(mode, mode)
        if guest.claude:
            try:
                await guest.claude.set_permission_mode(mode)
            except Exception as exc:
                log.exception("permission mode change failed")
                await self._write_block(guest, H.error(
                    "Could not change the permission mode: " + str(exc)[:160]))
                await self._send_options(guest)
                return
        log.info("permission mode -> %s", mode)
        note = "Permission mode set to " + label + "."
        caveat = MODE_CAVEAT.get(mode)
        if caveat:
            note += " " + caveat
        await self._write_block(guest, H.notice(note))
        await self._send_options(guest)

    async def _new_session_locked(self, guest: GuestSession,
                                  folder: str = "") -> None:
        """Start a fresh conversation.

        NO FOLDER IS CHOSEN HERE, and an earlier version asked for one. A
        folder is named after the conversation and the conversation has no
        name yet -- the CLI titles it from the first message -- so the prompt
        was asking the user to invent something before there was anything to
        base it on, and then live with it. `folder` is still accepted so an
        older guest is not broken by a header the bridge stopped reading.
        """
        name = ""
        log.info("new session requested")
        self._abandon_pending(guest)
        if guest.claude:
            await guest.claude.close()
        # A fresh conversation is not a resumed one: whatever the chat list
        # opened and never spoke in is abandoned here too, or the first
        # message in the NEW chat would resume the OLD one.
        guest.pending_resume = ""

        # CREATED HERE, not lazily: the session is about to be built with
        # this as its cwd, and the CLI is entitled to a directory that
        # exists. Inside the mirror, so the folder and everything in it
        # reaches the user's machine on the next pass without anything else
        # being told about it.
        # Back to unnamed, and back to sharing nothing. The new conversation
        # commits to a folder at its first tool call, like every other one,
        # and is named after its own first message rather than the one that
        # named the chat being left.
        guest.work_dir = ""
        guest.work_guest = ""
        guest.first_prompt = ""
        self._retarget_perms(guest)

        await guest.reset_transcript()

        # NOTHING IS SPAWNED HERE either, the same as opening a chat from the
        # list. It used to build the client immediately, and that made the
        # settings unusable in the one window where a user most wants them:
        # between pressing New chat and typing, Model, Effort and Mode all
        # acted on a client that existed but had never run a turn, and the
        # CLI refuses control requests against one -- so picking a mode said
        # "Could not change the permission mode" and picking an effort tried
        # to fork a session with no history to fork.
        #
        # With no client there is nothing to refuse. All three handlers
        # already have a "record it and move on" path, written for the chat
        # list, and `_make_session` reads guest.model, guest.effort and
        # guest.perm_mode when the first message finally builds one -- so a
        # setting chosen now applies to the first message rather than being
        # an error the user has to work around.
        #
        # Safe because the cleanup this used to carry moved out:
        # `_abandon_pending` above denies the old conversation's prompts and
        # fails its execs whether or not anything is rebuilt.
        guest.claude = None
        await self._send(guest, switched("", guest.pages.count))
        await self._send_page(guest, "live")
        await self._send_options(guest)

    async def _switch_locked(self, guest: GuestSession,
                             session_id: str) -> None:
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

        # Whatever the conversation being left behind still owes, it owes to a
        # client that is about to stop existing. This used to happen inside
        # _make_session, which this path no longer calls at all.
        self._abandon_pending(guest)
        if guest.claude:
            await guest.claude.close()
        guest.claude = None

        # NOTHING IS SPAWNED HERE. Opening a conversation to read it used to
        # start a full CLI subprocess with --resume: no turn ran and nothing
        # was billed, but on a machine where the user browses the list to find
        # something, every click paid for a process start. Replay is a file
        # read. The client is built at the first message instead
        # (_resume_pending), which is the first moment it can do anything.
        guest.pending_resume = session_id
        # Its OWN model, effort and mode, not the ones the chat being left
        # happened to be using. Before this, opening a conversation you had
        # set to Plan and Opus gave you whatever was on screen a moment ago.
        self._restore_settings(guest, session_id)
        # And its folder comes back with it. A conversation started in
        # C:\WORK\NOTES has to resume there, or the second half of it would
        # be written somewhere the first half is not -- so this is read from
        # the stored session's own cwd rather than left on whatever the
        # previous conversation was using.
        # FROM OUR OWN RECORD, not from the session's cwd. The folder is
        # named after the conversation and the conversation is named from its
        # first message -- by which point the session has already recorded
        # the directory it STARTED in, which is the root. Reopening a chat
        # showed "<unset>" for a conversation whose folder was sitting on
        # disk, because the only place that knew was here.
        name = self.conv.folder(session_id)
        if name and self.sync_dir:
            guest.work_dir = os.path.join(self.sync_dir, name)
            # The guest's own spelling, which it may have chosen itself and
            # which cannot be derived from a name on this side.
            guest.work_guest = (self.conv.guest_folder(session_id)
                                or self._guest_path(guest, name))
        else:
            guest.work_dir = self._stored_work_dir(session_id)
            guest.work_guest = ""
        # A conversation resumed from the chat list has a title already, and
        # that is what names it if it still has no folder -- so this must not
        # carry the previous chat's opening line into it.
        guest.first_prompt = ""
        self._retarget_perms(guest)

        # Fresh transcript state. Anything the old session had is on disk.
        await guest.reset_transcript()

        for block in sesslist.history_html(session_id):
            # Through _write_block, not pages.add (02.39): one path records a
            # block, so the split-across-a-seal handling that 02.33 fixed
            # cannot be right in one place and wrong in the other. Both sends
            # are withheld -- see _write_block's docstring for why replay is
            # the one caller that wants neither.
            await self._write_block(guest, block, stream=False, notify=False)
            if guest.pages.live.over_soft_cap():
                guest.pages.message_boundary()

        # reset_transcript() above already set view_page to None; nothing
        # in between reads or writes it.
        await self._send(guest, switched(session_id, guest.pages.count))
        await self._send_page(guest, "live")
        # The menus follow the conversation even though no client exists --
        # the model list is a property of the CLI, not of a session, and mode
        # and effort are the guest's own. _switch was the only session-
        # changing path that never re-sent them (02.x); now it does, and from
        # the cache rather than from a process it deliberately did not start.
        await self._send_options(guest)

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

        # An empty LIVE page is a conversation nobody has spoken in yet, and
        # it looked exactly like a conversation whose transcript failed to
        # arrive: PAGESTART clears the view, there are no runs, PAGEEND lands,
        # and the reader is left facing a blank white rectangle. The cold-start
        # placeholder the guest draws for itself (main.cpp, boot_local) is gone
        # by then -- the first PAGESTART cleared it -- and "Connected to
        # bridge." is gated on the FIRST connect, so New chat produced nothing
        # at all.
        #
        # A NOTICE, not a recorded block: it belongs to this view of this
        # moment, not to the transcript. Paginating it would put "Type a
        # message to start." above the first reply for the rest of the
        # conversation's life, and page 1 would still carry it when you paged
        # back to it a thousand messages later.
        if guest.view_page is None and not page.runs:
            await self._send(guest, notice(
                "empty", "Type a message below to start this conversation."))

    # --- sending -------------------------------------------------------
    #
    # The transport itself -- the writer, the send lock, the compressor, the
    # chunking -- lives in GuestLink (bridge/wire/link.py) now. These three
    # stay on Bridge, as thin delegations to guest.link, because both
    # syncrun.py's Pass and tests/python reach them by these names.

    async def _send(self, guest: GuestSession, frame: Frame) -> bool:
        return await guest.link.send(guest, frame)

    async def _send_html(self, guest: GuestSession, html: str, mode: str,
                         page_no: int) -> None:
        await guest.link.send_html(guest, html, mode, page_no)

    async def wire_yield(self, guest: GuestSession) -> None:
        await guest.link.wire_yield(guest)

    async def _try_send_raw(self, writer: asyncio.StreamWriter,
                            frame: Frame) -> None:
        await GuestLink.send_raw(writer, frame)


# Effort levels, in the order they should appear. Which of these a given model
# actually offers comes from the CLI, not from here.
EFFORT_ORDER = ["low", "medium", "high", "xhigh", "max"]
EFFORT_LABEL = {"low": "Low", "medium": "Medium", "high": "High",
                "xhigh": "Extra high", "max": "Maximum"}

# Permission modes offered to the guest, in menu order. THREE OF THE SDK'S
# SIX, and the three that are missing are missing for measured reasons:
#
#   bypassPermissions  the docs say can_use_tool is never invoked. The bar
#                      would stop appearing and nothing would say why.
#   dontAsk            denies anything not pre-approved by an allow rule, and
#                      we persist no allow rules yet (4.1j). Today it would
#                      deny everything, which is not a mode, it is a fault.
#
# `auto` IS offered, and was withheld for one release. It was asked for by
# name, and taking it off the menu on the strength of a measurement was the
# wrong call to make silently: the measurement says what auto does NOT do, and
# that belongs in front of the person choosing it rather than in a decision
# made for them. What was measured, on this bridge: with `can_use_tool`
# installed the model's own danger classifier is not consulted at all --
# `git reset --hard` on a repo with real uncommitted work, a documented
# blocked-by-default action, executed unchanged in `auto` and in `default`.
# So auto here means "the permission prompt still asks", not "something
# smarter is watching". `_set_mode` says so in the transcript when it is
# picked.
MODE_ORDER = ["default", "acceptEdits", "plan", "auto"]
MODE_LABEL = {"default": "Manual", "acceptEdits": "Accept edits",
              "plan": "Plan", "auto": "Auto"}
#: Said once, when the mode is chosen, for modes whose name promises more than
#: this client can deliver.
MODE_CAVEAT = {
    "auto": "Measured on this bridge: with the permission prompt installed, "
            "the model's own danger classifier is not consulted -- "
            "`git reset --hard` on a repo with uncommitted work ran "
            "unchanged in Auto and in Manual. Auto still asks you; it does "
            "not add a second opinion.",
}

# Slash commands offered on the Commands menu, in menu order.
#
# THREE OF FIFTY-THREE, and the ratio is the point. The CLI's own list is
# whatever the operator's install happens to carry -- on the machine this was
# written against: plugin management, cloud workflows billed by the run, a JS
# heap dump written to the operator's Desktop, three ways to grant a web
# service access to a design system. None of that means anything on a Win98
# box, and some of it spends money or touches the bridge host.
#
# There is no SDK list to lean on. `terminal_slash_commands` was assumed to
# exist and does not (see Session.server_commands). So the subset is ours, and
# the rule for adding to it is: does this do something a person sitting at a
# 640x480 screen with no terminal actually wants, that no other control here
# already does, without spending money by surprise?
#
# Deliberately NOT here, so nobody "completes" the list:
#   /model, /effort     the toolbar owns those. Going through the CLI would
#                       change the model WITHOUT a SETMODEL frame, so the
#                       dropdown would keep showing the old one -- a control
#                       that lies is worse than one that is missing.
#   /clear              New chat already does it, through a path that resets
#                       the transcript and the paginator too.
#   /context            the status bar has the gauge.
#   /code-review, /security-review, /ultrareview
#                       cloud agents billed per run, launched by one click on
#                       a machine whose owner cannot see what they cost.
#   /doctor, /config, /mcp, /reload-plugins, /import, /heapdump, /fast, /color
#                       operator-environment features. They configure or
#                       inspect the BRIDGE HOST, which is not the machine the
#                       person at the keyboard is sitting at.
#   /recap, /usage      MEASURED on the guest: both ran, both cost a turn,
#                       and both rendered an empty transcript. The CLI
#                       resolves them locally and sends nothing back. They
#                       were on this list until they were tried.
COMMAND_MENU = ["/compact", "/rename", "/init"]


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
