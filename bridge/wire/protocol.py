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

from . import fold
from . import frame as _frame
from .frame import (  # noqa: F401  -- re-exported: the bridge imports these from here
    MAX_FRAME,
    MAX_HEADER_KEY,
    MAX_HEADER_VALUE,
    MAX_HEADERS,
    MAX_HELLO_HEADERS,
    MAX_KEY,
    MAX_TYPE,
    ProtocolError,
    TextCodec,
)

PROTO_VERSION = 2


class _FoldText(TextCodec):
    """Header text for the bridge <-> guest protocol: folded to the
    connection's codepage (CP1252 by default), so a header and the body it
    describes agree about what a character is -- the guest has one codepage
    and it is not Unicode."""

    def codepage(self) -> str:
        return fold.codec()

    def encode(self, text: str) -> bytes:
        return fold.encode(text)

    def decode(self, data: bytes) -> str:
        return fold.decode(data)


_FOLD = _FoldText()


def _quote(value: str) -> str:
    """Escape a header value to printable ASCII, folded first -- see
    _FoldText and bridge/wire/frame.py's quote."""
    return _frame.quote(_FOLD.encode(value))


def _unquote(value: str) -> str:
    """Inverse of _quote. Never raises: a header is not worth a dropped link."""
    return _frame.unquote(value, _FOLD)


def truncate_header_value(val: str, max_wire: int = MAX_HEADER_VALUE) -> str:
    """Truncate val so that its percent-encoded wire representation does not
    exceed max_wire bytes. Appends '...' when truncated."""
    if len(_quote(val)) <= max_wire:
        return val
    marker = "..."
    for end in range(min(len(val), max_wire), 0, -1):
        candidate = val[:end] + marker
        if len(_quote(candidate)) <= max_wire:
            return candidate
    return marker[:max_wire]


class Frame(_frame.Frame):
    """The bridge's frames: libwire's codec with header text folded to the
    connection's codepage."""

    text = _FOLD


class FrameReader(_frame.FrameReader[Frame]):
    """The bridge's reader: decodes into protocol.Frame."""

    def __init__(self, max_frame: int = MAX_FRAME) -> None:
        super().__init__(Frame, max_frame)


# --- frame constructors -----------------------------------------------------
#
# This IS the protocol's vocabulary, as far as the bridge -> guest direction
# goes: one constructor per frame type the bridge sends, so header
# stringification and `fold.encode` are decided once here rather than
# independently at every `Frame(...)` call site. `GuestLink`
# (bridge/wire/link.py) is the seam that owns HOW a frame reaches the wire --
# the writer, the send lock, the compressor, the chunking. These functions
# decide WHAT the bridge is allowed to send: a constructor takes the values
# that vary and returns a `Frame` whose headers are always shaped the same
# way for that type.
#
# There is no replay buffer and no `stamp=` flag on a send: every frame goes
# out once, when it is sent. The never-replay invariant for
# EXEC/EXECKILL/SYNC* is enforced by never replaying anything -- see
# `test_exec_is_never_re_delivered` in test_server_integration.py.
#
# HELLO and ERROR predate the rest: they are sent before a GuestSession
# exists to hang a send helper off (both fakegui and the guest open a
# connection with HELLO; ERROR is sent standalone whenever a handshake step
# fails before any session-scoped state exists to send it through).
#
# `text_frame()` below is the constructor for the TEXT frame the bridge sends
# today; it is unrelated to any similarly-named helper elsewhere.


def hello(secret: str, sess: str | None = None) -> Frame:
    """First frame from the guest. Carries the protocol version and the cap so
    a mismatch is caught before anything else, and optionally names the
    session it is reattaching to. There is no replay cursor here -- reconnect
    always resumes from current state, never from a missed-frame log.
    """
    h = {"ver": str(PROTO_VERSION), "secret": secret, "max_frame": str(MAX_FRAME)}
    if sess:
        h["sess"] = sess
    return Frame("HELLO", h)


