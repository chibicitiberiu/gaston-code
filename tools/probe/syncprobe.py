#!/usr/bin/env python3
"""Drive the real guest's sync state machine, one handcrafted frame at a time.

Most of the guest's SYNC statuses are unreachable from the bridge on purpose:
`changed` needs a save to land between the scan and the commit, `crc_mismatch`
needs a corrupted transfer, `is_directory` needs a push at a path the guest
holds as a directory. The bridge will never produce any of them deliberately,
so on the real machine that code had never executed.

Trying to provoke them by racing did not work either. A 2.4MB push over
loopback finishes in about a second, which is not a window you can type into,
and `--link-bps` throttles the bridge's own sending logic rather than the wire.

So this is a bridge stub: enough of the handshake to satisfy the guest, then
whatever frames the test wants. It talks to the SHIPPING gaston.exe over a
real socket, so what it proves is about the binary on the Windows 98 machine
and not about a model of it.

    python3 tools/probe/syncprobe.py            # run every check
    python3 tools/probe/syncprobe.py --port 7890

Start it INSTEAD OF the bridge, then launch the client on the guest.
"""
from __future__ import annotations

import os
import socket
import sys
import time
import zlib

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import probelib
import toolkit

toolkit.on_path()

from bridge.files import paths as P  # noqa: E402
from bridge.wire.protocol import Frame, FrameReader  # noqa: E402

CHUNK = 8192


class Guest:
    """One connected guest, spoken to synchronously."""

    def __init__(self, conn: socket.socket):
        self.c = conn
        self.r = FrameReader()
        self.q: list[Frame] = []
        self.seq = 0

    def send(self, f: Frame) -> None:
        self.c.sendall(f.encode())

    def recv(self, want: str | tuple[str, ...], timeout: float = 30.0) -> Frame:
        want = (want,) if isinstance(want, str) else want
        self.c.settimeout(timeout)
        while True:
            for i, f in enumerate(self.q):
                if f.type in want:
                    return self.q.pop(i)
            data = self.c.recv(65536)
            if not data:
                raise RuntimeError(f"guest closed while waiting for {want}")
            self.q.extend(self.r.feed(data))

    def sid(self) -> str:
        self.seq += 1
        return f"s{self.seq}"

    # --- the operations under test -------------------------------------

    def scan(self) -> tuple[dict, dict]:
        sid = self.sid()
        self.send(Frame("SYNCSCAN", {"id": sid}))
        rows = {}
        while True:
            f = self.recv(("SYNCENT", "SYNCEND"))
            if f.type == "SYNCEND":
                return rows, f.headers
            for line in f.body.split(b"\n"):
                line = line.rstrip(b"\r")
                if not line:
                    continue
                # v2: both checksums per row. The bridge picks, because it is
                # the side that reads .gitattributes.
                p = line.split(b" ", 6)
                if len(p) < 7:
                    continue
                sp = p[6].index(b" ")
                name = P.wire_decode(p[6][sp + 1:sp + 1 + int(p[6][:sp])])
                rows[name] = {"type": p[0].decode(), "cls": p[1].decode(),
                              "size": int(p[2]),
                              "crc_norm": int(p[4]), "crc_raw": int(p[5])}
                rows[name]["crc"] = (rows[name]["crc_norm"]
                                     if rows[name]["cls"] == "t"
                                     else rows[name]["crc_raw"])

    def put(self, rel: str, data: bytes, *, if_crc: str,
            crc: int | None = None, cls: str | None = None) -> dict:
        """One push. `if_crc`, `crc` and `cls` are overridable so a test can
        lie about them -- which is the entire point of this file."""
        sid = self.sid()
        cls = cls or P.classify(data)
        wire = P.to_guest(data, cls)
        crc = zlib.crc32(P.normalise(data, cls)) if crc is None else crc
        rb = P.wire_encode(rel)
        prefix = str(len(rb)).encode() + b" " + rb
        off = 0
        while True:
            self.send(Frame("SYNCPUT", {"id": sid, "total": str(len(wire)),
                                        "off": str(off)},
                            prefix + wire[off:off + CHUNK]))
            off += CHUNK
            if off >= len(wire):
                break
        self.send(Frame("SYNCPUTEND", {"id": sid, "crc": str(crc),
                                       "cls": cls, "if_crc": if_crc}))
        return self.recv("SYNCPUTACK").headers

    def mkdir(self, rel: str) -> dict:
        sid = self.sid()
        self.send(Frame("SYNCMKDIR", {"id": sid}, P.wire_encode(rel)))
        return self.recv("SYNCMKDIRACK").headers

    def get(self, rel: str) -> tuple[dict, bytes]:
        sid = self.sid()
        self.send(Frame("SYNCGET", {"id": sid}, P.wire_encode(rel)))
        buf = bytearray()
        while True:
            f = self.recv(("SYNCDAT", "SYNCDATEND"))
            if f.type == "SYNCDATEND":
                return f.headers, bytes(buf)
            buf.extend(f.body)


