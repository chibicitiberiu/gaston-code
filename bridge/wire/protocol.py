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
# goes: one constructor per frame type the bridge sends, so header
# stringification and `fold.encode` are decided once here rather than
# independently at every `Frame(...)` call site (docs/audit/02-bridge-python.md
# #1.1). `GuestLink` (bridge/wire/link.py) is the seam that owns HOW a frame
# reaches the wire -- the writer, the send lock, the compressor, the chunking.
# These functions are what decides WHAT the bridge is allowed to send: a
# constructor takes the values that vary and returns a `Frame` whose headers
# are always shaped the same way for that type.
#
# `stamp=` used to live at the `GuestLink.send` call site, marking which
# frames entered the per-session replay buffer -- an EXEC or EXECKILL must
# never be replayed, a reissue after reconnect must not replay either, but a
# fresh TURN start should. Retired with the buffer itself (2026-09-18,
# docs/BACKLOG.md #5): the guest never sent `from=`, so nothing ever replayed
# anything, and every frame just goes out once, when it is sent. The
# never-replay invariant for EXEC/EXECKILL/SYNC* still holds -- see
# `test_exec_is_never_re_delivered` in test_server_integration.py -- it is
# just no longer a flag this module hands to the sender.
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


def hello(secret: str, sess: str | None = None) -> Frame:
    """First frame from the guest. Carries the protocol version and the cap so
    a mismatch is caught before anything else, and optionally names the
    session it is reattaching to.

    Used to also carry `from=<seq>`, asking the bridge to replay whatever the
    guest missed. Retired 2026-09-18 (docs/BACKLOG.md #5) along with the
    replay buffer it drove: the guest never sent it, so the path it would
    have triggered was dead code from Phase 1 onward.
    """
    h = {"ver": str(PROTO_VERSION), "secret": secret, "max_frame": str(MAX_FRAME)}
    if sess:
        h["sess"] = sess
    return Frame("HELLO", h)


def error(code: str, msg: str) -> Frame:
    """Errors need a frame type of their own, or a version-mismatch rejection
    arrives as bytes the peer cannot parse."""
    return Frame("ERROR", {"code": code, "msg": msg})


# --- session lifecycle --------------------------------------------------


def ready(sess: str, model: str, page: int, total: int) -> Frame:
    """First frame after HELLO succeeds, carrying the session id.

    Used to also carry `next_seq=`, the replay cursor a `from=` on the next
    HELLO would resume from. Retired with `hello()`'s `from_seq` -- see its
    docstring.
    """
    return Frame("READY", {
        "sess": sess,
        "ver": str(PROTO_VERSION),
        "model": model,
        "page": str(page),
        "total": str(total),
    })


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
    """Sent both for a fresh turn and, by `Bridge._reissue_turn`, to restore
    the busy UI after a reconnect -- same headers either way."""
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


def turn_end(ok: bool, subtype: str, cost_usd: float | None, ms: int,
             ctx_pct: int | None = None) -> Frame:
    """`ctx_pct` rides on this frame rather than getting one of its own.

    It is one small integer that only changes when a turn ends, and the guest
    is already parsing this frame at exactly that moment. A new frame type
    would mean a new dispatch entry on both sides and a new thing that can
    arrive when nothing expects it, to carry a number nobody needs sooner.
    Absent when the bridge could not get it, which the guest reads as "leave
    the gauge alone" rather than as zero.
    """
    h = {
        "state": "end",
        "ok": "1" if ok else "0",
        "subtype": subtype,
        "cost_usd": f"{cost_usd:.4f}" if cost_usd else "",
        "ms": str(ms),
    }
    if ctx_pct is not None:
        h["ctx_pct"] = str(ctx_pct)
    return Frame("TURN", h)


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


# --- export ----------------------------------------------------------------
#
# Three types, bridge to guest, for File > Export as HTML. The guest asks with
# a bare EXPORT and then writes what comes back straight to a file.
#
# A SEPARATE FAMILY rather than reusing TEXT, for one reason: TEXT is display.
# The guest hands a TEXT body to MSHTML, and an export is hundreds of KB on a
# machine with tens of KB free -- it must never reach the DOM, and a mode
# header on a frame type whose whole contract is "this is for the document"
# is exactly the kind of overload that gets missed on one side.
#
# No lane-table row is needed: `rc_frame_lane` returns RC_LANE_UI for anything
# it does not recognise, which is where these belong, and `enc=deflate` is
# handled centrally in the guest's reader before dispatch.


