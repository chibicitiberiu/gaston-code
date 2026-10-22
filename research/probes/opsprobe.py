"""The fs mutations, list_dir on a file, launch and live memory, on a guest.

Each case sends one frame and checks the status word the guest answers --
the reasons in win/fs/ops.cpp are classified on Windows 98's own error codes,
which only the guest can produce. Works in its own scratch folder under
C:\\GTEST and removes it.

    python3 -u research/probes/opsprobe.py --host 127.0.0.1 --port 7893 \\
        --remote 127.0.0.1:vm-secret

Start it INSTEAD OF the bridge, then launch the client. `--remote` puts the
one file the delete case needs, and ends the Notepad the launch case starts.
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

BASE = "C:\\GTEST\\OPS"


def cases(cd: str):
    """(name, frame builder, wanted status, extra header checks)."""
    return [
        ("mkdir-with-parents", lambda i: PR.fs_op(i, "FSMKDIR",
                                                  BASE + "\\P1\\P2\\P3"),
         "ok"),
        ("mkdir-again-is-fine", lambda i: PR.fs_op(i, "FSMKDIR",
                                                   BASE + "\\P1\\P2\\P3"),
         "ok"),
        ("mkdir-on-a-file", lambda i: PR.fs_op(i, "FSMKDIR",
                                               BASE + "\\DEL.TXT"), "is_file"),
        ("ls-a-file-is-one-row", lambda i: PR.fs_ls(i, BASE + "\\DEL.TXT"),
         "ok"),
        ("move-folder", lambda i: PR.fs_op(i, "FSMV", BASE + "\\P1",
                                           BASE + "\\Q1"), "ok"),
        ("move-onto-existing", lambda i: PR.fs_op(i, "FSMV", BASE + "\\Q1",
                                                  BASE + "\\DEL.TXT"),
         "exists"),
        # Across drives, MEASURED on the Athlon: a FILE moves (98 copies it)
        # and a FOLDER does not. `--cd` may be somebody's data disk, so the
        # file goes to a distinctive name and comes straight back. On a
        # read-only CD as `--cd` the file move is refused instead.
        ("move-file-across-drives", lambda i: PR.fs_op(
            i, "FSMV", BASE + "\\DEL.TXT", cd + "\\GOPSPRB.TMP"),
         ("ok", "denied")),
        ("move-file-back", lambda i: PR.fs_op(
            i, "FSMV", cd + "\\GOPSPRB.TMP", BASE + "\\DEL.TXT"),
         ("ok", "missing")),
        ("move-folder-across-drives", lambda i: PR.fs_op(
            i, "FSMV", BASE + "\\Q1", cd + "\\GOPSDIR.TMP"), "cross_volume"),
        ("rmdir-not-empty", lambda i: PR.fs_op(i, "FSRMDIR", BASE + "\\Q1"),
         "not_empty"),
        ("rmdir-drive-root", lambda i: PR.fs_op(i, "FSRMDIR", "C:\\"),
         "drive_root"),
        ("rm-a-folder", lambda i: PR.fs_op(i, "FSRM", BASE + "\\Q1"),
         "is_directory"),
        ("rm-missing", lambda i: PR.fs_op(i, "FSRM", BASE + "\\NOPE.TXT"),
         "missing"),
        ("rm-a-file", lambda i: PR.fs_op(i, "FSRM", BASE + "\\DEL.TXT"),
         "ok"),
        ("rmdir-leaf", lambda i: PR.fs_op(i, "FSRMDIR",
                                          BASE + "\\Q1\\P2\\P3"), "ok"),
        ("rmdir-middle", lambda i: PR.fs_op(i, "FSRMDIR", BASE + "\\Q1\\P2"),
         "ok"),
        ("rmdir-top", lambda i: PR.fs_op(i, "FSRMDIR", BASE + "\\Q1"), "ok"),
        ("rmdir-base", lambda i: PR.fs_op(i, "FSRMDIR", BASE), "ok"),
        ("launch-notepad", lambda i: PR.ps_launch(i, "notepad.exe"), "ok"),
        ("launch-missing", lambda i: PR.ps_launch(i, "C:\\NOPE\\NOPE.EXE"),
         "not_found"),
        ("ps-carries-memory", lambda i: PR.ps_list(i), "ok"),
    ]


def main() -> int:
    ap = probelib.arg_parser()
    ap.add_argument("--remote", default="")
    ap.add_argument("--cd", default="D:", help="a different volume")
    a = ap.parse_args()

    remote = None
    if a.remote:
        from gremote.client import Remote
        host, secret = a.remote.rsplit(":", 1)
        remote = Remote(host, secret=secret)
        remote.connect()
        with contextlib.suppress(Exception):      # there already
            remote.mkdir(BASE)
        remote.put(BASE + "\\DEL.TXT", b"delete me\r\n")

    conn, _ = probelib.listen_for_guest(a.host, a.port)
    reader = FrameReader()
    conn.settimeout(30)
    hello = None
    while hello is None:
        for f in reader.feed(conn.recv(65536)):
            if f.type == "HELLO":
                hello = f
    if hello.headers.get("ops") != "1":
        print(f"this client does not declare ops=1 ({hello.headers})")
        return 2
    conn.sendall(PR.ready("opsprobe", "none").encode())
    time.sleep(2)

    def ask(frame: Frame) -> tuple[Frame, list[Frame]]:
        conn.sendall(frame.encode())
        rows = []
        while True:
            for f in reader.feed(conn.recv(65536)):
                if f.headers.get("id") != frame.headers["id"]:
                    if f.type == "PING":
                        conn.sendall(Frame("PONG", {}).encode())
                    continue
                if f.type in ("FSENT",):
                    rows.append(f)
                    continue
                return f, rows

    fails = 0
    for n, (name, build, want) in enumerate(cases(a.cd)):
        # The client checks an id's prefix against the subsystem it is
        # routed to (`f` for fs, `p` for processes) and drops a stranger.
        frame = build("")
        frame.headers["id"] = ("p" if frame.type.startswith("PS") else
                               "f") + str(900 + n)
        end, rows = ask(frame)
        status = end.headers.get("status")
        wanted = want if isinstance(want, tuple) else (want,)
        ok = status in wanted
        extra = ""
        if name == "ls-a-file-is-one-row":
            body = b"".join(r.body for r in rows).decode("cp1252", "replace")
            ok = ok and end.headers.get("count") == "1" and "DEL.TXT" in body
            extra = f" rows={body.strip()!r}"
        if name == "ps-carries-memory":
            ok = ok and all(k in end.headers for k in (
                "mem_kb", "mem_free_kb", "swap_kb", "swap_free_kb",
                "mem_load"))
            extra = " " + " ".join(f"{k}={end.headers.get(k)}" for k in (
                "mem_kb", "mem_free_kb", "swap_kb", "swap_free_kb",
                "mem_load"))
        if name == "launch-notepad" and remote and status == "ok":
            pid = int(end.headers["pid"], 16)
            time.sleep(2)
            extra = f" pid={end.headers['pid']} running=" + str(
                any(p.pid == pid for p in remote.ps()))
            remote.kill(pid)
        fails += not ok
        print(f"[{'PASS' if ok else 'FAIL'}] {name}: status={status} "
              f"want={'/'.join(wanted)}{extra}", flush=True)

    if remote:
        with contextlib.suppress(Exception):
            remote.rm(a.cd + "\\GOPSPRB.TMP")
            print(f"(removed a stray {a.cd}\\GOPSPRB.TMP)")
        with contextlib.suppress(Exception):
            remote.rmtree(a.cd + "\\GOPSDIR.TMP")
            print(f"(removed a stray {a.cd}\\GOPSDIR.TMP)")
    print(f"\n{len(cases(a.cd)) - fails}/{len(cases(a.cd))} as expected")
    if remote:
        remote.close()
    conn.close()
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
