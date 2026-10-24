"""Watch gremote's tray lines while somebody works the keyboard.

notes/stage5-plan.md's K2 is a sitting at the machine: the icon is a
judgement, the standby timer needs a real idle box, and Explorer has to be
ended by hand -- BACKLOG 40 is what happened when that last one was tried
over the wire instead. None of that is drivable. What IS drivable is the
reading: every one of K2's answers lands in GREMOTE.LOG as a `tray:` line,
and this tails them as they appear so the person at the keyboard does not
have to fetch a log, and so nothing is missed between steps.

It also prints the notification area as a cropped image after every change,
because the other half of K2 is "does the icon look like anything" and that
is a picture, not a line.

WHAT TO DO AT THE KEYBOARD, and the line each step should produce:

  1. Stop gremote from its tray menu, run `PROBE.EXE platform`, read
     `physical memory: N KB free`. That is the no-gremote baseline, the one
     figure that cannot be taken over the wire -- asking gremote how much
     memory is free without gremote requires gremote not to be running.
     Start gremote again and this tool reconnects.
  2. Ctrl+Alt+Del, End Task on Explorer, then restart it.
         tray: the taskbar appeared; adding the icon
  3. Right-click the icon, then click somewhere else.
     No line; the check is that the menu goes away.
  4. Set prevent_sleep=0 in GREMOTE.INI, restart gremote, set the shortest
     standby, leave it alone.
         tray: the machine asks to suspend (...); Prevent sleep is off
     SILENCE HERE IS THE FINDING: if the box suspends without asking, no
     veto can ever help and the whole feature is moot on this hardware.
  5. Tray menu -> Prevent sleep. The checkmark and `[awake]` appear, and no
     restart is needed because the menu is deliberately not persisted.
         tray: Prevent sleep ON (...)
  6. Leave it idle again.
         tray: the machine asks to suspend (...); REFUSED (N so far)
         tray: the suspend was abandoned, so a refusal took (N ...)
     NOTE: win/remote/tray.cpp logs the FIRST of each kind and then every
     EIGHTH. A reading that expects one line per ask will under-count.
  7. Start > Shut Down > Standby, for 9x's own complaint.
  8. Tray menu -> Exit.
         tray: Prevent sleep OFF (...); N suspend requests refused
     This is the line the Wine suite structurally cannot see. gremote is
     gone afterwards and only a reboot brings it back (RunServices runs at
     boot), so do it last.

    .venv/bin/python research/probes/trayprobe.py \\
        --remote 10.0.0.158:7401:the-secret --minutes 45
"""
from __future__ import annotations

import argparse
import io
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))), "tools"))
import toolkit

toolkit.on_path()

from gremote.client import Remote  # noqa: E402

LOG = "C:\\GREMOTE\\gremote.log"

#: Long enough that a step at the keyboard is not a race, short enough that
#: a line appears while the person who caused it is still looking.
POLL_S = 3.0

#: HOW LONG A POLL MAY BLOCK, and it has to be set rather than defaulted.
#: `Remote.get` defaults to 120 s, which is right for fetching a file and
#: badly wrong for a liveness poll: a peer that vanishes WITHOUT sending a
#: RST -- a VM destroyed, a machine switched off at the wall, exactly what
#: step 8's tray Exit looks like from here -- leaves the read blocked for the
#: full two minutes, so the tool goes silent for two minutes at the moment it
#: is supposed to say "gremote is gone".
#:
#: Measured, not guessed: rehearsed against the QEMU guest by destroying the
#: domain mid-run, and at the default the disconnect line had still not
#: appeared after 110 seconds. GREMOTE.LOG is a few KB, so fifteen seconds is
#: generous even on a 200 MHz machine off a 1998 platter.
GET_S = 15.0


