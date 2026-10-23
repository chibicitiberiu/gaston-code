"""Whole-tree copy, move and delete (win/fs/tree.h) on a guest, no model.

The platform questions Wine cannot answer: does SHFileOperation put a folder
in THIS machine's Recycle Bin, does a folder move across drives come out
whole, are the counts right. Works under C:\\GTEST\\TREE and, with `--other`,
under a distinctive name on another drive, and removes both.

    python3 -u research/probes/treeprobe.py --host 10.0.0.45 --port 7893 \\
        --remote 10.0.0.125:<secret> --other D:

Start it INSTEAD OF the bridge, then launch the client. `--other` should be a
WRITABLE second drive; without one the cross-drive cases are skipped.
"""
from __future__ import annotations

import contextlib
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))), "tools"))
import probelib
import toolkit

toolkit.on_path()

from bridge.wire import protocol as PR  # noqa: E402
from bridge.wire.protocol import Frame, FrameReader  # noqa: E402

BASE = "C:\\GTEST\\TREE"
SRC = BASE + "\\SRC"
FILES = {"a.txt": b"alpha\r\n", "sub\\b.txt": b"bravo bravo\r\n",
         "sub\\deep\\c.bin": bytes(range(256)) * 40}


def main() -> int:
    ap = probelib.arg_parser()
    ap.add_argument("--remote", required=True)
    ap.add_argument("--other", default="", help="a writable second drive")
    a = ap.parse_args()

    from gremote.client import Remote
    host, secret = a.remote.rsplit(":", 1)
    r = Remote(host, secret=secret)
    r.connect()
    with contextlib.suppress(Exception):
        r.rmtree(BASE)
    for d in (BASE, SRC, SRC + "\\sub", SRC + "\\sub\\deep"):
        with contextlib.suppress(Exception):
            r.mkdir(d)
    for rel, data in FILES.items():
        r.put(SRC + "\\" + rel, data)
    total = sum(len(v) for v in FILES.values())
    other = (a.other.rstrip("\\") + "\\GTREEPRB.TMP") if a.other else ""

    conn, _ = probelib.listen_for_guest(a.host, a.port)
    reader = FrameReader()
    conn.settimeout(120)
    hello = None
    while hello is None:
        for f in reader.feed(conn.recv(65536)):
            if f.type == "HELLO":
                hello = f
    if hello.headers.get("ops") != "2":
        print(f"this client does not declare ops=2 ({hello.headers.get('ops')})")
        return 2
    conn.sendall(PR.ready("treeprobe", "none").encode())
    time.sleep(2)
    seq = [800]

    def ask(op, path, to="", **kw) -> dict:
        seq[0] += 1
        fr = PR.fs_op(f"f{seq[0]}", op, path, to, **kw)
        conn.sendall(fr.encode())
        while True:
            for f in reader.feed(conn.recv(65536)):
                if f.type == "PING":
                    conn.sendall(Frame("PONG", {}).encode())
                if f.headers.get("id") == fr.headers["id"]:
                    h = dict(f.headers)
                    h["_body"] = f.body.decode("cp1252", "replace")
                    return h

    def present(p):
        try:
            r.stat(p)
            return True
        except Exception:
            return False

    results = []

    def check(name, ok, info):
        results.append(ok)
        print(f"[{'PASS' if ok else 'FAIL'}] {name}: {info}", flush=True)

    h = ask("FSSTATS", SRC)
    check("stats", h["status"] == "ok" and h["files"] == "3"
          and h["dirs"] == "2" and h["bytes"] == str(total),
          f"{h['status']} files={h.get('files')} dirs={h.get('dirs')} "
          f"bytes={h.get('bytes')} first={h['_body'].split()!r}")

    h = ask("FSCOPY", SRC, BASE + "\\COPY")
    check("copy-tree", h["status"] == "ok" and h["files"] == "3"
          and r.get(BASE + "\\COPY\\sub\\deep\\c.bin").data
          == FILES["sub\\deep\\c.bin"], f"{h['status']} files={h['files']}")

    h = ask("FSCOPY", SRC + "\\a.txt", BASE + "\\COPY\\a.txt")
    check("copy-refuses-existing", h["status"] == "exists", h["status"])
    h = ask("FSCOPY", SRC + "\\a.txt", BASE + "\\COPY\\a.txt", overwrite=True)
    check("copy-overwrite", h["status"] == "ok", h["status"])

    h = ask("FSCOPY", SRC, SRC + "\\sub")
    check("copy-into-itself", h["status"] == "into_itself", h["status"])

    h = ask("FSCOPY", SRC, BASE + "\\COPY")
    check("copy-into-existing-folder-nests",
          h["status"] == "ok" and present(BASE + "\\COPY\\SRC\\a.txt"),
          h["status"])

    if other:
        with contextlib.suppress(Exception):
            r.rmtree(other)
        h = ask("FSMV", BASE + "\\COPY", other)
        ok = (h["status"] == "ok" and h.get("how") == "copied"
              and not present(BASE + "\\COPY")
              and r.get(other + "\\sub\\deep\\c.bin").data
              == FILES["sub\\deep\\c.bin"])
        check("move-folder-across-drives", ok,
              f"{h['status']} how={h.get('how')} files={h.get('files')}")
        h = ask("FSMV", other, BASE + "\\BACK")
        check("move-folder-back", h["status"] == "ok"
              and not present(other) and present(BASE + "\\BACK\\a.txt"),
              f"{h['status']} how={h.get('how')}")

    h = ask("FSRM", BASE + "\\BACK" if other else BASE + "\\COPY")
    target = BASE + "\\BACK" if other else BASE + "\\COPY"
    bin_has = False
    for rb in ("C:\\RECYCLED",):
        with contextlib.suppress(Exception):
            bin_has = any(e.name.upper().startswith("D")
                          for e in r.ls(rb))
    check("delete-to-recycle-bin", h["status"] == "ok"
          and h.get("how") == "recycled" and not present(target),
          f"{h['status']} how={h.get('how')} files={h.get('files')} "
          f"in C:\\RECYCLED: {bin_has}")

    h = ask("FSRM", SRC, permanent=True)
    check("delete-permanent", h["status"] == "ok" and h.get("how") == "deleted"
          and not present(SRC), f"{h['status']} how={h.get('how')}")

    h = ask("FSRM", "C:\\")
    check("delete-drive-root", h["status"] == "drive_root", h["status"])

    with contextlib.suppress(Exception):
        r.rmtree(BASE)
    if other:
        with contextlib.suppress(Exception):
            r.rmtree(other)
    r.close()
    conn.close()
    print(f"\n{sum(results)}/{len(results)} as expected")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
