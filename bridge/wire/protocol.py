"""Wire protocol between the bridge and the Win98 guest.

Deliberately small and text-shaped so the guest side can parse it with strchr
and a human can debug it with netcat. Explicitly NOT JSON: the guest has no
JSON parser and is not getting one.

    frame   := uint32le(len) payload          -- len <= MAX_FRAME
    payload := TYPE "\n" *(key "=" value "\n") "\n" body

`body` is raw bytes and may be empty. Everything before it is ASCII.

This is inspired by retro-agent's framing but is not the same protocol: there
is no status byte, it is full duplex with unsolicited server push, and headers
carry key=value rather than a positional argument string.

MAX_FRAME is 64KB, NOT the 32MB retro-agent uses. 32MB is the entire target
machine; a single corrupted length prefix would be fatal.
"""

from __future__ import annotations

import re
import struct
from dataclasses import dataclass, field

from . import fold
PROTO_VERSION = 1

# A corrupted length prefix must not be able to allocate the machine away.
# Anything genuinely larger is chunked by the sender.
#: The guest's per-header-value limit (`RC_MAX_VAL` in win/frameproto.h),
#: minus the NUL. A value longer than this is REFUSED by the guest's parser,
#: which drops the whole frame -- so sending one is a dropped command, and
#: sending one silently is worse: `cwd` goes straight to CreateProcessA, and a
#: clipped path names a different, existing directory.
MAX_HEADER_VALUE = 255

MAX_FRAME = 64 * 1024

_HEADER_END = b"\n\n"


class ProtocolError(Exception):
    """Malformed frame. Always fatal for the connection -- never try to resync
    a byte stream whose framing you have already lost."""


# Header values are printable ASCII on the wire. Everything else -- control
# characters and every byte a CP1252 text would use above 0x7E -- is
# percent-encoded.
#
# Escaping only %, LF and CR was not enough, and the gap was not theoretical:
# SESSION frames carry session titles, which are arbitrary user text, so the
# first real chat list from the guest crashed the bridge with
#   UnicodeEncodeError: 'ascii' codec can't encode character '\u2442'
# on every reconnect attempt.
_SAFE = frozenset(range(0x20, 0x7F)) - {ord("%")}

_PCT = re.compile(r"%([0-9A-Fa-f]{2})")


def _quote(value: str) -> str:
    """Escape a header value to printable ASCII.

    Folded to CP1252 first, so a header and the body it describes agree about
    what a character is -- the guest has one codepage and it is not Unicode.

    Percent-encoding rather than backslashes because the guest reads these with
    strchr and backslash escaping is exactly the tar pit claudewin9x fell into
    (its system prompt needed a section on quadrupling backslashes).
    """
    out = []
    for b in fold.encode(value):
        out.append(chr(b) if b in _SAFE else "%%%02X" % b)
    return "".join(out)


def _unquote(value: str) -> str:
    """Inverse of _quote. Never raises: a header is not worth a dropped link.

    Decoded in a single pass, so a literal "%25" that was encoded as "%2525"
    comes back as "%25" rather than being unescaped twice.
    """
    raw = bytearray()
    i = 0
    while i < len(value):
        m = _PCT.match(value, i)
        if m:
            raw.append(int(m.group(1), 16))
            i = m.end()
        else:
            raw.extend(value[i].encode("cp1252", errors="replace"))
            i += 1
    return fold.decode(bytes(raw))


