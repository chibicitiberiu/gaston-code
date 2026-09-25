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

import conftest
import pytest

from bridge.wire.protocol import FrameReader

#: This suite relied on `guest_exe`'s internal skip instead of marking
#: itself, which worked but left it invisible to `-m guest` -- so it would
#: have been the third suite in nobody's gate the moment the Makefile
#: stopped naming files by hand.
pytestmark = conftest.needs_guest


def _run(*args: str) -> str:
    # This had no timeout, so a framecheck that hung took the suite with it
    # rather than failing it. run_guest supplies one.
    out = conftest.run_guest("framecheck.exe", *args, text=True)
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
    "C:\\caf\xe9",              # non-ASCII Latin-1 path
    "caf\xe9 na\xefve \xbd \xa35",
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

    Audit 04.9: the previous guard checked only literal ast.Dict arguments to Frame(...),
    which missed the dynamic frames most likely to grow (READY has 10 headers,
    TURN has 6, TEXT has 3, EXEC has 4). We now analyze each function in protocol.py
    for all keys that can be added to the header dict, and verify all constructors
    dynamically as well.
    """
    import ast

    import bridge.wire.protocol as P

    cap = _limits()["RC_MAX_HEADERS"]

    # 1. AST analysis: for every function constructing a Frame, find all keys added
    # to the headers dict (initial literal keys + subscript assignments h["..."] = ...).
    tree = ast.parse(open(P.__file__).read())
    for fn in [n for n in tree.body if isinstance(n, ast.FunctionDef)]:
        # Track dict variables and their possible keys
        dict_keys: dict[str, set[str]] = {}
        for stmt in ast.walk(fn):
            if isinstance(stmt, ast.Assign):
                if isinstance(stmt.value, ast.Dict) and stmt.targets:
                    target = stmt.targets[0]
                    if isinstance(target, ast.Name):
                        keys = {k.value for k in stmt.value.keys if isinstance(k, ast.Constant)}
                        dict_keys[target.id] = keys
            elif isinstance(stmt, ast.Assign) and isinstance(stmt.targets[0], ast.Subscript):
                sub = stmt.targets[0]
                if isinstance(sub.value, ast.Name) and isinstance(sub.slice, ast.Constant):
                    dict_keys.setdefault(sub.value.id, set()).add(sub.slice.value)

        # Check any Frame(type, headers, ...) call in this function
        for node in ast.walk(fn):
            if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "Frame" and len(node.args) >= 2:
                arg = node.args[1]
                if isinstance(arg, ast.Dict):
                    keys = {k.value for k in arg.keys if isinstance(k, ast.Constant)}
                elif isinstance(arg, ast.Name) and arg.id in dict_keys:
                    keys = dict_keys[arg.id]
                else:
                    continue
                assert len(keys) <= cap, (
                    f"protocol.py:{fn.name} can build a frame with {len(keys)} headers "
                    f"({sorted(keys)}), exceeding guest cap {cap}"
                )

    # 2. Dynamic check: invoke constructors with all optional fields populated
    test_frames = [
        P.hello("sec", sess="s1"),
        P.ready("s1", "claude", 1, 5, conv="c1"),
        P.turn_end(ok=True, subtype="done", cost_usd=0.05, ms=1200, ctx_pct=42),
        P.exec_cmd("e1", "cmd", cwd="C:\\", timeout_ms=5000, shell="cmd.exe"),
        P.option("m", "model1", "Model 1", current=True, disabled=True, hint=True, desc="desc"),
        P.text_frame("append", "raw", 1, b"hello", total=5, off=0),
        P.export_end(ok=True, bytes_=123, reason="done"),
    ]
    for frame in test_frames:
        assert len(frame.headers) <= cap, (
            f"Frame {frame.type} generated {len(frame.headers)} headers, exceeding cap {cap}"
        )


def test_the_guest_limits_are_what_the_header_says():
    """Read from the binary, not from a copy of the constants."""
    import bridge.wire.protocol as P
    lim = _limits()
    assert lim["RC_MAX_FRAME"] == 64 * 1024
    assert lim["RC_MAX_HEADERS"] == 16
    assert lim["RC_MAX_KEY"] == 32
    assert lim["RC_MAX_VAL"] == 256
    assert lim["RC_MAX_TYPE"] == 32
    assert P.MAX_FRAME == lim["RC_MAX_FRAME"]
    assert P.MAX_HEADERS == lim["RC_MAX_HEADERS"]
    assert P.MAX_HEADER_KEY == lim["RC_MAX_KEY"] - 1
    assert P.MAX_HEADER_VALUE == lim["RC_MAX_VAL"] - 1
    assert P.MAX_TYPE == lim["RC_MAX_TYPE"] - 1


def test_framecheck_self_checks_all_pass():
    """The guest-internal half: build/parse agreement and the three limits."""
    out = _run()
    assert out.strip(), "framecheck produced no output"
    failures = [ln for ln in out.splitlines() if ln.startswith("FAIL")]
    assert not failures, "\n".join(failures)
