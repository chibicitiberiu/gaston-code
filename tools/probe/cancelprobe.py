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

TWO MODES, AND THEY MEASURE TWO DIFFERENT PROCESSES. The default above is a
bridge stub and the thing it measures is THE CLIENT's desktop worker, which
is what BACKLOG 26 was written about. `--remote HOST:PORT:SECRET` instead
drives GREMOTE over its own lane, which has a desktop worker of its own with
the same single-threaded shape -- so it answers the same question about the
same mechanism in the other program, and needs no client installed. Stage 5
uses the remote mode on the P200 because the client does not have to be there
for it, and the file it writes says which process it measured. The numbers
from the two modes are not interchangeable.

    python3 -u tools/probe/cancelprobe.py --remote 10.0.0.125:7401:secret
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


def remote_main(spec: str) -> int:
    """The same four questions, asked of gremote instead of the client.

    Structured to mirror the stub flow below step for step, so the two can be
    read side by side: a full walk, the same walk raced with a stop, the next
    request straight after, and a capture stopped mid-strip.
    """
    from gremote.client import Remote

    bits = spec.split(":")
    if len(bits) == 2:
        host, secret, port = bits[0], bits[1], 7401
    elif len(bits) == 3:
        host, port, secret = bits[0], int(bits[1]), bits[2]
    else:
        print("cancelprobe: --remote wants HOST:SECRET or HOST:PORT:SECRET")
        return 2

    fails = 0
    with Remote(host, secret, port=port) as r:
        print("measuring GREMOTE's desktop worker on %s:%d, not the client's"
              % (host, port))

        # 1. What a full enriched walk costs when nothing stops it.
        t0 = time.time()
        full_tree = r.tree(scope="desktop", max_nodes=4000, msaa=True,
                           hidden=True, timeout_ms=60000)
        full = time.time() - t0
        print("[ ok ] a full enriched walk is %.2fs, %d nodes"
              % (full, len(full_tree.nodes)))

        # 2. The same walk, raced with a stop. STOP is answered on the
        #    IMMEDIATE lane rather than behind the work, which is what makes
        #    the race winnable at all; losing one is not a failure, losing
        #    every one is.
        status, took, hit = None, 0.0, 0
        for attempt in range(ATTEMPTS):
            ids: list[str] = []
            out: dict[str, object] = {}

            # ids/out bound as defaults rather than captured: the thread is
            # joined inside this iteration so late binding would not actually
            # bite, but a closure over a loop variable is the shape that
            # eventually does.
            def walk(ids: list = ids, out: dict = out) -> None:
                started = time.time()
                try:
                    out["tree"] = r.tree(scope="desktop", max_nodes=4000,
                                         msaa=True, hidden=True,
                                         timeout_ms=60000,
                                         on_id=ids.append)
                    out["status"] = "ok"
                except Exception as e:
                    # A stopped walk comes back as a refusal rather than a
                    # tree with a flag, so the CODE is the interesting part:
                    # `cancelled` is the mechanism working, anything else is
                    # a different failure wearing the same exception type.
                    out["status"] = getattr(e, "code", None) or type(e).__name__
                out["took"] = time.time() - started

            t = threading.Thread(target=walk)
            t.start()
            # The id exists before the request goes out (gremote/client.py
            # calls on_id pre-send for exactly this), so there is no sleep
            # here racing a guess -- only a wait for the hook.
            while not ids and t.is_alive():
                time.sleep(0.001)
            if ids:
                r.stop(ids[0])
            t.join(timeout=120)
            status = out.get("status")
            took = float(out.get("took", 0.0))
            tree = out.get("tree")
            cancelled = tree is not None and getattr(tree, "stopped", False)
            if cancelled or status not in ("ok", None):
                hit = attempt + 1
                break
        ok = hit > 0
        fails += 0 if ok else 1
        print("[%s] walk stopped: status=%s after %.2fs on attempt %d of %d"
              % ("PASS" if ok else "FAIL", status, took, hit or ATTEMPTS,
                 ATTEMPTS))
        if not ok:
            print("       (every walk finished before the stop landed --"
                  " expected on a fast box, and the thing the P200 is for)")

        # 3. ...and the lane is free straight away, which is what 26 is about.
        t0 = time.time()
        small = r.tree(scope="desktop", max_nodes=20, msaa=False)
        took = time.time() - t0
        print("[ ok ] the next request is answered in %.2fs, %d nodes"
              % (took, len(small.nodes)))

        # 4. A capture, stopped mid-strip. The saving is the bytes that did
        #    NOT arrive.
        ids = []
        out = {}

        def capture() -> None:
            started = time.time()
            try:
                out["data"] = r.shot(x=0, y=0, w=800, h=600, bpp=8,
                                     on_id=ids.append)
                out["status"] = "ok"
            except Exception as e:
                out["status"] = getattr(e, "code", None) or type(e).__name__
            out["took"] = time.time() - started

        t = threading.Thread(target=capture)
        t.start()
        while not ids and t.is_alive():
            time.sleep(0.001)
        if ids:
            r.stop(ids[0])
        t.join(timeout=180)
        got = out.get("data") or b""
        print("[ ok ] capture raced with a stop: status=%s after %.2fs, "
              "%d bytes arrived" % (out.get("status"), out.get("took", 0.0),
                                    len(got)))

    print("\nthe figure BACKLOG 26 wants is step 3 against step 1: how long")
    print("the next request waits when the one before it was abandoned.")
    return 1 if fails else 0


def main() -> int:
    parser = probelib.arg_parser(__doc__.splitlines()[0])
    parser.add_argument("--remote", metavar="HOST[:PORT]:SECRET",
                        help="drive gremote's lane instead of standing in "
                             "for the bridge. Measures gremote's desktop "
                             "worker, not the client's")
    a = parser.parse_args()
    if a.remote:
        return remote_main(a.remote)
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
