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
from collections.abc import AsyncIterable
import contextlib
import logging
import json
import os
import time
from dataclasses import dataclass, field
from enum import Enum, auto

from . import handshake
from .files import caps as C
from .files import manifest
from .status import EXEC_STATUS_PROSE, ExecStatus
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
                            _quote,
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
                            status_frame,
                            turn_end,
                            turn_error,
                            turn_start,
                            )
from .wire.link import GuestLink
from .files.synctransport import GuestSyncState
from .view.transcript import TranscriptView
from .agent import sessions as sesslist
from .files.sync import Entry as SyncEntry, Type as SyncType
from .files.sync import AckResult, GetResult, ScanResult
from .files.syncrun import SyncError
from .agent.session import TOOLS, Session
from .agent import toolrep, toolrules
from .agent.toolrules import Found
from .services.config import BridgeConfig
from .services.connection import ConnectionService
from .services.options import (
    COMMAND_MENU,
    EFFORT_LABEL,
    EFFORT_ORDER,
    MODE_CAVEAT,
    MODE_LABEL,
    MODE_ORDER,
    OptionsService,
)
from .status import GuestState
from .services.workspace import WorkspaceService
from .services.presentation import PresentationService
from .services.guest_exec import ExecCall, GuestExecService
from .services.permission import PermissionService
from .services.sync import SyncService
from .services.attachment import AttachmentService
from .services.session import SessionService
from .services.turn_runner import TurnRunner, turn_verdict
from .services.coordinator import SessionCoordinator

log = logging.getLogger("bridge.server")

DEFAULT_PORT = 7890


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
    def zip_in(self) -> StreamDecompressor:
        return self.link.zip_in

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

    # --- derived state, for logging and diagnostics -------------------------

    @property
    def state(self) -> GuestState:
        """Derived state — a projection over existing fields.

        Priority: DISCONNECTED > HALTED > RUNNING_TURN > IDLE.

        The axes are orthogonal (turn-running, halt-raised, permission-
        pending and exec-in-flight can all be true simultaneously), so
        this is an observational projection, not an authoritative gate.
        """
        if not self.connected:
            return GuestState.DISCONNECTED
        if self.synccoord.halt_id:
            return GuestState.HALTED
        if self.turn_lock.locked():
            return GuestState.RUNNING_TURN
        return GuestState.IDLE

    @property
    def state_detail(self) -> str:
        """Human-readable state detail for log lines.

        Captures the orthogonal axes the single enum cannot represent:
        which combination of turn/halt/perm/exec is active right now.
        """
        parts = []
        if not self.connected:
            parts.append("disconnected")
            if self.lost_at is not None:
                parts.append(f"lost={time.monotonic() - self.lost_at:.0f}s ago")
        if self.synccoord.halt_id:
            parts.append(f"halted={self.synccoord.halt_id}")
        if self.turn_lock.locked():
            parts.append("turn-running")
        if self.perm_pending is not None:
            parts.append("perm-pending")
        if self.exec_pending:
            parts.append(f"exec={len(self.exec_pending)}")
        if self.sync_active:
            parts.append("sync-active")
        return ", ".join(parts) or "idle"


