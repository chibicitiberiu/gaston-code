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
import zlib

from bridge.wire.frame import Frame, FrameReader

from .errors import Cancelled, Disconnected, Refused, Timeout
from .ops import (
    Drive,
    Entry,
    ExecResult,
    FileBytes,
    Process,
    Stat,
    SysInfo,
    codepage,
    parse_drives,
    parse_entries,
    parse_exec,
    parse_get,
    parse_processes,
    parse_stat,
    parse_sysinfo,
)
from .ship import ShipResult, UpdateResult
from .ship import self_update as _self_update
from .ship import ship as _ship

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

    __slots__ = ("cancelled", "chunks", "done", "reply")

    def __init__(self) -> None:
        self.chunks: list[Frame] = []
        self.reply: Frame | None = None
        self.cancelled = False
        self.done = threading.Event()


class Remote:
    """A live connection to one machine.

    One caller at a time is the server's rule, not this class's: a second
    connection while another is live is answered `ERR code=busy` and closed.
    """

    def __init__(self, host: str, secret: str, port: int = DEFAULT_PORT,
                 connect_timeout: float = 10.0,
                 request_timeout: float = 30.0,
                 ping_every: float = 10.0,
                 busy_retry_s: float = 8.0) -> None:
        self.host = host
        self.port = port
        self.secret = secret
        self.request_timeout = request_timeout
        self.ping_every = ping_every
        #: How long to keep trying when the machine answers `busy`.
        #:
        #: It serves one caller at a time, and letting go is not instant: the
        #: reader has to notice the socket went, and the accept loop has to
        #: come round and tear the session down. MEASURED on the guest at
        #: roughly a second after a refused handshake, which pauses before it
        #: drops the link so that a wrong secret costs a second.
        #:
        #: So a script that disconnects and reconnects -- which is most
        #: scripts -- would otherwise meet `busy` for its own previous self.
        #: Set to 0 to get the raw answer, which is what a caller checking
        #: whether somebody ELSE has the box wants.
        self.busy_retry_s = busy_retry_s
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
        """Open the socket, authenticate, and keep HELLO.

        Retries a `busy` answer for `busy_retry_s`; see that attribute for
        why, and pass 0 to get the first answer instead.
        """
        deadline = time.monotonic() + self.busy_retry_s
        while True:
            try:
                self._connect_once()
                return
            except Refused as e:
                if e.code != "busy" or time.monotonic() >= deadline:
                    raise
                self.close()
                time.sleep(0.25)

    def _connect_once(self) -> None:
        self._sock = socket.create_connection((self.host, self.port),
                                              self._connect_timeout)
        self._sock.settimeout(None)
        self._closing.clear()
        self._greeting = _Call()
        self._failure = ""
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

    def cancel(self, ident: str, timeout: float = 10.0) -> bool:
        """Stop a request, and be promised you will never hear about it.

        9P's Tflush, which the guest copies rather than re-derives: the ack
        means the cancelled request will NEVER be answered, and its id is
        free to reuse the moment the ack arrives. Returns False if the
        machine had nothing by that name, which is the ordinary outcome of a
        cancel that lost the race against a reply rather than an error.

        `ident` is the id a call is running under -- `stream()` and
        `request()` take an `on_id` callback that hands it over the moment
        it exists, which is the only way to have it while the call is still
        in flight.

        The thread waiting on the cancelled request is woken with
        `Cancelled`, because nothing else is coming for it.

        The ack says the request will never be ANSWERED. It does not say the
        machine is idle: a cancelled EXEC is still walking the child's
        process tree and stopping it, so the next request on that lane can
        still be told `busy` for a moment afterwards. Those are different
        promises and only the first one is Tflush's.
        """
        try:
            self.request("CANCEL", {"target": ident}, timeout=timeout)
        except Refused as e:
            if e.code == "no_such_id":
                return False
            raise
        with self._calls_lock:
            call = self._calls.pop(ident, None)
        if call is not None and not call.done.is_set():
            call.cancelled = True
            self._finish(call, None)
        return True

    def exec(self, command: str, timeout: float = 20.0,
             cwd: str = "", shell: str = "", on_id=None) -> ExecResult:
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
                                  timeout=timeout + 15.0, on_id=on_id)
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

    def get(self, path: str, max_bytes: int = 0, timeout: float = 120.0,
            on_id=None) -> FileBytes:
        """Read a file off the machine, byte for byte.

        `max_bytes` is honoured BY THE GUEST. A cap applied here would still
        have a 200MHz box read a swap file off a 1998 platter and push it
        down a socket for as long as it takes; the guest stops and says
        `truncated` instead. Without one it stops at its own four-megabyte
        ceiling.
        """
        headers = {"path": path}
        if max_bytes:
            headers["max_bytes"] = str(max_bytes)
        end, chunks = self.stream("GET", headers, timeout=timeout,
                                  on_id=on_id)
        return parse_get(end, chunks)

    def put(self, path: str, data: bytes, chunk: int = 8192,
            timeout: float = 120.0) -> int:
        """Write a file onto the machine. Returns the bytes it took.

        The guest writes a temp beside the target, checks the CRC, and only
        then replaces -- and it never moves the old file aside first. The
        aside exists so no instant has no file, and it is safe only where a
        recovery pass sweeps afterwards; nothing will ever sweep C:\\WINDOWS.
        So a failure out here leaves the original in place or leaves nothing,
        never a `.gaston-old` with no file at the real path.

        The chunks go out without waiting for an ack. The guest's lane holds
        a short queue for them and stops reading the socket when it is full,
        which is what tells this side to slow down.
        """
        call = _Call()
        with self._calls_lock:
            self._seq += 1
            ident = f"c{self._seq}"
            self._calls[ident] = call
        try:
            crc = zlib.crc32(data) & 0xFFFFFFFF
            first = data[:chunk]
            self._send(Frame("PUT", {"id": ident, "path": path, "off": "0"},
                             first))
            off = len(first)
            while off < len(data):
                piece = data[off:off + chunk]
                self._send(Frame("DATA", {"id": ident, "off": str(off)},
                                 piece))
                off += len(piece)
            self._send(Frame("END", {"id": ident, "crc": f"{crc:08x}"}))
            reply = self._wait(call, timeout, "PUT")
        finally:
            with self._calls_lock:
                self._calls.pop(ident, None)
        if reply.type == "ERR":
            raise Refused(reply.headers.get("code", "?"), "PUT",
                          reply.headers.get("detail", ""))
        return int(reply.headers.get("bytes", "0"))

    def ship(self, local_path: str, remote_path: str, *,
             relaunch: bool = True, cwd: str = "") -> ShipResult:
        """Put a new build on the machine and start it again.

        See gremote/ship.py for the two traps this exists to encode: a
        running exe is locked, and a second copy of the client raises the
        first and exits.
        """
        return _ship(self, local_path, remote_path, relaunch=relaunch,
                     cwd=cwd)

    def self_update(self, local_path: str, **kw) -> UpdateResult:
        """Replace THIS server with `local_path`, and wait for it to answer.

        `ship()` cannot do it: gremote's own pid is on the guest's kill deny
        list, so ending the process holding the target image comes back
        `denied` -- measured on the hardware, 2026-10-01. See gremote/ship.py
        for the shape and win/remote/gupdate.cpp for the program that does
        the swap.

        THIS CONNECTION IS CLOSED by the time it returns, whatever happens:
        the server it was talking to is gone. The result carries the HELLO
        of the one that replaced it.
        """
        return _self_update(self, local_path, **kw)

    def rm(self, path: str) -> None:
        self.request("RM", {"path": path})

    def mkdir(self, path: str) -> None:
        """Create a directory, parents and all. `exists` if the last level is
        already there -- the parents being there is the normal case and is
        not an error."""
        self.request("MKDIR", {"path": path})

    def mv(self, src: str, dst: str) -> None:
        """Rename. Refuses an existing target rather than replacing it: a
        move that silently overwrote something would be a delete nobody
        asked for. Never crosses volumes -- MoveFileEx is NT-only and on the
        guest's import deny list."""
        self.request("MV", {"from": src, "to": dst})

    def stat(self, path: str) -> Stat:
        return parse_stat(self.request("STAT", {"path": path}))

    def drives(self, probe: bool = False) -> list[Drive]:
        """What letters exist, and what is behind them. `dir` can list one
        volume; it cannot say which letters there are or which is a CD.

        `probe` is off by default (BACKLOG 20): against a removable drive or
        a CD-ROM, asking for the label and free space moves the mechanism --
        an audible seek on a real floppy, and a multi-second retry if it is
        empty. Without it those drives still come back, with their letter
        and type known and `has_media` reported as `None` (not probed, and
        not the same thing as "probed and empty"). Pass `True` for the old
        behaviour."""
        headers = {"probe": "1"} if probe else None
        _, chunks = self.stream("DRIVES", headers)
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
                body: bytes = b"", timeout: float | None = None,
                on_id=None) -> Frame:
        """Send one request and return its terminal frame.

        Raises Refused on `ERR`, so a caller that only wants the happy path
        does not have to check. The chunks of a streaming op are on the
        returned call via `stream()` rather than here.
        """
        reply, _ = self.stream(op, headers, body, timeout, on_id)
        return reply

    def stream(self, op: str, headers: dict[str, str] | None = None,
               body: bytes = b"", timeout: float | None = None,
               on_id=None) -> tuple[Frame, list[Frame]]:
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
            if on_id is not None:
                # Before the send, so a cancel racing the reply still has an
                # id to name. The guest answers `no_such_id` if it is too
                # early, which is a truthful answer and not a failure.
                on_id(ident)
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
        if call.cancelled:
            raise Cancelled(f"{op} was cancelled; the machine will not "
                            f"answer it")
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
