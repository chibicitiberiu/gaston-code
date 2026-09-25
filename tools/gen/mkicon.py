#!/usr/bin/env python3
"""Generate win/resources/gaston.ico -- the application icon.

Why a generator rather than a checked-in blob nobody can edit: the art below
is the source. A .ico is a container, and the one thing that matters about it
here is not visible from the file -- it is **4 bits per pixel with the
standard 16-colour VGA palette**, which is what Windows 98 expects.

A modern tool (PIL, ImageMagick, anything that says "save as .ico") writes a
32bpp BGRA image with an alpha channel. That is an XP-era format: Win98's
shell has no alpha compositing, so a 32bpp icon renders with its alpha byte
interpreted as nothing at all -- the transparent margin comes out black, and
on the taskbar that is a black square. The AND mask below is the only
transparency Win98 understands.

The palette is pinned to the 16 VGA colours for the second reason: the guest
may be running an 8bpp display, where a 4bpp icon carrying its own arbitrary
palette gets colour-matched to the system halftone palette and shifts. These
sixteen are in the system palette at every depth, so they map exactly.

Run:  python3 tools/gen/mkicon.py   (writes win/resources/gaston.ico)
"""

import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import toolkit

# The Windows 16-colour palette, in its canonical index order.
PALETTE = [
    (0x00, 0x00, 0x00),  # 0  k  black
    (0x80, 0x00, 0x00),  # 1  m  maroon
    (0x00, 0x80, 0x00),  # 2  e  green
    (0x80, 0x80, 0x00),  # 3  o  olive
    (0x00, 0x00, 0x80),  # 4  n  navy
    (0x80, 0x00, 0x80),  # 5  p  purple
    (0x00, 0x80, 0x80),  # 6  t  teal
    (0xC0, 0xC0, 0xC0),  # 7  s  silver
    (0x80, 0x80, 0x80),  # 8  d  gray
    (0xFF, 0x00, 0x00),  # 9  r  red
    (0x00, 0xFF, 0x00),  # 10 l  lime
    (0xFF, 0xFF, 0x00),  # 11 y  yellow
    (0x00, 0x00, 0xFF),  # 12 b  blue
    (0xFF, 0x00, 0xFF),  # 13 f  fuchsia
    (0x00, 0xFF, 0xFF),  # 14 a  aqua
    (0xFF, 0xFF, 0xFF),  # 15 w  white
]

CH = {"k": 0, "m": 1, "e": 2, "o": 3, "n": 4, "p": 5, "t": 6, "s": 7,
      "d": 8, "r": 9, "l": 10, "y": 11, "b": 12, "f": 13, "a": 14, "w": 15}

TRANSPARENT = None


class Canvas:
    def __init__(self, w, h):
        self.w = w
        self.h = h
        self.px = [[TRANSPARENT] * w for _ in range(h)]

    def put(self, x, y, c):
        if 0 <= x < self.w and 0 <= y < self.h:
            self.px[y][x] = CH[c] if isinstance(c, str) else c

    def rect(self, x0, y0, x1, y1, c):
        """Filled, inclusive of both corners."""
        for y in range(y0, y1 + 1):
            for x in range(x0, x1 + 1):
                self.put(x, y, c)

    def frame(self, x0, y0, x1, y1, c):
        """One-pixel outline, inclusive."""
        for x in range(x0, x1 + 1):
            self.put(x, y0, c)
            self.put(x, y1, c)
        for y in range(y0, y1 + 1):
            self.put(x0, y, c)
            self.put(x1, y, c)

    def sprite(self, x0, y0, rows):
        """Stamp an art block. '.' leaves the pixel underneath alone."""
        for dy, row in enumerate(rows):
            for dx, ch in enumerate(row):
                if ch != ".":
                    self.put(x0 + dx, y0 + dy, ch)


# A speech bubble, 18x11, for the 32x32 screen. Three dots because the shape
# has to survive being 22 pixels wide: an ellipsis reads as "a conversation"
# where any glyph of actual text would just be noise at this size.
BUBBLE32 = [
    "..wwwwwwwwwwwwww..",
    ".wwwwwwwwwwwwwwww.",
    "wwwwwwwwwwwwwwwwww",
    "wwkkwwwwkkwwwwkkww",
    "wwkkwwwwkkwwwwkkww",
    "wwwwwwwwwwwwwwwwww",
    "wwwwwwwwwwwwwwwwww",
    ".wwwwwwwwwwwwwwww.",
    "..wwwwwwwwwwwwww..",
    "..wwww............",
    "..ww..............",
]