def dos(cmd: str) -> None:
    """Run a command in the guest's DOS box, out of band.

    Some states cannot be reached through the protocol at all -- a read-only
    attribute, a stray `.gaston-tmp` -- so they are set up the way a user would
    and then observed through it.
    """
    import subprocess
    subprocess.run(["python3", toolkit.repo_path("tools", "vm", "dos.py"), cmd],
                   check=True, capture_output=True)


# --- the checks -------------------------------------------------------------

FAILS: list[str] = []


def _short(v) -> str:
    """A 70KB binary is a real test case and a useless log line."""
    r = repr(v)
    return r if len(r) <= 60 else f"{r[:40]}... ({len(v)} bytes)"


def check(name: str, got, want) -> None:
    ok = got == want
    print(f"  {'ok  ' if ok else 'FAIL'} {name}: {_short(got)}"
          + ("" if ok else f" (want {_short(want)})"))
    if not ok:
        FAILS.append(name)


def run(g: Guest) -> None:
    print("\n== baseline: a clean push and a read-back ==")
    body = b"line one\nline two\n"
    check("push a new file", g.put("probe.txt", body, if_crc="absent")["status"],
          "ok")
    rows, _ = g.scan()
    check("it appears in the manifest", "probe.txt" in rows, True)
    check("stored native (CRLF)", rows["probe.txt"]["size"], len(body) + 2)
    check("crc is over normalised bytes", rows["probe.txt"]["crc"],
          zlib.crc32(body))

    print("\n== v2 manifest: both checksums, and they are both right ==")
    # The override machinery rests entirely on this. The guest calls probe.txt
    # text and reports the normalised checksum as its own; the RAW one is the
    # number a `-text` rule would be compared against, and before v2 it was
    # computed and thrown away -- so nothing ever checked it.
    stored = body.replace(b"\n", b"\r\n")          # native on this machine
    check("the row carries the normalised checksum", rows["probe.txt"]["crc_norm"],
          zlib.crc32(body))
    check("the row carries the raw checksum too", rows["probe.txt"]["crc_raw"],
          zlib.crc32(stored))
    check("and they differ for a CRLF file",
          rows["probe.txt"]["crc_norm"] != rows["probe.txt"]["crc_raw"], True)

    print("\n== a -text override: byte-exact against a file it calls text ==")
    # What `.gitattributes  *.txt -text` produces on the wire. The guest's own
    # classifier says 't' for these bytes; the bridge declares 'b' and sends
    # the RAW checksum. Both the commit check and the if_crc re-read have to
    # follow the declared class, or every push comes back crc_mismatch (the
    # commit) or changed (the compare-and-swap), forever.
    exact = b"kept\r\nexactly\r\n"
    ack = g.put("exact.txt", exact, if_crc="absent", cls="b",
                crc=zlib.crc32(exact))
    check("a byte-exact push is accepted", ack["status"], "ok")
    _, back = g.get("exact.txt")
    check("and arrives back byte for byte", back, exact)
    rows, _ = g.scan()
    check("the guest still calls it text on its own", rows.get("exact.txt", {}).get("cls"),
          "t")
    check("but its raw checksum is what we sent", rows.get("exact.txt", {}).get("crc_raw"),
          zlib.crc32(exact))

    nxt = b"second\r\nversion\r\n"
    ack = g.put("exact.txt", nxt, if_crc=str(zlib.crc32(exact)), cls="b",
                crc=zlib.crc32(nxt))
    check("if_crc against the RAW checksum is accepted", ack["status"], "ok")
    ack = g.put("exact.txt", b"no\r\n", if_crc=str(zlib.crc32(b"wrong")),
                cls="b", crc=zlib.crc32(b"no\r\n"))
    check("and a wrong raw if_crc is still refused", ack["status"], "changed")

    print("\n== compare-and-swap: the guest refuses when the world moved ==")
    # This is the mechanism the whole design rests on. The bridge believes the
    # guest holds something it does not, which is what a save landing after the
    # scanner walked past the file produces.
    ack = g.put("probe.txt", b"the agent's rewrite\n", if_crc="999999")
    check("a wrong if_crc is refused", ack["status"], "changed")
    _, back = g.get("probe.txt")
    check("and the file was NOT written", P.to_bridge(back, "t"), body)

    ack = g.put("probe.txt", b"the agent's rewrite\n", if_crc="absent")
    check("if_crc=absent over an existing file is refused", ack["status"],
          "changed")
    _, back = g.get("probe.txt")
    check("still not written", P.to_bridge(back, "t"), body)

    ack = g.put("probe.txt", b"a legitimate update\n",
                if_crc=str(zlib.crc32(body)))
    check("the right if_crc is accepted", ack["status"], "ok")
    _, back = g.get("probe.txt")
    check("and the file DID change", P.to_bridge(back, "t"),
          b"a legitimate update\n")

    print("\n== the guest verifies what it received ==")
    ack = g.put("corrupt.txt", b"content\n", if_crc="absent", crc=12345)
    check("a wrong crc is refused", ack["status"], "crc_mismatch")
    rows, _ = g.scan()
    check("and no file is left behind", "corrupt.txt" in rows, False)
    check("nor is a temp file", any(".gaston-" in k for k in rows), False)

    print("\n== paths the guest must refuse ==")
    check("escaping the root", g.put("../escaped.txt", b"x\n",
                                     if_crc="absent")["status"], "bad_path")
    check("an absolute path", g.put("C:\\escaped.txt", b"x\n",
                                    if_crc="absent")["status"], "bad_path")
    check("a dot component", g.put("./x.txt", b"x\n",
                                   if_crc="absent")["status"], "bad_path")
    check("into a missing directory", g.put("nope/x.txt", b"x\n",
                                            if_crc="absent")["status"],
          "no_parent")

    print("\n== directories ==")
    check("mkdir", g.mkdir("sub")["status"], "ok")
    check("mkdir again is 'exists', not an error", g.mkdir("sub")["status"],
          "exists")
    check("now the child lands", g.put("sub/x.txt", b"x\n",
                                       if_crc="absent")["status"], "ok")
    check("mkdir over a file", g.mkdir("probe.txt")["status"], "is_file")
    check("mkdir escaping the root", g.mkdir("../evil")["status"], "bad_path")
    # rc_sync_contained checks the path's SHAPE and says nothing about its
    # length, and the frame body is bounded by RC_MAX_FRAME rather than
    # MAX_PATH. Before do_mkdir checked rc_sync_join's return, this joined to
    # a truncated name and created a DIFFERENT directory (or answered
    # no_parent, which reads as "the bridge sent these out of order").
    check("mkdir a path too long to form", g.mkdir("d" * 260)["status"],
          "too_long")

    print("\n== get ==")
    st, _ = g.get("does-not-exist.txt")
    check("a missing file", st["status"], "missing")
    st, _ = g.get("sub")
    check("a directory", st["status"], "is_directory")
    st, _ = g.get("../escaped.txt")
    # `bad_path`, not `missing`: the probe asked for `missing` here until the
    # do_get comment explaining the difference was written and the code
    # changed under it. `missing` is something a retry can never fix;
    # `bad_path` blames the path the bridge sent.
    check("escaping the root", st["status"], "bad_path")
    # The worst case of an unchecked join: the truncated path names another
    # file that EXISTS, so its bytes go back acked as the path that was asked
    # for and the bridge writes one file over another with a crc that matches.
    st, _ = g.get("d" * 260)
    check("get a path too long to form", st["status"], "too_long")

    print("\n== byte fidelity, straight off the machine ==")
    cases = {
        "bin.dat":   bytes(range(256)) * 3,
        "crcr.txt":  b"a\r\r\nb\r\r\n",
        "mixed.txt": b"a\r\nb\nc\r\n",
        "lonecr.txt": b"a\rb\rc",
        "empty.txt": b"",
        "utf16.txt": "caf\u00e9 \u2014 x\n".encode("utf-16-le"),
        "big.bin":   bytes((i * 7919) % 251 for i in range(70000)),
    }
    for name, data in cases.items():
        cls = P.classify(data)
        st = g.put(name, data, if_crc="absent")
        if st["status"] != "ok":
            check(f"{name} push", st["status"], "ok")
            continue
        hdr, wire = g.get(name)
        check(f"{name} ({cls}) round-trips byte for byte",
              P.to_bridge(wire, hdr.get("cls", cls)), data)

    print("\n== a read-only target ==")
    # DeleteFileA fails on a read-only file, then MoveFileA fails because the
    # target still exists, and the temp is left in the tree -- where the next
    # scan picks it up as a new file and pushes it to the bridge. The attribute
    # has to be cleared and put back.
    v1 = b"before\n"
    check("push", g.put("ro.txt", v1, if_crc="absent")["status"], "ok")
    dos("ATTRIB +R C:\\WORK\\ro.txt")
    v2 = b"after\n"
    check("push over a read-only file", g.put("ro.txt", v2,
                                              if_crc=str(zlib.crc32(v1)))["status"],
          "ok")
    hdr, wire = g.get("ro.txt")
    check("the new content is there", P.to_bridge(wire, "t"), v2)
    rows, _ = g.scan()
    check("and no temp file was left behind",
          [k for k in rows if ".gaston-" in k], [])
    # Pushing again proves the read-only attribute was RESTORED and is still
    # handled -- if it had been left clear, this would pass for the wrong
    # reason, so the check is that it works twice rather than once.
    check("and it can be written again", g.put("ro.txt", b"third\n",
                                               if_crc=str(zlib.crc32(v2)))["status"],
          "ok")

    print("\n== a push at a path the guest holds as a directory ==")
    check("mkdir", g.mkdir("adir")["status"], "ok")
    check("pushing a file there is refused",
          g.put("adir", b"x\n", if_crc="absent")["status"] in
          ("is_directory", "bad_path", "readonly", "changed"), True)
    rows, _ = g.scan()
    check("and the directory is still a directory",
          rows.get("adir", {}).get("type"), "d")

    print("\n== the scanner hides our own working files ==")
    dos("ECHO leftover > C:\\WORK\\probe.txt.gaston-tmp")
    dos("ECHO leftover > C:\\WORK\\probe.txt.gaston-old")
    rows, _ = g.scan()
    check("neither is offered to the bridge",
          sorted(k for k in rows if ".gaston-" in k), [])

    print("\n== the streaming gate: chunks must not be answered 'busy' ==")
    # A transfer is a stream, and the one-at-a-time job slot used to reject
    # chunks 2..N of anything over 8KB.
    big = bytes((i * 31) % 256 for i in range(200000))
    check("a 200KB push in 25 chunks", g.put("stream.bin", big,
                                             if_crc="absent")["status"], "ok")
    hdr, wire = g.get("stream.bin")
    check("and it arrives intact", wire, big)


