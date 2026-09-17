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

import subprocess
import sys
import tempfile
import time

HERE = __import__("os").path.dirname(__file__)

#: Inside the DOS client area in the default window position, and clear of the
#: text that scrolls through it.
PROBE = (560, 380)


def shot() -> "object":
    from PIL import Image
    with tempfile.NamedTemporaryFile(suffix=".png") as f:
        subprocess.run(["bash", f"{HERE}/vmshot.sh", f.name],
                       check=True, capture_output=True)
        return Image.open(f.name).convert("RGB").copy()


def dos_focused() -> bool:
    px = shot().getpixel(PROBE)
    return sum(px) < 90          # near-black


def focus(tries: int = 6) -> None:
    for _ in range(tries):
        if dos_focused():
            return
        subprocess.run(["python3", f"{HERE}/qmp.py", "key", "alt-tab"],
                       check=True, capture_output=True)
        time.sleep(2.0)
    raise SystemExit(
        "could not bring the MS-DOS Prompt to the front; refusing to type "
        "into whatever is there instead")


def run(cmd: str, settle: float = 3.0) -> None:
    focus()
    subprocess.run(["python3", f"{HERE}/qmp.py", "type", cmd],
                   check=True, capture_output=True)
    subprocess.run(["python3", f"{HERE}/qmp.py", "key", "ret"],
                   check=True, capture_output=True)
    time.sleep(settle)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: dos.py <command> [<command> ...]", file=sys.stderr)
        raise SystemExit(2)
    for c in sys.argv[1:]:
        run(c)
    print("ok")
