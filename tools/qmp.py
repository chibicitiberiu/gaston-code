#!/usr/bin/env python3
"""Minimal QMP client for driving the Win98 guest from outside it.

This is the out-of-band control path the plan calls for: screendump and
send-key read and write QEMU's own device model, so they work even when the
guest is wedged -- which is exactly when you need them. No in-guest agent is
involved, and none exists for Win9x anyway.

    tools/qmp.py shot out.ppm
    tools/qmp.py key ret
    tools/qmp.py type "hello"
    tools/qmp.py save clean
    tools/qmp.py load clean
    tools/qmp.py reset
    tools/qmp.py status
"""
from __future__ import annotations

import json
import socket
import sys
import time

SOCK = "vm/qmp.sock"

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
        for ch in text:
            if ch in KEYMAP:
                self.key(KEYMAP[ch])
            elif ch.isupper():
                self.key(f"shift-{ch.lower()}")
            elif ch.isalnum():
                self.key(ch)
            else:
                continue
            time.sleep(0.02)

    def status(self) -> str:
        return self.cmd("query-status").get("status", "?")


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    verb, rest = sys.argv[1], sys.argv[2:]
    q = Qmp()
    if verb == "shot":
        q.shot(rest[0] if rest else "shot.ppm")
    elif verb == "key":
        q.key(*rest)
    elif verb == "type":
        q.type(" ".join(rest))
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