def run_recovery_check(g: Guest) -> None:
    """What the guest did to the tree at STARTUP, before the first scan.

    `rc_sync_commit` moves the old file aside to `<p>.gaston-old` and then
    renames the temp in. `MoveFileExA(REPLACE_EXISTING)` is NT-only, so there
    is a real instant with no file at the path -- that window is recoverable
    rather than absent, and this is the recovery. Without it the first scan
    after a crash reports the path as ABSENT, which the bridge reads as "the
    user deleted it".

    The tree is staged from the DOS box beforehand, because the states this
    covers are ones a healthy guest never produces.
    """
    print("\n== recovery from an interrupted commit ==")
    rows, _ = g.scan()
    check("the tree was staged before the client restarted",
          sorted(rows), ["finished.txt", "interrupted.txt", "sub",
                         "sub/deep.txt"])
    check("the aside file was put back", "interrupted.txt" in rows, True)
    check("including one in a subdirectory", "sub/deep.txt" in rows, True)
    check("an abandoned transfer's temp was deleted",
          "abandoned.txt.gaston-tmp" in rows, False)
    check("a finished commit's aside was dropped, not restored over it",
          "finished.txt" in rows, True)
    check("and nothing of ours is visible at all",
          sorted(k for k in rows if ".gaston-" in k), [])
    hdr, wire = g.get("interrupted.txt")
    check("the recovered content is the one that was moved aside",
          P.to_bridge(wire, hdr.get("cls", "t")),
          b"the version that was moved aside\n")
    hdr, wire = g.get("finished.txt")
    check("and the committed file was NOT overwritten by its aside",
          P.to_bridge(wire, hdr.get("cls", "t")),
          b"the committed version\n")


