"""HELLO's header budget, held on two sides in two languages.

The guest builds HELLO into two fixed arrays of `RC_HELLO_MAX` entries
(`win/net/net.cpp`) and the bridge refuses a HELLO carrying more than
`MAX_HELLO_HEADERS` (`bridge/wire/frame.py`). Neither number is derived from
the other and neither is derived from the list of headers actually sent, so
the three can drift apart in two directions, both silent:

  * past the GUEST's array, `HDR()` logs "array full" and DROPS the header.
    The connection succeeds. The bridge then sees a machine that declares
    one capability fewer than it has -- and every default in
    `bridge/files/caps.py` is the permissive-to-the-guest reading, so the
    symptom is a path rule quietly relaxed, not a refusal;
  * past the BRIDGE's cap, `Frame.decode` raises and the handshake dies with
    nothing on the guest but "Cannot reach the bridge ... retrying".

Adding a header is a two-line change that can cost either of those, so the
count is asserted rather than remembered. The `machine` header (BACKLOG.md
§21) went in at 27 of 32 and is why this file exists, and `automation`
(§19) at 28.

Reads both sources as text. No Wine, no build.
"""

from __future__ import annotations

import re
from pathlib import Path

import conftest

from bridge.wire.frame import MAX_HEADER_VALUE, MAX_HELLO_HEADERS

NET_CPP = Path(conftest.REPO) / "win" / "net" / "net.cpp"
CAPS_H = Path(conftest.REPO) / "win" / "net" / "caps.h"


#: C comments, stripped before anything is counted. net.cpp's own comment
#: about the stringify macro contains the literal text `HDR("eol", ...)`, and
#: counting that as a second `eol` is the kind of false positive that gets a
#: guard deleted rather than fixed.
_COMMENT = re.compile(r"/\*.*?\*/|//[^\n]*", re.S)


def _src() -> str:
    return _COMMENT.sub(" ", NET_CPP.read_text(encoding="utf-8"))


def _guest_array_cap() -> int:
    m = re.search(r"#define\s+RC_HELLO_MAX\s+(\d+)", _src())
    assert m, f"no #define RC_HELLO_MAX in {NET_CPP}"
    return int(m.group(1))


def _hello_keys() -> list[str]:
    """Every key `rc_net_start` can put in HELLO, in source order.

    Parsed out of the HDR() calls rather than listed here, for the same
    reason `test_fakegui_conformance` parses them: a list maintained beside
    a function that grows is a list that stops matching it.
    """
    keys = re.findall(r'HDR\("([a-z0-9_]+)"', _src())
    assert keys, f"no HDR() calls found in {NET_CPP}"
    return keys


def test_every_header_is_sent_at_most_once():
    """Two HDR() calls with one key is a header whose value depends on which
    one the far side keeps, and the two parsers disagree: the guest's own
    `rc_frame_get` returns the FIRST and Python's dict keeps the LAST."""
    keys = _hello_keys()
    dupes = sorted({k for k in keys if keys.count(k) > 1})
    assert not dupes, f"HELLO sends these twice: {dupes}"


def test_the_guests_array_holds_every_header_it_can_send():
    """All of them at once -- the optional ones are not optional together.
    `sync_root` is there whenever sharing is configured, `sess` on a resume,
    and `os`/`screen`/`disk_free_kb` on any machine that answers the call
    behind them, which is every real one."""
    sent, cap = len(_hello_keys()), _guest_array_cap()
    assert sent <= cap, (
        f"rc_net_start can send {sent} headers into an array of {cap}; "
        f"HDR() would drop the last {sent - cap} and only say so in the log")


def test_the_bridge_accepts_everything_the_guest_can_build():
    """The guest's array is the thing that bounds what it sends, so the
    bridge's cap has to cover the array and not just today's header list --
    otherwise the next header added is accepted by the guest and refused by
    the bridge, which is a connection that never completes."""
    assert _guest_array_cap() <= MAX_HELLO_HEADERS, (
        f"win/net/net.cpp builds up to {_guest_array_cap()} headers and "
        f"bridge/wire/frame.py refuses past {MAX_HELLO_HEADERS}")


def test_there_is_room_left_to_add_one():
    """Not a style rule. The header list IS the capability declaration
    (CLAUDE.md, "Client rules are declared at handshake"), so running out of
    room means the next client rule has nowhere to go -- and the cheap
    workaround, packing them into bit fields, is the thing caps.h exists to
    refuse."""
    sent, cap = len(_hello_keys()), _guest_array_cap()
    assert cap - sent >= 2, (
        f"{sent} of {cap} HELLO headers used. Raise RC_HELLO_MAX in "
        f"{NET_CPP} (and MAX_HELLO_HEADERS in bridge/wire/frame.py with it) "
        f"before adding another")


def test_the_machine_id_is_declared_at_the_handshake():
    """BACKLOG.md §21: which guest this is has to be a HELLO header, because
    the bridge files conversations under it from the first frame onwards --
    before anything else could have told it."""
    assert "machine" in _hello_keys()


def test_a_machine_id_cannot_overflow_a_header_value():
    """The guest refuses an id that does not fit its own 40-byte buffer, and
    that buffer has to stay well inside the wire's value limit."""
    m = re.search(r"char\s+machine\[(\d+)\]", (Path(conftest.REPO) / "win" /
                                               "net" / "net.h").read_text())
    assert m, "no machine[] field in win/net/net.h"
    assert int(m.group(1)) <= MAX_HEADER_VALUE


def test_screen_control_is_declared_at_the_handshake():
    """BACKLOG.md 19: whether the agent may drive this screen decides whether
    five tools exist for the session, and the tool list is built from HELLO --
    so this cannot arrive on a later frame. It is also the one HELLO value
    that is not a compile-time literal apart from the codepage: it comes from
    `[ui] automation` in gaston.ini via rc_net_set_automation."""
    assert "automation" in _hello_keys()


def test_every_cap_in_caps_h_reaches_the_wire():
    """A `RC_CAP_*` nobody sends is a rule the bridge will never hear about,
    and the bridge's default for an undeclared field is the permissive one."""
    declared = set(re.findall(r"#define\s+RC_CAP_([A-Z0-9]+)\s",
                              CAPS_H.read_text()))
    sent = {k.upper() for k in _hello_keys()}
    assert declared <= sent, (
        f"caps.h declares {sorted(declared - sent)} and HELLO never sends "
        f"them")