def error(code: str, msg: str) -> Frame:
    """Errors need a frame type of their own, or a version-mismatch rejection
    arrives as bytes the peer cannot parse."""
    return Frame("ERROR", {
        "code": truncate_header_value(code, MAX_HEADER_VALUE),
        "msg": truncate_header_value(msg, MAX_HEADER_VALUE),
    })


# --- session lifecycle --------------------------------------------------


def ready(sess: str, model: str, page: int, total: int,
          conv: str = "") -> Frame:
    """First frame after HELLO succeeds, carrying the session id.

    `sess` and `conv` are two different things and the difference matters
    here. `sess` is the per-CONNECTION handle, stable for the life of the
    socket and never the same as a conversation's id (02.24). `conv` is which
    CONVERSATION is on screen, and it is new: the guest keeps its own cached
    copy of a transcript keyed on that, and before this frame carried it,
    nothing told a freshly connected guest which conversation it was looking
    at unless a SWITCHED happened to follow -- which it does after a switch
    and does not after a plain reconnect.
    """
    h = {
        "sess": sess,
        "ver": str(PROTO_VERSION),
        "model": model,
        "page": str(page),
        "total": str(total),
    }
    if conv:
        h["conv"] = conv
    return Frame("READY", h)


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
        "title": truncate_header_value(title[:120], MAX_HEADER_VALUE),
        "cwd": truncate_header_value(cwd[:120], MAX_HEADER_VALUE),
        "group": truncate_header_value(group, MAX_HEADER_VALUE),
        "current": "1" if current else "0",
    })


def sessions_end(hidden: int = 0) -> Frame:
    """`hidden` is how many conversations were cut by the display cap.

    SAID RATHER THAN SILENTLY DROPPED. The cap is the client's own table --
    `MAX_CHATS` in win/app/chatlist.h, less the age-group headers -- so a row
    past it is a row the list would refuse, and cutting here keeps the wire
    honest. What it did not do was tell anybody: the user crossed 59
    conversations on 2026-10-01 and the older ones simply stopped appearing,
    with the count in the bridge's log and nowhere the person could see it.
    Zero omits the header, so a client that ignores it is unaffected.
    """
    h = {}
    if hidden > 0:
        h["hidden"] = str(hidden)
    return Frame("SESSIONSEND", h)


def notice(kind: str, text: str) -> Frame:
    return Frame("NOTICE", {"kind": kind}, fold.encode(text))


def status_frame(text: str = "") -> Frame:
    return Frame("STATUS", {"text": text})


# --- turns ----------------------------------------------------------------


