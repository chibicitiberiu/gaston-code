"""gremote's handshake against the frame grammar's own ceiling.

The client has `test_hello_headers.py` for this and gremote had nothing. It
matters more now than it did: the ui lane filled three of the six slots that
were free, and the failure mode is not a compile error -- `rc_frame_build`
refuses a frame with too many headers at RUNTIME, so a machine that has just
been updated would stop answering its handshake and say nothing about why.

Read out of the source rather than from a live guest, because the point is
to fail in `make test-fast` on the commit that adds the twelfth header, not
on the first connection after it ships.
"""
from __future__ import annotations

import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import conftest

REPO = pathlib.Path(conftest.REPO)
SERVER = REPO / "win" / "remote" / "server.cpp"
FRAME_H = REPO / "win" / "wire" / "frame.h"


def _hello() -> tuple[list[str], int, int]:
    """The header names, the array size, and the count actually sent."""
    text = SERVER.read_text()
    body = text[text.index("void Session::sendHello"):]
    body = body[:body.index("\n}")]
    names = re.findall(r'keys\[\d+\]\s*=\s*"([a-z_]+)"', body)
    size = int(re.search(r"const char \*keys\[(\d+)\]", body).group(1))
    sent = int(re.search(r'send\("HELLO",\s*keys,\s*vals,\s*(\d+)', body)
               .group(1))
    return names, size, sent


def test_the_handshake_fits_the_frame_grammar():
    _names, _size, sent = _hello()
    cap = int(re.search(r"#define RC_MAX_HEADERS\s+(\d+)",
                        FRAME_H.read_text()).group(1))
    assert sent <= cap, (
        f"HELLO sends {sent} headers and a frame may carry {cap}; "
        "rc_frame_build refuses the frame at runtime, so this would be a "
        "machine that stops answering its handshake and does not say why")


def test_the_array_the_count_and_the_names_agree():
    """Three literals that have to move together, and nothing but this makes
    them. An array of 13 filled with 12 sends a header whose value is
    whatever was on the stack."""
    names, size, sent = _hello()
    assert len(names) == sent == size, (names, size, sent)


def test_the_capabilities_a_caller_branches_on_are_declared():
    """Each is something the Python reads and changes behaviour on: `keys`
    gates a step list's key names, `automation` says whether the screen may
    be driven, `deflate` is negotiated per connection, and `machine` is how
    a measurement names the box it came from."""
    names, _, _ = _hello()
    for key in ("ver", "acp", "oemcp", "msaa", "deflate", "automation",
                "keys", "machine"):
        assert key in names, f"{key} is not in HELLO: {names}"
