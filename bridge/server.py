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
import logging
import os
import secrets
import uuid
from dataclasses import dataclass, field

from . import fold
from .coalesce import Coalescer
from .compress import ENC_DEFLATE, ENC_RAW, StreamCompressor, should_compress
from .markdown import MarkdownStream, STYLE_USER, user_block
from .pages import Paginator
from .protocol import (
    MAX_FRAME,
    PROTO_VERSION,
    Frame,
    FrameReader,
    ProtocolError,
    error,
)
from .replay import Replay
from . import sessions as sesslist
from .session import Session

log = logging.getLogger("bridge.server")

DEFAULT_PORT = 7890


@dataclass
class GuestSession:
    """Bridge-side state for one guest, surviving across reconnects."""

    sess_id: str
    replay: Replay = field(default_factory=Replay)
    claude: Session | None = None
    writer: asyncio.StreamWriter | None = None
    send_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    # One persistent deflate stream for the whole connection. Resetting it
    # mid-connection would desynchronise the guest's inflater, so it is only
    # reset on reconnect -- see _attach.
    zip_out: StreamCompressor = field(default_factory=StreamCompressor)
    link_bps: float | None = None

    # Which page the guest is showing. None means LIVE (follow the last page).
    # In HISTORY mode the bridge keeps buffering silently; the page counter
    # ticking up in the status bar is the new-content notification.
    view_page: int | None = None

    # Which page the guest is showing. None means LIVE (follow the last page).
    view_page: int | None = None

    # One coalescer per source stream (assistant text, thinking). Coalescing
    # happens BEFORE markdown parsing, so the parser receives whole lines in
    # ~2KB batches rather than single tokens -- which is what lets it merge
    # adjacent same-style runs and keeps the expensive
    # EM_SETCHARFORMAT/EM_REPLACESEL pair count down on the guest.
    coalescers: dict = field(default_factory=dict)
    markdown: MarkdownStream = field(default_factory=MarkdownStream)

    # The transcript lives here, in full. The guest only ever holds one page,
    # which is what bounds its memory by page size rather than by conversation
    # length.
    pages: Paginator = field(default_factory=Paginator)

    @property
    def connected(self) -> bool:
        return self.writer is not None and not self.writer.is_closing()


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
            hello = await self._read_hello(reader, frames, writer)
            if hello is None:
                return

            guest = await self._attach(hello, writer)
            if guest is None:
                return

            await self._pump(guest, reader, frames)

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
                        return None
                    for f in frames.feed(data):
                        if f.type != "HELLO":
                            await self._try_send_raw(
                                writer, error("expected_hello",
                                              f"first frame was {f.type}")
                            )
                            return None
                        return f
        except TimeoutError:
            await self._try_send_raw(writer, error("hello_timeout",
                                                   "no HELLO within 10s"))
            return None

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

        # A fresh connection means a fresh inflater on the guest, so the
        # compressor must restart in lockstep or the first frame is garbage.
        if self._guest is not None:
            self._guest.zip_out.reset()

        resumed = False
        if self._guest is None:
            self._guest = GuestSession(sess_id=str(uuid.uuid4()))
        guest = self._guest
        guest.writer = writer

        guest.link_bps = self.link_bps
        for c in guest.coalescers.values():
            c.set_link_speed(guest.link_bps)

        if guest.claude is None:
            guest.claude = await self._make_session(guest)

        await self._send(guest, Frame("READY", {
            "sess": guest.sess_id,
            "ver": str(PROTO_VERSION),
            "model": guest.claude.model or "(default)",
            "next_seq": str(guest.replay.next_seq),
            "page": str(guest.pages.live.number),
            "total": str(guest.pages.count),
        }), stamp=False)

        # Replay anything missed while disconnected.
        want = h.get("from")
        if want is not None:
            try:
                missed = guest.replay.since(int(want))
            except LookupError as exc:
                await self._send(guest, Frame("RESYNC", {"reason": str(exc)}),
                                 stamp=False)
            else:
                resumed = True
                log.info("replaying %d frames from seq %s", len(missed), want)
                for f in missed:
                    await self._send(guest, f, stamp=False)

        log.info("attached sess=%s (resumed=%s)", guest.sess_id, resumed)
        return guest

    # --- Claude session ----------------------------------------------------

    async def _make_session(self, guest: GuestSession,
                            resume: str | None = None) -> Session:
        async def on_text(t: str) -> None:
            await self._emit_text(guest, t, style="plain")

        async def on_thinking(t: str) -> None:
            await self._emit_text(guest, t, style="thinking")

        async def on_tool(name: str, args: dict) -> None:
            # Flush buffered text first: a tool line that jumps ahead of the
            # prose that introduced it reads as a non sequitur.
            await self._flush_text(guest)
            await self._send(guest, Frame("TOOL", {"name": name},
                                          fold.encode(_summarise(args))))

        async def on_tool_result(tool_id: str, body: str, is_err: bool) -> None:
            await self._send(guest, Frame(
                "TOOLRESULT",
                {"id": tool_id, "error": "1" if is_err else "0"},
                fold.encode(body[:2000])))

        async def on_system(subtype: str, data: dict) -> None:
            if subtype == "compact_boundary":
                await self._send(guest, Frame("NOTICE", {"kind": "compacted"},
                                              fold.encode("History condensed.")))

        s = Session(
            cwd=self.cwd,
            model=self.model,
            effort=self.effort,
            resume=resume,
            on_text=on_text,
            on_thinking=on_thinking,
            on_tool=on_tool,
            on_tool_result=on_tool_result,
            on_system=on_system,
        )
        await s.start()
        return s

    def _coalescer(self, guest: GuestSession, style: str) -> Coalescer:
        c = guest.coalescers.get(style)
        if c is None:
            if style == "plain":
                # Assistant prose goes through the markdown parser, which emits
                # its own styles.
                async def sink(text: str) -> None:
                    for run in guest.markdown.feed(text):
                        await self._write_text(guest, run.text, style=run.style)
            else:
                # Thinking and anything else is pre-styled; pass it straight
                # through rather than parsing markdown out of it.
                async def sink(text: str, _s=style) -> None:
                    await self._write_text(guest, text, style=_s)
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
        for run in guest.markdown.flush():
            await self._write_text(guest, run.text, style=run.style)

    async def _write_text(self, guest: GuestSession, text: str,
                          *, style: str, stream: bool = True) -> None:
        """Record into the paginator, then stream to the guest if it is on the
        live page.

        `stream=False` records without sending. That is for text the guest has
        already put on screen itself -- the echo of the user's own line -- which
        still has to reach the paginator, because a sealed page is re-sent in
        full when the reader pages back and a transcript of answers with no
        questions in it is close to useless.
        """
        if not text:
            return

        page_no = guest.pages.live.number
        sealed = guest.pages.add(style, text)

        # Only stream when the guest is watching the live page. In HISTORY mode
        # the bridge keeps buffering silently and the page counter ticking up
        # is the notification.
        if stream and (guest.view_page is None or guest.view_page == page_no):
            await self._emit_runs(guest, style, text, page_no)

        for pg in sealed:
            await self._send(guest, Frame("PAGESEALED", {
                "page": str(pg.number), "total": str(guest.pages.count)}))

    async def _emit_runs(self, guest: GuestSession, style: str, text: str,
                         page_no: int) -> None:
        """Fold to CP1252, compress if worthwhile, chunk under the frame cap.

        Chunking here rather than at the socket keeps the send lock free
        between chunks, so a long turn cannot delay a keystroke or a Stop.
        """
        data = fold.encode(text)
        if not data:
            return
        budget = MAX_FRAME - 1024  # headroom for headers and deflate overhead
        for i in range(0, len(data), budget):
            chunk = data[i:i + budget]
            enc = ENC_RAW
            if should_compress(chunk, guest.link_bps):
                chunk = guest.zip_out.compress(chunk)
                enc = ENC_DEFLATE
            await self._send(guest, Frame(
                "TEXT", {"style": style, "enc": enc, "page": str(page_no)},
                chunk))

    # --- frame pump --------------------------------------------------------

    async def _pump(
        self,
        guest: GuestSession,
        reader: asyncio.StreamReader,
        frames: FrameReader,
    ) -> None:
        while True:
            data = await reader.read(8192)
            if not data:
                return
            for f in frames.feed(data):
                await self._handle(guest, f)

    async def _handle(self, guest: GuestSession, f: Frame) -> None:
        if f.type == "INPUT":
            text = fold.decode(f.body)
            log.info("input: %s", text[:120])
            asyncio.create_task(self._run_turn(guest, text))

        elif f.type == "INTERRUPT":
            if guest.claude:
                await guest.claude.interrupt()

        elif f.type == "GETSESSIONS":
            rows = sesslist.listing(cwd=self.cwd)
            await self._send(guest, Frame("SESSIONS",
                                          {"count": str(len(rows))}))
            for i, r in enumerate(rows):
                await self._send(guest, Frame("SESSION", {
                    "i": str(i),
                    "id": r["id"],
                    "title": r["title"][:120],
                    "cwd": r["cwd"][:120],
                    "current": "1" if (guest.claude and
                                       guest.claude.session_id == r["id"])
                               else "0",
                }))
            await self._send(guest, Frame("SESSIONSEND", {}))

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
            await self._send(guest, Frame("PONG", {}), stamp=False)

        elif f.type == "SETMODEL":
            model = f.headers.get("model", "")
            if guest.claude and model:
                await guest.claude.set_model(model)
                await self._send(guest, Frame("STATUS", {"model": model}))

        else:
            log.warning("unknown frame type from guest: %s", f.type)
            await self._send(guest, error("unknown_type", f.type), stamp=False)

    async def _run_turn(self, guest: GuestSession, text: str) -> None:
        if not guest.claude:
            return
        # User turns are the landmarks a reader navigates by.
        anchor = guest.pages.mark_user_turn(text)
        # The guest echoed this line itself the moment Send was pressed, so it
        # is recorded but not streamed. USER_BLOCK is shared with the guest so
        # the two renderings are the same string, not merely similar.
        await self._write_text(guest, user_block(text), style=STYLE_USER,
                               stream=False)
        await self._send(guest, Frame("ANCHOR", {
            "page": str(anchor.page), "off": str(anchor.offset),
            "preview": anchor.preview}))
        await self._send(guest, Frame("TURN", {"state": "start"}))
        try:
            turn = await guest.claude.send(text)
        except Exception as exc:
            log.exception("turn failed")
            await self._flush_text(guest)
            await self._send(guest, Frame("TURN", {
                "state": "error", "msg": str(exc)[:200]}))
            return
        await self._flush_text(guest)
        # Seal only at a message boundary, so a turn is never split
        # mid-sentence while the reader is watching it stream.
        sealed = guest.pages.message_boundary()
        if sealed:
            await self._send(guest, Frame("PAGESEALED", {
                "page": str(sealed.number), "total": str(guest.pages.count)}))
        raw, comp = guest.zip_out.stats
        if comp:
            log.info("deflate so far: %d -> %d bytes (%.2f:1)",
                     raw, comp, guest.zip_out.ratio)
        await self._send(guest, Frame("TURN", {
            "state": "end",
            "ok": "1" if turn.ok else "0",
            "subtype": turn.subtype,
            "cost_usd": f"{turn.cost_usd:.4f}" if turn.cost_usd else "",
            "ms": str(turn.duration_ms),
        }))

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
        await self._send(guest, Frame("NOTICE", {"kind": "switching"},
                                      fold.encode("Loading conversation...")))

        if guest.claude:
            await guest.claude.close()

        # Fresh transcript state. Anything the old session had is on disk.
        guest.pages = Paginator()
        guest.markdown = MarkdownStream()
        for c in guest.coalescers.values():
            await c.stop()
        guest.coalescers.clear()

        try:
            guest.claude = await self._make_session(guest, resume=session_id)
        except Exception as exc:
            log.exception("resume failed")
            await self._send(guest, error("resume_failed", str(exc)[:200]))
            return

        for style, text in sesslist.history_runs(session_id):
            guest.pages.add(style, text)
            if guest.pages.live.over_soft_cap():
                guest.pages.message_boundary()

        guest.view_page = None
        await self._send(guest, Frame("SWITCHED", {
            "id": session_id,
            "total": str(guest.pages.count),
        }))
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

        await self._send(guest, Frame("PAGESTART", {
            "page": str(page.number),
            "total": str(guest.pages.count),
            "live": "1" if guest.view_page is None else "0",
            "continued": "1" if page.continued else "0",
        }))
        for run in page.runs:
            await self._emit_runs(guest, run.style, run.text, page.number)
        await self._send(guest, Frame("PAGEEND", {"page": str(page.number)}))

    # --- sending -----------------------------------------------------------

    async def _send(self, guest: GuestSession, frame: Frame,
                    *, stamp: bool = True) -> None:
        if stamp:
            guest.replay.append(frame)
        async with guest.send_lock:
            w = guest.writer
            if w is None or w.is_closing():
                return  # buffered in replay; the guest will ask for it
            try:
                w.write(frame.encode())
                await w.drain()
            except (ConnectionResetError, BrokenPipeError):
                guest.writer = None

    async def _try_send_raw(self, writer: asyncio.StreamWriter,
                            frame: Frame) -> None:
        try:
            writer.write(frame.encode())
            await writer.drain()
        except Exception:
            pass


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
    try:
        asyncio.run(bridge.serve_forever())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