@dataclass
class Frame:
    type: str
    headers: dict[str, str] = field(default_factory=dict)
    body: bytes = b""

    # --- convenience accessors for the headers every frame may carry ---

    @property
    def seq(self) -> int | None:
        v = self.headers.get("seq")
        return int(v) if v is not None else None

    @property
    def enc(self) -> str:
        return self.headers.get("enc", "raw")

    def encode(self) -> bytes:
        if not self.type or not self.type.isupper():
            raise ProtocolError(f"frame type must be uppercase: {self.type!r}")

        out = [self.type.encode("ascii"), b"\n"]
        for k, v in self.headers.items():
            if not k or not all(c.isalnum() or c == "_" for c in k):
                raise ProtocolError(f"bad header key: {k!r}")
            out.append(f"{k}={_quote(str(v))}\n".encode("ascii"))
        out.append(b"\n")
        out.append(self.body)

        payload = b"".join(out)
        if len(payload) > MAX_FRAME:
            raise ProtocolError(
                f"frame is {len(payload)}B, over the {MAX_FRAME}B cap; "
                "the sender must chunk it"
            )
        return struct.pack("<I", len(payload)) + payload

    @classmethod
    def decode(cls, payload: bytes) -> "Frame":
        split = payload.find(_HEADER_END)
        if split < 0:
            raise ProtocolError("no header terminator in frame")

        head = payload[:split].decode("ascii", errors="replace")
        body = payload[split + 2 :]

        lines = head.split("\n")
        ftype = lines[0]
        if not ftype:
            raise ProtocolError("empty frame type")

        headers: dict[str, str] = {}
        for line in lines[1:]:
            if not line:
                continue
            k, eq, v = line.partition("=")
            if not eq:
                raise ProtocolError(f"header line without '=': {line!r}")
            if k in headers:
                raise ProtocolError(f"duplicate header key: {k!r}")
            headers[k] = _unquote(v)

        return cls(type=ftype, headers=headers, body=body)


class FrameReader:
    """Incremental reassembler. Feed it whatever recv() returned."""

    def __init__(self, max_frame: int = MAX_FRAME) -> None:
        self._buf = bytearray()
        self._max = max_frame

    def feed(self, data: bytes) -> list[Frame]:
        self._buf.extend(data)
        frames: list[Frame] = []
        while True:
            if len(self._buf) < 4:
                return frames
            (length,) = struct.unpack_from("<I", self._buf, 0)
            if length > self._max:
                # Do not attempt recovery. Either the peer is broken or the
                # stream is desynchronised; both mean the connection is over.
                raise ProtocolError(
                    f"frame claims {length}B, cap is {self._max}B"
                )
            if len(self._buf) < 4 + length:
                return frames
            payload = bytes(self._buf[4 : 4 + length])
            del self._buf[: 4 + length]
            frames.append(Frame.decode(payload))


# --- frame constructors -----------------------------------------------------
#
# This IS the protocol's vocabulary, as far as the bridge -> guest direction
# goes: one constructor per frame type the bridge sends, so `stamp=`, header
# stringification and `fold.encode` are decided once here rather than
# independently at every `Frame(...)` call site (docs/audit/02-bridge-python.md
# #1.1). `GuestLink` (bridge/wire/link.py) is the seam that owns HOW a frame
# reaches the wire -- the writer, the send lock, the compressor, the chunking.
# These functions are what decides WHAT the bridge is allowed to send: a
# constructor takes the values that vary and returns a `Frame` whose headers
# are always shaped the same way for that type.
#
# `stamp=` -- whether a frame enters the replay buffer -- is deliberately NOT
# decided here. It is almost always a property of the type (an EXEC or an
# EXECKILL must never be replayed; a reissue after reconnect restores state
# that already happened and must not replay either), but TURN start is sent
# both ways: a fresh turn should replay, an after-reconnect reissue of the
# busy UI (Bridge._reissue_turn) must not, for the SAME headers. So `stamp=`
# stays an explicit argument at the `GuestLink.send` call site, same as
# before -- only the header-building duplication is gone.
#
# HELLO and ERROR predate this: they are sent before a GuestSession exists to
# hang a send helper off (both fakegui and the guest open a connection with
# HELLO; ERROR is sent standalone by the bridge whenever a handshake step
# fails before any session-scoped state exists to send it through).
#
# Four other constructors used to live here -- text(), user_input(), status(),
# interrupt() -- and were dead: no caller anywhere in bridge/, win/ or tests/.
# text()'s own `style=` parameter named a frame shape the bridge had already
# stopped sending. Removed rather than fixed, in the same pass that reworked
# fakegui.py to render TEXT by the `mode=` header the bridge actually sends.
# `text_frame()` below is a new, unrelated constructor for the TEXT frame the
# bridge actually sends today.


