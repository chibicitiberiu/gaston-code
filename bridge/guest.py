"""`GuestSession`: one connected machine, and the conversation on it.

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
from typing import TYPE_CHECKING, Any

from .agent.session import Session
from .files.synctransport import GuestSyncState
from .status import GuestState
from .synccoord import SyncCoordinator
from .view.transcript import TranscriptView
from .wire.link import GuestLink

if TYPE_CHECKING:  # annotations only -- importing these for real is a cycle
    from .agent.permdesk import PermissionDesk
    from .agent.toolrules import Found
    from .files import caps as C

@dataclass
class ExecCall:
    """One outstanding EXEC, and what has happened to it so far."""

    future: asyncio.Future
    out: bytearray = field(default_factory=bytearray)
    killed: bool = False
    capped: bool = False
    #: Spawned with no shell because the machine reported none (not because
    #: the agent asked for `shell: "none"`). A built-in like `ver` then fails
    #: to start with nothing saying why -- BACKLOG 17.
    no_shell: bool = False
    #: Run through a wrapper batch file so an exit code could be recovered at
    #: all (`bridge/services/execwrap.py`). The flag and not the sentinel,
    #: because the sentinel is a module constant: what varies per call is
    #: whether to look for it, and looking for it when nothing wrote it would
    #: strip a line out of a program's real output.
    wrapped: bool = False


@dataclass
class FsCall:
    """One outstanding fs-lane request.

    `data` accumulates FSDAT bodies for a get and `rows` accumulates FSDRIVE
    records; a put fills neither and is resolved by its ack alone. Here rather
    than in the service for the same reason UiCall is: the table it lives in
    belongs to the session, and a service that owned the type would have the
    session importing it back.
    """

    kind: str                       # get | put | drives
    future: asyncio.Future
    data: bytearray = field(default_factory=bytearray)
    rows: list = field(default_factory=list)


@dataclass
class UiCall:
    """One outstanding desktop request.

    `records` accumulates UIENT bodies and `data` accumulates UIDAT ones --
    a tree and a screenshot come back through the same id namespace and the
    same END frame, and the guest never sends both for one request.
    """

    future: asyncio.Future
    kind: str = "tree"
    records: bytearray = field(default_factory=bytearray)
    data: bytearray = field(default_factory=bytearray)
    capped: bool = False


@dataclass
class GuestSession:
    """Bridge-side state for one guest, surviving across reconnects."""

    #: Set when the SDK reports something during a turn that means the answer
    #: cannot be trusted as complete -- a RateLimitEvent today. Read and
    #: cleared by _run_turn_locked when it sends TURN end.
    #:
    #: The SDK reports ok=True for a rate-limited turn that streamed nothing,
    #: so without this the status bar says "done $?" beside a transcript
    #: notice saying the request may not have run.
    turn_degraded: str = ""
    #: Did the model actually produce anything this turn -- text or a tool
    #: call? Set from the SDK's own callbacks, so a notice the BRIDGE wrote
    #: (the sync preamble, a rate-limit warning) does not count as output.
    #: Reset when a turn starts.
    turn_produced: bool = False
    #: The user pressed Stop during this turn. The SDK then ends the turn
    #: with `error_during_execution`, which the guest rendered as "The
    #: request did not complete ... it may not have run at all" -- wrong on
    #: both counts after a deliberate Stop, since things ran and the user
    #: asked for it to end. Set by on_interrupt, reset when a turn starts.
    turn_stopped: bool = False
    #: The LAST finished turn was ended by Stop. Outlives the turn, unlike
    #: turn_stopped: Continue on a later sync halt reads it, so that the
    #: agent is not told to carry on with work the person stopped.
    last_turn_stopped: bool = False

    #: A stable per-connection handle. Set once, here, and never overwritten:
    #: nothing reads it but READY's header and logging, so a stale value is
    #: invisible until someone trusts it. The SDK's own id travels in
    #: SWITCHED and in the `init` system message.
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
    #: Empty means the shared one (`BridgeConfig.cwd`). When set it is a
    #: SUBFOLDER OF THE MIRROR, not a new mirror: one mirror, one baseline,
    #: one watcher (BACKLOG.md §7).
    work_dir: str = ""
    #: The same folder as the GUEST spells it -- `C:\Gaston\LISTFILE`.
    #:
    #: Empty until the conversation has been spoken in. The folder is named
    #: after the conversation, and the CLI titles a conversation from its
    #: first message, so anything chosen before then is a guess the user has
    #: to live with. The toolbar shows "<unset>" for exactly that long.
    work_guest: str = ""
    #: The folder NAME this conversation has settled on, with no root in
    #: front of it -- `LISTFILE`.
    #:
    #: Set either when the folder is committed or, earlier, when the system
    #: prompt asks where the folder is GOING to be: the prompt is built when
    #: the session is, and the folder is not created until the first tool
    #: call, so without this the prompt would name one candidate and
    #: `name_work_dir` would be free to pick another. The agent would be
    #: holding a path that never gets made, which with no mirror is the only
    #: path it has (`WorkspaceService.planned_guest_dir`).
    work_planned: str = ""
    #: The folder path the AGENT has been told to work in, as last told.
    #:
    #: Only used when nothing is mirrored, which is when that path is the
    #: only destination the agent is ever given
    #: (`TurnRunner.with_workdir_preamble`). Kept so the instruction goes in
    #: front of one turn rather than every one, and so a folder the user
    #: changes under the agent is said again.
    work_told: str = ""
    #: The first thing the user said in this conversation.
    #:
    #: Kept because the FOLDER IS NAMED FROM IT, and the folder is committed
    #: at the first TOOL CALL -- which can happen inside that same first
    #: turn, before the CLI has titled anything. Renaming a placeholder
    #: afterwards would mean a new tree_id and a new baseline for a tree sync
    #: has already scanned.
    first_prompt: str = ""
    #: A conversation the guest has OPENED but not yet spoken in.
    #:
    #: Switching chats replays stored history and builds no SDK client, so
    #: between the switch and the first message `claude` is None and this
    #: holds the id the client will resume from. Ask `conv_id`, not this.
    pending_resume: str = ""
    #: The conversation's EXECUTION MODE (bridge/agent/policy.py): a preset
    #: id or one of the client's own. Per guest for the same reason model and
    #: effort are: a switch, a new session and the effort fork all rebuild
    #: the Session, and a mode the user chose must not quietly revert
    #: because the object holding it was thrown away. The SDK's own
    #: permission mode is derived from it (`policy.Mode.sdk`), never stored.
    mode_id: str = "manual"
    #: The mode to return to when Plan first's plan is approved.
    mode_before_plan: str = ""
    #: "Allow for this chat" grants, `cat` or `cat:subject` (policy.py).
    #: Cleared on a switch and on a mode change.
    grants: set = field(default_factory=set)
    #: A reopened chat saved as the CLI's "auto", which did less on its own
    #: than this bridge's Auto does; said once, then cleared.
    legacy_auto: bool = False
    #: `policy.surface` of the live session: what it was told it has.
    surface: tuple | None = None
    #: The client's declared settings (bridge/prefs.py), replaced wholesale
    #: at every handshake and on every PREFS. None until the first attach.
    prefs: Any = None
    #: The person's last few prompts, newest last, for the Auto reviewer.
    recent_prompts: list = field(default_factory=list)
    #: Everything about writing bytes to this guest -- the socket writer, the
    #: send lock, the persistent compressor and the measured link speed.
    link: GuestLink = field(default_factory=GuestLink)
    #: The mirror root as the GUEST spells it, learned from SYNCEND rather
    #: than assumed. Per guest, not per process: it is a fact about one
    #: machine, and a second guest reading the first one's was a real leak.
    guest_sync_root: str | None = None
    #: What the last sync pass reported, for `sync_status_text` to repeat
    #: once. Cleared by the next pass.
    last_sync_reports: list[str] = field(default_factory=list)
    #: Consecutive passes refused for a reason that fixes itself. See the
    #: `transient` branch in `SyncService.run_sync`: `no_root` is the two
    #: sides mid-handover and resolves within a second, so it is counted
    #: rather than announced until it stops looking transient.
    transient_aborts: int = 0
    #: The halt bar's state and the agent's notice queue. One per GUEST, not
    #: one per bridge: once R7 lands, a shared one would mean agent A
    #: resolving its own conflict takes the bar down on B's screen and drains
    #: B's notices into A's next turn.
    synccoord: SyncCoordinator = field(default_factory=SyncCoordinator)
    #: `C.from_hello(hello)`, parsed once at attach rather than at every site
    #: that needs a path rule. A re-parse cannot raise, but one prior
    #: re-parse site fell back to `{}` when this guest was merely unset --
    #: guessing Win9x defaults instead of reading what the client declared.
    caps: C.Caps | None = None

    #: What that machine has installed, as HELLO's body said and the rules in
    #: bridge/agent/toolrules.py read it. Set once per CONNECTION, not once
    #: per session: every conversation opened on one connection sees the same
    #: inventory. A guest built before this existed sends no body and lands
    #: here as [], which the prompt renders as nothing at all.
    tools: list[Found] = field(default_factory=list)
    #: That machine's PATH, as it was at connect. Same lifetime as `tools`.
    path: list[str] = field(default_factory=list)

    #: What this guest is watching and how much of it fits: the paginator,
    #: the renderer, the per-stream coalescers, which page is on screen, and
    #: the two page-size limits scaled to this guest's RAM at handshake.
    view: TranscriptView = field(default_factory=TranscriptView)

    #: The prompt currently on the guest's screen, the ones queued behind it,
    #: and the state machine that owns both. Unlike `link`/`view`/`sync`
    #: there is no bare default: a `PermissionDesk` needs its owning guest
    #: and three of the bridge's own operations, so it is built and assigned
    #: in `PermissionService.attach` right after the `GuestSession` itself.
    perms: PermissionDesk | None = None
    #: When the guest was last heard from, for the idle reaper. A guest that
    #: GPFs leaves no FIN behind, so without this the connection parks
    #: forever and `connected` stays True -- rejecting every reconnect as
    #: "busy".
    last_seen: float = field(default_factory=time.monotonic)
    #: When the link dropped, or None while connected. A prompt outlives a
    #: brief disconnect so a reconnect can restore the bar.
    lost_at: float | None = None
    #: In-flight EXEC calls, keyed by our own id. Separate namespace from
    #: tool_use_id so a stray frame cannot cross-resolve against TOOLRESULT.
    exec_pending: dict[str, ExecCall] = field(default_factory=dict)
    exec_seq: int = 0
    #: One worker thread on the guest means one command at a time. The model
    #: can request tools in parallel, and an "Always" rule bypasses the
    #: prompt queue that was implicitly serialising them.
    exec_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    #: The desktop subprotocol, with its own id namespace: the guest checks
    #: the prefix (`win/rpc/rpc.cpp`'s id_matches_family), so a UI reply can
    #: never cross-resolve against an exec or a sync call. exec is `x`,
    #: sync is `s`, this is `u`.
    ui_pending: dict[str, UiCall] = field(default_factory=dict)
    ui_seq: int = 0
    #: One desktop worker on the guest means one request at a time.
    ui_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    #: Process list/kill, `p`-prefixed ids. Answered on the guest's reader
    #: thread, so no lock: nothing on that side queues them.
    proc_pending: dict[str, asyncio.Future] = field(default_factory=dict)
    proc_seq: int = 0
    #: In-flight fs-lane calls, keyed by `f`-prefixed id. A separate table
    #: from `sync.pending` even though both travel the guest's STREAM lane:
    #: they answer different futures, and the guest enforces the prefix
    #: split rather than relying on the two allocators never colliding.
    fs_pending: dict[str, FsCall] = field(default_factory=dict)
    fs_seq: int = 0
    #: The sync subprotocol's own id namespace, in-flight table, baseline
    #: lock and running flag.
    sync: GuestSyncState = field(default_factory=GuestSyncState)
    #: One turn at a time, per guest.
    #:
    #: Two turns on one session is not "two prompt bars". The SDK stream has
    #: a single consumer, so two iterators get a DISJOINT split of messages:
    #: one turn can consume the other's ResultMessage and return with its
    #: cost while the other's text lands in the wrong callbacks. The
    #: transcript itself is corrupted, and `Session._busy` is a flag rather
    #: than a depth, so the first `finally` clears it and Stop stops nothing.
    #:
    #: Reachable through the DESIGNED conflict flow, not a race: Continue is
    #: not gated on a live turn, and the agent writing files is what raises
    #: the halt in the first place.
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

    @property
    def machine(self) -> str:
        """WHICH machine this is -- the guest's own id, from HELLO.

        Not `sess_id`, which is minted here and lasts one connection, and
        not the hostname, which is `WIN98` on every fresh install. The guest
        mints a GUID on first run and keeps it beside the exe
        (`win/app/machineid.h`), so it survives reboots, reconnects and
        bridge restarts.

        "" when the client does not send one -- any build from before
        2026-10-01, or one whose id file could not be written. Everything
        that reads this has to treat "" as "do not narrow", because the
        alternative is a client that silently loses its own chat list
        (BACKLOG.md §21).
        """
        return self.hello.get("machine", "")

    def work_root_on_guest(self) -> str:
        return work_root_on_guest(self)

    def on_guest(self, path: str) -> str | None:
        """`path` made absolute against work_root_on_guest(), or None.
        See bridge/files/paths.py on_guest.

        One more spelling is understood: the agent's own mirror of this
        conversation, as a path on the proxy. It runs in that folder, so
        `/tmp/.../work/CONV/x.txt` is what it naturally writes for
        `C:\\WORK\\CONV\\x.txt` -- and it is mapped to exactly that. Any
        other POSIX path is the proxy's and comes back None."""
        import ntpath
        import posixpath

        from .files.paths import on_guest
        p = (path or "").strip()
        if p.startswith("/"):
            wd, wg = self.work_dir, self.work_guest
            if not wd or not wg:
                return None
            rp, root = posixpath.normpath(p), posixpath.normpath(wd)
            if rp != root and not rp.startswith(root + "/"):
                return None
            rel = posixpath.relpath(rp, root)
            return wg if rel == "." else ntpath.normpath(
                wg.rstrip("\\") + "\\" + rel.replace("/", "\\"))
        return on_guest(p, self.work_root_on_guest())

    def set_link_speed(self, bps: float | None) -> None:
        """Propagate a measured (or CLI-default) link speed to every live
        coalescer, so a conversation already streaming picks it up too, not
        only ones started after.

        Spans two groups (the link's speed, the view's coalescers) and so
        lives here rather than on either `GuestLink` or `TranscriptView`.
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

    @property
    def state(self) -> GuestState:
        """Derived state — a projection over existing fields.

        Priority: DISCONNECTED > HALTED > RUNNING_TURN > IDLE.

        The axes are orthogonal (turn-running, halt-raised, permission-
        pending and exec-in-flight can all be true simultaneously), so
        this is an observational projection, not an authoritative gate.
        """
        if not self.link.connected:
            return GuestState.DISCONNECTED
        if self.synccoord.halt_id:
            return GuestState.HALTED
        if self.turn_lock.locked():
            return GuestState.RUNNING_TURN
        return GuestState.IDLE

    @property
    def state_detail(self) -> str:
        """Which combination of turn/halt/perm/exec is active, for log lines.

        Captures the orthogonal axes the single `state` enum cannot.
        """
        parts = []
        if not self.link.connected:
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
        if self.sync.active:
            parts.append("sync-active")
        return ", ".join(parts) or "idle"


def work_root_on_guest(g) -> str:
    r"""Where a conversation works on the machine, spelled the guest's way:
    its own folder once committed, else the shared root. A function, not
    only a method, so a service handed any guest-shaped object can ask."""
    if g.work_guest:
        return g.work_guest
    return ((g.hello or {}).get("sync_root") or g.guest_sync_root or "")
