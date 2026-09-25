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

import argparse
import asyncio
import logging
import os

from .agent import permissions as perms
from .agent import sessions as sesslist
from .agent.conversations import Conversations
from .agent.session import Session
from .agent.toolrules import Found
from .files import paths as P
from .guest import ExecCall, GuestSession
from .services.attachment import AttachmentService
from .services.config import BridgeConfig
from .services.connection import ConnectionService
from .services.coordinator import SessionCoordinator
from .services.guest_exec import GuestExecService
from .services.options import (
    MODE_LABEL,
    MODE_ORDER,
    OptionsService,
)
from .services.permission import PermissionService
from .services.presentation import PresentationService
from .services.session import SessionService
from .services.sync import SyncService
from .services.turn_runner import TurnRunner, turn_verdict
from .services.workspace import WorkspaceService
from .view import html as H
from .view.html import APPEND
from .view.pages import Paginator
from .view.transcript import TranscriptView
from .wire.protocol import (
    Frame,
    doc,
    error,
    notice,
    ready,
)

#: Re-exported on purpose. These are not used inside this module -- tests and
#: tools import them from here because this is where the bridge's public
#: surface has always been, and naming them keeps a lint pass from deciding
#: they are dead.
__all__ = [
    "APPEND",
    "MODE_LABEL",
    "MODE_ORDER",
    "Bridge",
    "ExecCall",
    "Found",
    "Frame",
    "GuestSession",
    "H",
    "Paginator",
    "Session",
    "TranscriptView",
    "init_logging",
    "load_config",
    "main",
    "notice",
    "parse_args",
    "prepare_state",
    "serve",
    "sesslist",
    "turn_verdict",
]

#: `Session` and `sesslist` are reached through this module BY NAME rather
#: than imported from it: `SessionService._resolve_session_cls` asks
#: `hasattr(sys.modules["bridge.server"], "Session")` so a test can swap the
#: SDK session for a fake, and tests monkeypatch `srv.sesslist.listing`. A
#: lint pass called them unused imports -- which they are -- and removing them
#: took out 248 tests at once. That is what this list is protecting.

log = logging.getLogger("bridge.server")

