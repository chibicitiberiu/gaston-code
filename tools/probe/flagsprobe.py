"""Does -ot earn its bytes? The round trip that can answer, as a script.

docs/measurements/flags-vm-20261011.txt settled the whole-program question on
the QEMU guest and recorded the method in prose. Stage 5 asks the remaining
half on the real Pentium 200 -- whether -ot still earns its place in the three
modules it is kept for -- and the method had never been a tool, so it was done
by hand. A measurement done by hand once is a measurement nobody can repeat.

WHY NOT `probe deflate`. win/Makefile says it, and it is worth not
rediscovering: that section times 10-15 ms of work against GetTickCount's ~10
ms, so both builds measured 10-11 ms at level 1 and the difference lay inside
one tick. The round trip through gremote is the instrument that works, because
it moves enough bytes to be timed from the host with an ordinary clock.

THE PAYLOAD IS NOT ARBITRARY. 377,990 bytes of this project's own prose and
code, about 2.6:1 compressible, and **led by a NUL** -- that byte is what makes
the fs lane classify it binary, so the single-pass CRLF fold does not rewrite
it on the way through and both directions move exactly the same bytes. Without
it the comparison measures the fold as well as the codec.

THE GET IS THE MEASUREMENT. guest -> host means the guest COMPRESSES, and
deflate's search is the thing -ot buys; the put direction only inflates on the
guest, which is table lookups, and on the VM it never moved outside its own
spread. Both are reported anyway, because a put that DOES move is a sign the
run is contaminated.

A/B/A, NOT A/B. The first arm is measured again at the end. A single pass
cannot tell a regression from a drift: a 98 box warms its disk cache, and a
number that only gets measured once has no way to say which it was.

    .venv/bin/python tools/probe/flagsprobe.py \\
        --remote 10.0.0.125:7401:the-secret \\
        --arm head=build/win/gremote.exe \\
        --arm all-os=/tmp/allos/gremote.exe

Each arm is installed with gremote's own self_update, which needs GUPDATE.EXE
beside the running image on the box (tools/gen/mkw98kit.sh puts it on the
floppy). Every install is also one TIME_WAIT sample and one -verify sample --
GREMOTE.LOG's bind-retry line and GUPDATE.LOG's survival time -- so run this
with those logs fetched afterwards and BACKLOG 22's question answers itself
alongside.
"""
from __future__ import annotations

import argparse
import contextlib
import os
import statistics
import sys
import time
import zlib

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import toolkit

toolkit.on_path()

from gremote.client import Remote  # noqa: E402
from gremote.ship import self_update  # noqa: E402

#: What flags-vm-20261011.txt used. Kept exactly, so this run and that one are
#: the same experiment on two machines rather than two experiments.
PAYLOAD_BYTES = 377_990

#: Where the payload lives on the guest. C:\TEMP\GR is the only place a test
#: may write (docs/REMOTE.md, "Where things go on a target machine"), and it is
#: emptied at the end of the session that made it.
REMOTE_DIR = "C:\\TEMP\\GR"
REMOTE_PAYLOAD = REMOTE_DIR + "\\PAYLOAD.BIN"

#: The same explicit, sorted list idea as tools/gen/mkcorpus.py: a directory
#: walk would let a new file change the payload with nothing saying so.
SOURCES = [
    "docs/PLAN.md",
    "docs/PROGRESS.md",
    "docs/REMOTE.md",
    "docs/UIAUTOMATION.md",
    "docs/BACKLOG.md",
    "research/guest/probe.c",
    "win/automation/input.cpp",
    "win/remote/server.cpp",
    "win/ui/uiengine.cpp",
    "win/sync/scanner.cpp",
]


def payload(repo: str) -> bytes:
    """Exactly PAYLOAD_BYTES, deterministic, with a leading NUL.

    The NUL is load-bearing: it is what the fs lane's single-pass classifier
    reads as "binary", which keeps the EOL fold off the content. See the
    module docstring.
    """
    out = bytearray(b"\0")
    for rel in SOURCES:
        path = os.path.join(repo, rel)
        if not os.path.isfile(path):
            sys.exit(f"flagsprobe: {rel} is missing; the payload list is stale")
        with open(path, "rb") as fh:
            out += fh.read().replace(b"\r\n", b"\n")
        if len(out) >= PAYLOAD_BYTES:
            break
    if len(out) < PAYLOAD_BYTES:
        sys.exit(f"flagsprobe: the sources give only {len(out)} bytes of "
                 f"{PAYLOAD_BYTES}; add to SOURCES rather than padding, "
                 "because padding is compressible and would flatter the ratio")
    return bytes(out[:PAYLOAD_BYTES])


def median_ms(samples: list[float]) -> int:
    return round(statistics.median(samples) * 1000)