def hello(secret: str, sess: str | None = None, from_seq: int | None = None) -> Frame:
    """First frame from the guest. Carries the protocol version and the cap so
    a mismatch is caught before anything else, and optionally resumes."""
    h = {"ver": str(PROTO_VERSION), "secret": secret, "max_frame": str(MAX_FRAME)}
    if sess:
        h["sess"] = sess
    if from_seq is not None:
        h["from"] = str(from_seq)
    return Frame("HELLO", h)


def error(code: str, msg: str) -> Frame:
    """Errors need a frame type of their own, or a version-mismatch rejection
    arrives as bytes the peer cannot parse."""
    return Frame("ERROR", {"code": code, "msg": msg})


# --- session lifecycle --------------------------------------------------


def ready(sess: str, model: str, next_seq: int, page: int, total: int) -> Frame:
    """First frame after HELLO succeeds, carrying the session id and the
    replay cursor a future `from=` would resume from."""
    return Frame("READY", {
        "sess": sess,
        "ver": str(PROTO_VERSION),
        "model": model,
        "next_seq": str(next_seq),
        "page": str(page),
        "total": str(total),
    })


def resync(reason: str) -> Frame:
    """Told to a guest whose requested replay point the buffer no longer
    has. The guest has no handler for this today -- see
    docs/audit/02-bridge-python.md #6.1 -- but the bridge still sends it
    rather than silently resuming from the wrong place."""
    return Frame("RESYNC", {"reason": reason})


def doc(html: str) -> Frame:
    """The document skeleton and stylesheet, sent once per connection."""
    return Frame("DOC", {}, fold.encode(html))


def switched(session_id: str, total: int) -> Frame:
    """A different conversation is now live: NEWSESSION, SWITCH or an effort
    fork all land here."""
    return Frame("SWITCHED", {"id": session_id, "total": str(total)})


def sessions_start(count: int) -> Frame:
    return Frame("SESSIONS", {"count": str(count)})


def session_row(i: int, sess_id: str, title: str, cwd: str, group: str,
                current: bool) -> Frame:
    return Frame("SESSION", {
        "i": str(i),
        "id": sess_id,
        "title": title[:120],
        "cwd": cwd[:120],
        "group": group,
        "current": "1" if current else "0",
    })


def sessions_end() -> Frame:
    return Frame("SESSIONSEND", {})


def notice(kind: str, text: str) -> Frame:
    return Frame("NOTICE", {"kind": kind}, fold.encode(text))


# --- turns ----------------------------------------------------------------


def turn_start() -> Frame:
    """Sent both for a fresh turn and, with `stamp=False`, to restore the
    busy UI after a reconnect -- see the note on `stamp=` above."""
    return Frame("TURN", {"state": "start"})


def turn_error(msg: str) -> Frame:
    """A turn that raised. The message travels in the BODY, not a header.

    It was a `msg=` header capped at 200 characters, and that was a trap with
    a sharp edge. `_quote` expands every byte outside 0x20..0x7E to three, so
    a 200-character message with about 28 newlines in it -- a stack trace, or
    a JSON error blob, which is exactly what an API failure looks like --
    crosses the guest's `RC_MAX_VAL` of 256. The guest REFUSES an over-long
    header value rather than truncating it (audit 01.44, deliberately), and
    `net.cpp` treats a frame that will not parse as a dead stream and drops
    the link.

    So the failure mode was: the longer and more informative the error, the
    more likely the user saw a disconnection instead of the error. The body
    has no such ceiling, and `HALT` already carries its text there for the
    same reason.
    """
    return Frame("TURN", {"state": "error"}, fold.encode(msg[:2000]))


def turn_end(ok: bool, subtype: str, cost_usd: float | None, ms: int) -> Frame:
    return Frame("TURN", {
        "state": "end",
        "ok": "1" if ok else "0",
        "subtype": subtype,
        "cost_usd": f"{cost_usd:.4f}" if cost_usd else "",
        "ms": str(ms),
    })