DEFAULT_PORT = 7890


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
        self.presentation = PresentationService(connection=self.connection)
        self.options = OptionsService(
            connection=self.connection,
            conv=self.conv,
            config=self.config,
            write_block=self.presentation.write_block,
            set_effort=lambda g, e: self.coordinator.set_effort(g, e),
        )
        self.workspace = WorkspaceService(
            config=self.config,
            conv=self.conv,
            write_block=self.presentation.write_block,
            send_options=self.options.send_options,
            spawn_sync=lambda reason: self._spawn(self.sync.run_sync(reason)),
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

        self._server: asyncio.Server | None = None
        # Strong references to fire-and-forget tasks. asyncio holds only a
        # weak one, so a bare create_task can be collected mid-flight.
        self._tasks: set = set()
        self._reaper: asyncio.Task | None = None
        self._sweeper: asyncio.Task | None = None


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


    # --- lifecycle ---------------------------------------------------------

    #: Config that is reachable only as `bridge.config.<name>`. Assigning
    #: `bridge.sync_dir = x` is otherwise a silent no-op -- the attribute
    #: lands on the Bridge, `config` keeps its old value, and nothing says
    #: so. `__setattr__` below turns that into an error at the mistake.
    _CONFIG_FIELDS = frozenset((
        "cwd", "effort", "host", "idle_check_s", "idle_timeout_s", "link_bps",
        "model", "perm_grace_s", "port", "sync_dir", "sync_state",
        "sync_sweep_s", "secret", "hello_timeout_s",
    ))

    def __setattr__(self, name: str, value) -> None:
        if name in self._CONFIG_FIELDS:
            raise AttributeError(
                f"{name!r} is configuration, not state on the Bridge: set "
                f"`bridge.config.{name}` instead. Assigning it here would "
                f"shadow the config silently and nothing would read it.")
        object.__setattr__(self, name, value)

    async def serve_forever(self) -> None:
        self._server = await asyncio.start_server(
            self.connection.on_client, self.config.host, self.config.port
        )
        addr = ", ".join(str(s.getsockname()) for s in self._server.sockets or [])
        log.info("listening on %s", addr)
        async with self._server:
            await self._server.serve_forever()

    async def close(self) -> None:
        # BOTH long-lived loops, and everything _spawn started. Missing
        # either one leaves a task looping against a guest whose writer is
        # gone -- harmless out of process, a leak in the test suite and in
        # any future embedding.
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
        if self.connection.guest and self.connection.guest.claude:
            await self.connection.guest.claude.close()

    # --- connection --------------------------------------------------------

    def _create_guest(self, page_bytes: int, page_blocks: int) -> GuestSession:
        g = GuestSession(
            view=TranscriptView(pages=Paginator(page_bytes, page_blocks)),
            synccoord=self.connection._preattach_synccoord,
        )
        self.permissions.attach(g, cwd=self.config.cwd)
        return g

    async def _post_attach(self, guest: GuestSession, is_reconnect: bool) -> None:
        if guest.claude is None and not guest.pending_resume:
            guest.claude = await self.session.make_session(guest)

        await self.connection.send(guest, ready(
            guest.sess_id,
            (guest.claude.model if guest.claude else guest.model)
            or "(default)",
            guest.view.pages.live.number, guest.view.pages.count,
            conv=guest.conv_id))

        await self.connection.send(guest, doc(H.document()))
        await self.options.send_options(guest)

        guest.lost_at = None
        await self.coordinator.sync_state_to_guest(guest)
        await self.sync.reconcile_halt(guest)

        if self.config.sync_dir:
            self.turn_runner.seed_agent_notices()
            self._spawn(self.sync.run_sync("the machine reconnected"))

        log.info("attached sess=%s (reconnect=%s)", guest.sess_id, is_reconnect)


    # --- the conversation's own folder -------------------------------------
    #
    # A SUBFOLDER OF THE MIRROR, never a second mirror (BACKLOG.md §7).
    # Moving the sync root instead would mean a new baseline, a new
    # `tree_id`, a watcher restart and a recovery pass -- four chances to
    # report deletions nobody made, in the one layer that can lose a person's
    # work. Inside the mirror gets each chat its own folder with one
    # baseline and one watcher, and makes everything the agent can write
    # mirrored by construction.

    #: A folder name is read by a person, in a title bar and a status line, so
    #: it is capped for legibility as well as by the filesystem.
    FOLDER_MAX_LONG = P.FOLDER_MAX_LONG
    folder_max = staticmethod(P.folder_max)
    folder_name = staticmethod(P.folder_name)
    _title_folder = staticmethod(P.title_folder)


    def _spawn(self, coro) -> None:
        """Fire-and-forget, with a reference kept AND the result looked at.

        asyncio only holds a weak reference to a running task, so a bare
        create_task can be garbage-collected mid-flight.

        Everything fire-and-forget on this bridge goes through here, and the
        reason it must is `_done`: without it a raise produces no log line at
        the moment it happens, only asyncio's "Task exception was never
        retrieved" whenever the task is collected. A turn dying mid-flight is
        a TURN end frame that never goes out and a Send button that stays
        disabled until the link is re-established.
        """
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._done)

    def _done(self, task: asyncio.Task) -> None:
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


    # --- frame pump --------------------------------------------------------


    async def _dispatch_frame(self, guest: GuestSession, f: Frame) -> None:
        handler = self._dispatch.get(f.type)
        if handler is None:
            log.warning("unknown frame type from guest: %s", f.type)
            await self.connection.send(guest, error("unknown_type", f.type))
            return
        await handler(guest, f)


    # --- running commands on the guest -------------------------------------

    async def _pre_exec(self, guest: GuestSession, command: str) -> None:
        await self.workspace.ensure_work_dir(guest)
        await self.sync.ensure_sync_flush(guest, why=f"mcp exec: {command[:32]}")


    #: How many passes in a row may be refused for a self-fixing reason
    #: before it is announced anyway. Passes run about every thirty seconds
    #: and a handover resolves in one, so three is a minute and a half of
    #: patience -- long enough that no change announces itself, short enough
    #: that a root which genuinely cannot be made is still reported while the
    #: user remembers choosing it.
    TRANSIENT_ABORT_GRACE = 3


    #: The bridge's own ceiling on captured output -- the guest caps at 32KB
    #: and this is the same number with room to spare. It exists because
    #: nothing else here trusts a length a guest supplies: a client that
    #: lied could otherwise grow this buffer at 64KB a frame for as long as
    #: the call has left to run.
    EXEC_OUT_MAX = 64 * 1024

    #: The guest's own EXEC_MAX_CMD (win/rpc/exec.cpp, currently 1024),
    #: named here only so the message shown to the model is a grep away from
    #: the number it quotes. NOT enforced before sending: the guest prepends
    #: its own shell prefix, whose length this side does not know, so a
    #: bridge-side pre-check would reject commands that would have fit. The
    #: guest's `too_long` refusal is the authoritative one.
    EXEC_MAX_CMD = 1024


    def _on_lost(self, guest: GuestSession) -> None:
        self.sync.fail_syncs(guest, "The connected machine disconnected mid-sync.")
        self.guest_exec.fail_execs(
            guest,
            "The connected machine disconnected while this command was "
            "running. It may or may not have completed.",
        )


    def _ensure_background_tasks(self, guest: GuestSession) -> None:
        if self._reaper is None:
            self._reaper = asyncio.create_task(self.connection.reap_idle())
        if self._sweeper is None and self.config.sync_dir:
            self._sweeper = asyncio.create_task(self.sync.sweep_sync())


