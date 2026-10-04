"""What the client holds in physical memory: one build against another.

The method of docs/measurements/workingset-vm-20261002.txt, as a tool: each
build starts FRESH from its own folder, idle, pointed at a port nothing
listens on (no session, no transcript); free physical memory is sampled with
it up and again after it ends, and the two builds alternate, because free
memory on 98 drifts as the disk cache settles and only a pair taken together
compares.

    .venv/bin/python tools/probe/wsprobe.py --remote 127.0.0.1:7401:vm-secret \\
        --old ~/backups/gaston-baseline-91b6b76.exe --new build/win/gaston.exe

The machine's own client (C:\\GASTON) is single-instance, so it is stopped
for the run and started again afterwards.
"""
from __future__ import annotations

import argparse
import contextlib
import os
import statistics
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import toolkit

toolkit.on_path()

from gremote.client import Remote  # noqa: E402

INI = (b"[bridge]\r\nhost=127.0.0.1\r\nport=1\r\nsecret=x\r\n"
       b"[sync]\r\nroot=\r\n")


def free_kb(r: Remote, n: int = 5) -> int:
    samples = []
    for _ in range(n):
        samples.append(r.sysinfo().mem_free_kb)
        time.sleep(2)
    return int(statistics.median(samples))


def stop_clients(r: Remote) -> None:
    for p in r.ps():
        if p.exe.upper().endswith("\\GASTON.EXE"):
            r.kill(p.pid)
    time.sleep(3)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--remote", required=True, help="host:port:secret")
    ap.add_argument("--old", required=True)
    ap.add_argument("--new", required=True)
    ap.add_argument("--rounds", type=int, default=2)
    a = ap.parse_args()
    host, port, secret = a.remote.split(":")
    r = Remote(host, secret=secret, port=int(port))
    r.connect()

    stop_clients(r)
    for d in ("C:\\GTEST", "C:\\GTEST\\WSOLD", "C:\\GTEST\\WSNEW"):
        with contextlib.suppress(Exception):
            r.mkdir(d)
    detect = r.get("C:\\GASTON\\TOOLS_DETECT.INI").data
    for name, exe in (("WSOLD", a.old), ("WSNEW", a.new)):
        with open(exe, "rb") as fh:
            r.put(f"C:\\GTEST\\{name}\\GASTON.EXE", fh.read())
        r.put(f"C:\\GTEST\\{name}\\GASTON.INI", INI)
        r.put(f"C:\\GTEST\\{name}\\TOOLS_DETECT.INI", detect)

    rows = []
    for rnd in range(a.rounds):
        for name in ("WSOLD", "WSNEW"):
            r.launch(f"C:\\GTEST\\{name}\\GASTON.EXE", cwd=f"C:\\GTEST\\{name}")
            time.sleep(20)
            up = free_kb(r)
            stop_clients(r)
            time.sleep(15)
            down = free_kb(r)
            rows.append((rnd, name, up, down, down - up))
            print(f"round {rnd}  {name}  free up {up:,}  down {down:,}  "
                  f"holds {down - up:,} KB", flush=True)

    for name in ("WSOLD", "WSNEW"):
        held = [x[4] for x in rows if x[1] == name]
        print(f"{name}: median holds {int(statistics.median(held)):,} KB")
    try:
        r.rmtree("C:\\GTEST\\WSOLD")
        r.rmtree("C:\\GTEST\\WSNEW")
    except Exception:
        pass
    r.launch("C:\\GASTON\\GASTON.EXE", cwd="C:\\GASTON")
    return 0


if __name__ == "__main__":
    sys.exit(main())