# At 16x16 the bubble is gone. The screen's usable interior is 10x5 pixels,
# and a bubble drawn to fit it has no black margin left to be a bubble
# against -- rendered, it reads as a face. What survives the size is the
# ellipsis alone, lit on a dark screen: the same "a conversation is
# happening" and none of the shape that cannot be drawn.
DOTS16 = [
    "wwkwwkww",
    "wwkwwkww",
]


def art32():
    """A CRT on its stand, with a chat bubble on the screen.

    Silver rather than the beige a real 1998 monitor was: beige is not in the
    16 colours, and C0C0C0 is what Win95's own My Computer icon uses for the
    same object -- so it reads as "a PC" to the eye the icon is aimed at.
    """
    c = Canvas(32, 32)
    # Chassis: black outline, silver body, lit from the top left.
    c.rect(1, 3, 30, 23, "k")
    c.rect(2, 4, 29, 22, "s")
    c.rect(2, 4, 29, 4, "w")
    c.rect(2, 4, 2, 22, "w")
    c.rect(29, 5, 29, 22, "d")
    c.rect(3, 22, 29, 22, "d")
    # Screen, sunken: dark on the top and left, light on the bottom and right.
    c.rect(4, 7, 27, 7, "d")
    c.rect(4, 7, 4, 19, "d")
    c.rect(5, 19, 27, 19, "w")
    c.rect(27, 8, 27, 19, "w")
    c.rect(5, 8, 26, 18, "k")
    c.sprite(7, 8, BUBBLE32)
    # Power LED. Two pixels of lime, and the only saturated colour in the
    # icon -- which is what makes the machine read as switched on.
    c.rect(25, 21, 26, 21, "l")
    # Neck and base.
    c.rect(12, 24, 19, 25, "k")
    c.rect(13, 24, 18, 24, "s")
    c.rect(13, 25, 18, 25, "d")
    c.rect(8, 26, 23, 28, "k")
    c.rect(9, 27, 22, 27, "s")
    return c


def art16():
    """The same object with every optional pixel removed."""
    c = Canvas(16, 16)
    c.rect(0, 1, 15, 11, "k")
    c.rect(1, 2, 14, 10, "s")
    c.rect(1, 2, 14, 2, "w")
    c.rect(1, 2, 1, 10, "w")
    c.rect(14, 3, 14, 10, "d")
    c.rect(2, 10, 14, 10, "d")
    c.rect(2, 3, 13, 3, "d")
    c.rect(2, 3, 2, 9, "d")
    c.rect(3, 9, 13, 9, "w")
    c.rect(13, 4, 13, 9, "w")
    c.rect(3, 4, 12, 8, "k")
    c.sprite(4, 5, DOTS16)
    # No LED at this size: one lime pixel next to a silver one is a speck.
    c.rect(6, 12, 9, 12, "k")
    c.rect(7, 12, 8, 12, "d")
    c.rect(4, 13, 11, 14, "k")
    c.rect(5, 13, 10, 13, "s")
    return c


#: The status bar's connection light, three states. A small server box with
#: an LED, which is what the user described: "server icon with green dot =
#: ok, orange or red = not ok". The box is what makes it read as a machine
#: rather than a generic dot; the LED is what carries the state.
#:
#: 16x16 only -- the status bar asks for SM_CXSMICON and nothing else ever
#: shows these.
_NET = [
    "................",
    "..kkkkkkkkkkkk..",
    "..kwwwwwwwwwwsk.",
    "..kwkkkkkkkkwsk.",
    "..kwkssssssdwsk.",
    "..kwkssssssdwsk.",
    "..kwkddddddd wsk",
    "..kwwwwwwwwwwsk.",
    "..kw@@wwwwwwwsk.",
    "..kwwwwwwwwwwsk.",
    "..kwkkkkkkkkwsk.",
    "..kwkssssssdwsk.",
    "..kwkddddddd wsk",
    "..kwwwwwwwwwwsk.",
    "..kssssssssssk..",
    "..kkkkkkkkkkkk..",
]


def art_net(led):
    """The connection light. `led` is the palette char for the LED."""
    c = Canvas(16, 16)
    for y, row in enumerate(_NET):
        for x, ch in enumerate(row):
            if ch in (".", " "):
                continue
            c.put(x, y, led if ch == "@" else ch)
    # Two pixels of LED, the only saturated colour in the glyph.
    return c


