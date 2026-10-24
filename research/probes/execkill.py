#!/usr/bin/env python3
"""Drive the guest's exec path the way the bridge does, and watch it stop.

WHY. On Windows 98 a piped `COMMAND.COM /c` job is REDIR32.EXE -> the DOS VM
-> the program, and TerminateProcess on the pid exec held did nothing: every
timeout and EXECKILL leaked a REDIR32 (notes/BACKLOG.md 14). libexec stops the
whole tree now, and EXECKILL names the command it kills. Both are only real
on the real machine, so this stands in for the bridge and runs:

  1. a quick command through the shell            -> ok, output
  2. a DOS program that outlives its timeout       -> timeout
  3. a long command, then an EXECKILL for ANOTHER id, then one with none
     (both must be ignored), then the real one     -> cancelled
  4. an EXEC queued behind a busy one, then killed -> cancelled, never ran
  5. a Win32 program under the shell, timed out and killed: on 98 the stop
     kills only the Win32 side and the job ends itself, so the log shows no
     leak for these two (the DOS program in 2 and 3 still costs one each)

    python3 -u research/probes/execkill.py --port 7891

Start it INSTEAD OF the bridge (on a port the guest's INI names), then launch
the client. Afterwards `EXECPRB.EXE --list` on the guest shows what was left.
"""
from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))), "tools"))
import probelib
import toolkit

toolkit.on_path()

from bridge.wire.protocol import Frame, FrameReader  # noqa: E402

SHELL = r"C:\WINDOWS\COMMAND.COM /c"
CHOICE = r"C:\WINDOWS\COMMAND\CHOICE.COM"


class Link:
    def __init__(self, conn):
        self.conn = conn
        self.reader = FrameReader()
        self.pend: list[Frame] = []
        self.t0 = time.time()

    def send(self, f: Frame) -> None:
        self.conn.sendall(f.encode())
        print(f"[{self.ms():7.0f}ms] -> {f.type} "
              f"{' '.join(f'{k}={v}' for k, v in f.headers.items())} "
              f"{f.body[:60]!r}" if f.body else
              f"[{self.ms():7.0f}ms] -> {f.type} "
              f"{' '.join(f'{k}={v}' for k, v in f.headers.items())}")

    def ms(self) -> float:
        return (time.time() - self.t0) * 1000

    def next(self, timeout: float):
        deadline = time.time() + timeout
        while not self.pend:
            left = deadline - time.time()
            if left <= 0:
                return None
            self.conn.settimeout(left)
            try:
                data = self.conn.recv(65536)
            except TimeoutError:
                return None
            if not data:
                raise SystemExit("guest closed the link")
            self.pend.extend(self.reader.feed(data))
        return self.pend.pop(0)

    def result(self, xid: str, timeout: float) -> dict:
        """Wait for EXECRESULT id=xid, printing everything on the way."""
        out = b""
        deadline = time.time() + timeout
        while time.time() < deadline:
            f = self.next(deadline - time.time())
            if f is None:
                break
            if f.type == "EXECOUT" and f.headers.get("id") == xid:
                out += f.body
                continue
            if f.type == "EXECRESULT":
                print(f"[{self.ms():7.0f}ms] <- EXECRESULT "
                      f"{' '.join(f'{k}={v}' for k, v in f.headers.items())}")
                if f.headers.get("id") == xid:
                    r = dict(f.headers)
                    r["out"] = out
                    return r
                continue
            if f.type not in ("PING", "STAT"):
                print(f"[{self.ms():7.0f}ms] <- {f.type}")
        return {"status": "(no result)", "out": out}


def exec_frame(xid: str, body: str, timeout_ms: int, shell: str) -> Frame:
    return Frame("EXEC", {"id": xid, "timeout_ms": str(timeout_ms),
                          "shell": shell}, body.encode("cp437"))


