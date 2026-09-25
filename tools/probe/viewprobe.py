#!/usr/bin/env python3
"""Drive the real guest's TRANSCRIPT on the real machine, with no agent.

WHY THIS EXISTS. tools/probe/syncprobe.py proves the sync layer against the shipping
binary, and it is the only thing that does. It never sends DOC, so the HTML
view has never been exercised on the Windows 98 box by anything but a live
bridge with an API key -- which means the view is the one layer whose
verification depended on a paid round trip and an agent being in a good mood.

That mattered on 2026-09-18, when htmlview stopped being a handle plus
nineteen free functions and became a class: a pimpl, ComPtr/Bstr guards over
every COM reference, and a vtable built by a C++ front end that will not take
the (void *) casts C accepted. All of that either works against MSHTML or does
not, and Wine cannot answer -- it does not host MSHTML the way IE5 on 98 does.

So this is a bridge stub that speaks exactly enough to make the transcript
render: HELLO, READY, DOC with the REAL stylesheet from bridge/view/html.py,
then one block of each kind the view knows how to say. It holds the connection
open afterwards so the window can be screenshotted.

    python3 -u tools/probe/viewprobe.py            # then launch the client
    python3 -u tools/probe/viewprobe.py --port 7890

Start it INSTEAD OF the bridge. It sends no EXEC and no SYNC frame, so it
cannot touch the guest's disk.
"""
from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import probelib
import toolkit

toolkit.on_path()

from bridge.view import html as H  # noqa: E402
from bridge.wire import protocol as PR  # noqa: E402
from bridge.wire.protocol import Frame, FrameReader  # noqa: E402


def main() -> int:
    ap = probelib.arg_parser()
    ap.add_argument("--hold", type=float, default=600.0,
                    help="seconds to keep the link up so the window can be "
                         "looked at; the guest drops the bar and the "
                         "transcript if the socket closes")
    a = ap.parse_args()

    conn, _peer = probelib.listen_for_guest(a.host, a.port)

    r = FrameReader()
    pending: list[Frame] = []

    def recv(want: str, timeout: float = 30.0) -> Frame:
        conn.settimeout(timeout)
        while True:
            for i, f in enumerate(pending):
                if f.type == want:
                    return pending.pop(i)
            data = conn.recv(65536)
            if not data:
                raise RuntimeError(f"guest closed while waiting for {want}")
            pending.extend(r.feed(data))

    def send(f: Frame) -> None:
        conn.sendall(f.encode())

    hello = recv("HELLO")
    if hello.headers.get("secret") != a.secret:
        print("bad secret")
        return 2
    print(f"guest says mem_kb={hello.headers.get('mem_kb')} "
          f"acp={hello.headers.get('acp')} ver={hello.headers.get('ver')}")

    # READY then DOC, in that order, same as the bridge. The view buffers
    # anything appended before the document arrives and flushes on poll(), so
    # getting this order wrong would still LOOK right -- which is exactly the
    # kind of thing that should be exercised rather than reasoned about.
    send(PR.ready("viewprobe", "none", 1, 1, 1))
    send(PR.doc(H.document()))
    send(Frame("OPTIONSEND", {}))

    # One of each thing the view can be told. The first three are PLAIN TEXT
    # and the view escapes them itself; the rest are finished markup from
    # bridge/view/html.py, which is the only path allowed to carry markup
    # because the guest transforms nothing.
    checks = [
        ("notice", H.notice("viewprobe: this is a notice")),
        ("error", H.error("viewprobe: this is an error <&> \" escaped")),
        ("user turn", H.user_turn("a user turn with <angle> & ampersand")),
        ("tool call", H.tool_call("exec", "dir C:\\WORK")),
        ("tool result", H.tool_result("VOLUME IN DRIVE C IS WIN98\n"
                                      " 3 FILE(S)      1,234 BYTES")),
        ("tool error", H.tool_result("Access denied.", error=True)),
        ("markdown", H.inline("**bold**, `code`, and a [link](http://x/) "
                              "with <escaping> to check")),
    ]
    for name, markup in checks:
        send(PR.text_frame("append", "plain", 1, markup.encode("cp1252",
                                                               "replace")))
        print(f"  sent {name}")
        time.sleep(0.2)

    send(PR.text_frame("append", "plain", 1,
                       H.notice("if you can read every line above, the "
                                "MSHTML host and the view class both work"
                                ).encode("cp1252", "replace")))

    print(f"\nall frames sent. holding the link for {a.hold:.0f}s -- "
          f"look at the guest window now.")
    try:
        conn.settimeout(a.hold)
        while True:
            data = conn.recv(65536)
            if not data:
                print("guest closed the link")
                break
            for f in r.feed(data):
                print(f"  guest -> {f.type} {f.headers}")
    except TimeoutError:
        print("hold expired")
    except KeyboardInterrupt:
        pass
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