#: How many pie frames. The user asked for "~8 frames for it to be good
#: enough" and that is about right: at 16x16 there are not enough pixels to
#: distinguish more, and a status indicator that moves in visible steps reads
#: better than one that creeps.
PIE_STEPS = 8


def art_pie(filled):
    """A context-usage pie, `filled` eighths of it.

    Drawn by testing each pixel rather than by an arc primitive, because
    there is no arc primitive here and because at this size the only thing
    that matters is which pixels are in. Clockwise from twelve o'clock, the
    direction every gauge in the world fills.
    """
    import math
    c = Canvas(16, 16)
    cx, cy, r = 7.5, 7.5, 6.5
    sweep = 2.0 * math.pi * (float(filled) / PIE_STEPS)

    for y in range(16):
        for x in range(16):
            dx, dy = x - cx, y - cy
            d = math.hypot(dx, dy)
            if d > r:
                continue
            if d > r - 1.2:
                c.put(x, y, "k")            # rim, always drawn
                continue
            if filled <= 0:
                c.put(x, y, "w")
                continue
            # atan2 with the axes arranged so 0 is up and angle grows
            # clockwise, which is what "filled" means to a reader.
            ang = math.atan2(dx, -dy)
            if ang < 0:
                ang += 2.0 * math.pi
            # Past three quarters the slice is the warning, not the news.
            c.put(x, y, ("r" if filled >= 7 else "n") if ang <= sweep else "w")
    return c


#: The Attach paperclip, 16x16, for the toolbar.
#:
#: DRAWN, because comctl32 does not have one. IDB_STD_SMALL_COLOR is New,
#: Open, Save, Cut, Copy, Paste, Undo, Redo, Delete, Properties, Find,
#: Replace, Print and Help -- a fifteen-button office toolbar from 1995 with
#: no paperclip in it, and STD_FILEOPEN standing in for "attach" reads as
#: "open a document", because that is what it is.
#:
#: TWO NESTED U SHAPES, OPEN AT THE TOP, joined by a bend over the right.
#: The first two attempts drew closed loops and came out as a pair of rings:
#: what makes a paperclip readable at this size is the free ends, not the
#: curves. The wire is one pixel and there is no highlight -- at 16x16 on an
#: 8bpp screen a second tone on a one-pixel stroke is mud.
_CLIP = [
    "................",
    "................",
    "..........k.....",
    ".........k.k....",
    ".........k.k....",
    "....k....k.k....",
    "....k.k..k.k....",
    "....k.k..k.k....",
    "....k.k..k.k....",
    "....k.k..k.k....",
    "....k.k..k.k....",
    "....k..kk..k....",
    "....k......k....",
    ".....k.....k....",
    "......kkkkk.....",
    "................",
]


#: The autoscroll button: a page-down arrow, 16x16, for the page-control row.
#:
#: comctl32 has no arrow in IDB_STD_SMALL_COLOR -- the view and history strips
#: have back/forward chevrons, which point the wrong way and read as
#: navigation. This is the "follow to the end" arrow: a bar at the bottom for
#: the end of the document, and a chevron pointing at it.
_PGDN = [
    "................",
    "................",
    ".......kk.......",
    ".......kk.......",
    ".......kk.......",
    ".......kk.......",
    ".......kk.......",
    "..kk...kk...kk..",
    "...kk..kk..kk...",
    "....kk.kk.kk....",
    ".....kkkkkk.....",
    "......kkkk......",
    ".......kk.......",
    "................",
    "..kkkkkkkkkkkk..",
    "................",
]


def art_pgdn():
    """The autoscroll toggle's face, on the button colour like the clip."""
    c = Canvas(16, 16)
    c.rect(0, 0, 15, 15, "s")
    for y, row in enumerate(_PGDN):
        for x, ch in enumerate(row):
            if ch != ".":
                c.put(x, y, ch)
    return c


def art_clip():
    """The Attach paperclip, on the button face so comctl32 maps it away.

    A TOOLBAR BITMAP, not an icon: TB_ADDBITMAP takes a DIB and there is no
    AND mask, so transparency comes from colour mapping instead. Silver
    (192,192,192) is the one the toolbar rewrites to whatever the user's
    button face is, so the background is drawn in it rather than left
    transparent -- which would come out black.
    """
    c = Canvas(16, 16)
    c.rect(0, 0, 15, 15, "s")
    for y, row in enumerate(_CLIP):
        for x, ch in enumerate(row):
            if ch != ".":
                c.put(x, y, ch)
    return c