def stage_recovery(g: Guest) -> None:
    """Put the tree into the state an interrupted commit leaves behind.

    The DOS box cannot do this. `ECHO x > C:\\WORK\\a.txt.gaston-old` looks like
    it works and silently writes a mangled 8.3 name instead, so the first run
    of this test "passed" three of its checks by having created nothing at all.
    Push the files through the protocol, where the names are exact, then rename
    them with REN, which does speak long filenames.
    """
    print("staging an interrupted commit ...")
    for rel, body in (
            ("interrupted.txt", b"the version that was moved aside\n"),
            ("abandoned.txt",   b"half a write\n"),
            ("finished.txt",    b"the committed version\n"),
            ("stale.txt",       b"stale aside\n")):
        st = g.put(rel, body, if_crc="absent")["status"]
        if st != "ok":
            raise SystemExit(f"could not stage {rel}: {st}")
    g.mkdir("sub")
    g.put("sub/deep.txt", b"deep one\n", if_crc="absent")

    # interrupted: the aside exists and the real file does not, which is the
    # instant between the two renames -> must be put back
    dos("REN C:\\WORK\\interrupted.txt interrupted.txt.gaston-old")
    dos("REN C:\\WORK\\SUB\\deep.txt deep.txt.gaston-old")
    # abandoned: a temp from a transfer that never finished -> deleted
    dos("REN C:\\WORK\\abandoned.txt abandoned.txt.gaston-tmp")
    # finished: the commit completed, so the aside is stale -> dropped, and
    # the committed file must NOT be overwritten by it
    dos("REN C:\\WORK\\stale.txt finished.txt.gaston-old")
    print("staged; restart the client, then run --recovery")


