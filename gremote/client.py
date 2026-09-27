"""The connection: handshake, request/reply, heartbeat.

WHY A READER THREAD. The simple shape -- send, then read until a frame
arrives -- breaks on the first streaming op and on the heartbeat, both of
which put frames on the wire that the caller did not ask for at that moment.
So one thread reads, and dispatches each frame to whoever is waiting on its
`id`. Requests then block on an Event instead of on the socket, and a `GET`
streaming chunks while a `PING` goes out in the middle is unremarkable
rather than a desynchronised stream.

WHY THE CODEC COMES FROM THE BRIDGE. `bridge/wire/frame.py` is the frame
grammar, deliberately stdlib-only and pinned that way by a test that spawns a
subprocess and asserts the import graph is exactly bridge, bridge.wire,
bridge.wire.frame. Nothing else from the bridge is imported here -- not
`protocol.py`, which pulls in the CP1252 folding and the capability tables
that belong to the client's protocol and not to this one.

DEFLATE IS OFF. The server reports `deflate=0` in HELLO and this never sets
`enc=deflate`; a compressing peer talking to a non-compressing one has the
link dropped under it rather than degrading, so the two ends agree by not
trying. See docs/gremote-plan.md.
"""

from __future__ import annotations

import contextlib
import socket
import threading
import time

from bridge.wire.frame import Frame, FrameReader

from .errors import Disconnected, Refused, Timeout
from .ops import (
    Drive,
    Entry,
    ExecResult,
    Process,
    Stat,
    SysInfo,
    codepage,
    parse_drives,
    parse_entries,
    parse_exec,
    parse_processes,
    parse_stat,
    parse_sysinfo,
)

#: 7890 and 7897 are the Gaston client's, 7900+ the test harness's.
DEFAULT_PORT = 7401

#: The protocol this speaks. Not the client's RC_PROTO_VERSION: gremote is a
#: separate op namespace on the same frame grammar and the two must not drift
#: into each other (docs/REMOTE.md).
PROTO_VERSION = 1

#: Frame types that finish a request. `DATA` accumulates instead.
_TERMINAL = frozenset({"OK", "ERR", "END"})


class _Call:
    """One outstanding request, waited on by the thread that made it."""

    __slots__ = ("chunks", "done", "reply")

    def __init__(self) -> None:
        self.chunks: list[Frame] = []
        self.reply: Frame | None = None
        self.done = threading.Event()


