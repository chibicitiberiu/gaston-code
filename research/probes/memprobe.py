"""What gremote costs this machine, idle and UNDER LOAD.

`docs/REMOTE.md`'s P200 checklist asks for "gremote memory idle and under
load, with and without the client running", and `REMOTE.md` wants it taken
first so later client numbers can subtract it. Three of those four quantities
had no instrument:

  * `research/probes/wsprobe.py` compares two CLIENT builds, idle, pointed at a
    dead port. Useful, and not this.
  * `PS` carries pid, ppid, threads and exe (`gremote/ops.py`) -- there is no
    per-process memory on this path at all, and nothing cheap on 9x that this
    codebase trusts.

So every figure here is a `GlobalMemoryStatus` differential, which attributes
a delta to a program BY CONSTRUCTION rather than by measurement. That is the
honest description of it and the reason the no-gremote baseline cannot be
taken over the wire: asking gremote how much memory is free without gremote
requires gremote not to be running. That one is a reading at the keyboard
(`PROBE.EXE platform`), and `--baseline` folds it in here.

WHAT THIS MEASURES ON A 32MB BOX, measured 2026-10-23 on the Pentium 200
(`research/measurements/memory-p200-20261023.txt`): gremote alone leaves
1,616 KB of dwAvailPhys and launching the client takes it to 8. So the
differentials below are readable WITHOUT the client and are noise WITH it --
`shot` "costs" nothing and ends 336 KB up, `idle` reads 24, 20, 0. The tool
still reports honestly either way, because it always takes a before/after
pair and prints the sample count; what it cannot do is invent headroom that
is not there. Read the before-column first and stop if it is near zero.

WHAT mem_free_kb ACTUALLY IS. `dwAvailPhys`. `BACKLOG.md` §2 records this
project being misled by that field before: it EXCLUDES the disk cache Win98
shrinks under pressure, so a small figure is not by itself evidence that
anything is short, and the "28 KB free after a turn" scare was never an
allocation failing. Read it as a relative quantity between two states of the
same machine, which is all this tool uses it for.

HOW THE SAMPLING WORKS, AND WHY IT IS ALLOWED. The load runs on one thread
and `SYSINFO` is asked from another, over the SAME connection: gremote is
single-session (a second caller gets `busy` and a FIN), but `SYSINFO` is a
control-lane op answered inline on the reader rather than behind the work,
and `Remote` holds a send lock and dispatches replies to pending calls. So a
walk can be in flight on the ui lane while this samples. Verified against the
QEMU guest 2026-10-15: concurrent sysinfo during a tree walk and during a
full-screen capture, no busy, no error.

A BEFORE/AFTER PAIR IS ALWAYS TAKEN, not just the series. On a fast emulated
guest a full-screen 24bpp capture finishes in 0.11 s -- less than one sample
interval -- so the series can legitimately contain a single point. The report
says how many samples it got, so "one sample" is visible instead of being
averaged into something that looks like a measurement.

    .venv/bin/python research/probes/memprobe.py --remote 10.0.0.125:7401:secret \\
        --baseline 20416 --load idle --load tree --load shot --load exec
"""
from __future__ import annotations

import argparse
import os
import statistics
import sys
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))), "tools"))
import toolkit

toolkit.on_path()

from gremote.client import Remote  # noqa: E402

#: Tight enough that a load lasting a second on a P200 yields several points,
#: loose enough that the sampling is not itself the load.
INTERVAL_S = 0.2

#: Disk-heavy on purpose for the `exec` load: on a 1998 IDE platter a
#: recursive directory listing is the cheapest way to make the machine work
#: for long enough to be sampled, and it allocates nothing of ours.
#:
#: IT NEEDS THE SHELL. DIR is a COMMAND.COM builtin and not an image, so a
#: direct spawn is refused with `spawn_failed: 2` -- which is what happened on
#: the first run of this tool. gremote does not choose a shell for itself (the
#: client declares the OS, HELLO says what the box is), so the prefix is
#: passed in. `exit` is meaningless through COMMAND.COM on 9x anyway -- it is
#: 0 whatever happens, which REMOTE.md's checklist records -- and this load
#: cares about the work, not the verdict.
EXEC_CMD = "DIR C:\\ /S"
EXEC_SHELL = "COMMAND.COM /C"