def main() -> int:
    ap = probelib.arg_parser()
    a = ap.parse_args()
    conn, _peer = probelib.listen_for_guest(a.host, a.port)
    link = Link(conn)
    hello = None
    while hello is None:
        f = link.next(30)
        if f is None:
            raise SystemExit("no HELLO")
        if f.type == "HELLO":
            hello = f
    if not probelib.secret_ok(hello, a.secret):
        print("bad secret")
        return 2
    print(f"guest: {hello.headers.get('os')} mem_kb={hello.headers.get('mem_kb')}"
          f" acp={hello.headers.get('acp')} oemcp={hello.headers.get('oemcp')}")
    time.sleep(2)
    verdicts = []

    def verdict(name, ok, detail=""):
        verdicts.append((name, ok))
        print(f"{'pass' if ok else 'FAIL'}\t{name}\t{detail}", flush=True)

    # 1. quick, through the shell
    link.t0 = time.time()
    link.send(exec_frame("x1", "ver", 20000, SHELL))
    r = link.result("x1", 30)
    verdict("shell-command-runs", r.get("status") == "ok" and b"Windows" in r["out"],
            f"status={r.get('status')} out={r['out'][:60]!r}")

    # 2. a DOS program that outlives its timeout, through the shell
    link.send(exec_frame("x2", f"{CHOICE} /c:y /t:y,60", 3000, SHELL))
    t = time.time()
    r = link.result("x2", 30)
    took = time.time() - t
    verdict("timeout-reported", r.get("status") == "timeout" and took < 15,
            f"status={r.get('status')} after {took:.1f}s")

    # 3. stale and id-less kills are ignored, the real one works
    link.send(exec_frame("x3", f"{CHOICE} /c:y /t:y,60", 60000, SHELL))
    time.sleep(3)
    link.send(Frame("EXECKILL", {"id": "x999"}))
    link.send(Frame("EXECKILL", {}))
    r = link.result("x3", 4)
    verdict("stale-kills-ignored", r.get("status") == "(no result)",
            f"got {r.get('status')} within 4s")
    t = time.time()
    link.send(Frame("EXECKILL", {"id": "x3"}))
    r = link.result("x3", 20)
    took = time.time() - t
    verdict("kill-cancels", r.get("status") == "cancelled" and took < 15,
            f"status={r.get('status')} after {took:.1f}s")

    # 4. a queued EXEC killed before it runs
    link.send(exec_frame("x4", f"{CHOICE} /c:y /t:y,8", 20000, SHELL))
    time.sleep(1.5)
    link.send(exec_frame("x5", r"echo ran > C:\EP\X5RAN.TXT", 20000, SHELL))
    time.sleep(0.5)
    link.send(Frame("EXECKILL", {"id": "x5"}))
    r5 = link.result("x5", 5)
    verdict("queued-kill-answers-cancelled", r5.get("status") == "cancelled",
            f"status={r5.get('status')}")
    r4 = link.result("x4", 30)
    verdict("the-busy-one-still-finishes", r4.get("status") == "ok",
            f"status={r4.get('status')}")
    link.send(exec_frame("x6", r"if exist C:\EP\X5RAN.TXT echo X5-RAN", 20000,
                         SHELL))
    r6 = link.result("x6", 30)
    verdict("queued-kill-never-ran", r6.get("status") == "ok" and
            b"X5-RAN" not in r6["out"], f"out={r6['out'][:40]!r}")

    # 5. a Win32 program under the shell: stopped by timeout and by EXECKILL.
    #    The stop kills only the Win32 side and lets the job end itself, so
    #    on 98 neither should leave a REDIR32 (execprobe --gentle).
    link.send(exec_frame("x7", r"C:\EP\EXECPRB.EXE --tick C:\EP\T7.TXT 60",
                         3000, SHELL))
    t = time.time()
    r = link.result("x7", 30)
    took = time.time() - t
    verdict("win32-timeout-reported", r.get("status") == "timeout" and took < 15,
            f"status={r.get('status')} after {took:.1f}s")
    link.send(exec_frame("x8", r"C:\EP\EXECPRB.EXE --tick C:\EP\T8.TXT 60",
                         60000, SHELL))
    time.sleep(3)
    t = time.time()
    link.send(Frame("EXECKILL", {"id": "x8"}))
    r = link.result("x8", 30)
    took = time.time() - t
    verdict("win32-kill-cancels", r.get("status") == "cancelled" and took < 15,
            f"status={r.get('status')} after {took:.1f}s")

    bad = [n for n, ok in verdicts if not ok]
    print(f"\n{len(verdicts) - len(bad)}/{len(verdicts)} passed"
          + (f"; FAILED: {', '.join(bad)}" if bad else ""))
    print("Now close the client and run C:\\EP\\EXECPRB.EXE --list on the guest.")
    conn.close()
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