def anchor_frame(page: int, off: int, preview: str) -> Frame:
    """Where a user turn landed in the transcript, so the guest can jump
    back to it from history."""
    return Frame("ANCHOR", {
        "page": str(page), "off": str(off), "preview": preview,
    })


# --- permission prompts -----------------------------------------------------


def permreq(tool_use_id: str, tool_name: str, allows: bool, waiting: int,
            summary: str) -> Frame:
    return Frame("PERMREQ", {
        "id": tool_use_id,
        "tool": tool_name,
        "allows": "1" if allows else "0",
        "waiting": str(waiting),
    }, fold.encode(summary))


def permstate(tool_use_id: str, tool_name: str, allows: bool, waiting: int,
              summary: str) -> Frame:
    """Reissued after a reconnect to put the bar back -- same shape as
    PERMREQ under a different type, so the guest can tell a fresh prompt
    from one it should already be showing."""
    return Frame("PERMSTATE", {
        "id": tool_use_id,
        "tool": tool_name,
        "allows": "1" if allows else "0",
        "waiting": str(waiting),
    }, fold.encode(summary))


def permwait(tool_use_id: str, waiting: int) -> Frame:
    """Refreshes the queue depth shown on the bar."""
    return Frame("PERMWAIT", {"id": tool_use_id, "waiting": str(waiting)})


def permend(tool_use_id: str, reason: str) -> Frame:
    return Frame("PERMEND", {"id": tool_use_id, "reason": reason})


# --- the sync halt bar -------------------------------------------------


def halt(halt_id: str, text: str) -> Frame:
    return Frame("HALT", {"id": halt_id}, fold.encode(text))


def haltend(halt_id: str) -> Frame:
    return Frame("HALTEND", {"id": halt_id})


# --- transcript paging -------------------------------------------------


def pagesealed(page: int, total: int) -> Frame:
    return Frame("PAGESEALED", {"page": str(page), "total": str(total)})


def pagestart(page: int, total: int, live: bool, continued: bool) -> Frame:
    return Frame("PAGESTART", {
        "page": str(page),
        "total": str(total),
        "live": "1" if live else "0",
        "continued": "1" if continued else "0",
    })


def pageend(page: int) -> Frame:
    return Frame("PAGEEND", {"page": str(page)})


def text_frame(mode: str, enc: str, page: int, body: bytes = b"", *,
               total: int | None = None, off: int | None = None) -> Frame:
    """One TEXT frame -- either a LIVE replace or one chunk of an APPEND
    block. `total`/`off` are set together, only when this is a chunk of a
    larger send; see GuestLink.send_html for why they are on every chunk."""
    h = {"mode": mode, "enc": enc, "page": str(page)}
    if total is not None:
        h["total"] = str(total)
        h["off"] = str(off)
    return Frame("TEXT", h, body)


# --- exec ----------------------------------------------------------------


def exec_cmd(exec_id: str, command: str, *, timeout_ms: int, shell: bool,
             cwd: str = "") -> Frame:
    """The command goes in the BODY, never a header: a header value is
    truncated at 255 bytes silently, and a truncated command is still
    EXECUTED -- `del c:\\temp\\*.tmp` cut short becomes `del c:\\temp\\*`."""
    headers = {"id": exec_id, "timeout_ms": str(timeout_ms),
               "shell": "1" if shell else "0"}
    if cwd:
        headers["cwd"] = cwd
    return Frame("EXEC", headers, fold.encode(command))


def exec_kill(exec_id: str) -> Frame:
    return Frame("EXECKILL", {"id": exec_id})


# --- model / effort menus ---------------------------------------------


def option(kind: str, id: str, label: str, current: bool, *,
           disabled: bool = False) -> Frame:
    h = {"kind": kind, "id": id, "label": label,
         "current": "1" if current else "0"}
    if disabled:
        h["disabled"] = "1"
    return Frame("OPTION", h)


def optionsend() -> Frame:
    return Frame("OPTIONSEND", {})


def pong() -> Frame:
    return Frame("PONG", {})
