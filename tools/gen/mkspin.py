#!/usr/bin/env python3
"""Generate the transcript's spinner, win/resources/spin.gif.

An animated GIF rather than anything cleverer, because the transcript is
MSHTML on Internet Explorer 5 and that is the animation IE5 has. It also has
no support for `data:` URIs, so this cannot be inlined into the document --
it has to be a real file sitting beside view.htm, which is why
tools/gen/mktools-iso.sh ships it.

Eight frames of a rotating dot ring. Grey to match `.status` in the
stylesheet, on white to match the transcript background: no transparency,
because a transparent GIF over a white page buys nothing and GIF's
single-colour transparency is the classic source of a halo.

Run:  python3 tools/gen/mkspin.py   (writes win/resources/spin.gif)
"""

import math
import os
import sys                                                # noqa: E402
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import toolkit                                            # noqa: E402


from PIL import Image, ImageDraw

SIZE = 16          # what the <img> asks for; drawn 4x and downsampled
SCALE = 4
DOTS = 8
FRAMES = 8
BG = (255, 255, 255)


def frame(lead):
    """One frame, with `lead` naming which dot is darkest."""
    n = SIZE * SCALE
    im = Image.new("RGB", (n, n), BG)
    dr = ImageDraw.Draw(im)
    centre = n / 2.0
    radius = n * 0.34
    dot = n * 0.11

    for i in range(DOTS):
        # How far behind the leading dot this one is, 0 (brightest) to 1.
        age = ((i - lead) % DOTS) / float(DOTS)
        # 0x60 at the head fading to near the background at the tail. Stops
        # short of white so the ring never disappears entirely.
        level = int(0x60 + (0xD8 - 0x60) * age)
        angle = 2.0 * math.pi * i / DOTS - math.pi / 2.0
        x = centre + radius * math.cos(angle)
        y = centre + radius * math.sin(angle)
        dr.ellipse([x - dot, y - dot, x + dot, y + dot],
                   fill=(level, level, level))

    # Downsample for antialiasing we cannot get from the draw calls. The
    # result is 16x16 true colour; GIF quantises it to a palette on save.
    return im.resize((SIZE, SIZE), Image.LANCZOS)


def main():
    frames = [frame(i) for i in range(FRAMES)]
    top = toolkit.REPO
    path = os.path.join(top, "win", "resources", "spin.gif")
    frames[0].save(
        path, save_all=True, append_images=frames[1:],
        # 100ms a frame: one full rotation in 0.8s. Slower than a modern
        # spinner on purpose -- this runs on a P200 that is also rendering a
        # transcript, and a fast animation there reads as jitter.
        duration=100, loop=0, optimize=False)
    print("wrote %s (%d bytes, %d frames)"
          % (path, os.path.getsize(path), FRAMES))


if __name__ == "__main__":
    main()
