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
import getpass
import logging
import os
import tempfile

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
from .services.guest_fs import GuestFsService
from .services.options import (
    MODE_ORDER,
    OptionsService,
)
from .services.permission import PermissionService
from .services.presentation import PresentationService
from .services.proc import ProcService
from .services.session import SessionService
from .services.sync import SyncService
from .services.turn_runner import TurnRunner, turn_verdict
from .services.ui import UiService
from .services.workspace import WorkspaceService
from .synccoord import SyncCoordinator
from .view import html as H
from .view.html import APPEND
from .view.pages import Transcript
from .view.transcript import TranscriptView
from .wire.protocol import (
    Frame,
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
    "MODE_ORDER",
    "Bridge",
    "ExecCall",
    "Found",
    "Frame",
    "GuestSession",
    "H",
    "Session",
    "Transcript",
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
            spawn_sync=lambda reason, guest: self._spawn(
                self.sync.run_sync(reason, guest=guest)),
        )
        self.ui = UiService(connection=self.connection)
        self.proc = ProcService(connection=self.connection)
        self.guest_fs = GuestFsService(connection=self.connection)
        # AFTER guest_fs, because it takes it: the exit-code wrapper is a file
        # written through the fs lane (bridge/services/execwrap.py).
        self.guest_exec = GuestExecService(
            connection=self.connection,
            spawn=self._spawn,
            pre_exec=self._pre_exec,
            fs=self.guest_fs,
        )
        async def mode_changed(g):
            # The desk changed the mode itself (Plan first approved):
            # remember it for the chat and put the tick where it now is.
            self.options.remember_settings(g)
            await self.options.send_options(g)

        self.permissions = PermissionService(
            connection=self.connection,
            presentation=self.presentation,
            workspace=self.workspace,
            spawn=self._spawn,
            guest_fs=self.guest_fs,
            on_mode=mode_changed,
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
            ui=self.ui,
            proc=self.proc,
            guest_fs=self.guest_fs,
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
            "UIENT":        self.ui.ui_frame,
            "UIDAT":        self.ui.ui_frame,
            "UIEND":        self.ui.ui_frame,
            "PSRESULT":     self.proc.proc_frame,
            "PSKILLRESULT": self.proc.proc_frame,
            "PSLAUNCHED":   self.proc.proc_frame,
            "FSDAT":        self.guest_fs.fs_frame,
            "FSDATEND":     self.guest_fs.fs_frame,
            "FSPUTACK":     self.guest_fs.fs_frame,
            "FSDRIVE":      self.guest_fs.fs_frame,
            "FSDRIVEEND":   self.guest_fs.fs_frame,
            "FSENT":        self.guest_fs.fs_frame,
            "FSENTEND":     self.guest_fs.fs_frame,
            "FSOPEND":      self.guest_fs.fs_frame,
            "PERMREPLY":    self.permissions.reply,
            "GETSESSIONS":  self.session.on_getsessions,
            "GETBLOCKS":    self.presentation.on_getblocks,
            "STAT":         self.connection.on_stat,
            "PING":         self.connection.on_ping,
            "SETMODEL":     self.options.on_setmodel,
            "SETEFFORT":    self.options.on_seteffort,
            "SETMODE":      self.options.on_setmode,
            "EXPORT":       self.session.on_export,
            "RULES":        self.permissions.on_rules,
            "PREFS":        self.options.on_prefs,
            "GETLISTS":     self.permissions.on_getlists,
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
        # EVERY GUEST. This closed `the` guest's session, which was all
        # of them when there was one slot.
        for guest in list(self.connection._guests.values()):
            if guest.claude:
                await guest.claude.close()

    # --- connection --------------------------------------------------------

    def _create_guest(self) -> GuestSession:
        # ONE COORDINATOR EACH. Every guest was handed the SAME object --
        # the one that exists before any guest does -- which was free while
        # only one could attach and is the failure synccoord.py's own
        # docstring warns about the moment two can: "agent A resolving its
        # own conflict takes the bar down on B's screen and drains B's
        # notices into A's next turn".
        #
        # The FIRST guest adopts the pre-attach one, so a notice recorded
        # before anyone connected still reaches a screen. Every guest after
        # that gets its own, because that queue has already been claimed and
        # a second machine has no business receiving it.
        pre = self.connection._preattach_synccoord
        g = GuestSession(
            view=TranscriptView(pages=Transcript()),
            synccoord=pre if not self.connection._guests else SyncCoordinator(),
        )
        self.permissions.attach(g, cwd=self.config.cwd)
        return g

    def _conversation_to_reopen(self, guest: GuestSession,
                                is_reconnect: bool) -> str:
        """The conversation a FRESH attach should come back to, or "".

        A client keeps the conversation id READY gave it and sends it back
        in HELLO as `conv`. This bridge only knows its own guest by memory,
        so after a bridge restart every client used to land in a new, empty
        conversation with nothing said -- its open chat simply gone from
        the pane (measured on the Athlon, 2026-10-03). Only an id this
        bridge lists for its working directory is taken; anything else is
        a stranger and a new conversation is right.
        """
        if is_reconnect:
            return ""
        conv = str(guest.hello.get("conv", "") or "").strip()
        if not conv or conv == guest.conv_id:
            return ""
        # The chat list's own rule (SessionService.ours), not a listing of
        # --cwd alone: a conversation's session runs in its own folder under
        # --sync-dir, which a one-directory listing never finds.
        try:
            rows = self.session.ours(
                self.session._resolve_sesslist().listing(), guest)
        except Exception as exc:          # a listing failure costs the resume
            log.info("could not list conversations to reopen %s: %s",
                     conv, exc)
            return ""
        return conv if any(r.get("id") == conv for r in rows) else ""

    async def _post_attach(self, guest: GuestSession, is_reconnect: bool) -> None:
        reopen = self._conversation_to_reopen(guest, is_reconnect)
        if guest.claude is None and not guest.pending_resume and not reopen:
            guest.claude = await self.session.make_session(guest)

        await self.connection.send(guest, ready(
            guest.sess_id,
            (guest.claude.model if guest.claude else guest.model)
            or "(default)",
            conv=guest.conv_id))

        await self.options.send_options(guest)

        guest.lost_at = None
        await self.coordinator.sync_state_to_guest(guest)
        await self.sync.reconcile_halt(guest)

        log.info("attached sess=%s (reconnect=%s)", guest.sess_id, is_reconnect)

        if reopen:
            log.info("reopening conversation %s, which the machine was in",
                     reopen)
            async with guest.turn_lock:
                await self.session.switch_locked(guest, reopen)
                # NOW, not at the first message as a switch from the list
                # does. The options batch -- models, mode, and the work
                # folder the guest's sync root comes from -- is only sent
                # once a client exists or the model list is cached, and a
                # bridge that has just started has neither: the reopened
                # conversation sat with sync on "no_root" and stale menus
                # (measured on the Athlon, 2026-10-03). A fresh attach
                # starts its session here too, so this costs nothing new.
                if await self.session.resume_pending(guest):
                    await self.options.send_options(guest)

        # AFTER the reopen. Its options batch is what names the folder the
        # guest mirrors, and the wire is ordered: a scan sent before it found
        # the guest with no root ("no_root") and waited out a retry
        # (2026-10-03). Sent after, the guest has applied the folder first.
        if self.config.sync_dir:
            self.turn_runner.seed_agent_notices(guest)
            self._spawn(self.sync.run_sync("the machine reconnected",
                                          guest=guest))


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
        self.ui.fail_calls(
            guest,
            "The connected machine disconnected while this was running.",
        )
        self.proc.fail_calls(guest)
        self.guest_fs.fail_calls(guest)


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
    ap = argparse.ArgumentParser(description="Gaston Code bridge")
    ap.add_argument("--host", default="127.0.0.1",
                    help="bind address; use the retro-segment interface, not 0.0.0.0")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--cwd", default=None, help="working directory for Claude")
    ap.add_argument("--model", default="sonnet",
                    help="model for sessions (default: sonnet)")
    ap.add_argument("--effort", default=None, choices=[None, "low", "medium",
                                                       "high", "xhigh", "max"])
    # Deliberately separate from --cwd, and never defaulted TO THE LAUNCH
    # DIRECTORY -- that would point sync at the whole repo, .venv and
    # vm/*.qcow2 included. It IS defaulted, to a directory of its own under
    # the system temp dir: see `default_sync_root`.
    ap.add_argument("--sync-dir", default=None,
                    help="the tree mirrored to the guest (default: a "
                         "directory under the system temp dir)")
    ap.add_argument("--no-sync", action="store_true",
                    help="do not mirror anything, not even the default temp "
                         "tree; files reach the guest only by put_file")
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


def default_sync_root() -> str:
    r"""Where the mirrored tree lives when nobody named one.

    SYNC IS ON WHENEVER THERE IS A WORK DIRECTORY, which is the design and
    not a convenience: a conversation's folder is created on the guest the
    first time a tool runs, and from that moment the two sides are supposed
    to converge. A bridge started without `--sync-dir` used to leave the
    guest half of that arrangement in place and the bridge half missing --
    the client made `C:\WORK\<title>`, named it in the prompt, and nothing
    was mirrored into it. Every file then had to travel by `put_file`, which
    writes bytes with no opinion about line endings, so a batch file written
    on this side did not run on that side (docs/measurements/
    batch-eol-and-dir-b-p200-20261001.txt). The sync layer converts; the
    one-off path does not. Having the default be "no mirror" meant the
    cheaper, lossier path was what you got by accident.

    STABLE, NOT RANDOM. `mkdtemp` would be a new tree on every restart, so
    the baseline would be thrown away and every conversation would re-sync
    from nothing. One path per user, reused, so a bridge restart picks up
    where it left off.

    Under the system temp dir because these are scratch trees for a machine
    that is being talked to, not a place to keep work -- say so, loudly, at
    startup, because a reboot of THIS host takes them.
    """
    base = os.path.join(tempfile.gettempdir(), "gaston-%s" % getpass.getuser())
    return os.path.join(base, "work")


def load_config(args: argparse.Namespace) -> BridgeConfig:
    """Command line plus environment, validated, or `SystemExit`.

    The secret is the one thing that is not an argument: a shared secret on a
    command line is readable by every process on the box.
    """
    secret = os.environ.get("GASTON_SECRET")
    if not secret:
        secret = "dev-secret"
        log.warning("GASTON_SECRET unset; using %r (development only)",
                    secret)
    # `--no-sync` is the only way to get the old behaviour, and it has to be
    # asked for: see `default_sync_root` for why "off" is the wrong default.
    sync_dir = args.sync_dir
    if not sync_dir and not args.no_sync:
        # A named --cwd is mirrored AS IT STANDS. The agent's file tools
        # operate there, and a mirror somewhere else would leave every edit
        # on this side -- which is the exact arrangement this default exists
        # to end. The old warning was about defaulting to the LAUNCH
        # directory, which nobody chose; --cwd is chosen.
        sync_dir = args.cwd or default_sync_root()
        log.info("no --sync-dir given; mirroring %s%s", sync_dir,
                 "" if args.cwd else " (under the system temp dir, so a "
                                     "reboot of THIS host takes it)")

    config = BridgeConfig(
        secret=secret,
        host=args.host,
        port=args.port,
        # The agent's file tools operate on `cwd`; if that is not the
        # mirrored tree then everything it writes stays on this side. When
        # neither was named, they are the same directory and the prompt's
        # "the whole working directory is mirrored" branch is the true one.
        cwd=args.cwd or sync_dir,
        model=args.model,
        effort=args.effort,
        link_bps=args.link_bps,
        sync_dir=sync_dir,
        sync_state=args.sync_state,
    )
    try:
        config.validate()
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    return config


def prepare_state(config: BridgeConfig) -> None:
    """Make what the baseline needs before anything can write to it."""
    if config.sync_dir:
        # The mirror too, not just the baseline: a default under the temp dir
        # does not exist on a fresh boot, and the first scan of a missing
        # tree is not a useful error message.
        os.makedirs(config.sync_dir, exist_ok=True)
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
