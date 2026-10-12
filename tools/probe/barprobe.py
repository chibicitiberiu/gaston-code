#!/usr/bin/env python3
"""Drive the permission and halt bars on the real machine, step by step.

WHY. The bar is what the project's first standing rule is about -- the person
at the keyboard never loses a byte, and is never asked twice -- and it was two
modes arbitrated pairwise across five `if`s in four functions, with
`g_halt_id[0]` doing double duty as "the halt on screen" and "the halt waiting
behind the permission bar". That arrangement cost four data-loss bugs. It is a
bounded state STACK now (`win/app/bar.cpp`), where the shown entry is DERIVED
from the stack rather than stored beside it, so the two cannot disagree.

Neither `viewprobe.py` nor `drainprobe.py` sends a single PERM or HALT frame,
so none of that has ever been exercised on the machine by anything but a live
bridge with an API key and a cooperating agent. This does it deliberately, one
step at a time, pausing so each state can be screenshotted.

    python3 -u tools/probe/barprobe.py --port 7890

Start it INSTEAD OF the bridge, then launch the client. It sends no EXEC and
no SYNC frame, so it cannot touch the guest's disk.

The interesting step is 3. A HALT arriving while a PERMREQ is up must NOT
replace the permission bar, and when that permission request ends the halt
must appear on its own -- previously the "remembered behind" case needed an
explicit branch in the HALTEND handler, and getting it wrong lost the halt.
"""
from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import probelib
import toolkit

toolkit.on_path()

from bridge.view import blocks as B  # noqa: E402
from bridge.view import html as H  # noqa: E402
from bridge.wire import protocol as PR  # noqa: E402
from bridge.wire.compress import ENC_RAW  # noqa: E402
from bridge.wire.protocol import Frame, FrameReader  # noqa: E402

STEPS = [
    ("perm-with-always",
     "PERMREQ allows=1: Allow, Always and Deny should all be visible",
     [lambda: PR.permreq("p1", "exec", True, 0, "Run: dir C:\\WORK")]),

    ("perm-without-always",
     "PERMREQ allows=0: Always must be HIDDEN, and the label gets its width",
     [lambda: PR.permend("p1", "answered"),
      lambda: PR.permreq("p2", "exec", False, 0, "Write C:\\WORK\\notes.md -- "
                                                 "a deliberately long summary "
                                                 "to see how much room the "
                                                 "label actually gets")]),

    ("permwait-count",
     "PERMWAIT: the queue depth appears beside the prompt, focus must NOT move",
     [lambda: PR.permwait("p2", 3)]),

    ("halt-behind-perm",
     "HALT arrives while the PERM bar is up: PERM must STAY, halt waits behind",
     [lambda: PR.halt("h1", "A conflicting edit was found in notes.md.")]),

    ("halt-surfaces",
     "PERMEND for the permission: the HALT must now appear on its own",
     [lambda: PR.permend("p2", "answered")]),

    ("duplicate-id-refused",
     "A second HALT with a DIFFERENT id: refused and logged, first stays",
     [lambda: PR.halt("h2", "A second halt that should be refused.")]),

    ("halt-cleared",
     "HALTEND: the bar comes down and Send re-enables",
     [lambda: PR.haltend("h1")]),
]


def main() -> int:
    ap = probelib.arg_parser()
    ap.add_argument("--step-seconds", type=float, default=6.0,
                    help="pause between steps, so each state can be captured")
    a = ap.parse_args()

    conn, _ = probelib.listen_for_guest(a.host, a.port)
    r = FrameReader()
    pend: list[Frame] = []

    conn.settimeout(30)
    hello = None
    while hello is None:
        for f in r.feed(conn.recv(65536)):
            (pend.append(f) if f.type != "HELLO" else None)
            if f.type == "HELLO":
                hello = f
    if hello.headers.get("secret") != a.secret:
        print("bad secret")
        return 2
    print(f"guest connected, mem_kb={hello.headers.get('mem_kb')}")

    # No DOC: stage 3 C4 deleted the frame (see viewprobe).
    conn.sendall(PR.ready("barprobe", "none").encode())
    conn.sendall(Frame("OPTIONSEND", {}).encode())
    # RECORDS, not markup. The probe sent rendered HTML through the bridge's
    # emitter until stage 3b deleted it; the client renders now, which is
    # what the probe is there to look at.
    conn.sendall(PR.text_frame(
        "append", ENC_RAW,
        B.encode(H.notice("barprobe: driving the bar states"))).encode())
    time.sleep(1.5)

    for i, (name, what, frames) in enumerate(STEPS, 1):
        for make in frames:
            conn.sendall(make().encode())
        print(f"[step {i}] {name}\n          {what}")
        deadline = time.time() + a.step_seconds
        conn.settimeout(0.5)
        while time.time() < deadline:
            try:
                data = conn.recv(65536)
                if not data:
                    print("          guest closed the link")
                    return 1
                for f in r.feed(data):
                    print(f"          guest -> {f.type} {f.headers}")
            except TimeoutError:
                pass

    print("\nall steps sent. The guest's own log is the other half of this:\n"
          "  step 6 should have logged a refusal naming the outstanding id.")
    conn.settimeout(60)
    try:
        while True:
            data = conn.recv(65536)
            if not data:
                break
            for f in r.feed(data):
                print(f"  guest -> {f.type} {f.headers}")
    except (TimeoutError, KeyboardInterrupt):
        pass
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
