"""Does UICANCEL actually stop a walk and a capture, and what did it save?

BACKLOG 26 asked for a number before a cancel path was built: the desktop
worker is single-threaded, so an enumeration nobody wants is work the NEXT
request queues behind. The ui lane's STOP needed the flag anyway (REMOTE.md
step 6), so this measures what reaching the walk and the capture with it is
worth -- and it is what found the window a cancel used to fall through, with
the request still queued and nothing yet knowing its id.

    python3 -u tools/probe/cancelprobe.py --host 127.0.0.1 --port 7890

Start it INSTEAD OF the bridge, then launch the client. It drives the desktop
it finds and opens nothing, so it is safe on a machine somebody is using --
but it does take a full-screen screenshot.
"""
from __future__ import annotations

import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import probelib
import toolkit

toolkit.on_path()

from bridge.wire import protocol as PR  # noqa: E402
from bridge.wire.protocol import FrameReader  # noqa: E402

#: How many times to race the cancel against the walk before calling it. The
#: walk is a fraction of a second on an emulator, so whether a cancel lands
#: mid-walk is a race by nature; losing one is not a failure, losing every
#: one is.
ATTEMPTS = 8


def main() -> int:
    a = probelib.arg_parser(__doc__.splitlines()[0]).parse_args()
    conn, _ = probelib.listen_for_guest(a.host, a.port)
    reader = FrameReader()
    stream = conn.makefile("rwb")
    lock = threading.Lock()
    fails = 0

    def send(frame) -> None:
        with lock:
            stream.write(frame.encode())
            stream.flush()

    def collect(uid: str, budget: float = 90.0):
        """Everything for one id, and how long it took."""
        data = bytearray()
        started = time.time()
        while time.time() - started < budget:
            chunk = stream.read1(65536)
            if not chunk:
                break
            for frame in reader.feed(chunk):
                if frame.headers.get("id") != uid:
                    continue
                if frame.type == "UIDAT":
                    data += frame.body
                elif frame.type == "UIEND":
                    return bytes(data), frame, time.time() - started
        return bytes(data), None, time.time() - started

    # The greeting, so the first collect() is not reading it.
    for _ in reader.feed(stream.read1(65536)):
        pass

    # 1. What a full enriched walk costs when nothing stops it.
    send(PR.ui_tree("k0", scope="desktop", max_nodes=4000, msaa=True,
                    hidden=True, timeout_ms=60000))
    _, end, full = collect("k0")
    print("[ ok ] a full enriched walk is %.2fs, status=%s nodes=%s"
          % (full, end and end.headers.get("status"),
             end and end.headers.get("nodes")))

    # 2. The same walk, cancelled. Raced rather than timed: UICANCEL is on
    #    the IMMEDIATE lane and is answered by the reader, so it can land
    #    while the desktop worker is still walking.
    status, took, hit = None, 0.0, 0
    for attempt in range(ATTEMPTS):
        uid = "k1_%d" % attempt
        send(PR.ui_tree(uid, scope="desktop", max_nodes=4000, msaa=True,
                        hidden=True, timeout_ms=60000))
        send(PR.ui_cancel(uid))
        _, end, took = collect(uid)
        status = end and end.headers.get("status")
        if status == "cancelled":
            hit = attempt + 1
            break
    ok = status == "cancelled"
    fails += 0 if ok else 1
    print("[%s] walk cancelled: status=%s after %.2fs on attempt %d"
          % ("PASS" if ok else "FAIL", status, took, hit or ATTEMPTS))

    # 3. ...and the lane is free straight away, which is what 26 is about.
    send(PR.ui_tree("k2", scope="desktop", max_nodes=20, msaa=False))
    _, end, took = collect("k2")
    ok = end is not None and end.headers.get("status") == "ok"
    fails += 0 if ok else 1
    print("[%s] the next request is answered in %.2fs, status=%s"
          % ("PASS" if ok else "FAIL", took, end and end.headers.get("status")))

    # 4. A capture, stopped at a strip boundary. The saving is the bytes
    #    that did NOT arrive.
    send(PR.ui_shot("k3", x=0, y=0, w=800, h=600, bpp=8))
    send(PR.ui_cancel("k3"))
    data, end, took = collect("k3")
    status = end and end.headers.get("status")
    ok = status in ("cancelled", "ok")
    fails += 0 if ok else 1
    print("[%s] capture cancelled: status=%s after %.2fs, %d bytes arrived"
          % ("PASS" if ok else "FAIL", status, took, len(data)))
    if status == "ok":
        print("       (it finished first; a full screen is quick here)")

    print("\n%d/3 as expected" % (3 - fails))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
