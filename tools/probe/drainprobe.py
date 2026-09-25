#!/usr/bin/env python3
"""Queue sync work on the guest, then watch what shutdown does with it.

WHY. rpc.cpp's shutdown drain says anything still queued when the app closes
is "answered, not silently dropped -- the bridge is holding a future for it".
That was false for the whole of Phase 3: rc_net_stop cleared n->running before
calling rc_rpc_stop(), and rc_net_send bails on !n->running, so every ack the
drain composed returned -1 and nothing reached the wire (finding 01.51).

The fix reorders rc_net_stop into three phases so the drain can still send.
Reordering shutdown on Win9x is exactly where this project has twice produced
a GPF dialog at exit, so it has to be watched on the real machine rather than
reasoned about.

    python3 -u tools/probe/drainprobe.py --port 7890 --burst 24

Start it INSTEAD OF the bridge, launch the client, then close the client while
this is running. Every frame is printed with the milliseconds since the burst
was sent, so an ack that arrives AFTER the close is visibly the drain's.
"""
from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import probelib
import toolkit

toolkit.on_path()

from bridge.files import paths as P  # noqa: E402
from bridge.wire.protocol import Frame, FrameReader  # noqa: E402


def main() -> int:
    ap = probelib.arg_parser()
    ap.add_argument("--burst", type=int, default=24,
                    help="how many SYNCGETs to queue; the guest's sync queue "
                         "is RPC_SYNCQ_MAX = 24 slots")
    ap.add_argument("--path", default="big.bin",
                    help="a file in the sync root big enough that answering "
                         "it takes real work (8KB chunks)")
    ap.add_argument("--work", default="",
                    help="the folder to arm as the sync root; default is "
                         "the sync_root the guest reports in HELLO")
    a = ap.parse_args()

    conn, _peer = probelib.listen_for_guest(a.host, a.port)

    r = FrameReader()
    pend: list[Frame] = []

    def recv_one(timeout: float):
        conn.settimeout(timeout)
        if pend:
            return pend.pop(0)
        data = conn.recv(65536)
        if not data:
            return None
        pend.extend(r.feed(data))
        return pend.pop(0) if pend else Frame("(none)", {})

    conn.settimeout(30)
    hello = None
    while hello is None:
        for f in r.feed(conn.recv(65536)):
            if f.type == "HELLO":
                hello = f
            else:
                pend.append(f)
    if hello.headers.get("secret") != a.secret:
        print("bad secret")
        return 2
    print(f"guest says mem_kb={hello.headers.get('mem_kb')} "
          f"root={hello.headers.get('sync_root')}")
    # The work directory arms the guest's sync root. Without it every
    # SYNCGET is answered at once with no_root, nothing ever queues, and the
    # drain has nothing to answer -- a pass that tests nothing. The guest
    # takes it from an OPTION batch since per-conversation work folders, so
    # say it the way the bridge does (bridge/services/options.py).
    work = a.work or hello.headers.get("sync_root") or ""
    if not work:
        print("guest reported no sync_root and --work was not given")
        return 2
    conn.sendall(Frame("OPTION", {"kind": "work", "id": work,
                                  "label": work, "current": "1"}).encode())
    conn.sendall(Frame("OPTIONSEND", {}).encode())

    # SYNCGET's body is the bare encoded path. Only SYNCPUT length-prefixes
    # it, because a put carries file bytes after the name and a get does not.
    body = P.wire_encode(a.path)
    t0 = time.time()
    for i in range(a.burst):
        conn.sendall(Frame("SYNCGET", {"id": f"s{i}"}, body).encode())
    print(f"queued {a.burst} SYNCGET for {a.path!r}\n"
          f"NOW CLOSE THE CLIENT. Anything below that arrives after the close "
          f"is the shutdown drain answering.\n")

    seen: dict[str, int] = {}
    try:
        while True:
            f = recv_one(120)
            if f is None:
                print(f"\n[{(time.time()-t0)*1000:8.0f}ms] guest closed the link")
                break
            if f.type == "(none)":
                continue
            seen[f.type] = seen.get(f.type, 0) + 1
            st = f.headers.get("status", "")
            print(f"[{(time.time()-t0)*1000:8.0f}ms] {f.type:<14} "
                  f"id={f.headers.get('id',''):<4} "
                  f"{('status=' + st) if st else ''}")
    except TimeoutError:
        print("timed out waiting")
    except KeyboardInterrupt:
        pass
    finally:
        conn.close()

    print("\ntotals:", ", ".join(f"{k}={v}" for k, v in sorted(seen.items())))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
