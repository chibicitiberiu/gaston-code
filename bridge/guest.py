"""`GuestSession`: one connected machine, and the conversation on it.

The model, not the server. It lived in `server.py` because that is where it
grew, and the cost was paid by every service: each one carried

    if TYPE_CHECKING:
        from ..server import GuestSession

because importing it for real would have been a cycle -- `server` builds the
services, so a service importing `server` closes the loop. Eleven modules
deferred an import to describe their own parameter. Here there is no cycle and
the import is ordinary.

WHAT SCOPE MEANS HERE, since two live on this object. `GuestSession` is the
MACHINE: it survives a reconnect, and holds the link, the capabilities, the
detected tools, the in-flight execs and the sync state. `view` is the
CONVERSATION: transcript, paginator, coalescers. Switching chats replaces the
second and keeps the first.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field

from .agent import permissions as perms
from .agent.session import Session
from .files.synctransport import GuestSyncState
from .status import GuestState
from .synccoord import SyncCoordinator
from .view.html import HtmlStream
from .view.pages import Paginator
from .view.transcript import TranscriptView
from .wire.compress import StreamCompressor
from .wire.link import GuestLink

@dataclass
class ExecCall:
    """One outstanding EXEC, and what has happened to it so far."""

    future: asyncio.Future
    out: bytearray = field(default_factory=bytearray)
    killed: bool = False
    capped: bool = False


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

    # Was a separate `GuestIdentity` with a forwarding property per
    # field. Its own docstring said why that was wrong: "nothing here
    # is its own actor with a lock or an RPC namespace the way
    # Link/View/Sync/Prompts are ... moved onto its own type only so
    # GuestSession stops being the thing that holds every group's
    # fields directly". Ten fields, no methods, one consumer, and a
    # relay each so nothing had to change shape -- an abstraction that
    # named nothing, paid for twice.
    #: A stable per-connection handle. Set once, here, and never overwritten
    #: again -- it used to be re-pointed at whichever Claude session happened
    #: to be live: "a bridge handle" on a fresh connection, "the SDK session"
    #: after a new-session or an effort fork, and simply stale after a
    #: switch, which never touched it at all. Nothing reads it for anything
    #: but READY's header and logging, which is exactly why it was free to
    #: drift (docs/audit/02-bridge-python.md #02.24) -- the SDK's own id
    #: already travels in SWITCHED and in the `init` system message.
    sess_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    #: The machine facts from HELLO, kept so every session this guest opens
    #: gets the same description of the computer it is acting for.
    hello: dict = field(default_factory=dict)
    #: The live Claude Agent SDK session, or None before the first one is
    #: built.
    claude: Session | None = None
    #: Chosen from the Model and Effort menus. Held per guest rather than per
    #: process so a reconnect or an effort fork keeps the choice.
    model: str = ""
    effort: str = ""
    #: This conversation's own working folder, absolute, on the BRIDGE.
    #:
    #: Empty means the shared one (`Bridge.cwd`), which is what every
    #: conversation used before per-chat folders existed and is still what a
    #: chat created without naming one gets. When set it is a SUBFOLDER OF
    #: THE MIRROR, not a new mirror: one mirror, one baseline, one watcher
    #: (BACKLOG.md §7) -- the conversation moves inside the tree rather than
    #: the tree moving.
    work_dir: str = ""
    #: The same folder as the GUEST spells it -- `C:\Gaston\LISTFILE`.
    #:
    #: Empty until the conversation has been spoken in. A folder is named
    #: after the conversation, and a conversation has no name until the CLI
    #: titles it from the first message, so anything chosen before then would
    #: be a guess the user would have to live with. The toolbar shows
    #: "<unset>" for exactly that long.
    work_guest: str = ""
    #: The first thing the user said in this conversation.
    #:
    #: Kept because the FOLDER IS NAMED FROM IT. A folder is named after the
    #: conversation, and the CLI titles a conversation from its first message
    #: -- but the folder is committed at the first TOOL CALL, which can
    #: happen inside that same first turn, before any title exists. The
    #: alternative was a placeholder folder renamed afterwards, which is a
    #: new tree_id and a new baseline for a tree sync has already scanned.
    first_prompt: str = ""
    #: A conversation the guest has OPENED but not yet spoken in.
    #:
    #: Switching chats replays stored history and builds no SDK client, so
    #: between the switch and the first message `claude` is None and this
    #: holds the id the client will resume from. `conv_id` is the question
    #: "which conversation is this" that everything else should ask, because
    #: the answer lives in one of these two places depending on a state the
    #: caller does not care about.
    pending_resume: str = ""
    #: The permission mode the next session is built with, and the one the
    #: live client is currently in. Per guest for the same reason model and
    #: effort are: a switch, a new session and the effort fork all rebuild
    #: the Session, and a mode the user chose must not quietly revert to
    #: "default" because the object holding it was thrown away.
    perm_mode: str = "default"
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
        return self.pending_resume


    # --- forwarded to self.perms, for the same reason as self.link above ----


    # --- forwarded to self.view, for the same reason as self.link above -----


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
        self.link.link_bps = bps
        for c in self.view.coalescers.values():
            c.set_link_speed(bps)


    @property
    def perm_pending(self):
        """The prompt on screen, or None -- including when there is no desk.

        NOT a forward. `perms` is `PermissionDesk | None`: a guest has none
        between construction and `PermissionService.attach`, and the reaper
        can look at one inside that window. This turns a two-step reach that
        can raise into a question with a defined answer, which is the only
        reason a property here earns its place.
        """
        return self.perms.pending if self.perms else None

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
