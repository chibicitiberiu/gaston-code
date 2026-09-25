#!/usr/bin/env python3
"""Type a command into the guest's MS-DOS Prompt, having first made sure that
is where it is going.

Alt-Tab is not addressing. Twice during sync verification it landed on the
Gaston Code window instead, so `DELTREE /Y C:\\WORK` went into the chat input
and was sent to the model as a prompt -- once costing a permission dialog and a
denied tool call, once leaving a test believing it had cleared a directory it
had not, which made the next result meaningless.

So: focus, then CHECK, then type. The check is that the DOS client area is
black, which the Win98 desktop and the client's white transcript are not.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time

#: This directory. qmp.py and vmshot.sh are siblings, which is why they
#: are reached from HERE and not from the repo root.
HERE = os.path.dirname(os.path.abspath(__file__))

#: Both constants that used to live here -- a fixed point in the title bar
#: and a fixed point in the client area -- are gone. They were measured
#: against one window position at one screen size, and the day the guest was
#: switched to 800x600 they pointed at the desktop and at somebody else's
#: window. `focus` then alt-tabbed six times and refused, which reads as "the
#: window would not come forward" while the DOS box sat in front of it.
#: `find_caption` looks for the title bar instead.

def is_active_caption(px) -> bool:
    """Win98's active title bar navy, allowing for the palette.

    NOT an exact tuple, which is what this was. The colour is (0,0,128) at
    640x480x8 and (0,0,120) at 800x600 -- the 8bpp palette does not hold the
    system colours at identical values across modes -- so an exact compare
    silently stopped matching anything the day the guest was resized, and
    `focus` alt-tabbed six times and refused with the DOS box already in
    front of it.

    Still not a brightness test, for the reason the old comment gave: the
    desktop teal and the wizard artwork are both "bluish". Pinning red and
    green near zero excludes teal (g is 128) and the band on blue excludes
    anything brighter than a caption.
    """
    r, g, b = px[0], px[1], px[2]
    return r < 40 and g < 40 and 90 <= b <= 160


def shot() -> "object":
    from PIL import Image
    with tempfile.NamedTemporaryFile(suffix=".png") as f:
        subprocess.run(["bash", f"{HERE}/vmshot.sh", f.name],
                       check=True, capture_output=True)
        return Image.open(f.name).convert("RGB").copy()


def find_caption(im):
    """The centre of the widest run of active-caption navy, or None.

    A caption is the one wide band of this exact colour on the screen, and
    its centre is a point inside it and clear of both the caption text (drawn
    in white) and the three buttons at the right-hand end. Found rather than
    assumed, so moving or resizing the window does not silently turn the
    check into a reading of the desktop.
    """
    w, h = im.size
    best = None

    def offer(run, start, y):
        nonlocal best
        if run >= 120 and (best is None or run > best[0]):
            best = (run, start + run // 2, y)

    for y in range(0, h - 40, 2):
        run, start = 0, 0
        for x in range(0, w, 2):
            if is_active_caption(im.getpixel((x, y))):
                if not run:
                    start = x
                run += 2
            else:
                offer(run, start, y)
                run = 0
        offer(run, start, y)
    return None if best is None else (best[1], best[2])


def dos_focused() -> bool:
    """Is the MS-DOS Prompt the window holding the keyboard?

    TWO questions, and only the second is about focus. "Visible" and
    "focused" are different, and that difference cost three misdirected
    command batches in one session: a stray click opened My Documents over
    part of the screen, the DOS box stayed VISIBLE with its client area still
    black, and `DELTREE /Y C:\\WORK` went to the window that actually had the
    keyboard. So: find the ACTIVE caption, then check that what is under it
    is the black client area of a console.
    """
    im = shot()
    cap = find_caption(im)
    if cap is None:
        return False
    x, y = cap
    probe = (x, min(y + 120, im.size[1] - 1))
    return sum(im.getpixel(probe)) < 90


def _click(x: int, y: int) -> None:
    subprocess.run(["python3", f"{HERE}/qmp.py", "click", str(x), str(y)],
                   check=True, capture_output=True)


def focus(tries: int = 2) -> None:
    """Bring the MS-DOS Prompt forward, or refuse to type at all.

    Alt-tab first, because it is one keystroke and usually right. Then the
    TASKBAR, because alt-tab is not addressing: a discrete alt+tab toggles
    between the two most recently used windows, so with three open -- the
    client, an Explorer window and this -- it oscillates between the other
    two forever and this function used to spend six rounds proving it.

    The taskbar buttons are walked left to right and each is checked. x
    starts past the Start button and the quick-launch icons; clicking one of
    those would open a menu over the very thing being looked for.
    """
    for _ in range(tries):
        if dos_focused():
            return
        subprocess.run(["python3", f"{HERE}/qmp.py", "key", "alt-tab"],
                       check=True, capture_output=True)
        time.sleep(1.5)

    im = shot()
    w, h = im.size
    row = h - 13
    for x in range(170, w - 110, 55):
        _click(x, row)
        time.sleep(1.0)
        if dos_focused():
            return

    raise SystemExit(
        "could not bring the MS-DOS Prompt to the front; refusing to type "
        "into whatever is there instead")


def install_client(name: str) -> str:
    """Put the freshly built client on the guest under `name`, and return it.

    It does NOT overwrite the running one, and that is the whole point.
    Windows 98 maps a running image, so `COPY D:\\RETROCL.EXE C:\\RETRO` fails
    with "Sharing violation" -- which prints as one line in a console whose
    screendumps are frequently stale, so it reads as success. Twice a test ran
    against the previous binary and its result was taken at face value.

    Closing the client first was the obvious fix and it is not reliable:
    Alt+F4 acts on whatever has focus, which was the DOS box once and, on
    another occasion, one of TWO clients that were running. (Two running also
    explains a run of duplicate "Connected to bridge" lines that had been put
    down to reconnect churn.)

    A fresh name sidesteps all of it. The config is read from the exe's own
    directory, so `C:\\RETRO\\RC7.EXE` picks up the same retroclaude.ini, and
    193KB on a 1.7GB disk is not a consideration.
    """
    run("CLS")
    run(f"COPY D:\\RETROCL.EXE C:\\RETRO\\{name}", settle=5.0)
    return f"C:\\RETRO\\{name}"


def run(cmd: str, settle: float = 3.0) -> None:
    """Type one command into the DOS box and press Enter.

    Focus is checked BEFORE typing and again before Enter. The second check is
    not belt and braces: a client launched by the previous command takes focus
    a second or two later, and a `DELTREE /Y C:\\WORK` half-typed into the DOS
    box and half into the chat input sent "/Y C:\\WORK" to the model as a
    prompt. Seen, in that exact form.
    """
    focus()
    subprocess.run(["python3", f"{HERE}/qmp.py", "type", cmd],
                   check=True, capture_output=True)
    if not dos_focused():
        raise SystemExit(
            f"focus moved away while typing {cmd!r}; NOT pressing Enter -- "
            f"half of it has gone somewhere else and running the rest would "
            f"make the result meaningless")
    subprocess.run(["python3", f"{HERE}/qmp.py", "key", "ret"],
                   check=True, capture_output=True)
    time.sleep(settle)



def serial_log() -> str:
    """Where the guest's COM1 lands on the host, from the domain's own XML.

    Read, not assumed: the file belongs to whichever checkout set the device
    up, and a second clone working the same VM has no copy of its own.
    """
    import re
    dom = os.environ.get("RC_DOMAIN", "retroclaude-win98")
    uri = os.environ.get("LIBVIRT_DEFAULT_URI", "qemu:///session")
    xml = subprocess.run(["virsh", "-c", uri, "dumpxml", dom], check=True,
                         capture_output=True, text=True).stdout
    m = re.search(r"<serial type='file'>\s*<source path='([^']+)'", xml)
    if not m:
        raise SystemExit(f"{dom} has no file-backed serial port")
    return m.group(1)


def fetch(guest_path: str, quiet: float = 1.5, limit: float = 60.0) -> str:
    """A guest TEXT file's contents, by `TYPE <path> > COM1`.

    Screenshots of a scrolling DOS box were how a guest log used to be read,
    a page at a time. This returns it whole. The serial file is appended to
    and shared, so only what arrives after the command is returned; the
    transfer is taken as finished once the file stops growing for `quiet`
    seconds. TEXT only: TYPE stops at the first ^Z, so a binary file comes
    back cut short. A file the guest still holds open (the client's own log,
    while it runs) may come back stale -- FAT updates the entry on close.
    """
    path = serial_log()
    start = os.path.getsize(path) if os.path.exists(path) else 0
    run(f"TYPE {guest_path} > COM1", settle=0.5)
    size, still, waited = start, 0.0, 0.0
    while waited < limit and still < quiet:
        time.sleep(0.25)
        waited += 0.25
        now = os.path.getsize(path)
        still = still + 0.25 if now == size else 0.0
        size = now
    with open(path, "rb") as f:
        f.seek(start)
        data = f.read()
    return data.decode("cp1252", "replace").replace("\r\n", "\n")

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: dos.py <command> [<command> ...]", file=sys.stderr)
        raise SystemExit(2)
    for c in sys.argv[1:]:
        run(c)
    print("ok")