def measure(r: Remote, data: bytes, repeat: int) -> dict[str, object]:
    """`repeat` puts and `repeat` gets, timed from here, medians reported.

    Timed host-side on purpose: a guest-side clock on 9x is GetTickCount at
    ~55 ms, and the whole reason this instrument exists is that the work has
    to be bigger than the clock.
    """
    puts: list[float] = []
    gets: list[float] = []
    for _ in range(repeat):
        t0 = time.monotonic()
        r.put(REMOTE_PAYLOAD, data)
        puts.append(time.monotonic() - t0)

        t0 = time.monotonic()
        got = r.get(REMOTE_PAYLOAD, timeout=300.0).data
        gets.append(time.monotonic() - t0)
        # Checked every pass rather than once: a build that corrupts the codec
        # would otherwise be reported as a fast one.
        if got != data:
            sys.exit(f"flagsprobe: round trip differs "
                     f"({len(got)} bytes back, {len(data)} sent) -- stopping, "
                     "because a timing on wrong bytes is worth nothing")
    return {
        "put_ms": median_ms(puts),
        "get_ms": median_ms(gets),
        "put_all": [round(s * 1000) for s in puts],
        "get_all": [round(s * 1000) for s in gets],
    }


def install(r: Remote, label: str, exe: str) -> None:
    if not os.path.isfile(exe):
        sys.exit(f"flagsprobe: {exe} is missing (arm {label})")
    print(f"-- installing {label}: {exe}")
    res = self_update(r, exe)
    print(f"   {res}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--remote", required=True,
                    metavar="HOST[:PORT]:SECRET",
                    help="the box to drive, e.g. 10.0.0.125:7401:the-secret")
    ap.add_argument("--arm", action="append", default=[], metavar="NAME=EXE",
                    help="a build to measure; repeat for each arm")
    ap.add_argument("--repeat", type=int, default=5,
                    help="puts and gets per arm (default 5, as flags-vm did)")
    ap.add_argument("--no-reinstall-first", action="store_true",
                    help="skip the closing A of A/B/A. Only for a dry run: "
                         "without it a drift reads as a regression")
    a = ap.parse_args()

    arms: list[tuple[str, str]] = []
    for spec in a.arm:
        if "=" not in spec:
            sys.exit(f"flagsprobe: --arm wants NAME=EXE, got {spec!r}")
        name, _, exe = spec.partition("=")
        arms.append((name, exe))
    if not arms:
        sys.exit("flagsprobe: at least one --arm")

    bits = a.remote.split(":")
    if len(bits) == 2:
        host, secret, port = bits[0], bits[1], 7401
    elif len(bits) == 3:
        host, port, secret = bits[0], int(bits[1]), bits[2]
    else:
        sys.exit("flagsprobe: --remote wants HOST:SECRET or HOST:PORT:SECRET")

    data = payload(toolkit.REPO)
    ratio = len(data) / len(zlib.compress(data, 6))
    print(f"payload: {len(data)} bytes, {ratio:.2f}:1 at level 6, "
          f"leading byte 0x{data[0]:02x}")
    print(f"target : {host}:{port}, {a.repeat} puts and {a.repeat} gets per arm")
    print()

    results: list[tuple[str, dict[str, object]]] = []
    # A/B/A: the first arm again at the end, under its own label so the file
    # shows both readings rather than an average that hides the spread.
    order = list(arms)
    if not a.no_reinstall_first and len(arms) > 1:
        order.append((arms[0][0] + " (2)", arms[0][1]))

    for label, exe in order:
        with Remote(host, secret, port=port) as r:
            install(r, label, exe)
        # A fresh connection after the update: self_update restarts the
        # process, so the old socket is gone and the next one lands on the
        # new image. Reconnecting here also means the bind-retry this install
        # may have provoked is over before the clock starts.
        with Remote(host, secret, port=port) as r:
            with contextlib.suppress(Exception):
                r.mkdir(REMOTE_DIR)
            m = measure(r, data, a.repeat)
        results.append((label, m))
        print(f"   {label:<12} put {m['put_ms']:>5} ms   "
              f"get {m['get_ms']:>5} ms")
        print(f"   {'':<12} put {m['put_all']}")
        print(f"   {'':<12} get {m['get_all']}")
        print()

    print("=== medians ===")
    print(f"{'arm':<14}{'put ms':>8}{'get ms':>8}")
    for label, m in results:
        print(f"{label:<14}{m['put_ms']:>8}{m['get_ms']:>8}")
    print()
    print("The GET is the one to read: guest -> host, so the guest compresses,")
    print("and deflate's search is what -ot buys. A put that moves outside its")
    print("own spread means something else changed and the run is suspect.")

    with Remote(host, secret, port=port) as r:
        try:
            r.rm(REMOTE_PAYLOAD)
            print(f"\nremoved {REMOTE_PAYLOAD}")
        except Exception as e:
            print(f"\ncould not remove {REMOTE_PAYLOAD}: {e}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
