#!/usr/bin/env python3
"""Minimal QMP client for driving the Win98 guest from outside it.

This is the out-of-band control path the plan calls for: screendump and
send-key read and write QEMU's own device model, so they work even when the
guest is wedged -- which is exactly when you need them. No in-guest agent is
involved, and none exists for Win9x anyway.

Two transports, picked automatically: a raw QMP unix socket when one exists
(qemu started by hand), otherwise `virsh qemu-monitor-command` against the
libvirt domain. Same commands either way -- libvirt owns its own monitor
socket and will not share it, so the raw path stops working the moment the
domain is defined.

    tools/qmp.py shot out.ppm
    tools/qmp.py key ret
    tools/qmp.py click 208 190
    tools/qmp.py dclick 208 190
    tools/qmp.py type "hello"
    tools/qmp.py save clean
    tools/qmp.py load clean
    tools/qmp.py reset
    tools/qmp.py status
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import time

SOCK = "vm/qmp.sock"
DOMAIN = os.environ.get("RC_DOMAIN", "retroclaude-win98")
URI = os.environ.get("LIBVIRT_DEFAULT_URI", "qemu:///session")

# QEMU key names differ from ASCII; enough of a map to type a DOS command line.
KEYMAP = {
    " ": "spc", "\n": "ret", "\t": "tab", "-": "minus", "=": "equal",
    "[": "bracket_left", "]": "bracket_right", ";": "semicolon",
    "'": "apostrophe", "`": "grave_accent", "\\": "backslash",
    ",": "comma", ".": "dot", "/": "slash", ":": "shift-semicolon",
    "_": "shift-minus", "+": "shift-equal", "?": "shift-slash",
    "!": "shift-1", "@": "shift-2", "#": "shift-3", "$": "shift-4",
    "%": "shift-5", "^": "shift-6", "&": "shift-7", "*": "shift-8",
    "(": "shift-9", ")": "shift-0", '"': "shift-apostrophe",
    "<": "shift-comma", ">": "shift-dot", "{": "shift-bracket_left",
    "}": "shift-bracket_right", "|": "shift-backslash",
    "~": "shift-grave_accent",
}


class Qmp:
    def __init__(self, path: str = SOCK, timeout: float = 10.0):
        self.s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.s.settimeout(timeout)
        self.s.connect(path)
        self.f = self.s.makefile("rwb")
        self._read()                      # greeting
        self.cmd("qmp_capabilities")

    def _read(self) -> dict:
        while True:
            line = self.f.readline()
            if not line:
                raise ConnectionError("qmp closed")
            msg = json.loads(line)
            if "event" in msg:            # ignore async events
                continue
            return msg

    def cmd(self, name: str, **args) -> dict:
        payload = {"execute": name}
        if args:
            payload["arguments"] = args
        self.f.write((json.dumps(payload) + "\n").encode())
        self.f.flush()
        r = self._read()
        if "error" in r:
            raise RuntimeError(f"{name}: {r['error']['desc']}")
        return r.get("return", {})

    # --- convenience -------------------------------------------------------

    def shot(self, path: str) -> None:
        import os
        self.cmd("screendump", filename=os.path.abspath(path))

    def key(self, *keys: str) -> None:
        for k in keys:
            parts = k.split("-")
            self.cmd("send-key", keys=[{"type": "qcode", "data": p}
                                       for p in parts])
            time.sleep(0.05)

    def type(self, text: str) -> None:
        """Type `text` into the guest, or refuse.

        It used to `continue` past anything it could not map, which is how
        `ECHO x >> file` arrived in the guest as `ECHO x  file`: the two `>`
        were dropped in silence, the command printed to the screen instead of
        writing a file, and the sync pass that followed drew a perfectly
        reasonable conclusion from a test that had never set up its own
        precondition. A driver that silently types something other than what
        you asked for is worse than one that does nothing.
        """
        # ASCII alnum only: 'é'.isalnum() is True in Python and there is no
        # qcode for it, so the permissive test let it through to send-key.
        missing = sorted({c for c in text
                          if c not in KEYMAP
                          and not (c.isascii() and c.isalnum())})
        if missing:
            raise ValueError(
                f"no key mapping for {missing!r} in {text!r} -- add it to "
                f"KEYMAP rather than letting it be dropped")
        for ch in text:
            if ch in KEYMAP:
                self.key(KEYMAP[ch])
            elif ch.isupper():
                self.key(f"shift-{ch.lower()}")
            else:
                self.key(ch)
            time.sleep(0.02)

    # --- mouse -------------------------------------------------------------
    #
    # The guest has a PS/2 mouse, which is a RELATIVE device: there is no
    # "move to 400,300". So home the pointer into the top-left corner with one
    # oversized move, then walk to the target in single-pixel steps.
    #
    # Single pixels, not one big delta, because Win98's pointer acceleration
    # doubles any movement past its threshold (6 by default) and a batched
    # 400px jump lands somewhere else entirely. Deltas of 1 never trip it.
    def _rel(self, events) -> None:
        self.cmd("input-send-event", events=events)

    def _walk(self, dx: int, dy: int) -> None:
        """Walk to the target in small steps, paced.

        Two limits, both easy to trip and both silent when you do:
        QEMU's PS/2 queue is short, so a burst of a few hundred motion events
        is simply dropped; and Win98 accelerates, so the pointer travels
        further than asked. Measured: with a step of 4 a requested (100,100)
        landed at (197,192), almost exactly double. A step of 1 stays under
        the acceleration threshold, and the sleep keeps the queue drained.
        """
        STEP = 1
        for axis, d in (("x", dx), ("y", dy)):
            sign = 1 if d >= 0 else -1
            left = abs(d)
            while left:
                n = min(STEP, left)
                self._rel([{"type": "rel", "data": {"axis": axis, "value": sign * n}}])
                left -= n
                time.sleep(0.004)

    def home(self) -> None:
        """Pin the pointer at (0,0). A corner cannot be overshot.

        A PS/2 packet carries a 9-bit signed delta, so one huge move is
        clamped rather than obeyed -- hence a few repeats.
        """
        for _ in range(6):
            self._rel([{"type": "rel", "data": {"axis": "x", "value": -200}},
                       {"type": "rel", "data": {"axis": "y", "value": -200}}])
            time.sleep(0.01)

    def move(self, x: int, y: int) -> None:
        self.home()
        time.sleep(0.15)
        self._walk(x, y)
        time.sleep(0.15)

    def click(self, x: int, y: int, button: str = "left", double: bool = False) -> None:
        self.move(x, y)
        for i in range(2 if double else 1):
            if i:
                time.sleep(0.06)
            self._rel([{"type": "btn", "data": {"down": True, "button": button}}])
            time.sleep(0.05)
            self._rel([{"type": "btn", "data": {"down": False, "button": button}}])
        time.sleep(0.2)

    def status(self) -> str:
        return self.cmd("query-status").get("status", "?")


class VirshQmp:
    """Same surface as Qmp, tunnelled through libvirt's monitor passthrough."""

    def __init__(self, domain: str = DOMAIN):
        self.domain = domain

    def cmd(self, name: str, **args) -> dict:
        payload = {"execute": name}
        if args:
            payload["arguments"] = args
        out = subprocess.run(
            ["virsh", "-c", URI, "qemu-monitor-command", self.domain,
             json.dumps(payload)],
            capture_output=True, text=True, check=True).stdout
        r = json.loads(out)
        if "error" in r:
            raise RuntimeError(f"{name}: {r['error']['desc']}")
        return r.get("return", {})

    shot = Qmp.shot
    key = Qmp.key
    type = Qmp.type
    status = Qmp.status
    _rel = Qmp._rel
    _walk = Qmp._walk
    home = Qmp.home
    move = Qmp.move
    click = Qmp.click


def connect():
    if os.path.exists(SOCK):
        return Qmp()
    if shutil.which("virsh"):
        return VirshQmp()
    raise SystemExit(f"no {SOCK} and no virsh -- is the guest running?")


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    verb, rest = sys.argv[1], sys.argv[2:]
    q = connect()
    if verb == "shot":
        q.shot(rest[0] if rest else "shot.ppm")
    elif verb == "key":
        q.key(*rest)
    elif verb == "type":
        q.type(" ".join(rest))
    elif verb == "move":
        q.move(int(rest[0]), int(rest[1]))
    elif verb == "click":
        q.click(int(rest[0]), int(rest[1]))
    elif verb == "dclick":
        q.click(int(rest[0]), int(rest[1]), double=True)
    elif verb == "rclick":
        q.click(int(rest[0]), int(rest[1]), button="right")
    elif verb == "save":
        print(q.cmd("human-monitor-command",
                    **{"command-line": f"savevm {rest[0]}"}))
    elif verb == "load":
        print(q.cmd("human-monitor-command",
                    **{"command-line": f"loadvm {rest[0]}"}))
    elif verb == "reset":
        q.cmd("system_reset")
    elif verb == "status":
        print(q.status())
    elif verb == "quit":
        q.cmd("quit")
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