class Sampler:
    """Poll mem_free_kb on a thread while something else occupies the box."""

    def __init__(self, r: Remote) -> None:
        self.r = r
        self.samples: list[int] = []
        self.errors: list[str] = []
        self._stop = threading.Event()
        self._t: threading.Thread | None = None

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.samples.append(self.r.sysinfo().mem_free_kb)
            except Exception as e:
                self.errors.append(repr(e)[:120])
            self._stop.wait(INTERVAL_S)

    def __enter__(self) -> Sampler:
        self._t = threading.Thread(target=self._run, daemon=True)
        self._t.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._stop.set()
        if self._t:
            self._t.join(timeout=5)


def load_idle(r: Remote) -> str:
    time.sleep(3.0)
    return "3s of nothing"


def load_tree(r: Remote) -> str:
    t = r.tree(scope="desktop")
    return f"desktop walk, {len(t.nodes)} nodes"


def load_shot(r: Remote) -> str:
    b = r.shot(bpp=24)
    return f"full-screen 24bpp capture, {len(b)} bytes"


def load_exec(r: Remote) -> str:
    res = r.exec(EXEC_CMD, timeout=120.0, shell=EXEC_SHELL)
    return (f"{EXEC_SHELL} {EXEC_CMD}, {res.bytes_produced} bytes"
            + (" (truncated)" if res.truncated else ""))


LOADS = {
    "idle": load_idle,
    "tree": load_tree,
    "shot": load_shot,
    "exec": load_exec,
}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--remote", required=True, metavar="HOST[:PORT]:SECRET")
    ap.add_argument("--load", action="append", default=[],
                    choices=sorted(LOADS), help="repeat for each load")
    ap.add_argument("--baseline", type=int, default=0, metavar="KB",
                    help="free KB with NOTHING of ours running, read at the "
                         "keyboard with PROBE.EXE platform. Cannot be taken "
                         "over the wire -- see the module docstring")
    a = ap.parse_args()

    bits = a.remote.split(":")
    if len(bits) == 2:
        host, secret, port = bits[0], bits[1], 7401
    elif len(bits) == 3:
        host, port, secret = bits[0], int(bits[1]), bits[2]
    else:
        sys.exit("memprobe: --remote wants HOST:SECRET or HOST:PORT:SECRET")

    loads = a.load or ["idle", "tree", "shot", "exec"]
    rows: list[tuple[str, str, int, int, int, int, int]] = []

    with Remote(host, secret, port=port) as r:
        si = r.sysinfo()
        print(f"machine: {si.mem_kb} KB total, {si.mem_free_kb} KB free now, "
              f"up {si.uptime_ms // 1000}s")
        running = [p.exe for p in r.ps()
                   if "GREMOTE" in p.exe.upper() or "GASTON" in p.exe.upper()]
        print(f"ours up: {running or 'none found'}")
        if a.baseline:
            print(f"baseline: {a.baseline} KB free with nothing of ours "
                  "running (keyboard reading)")
        print()

        for name in loads:
            before = r.sysinfo().mem_free_kb
            with Sampler(r) as s:
                detail = LOADS[name](r)
            after = r.sysinfo().mem_free_kb
            got = s.samples
            lo = min(got) if got else before
            med = round(statistics.median(got)) if got else before
            rows.append((name, detail, before, lo, med, after, len(got)))
            if s.errors:
                print(f"  {name}: sampler errors {s.errors[:2]}")

    print(f"{'load':<7}{'before':>8}{'min':>8}{'median':>8}{'after':>8}"
          f"{'n':>4}  what ran")
    for name, detail, before, lo, med, after, n in rows:
        print(f"{name:<7}{before:>8}{lo:>8}{med:>8}{after:>8}{n:>4}  {detail}")
    print()
    print("All figures are free KB (dwAvailPhys), which excludes the disk")
    print("cache Win98 shrinks under pressure -- BACKLOG 2. Read them against")
    print("each other, not as headroom.")
    if any(n <= 1 for *_, n in rows):
        print()
        print("NOTE: a load with n<=1 finished inside one sample interval, so")
        print("its 'min' is the before-reading and says nothing about a peak.")
        print("That is expected on a fast host, and it DOES happen on a P200:")
        print("measured 2026-10-23, a 28-node desktop walk on a quiet machine")
        print("finished inside one 0.2s interval. Read it as 'too quick to")
        print("sample', not as 'this box is fast'.")
    if a.baseline:
        print()
        idle_rows = [r_ for r_ in rows if r_[0] == "idle"]
        if idle_rows:
            idle_after = idle_rows[0][5]
            print(f"gremote idle costs about {a.baseline - idle_after} KB "
                  f"against the keyboard baseline ({a.baseline} -> "
                  f"{idle_after}), attributed by construction: it is the "
                  "difference between two states of one machine.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
