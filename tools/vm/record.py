#!/usr/bin/env python3
"""Record a Windows 9x machine's screen over gremote, as an animated GIF.

WHY A GIF AND NOT VIDEO. There is no capture hardware in the path and no
screen recorder that a 1998 machine can run without changing what it is
recording. What there is, is gremote's `shot`, which reads the screen out
of the machine over the network. Measured on the Pentium 200:

    full screen (800x600)      0.57 s per frame   1.7 fps
    quarter screen (400x300)   0.14 s per frame   7.1 fps

So the frame rate is bounded by how much screen is asked for, and a crop
around the window that matters is what makes this watchable. A GIF at 4-5
fps reads as a demo; the same frames at 1.7 fps read as a slideshow.

NOT A SUBSTITUTE FOR FILMING. The honest ways to capture a CRT are a
VGA-to-HDMI converter into a capture stick, which gives clean 60fps, or a
phone with its shutter matched to the monitor's refresh so the rolling
bands go away. This exists because neither needs to be bought before a
README can have a moving picture in it.

    python3 tools/vm/record.py --host 10.0.0.158 --secret-file vm/gremote.ini \\
        --seconds 30 --crop 120,60,760,580 --out tmp/demo.gif
"""

import argparse
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import toolkit

toolkit.on_path()

from gremote import Remote  # noqa: E402


def secret_from(path):
    """The secret out of a gremote.ini, so it is never on a command line."""
    with open(path, encoding="utf-8", errors="replace") as f:
        found = re.search(r"^\s*secret\s*=\s*(\S+)", f.read(), re.M)
    if not found:
        raise SystemExit("record: no secret= line in %s" % path)
    return found.group(1)


def frames(remote, seconds, every, crop):
    """Capture until the clock runs out, returning PIL images.

    Captures as fast as the link allows and sleeps only the remainder, so
    a slow machine degrades the frame rate rather than the duration.
    """
    import io

    from PIL import Image

    out, started, n = [], time.time(), 0
    while time.time() - started < seconds:
        due = started + n * every
        png = (remote.shot(x=crop[0], y=crop[1],
                           w=crop[2] - crop[0], h=crop[3] - crop[1])
               if crop else remote.png())
        out.append(Image.open(io.BytesIO(png)).convert("RGB"))
        n += 1
        behind = due + every - time.time()
        if behind > 0:
            time.sleep(behind)
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(prog="record.py", description=__doc__.split("\n")[0])
    ap.add_argument("--host", required=True)
    ap.add_argument("--port", type=int, default=7401)
    ap.add_argument("--secret-file", default="vm/gremote.ini",
                    help="an ini with a secret= line (default: vm/gremote.ini)")
    ap.add_argument("--seconds", type=float, default=20.0)
    ap.add_argument("--fps", type=float, default=5.0,
                    help="target; the link decides what is actually reached")
    ap.add_argument("--crop", default="",
                    help="x1,y1,x2,y2 -- a smaller box is a faster capture")
    ap.add_argument("--speed", type=float, default=1.0,
                    help="playback multiplier; 5 turns four minutes of "
                         "building into a clip somebody will watch")
    ap.add_argument("--out", default="tmp/demo.gif",
                    help="extension picks the format: .gif or .webp")
    args = ap.parse_args(argv)

    crop = [int(v) for v in args.crop.split(",")] if args.crop else None
    if crop and len(crop) != 4:
        raise SystemExit("record: --crop takes x1,y1,x2,y2")

    secret = secret_from(args.secret_file)
    with Remote(args.host, secret, port=args.port) as r:
        started = time.time()
        shots = frames(r, args.seconds, 1.0 / args.fps, crop)
    took = time.time() - started

    if not shots:
        raise SystemExit("record: no frames captured")

    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    rate = len(shots) / took
    # The GIF plays at what was ACHIEVED, not what was asked for: a file
    # whose frame delay claims 5fps while the frames were taken at 2 runs
    # at double speed and makes every pause look like a dropped frame.
    delay = max(20, round(1000.0 / (rate * args.speed)))
    if args.out.lower().endswith(".webp"):
        # Animated WebP renders in GitHub markdown through a plain
        # `![](x.webp)` and keeps every colour, where a GIF has 256 and
        # dithers the rest. Lossless because this is UI, not photography:
        # lossy WebP puts ringing around 8pt text, which is most of what a
        # screen recording of a text editor contains.
        shots[0].save(args.out, save_all=True, append_images=shots[1:],
                      duration=delay, loop=0, lossless=True, method=4)
    else:
        # 256 colours, and an adaptive palette rather than the web one: a
        # Win98 desktop is mostly system greys plus one photograph, and the
        # fixed palette dithers the greys into noise that triples the size.
        quantized = [im.quantize(colors=256, method=2) for im in shots]
        quantized[0].save(args.out, save_all=True,
                          append_images=quantized[1:],
                          duration=delay, loop=0, optimize=True)

    print("%s  --  %d frames, %.1f fps captured, %d ms/frame playback, "
          "%d bytes" % (args.out, len(shots), rate, delay,
                        os.path.getsize(args.out)))


if __name__ == "__main__":
    main()
