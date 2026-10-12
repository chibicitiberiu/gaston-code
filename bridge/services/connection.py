"""ConnectionService: TCP admission, HELLO handshake, codepage binding, and frame pump.

Owns the transport and connection lifecycle for a GuestSession:
connection admission (on_client), HELLO negotiation and deadline (read_hello),
session attachment and reconnect recovery (attach), Windows codepage mapping
(bind_codepages, oem_for), raw frame transmission (try_send_raw), framing pump
and inflation (pump, handle), idle reaping (reap_idle), and disconnect marking (mark_lost).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import sys
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from .. import handshake
from .. import prefs as prefs_mod
from ..agent import policy, profiles, toolrep, toolrules
from ..guest import GuestSession
from ..synccoord import SyncCoordinator
from ..view.transcript import TranscriptView
from ..wire import fold
from ..wire.link import GuestLink
from ..wire.protocol import Frame, FrameReader, ProtocolError, error, pong

if TYPE_CHECKING:
    from .config import BridgeConfig

log = logging.getLogger("bridge.connection")


class ConnectionService:
    """Manages connection admission, HELLO negotiation, framing pump, and idle reaping."""

    def __init__(
        self,
        config: BridgeConfig,
        create_guest: Callable[[], Any] | None = None,
        on_attach: Callable[[Any, bool], Any] | None = None,
        handle_frame: Callable[[Any, Frame], Any] | None = None,
        on_lost: Callable[[Any], None] | None = None,
        ensure_background_tasks: Callable[[Any], None] | None = None,
        spawn: Callable[[Any], None] | None = None,
    ) -> None:
        self.config = config
        self.create_guest = create_guest
        self.on_attach = on_attach
        self.handle_frame = handle_frame
        self.on_lost = on_lost
        self.ensure_background_tasks = ensure_background_tasks
        self.spawn = spawn
        self._warned_ambiguous = False
        #: EVERY GUEST, keyed by the machine id it declared in HELLO.
        #:
        #: This was one slot, and one slot is why a second machine
        #: connecting after the first had gone silently inherited its
        #: session: `is_reconnect` asked whether a GuestSession existed,
        #: never whose it was, so attach replaced the link, caps, prefs and
        #: tools and left the live conversation, work folder, grants,
        #: permission desk and transcript belonging to the previous
        #: machine. Both of this project's machines talk to one bridge, so
        #: that was reachable rather than theoretical.
        #:
        #: The key is `machine`, which is an UNAUTHENTICATED ASSERTION and
        #: is accepted as one -- `docs/BACKLOG.md` §7 and audit 04.21 asked
        #: for that in writing rather than a per-client secret bolted on
        #: later. There is one shared secret and it already grants
        #: everything; a client claiming another's id inherits its
        #: transcript and its pending prompts, exactly as before.
        self._guests: dict[str, GuestSession] = {}
        #: The order they last attached in, so `guest` has a defined answer
        #: when there is more than one. See the property.
        self._order: list[str] = []
        #: Where the halt bar and the agent-notice queue live until a guest
        #: attaches and brings its own. A forward, not storage -- see the
        #: `synccoord` property below; D5 deletes this.
        self._preattach_synccoord = SyncCoordinator()

    # --- the one-guest view, which is temporary -----------------------------
    #
    # Nearly two hundred call sites across the bridge and the suite reach for
    # `connection.guest`, and turning them all into explicit guests in the
    # same commit as the dict would be one unreviewable change. So the
    # property stays until D6 and has a DEFINED answer for more than one:
    # the guest that attached most recently.
    #
    # That answer is right for nothing in particular. It is right for the
    # single-guest case, which is every case until a second machine connects,
    # and it is wrong in a way that is at least deterministic for the four
    # paths that still use it -- reap_idle, synccoord, Bridge.close and
    # SyncService._get_guest. D2 makes the loops iterate and D6 deletes the
    # property; until then, a bridge with two guests logs which one it
    # guessed the first time it has to guess.

    @property
    def guest(self) -> GuestSession | None:
        if not self._order:
            return None
        if len(self._order) > 1 and not self._warned_ambiguous:
            self._warned_ambiguous = True
            log.warning(
                "%d guests attached and something asked for `the` guest; "
                "answering with the most recent (%s). Until D6 removes this "
                "property, that is a guess with a defined value rather than "
                "a correct answer.",
                len(self._order), self._order[-1] or "(no id)")
        return self._guests.get(self._order[-1])

    @guest.setter
    def guest(self, g: GuestSession | None) -> None:
        """Only the tests assign this, and only to install or clear one."""
        self._guests.clear()
        self._order.clear()
        if g is not None:
            key = g.machine
            self._guests[key] = g
            self._order.append(key)

    def _forget(self, key: str) -> None:
        """Drop a guest from the registry and close what it was holding."""
        g = self._guests.pop(key, None)
        if key in self._order:
            self._order.remove(key)
        if g is not None and g.claude is not None and self.spawn:
            self.spawn(g.claude.close())

    def _remember(self, key: str, g: GuestSession) -> None:
        self._guests[key] = g
        if key in self._order:
            self._order.remove(key)
        self._order.append(key)

    @property
    def synccoord(self) -> SyncCoordinator:
        g = self.guest
        return g.synccoord if g is not None else self._preattach_synccoord

    @staticmethod
    def oem_for(guest: GuestSession) -> str:
        """Which codepage this machine's console children write."""
        caps = guest.caps
        shell = toolrules.shell_of(guest.tools)
        return (
            getattr(caps, "oemcp", "")
            or (
                str(profiles.shell(shell).get("oem_codepage", ""))
                if shell
                else ""
            )
        )

    def bind_codepages(self, guest: GuestSession) -> None:
        """Bind this guest's display and console codepages in THIS context."""
        caps = guest.caps
        if caps is None:
            return
        fold.use(caps.acp)
        oem = self.oem_for(guest)
        if oem:
            fold.use(oem=oem)

    @staticmethod
    async def try_send_raw(writer: asyncio.StreamWriter, frame: Frame) -> None:
        """Send raw uncompressed frame over socket stream."""
        await GuestLink.send_raw(writer, frame)

    @staticmethod
    async def send(guest: GuestSession, frame: Frame) -> bool:
        """Send frame through guest link."""
        return await guest.link.send(guest, frame)

    async def read_hello(
        self,
        reader: asyncio.StreamReader,
        frames: FrameReader,
        writer: asyncio.StreamWriter,
    ) -> tuple[Frame | None, list[Frame]]:
        """First frame must be HELLO, within a short deadline.

        Returns (hello, pipelined).
        """
        timeout_s = self.config.hello_timeout_s
        try:
            async with asyncio.timeout(timeout_s):
                while True:
                    data = await reader.read(4096)
                    if not data:
                        return None, []
                    batch = frames.feed(data)
                    for i, f in enumerate(batch):
                        if f.type != "HELLO":
                            await self.try_send_raw(
                                writer,
                                error(
                                    "expected_hello",
                                    f"first frame was {f.type}",
                                ),
                            )
                            return None, []
                        return f, batch[i + 1 :]
        except TimeoutError:
            await self.try_send_raw(
                writer, error("hello_timeout", "no HELLO within 10s")
            )
            return None, []

    async def read_prefs(
        self,
        reader: asyncio.StreamReader,
        frames: FrameReader,
        writer: asyncio.StreamWriter,
        early: list,
    ) -> tuple[Frame | None, list]:
        """The PREFS frame a client that declared `prefs` sends next, from
        what came in with HELLO or from the socket, under the same deadline.
        It MUST be the next frame: settings that arrive after the session
        is built arrive too late for the tools it was told about."""
        if early:
            if early[0].type == "PREFS":
                return early[0], early[1:]
            await self.try_send_raw(writer, error(
                "expected_prefs", f"frame after HELLO was {early[0].type}"))
            return None, []
        try:
            async with asyncio.timeout(self.config.hello_timeout_s):
                while True:
                    data = await reader.read(4096)
                    if not data:
                        return None, []
                    batch = frames.feed(data)
                    if not batch:
                        continue
                    if batch[0].type != "PREFS":
                        await self.try_send_raw(writer, error(
                            "expected_prefs",
                            f"frame after HELLO was {batch[0].type}"))
                        return None, []
                    return batch[0], batch[1:]
        except TimeoutError:
            await self.try_send_raw(
                writer, error("prefs_timeout", "no PREFS after HELLO"))
            return None, []

    async def attach(
        self, hello: Frame, writer: asyncio.StreamWriter,
        prefs_frame: Frame | None = None,
    ) -> GuestSession | None:
        """Negotiate HELLO handshake and attach/recover guest session."""
        h = hello.headers

        try:
            neg = handshake.negotiate(h, self.config.secret)
        except handshake.Refused as exc:
            log.warning(
                "refused %s: %s (%s)",
                writer.get_extra_info("peername"),
                exc.code,
                exc.message,
            )
            await self.try_send_raw(writer, error(exc.code, exc.message))
            return None

        # The client's settings, parsed with ITS codepage, refused like a
        # bad cap: a list cut short in silence is a place quietly
        # unprotected.
        client_prefs = prefs_mod.DEFAULT
        if prefs_frame is not None:
            try:
                client_prefs = prefs_mod.parse(prefs_frame.body or b"",
                                               neg.caps.acp)
            except prefs_mod.PrefsError as exc:
                log.warning("refused %s: bad prefs (%s)",
                            writer.get_extra_info("peername"), exc)
                await self.try_send_raw(writer,
                                        error("bad_prefs", str(exc)[:200]))
                return None

        # WHICH MACHINE IS CONNECTING. "" is a client from before
        # 2026-10-01 and means "do not narrow" for the CHAT LIST -- but it
        # cannot identify a connection, so here it is a key like any other
        # and two id-less clients collide on it deliberately. The second one
        # is refused below, because the bridge cannot tell them apart and
        # the thing it would otherwise do is hand the second one the first
        # one's conversation, which is the bug this keying exists to close.
        machine = (hello.headers.get("machine") or "").strip()

        # REJECT, DON'T EVICT -- narrowed from "a guest is connected" to
        # "THIS MACHINE is connected" (audit 04.21). A second machine is now
        # a second guest rather than a refusal; a second window on the same
        # machine is still refused, which is what the client's own
        # single-instance mutex already enforces at the other end.
        held = self._guests.get(machine)
        if held is not None and held.link.connected:
            # NAME THE MACHINE. "another client holds this session" with no
            # id is unanswerable from a log when there are two machines and
            # the question is which one collided.
            log.warning("refused %s: %s is already attached",
                        writer.get_extra_info("peername"),
                        machine or "a client with no machine id")
            await self.try_send_raw(
                writer, error("busy", "another client holds this session")
            )
            return None
        if not machine and self._guests and machine not in self._guests:
            # Unreachable while "" is a key like any other; kept because the
            # two branches say different things and a future key scheme that
            # makes them differ should fail loudly here.
            await self.try_send_raw(
                writer, error("busy", "a client with no machine id is "
                                      "already attached")
            )
            return None

        caps = neg.caps
        # The page size used to be worked out here, from HELLO's mem_kb and
        # the client's view.page preference, and handed to a Paginator.
        # Stage 3b moved both halves to the client and did not replace them
        # with a declaration: a window is whatever the GETBLOCKS in front of
        # the bridge asks for, and what it is asking of is a list.
        log.info("guest has %d MB", neg.mem_kb // 1024)
        log.info("client rules: %s", caps.summary())
        if client_prefs.declared:
            log.info("client settings: %d protected, %d dangerous, %d "
                     "trusted, %d own modes, default mode %s",
                     len(client_prefs.lists.protect),
                     len(client_prefs.lists.danger),
                     len(client_prefs.lists.trust), len(client_prefs.modes),
                     client_prefs.general_mode or "(bridge's)")
        # WHICH machine, so "why is my chat list empty" and "why am I seeing
        # the other box's chats" are both one grep away. An absent id is a
        # client from before 2026-10-01, and the wide list that comes with
        # it; say so rather than printing nothing, because nothing reads as
        # a bridge that did not look (BACKLOG.md §21).
        log.info(
            "guest identifies as %s",
            h.get("machine")
            or "(no id -- it sees every conversation this bridge started)",
        )
        # BOTH HALVES, OR NEITHER. This used to print the guest's declared
        # share and stop, which is how one log came to hold
        #     guest mirrors C:\\WORK
        # and, two minutes later and from the same connection,
        #     no folder is mirrored from this machine
        # with nothing joining them. Both lines were true: the guest had been
        # configured with a share, and this bridge had not been given
        # `--sync-dir`, so nothing was being copied. The operator could not
        # see the disagreement, and neither could the agent, which put a file
        # in the root of C: instead. The agent is told now (machine.toml's
        # `no_sync_declared_folder`); this is the same sentence for the
        # operator.
        #
        # "AND NO CONVERSATION FOLDER WILL BE CREATED UNDER IT" is gone from
        # the warning, because it stopped being true the same day: the client
        # makes the conversation's folder under its own work root whether or
        # not this end mirrors anything. What is missing with no `--sync-dir`
        # is the copying, which is what this now says.
        declared = h.get("sync_root") or ""
        if not declared:
            log.info(
                "guest declared no work root, so this bridge will not name a "
                "folder on it for any conversation -- every file reaching it "
                "goes to a path the agent asks the user for")
        elif self.config.sync_dir:
            log.info("guest mirrors %s", declared)
        else:
            log.warning(
                "guest declared %s as its work root, but this bridge was "
                "started with no --sync-dir: each conversation still gets its "
                "own folder under it, made by the client, and NOTHING is "
                "mirrored -- every file reaching that machine will be a "
                "one-off put_file with no baseline and no staged copy behind "
                "it. Pass --sync-dir if the two were meant to agree.",
                declared)

        # A RECONNECT IS THIS MACHINE COMING BACK, not "a GuestSession
        # exists". That distinction is the whole of the bug: with one slot,
        # a different machine attaching after the first had gone was a
        # reconnect, so it kept `claude`, work_dir, grants, mode_id, perms,
        # sess_id and the transcript of a conversation it had never had.
        guest = self._guests.get(machine)
        is_reconnect = guest is not None
        if guest is not None:
            guest.link.zip_out.reset()
            guest.link.zip_in.reset()
        else:
            if self.create_guest:
                guest = self.create_guest()
            else:
                mod = sys.modules.get("bridge.server")
                guest_cls = getattr(mod, "GuestSession", None) if mod else None
                if guest_cls:
                    guest = guest_cls(view=TranscriptView())

        if guest is None:
            log.error("cannot attach: no GuestSession could be instantiated")
            return None
        self._remember(machine, guest)

        guest.link.writer = writer
        # HERE, and not alongside the page limits in options.py, which is the
        # trap the stage-3 plan named: limits_for has TWO production callers
        # -- this one and the PREFS handler -- and PREFS is the user's
        # settings, which have no business overwriting what the CLIENT
        # declared it can render. Page size is a preference; how much of a
        # tool result the machine can show is a fact about the machine.
        guest.view.toolmax = caps.toolmax
        guest.hello = dict(h)
        guest.caps = caps
        fold.use(caps.acp)
        # WHOLESALE, every handshake: a reconnect is the client's current
        # answer, and a client that sends none gets the defaults -- never
        # the previous client's lists and modes (plan review, B2).
        guest.prefs = client_prefs
        if not is_reconnect:
            # A FIRST connection starts in the client's default mode; a
            # reconnect keeps the open chat's own.
            guest.mode_id = policy.default_mode(guest)

        _facts = toolrep.parse_rows(hello.body or b"", caps)
        guest.tools = toolrules.inventory(_facts)
        _shell = toolrules.shell_of(guest.tools)
        guest.path = toolrules.path_dirs(_facts, _shell)

        self.bind_codepages(guest)
        if guest.tools:
            log.info(
                "machine has: %s",
                ", ".join(f"{f.name} ({f.path})" for f in guest.tools),
            )
        if not toolrules.usable_shells(guest.tools):
            log.warning(
                "machine reported no shell: every exec will be spawned bare "
                "(a client run without its tools_detect.ini does this)")

        guest.set_link_speed(self.config.link_bps)

        if self.on_attach:
            await self.on_attach(guest, is_reconnect)

        return guest

    async def handle(self, guest: GuestSession, f: Frame) -> None:
        """Handle incoming frame, performing inflation if compressed."""
        guest.last_seen = time.monotonic()
        if f.headers.get("enc") == "deflate":
            try:
                body = guest.link.zip_in.decompress(f.body)
            except Exception as e:
                log.error("cannot inflate frame %s: %s", f.type, e)
                await self.send(guest, error("corrupt_frame", f.type))
                return
            new_headers = {k: v for k, v in f.headers.items() if k != "enc"}
            f = Frame(f.type, new_headers, body)

        if self.handle_frame:
            await self.handle_frame(guest, f)

    async def send_records(self, guest: GuestSession, body: bytes,
                           mode: str, total: int | None = None) -> None:
        await guest.link.send_records(guest, body, mode, total)

    async def wire_yield(self, guest: GuestSession) -> None:
        await guest.link.wire_yield(guest)

    async def on_ping(self, guest: GuestSession, f: Frame) -> None:
        await self.send(guest, pong())

    async def on_stat(self, guest: GuestSession, f: Frame) -> None:
        """The guest measures its own throughput for the progress UI.

        The sender uses it to pick a codec. This is what makes one build work
        on both a 10Mbit LAN and a modem.
        """
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

    async def pump(
        self,
        guest: GuestSession,
        reader: asyncio.StreamReader,
        frames: FrameReader,
        early: list,
    ) -> None:
        """Read loop delivering parsed frames to handle()."""
        for f in early:
            await self.handle(guest, f)
        while True:
            data = await reader.read(8192)
            if not data:
                return
            for f in frames.feed(data):
                await self.handle(guest, f)

    def mark_lost(self, guest: GuestSession) -> None:
        """Record that this guest's link is gone. Idempotent."""
        if guest.lost_at is None:
            guest.lost_at = time.monotonic()
        if self.on_lost:
            self.on_lost(guest)
        if guest.perm_pending is not None:
            log.info(
                "link lost with a prompt pending; holding %.0fs",
                getattr(self.config, "perm_grace_s", 60.0),
            )

    async def reap_idle(self) -> None:
        """Background loop: reap guests that have stopped talking."""
        idle_check_s = getattr(self.config, "idle_check_s", 15.0)
        perm_grace_s = getattr(self.config, "perm_grace_s", 60.0)
        idle_timeout_s = getattr(self.config, "idle_timeout_s", 90.0)
        evict_after_s = getattr(self.config, "evict_after_s", 3600.0)
        while True:
            await asyncio.sleep(idle_check_s)
            # EVERY GUEST, not the one. A list() because eviction below
            # mutates the registry.
            for key, guest in list(self._guests.items()):
                self._reap_one(key, guest, perm_grace_s, idle_timeout_s,
                               evict_after_s)

    def _reap_one(self, key: str, guest: GuestSession, perm_grace_s: float,
                  idle_timeout_s: float, evict_after_s: float) -> None:
        """One guest's turn through the reaper. Split out when the loop
        learned to iterate, so the body did not grow a level of indent and
        a `continue` did not quietly become a `return` with a different
        meaning."""
        # ONE GUEST'S CODEPAGE, bound before anything writes a frame for
        # it. `deny_all` below builds blocks and sends them, and `fold`
        # keeps the codepage in a ContextVar -- so without this a prompt
        # denied on guest B is folded through whichever guest attached
        # first in this task's context.
        self.bind_codepages(guest)
        if not guest.link.connected:
            # `perms is not None` is implied by `perm_pending is not
            # None` -- the property returns None without a desk -- but
            # saying it is what lets a checker see it too.
            if (
                guest.perms is not None
                and guest.perm_pending is not None
                and guest.lost_at is not None
                and time.monotonic() - guest.lost_at > perm_grace_s
            ):
                guest.perms.deny_all(
                    "gone",
                    "The Windows machine disconnected before anyone could "
                    "answer, so this was not approved. It was not refused "
                    "on purpose -- ask again if you still need it.",
                )
            # AND EVENTUALLY IT GOES. Nothing ever removed an entry
            # while there was one slot: the session was deliberately
            # kept so a reconnect reattached it, and one retained
            # GuestSession cost nothing. Keyed on the machine id it is
            # one retained session, SDK subprocess and permission desk
            # PER MACHINE ID EVER SEEN -- and ids do churn, because
            # machineid.cpp re-mints over a malformed machine.id.
            if (guest.lost_at is not None
                    and time.monotonic() - guest.lost_at > evict_after_s):
                log.info("guest %s has been gone %.0fs; forgetting it",
                         key or "(no id)", time.monotonic() - guest.lost_at)
                self._forget(key)
            return
        if guest.exec_pending or guest.sync.active:
            return
        idle = time.monotonic() - guest.last_seen
        if idle < idle_timeout_s:
            return
        log.warning(
            "guest silent for %.0fs; closing so it can reconnect", idle
        )
        if guest.perms is not None:
            guest.perms.deny_all(
                "idle", "The Windows machine stopped responding."
            )
        w, guest.link.writer = guest.link.writer, None
        self.mark_lost(guest)
        try:
            if w is not None:
                w.close()
        except Exception:
            pass

    async def on_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        """Top-level socket server connection handler."""
        peer = writer.get_extra_info("peername")
        log.info("connection from %s", peer)
        frames = FrameReader()

        try:
            hello, early = await self.read_hello(reader, frames, writer)
            if hello is None:
                return

            prefs_frame = None
            if hello.headers.get("prefs", "0") not in ("", "0"):
                # ONLY WHEN DECLARED. Waiting for a frame a client never
                # sends would hold every older client for the whole hello
                # deadline (plan review, B9).
                prefs_frame, early = await self.read_prefs(
                    reader, frames, writer, early)
                if prefs_frame is None:
                    return

            guest = await self.attach(hello, writer, prefs_frame)
            if guest is None:
                return

            assert guest is not None and guest.caps is not None

            if self.ensure_background_tasks:
                self.ensure_background_tasks(guest)

            await self.pump(guest, reader, frames, early)

        except ProtocolError as exc:
            log.warning("protocol error from %s: %s", peer, exc)
            await self.try_send_raw(writer, error("protocol", str(exc)))
        except (ConnectionResetError, BrokenPipeError):
            log.info("%s dropped", peer)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("unhandled error serving %s", peer)
        finally:
            guest = self.guest
            if guest and (guest.link.writer is writer or guest.link.writer is None):
                guest.link.writer = None
                self.mark_lost(guest)
            try:
                if writer.can_write_eof():
                    writer.write_eof()
            except Exception:
                pass
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
            log.info("connection to %s closed", peer)