def turn_start() -> Frame:
    """Sent both for a fresh turn and, by `SessionCoordinator.reissue_turn`, to restore
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
             ctx_pct: int | None = None, conv: str = "") -> Frame:
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
    # Which conversation this turn belongs to. A NEW conversation only gets
    # an id during its first turn, and READY (empty then) and SWITCHED (not
    # sent) never said it -- so the guest had nothing to ask a restarted
    # bridge for, and landed in a fresh conversation (2026-10-03).
    if conv:
        h["conv"] = conv
    return Frame("TURN", h)


def anchor_frame(page: int, off: int, preview: str) -> Frame:
    """Where a user turn landed in the transcript, so the guest can jump
    back to it from history."""
    return Frame("ANCHOR", {
        "page": str(page), "off": str(off),
        "preview": truncate_header_value(preview, MAX_HEADER_VALUE),
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
        # Folded and back, so the header carries only what the guest can
        # actually render. Through the CONNECTION's codepage, not a literal:
        # the round trip has to use one table both ways or it invents
        # characters rather than removing them.
        h["reason"] = fold.decode(fold.encode(reason))
    return Frame("EXPORTEND", h)


# --- exec ----------------------------------------------------------------


def exec_cmd(exec_id: str, command: str, *, timeout_ms: int, shell: str = "",
             cwd: str = "", shown: str = "") -> Frame:
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
    # `shell` IS THE INVOCATION PREFIX, not a flag -- e.g.
    # `C:\\WINDOWS\\COMMAND.COM /c`, composed from what detection found and
    # what that shell's profile says its run switch is. The guest splices it
    # in front of the command and knows nothing else about shells; empty
    # means spawn the program directly. See PROTO_VERSION above for why an
    # older guest cannot be handed this.
    headers = {"id": exec_id, "timeout_ms": str(timeout_ms),
               "shell": shell or ""}
    if cwd:
        headers["cwd"] = cwd
    # WHAT THE AGENT ASKED FOR, when the body is the exit-code wrapper that
    # runs it: the client's command log (Options > Misc) records this, not
    # "C:\WINDOWS\TEMP\GEXEC.bat". A header, so it is CUT at 255 -- which
    # is fine for a record and is why the body, never this, is what runs.
    if shown and shown != command:
        headers["cmd"] = shown[:MAX_HEADER_VALUE - 5]
    return Frame("EXEC", headers, fold.encode_exact(command))


def exec_kill(exec_id: str) -> Frame:
    return Frame("EXECKILL", {"id": exec_id})


# --- driving the desktop -------------------------------------------------
#
# One constructor per frame type, as above. The guest answers every one of
# these with exactly one UIEND, so a caller that gets nothing has lost the
# connection rather than been ignored.


def ui_tree(ui_id: str, *, scope: str = "desktop", pid: int = 0,
            max_nodes: int = 400, text: bool = True, msaa: bool = False,
            hidden: bool = False, wait_for: str = "", wait_ms: int = 5000,
            settle_ms: int = 0, timeout_ms: int = 8000,
            inproc: bool = False, target: str = "",
            items: bool = True, acc: bool = False) -> Frame:
    """Read the control tree.

    `wait_for` and `settle_ms` are parameters rather than a separate op on
    purpose: the loop an agent runs is "do a thing, then look", and folding
    the wait into the look turns two round trips into one. Behind a socket
    to a 200MHz machine the round trip is the expensive part.

    `msaa` defaults OFF. Each MSAA field is a marshalled call into another
    process and a tree is hundreds of nodes; the walk already derives a role
    from class and style for free, which covered over half the desktop on
    both measured machines.
    """
    h = {"id": ui_id, "scope": scope, "max_nodes": str(max_nodes),
         "text": "1" if text else "0", "msaa": "1" if msaa else "0",
         "hidden": "1" if hidden else "0", "timeout_ms": str(timeout_ms)}
    if pid:
        h["pid"] = str(pid)
    if wait_for:
        h["wait_for"] = truncate_header_value(wait_for)
        h["wait_ms"] = str(wait_ms)
    if settle_ms:
        h["settle_ms"] = str(settle_ms)
    if acc:
        # ONE CONTROL'S ITEMS (its accessible children), not the window tree:
        # the names `input`'s `item` targets. `target` names the control.
        h["acc"] = "1"
        h["target"] = truncate_header_value(target)
        return Frame("UITREE", h)
    if inproc:
        # Opt-in, and it does NOT fall back: the two tiers give different
        # answers and only one of them can read a list box, so a caller
        # that asked for this and quietly got the other would conclude a
        # list is empty. The guest says which tier answered.
        h["inproc"] = "1"
        h["items"] = "1" if items else "0"
        if target:
            h["target"] = truncate_header_value(target)
    return Frame("UITREE", h)




def fs_get(fs_id: str, path: str, *, max_bytes: int = 0) -> Frame:
    """Read a file on the connected machine, at any path.

    The path is a HEADER and not the body. SYNCPUT puts its path in the body,
    length-prefixed, because that format predates the header being an option,
    and getting it back out needs a parser with four refusals; a Windows path
    cannot contain a newline, so there is nothing to work around here.

    `max_bytes` is enforced ON THE GUEST, not by counting what arrives. A cap
    applied on this side still has the guest read and send the whole file, so
    a request for a swap file is a socket held open for as long as it takes.
    Zero means the guest's own ceiling, and the reply says `truncated=1` when
    either bit.
    """
    h = {"id": fs_id, "path": truncate_header_value(path)}
    if max_bytes:
        h["max_bytes"] = str(max_bytes)
    return Frame("FSGET", h)


def fs_put(fs_id: str, path: str, off: int, chunk: bytes) -> Frame:
    """One chunk of a file going the other way.

    `path` rides every frame but is read only from the one at offset zero --
    the guest captures it once for the life of the transfer, because a
    mid-stream change would splice a file out of two destinations.

    `off` is checked by the guest against what it has already written. A gap
    means a frame was lost, duplicated or reordered, and writing it anyway
    makes a file of the right length and the wrong contents.
    """
    return Frame("FSPUT", {"id": fs_id, "path": truncate_header_value(path),
                           "off": str(off)}, chunk)


def fs_putend(fs_id: str, crc: int) -> Frame:
    """Close a transfer and ask the guest to put it in place.

    The checksum is over the raw bytes. Nothing on the fs lane classifies or
    folds line endings -- that is the sync layer's negotiation, and this is a
    byte-exact transfer.
    """
    return Frame("FSPUTEND", {"id": fs_id, "crc": str(crc)})


def fs_op(fs_id: str, op: str, path: str, to: str = "", *,
          overwrite: bool = False, permanent: bool = False) -> Frame:
    """FSMKDIR, FSRM, FSRMDIR, FSMV, FSCOPY or FSSTATS (`to` for a move or
    a copy). `overwrite` lets a copy or move replace; `permanent` makes a
    delete skip the Recycle Bin. Each is answered by one FSOPEND."""
    h = {"id": fs_id, "path": truncate_header_value(path)}
    if to:
        h["to"] = truncate_header_value(to)
    if overwrite:
        h["overwrite"] = "1"
    if permanent:
        h["permanent"] = "1"
    return Frame(op, h)


def fs_ls(fs_id: str, path: str, *, max_entries: int = 0) -> Frame:
    """One level of one directory on the connected machine, at any path.

    Rows arrive in FSENT bodies, one per line, `<type> <size> <mtime> <attrs>
    <name>` -- the same shape gremote's LS has used since step 4
    (win/remote/fsops.cpp), because the primitive underneath is the same
    `listDirectory` and two formats for one answer is two parsers to keep
    right.

    `max_entries` is enforced ON THE GUEST and the reply says `truncated=1`
    when it bit. A capped listing is NOT a short listing: a caller that reads
    one as the whole directory has been told files were deleted.
    """
    h = {"id": fs_id, "path": truncate_header_value(path)}
    if max_entries:
        h["max_entries"] = str(max_entries)
    return Frame("FSLS", h)


def fs_drives(fs_id: str, *, probe: bool = False) -> Frame:
    """Every drive letter the machine has, with type, label and free space.

    `probe` is off by default (BACKLOG 20): against a removable drive or a
    CD-ROM bay, the label and free-space calls move the mechanism -- an
    audible seek on a real floppy, and on some drives a multi-second retry
    when it is empty. The letter and the type are unconditional either way;
    a caller that genuinely wants to know what is in A: sets `probe=True`
    and gets the old behaviour.
    """
    h = {"id": fs_id}
    if probe:
        h["probe"] = "1"
    return Frame("FSDRIVES", h)






def ui_shot(ui_id: str, *, x: int = 0, y: int = 0, w: int = 0, h: int = 0,
            bpp: int = 8) -> Frame:
    """Capture the screen, or a rectangle of it.

    A width and a height, not a right and a bottom: the guest adds the
    origin, so a caller says "100 wide" and is not asked to do the
    arithmetic.
    """
    return Frame("UISHOT", {"id": ui_id, "x": str(x), "y": str(y),
                            "w": str(w), "h": str(h), "bpp": str(bpp)})


def ui_run(ui_id: str, body: bytes, *, timeout_ms: int, step_ms: int,
           settle: int = 0) -> Frame:
    """The `input` tool's step list, compiled by agent/steps.py.

    The steps ride the BODY: a header value is capped at 255 encoded bytes,
    and a selector clipped to fit one is a different selector. The guest
    answers with one UIEND whose body holds a line per step that ran."""
    h = {"id": ui_id, "timeout_ms": str(timeout_ms), "step_ms": str(step_ms)}
    if settle:
        h["settle"] = str(settle)
    return Frame("UIRUN", h, body)


def ui_cancel(ui_id: str) -> Frame:
    """Stop a running step list. The guest answers on the run's own UIEND,
    with `cancelled`; this frame has no reply of its own."""
    return Frame("UICANCEL", {"id": ui_id})


# --- processes ------------------------------------------------------------


def ps_list(ps_id: str) -> Frame:
    """Every process on the machine. Answered on the guest's reader thread,
    so it works while an EXEC is stuck -- which is when it is needed."""
    return Frame("PSLIST", {"id": ps_id})


def ps_launch(ps_id: str, command: str, *, cwd: str = "",
              show: str = "") -> Frame:
    """Start a program and do not wait for it. `show` is "", "min" or "max".
    The command can exceed a header's 255 bytes only by being a command
    nobody should be launching; it is clipped like any other header."""
    h = {"id": ps_id, "command": truncate_header_value(command)}
    if cwd:
        h["cwd"] = truncate_header_value(cwd)
    if show:
        h["show"] = show
    return Frame("PSLAUNCH", h)


def ps_kill(ps_id: str, pid: str, *, force: bool = False) -> Frame:
    """End one process. `pid` is the hex id `ps_list` reported; `force`
    lifts the guest's softer refusals (Explorer, 16-bit programs), never
    its hard ones (the system's own processes)."""
    h = {"id": ps_id, "pid": pid}
    if force:
        h["force"] = "1"
    return Frame("PSKILL", h)


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
    h = {"kind": kind,
         "id": truncate_header_value(id, MAX_HEADER_VALUE),
         "label": truncate_header_value(label, MAX_HEADER_VALUE),
         "current": "1" if current else "0"}
    if disabled:
        h["disabled"] = "1"
    if hint:
        h["hint"] = "1"
    if desc:
        h["desc"] = truncate_header_value(desc, MAX_HEADER_VALUE)
    return Frame("OPTION", h)


def optionsend() -> Frame:
    return Frame("OPTIONSEND", {})


def pong() -> Frame:
    return Frame("PONG", {})


def listent(list_id: str, rows: list, acp: str = "cp1252") -> Frame:
    """Rows of one list for the Options inspector: `entry\treason\tsource`
    lines in the client's codepage. Several frames per list, each well
    under MAX_FRAME."""
    return Frame("LISTENT", {"list": list_id},
                 "".join(r + "\n" for r in rows).encode(acp, "replace"))


def listend(list_id: str, hidden: int = 0, **extra) -> Frame:
    """The end of a list (`hidden` rows past the cap were not sent), or the
    answer to a test line: `value`, `mode`, and the findings in the body."""
    h = {"list": list_id, "hidden": str(hidden)}
    body = extra.pop("body", b"")
    h.update({k: str(v)[:200] for k, v in extra.items()})
    return Frame("LISTEND", h, body)


def prefsack(ok: bool, key: str = "", why: str = "") -> Frame:
    """The answer to a PREFS re-sent from Options OK. Never an ERROR: an
    ERROR mid-turn aborts the client's turn UI (mainwindow.cpp onError), and
    a setting the bridge did not take is not a turn that failed."""
    h = {"ok": "1" if ok else "0"}
    if key:
        h["key"] = key[:64]
    return Frame("PREFSACK", h, why.encode("utf-8")[:200] if why else b"")