def main() -> int:
    ap = probelib.arg_parser()
    ap.add_argument("--stage-recovery", action="store_true",
                    help="put the guest tree into a post-crash state and exit")
    ap.add_argument("--recovery", action="store_true",
                    help="only the startup-recovery checks; the tree must be "
                         "staged first and the client restarted after this "
                         "starts listening")

    a = ap.parse_args()

    conn, _peer = probelib.listen_for_guest(a.host, a.port)

    g = Guest(conn)
    hello = g.recv("HELLO")
    if hello.headers.get("secret") != a.secret:
        print("bad secret")
        return 2
    # Minimal handshake. The guest needs an active work directory and
    # OPTIONSEND to consider itself live and initialise its sync root.
    g.send(Frame("OPTION", {"kind": "work", "id": "C:\\WORK", "label": "C:\\WORK"}))
    g.send(Frame("OPTIONSEND", {}))
    time.sleep(2.0)
    print(f"guest says mem_kb={hello.headers.get('mem_kb')} "
          f"ver={hello.headers.get('ver')}")

    try:
        if a.stage_recovery:
            stage_recovery(g)
        elif a.recovery:
            run_recovery_check(g)
        else:
            run(g)
    finally:
        conn.close()

    print()
    if FAILS:
        print(f"{len(FAILS)} FAILURES: {', '.join(FAILS)}")
        return 1
    print("all guest-side sync checks passed on the real machine")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
