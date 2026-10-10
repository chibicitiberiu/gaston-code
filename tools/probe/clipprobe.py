"""The clipboard, both directions, against a real guest.

Like uilaneprobe.py and unlike every other probe here, this needs no client
and no bridge: it drives gremote directly, which is the point of the lane.

    python3 -u tools/probe/clipprobe.py --remote 127.0.0.1:vm-secret

Wine answers every case in uicheck and tests/python/remote, round trip
included. What only a real 98 settles is whether CF_TEXT means what the code
assumes when the program on the other side of it is Notepad rather than
ourselves -- and the first run of this list found that it does, while the
advice the tool was giving about how to fill the clipboard did not work on
the machine it was written for (docs/measurements/clipboard-vm-20261010.txt).

It closes any Notepad it finds and opens one of its own: `Notepad` as a
selector matches every instance, so a desktop with two of them makes every
step here `ambiguous` -- the engine being right and the fixture being wrong.
Not for a machine somebody is using.
"""
import sys
import time

sys.path.insert(0, "/home/tibi/Dev/RetroAI")
import argparse

from gremote import Remote
from gremote.errors import Refused

ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
ap.add_argument("--remote", default="127.0.0.1:vm-secret",
                help="host:secret of the gremote to drive")
ap.add_argument("--port", type=int, default=7401)
_a = ap.parse_args()
P, SECRET = _a.remote.rsplit(":", 1)
fails = 0


def say(n, ok, detail=""):
    global fails
    print("[%s] %-44s %s" % ("PASS" if ok else "FAIL", n, detail))
    if not ok:
        fails += 1


def notepads(r):
    t = r.tree(max_nodes=250)
    return [s for n, s in zip(t.nodes, t.selectors, strict=True)
            if n.cls == "Notepad"]


def edit_text(r):
    return next((n.text for n in r.tree(max_nodes=250).nodes
                 if n.cls == "Edit"), None)


with Remote(P, SECRET, port=_a.port) as r:
    print("HELLO", {k: r.hello.get(k) for k in
                    ("machine", "os", "automation", "clip")})
    if r.hello.get("clip") != "1":
        print("this machine does not declare the clipboard op")
        sys.exit(2)

    # 1. Whatever is there now. A machine that has copied nothing has no
    #    CF_TEXT at all, which is its own answer and not an empty string.
    try:
        got = r.clipboard()
        say("reads-whatever-is-there", True, "%d bytes" % len(got))
    except Refused as e:
        say("reads-whatever-is-there", e.code == "no_clip_text", e.code)

    for sel in notepads(r):
        r.run([{"close": sel}])
        time.sleep(1)
        r.run(["[Alt+N]"])
        time.sleep(1)

    r.launch("notepad.exe")
    time.sleep(3)
    pads = notepads(r)
    say("one-notepad-to-work-with", len(pads) == 1, str(len(pads)))
    pad = pads[0] if pads else "Notepad"

    # 2. A write from here reaches a program that knows nothing about us.
    written = "the bridge put this here"
    r.clipboard(written)
    r.run([{"focus": pad}, "[Ctrl+V]"])
    time.sleep(1)
    held = edit_text(r) or ""
    say("a-write-lands-in-another-program", written in held, repr(held[:40]))

    # 3. And a read sees what that program copied. NOT [Ctrl+A]: on Win98's
    #    Notepad it selects nothing, and copying an empty selection leaves
    #    the clipboard alone while reporting success -- which reads from here
    #    exactly like a broken copy. Measured 2026-10-10.
    typed = "copied by notepad itself"
    r.clipboard("--- marker ---")
    time.sleep(0.5)
    r.run([{"focus": pad}, "[Ctrl+Home]", "[Ctrl+Shift+End]", typed])
    time.sleep(1)
    r.run([{"focus": pad}, "[Ctrl+Home]", "[Ctrl+Shift+End]", "[Ctrl+C]"])
    time.sleep(2)
    back = r.clipboard()
    say("a-read-sees-what-it-copied", back.strip() == typed, repr(back[:50]))

    # 4. The platform fact that cost an hour, pinned so it stays pinned.
    r.run([{"focus": pad}, "[Ctrl+Home]", "[Ctrl+Shift+End]", "BASE"])
    time.sleep(1)
    r.run([{"focus": pad}, "[Ctrl+A]"])
    time.sleep(1)
    r.run([{"focus": pad}, "X"])
    time.sleep(1)
    after = edit_text(r) or ""
    say("ctrl-a-selects-nothing-on-this-notepad", after == "BASEX",
        repr(after) + "  (appended, so nothing was selected)")

    # 5. Refused rather than clipped: a document that does not fit is not
    #    silently made into a different document.
    try:
        r.clipboard("x" * 60000)
        say("too-big-is-refused", False, "60000 bytes was accepted")
    except Refused as e:
        say("too-big-is-refused", e.code == "too_big", e.code)

    # 6. And a character the machine's codepage cannot carry is refused on
    #    this side, before the wire.
    try:
        r.clipboard("box drawing ─ here")
        say("unencodable-is-refused-here", False, "accepted")
    except UnicodeEncodeError:
        say("unencodable-is-refused-here", True, "UnicodeEncodeError")

    r.run([{"close": pad}])
    time.sleep(2)
    r.run(["[Alt+N]"])
    time.sleep(1)
    say("tidied-up", not notepads(r), "no Notepad left behind")

print("\n%s" % ("all passed" if not fails else "%d FAILED" % fails))
sys.exit(1 if fails else 0)
