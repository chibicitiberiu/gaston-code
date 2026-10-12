"""Do Windows 98's accessibility proxies answer for the controls `item`
targets? Lists one control's items (UITREE acc=1) for each kind -- the desktop
icons, an Explorer file list, its folder tree, its toolbars -- and moves the
pointer to one item by name through `input`.

    python3 -u tools/probe/itemsprobe.py --host 10.0.0.45 --port 7893 \\
        --remote 10.0.0.125:<secret>

Start it INSTEAD OF the bridge, then launch the client. It opens one
Explorer window on C:\\GTEST -- not C:\\WINDOWS, which 98 shows as a
"Show Files" warning page with no list at all -- (through gremote) and closes it at the end.
"""
from __future__ import annotations

import contextlib
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import probelib
import toolkit

toolkit.on_path()

from bridge.agent import steps as S  # noqa: E402
from bridge.wire import protocol as PR  # noqa: E402
from bridge.wire import uirec  # noqa: E402
from bridge.wire.protocol import Frame, FrameReader  # noqa: E402

CONTROLS = [
    ("desktop icons", "Progman SysListView32"),
    ("explorer file list", "ExploreWClass SysListView32"),
    ("explorer folder tree", "ExploreWClass SysTreeView32"),
    ("explorer toolbar", "ExploreWClass ToolbarWindow32:nth(1)"),
]


def main() -> int:
    ap = probelib.arg_parser()
    ap.add_argument("--remote", required=True)
    a = ap.parse_args()
    from gremote.client import Remote
    host, secret = a.remote.rsplit(":", 1)
    r = Remote(host, secret=secret)
    r.connect()
    explorer = None          # opened once the old windows are closed

    conn, _ = probelib.listen_for_guest(a.host, a.port)
    reader = FrameReader()
    conn.settimeout(60)
    hello = None
    while hello is None:
        for f in reader.feed(conn.recv(65536)):
            if f.type == "HELLO":
                hello = f
    conn.sendall(PR.ready("itemsprobe", "none").encode())
    # Close what an earlier run left, so "the" Explorer window is one.
    for _ in range(3):
        c = S.compile_steps([{"close": "ExploreWClass:nth(1)"}])
        conn.sendall(PR.ui_run("u69" + str(_), c.body, timeout_ms=c.timeout_ms,
                               step_ms=S.STEP_TIMEOUT_DEFAULT).encode())
        time.sleep(2)
    r.launch("EXPLORER.EXE /e,C:\\GTEST")
    time.sleep(6)            # Explorer filling its list on a slow machine
    seq = [700]

    def tree(sel):
        seq[0] += 1
        uid = f"u{seq[0]}"
        conn.sendall(PR.ui_tree(uid, acc=True, target=sel,
                                max_nodes=400, timeout_ms=20000).encode())
        recs = bytearray()
        while True:
            for f in reader.feed(conn.recv(65536)):
                if f.type == "PING":
                    conn.sendall(Frame("PONG", {}).encode())
                if f.headers.get("id") != uid:
                    continue
                if f.type == "UIENT":
                    recs += f.body
                if f.type == "UIEND":
                    return f.headers, uirec.parse_records(bytes(recs),
                                                          "cp1252")

    results = []
    first_item = ""
    for what, sel in CONTROLS:
        h, nodes = tree(sel)
        names = [n.name for n in nodes if n.name]
        ok = h.get("status") == "ok" and len(names) > 0
        results.append(ok)
        if what == "explorer file list" and names:
            first_item = names[0]
        print(f"[{'PASS' if ok else 'FAIL'}] {what}: status={h.get('status')}"
              f" items={len(nodes)} named={len(names)} "
              f"walk={h.get('walk')} first={names[:4]!r}", flush=True)

    if first_item:
        c = S.compile_steps([{"move": "ExploreWClass SysListView32",
                              "item": first_item}])
        seq[0] += 1
        uid = f"u{seq[0]}"
        conn.sendall(PR.ui_run(uid, c.body, timeout_ms=c.timeout_ms,
                               step_ms=S.STEP_TIMEOUT_DEFAULT).encode())
        end = None
        while end is None:
            for f in reader.feed(conn.recv(65536)):
                if f.type == "UIEND" and f.headers.get("id") == uid:
                    end = f
        ok = end.headers.get("status") == "ok"
        results.append(ok)
        print(f"[{'PASS' if ok else 'FAIL'}] move to item {first_item!r}: "
              f"{end.headers.get('status')} {end.body.decode('cp1252')!r}")

    # Explorer hands the window to the shell's own process, so the launched
    # pid is gone already; the window is closed the polite way instead.
    # Every Explorer window this left, one `close` at a time until none is
    # left (an earlier run may have left one too).
    for _ in range(4):
        c = S.compile_steps([{"close": "ExploreWClass:nth(1)"}])
        seq[0] += 1
        uid = f"u{seq[0]}"
        conn.sendall(PR.ui_run(uid, c.body, timeout_ms=c.timeout_ms,
                               step_ms=S.STEP_TIMEOUT_DEFAULT).encode())
        status = ""
        with contextlib.suppress(Exception):
            while not status:
                for f in reader.feed(conn.recv(65536)):
                    if f.type == "UIEND" and f.headers.get("id") == uid:
                        status = f.headers.get("status", "?")
        if status != "ok":
            break
    del explorer
    r.close()
    conn.close()
    print(f"\n{sum(results)}/{len(results)} as expected")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