def tray_lines(r: Remote) -> list[str]:
    """The `tray:` lines in GREMOTE.LOG.

    LETS THE EXCEPTION OUT, and that is the whole point. This swallowed it
    and returned [] in the first version, which made the tool silently
    useless in the one case it was written for: gremote going away. The
    reader would then see `[][len(seen):]`, which is [], decide nothing had
    changed, and spin -- never printing the "gone" line, never reconnecting,
    and never saying a word for the rest of the sitting. Caught by rehearsing
    it against the QEMU guest and pulling the port forward out from under it,
    which is the only reason it did not happen on the real box.
    """
    return [ln for ln in r.get(LOG, timeout=GET_S).data.decode("latin-1")
            .splitlines() if "tray:" in ln]


def save_tray_crop(r: Remote, out: str, tag: str) -> str | None:
    """The notification area, scaled up. Returns the path, or None."""
    try:
        from PIL import Image
    except ImportError:
        return None
    try:
        im = Image.open(io.BytesIO(r.png())).convert("RGB")
    except Exception:
        return None
    w, h = im.size
    # The taskbar's right-hand end. Generous, because the number of icons
    # changes as Explorer restarts and this must not crop ours out.
    box = (max(0, w - 240), max(0, h - 28), w, h)
    path = os.path.join(out, f"tray-{tag}.png")
    im.crop(box).resize(((box[2] - box[0]) * 8, (box[3] - box[1]) * 8),
                        Image.NEAREST).save(path)
    return path


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--remote", required=True, metavar="HOST[:PORT]:SECRET")
    ap.add_argument("--minutes", type=float, default=45.0)
    ap.add_argument("--out", default=".", help="where tray crops are written")
    a = ap.parse_args()

    bits = a.remote.split(":")
    if len(bits) == 2:
        host, secret, port = bits[0], bits[1], 7401
    elif len(bits) == 3:
        host, port, secret = bits[0], int(bits[1]), bits[2]
    else:
        sys.exit("trayprobe: --remote wants HOST:SECRET or HOST:PORT:SECRET")

    print(__doc__.split("WHAT TO DO AT THE KEYBOARD")[1].split('    .venv')[0])
    print("=" * 68)

    seen: list[str] = []
    deadline = time.monotonic() + a.minutes * 60
    shots = 0
    connected = False

    while time.monotonic() < deadline:
        try:
            with Remote(host, secret, port=port) as r:
                if not connected:
                    print(f"[connected] uptime {r.sysinfo().uptime_ms // 1000}s")
                    connected = True
                    seen = tray_lines(r)
                    for ln in seen[-4:]:
                        print("  (already in the log)", ln)
                    p = save_tray_crop(r, a.out, "start")
                    if p:
                        print(f"  notification area -> {p}")
                while time.monotonic() < deadline:
                    # Any failure here leaves the `with`, so the outer loop
                    # reconnects. That is the recovery path for steps 1 and
                    # 8, which both take gremote away on purpose.
                    now = tray_lines(r)
                    fresh = now[len(seen):]
                    if fresh:
                        for ln in fresh:
                            print(">>", ln, flush=True)
                        seen = now
                        shots += 1
                        p = save_tray_crop(r, a.out, f"{shots:02d}")
                        if p:
                            print(f"   notification area -> {p}", flush=True)
                    time.sleep(POLL_S)
        except Exception as e:
            # gremote going away is EXPECTED here -- step 1 stops it and
            # step 8 exits it -- so this says so rather than failing.
            if connected:
                print(f"[gremote is gone: {type(e).__name__}] waiting; this is "
                      "expected after a tray Exit or a restart", flush=True)
                connected = False
            time.sleep(POLL_S)
            continue

    print("=" * 68)
    print(f"{len(seen)} tray lines in the log at the end. The ones K2 wants:")
    for want in ("the taskbar appeared", "asks to suspend", "REFUSED",
                 "suspend was abandoned", "Prevent sleep ON",
                 "Prevent sleep OFF", "no notification area"):
        hit = [ln for ln in seen if want in ln]
        print(f"  {want:<26} {len(hit)}")
        for ln in hit[:3]:
            print(f"      {ln}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
