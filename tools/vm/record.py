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
import shutil
import subprocess
import sys
import tempfile
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


def capture(remote, seconds, every, crop, into):
    """Write one PNG per frame into `into`, and return how many.

    STRAIGHT TO DISK, AND NOT THROUGH PIL. `shot` already returns PNG
    bytes, so decoding each frame into an Image only to re-encode it later
    buys nothing and costs everything: an earlier version of this held
    every frame in memory and reached 12.7 GB resident on a five-minute
    capture before it was killed. Five minutes at 14 fps is four thousand
    frames, and 640x480x3 each is 3.7 GB of pixels before Python's
    overhead. On disk they are about 20 KB each.

    Captures as fast as the link allows and sleeps only the remainder, so
    a slow machine loses frame rate rather than duration.
    """
    started, n = time.time(), 0
    while time.time() - started < seconds:
        due = started + n * every
        png = (remote.shot(x=crop[0], y=crop[1],
                           w=crop[2] - crop[0], h=crop[3] - crop[1])
               if crop else remote.png())
        with open(os.path.join(into, "f%06d.png" % n), "wb") as f:
            f.write(png)
        n += 1
        behind = due + every - time.time()
        if behind > 0:
            time.sleep(behind)
    return n, time.time() - started


def encode(into, count, fps, out):
    """ffmpeg over the PNG sequence. It streams; nothing is held in RAM.

    GIF goes through palettegen/paletteuse rather than ffmpeg's default
    quantiser: a Win98 desktop is system greys plus one photograph, and
    the default palette dithers the greys into noise that doubles the
    file for a worse picture.
    """
    pattern = os.path.join(into, "f%06d.png")
    common = ["ffmpeg", "-y", "-loglevel", "error",
              "-framerate", "%.4f" % fps, "-i", pattern]
    if out.lower().endswith(".gif"):
        palette = os.path.join(into, "palette.png")
        subprocess.run([*common, "-vf", "palettegen=stats_mode=diff",
                                 palette], check=True)
        subprocess.run([*common, "-i", palette, "-lavfi",
                                 "paletteuse=dither=bayer:bayer_scale=5:"
                                 "diff_mode=rectangle", out], check=True)
    elif out.lower().endswith(".webp"):
        # Lossless because this is UI, not photography: lossy WebP rings
        # around 8pt text, which is most of a screen recording.
        subprocess.run([*common, "-loop", "0", "-lossless", "1",
                                 "-compression_level", "4", out], check=True)
    else:
        # yuv420p and an even size, or the file will not play in a browser.
        subprocess.run([*common, "-c:v", "libx264", "-pix_fmt", "yuv420p",
                                 "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2",
                                 "-movflags", "+faststart", out], check=True)


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

    if not shutil.which("ffmpeg"):
        raise SystemExit("record: needs ffmpeg on PATH to encode the frames")

    secret = secret_from(args.secret_file)
    work = tempfile.mkdtemp(prefix="gremote-rec-")
    try:
        with Remote(args.host, secret, port=args.port) as r:
            count, took = capture(r, args.seconds, 1.0 / args.fps, crop, work)
        if not count:
            raise SystemExit("record: no frames captured")
        rate = count / took
        os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
        # Play back at what was ACHIEVED times the speed asked for. A file
        # claiming 5fps over frames taken at 2 plays at double speed and
        # makes every pause look like a dropped frame.
        encode(work, count, rate * args.speed, args.out)
    finally:
        shutil.rmtree(work, ignore_errors=True)

    print("%s  --  %d frames, %.1f fps captured, %.1f fps playback, %d bytes"
          % (args.out, count, rate, rate * args.speed,
             os.path.getsize(args.out)))


if __name__ == "__main__":
    main()
