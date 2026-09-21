"""The frame header contract, checked ACROSS the two implementations.

`framecheck.exe` proves the guest's builder and parser agree with each other.
That is necessary and not sufficient: two halves of one file can agree on a
format the other side does not speak, and that is exactly what happened here.
`frameproto.h` promised percent-encoded header values from the day it was
written; the parser decoded them and the builder never encoded them, so the
guest sent raw bytes to a bridge that percent-DECODES everything it receives.
A value containing `%41` arrived as `A`. Nothing failed, because nothing ran
the guest's builder against the bridge's parser.

So this file does that. `framecheck --emit` hands back the exact bytes the
SHIPPING builder produces, and they go through `bridge/wire/protocol.py`'s
FrameReader -- the real one the bridge runs. A change on either side fails
here, which is the property the manifest parser got in `bridge/files/
manifest.py` for the same reason.
"""
from __future__ import annotations

import struct
import subprocess

import pytest

import conftest
from conftest import WINE, guest_exe, wine_env

from bridge.wire.protocol import FrameReader

#: This suite relied on `guest_exe`'s internal skip instead of marking
#: itself, which worked but left it invisible to `-m guest` -- so it would
#: have been the third suite in nobody's gate the moment the Makefile
#: stopped naming files by hand.
pytestmark = conftest.needs_guest


def _run(*args: str) -> str:
    out = subprocess.run([WINE, guest_exe("framecheck.exe"), *args],
                         env=wine_env(), capture_output=True, text=True)
    return out.stdout


def _limits() -> dict[str, int]:
    rows = {}
    for line in _run("--limits").splitlines():
        parts = line.split("\t")
        if len(parts) == 3 and parts[0] == "limit":
            rows[parts[1]] = int(parts[2])
    return rows


def _build_on_guest(value: str) -> bytes:
    """The payload the real rc_frame_build emits for one header `v=<value>`."""
    for line in _run("--emit", value).splitlines():
        parts = line.split("\t")
        if len(parts) == 3 and parts[0] == "emit":
            assert parts[1] != "FAIL", f"guest refused to build {value!r}"
            return bytes.fromhex(parts[2])
    raise AssertionError(f"no emit row for {value!r}; got:\n{_run('--emit', value)}")


def _parse_with_bridge(payload: bytes):
    """Feed guest-built bytes to the bridge's own reader."""
    r = FrameReader()
    frames = r.feed(struct.pack("<I", len(payload)) + payload)
    assert len(frames) == 1, f"bridge read {len(frames)} frames, wanted 1"
    return frames[0]


@pytest.mark.parametrize("value", [
    "C:\\WORK",                 # the ordinary case
    "C:\\100%WORK",             # a literal % that is NOT followed by hex
    "50%41",                    # a % followed by hex -- decodes to "50A" unescaped
    "%25",                      # already-encoded-looking input
    "one\ntwo",                 # would inject a header line
    "one\rtwo",
    "a%0Ab",                    # looks like an escape but is literal text
    "100%%",
    "",
])
def test_guest_built_headers_survive_the_bridge_parser(value):
    """What the guest builds is what the bridge reads, byte for byte.

    `50%41` is the case that matters and the one a round trip inside the guest
    alone would miss: unescaped, the bridge decodes it to `50A` and every
    layer downstream is consistent about the wrong string.
    """
    frame = _parse_with_bridge(_build_on_guest(value))
    assert frame.type == "TEST"
    assert frame.headers["v"] == value


def test_a_newline_in_a_value_cannot_forge_a_header():
    """The injection case, stated as itself rather than as a round trip.

    An unescaped LF ends the header line, so everything after it is parsed as
    a further header. `v=a\\nid=99` would hand the bridge an `id` the guest
    never sent, and `id` is what routes an ack.
    """
    frame = _parse_with_bridge(_build_on_guest("a\nid=99"))
    assert frame.headers["v"] == "a\nid=99"
    assert "id" not in frame.headers


def test_bridge_never_sends_more_headers_than_the_guest_can_hold():
    """The guest REFUSES a frame past RC_MAX_HEADERS rather than dropping the
    surplus silently, which makes the bridge's own maximum load-bearing.

    Counted from the Frame(...) literals in protocol.py rather than asserted
    as a number here, so adding a header to a frame is what moves this.
    """
    import ast
    import bridge.wire.protocol as P

    tree = ast.parse(open(P.__file__).read())
    worst = 0
    where = None
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call)
                and getattr(node.func, "id", "") == "Frame"
                and len(node.args) >= 2
                and isinstance(node.args[1], ast.Dict)):
            if len(node.args[1].keys) > worst:
                worst = len(node.args[1].keys)
                where = node.lineno

    cap = _limits()["RC_MAX_HEADERS"]
    assert worst <= cap, (
        f"protocol.py:{where} builds a frame with {worst} headers and the "
        f"guest refuses anything over {cap} -- that is a dropped LINK, not a "
        f"dropped header")


def test_the_guest_limits_are_what_the_header_says():
    """Read from the binary, not from a copy of the constants."""
    lim = _limits()
    assert lim["RC_MAX_FRAME"] == 64 * 1024
    assert lim["RC_MAX_HEADERS"] == 16
    assert lim["RC_MAX_KEY"] == 32
    assert lim["RC_MAX_VAL"] == 256


def test_framecheck_self_checks_all_pass():
    """The guest-internal half: build/parse agreement and the three limits."""
    out = _run()
    assert out.strip(), "framecheck produced no output"
    failures = [ln for ln in out.splitlines() if ln.startswith("FAIL")]
    assert not failures, "\n".join(failures)