def write_bmp(path, c):
    """A 16-colour bottom-up DIB, which is what TB_ADDBITMAP wants.

    No AND mask and no doubled height, unlike write_ico: a toolbar bitmap is
    a plain image and the transparency comes from colour mapping.
    """
    rowbytes = ((c.w * 4 + 31) // 32) * 4
    bits = bytearray()
    for y in range(c.h - 1, -1, -1):
        row = bytearray()
        for x in range(0, c.w, 2):
            hi = c.px[y][x]
            lo = c.px[y][x + 1] if x + 1 < c.w else 0
            row.append(((hi & 0xF) << 4) | (lo & 0xF))
        row += b"\x00" * (rowbytes - len(row))
        bits += row

    info = struct.pack("<IiiHHIIiiII", 40, c.w, c.h, 1, 4, 0, len(bits),
                       2835, 2835, 16, 0)
    pal = b"".join(struct.pack("<BBBB", b, g, r, 0) for (r, g, b) in PALETTE)
    offset = 14 + len(info) + len(pal)
    hdr = struct.pack("<2sIHHI", b"BM", offset + len(bits), 0, 0, offset)
    with open(path, "wb") as fh:
        fh.write(hdr + info + pal + bytes(bits))
    print("wrote %s (%d bytes, %dx%d)"
          % (path, offset + len(bits), c.w, c.h))


def encode(c):
    """One ICONDIRENTRY's worth of image: header, palette, XOR bits, AND mask.

    Both bitmaps are bottom-up and each ROW is padded to a 4-byte boundary --
    separately. Getting the AND mask's padding wrong is the classic way to
    produce an icon that looks right in a viewer and shears on the desktop.
    """
    w, h = c.w, c.h
    hdr = struct.pack("<IiiHHIIiiII", 40, w, h * 2, 1, 4, 0, 0, 0, 0, 16, 0)
    pal = b"".join(struct.pack("<BBBB", b, g, r, 0) for (r, g, b) in PALETTE)

    xor_stride = ((w * 4 + 31) // 32) * 4
    and_stride = ((w + 31) // 32) * 4
    xor, mask = bytearray(), bytearray()
    for y in range(h - 1, -1, -1):
        row = c.px[y]
        rx = bytearray(xor_stride)
        rm = bytearray(and_stride)
        for x in range(w):
            v = row[x]
            if v is TRANSPARENT:
                # The AND mask is 1 where the background shows through, and
                # the XOR pixel under a set mask bit must be 0 -- otherwise
                # the two XOR together and the "transparent" pixel inverts
                # whatever is behind it.
                rm[x >> 3] |= 0x80 >> (x & 7)
                v = 0
            if x & 1:
                rx[x >> 1] |= v
            else:
                rx[x >> 1] |= v << 4
        xor += rx
        mask += rm
    return hdr + pal + bytes(xor) + bytes(mask)


def write_ico(path, images):
    blobs = [encode(c) for c in images]
    out = bytearray(struct.pack("<HHH", 0, 1, len(images)))
    offset = 6 + 16 * len(images)
    for c, blob in zip(images, blobs):
        out += struct.pack("<BBBBHHII", c.w, c.h, 16, 0, 1, 4,
                           len(blob), offset)
        offset += len(blob)
    for blob in blobs:
        out += blob
    with open(path, "wb") as fh:
        fh.write(out)
    print("wrote %s (%d bytes, %s)"
          % (path, len(out), ", ".join("%dx%d" % (c.w, c.h) for c in images)))


def main():
    top = toolkit.REPO
    app = os.path.join(top, "win", "resources")
    write_ico(os.path.join(app, "gaston.ico"), [art16(), art32()])
    # Lime / yellow / red, the three the VGA palette actually has. Amber
    # would be better than yellow and is not one of the sixteen; a dithered
    # amber on an 8bpp display is noise at this size.
    for name, led in (("netok", "l"), ("netwarn", "y"), ("netbad", "r")):
        write_ico(os.path.join(app, name + ".ico"), [art_net(led)])
    for i in range(PIE_STEPS + 1):
        write_ico(os.path.join(app, "pie%d.ico" % i), [art_pie(i)])
    write_bmp(os.path.join(app, "attach.bmp"), art_clip())
    write_bmp(os.path.join(app, "pgdn.bmp"), art_pgdn())


if __name__ == "__main__":
    main()