def _summarise(args: dict) -> str:
    """One readable line for the tool-activity pane.

    `perms.telling_argument` is the one place "which argument matters" is
    decided; do not grow a second list here.
    """
    told = perms.telling_argument(args)
    if told:
        key, value = told
        return f"{key}: {value[:160]}"
    return ", ".join(f"{k}={str(v)[:40]}" for k, v in list(args.items())[:3])


# --- lifecycle --------------------------------------------------------------
#
# Named phases rather than one function, because the order is the design and a
# flat `main` hides it. Each does one thing and nothing before it needs to have
# happened except what the signature asks for -- so `load_config` can be called
# by a test, and `serve` can be called by anything that already has a config.


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
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
    return ap.parse_args(argv)


def init_logging(verbose: bool = False) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def load_config(args: argparse.Namespace) -> BridgeConfig:
    """Command line plus environment, validated, or `SystemExit`.

    The secret is the one thing that is not an argument: a shared secret on a
    command line is readable by every process on the box.
    """
    secret = os.environ.get("RETROCLAUDE_SECRET")
    if not secret:
        secret = "dev-secret"
        log.warning("RETROCLAUDE_SECRET unset; using %r (development only)",
                    secret)
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
    try:
        config.validate()
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    return config


def prepare_state(config: BridgeConfig) -> None:
    """Make what the baseline needs before anything can write to it."""
    if config.sync_state:
        os.makedirs(config.sync_state, exist_ok=True)
        log.info("sync: %s <-> guest, baseline in %s",
                 config.sync_dir, config.sync_state)


async def serve(config: BridgeConfig) -> None:
    """Build the services, serve, and shut down -- always in that order.

    THE `finally` IS THE POINT. `close()` cancels the reaper and the
    sweeper, cancel-gathers everything `_spawn` started, closes the listener
    and closes the agent session. It had no caller outside the test suite:
    `main` ran `serve_forever` and swallowed KeyboardInterrupt, so on Ctrl+C
    none of that ran and the process simply died.
    """
    bridge = Bridge(config=config)
    try:
        await bridge.serve_forever()
    finally:
        await bridge.close()


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    init_logging(args.verbose)
    config = load_config(args)
    prepare_state(config)
    try:
        asyncio.run(serve(config))
    except KeyboardInterrupt:
        log.info("interrupted")


if __name__ == "__main__":
    main()