class Remote:
    """A live connection to one machine.

    One caller at a time is the server's rule, not this class's: a second
    connection while another is live is answered `ERR code=busy` and closed.
    """

    def __init__(self, host: str, secret: str, port: int = DEFAULT_PORT,
                 connect_timeout: float = 10.0,
                 request_timeout: float = 30.0,
                 ping_every: float = 10.0) -> None:
        self.host = host
        self.port = port
        self.secret = secret
        self.request_timeout = request_timeout
        self.ping_every = ping_every
        self.hello: dict[str, str] = {}
        #: Filenames and window text are ANSI; what a console child writes is
        #: OEM. HELLO reports both so nothing here has to guess, and getting
        #: them the wrong way round is silent until somebody has an accented
        #: character in a path.
        self.acp = "latin-1"
        self.oemcp = "latin-1"

        self._connect_timeout = connect_timeout
        self._sock: socket.socket | None = None
        self._send_lock = threading.Lock()
        self._calls: dict[str, _Call] = {}
        self._calls_lock = threading.Lock()
        self._greeting = _Call()
        self._seq = 0
        self._closing = threading.Event()
        self._reader: threading.Thread | None = None
        self._pinger: threading.Thread | None = None
        self._last_sent = 0.0
        self._failure: str = ""

    # --- lifecycle ---------------------------------------------------------

    def __enter__(self) -> Remote:
        self.connect()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def connect(self) -> None:
        """Open the socket, authenticate, and keep HELLO."""
        self._sock = socket.create_connection((self.host, self.port),
                                              self._connect_timeout)
        self._sock.settimeout(None)
        self._reader = threading.Thread(target=self._read_loop,
                                        name="gremote-reader", daemon=True)
        self._reader.start()
        self._send(Frame("AUTH", {"ver": str(PROTO_VERSION),
                                  "secret": self.secret}))
        reply = self._wait(self._greeting, self._connect_timeout, "AUTH")
        if reply.type != "HELLO":
            raise Refused(reply.headers.get("code", "?"), "AUTH")
        self.hello = dict(reply.headers)
        self.acp = codepage(self.hello.get("acp", ""))
        self.oemcp = codepage(self.hello.get("oemcp", ""))
        self._pinger = threading.Thread(target=self._ping_loop,
                                        name="gremote-heartbeat", daemon=True)
        self._pinger.start()

    def close(self) -> None:
        """Hang up. The machine is free for the next caller at once, rather
        than after its heartbeat timeout."""
        self._closing.set()
        sock, self._sock = self._sock, None
        if sock is not None:
            # A FIN before the close: an abrupt RST is reported to crash
            # Win98's Winsock, and the server follows the same rule.
            with contextlib.suppress(OSError):
                sock.shutdown(socket.SHUT_WR)
            sock.close()
        for t in (self._reader, self._pinger):
            if t is not None and t is not threading.current_thread():
                t.join(timeout=5)
        self._reader = self._pinger = None
        self._fail_pending("the connection was closed")

    # --- requests ----------------------------------------------------------

    def ping(self) -> None:
        self.request("PING")

    def exec(self, command: str, timeout: float = 20.0,
             cwd: str = "", shell: str = "") -> ExecResult:
        """Run a command and wait for it.

        `shell` is the invocation prefix to splice in front, and the machine
        does not choose it: HELLO says what this box is, and a guest picking
        for itself would be guessing on behalf of something that knows
        better. Without one the program is spawned directly, which is also
        the only way `exit` means anything.

        The local wait is the guest's own timeout plus a margin -- the guest
        stops the command itself, and a client that gave up first would
        leave a running child and no way to tell.
        """
        headers = {"timeout_ms": str(int(timeout * 1000))}
        if cwd:
            headers["cwd"] = cwd
        if shell:
            headers["shell"] = shell
        end, chunks = self.stream("EXEC", headers,
                                  command.encode(self.acp, "replace"),
                                  timeout=timeout + 15.0)
        return parse_exec(end, chunks)

    def launch(self, command: str, cwd: str = "", show: int = 1) -> int:
        """Start a program and do not wait. Returns its pid."""
        headers = {"show": str(show)}
        if cwd:
            headers["cwd"] = cwd
        reply = self.request("LAUNCH", headers,
                             command.encode(self.acp, "replace"))
        return int(reply.headers.get("pid", "0"), 16)

    # --- the disk ----------------------------------------------------------

    def ls(self, path: str, max_entries: int = 0) -> list[Entry]:
        """One directory, not a walk. `truncated` on the END frame says the
        listing stopped at the cap rather than at the end -- a short listing
        that looks complete is indistinguishable from a mass deletion."""
        headers = {"path": path}
        if max_entries:
            headers["max_entries"] = str(max_entries)
        _, chunks = self.stream("LS", headers)
        return parse_entries(chunks, self.acp)

    def stat(self, path: str) -> Stat:
        return parse_stat(self.request("STAT", {"path": path}))

    def drives(self) -> list[Drive]:
        """What letters exist, and what is behind them. `dir` can list one
        volume; it cannot say which letters there are or which is a CD."""
        _, chunks = self.stream("DRIVES")
        return parse_drives(chunks, self.acp)

    def sysinfo(self) -> SysInfo:
        return parse_sysinfo(self.request("SYSINFO"))

    def ps(self) -> list[Process]:
        """Every process on the machine.

        Raises Refused("no_toolhelp") where there is no way to enumerate --
        NT 4, which 9x is not, but the guest resolves Toolhelp at runtime
        rather than importing it and so can say so instead of failing to
        load.
        """
        _, chunks = self.stream("PS")
        return parse_processes(chunks, self.acp)

    def kill(self, pid: int, force: bool = False) -> None:
        """End a process.

        Refusals worth knowing by name: `denied` for the handful that hang
        9x when killed, `dos_session` for anything with a DOS VM under it
        (which needs force, and costs a stranded REDIR32 either way), and
        `pending` -- on 98 TerminateProcess returning success is not the
        same as the process being gone.
        """
        headers = {"pid": f"{pid:08x}"}
        if force:
            headers["force"] = "1"
        self.request("KILL", headers)

    def reboot(self, mode: str = "reboot") -> None:
        """Restart, log off, or power down. There is no undo and no
        confirmation beyond this call, so it spells the confirmation out on
        the wire rather than letting a stray REBOOT frame end somebody's
        session."""
        self.request("REBOOT", {"confirm": "yes", "mode": mode})

    def request(self, op: str, headers: dict[str, str] | None = None,
                body: bytes = b"", timeout: float | None = None) -> Frame:
        """Send one request and return its terminal frame.

        Raises Refused on `ERR`, so a caller that only wants the happy path
        does not have to check. The chunks of a streaming op are on the
        returned call via `stream()` rather than here.
        """
        reply, _ = self.stream(op, headers, body, timeout)
        return reply

    def stream(self, op: str, headers: dict[str, str] | None = None,
               body: bytes = b"",
               timeout: float | None = None) -> tuple[Frame, list[Frame]]:
        """As `request`, and also the `DATA` frames that arrived before the
        terminal one."""
        call = _Call()
        sent = dict(headers or {})
        with self._calls_lock:
            self._seq += 1
            # Ids restart at 1 per connection. The server tags every request
            # with the connection generation, so an answer from a previous
            # one is dropped rather than landing in this stream.
            ident = f"c{self._seq}"
            sent["id"] = ident
            self._calls[ident] = call
        try:
            self._send(Frame(op, sent, body))
            reply = self._wait(call, timeout or self.request_timeout, op)
        finally:
            with self._calls_lock:
                self._calls.pop(ident, None)
        if reply.type == "ERR":
            raise Refused(reply.headers.get("code", "?"), op,
                          reply.headers.get("msg", ""))
        return reply, call.chunks

    # --- plumbing ----------------------------------------------------------

    def _send(self, frame: Frame) -> None:
        sock = self._sock
        if sock is None:
            raise Disconnected(self._failure or "not connected")
        data = frame.encode()
        with self._send_lock:
            try:
                sock.sendall(data)
            except OSError as e:
                raise Disconnected(f"sending {frame.type}: {e}") from e
            self._last_sent = time.monotonic()

    def _wait(self, call: _Call, timeout: float, op: str) -> Frame:
        if not call.done.wait(timeout):
            raise Timeout(f"no answer to {op} in {timeout:g}s")
        if call.reply is None:
            raise Disconnected(self._failure or f"the link went away during {op}")
        return call.reply

    def _finish(self, call: _Call, frame: Frame | None) -> None:
        call.reply = frame
        call.done.set()

    def _fail_pending(self, why: str) -> None:
        self._failure = self._failure or why
        with self._calls_lock:
            waiting = list(self._calls.values())
            self._calls.clear()
        for call in [*waiting, self._greeting]:
            if not call.done.is_set():
                self._finish(call, None)

    def _read_loop(self) -> None:
        reader: FrameReader[Frame] = FrameReader(Frame)
        why = "the machine closed the connection"
        try:
            while True:
                sock = self._sock
                if sock is None:
                    break
                data = sock.recv(65536)
                if not data:
                    break
                for frame in reader.feed(data):
                    self._dispatch(frame)
        except OSError as e:
            why = f"the link failed: {e}"
        except Exception as e:  # a codec error is still the link's end
            why = f"the machine sent something unreadable: {e}"
        finally:
            if not self._closing.is_set():
                self._fail_pending(why)

    def _dispatch(self, frame: Frame) -> None:
        ident = frame.headers.get("id", "")
        if not ident:
            # HELLO, and the ERR that replaces it when the handshake fails,
            # are the only frames with no id to correlate on. After the
            # handshake an id-less frame is the answer to the heartbeat's
            # own PING, which nobody is waiting for.
            if not self._greeting.done.is_set():
                self._finish(self._greeting, frame)
            return
        with self._calls_lock:
            call = self._calls.get(ident)
        if call is None:
            # An answer to a request nobody is waiting for: one that timed
            # out, or one from a previous connection. Dropping it is the
            # point of tagging the generation.
            return
        if frame.type in _TERMINAL:
            self._finish(call, frame)
        else:
            call.chunks.append(frame)

    def _ping_loop(self) -> None:
        """A PING when the socket has been quiet, so a long call on the main
        thread never looks like a dead client."""
        while not self._closing.wait(1.0):
            quiet = time.monotonic() - self._last_sent
            if quiet < self.ping_every:
                continue
            try:
                self._send(Frame("PING"))
            except Disconnected:
                return
