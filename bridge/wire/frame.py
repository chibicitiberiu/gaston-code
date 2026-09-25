"""The frame codec, with no dependencies beyond the standard library.

    frame   := uint32le(len) payload          -- len <= MAX_FRAME
    payload := TYPE "\\n" *(key "=" value "\\n") "\\n" body

`body` is raw bytes and may be empty. Everything before it is ASCII: header
values are percent-encoded for '%', controls and anything outside printable
ASCII. Mirror of win/wire/frame.{h,cpp} -- keep in sync.

This is libwire's format, shared by every protocol built on it: the bridge's
(bridge/wire/protocol.py, which plugs in its CP1252 folding) and gremote's
Python client, which must not import the bridge. It knows the wire format and
no frame types.

MAX_FRAME is 64KB, NOT the 32MB retro-agent uses. 32MB is the entire target
machine; a single corrupted length prefix would be fatal.
"""

from __future__ import annotations

import re
import struct
from dataclasses import dataclass, field
from typing import ClassVar, Generic, Self, TypeVar

MAX_TYPE = 31
MAX_HEADERS = 16
MAX_HELLO_HEADERS = 32
MAX_HEADER_KEY = 31
MAX_KEY = MAX_HEADER_KEY
MAX_HEADER_VALUE = 255

MAX_FRAME = 64 * 1024

_HEADER_END = b"\n\n"


class ProtocolError(Exception):
    """Malformed frame. Always fatal for the connection -- never try to resync
    a byte stream whose framing you have already lost."""


class TextCodec:
    """How header TEXT becomes bytes and back. The percent-encoding works on
    bytes; which codepage those bytes are in is the protocol's decision."""

    def codepage(self) -> str:
        return "latin-1"

    def encode(self, text: str) -> bytes:
        return text.encode(self.codepage(), errors="replace")

    def decode(self, data: bytes) -> str:
        return data.decode(self.codepage(), errors="replace")


_SAFE = frozenset(range(0x20, 0x7F)) - {ord("%")}

_PCT = re.compile(r"%([0-9A-Fa-f]{2})")


def quote(raw: bytes) -> str:
    """Percent-encode bytes to printable ASCII.

    Percent-encoding rather than backslashes because the guest reads these
    with strchr, and backslash escaping is exactly the tar pit claudewin9x
    fell into (its system prompt needed a section on quadrupling
    backslashes)."""
    return "".join(chr(b) if b in _SAFE else "%%%02X" % b for b in raw)


def unquote(value: str, codec: TextCodec) -> str:
    """Inverse of quote, back to text. Never raises: a header is not worth a
    dropped link.

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
            # The codec's codepage, not a literal: `quote` saw the bytes
            # the codec made, so decoding with a different table would
            # invent bytes rather than reverse them.
            raw.extend(value[i].encode(codec.codepage(), errors="replace"))
            i += 1
    return codec.decode(bytes(raw))


@dataclass
class Frame:
    type: str
    headers: dict[str, str] = field(default_factory=dict)
    body: bytes = b""

    #: Header text <-> bytes. A protocol subclasses Frame to plug in its own.
    text: ClassVar[TextCodec] = TextCodec()

    @property
    def enc(self) -> str:
        return self.headers.get("enc", "raw")

    def encode(self) -> bytes:
        if not self.type or not self.type.isupper():
            raise ProtocolError(f"frame type must be uppercase: {self.type!r}")
        if len(self.type) > MAX_TYPE:
            raise ProtocolError(
                f"frame type too long: {self.type!r} ({len(self.type)} > {MAX_TYPE})"
            )
        max_h = MAX_HELLO_HEADERS if self.type == "HELLO" else MAX_HEADERS
        if len(self.headers) > max_h:
            raise ProtocolError(
                f"too many headers: {len(self.headers)} exceeds cap {max_h}"
            )

        out = [self.type.encode("ascii"), b"\n"]
        for k, v in self.headers.items():
            if not k or not all(c.isalnum() or c == "_" for c in k):
                raise ProtocolError(f"bad header key: {k!r}")
            if len(k) > MAX_HEADER_KEY:
                raise ProtocolError(
                    f"header key too long: {k!r} ({len(k)} > {MAX_HEADER_KEY})"
                )
            quoted = quote(self.text.encode(str(v)))
            if len(quoted) > MAX_HEADER_VALUE:
                raise ProtocolError(
                    f"header {k!r} value too long: wire length {len(quoted)}B "
                    f"exceeds guest limit {MAX_HEADER_VALUE}B"
                )
            out.append(f"{k}={quoted}\n".encode("ascii"))
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
    def decode(cls, payload: bytes) -> Self:
        if len(payload) > MAX_FRAME:
            raise ProtocolError(
                f"payload {len(payload)}B exceeds MAX_FRAME {MAX_FRAME}B"
            )
        split = payload.find(_HEADER_END)
        if split < 0:
            raise ProtocolError("no header terminator in frame")

        try:
            head = payload[:split].decode("ascii")
        except UnicodeDecodeError as e:
            raise ProtocolError(f"non-ascii byte in header block: {e}") from e
        body = payload[split + 2 :]

        lines = head.split("\n")
        ftype = lines[0]
        if not ftype:
            raise ProtocolError("empty frame type")
        if len(ftype) > MAX_TYPE:
            raise ProtocolError(
                f"frame type too long: {ftype!r} ({len(ftype)} > {MAX_TYPE})"
            )

        max_h = MAX_HELLO_HEADERS if ftype == "HELLO" else MAX_HEADERS
        headers: dict[str, str] = {}
        for line in lines[1:]:
            if not line:
                continue
            if len(headers) >= max_h:
                raise ProtocolError(
                    f"too many headers: exceeds {max_h}"
                )
            k, eq, v = line.partition("=")
            if not eq:
                raise ProtocolError(f"header line without '=': {line!r}")
            if len(k) > MAX_HEADER_KEY:
                raise ProtocolError(
                    f"header key too long: {k!r} ({len(k)} > {MAX_HEADER_KEY})"
                )
            if len(v) > MAX_HEADER_VALUE:
                raise ProtocolError(
                    f"header {k!r} value too long: wire length {len(v)} > {MAX_HEADER_VALUE}"
                )
            if k in headers:
                raise ProtocolError(f"duplicate header key: {k!r}")
            headers[k] = unquote(v, cls.text)

        return cls(type=ftype, headers=headers, body=body)


F = TypeVar("F", bound=Frame)


class FrameReader(Generic[F]):
    """Incremental reassembler. Feed it whatever recv() returned.

    `frame_class` decides how header text is decoded: the bridge's reader
    passes its folding Frame, gremote's client the plain one."""

    def __init__(self, frame_class: type[F], max_frame: int = MAX_FRAME) -> None:
        self._frame_class = frame_class
        self._buf = bytearray()
        self._max = max_frame

    def feed(self, data: bytes) -> list[F]:
        self._buf.extend(data)
        frames: list[F] = []
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
            frames.append(self._frame_class.decode(payload))
