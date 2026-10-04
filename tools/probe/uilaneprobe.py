"""REMOTE.md step 6's verification list, end to end, against a real guest.

Unlike every other probe here this one is NOT a bridge stub: it needs no
client and no bridge, because that is the point of the lane. It drives
gremote directly.

    python3 -u tools/probe/uilaneprobe.py --remote 127.0.0.1:vm-secret

It kills any NOTEPAD or CALC it finds and opens one Notepad of its own --
exactly one, because a desktop with two of anything makes every selector
ambiguous, which is the engine being right and the fixture being wrong. Not
for a machine somebody is using.
"""
import contextlib
import sys
import threading
import time

sys.path.insert(0, "/home/tibi/Dev/RetroAI")
import argparse

from gremote import Remote
from gremote.errors import Cancelled, Refused, RemoteError

ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
ap.add_argument("--remote", default="127.0.0.1:vm-secret",
                help="host:secret of the gremote to drive")
ap.add_argument("--port", type=int, default=7401)
_a = ap.parse_args()
P, SECRET = _a.remote.rsplit(":", 1)
fails = 0
def say(n, ok, detail=""):
    global fails
    print("[%s] %-46s %s" % ("PASS" if ok else "FAIL", n, detail))
    if not ok:
        fails += 1

with Remote(P, SECRET, port=_a.port) as r:
    print("HELLO", {k: r.hello[k] for k in
                    ("machine","automation","keys","msaa","deflate","os")})

    # 1. a tree of the desktop, of one process, and one control's items
    t = r.tree(max_nodes=400)
    # Not "more than 20": the Athlon's whole desktop is 11 nodes (taskbar,
    # tray, Progman), and the probe was failing a machine for being tidy.
    say("1a tree of the desktop", t.walk == "complete" and len(t.nodes) >= 3,
        "%d nodes, %d selectors" % (len(t.nodes), len(t.selectors)))
    pid = next((p.pid for p in r.ps() if p.exe.upper().endswith("EXPLORER.EXE")), 0)
    tp = r.tree("pid", pid=pid, max_nodes=400)
    # <=, not <: when every window on the desktop is Explorer's -- the
    # same tidy machine -- the process tree IS the desktop tree.
    # `<=` and not `<`: on a freshly booted box every visible window can
    # belong to Explorer, and then the filtered walk IS the whole walk.
    say("1b tree of one process", 0 < len(tp.nodes) <= len(t.nodes),
        "explorer pid %x -> %d of %d" % (pid, len(tp.nodes), len(t.nodes)))

    # ...which on its own cannot tell a filter that worked from one that was
    # IGNORED, and `scope=pid` was silently returning the whole desktop until
    # 2026-10-03 (a 9x pid has the high bit set and no signed clamp carries
    # it). gremote's own pid is the discriminator: every walk excludes this
    # process, so a filter that is applied returns nothing and a filter that
    # is dropped returns everything. A node record carries no pid of its own
    # -- no handle crosses this wire -- so the count is the evidence.
    me = next(p.pid for p in r.ps() if p.exe.upper().endswith("GREMOTE.EXE"))
    mine = r.tree("pid", pid=me, max_nodes=400)
    say("1c the pid filter is applied, not dropped", len(mine.nodes) == 0,
        "our own pid %x -> %d nodes (the desktop has %d)"
        % (me, len(mine.nodes), len(t.nodes)))
    with contextlib.suppress(Refused):
        r.stream("TREE", {"inproc": "1", "target": "Progman"})
    try:
        r.stream("TREE", {"inproc": "1", "target": "Progman"})
        say("1d inproc refused, not served", False, "it answered")
    except Refused as e:
        say("1d inproc refused, not served", e.code == "inproc_unsupported", e.code)

    # 2. a real step list: open Notepad, type, read it back.
    #    Exactly ONE Notepad, killed by pid rather than closed by selector --
    #    two of them make every selector below ambiguous, which is the
    #    engine being right and the fixture being wrong.
    for p in r.ps():
        if p.exe.upper().endswith(("NOTEPAD.EXE", "CALC.EXE")):
            with contextlib.suppress(Refused):
                r.kill(p.pid)
    time.sleep(1.0)
    r.launch(r"C:\WINDOWS\NOTEPAD.EXE")
    time.sleep(2.0)
    run = r.run([{"focus": "Notepad"},
                 "hello from gremote",
                 {"wait_text": "Notepad Edit", "text": "hello from gremote",
                  "timeout": 4000}])
    say("2  focus, type and read back in one list", run.ok,
        "%d steps, last=%s" % (len(run.steps), run.steps[-1].status if run.steps else "-"))

    # 3. a list that fails mid-way still reports what ran
    run = r.run([{"focus": "Notepad"},
                 {"wait_for": "NoSuchThing", "timeout": 1200}])
    say("3  a failing list reports the steps that ran",
        not run.ok and len(run.steps) == 2 and run.steps[0].status == "ok",
        "status=%s steps=%s" % (run.status, [s.status for s in run.steps]))

    # 4. a list that leaves something held releases it
    # The compiler refuses a list that LEAVES something held, so the only
    # way to reach the runner's release is a balanced list that fails
    # between the down and the up.
    run = r.run([{"focus": "Notepad"}, {"down": "Shift"},
                 {"wait_for": "NoSuchThing", "timeout": 1000},
                 {"up": "Shift"}])
    say("4  nothing stays held after a failure", run.released and not run.ok,
        "released=%s status=%s" % (run.released, run.status))

    # 5. a malformed list is refused before anything is sent
    try:
        r.run([{"wiggle": "Notepad"}])
        say("5  a list that cannot run never leaves here", False)
    except Exception as e:
        say("5  a list that cannot run never leaves here",
            "step 1" in str(e), str(e)[:60])

    # 6. screenshots: whole screen and one window's rect, as PNG
    full = r.shot()
    png = r.png(w=200, h=120)
    say("6  a screenshot of the screen and of a rect",
        full[:2] == b"BM" and png[:4] == b"\x89PNG",
        "%d bytes BMP, %d bytes PNG" % (len(full), len(png)))

    # 7. the ceiling fires while the frames arrive
    try:
        r.shot(max_bytes=4096)
        say("7  a shot over the ceiling is stopped", False, "it all arrived")
    except RemoteError as e:
        say("7  a shot over the ceiling is stopped", "more than" in str(e), str(e)[:50])

    # 8. STOP answers, CANCEL does not
    def long_wait(ids, out):
        """A list that waits for something that never comes, so there is
        always something in flight to stop."""
        try:
            out.append(r.run([{"wait_for": "NeverEver", "timeout": 20000}],
                             timeout_ms=25000, timeout=40,
                             on_id=ids.append))
        except Cancelled:
            out.append("silence")

    for stopper, want in ((r.stop, "cancelled"), (r.cancel, None)):
        ids: list = []
        out: list = []
        th = threading.Thread(target=long_wait, args=(ids, out))
        th.start()
        while not ids:
            time.sleep(0.02)
        time.sleep(0.5)
        stopper(ids[0])
        th.join(30)
        got = out[0] if out else None
        ok = (getattr(got, "status", None) == want) if want else (got == "silence")
        say("8%s %s" % ("a" if want else "b",
                        "STOP: the target still answers" if want
                        else "CANCEL: the target never answers"),
            ok, str(getattr(got, "status", got))[:40])

    # 9. automation read live: the tray can turn it off mid-session
    say("9  automation is declared", r.hello["automation"] == "2",
        r.hello["automation"])

    # 10. a large GET beside the heartbeat
    t0 = time.time()
    big = r.get(r"C:\WINDOWS\SYSTEM\SHELL32.DLL", max_bytes=2 * 1024 * 1024)
    r.ping()
    say("10 a large GET, and a PING answered after it",
        (len(big.data) > 500000 and not big.truncated) or big.truncated,
        "%d bytes in %.1fs" % (len(big.data), time.time() - t0))

    # 11. the box still answers after all of it, and says what it has left.
    #     Not a threshold: free PHYSICAL memory on a 32MB 98 box is whatever
    #     is not paged out, and a small number is the OS doing its job.
    info = r.sysinfo()
    r.ping()
    say("11 the box still answers afterwards", True,
        "%d KB physical free of %d" % (info.mem_free_kb, info.mem_kb))

    # By pid, like the start: `close` stops at Notepad's own "save
    # changes?" prompt, and the probe used to leave both on the screen.
    for p in r.ps():
        if p.exe.upper().endswith("NOTEPAD.EXE"):
            with contextlib.suppress(Refused):
                r.kill(p.pid)

print("\n%s" % ("ALL PASS" if not fails else "%d FAILED" % fails))
sys.exit(1 if fails else 0)