def export_start(total: int) -> Frame:
    """How many bytes are coming, sent before any of them.

    NO FILENAME. The guest picks the path before it asks -- it has to, because
    a Save dialog opened after this frame would be pumping messages while the
    chunks arrived with nowhere to go. It also owns its own filesystem and
    declared its own path rules at handshake, so a name asserted from here
    would be the bridge guessing at 8.3 rules it was told about.

    `total` is in DECOMPRESSED bytes.
    """
    return Frame("EXPORTSTART", {"total": str(total)})


def export_data(off: int, total: int, enc: str, body: bytes) -> Frame:
    """One chunk.

    `off` is the decompressed offset, so the guest can refuse a chunk that
    does not continue where the last one stopped rather than writing a file
    with a hole in it. `total` repeats on every chunk for the same reason
    `send_html` repeats it -- the progress figure needs a denominator, and a
    guest that missed the START would otherwise never have one.
    """
    return Frame("EXPORTDAT",
                 {"off": str(off), "total": str(total), "enc": enc}, body)


def export_end(ok: bool, *, bytes_: int = 0, reason: str = "") -> Frame:
    """The transfer is over, one way or the other.

    An END that says `status=error` is the point of having one: without it a
    transfer that died halfway is indistinguishable from one still arriving,
    and the guest would leave a half-written file looking finished.
    """
    h = {"status": "ok" if ok else "error", "bytes": str(bytes_)}
    if reason:
        h["reason"] = fold.encode(reason).decode("cp1252", "replace")
    return Frame("EXPORTEND", h)


# --- exec ----------------------------------------------------------------


def exec_cmd(exec_id: str, command: str, *, timeout_ms: int, shell: bool,
             cwd: str = "") -> Frame:
    """The command goes in the BODY, never a header: a header value is
    truncated at 255 bytes silently, and a truncated command is still
    EXECUTED -- `del c:\\temp\\*.tmp` cut short becomes `del c:\\temp\\*`.

    `encode_exact`, NOT `fold.encode`. This used to fold, which is the display
    transliterator: it rewrote the command on its way to the wire, so the
    bytes that ran were not the bytes anything had inspected. U+00B7 became
    '.', U+2502 became '|', U+00BB became '>>'. See fold.encode_exact.

    It raises on a character CP1252 cannot carry. Callers refuse; nobody
    approximates a command line.
    """
    headers = {"id": exec_id, "timeout_ms": str(timeout_ms),
               "shell": "1" if shell else "0"}
    if cwd:
        headers["cwd"] = cwd
    return Frame("EXEC", headers, fold.encode_exact(command))


def exec_kill(exec_id: str) -> Frame:
    return Frame("EXECKILL", {"id": exec_id})


# --- model / effort menus ---------------------------------------------


def option(kind: str, id: str, label: str, current: bool, *,
           disabled: bool = False, hint: str = "", desc: str = "") -> Frame:
    """One row of a bridge-driven menu.

    `id` is what the guest sends back; `label` is what it shows. They differ
    on purpose for commands, where the label carries the argument hint
    ("/compact <optional custom summarization instructions>") and the id is
    the bare command.

    `hint` is present only when the command TAKES an argument, and the guest
    reads it as "put this in the input box rather than sending it" -- so the
    label showing a hint and the click opening the box for you to fill it in
    are the same fact, not two that can drift.

    `desc` is extra text for the MENU only, kept out of `label` because the
    label is also what a toolbar button shows and "Haiku 4.5 - Fastest for
    quick answers" does not fit on a 640px screen while "Haiku" does.
    """
    h = {"kind": kind, "id": id, "label": label,
         "current": "1" if current else "0"}
    if disabled:
        h["disabled"] = "1"
    if hint:
        h["hint"] = "1"
    if desc:
        h["desc"] = desc
    return Frame("OPTION", h)


def optionsend() -> Frame:
    return Frame("OPTIONSEND", {})


def pong() -> Frame:
    return Frame("PONG", {})
