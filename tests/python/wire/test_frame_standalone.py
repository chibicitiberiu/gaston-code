"""bridge/wire/frame.py is libwire's codec on the Python side, and gremote's
client imports it without the bridge. So it must stay stdlib-only: importing
it may not pull in any other bridge module (the folding, the caps, the view).
"""
import subprocess
import sys

from bridge.wire import frame
from bridge.wire import protocol


def test_importing_the_codec_loads_no_other_bridge_module():
    code = (
        "import sys; import bridge.wire.frame; "
        "print(sorted(m for m in sys.modules if m.startswith('bridge')))"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True,
                         text=True, check=True).stdout.strip()
    assert out == "['bridge', 'bridge.wire', 'bridge.wire.frame']", out


def test_plain_round_trip():
    f = frame.Frame("PING", {"id": "7", "note": "50%\nmore"}, b"\x00\x01")
    got = frame.FrameReader(frame.Frame).feed(f.encode())
    assert got == [f]


def test_the_bridge_reader_decodes_into_its_own_frames():
    got = protocol.FrameReader().feed(protocol.Frame("PING").encode())
    assert type(got[0]) is protocol.Frame
