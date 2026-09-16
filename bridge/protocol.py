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
# Kept as functions rather than subclasses so the guest and bridge agree on one
# flat vocabulary that is easy to grep for.


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


def text(body: bytes, *, seq: int, style: str = "plain",
         page: int | None = None, enc: str = "raw") -> Frame:
    """A styled run of transcript text. The bridge has already done the
    markdown parsing, the CP1252 fold and (later) the compression."""
    h = {"seq": str(seq), "style": style, "enc": enc}
    if page is not None:
        h["page"] = str(page)
    return Frame("TEXT", h, body)


def user_input(body: bytes) -> Frame:
    return Frame("INPUT", {}, body)


def status(**kw: object) -> Frame:
    """Bridge -> guest status line: connection state, turn state, cost, context."""
    return Frame("STATUS", {k: str(v) for k, v in kw.items()})


def interrupt() -> Frame:
    """Guest -> bridge Stop button."""
    return Frame("INTERRUPT", {})
