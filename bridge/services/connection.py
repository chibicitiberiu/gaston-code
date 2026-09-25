"""ConnectionService: TCP admission, HELLO handshake, codepage binding, and frame pump.

Owns the transport and connection lifecycle for a GuestSession:
connection admission (_on_client), HELLO negotiation and deadline (_read_hello),
session attachment and reconnect recovery (_attach), Windows codepage mapping
(_bind_codepages, _oem_for), raw frame transmission (_try_send_raw), framing pump
and inflation (_pump, _handle), idle reaping (_reap_idle), and disconnect marking (_mark_lost).
"""

from __future__ import annotations

import asyncio
import logging
import sys
import time
from typing import TYPE_CHECKING, Any, Callable

from .. import handshake
from ..guest import GuestSession
from ..agent import profiles, toolrep, toolrules
from ..view.pages import Paginator
from ..view.transcript import TranscriptView
from ..wire import fold
from ..synccoord import SyncCoordinator
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
        create_guest: Callable[[int, int], Any] | None = None,
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
        self.guest: GuestSession | None = None
        #: Where the halt bar and the agent-notice queue live until a guest
        #: attaches and brings its own. A forward, not storage -- see
        #: `Bridge.synccoord` for the 02.18 reasoning and why R7 deletes it.
        self._preattach_synccoord = SyncCoordinator()

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

    async def attach(
        self, hello: Frame, writer: asyncio.StreamWriter
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

        if self.guest and self.guest.link.connected:
            await self.try_send_raw(
                writer, error("busy", "another client holds this session")
            )
            return None

        page_bytes, page_blocks, caps = neg.page_bytes, neg.page_blocks, neg.caps
        log.info(
            "guest has %d MB; pages capped at %d bytes / %d blocks",
            neg.mem_kb // 1024,
            page_bytes,
            page_blocks,
        )
        log.info("client rules: %s", caps.summary())
        log.info(
            "guest mirrors %s", h.get("sync_root") or "(file sharing is off)"
        )

        is_reconnect = self.guest is not None
        if self.guest is not None:
            self.guest.link.zip_out.reset()
            self.guest.link.zip_in.reset()

        if self.guest is None:
            if self.create_guest:
                self.guest = self.create_guest(page_bytes, page_blocks)
            else:
                mod = sys.modules.get("bridge.server")
                guest_cls = getattr(mod, "GuestSession", None) if mod else None
                if guest_cls:
                    self.guest = guest_cls(
                        view=TranscriptView(
                            pages=Paginator(page_bytes, page_blocks)
                        )
                    )

        guest = self.guest
        if guest is None:
            log.error("cannot attach: no GuestSession could be instantiated")
            return None

        guest.link.writer = writer
        guest.view.page_bytes = page_bytes
        guest.view.page_blocks = page_blocks
        guest.hello = dict(h)
        guest.caps = caps
        fold.use(caps.acp)

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

    async def send_html(self, guest: GuestSession, html: str, mode: str,
                        page_no: int) -> None:
        await guest.link.send_html(guest, html, mode, page_no)

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
        while True:
            await asyncio.sleep(idle_check_s)
            guest = self.guest
            if guest is None:
                continue
            if not guest.link.connected:
                if (
                    guest.perm_pending is not None
                    and guest.lost_at is not None
                    and time.monotonic() - guest.lost_at > perm_grace_s
                ):
                    guest.perms.deny_all(
                        "gone",
                        "The Windows machine disconnected before anyone could "
                        "answer, so this was not approved. It was not refused "
                        "on purpose -- ask again if you still need it.",
                    )
                continue
            if guest.exec_pending or guest.sync.active:
                continue
            idle = time.monotonic() - guest.last_seen
            if idle < idle_timeout_s:
                continue
            log.warning(
                "guest silent for %.0fs; closing so it can reconnect", idle
            )
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

            guest = await self.attach(hello, writer)
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
            try:
                await writer.wait_closed()
            except Exception:
                pass
            log.info("connection to %s closed", peer)

