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
from .services.options import (
    COMMAND_MENU,
    EFFORT_LABEL,
    EFFORT_ORDER,
    MODE_CAVEAT,
    MODE_LABEL,
    MODE_ORDER,
    OptionsService,
)
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


class GuestState(Enum):
    """Derived guest state — a projection over existing fields.

    This is a COMPUTED PROPERTY, not a stored field.  It is derived from
    ``turn_lock``, ``synccoord.halt_id``, ``perms.pending``,
    ``exec_pending`` and ``writer``/``lost_at``.

    Used for logging and diagnostics ONLY.  **No gating** —
    gating inbound frames on this enum is intentionally avoided: the
    RESUME flow (which starts a turn AND lifts a halt in one operation)
    must not be blocked by a naive single-state gate.

    The axes are orthogonal — turn-running, halt-raised, permission-
    pending and exec-in-flight can all be true at the same time.  This
    enum is a projection, not a replacement.
    """
    IDLE = auto()
    RUNNING_TURN = auto()
    HALTED = auto()
    DISCONNECTED = auto()


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
    HELLO_TIMEOUT: float = 10.0
    STRAY_SCAN_BUDGET: int = SyncService.STRAY_SCAN_BUDGET

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
        self.presentation = PresentationService(connection=self)
        self.options = OptionsService(
            connection=self,
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
            connection=self,
            spawn=self._spawn,
            pre_exec=self._pre_exec,
        )
        self.permissions = PermissionService(
            connection=self,
            presentation=self.presentation,
            workspace=self.workspace,
            spawn=self._spawn,
        )
        self.sync = SyncService(
            config=self.config,
            connection=self,
            workspace=self.workspace,
            presentation=self.presentation,
            spawn=self._spawn,
        )
        self.attachment = AttachmentService(
            connection=self,
            workspace=self.workspace,
            sync=self.sync,
        )
        self.session = SessionService(
            connection=self,
            presentation=self.presentation,
            workspace=self.workspace,
            options=self.options,
            guest_exec=self.guest_exec,
            conv=self.conv,
            spawn=self._spawn,
        )
        self.turn_runner = TurnRunner(
            config=self.config,
            connection=self,
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
            connection=self,
            turn_runner=self.turn_runner,
            sync=self.sync,
            permission=self.permissions,
            session=self.session,
            workspace=self.workspace,
            guest_exec=self.guest_exec,
            spawn=self._spawn,
        )

        # One guest, one session. See PLAN.md "Multi-session".
        self._guest: GuestSession | None = None
        self._server: asyncio.Server | None = None
        # Strong references to fire-and-forget tasks. asyncio holds only a
        # weak one, so a bare create_task can be collected mid-flight.
        self._tasks: set = set()
        # Frames pipelined behind HELLO in a single read, handed to _pump.
        self._reaper: asyncio.Task | None = None
        self._sweeper: asyncio.Task | None = None
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

    def _ensure_config(self) -> BridgeConfig:
        if not hasattr(self, "config"):
            self.config = BridgeConfig(secret="")
        return self.config

    def _ensure_sync(self) -> SyncService:
        if not hasattr(self, "sync"):
            cfg = self._ensure_config()
            conv = getattr(self, "conv", None) or Conversations()
            self.conv = conv
            pres = getattr(self, "presentation", None) or PresentationService(connection=self)
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
                connection=self,
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
    def secret(self) -> str:
        return self._ensure_config().secret

    @secret.setter
    def secret(self, val: str) -> None:
        self._ensure_config().secret = val

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
    def _models_cache(self) -> list[dict] | None:
        return self.options._models_cache

    @_models_cache.setter
    def _models_cache(self, val: list[dict] | None) -> None:
        self.options._models_cache = val

    @property
    def _commands_cache(self) -> list[dict] | None:
        return self.options._commands_cache

    @_commands_cache.setter
    def _commands_cache(self, val: list[dict] | None) -> None:
        self.options._commands_cache = val

    async def send(self, guest: GuestSession, frame: Frame) -> bool:
        return await self._send(guest, frame)

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
            # 02.6 Background tasks must be created after _attach binds ContextVars
            assert guest is not None and guest.caps is not None

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
            async with asyncio.timeout(self.HELLO_TIMEOUT):
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
            # EVERY refusal is logged, by name. Two were, and the rest closed
            # the socket in silence -- which stopped being theoretical the
            # moment PROTO_VERSION first moved: a reflashed bridge refused a
            # guest that had not been, the guest showed `[bad_version] bridge
            # speaks 2, guest speaks 1` on its own screen, and the bridge's
            # log said nothing at all. The side being debugged is usually
            # this one.
            log.warning("refused %s: %s (%s)",
                        writer.get_extra_info("peername"),
                        exc.code, exc.message)
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
            self._guest.zip_in.reset()

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
            self.permissions.attach(self._guest, cwd=self.cwd)
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
        return self.options.effective_model(guest)

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

    def _stored_work_dir(self, session_id: str) -> str:
        return self.workspace.stored_work_dir(session_id)

    def _retarget_perms(self, guest: GuestSession) -> None:
        self.workspace.retarget_perms(guest)

    def _mirror_dir(self, guest: GuestSession) -> str:
        return self.workspace.mirror_dir(guest)

    def _work_dir(self, guest: GuestSession) -> str:
        return self.workspace.work_dir(guest)

    def _validate_attach_target(self, guest: GuestSession, attach: str) -> str | None:
        return self.attachment.validate_attach_target(guest, attach)

    def _remember_settings(self, guest: GuestSession,
                           session_id: str = "") -> None:
        self.options.remember_settings(guest, session_id)

    def _restore_settings(self, guest: GuestSession, session_id: str) -> None:
        self.session.restore_settings(guest, session_id)

    async def _ensure_work_dir(self, guest: GuestSession) -> None:
        await self.workspace.ensure_work_dir(guest)

    async def _name_work_dir(self, guest: GuestSession) -> None:
        await self.workspace.name_work_dir(guest)

    @staticmethod
    def _sep_of(root: str) -> str:
        return P.sep_of(root)

    def _guest_path(self, guest: GuestSession, name: str) -> str:
        return self.workspace.guest_path(guest, name)

    def guest_work_root(self, guest: GuestSession) -> str:
        return self.workspace.guest_work_root(guest)

    def _sub_of(self, guest: GuestSession) -> str:
        return self.workspace.sub_of(guest)

    def _effective_effort(self, guest: GuestSession) -> str:
        return self.options.effective_effort(guest)

    def _abandon_pending(self, guest: GuestSession) -> None:
        self.session.abandon_pending(guest)

    async def _resume_pending(self, guest: GuestSession) -> bool:
        return await self.session.resume_pending(guest)

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

    async def _send_sessions(self, guest: GuestSession) -> None:
        await self.session.send_sessions(guest)

    async def _reissue_halt(self, guest: GuestSession) -> None:
        await self.coordinator.reissue_halt(guest)

    async def _reissue_turn(self, guest: GuestSession) -> None:
        await self.coordinator.reissue_turn(guest)

    async def sync_state_to_guest(self, guest: GuestSession) -> None:
        await self.coordinator.sync_state_to_guest(guest)

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
        return self.presentation.coalescer(guest, style)

    async def _emit_text(self, guest: GuestSession, text: str,
                         *, style: str) -> None:
        await self.presentation.emit_text(guest, text, style=style)

    async def _flush_text(self, guest: GuestSession) -> None:
        await self.presentation.flush_text(guest)

    async def _write_updates(self, guest: GuestSession,
                             updates: list) -> None:
        await self.presentation.write_updates(guest, updates)

    async def _write_block(self, guest: GuestSession, html: str,
                           *, stream: bool = True,
                           notify: bool = True) -> None:
        await self.presentation.write_block(guest, html, stream=stream, notify=notify)

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
        if f.headers.get("enc") == "deflate":
            try:
                body = guest.link.zip_in.decompress(f.body)
            except Exception as e:
                log.error("cannot inflate frame %s: %s", f.type, e)
                await self._send(guest, error("corrupt_frame", f.type))
                return
            new_headers = {k: v for k, v in f.headers.items() if k != "enc"}
            f = Frame(f.type, new_headers, body)
        name = self._DISPATCH.get(f.type)
        if name is None:
            log.warning("unknown frame type from guest: %s", f.type)
            await self._send(guest, error("unknown_type", f.type))
            return
        await getattr(self, name)(guest, f)

    async def _on_input(self, guest: GuestSession, f: Frame) -> None:
        await self.coordinator.on_input(guest, f)

    async def _on_resume(self, guest: GuestSession, f: Frame) -> None:
        await self.coordinator.on_resume(guest, f)

    async def _on_syncdirty(self, guest: GuestSession, f: Frame) -> None:
        # The guest noticed its tree move. It never reconciles anything
        # itself -- the bridge owns the baseline and decides.
        self._spawn(self.run_sync("the guest reported a change"))

    async def _on_interrupt(self, guest: GuestSession, f: Frame) -> None:
        await self.coordinator.on_interrupt(guest, f)

    async def _on_getsessions(self, guest: GuestSession, f: Frame) -> None:
        await self.session.on_getsessions(guest, f)

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
        try:
            def_bps = float(f.headers.get("deflate_bps", "0"))
        except ValueError:
            def_bps = 0.0
        if def_bps > 0:
            guest.link.deflate_bps = def_bps
            log.info("guest reports deflate %.0f B/s", def_bps)

    async def _on_ping(self, guest: GuestSession, f: Frame) -> None:
        await self._send(guest, pong())

    async def _on_newsession(self, guest: GuestSession, f: Frame) -> None:
        await self._new_session(guest, f.headers.get("folder", ""))

    async def _on_setmodel(self, guest: GuestSession, f: Frame) -> None:
        await self.options.on_setmodel(guest, f)

    async def _on_seteffort(self, guest: GuestSession, f: Frame) -> None:
        await self.options.on_seteffort(guest, f)

    async def _on_setmode(self, guest: GuestSession, f: Frame) -> None:
        await self.options.on_setmode(guest, f)

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
        await self.workspace.on_setwork(guest, f)

    async def _on_export(self, guest: GuestSession, f: Frame) -> None:
        await self.session.on_export(guest, f)

    def _conv_title(self, guest: GuestSession) -> str:
        return self.session.conv_title(guest)

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
        self.turn_runner.seed_agent_notices()

    async def _with_sync_preamble(self, text: str) -> str:
        return await self.turn_runner.with_sync_preamble(text)

    async def _clear_agent_notices(self, delivered: list) -> None:
        await self.turn_runner.clear_agent_notices(delivered)

    async def _resume_turn(self, guest: GuestSession, answered) -> None:
        await self.turn_runner.resume_turn(guest, answered)

    async def _run_turn(self, guest: GuestSession, text: str,
                        *, echo: bool = True,
                        attach: str | None = None,
                        attach_id: str | None = None,
                        attach_name: str | None = None) -> None:
        await self.turn_runner.run_turn(
            guest, text, echo=echo, attach=attach,
            attach_id=attach_id, attach_name=attach_name,
        )

    async def _run_turn_locked(self, guest: GuestSession, text: str,
                               *, echo: bool = True,
                               attach: str | None = None,
                               attach_id: str | None = None,
                               attach_name: str | None = None) -> None:
        await self.turn_runner.run_turn_locked(
            guest, text, echo=echo, attach=attach,
            attach_id=attach_id, attach_name=attach_name,
        )

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

    def _kill_exec(self, guest: GuestSession, exec_id: str) -> None:
        self.guest_exec.kill_exec(guest, exec_id)

    async def ensure_sync_flush(self, guest: "GuestSession | None" = None,
                                why: str = "mcp command") -> dict:
        return await self.sync.ensure_sync_flush(guest, why=why)

    async def _set_status_override(self, guest: "GuestSession", text: str) -> None:
        await self.sync.set_status_override(guest, text)

    async def run_sync(self, why: str, *, wait: bool = False,
                       guest: "GuestSession | None" = None) -> dict:
        return await self.sync.run_sync(why, wait=wait, guest=guest)

    async def _report_abort(self, guest: GuestSession, reason: str, *,
                            bug: bool = False) -> None:
        await self.sync.report_abort(guest, reason, bug=bug)

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

    def sync_status_text(self, guest: "GuestSession | None" = None) -> str:
        return self._ensure_sync().sync_status_text(guest)

    def _edits_outside_the_mirror(
        self, guest: "GuestSession | None" = None, limit: int = 20
    ) -> tuple[list[str], bool]:
        return self._ensure_sync().edits_outside_the_mirror(guest, limit=limit)

    async def sync_resolve(self, path: str, action: str,
                           guest: "GuestSession | None" = None) -> str:
        return await self._ensure_sync().sync_resolve(path, action, guest=guest)

    def _sync_resolve_locked(
        self, path: str, action: str, guest: "GuestSession | None" = None
    ) -> str:
        return self._ensure_sync().sync_resolve_locked(path, action, guest=guest)

    def _baseline_lock(self, guest: "GuestSession | None" = None):
        return self._ensure_sync().baseline_lock(guest)

    async def _release_all_latches(self, only=None) -> int:
        return await self.sync.release_all_latches(only=only)

    async def _raise_halt(self, guest: GuestSession, line: str,
                          paths=()) -> None:
        await self.sync.raise_halt(guest, line, paths=paths)

    async def _clear_halt(self, guest: GuestSession) -> None:
        await self.sync.clear_halt(guest)

    async def _reconcile_halt(self, guest: GuestSession, base=None) -> None:
        await self.sync.reconcile_halt(guest, base=base)

    async def _sync_frame(self, guest: GuestSession, f: Frame) -> None:
        await self.sync.sync_frame(guest, f)

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

    async def _exec_out(self, guest: GuestSession, f: Frame) -> None:
        await self.guest_exec.exec_out(guest, f)

    async def _exec_result(self, guest: GuestSession, f: Frame) -> None:
        await self.guest_exec.exec_result(guest, f)

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
        self.guest_exec.fail_execs(guest, why)

    async def _perm_reply(self, guest: GuestSession, f: Frame) -> None:
        """Answer from the guest's prompt bar."""
        await self.permissions.reply(guest, f)

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
        return await self.options._models(guest)

    async def _commands(self, guest: GuestSession) -> list[dict]:
        return await self.options._commands(guest)

    async def _send_options(self, guest: GuestSession) -> None:
        await self.options.send_options(guest)

    async def _quiesce_turn(self, guest: GuestSession) -> None:
        await self.coordinator.quiesce_turn(guest)

    async def _set_effort(self, guest: GuestSession, effort: str) -> None:
        await self.coordinator.set_effort(guest, effort)

    async def _new_session(self, guest: GuestSession,
                           folder: str = "") -> None:
        await self.coordinator.new_session(guest, folder=folder)

    async def _switch(self, guest: GuestSession, session_id: str) -> None:
        await self.coordinator.switch(guest, session_id)

    async def _set_effort_locked(self, guest: GuestSession,
                                 effort: str) -> None:
        await self.session.set_effort_locked(guest, effort)

    async def _set_mode(self, guest: GuestSession, mode: str) -> None:
        await self.options.set_mode(guest, mode)

    async def _new_session_locked(self, guest: GuestSession,
                                  folder: str = "") -> None:
        await self.session.new_session_locked(guest, folder=folder)

    async def _switch_locked(self, guest: GuestSession,
                             session_id: str) -> None:
        await self.session.switch_locked(guest, session_id)

    async def _send_page(self, guest: GuestSession, want: str) -> None:
        await self.presentation.send_page(guest, want)

    def mark_user_turn(self, guest: GuestSession, line: str) -> Any:
        return self.presentation.mark_user_turn(guest, line)

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

    async def send_html(self, guest: GuestSession, html: str, mode: str,
                        page_no: int) -> None:
        await guest.link.send_html(guest, html, mode, page_no)

    async def wire_yield(self, guest: GuestSession) -> None:
        await guest.link.wire_yield(guest)

    async def _try_send_raw(self, writer: asyncio.StreamWriter,
                            frame: Frame) -> None:
        await GuestLink.send_raw(writer, frame)





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
