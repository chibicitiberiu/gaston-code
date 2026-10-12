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
from bridge.wire.compress import ENC_RAW  # noqa: E402
from bridge.wire.protocol import Frame, FrameReader  # noqa: E402


class _SockWriter:
    """Just enough of asyncio.StreamWriter for GuestLink to write to a socket.

    The probe is synchronous and GuestLink is not, which is why this exists at
    all -- and it is worth the twelve lines, because the alternative is the
    probe reimplementing send_html's budget, its clip marker and its deflate
    decision. Those are production policy; a second copy here would drift and
    the measurement would stop describing what the guest actually receives.
    """

    def __init__(self, sock):
        self._sock = sock

    def is_closing(self) -> bool:
        return self._sock.fileno() < 0

    def write(self, data: bytes) -> None:
        self._sock.sendall(data)

    async def drain(self) -> None:
        return None


def replay_fence(send, conn, a) -> int:
    """Stream a long fenced block the way a real answer arrives.

    The point is the LIVE path, which is where stage 3's cost lands: the
    bridge re-sends the WHOLE open block on every flush (capped at
    HtmlStream.LIVE_CAP), so the guest re-renders and re-writes `#l` ten times
    a second for the length of an answer. Moving rendering onto a 1998 CPU
    multiplies whatever that already costs, and `rc_html::live_report` logs
    the before and after -- but only inside a TURN, so this sends TURN start
    and end around the stream.

    Everything goes through the REAL HtmlStream and the REAL GuestLink:
    `_tail`'s elision, the fence's cross-feed state, the deflate decision and
    send_html's budget. A hand-built corpus pushed through bare text_frame
    measures a different shape than production -- and gets it wrong in a way
    that shows up immediately, because the sealing APPEND of a 60KB fence is
    65,821 bytes on the wire and the cap is 65,536. send_html truncates it
    with the clip marker; a probe that did not would just crash, which is how
    this was found.

    The cadence is the coalescer's (bridge/services/presentation.py), and the
    measurement is per update.
    """
    import asyncio

    from bridge.wire.link import GuestLink

    kb = a.replay_fence
    # Shaped like real model output, not filler: `_fold_long_lines` and the
    # fence grammar both care about line length, and a single 60KB line would
    # exercise neither.
    lines = []
    i = 0
    while sum(len(x) + 1 for x in lines) < kb * 1024:
        i += 1
        lines.append(f"    buf[{i}] = decode(src + {i * 7}, &used);"
                     f"  /* step {i} */")
    body = "```c\n" + "\n".join(lines) + "\n```\n"

    # One live update per line, which is the WORST realistic count for an
    # answer this long rather than the typical one -- a model emitting faster
    # than ten lines a second has several lines coalesced into one flush. The
    # per-update payload is what makes this faithful: _tail caps it at
    # LIVE_CAP source bytes, which comes out around 4.4KB of markup, and that
    # is the figure the renderer will be handed.
    print(f"replaying a {len(body)} byte fence in {len(lines)} lines "
          f"at {a.cadence:.2f}s; LIVE_CAP={H.HtmlStream.LIVE_CAP}, so about "
          f"{len(lines) * a.cadence:.0f}s")

    link = GuestLink()
    link.writer = _SockWriter(conn)

    async def drive() -> tuple[int, int]:
        n_live = n_append = 0
        stream = H.HtmlStream()
        # Fed a line at a time, which is what the paginator works in.
        for line in body.splitlines(keepends=True):
            for u in stream.feed(line):
                await link.send_html(None, u.html, u.mode, 1)
                if u.mode == H.LIVE:
                    n_live += 1
                else:
                    n_append += 1
                # The real cadence. Without it this is a burst test, which
                # measures the socket rather than the view.
                await asyncio.sleep(a.cadence)
        for u in stream.flush():
            await link.send_html(None, u.html, u.mode, 1)
            n_append += 1
        return n_live, n_append

    send(PR.turn_start())
    t0 = time.time()
    n_live, n_append = asyncio.run(drive())
    send(PR.turn_end(True, "", None, int((time.time() - t0) * 1000)))

    print(f"sent {n_live} live updates and {n_append} appends in "
          f"{time.time() - t0:.1f}s")
    print("\nNow read the guest's log for the `live:` line -- one per turn, "
          "with n, bytes, dom_us and render_us. render_us is 0 until the "
          "renderer lands (stage 3 C2), which is the before number.")

    conn.settimeout(a.hold)
    try:
        while conn.recv(65536):
            pass
    except (TimeoutError, OSError):
        pass
    return 0


def main() -> int:
    ap = probelib.arg_parser()
    ap.add_argument("--hold", type=float, default=600.0,
                    help="seconds to keep the link up so the window can be "
                         "looked at; the guest drops the bar and the "
                         "transcript if the socket closes")
    ap.add_argument("--replay-fence", type=int, default=0, metavar="KB",
                    help="instead of one block of each kind, stream a KB-sized "
                         "fenced code block the way a real answer arrives: "
                         "LIVE updates carrying the whole open block each "
                         "time, then the APPEND that seals it. This is the "
                         "corpus the live-cost measurement uses; 60 is the "
                         "size it was calibrated at")
    ap.add_argument("--cadence", type=float, default=0.1, metavar="S",
                    help="seconds between live updates (default 0.1, the "
                         "coalescer's). Lower it to iterate faster, but the "
                         "resulting numbers measure the socket and the "
                         "scheduler rather than the view, so do not record "
                         "them")
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

    if a.replay_fence:
        return replay_fence(send, conn, a)

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
        # No link case: there is no [text](url) run. `_INLINE` has three
        # groups -- code, bold, em -- and `inline("[t](http://x/)")` returns
        # its input. This read as a link test until 2026-10-12 and was
        # silently passing on the escaping instead.
        ("inline runs", H.inline("**bold**, _em_, `code`, nested "
                                 "**bold with `code` inside**, and "
                                 "<escaping> & \"quotes\" to check")),
    ]
    for name, markup in checks:
        send(PR.text_frame("append", ENC_RAW, 1, markup.encode("cp1252",
                                                              "replace")))
        print(f"  sent {name}")
        time.sleep(0.2)

    send(PR.text_frame("append", ENC_RAW, 1,
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