class Bridge:

    def __init__(
        self,
        secret: str = "",
        *,
        config: BridgeConfig | None = None,
        host: str = "127.0.0.1",
        port: int = DEFAULT_PORT,
        cwd: str | None = None,
        model: str | None = None,
        effort: str | None = None,
        link_bps: float | None = None,
    ) -> None:
        if config is None:
            config = BridgeConfig(
                secret=secret,
                host=host,
                port=port,
                cwd=cwd,
                model=model,
                effort=effort,
                link_bps=link_bps,
            )
        self.config = config
        #: What this bridge knows about each conversation: whether it
        #: started it (so the chat list shows those and not every
        #: conversation on the machine), the model, effort and permission
        #: mode chosen for it, and the folder it works in on both sides.
        #: One row, one file -- see agent/conversations.py for why those
        #: three were not worth three stores.
        self.conv = Conversations()
        self.connection = ConnectionService(
            config=self.config,
            create_guest=self._create_guest,
            on_attach=self._post_attach,
            handle_frame=self._dispatch_frame,
            on_lost=self._on_lost,
            ensure_background_tasks=self._ensure_background_tasks,
            spawn=self._spawn,
        )
        if hasattr(self, "_fallback_guest") and self._fallback_guest is not None:
            self.connection.guest = self._fallback_guest
        self.presentation = PresentationService(connection=self.connection)
        self.options = OptionsService(
            connection=self.connection,
            conv=self.conv,
            config=self.config,
            write_block=self.presentation.write_block,
            set_effort=self._set_effort,
        )
        self.workspace = WorkspaceService(
            config=self.config,
            conv=self.conv,
            write_block=self.presentation.write_block,
            send_options=self._send_options,
            spawn_sync=lambda reason: self._spawn(self.run_sync(reason)),
        )
        self.guest_exec = GuestExecService(
            connection=self.connection,
            spawn=self._spawn,
            pre_exec=self._pre_exec,
        )
        self.permissions = PermissionService(
            connection=self.connection,
            presentation=self.presentation,
            workspace=self.workspace,
            spawn=self._spawn,
        )
        self.sync = SyncService(
            config=self.config,
            connection=self.connection,
            workspace=self.workspace,
            presentation=self.presentation,
            spawn=self._spawn,
        )
        self.attachment = AttachmentService(
            connection=self.connection,
            workspace=self.workspace,
            sync=self.sync,
        )
        self.session = SessionService(
            connection=self.connection,
            presentation=self.presentation,
            workspace=self.workspace,
            options=self.options,
            guest_exec=self.guest_exec,
            sync=self.sync,
            conv=self.conv,
            spawn=self._spawn,
        )
        self.turn_runner = TurnRunner(
            config=self.config,
            connection=self.connection,
            presentation=self.presentation,
            attachment=self.attachment,
            sync=self.sync,
            guest_exec=self.guest_exec,
            permission=self.permissions,
            options=self.options,
            workspace=self.workspace,
            session=self.session,
            conv=self.conv,
        )
        self.coordinator = SessionCoordinator(
            connection=self.connection,
            turn_runner=self.turn_runner,
            sync=self.sync,
            permission=self.permissions,
            session=self.session,
            workspace=self.workspace,
            guest_exec=self.guest_exec,
            attachment=self.attachment,
            spawn=self._spawn,
        )

        # Frame type -> the service method that answers it. Bound callables,
        # not method names resolved by getattr on this class: the handlers
        # belong to the services now, and a name-keyed table was the last
        # thing forcing a forwarder onto Bridge for every frame type.
        self._dispatch: dict = {
            "INPUT":        self.coordinator.on_input,
            "RESUME":       self.coordinator.on_resume,
            "INTERRUPT":    self.coordinator.on_interrupt,
            "SWITCH":       self.coordinator.on_switch,
            "NEWSESSION":   self.coordinator.on_newsession,
            "SYNCDIRTY":    self.sync.on_syncdirty,
            "SYNCENT":      self.sync.sync_frame,
            "SYNCEND":      self.sync.sync_frame,
            "SYNCPUTACK":   self.sync.sync_frame,
            "SYNCMKDIRACK": self.sync.sync_frame,
            "SYNCDAT":      self.sync.sync_frame,
            "SYNCDATEND":   self.sync.sync_frame,
            "EXECOUT":      self.guest_exec.exec_out,
            "EXECRESULT":   self.guest_exec.exec_result,
            "PERMREPLY":    self.permissions.reply,
            "GETSESSIONS":  self.session.on_getsessions,
            "GETPAGE":      self.presentation.on_getpage,
            "STAT":         self.connection.on_stat,
            "PING":         self.connection.on_ping,
            "SETMODEL":     self.options.on_setmodel,
            "SETEFFORT":    self.options.on_seteffort,
            "SETMODE":      self.options.on_setmode,
            "EXPORT":       self.session.on_export,
            "RULES":        self.permissions.on_rules,
            "SETWORK":      self.workspace.on_setwork,
        }

        # One guest, one session. See PLAN.md "Multi-session".
        self._server: asyncio.Server | None = None
        # Strong references to fire-and-forget tasks. asyncio holds only a
        # weak one, so a bare create_task can be collected mid-flight.
        self._tasks: set = set()
        # Frames pipelined behind HELLO in a single read, handed to _pump.
        self._reaper: asyncio.Task | None = None
        self._sweeper: asyncio.Task | None = None
        # The mirror root the guest reported, the reports from the last pass,
        # the resolved baseline path and the transient-abort count all used to
        # be mirrored here as well as on the GuestSession that owns them. A
        # second guest could read the first one's, so the mirrors are gone and
        # `guest.guest_sync_root` / `.last_sync_reports` / `.transient_aborts`
        # are the only copies. The pre-attach SyncCoordinator moved to
        # ConnectionService, which owns the guest slot it precedes.

    def _ensure_config(self) -> BridgeConfig:
        if not hasattr(self, "config"):
            self.config = BridgeConfig(secret="")
        return self.config

    def _ensure_connection(self) -> ConnectionService:
        """The link service, created on demand.

        Tests build a Bridge with `__new__` and no `__init__`, so every lazy
        bootstrap below has to be able to stand one up. Services depend on
        ConnectionService rather than on this composition root, so it is the
        one that has to exist first.
        """
        conn = getattr(self, "connection", None)
        if conn is None:
            conn = self.connection = ConnectionService(config=self._ensure_config())
            # `_guest` parks the session on `_fallback_guest` while there is
            # no link service to hold it. Hand it over, or a guest assigned
            # before the first lazy bootstrap is silently lost.
            fallback = getattr(self, "_fallback_guest", None)
            if fallback is not None:
                conn.guest = fallback
        return conn

    def _ensure_sync(self) -> SyncService:
        if not hasattr(self, "sync"):
            cfg = self._ensure_config()
            conn = self._ensure_connection()
            conv = getattr(self, "conv", None) or Conversations()
            self.conv = conv
            pres = getattr(self, "presentation", None) or PresentationService(connection=conn)
            self.presentation = pres
            ws = getattr(self, "workspace", None) or WorkspaceService(
                config=cfg,
                conv=conv,
                write_block=pres.write_block,
                send_options=getattr(self, "_send_options", lambda *a, **kw: None),
                spawn_sync=lambda reason: None,
            )
            self.workspace = ws
            self.sync = SyncService(
                config=cfg,
                connection=conn,
                workspace=ws,
                presentation=pres,
                spawn=getattr(self, "_spawn", lambda coro: None),
            )
        return self.sync

    @property
    def conv(self) -> Conversations:
        c = getattr(self, "_conv", None)
        if c is None:
            c = self._conv = Conversations()
        return c

    @conv.setter
    def conv(self, val: Conversations) -> None:
        self._conv = val
        if hasattr(self, "options") and self.options is not None:
            self.options.conv = val
        if hasattr(self, "workspace") and self.workspace is not None:
            self.workspace.conv = val
        if hasattr(self, "session") and self.session is not None:
            self.session.conv = val


    @property
    def host(self) -> str:
        return self._ensure_config().host

    @host.setter
    def host(self, val: str) -> None:
        self._ensure_config().host = val

    @property
    def port(self) -> int:
        return self._ensure_config().port

    @port.setter
    def port(self, val: int) -> None:
        self._ensure_config().port = val

    @property
    def cwd(self) -> str | None:
        return self._ensure_config().cwd

    @cwd.setter
    def cwd(self, val: str | None) -> None:
        self._ensure_config().cwd = val

    @property
    def model(self) -> str | None:
        return self._ensure_config().model

    @model.setter
    def model(self, val: str | None) -> None:
        self._ensure_config().model = val

    @property
    def effort(self) -> str | None:
        return self._ensure_config().effort

    @effort.setter
    def effort(self, val: str | None) -> None:
        self._ensure_config().effort = val

    @property
    def link_bps(self) -> float | None:
        return self._ensure_config().link_bps

    @link_bps.setter
    def link_bps(self, val: float | None) -> None:
        self._ensure_config().link_bps = val

    @property
    def sync_dir(self) -> str | None:
        return self._ensure_config().sync_dir

    @sync_dir.setter
    def sync_dir(self, val: str | None) -> None:
        self._ensure_config().sync_dir = val

    @property
    def sync_state(self) -> str | None:
        return self._ensure_config().sync_state

    @sync_state.setter
    def sync_state(self, val: str | None) -> None:
        self._ensure_config().sync_state = val

    @property
    def sync_sweep_s(self) -> float:
        return self._ensure_config().sync_sweep_s

    @sync_sweep_s.setter
    def sync_sweep_s(self, val: float) -> None:
        self._ensure_config().sync_sweep_s = val

    @property
    def idle_timeout_s(self) -> float:
        return self._ensure_config().idle_timeout_s

    @idle_timeout_s.setter
    def idle_timeout_s(self, val: float) -> None:
        self._ensure_config().idle_timeout_s = val

    @property
    def idle_check_s(self) -> float:
        return self._ensure_config().idle_check_s

    @idle_check_s.setter
    def idle_check_s(self, val: float) -> None:
        self._ensure_config().idle_check_s = val

    @property
    def perm_grace_s(self) -> float:
        return self._ensure_config().perm_grace_s

    @perm_grace_s.setter
    def perm_grace_s(self, val: float) -> None:
        self._ensure_config().perm_grace_s = val


    @property
    def _guest(self) -> GuestSession | None:
        conn = getattr(self, "connection", None)
        if conn is not None:
            return conn.guest
        return getattr(self, "_fallback_guest", None)

    @_guest.setter
    def _guest(self, val: GuestSession | None) -> None:
        conn = getattr(self, "connection", None)
        if conn is not None:
            conn.guest = val
        else:
            self._fallback_guest = val


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
        return self._ensure_connection().synccoord

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

    def _create_guest(self, page_bytes: int, page_blocks: int) -> GuestSession:
        g = GuestSession(
            view=TranscriptView(pages=Paginator(page_bytes, page_blocks)),
            synccoord=self._ensure_connection()._preattach_synccoord,
        )
        self.permissions.attach(g, cwd=self.cwd)
        return g

    async def _post_attach(self, guest: GuestSession, is_reconnect: bool) -> None:
        if guest.claude is None and not guest.pending_resume:
            guest.claude = await self._make_session(guest)

        await self._send(guest, ready(
            guest.sess_id,
            (guest.claude.model if guest.claude else guest.model)
            or "(default)",
            guest.pages.live.number, guest.pages.count,
            conv=guest.conv_id))

        await self._send(guest, doc(H.document()))
        await self._send_options(guest)

        guest.lost_at = None
        await self.coordinator.sync_state_to_guest(guest)
        await self.sync.reconcile_halt(guest)

        if self.sync_dir:
            self.turn_runner.seed_agent_notices()
            self._spawn(self.run_sync("the machine reconnected"))

        log.info("attached sess=%s (reconnect=%s)", guest.sess_id, is_reconnect)

    async def _on_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        await self.connection.on_client(reader, writer)


    # --- Claude session ----------------------------------------------------


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
    FOLDER_MAX_LONG = P.FOLDER_MAX_LONG
    folder_max = staticmethod(P.folder_max)
    folder_name = staticmethod(P.folder_name)
    _title_folder = staticmethod(P.title_folder)


    async def _ensure_work_dir(self, guest: GuestSession) -> None:
        await self.workspace.ensure_work_dir(guest)


    async def _make_session(self, guest: GuestSession,
                            resume: str | None = None,
                            fork: bool = False) -> Session:
        return await self.session.make_session(guest, resume=resume, fork=fork)

    # --- permissions -------------------------------------------------------
    #
    # The prompt bar itself -- the can_use_tool callback, the queue, reconnect
    # recovery, and the two ways a prompt ends -- lives on `guest.perms`
    # (bridge/agent/permdesk.py) now. What is left here is what still
    # genuinely needs the whole Bridge: the chat list and the halt bar, which
    # are not part of the audit's "Prompts" group (docs/audit/02-bridge-python.md
    # 02.41).


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


    async def _write_block(self, guest: GuestSession, html: str,
                           *, stream: bool = True,
                           notify: bool = True) -> None:
        await self.presentation.write_block(guest, html, stream=stream, notify=notify)

    # --- frame pump --------------------------------------------------------


    async def _dispatch_frame(self, guest: GuestSession, f: Frame) -> None:
        handler = self._dispatch.get(f.type)
        if handler is None:
            log.warning("unknown frame type from guest: %s", f.type)
            await self._send(guest, error("unknown_type", f.type))
            return
        await handler(guest, f)


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
        try:
            def_bps = float(f.headers.get("deflate_bps", "0"))
        except ValueError:
            def_bps = 0.0
        if def_bps > 0:
            guest.link.deflate_bps = def_bps
            log.info("guest reports deflate %.0f B/s", def_bps)


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


    # --- running commands on the guest -------------------------------------

    async def _pre_exec(self, guest: GuestSession, command: str) -> None:
        await self._ensure_work_dir(guest)
        await self.ensure_sync_flush(guest, why=f"mcp exec: {command[:32]}")

    async def run_on_guest(self, command: str, *, timeout_ms: int,
                           shell: str, cwd: str,
                           guest: "GuestSession | None" = None) -> dict:
        return await self.guest_exec.run_on_guest(
            command, timeout_ms=timeout_ms, shell=shell, cwd=cwd, guest=guest
        )


    async def ensure_sync_flush(self, guest: "GuestSession | None" = None,
                                why: str = "mcp command") -> dict:
        return await self.sync.ensure_sync_flush(guest, why=why)


    async def run_sync(self, why: str, *, wait: bool = False,
                       guest: "GuestSession | None" = None) -> dict:
        return await self.sync.run_sync(why, wait=wait, guest=guest)


    #: How many passes in a row may be refused for a self-fixing reason
    #: before it is announced anyway. Passes run about every thirty seconds
    #: and a handover resolves in one, so three is a minute and a half of
    #: patience -- long enough that no change announces itself, short enough
    #: that a root which genuinely cannot be made is still reported while the
    #: user remembers choosing it.
    TRANSIENT_ABORT_GRACE = 3

    def _base_path(self, guest: "GuestSession | None" = None) -> str:
        return self._ensure_sync().base_path(guest)

    def _current_guest_root(self, guest: "GuestSession | None" = None) -> str:
        return self._ensure_sync().current_guest_root(guest)

    def _load_baseline(self, guest: "GuestSession | None" = None):
        return self._ensure_sync().load_baseline(guest)

    def guest_work_root(self, guest: "GuestSession | None" = None) -> str:
        """Where a command runs, spelled the way the guest spells it.

        Reached from `agent/tools.py` to default `exec`'s cwd, which is why
        it stays on the composition root: the MCP closure is handed a Bridge
        on purpose (audit 04.1).
        """
        return self.workspace.guest_work_root(guest or self._guest)

    def sync_status_text(self, guest: "GuestSession | None" = None) -> str:
        return self._ensure_sync().sync_status_text(guest)

    def _edits_outside_the_mirror(
        self, guest: "GuestSession | None" = None, limit: int = 20
    ) -> tuple[list[str], bool]:
        return self._ensure_sync().edits_outside_the_mirror(guest, limit=limit)

    async def sync_resolve(self, path: str, action: str,
                           guest: "GuestSession | None" = None) -> str:
        return await self._ensure_sync().sync_resolve(path, action, guest=guest)


    def _fail_syncs(self, guest: GuestSession | None, why: str) -> None:
        self.sync.fail_syncs(guest, why)

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


    def _on_lost(self, guest: GuestSession) -> None:
        self._fail_syncs(guest, "The connected machine disconnected mid-sync.")
        self._fail_execs(
            guest,
            "The connected machine disconnected while this command was "
            "running. It may or may not have completed.",
        )


    def _fail_execs(self, guest: GuestSession | None, why: str) -> None:
        self.guest_exec.fail_execs(guest, why)


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
                self.connection.bind_codepages(guest)
                await self.run_sync("the bridge's own sweep")

    def _ensure_background_tasks(self, guest: GuestSession) -> None:
        if self._reaper is None:
            self._reaper = asyncio.create_task(self._reap_idle())
        if self._sweeper is None and self.sync_dir:
            self._sweeper = asyncio.create_task(self._sweep_sync())

    async def _reap_idle(self) -> None:
        await self.connection.reap_idle()


    async def _send_options(self, guest: GuestSession) -> None:
        await self.options.send_options(guest)


    async def _set_effort(self, guest: GuestSession, effort: str) -> None:
        await self.coordinator.set_effort(guest, effort)


    # --- sending -------------------------------------------------------
    #
    # The transport itself -- the writer, the send lock, the compressor, the
    # chunking -- lives in GuestLink (bridge/wire/link.py) now. These three
    # stay on Bridge, as thin delegations to guest.link, because both
    # syncrun.py's Pass and tests/python reach them by these names.

    async def _send(self, guest: GuestSession, frame: Frame) -> bool:
        return await self.connection.send(guest, frame)

    async def _send_html(self, guest: GuestSession, html: str, mode: str,
                         page_no: int) -> None:
        await guest.link.send_html(guest, html, mode, page_no)


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
    ap.add_argument("--model", default="sonnet",
                    help="model for sessions (default: sonnet)")
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

    try:
        config = BridgeConfig(
            secret=secret,
            host=args.host,
            port=args.port,
            cwd=args.cwd,
            model=args.model,
            effort=args.effort,
            link_bps=args.link_bps,
            sync_dir=args.sync_dir,
            sync_state=args.sync_state,
        )
        config.validate()
    except ValueError as exc:
        raise SystemExit(str(exc))

    if config.sync_state:
        os.makedirs(config.sync_state, exist_ok=True)
        log.info("sync: %s <-> guest, baseline in %s",
                 config.sync_dir, config.sync_state)

    bridge = Bridge(config=config)
    try:
        asyncio.run(bridge.serve_forever())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
