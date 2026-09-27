#!/usr/bin/env python3
"""Minimal QMP client for driving the Win98 guest from outside it.

This is the out-of-band control path the plan calls for: screendump and
send-key read and write QEMU's own device model, so they work even when the
guest is wedged -- which is exactly when you need them. No in-guest agent is
involved, and none exists for Win9x anyway.

Three transports, picked automatically and in this order:

  * a raw QMP unix socket, when one exists (qemu started by hand via runvm.sh);
  * libvirt's monitor passthrough over a PERSISTENT python connection;
  * the same passthrough, shelled out to `virsh` once per command.

Same commands either way -- libvirt owns its own monitor socket and will not
share it, so the raw path stops working the moment the domain is defined,
which is the normal case here.

A click used to take 18 seconds, and both halves of that are worth knowing.
The guest has a PS/2 mouse, so a move is a stream of relative deltas; it used
to send one per pixel, ~700 of them, each shelling out to `virsh` at 22ms. So:
send calibrated deltas instead of single pixels (see `_walk`, which is where
the measurements are), and hold one libvirt connection open instead of forking
per command -- 0.73ms against 22ms. A click is now about fifteen packets.

    tools/vm/qmp.py shot out.ppm
    tools/vm/qmp.py key ret
    tools/vm/qmp.py click 208 190
    tools/vm/qmp.py dclick 208 190
    tools/vm/qmp.py type "hello"
    tools/vm/qmp.py home
    tools/vm/qmp.py save clean
    tools/vm/qmp.py load clean
    tools/vm/qmp.py reset
    tools/vm/qmp.py status
    tools/vm/qmp.py hostfwd 7401            # reach a listener INSIDE the guest
    tools/vm/qmp.py hostfwd 7401 --remove

`hostfwd` is how the host reaches something the guest is LISTENING on, which
until gremote nothing needed. The domain uses QEMU's user-mode (slirp)
networking, so the guest can dial out and the host cannot dial in -- and
libvirt's own `<portForward>` is not the answer: it refuses anything but the
passt backend ("the <portForward> element can only be used with the 'passt'
backend"), and switching backends would swap this guest's whole network
stack, DHCP range and hand-configured NE2000 for the sake of one port.
slirp's own hostfwd_add does it at runtime instead: no XML edit, no restart,
and `--remove` undoes it. It does NOT survive a restart, which is the price.
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import toolkit                                            # noqa: E402

#: The raw QMP socket runvm.sh asks qemu for. ABSOLUTE, which it was not:
#: `"vm/qmp.sock"` is relative to the working directory, so the socket
#: transport was silently unavailable to anything invoking this from
#: anywhere but the repo root -- including dos.py, which spawns it by
#: absolute path and inherits whatever cwd its own caller had.
SOCK = toolkit.repo_path("vm", "qmp.sock")
DOMAIN = toolkit.DOMAIN
URI = toolkit.URI

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


class Monitor:
    """Everything that drives the guest's devices. Subclasses supply `cmd`.

    This used to be three classes that borrowed methods from each other by
    assignment -- `shot = Qmp.shot`, and so on down a list that had to be
    maintained by hand. A borrowed method reading `self.DOUBLE_GAP` needs
    DOUBLE_GAP borrowed too, and it was not: `dclick` raised AttributeError
    through one class and worked through the other. The list is gone; the
    behaviour lives in one place and the subclasses differ only in how a
    command reaches QEMU, which is the only thing that actually differs.
    """

    #: Set by subclasses. Used to key the pointer cache, so two domains
    #: driven from the same checkout do not inherit each other's position.
    name = "qmp"

    def cmd(self, name: str, **args) -> dict:
        raise NotImplementedError

    # --- convenience -------------------------------------------------------

    def shot(self, path: str) -> None:
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
    # "move to 400,300". Every move is a sequence of deltas, and the only
    # question is how few of them it takes to land on the pixel asked for.
    #
    # Win98 applies pointer acceleration, so a delta is not the distance
    # travelled. Measured on this guest, by sending one packet from the
    # corner and diffing two screendumps to find the cursor:
    #
    #     delta sent       1    2    3    4   ..   96   127   128+
    #     px travelled     1    2    6    8   ..  192   254    254
    #
    # Three facts fall out of that, and the walk is built on them:
    #
    #   * The threshold is 3. Deltas of 1 and 2 are exact; 3 and everything
    #     above it is doubled. Measured by sending one delta k times and
    #     dividing -- a single small delta cannot be seen, because the two
    #     cursor images overlap.
    #   * It is doubled and ONLY ever doubled: no second threshold anywhere
    #     in the range, so the scale is 1 or 2 and nothing else. 127 is the
    #     ceiling -- QEMU clamps a ps2 delta to it, which is why 128, 192 and
    #     255 all travel the same 254.
    #   * **The scale is chosen per PACKET, not per axis.** One packet of
    #     x=96 y=3 lands at (192, 6): y was doubled because x was big. This
    #     is the fact that shapes the code -- a packet cannot mix a coarse
    #     axis with a fine one, so the walk runs in two passes at a fixed
    #     scale rather than per-axis.
    #
    # And x and y go in the SAME packet, which is what a real mouse does: one
    # `input-send-event` carrying a rel on each axis produces one ps2 packet,
    # so a diagonal costs max(|dx|,|dy|) packets rather than |dx|+|dy|.
    #
    # What does NOT work, and looks like it should: sending many small deltas
    # in one command. QEMU accumulates the rel events of a single
    # `input-send-event` and emits ONE packet at the closing sync, so a batch
    # of two hundred 1s is a delta of 200 and accelerates exactly as one 200
    # would. Fewer, larger, calibrated packets is the only way to cut the
    # count, which is what this does.

    def _rel(self, events) -> None:
        self.cmd("input-send-event", events=events)

    def _packet(self, dx: int, dy: int) -> None:
        ev = []
        if dx:
            ev.append({"type": "rel", "data": {"axis": "x", "value": dx}})
        if dy:
            ev.append({"type": "rel", "data": {"axis": "y", "value": dy}})
        if ev:
            self._rel(ev)
            time.sleep(self.STEP_GAP)

    #: Largest delta worth sending. QEMU clamps a ps2 packet to 127, so
    #: anything above travels no further, and 127 doubles to 254.
    COARSE_MAX = 127

    #: Smallest delta that is doubled, so the smallest the coarse pass may
    #: send. Getting this wrong is silent and looks like nothing: it was 7
    #: on the first attempt and every move landed exactly 14px long, because
    #: two "fine" packets of 7 each travelled 14.
    COARSE_MIN = 3

    #: Largest delta that is NOT accelerated.
    FINE_MAX = 2

    def _walk(self, dx: int, dy: int) -> None:
        """Walk the pointer by (dx, dy), landing on the pixel.

        Coarse pass first, at the x2 scale, while either axis has more than
        `2 * COARSE_MIN` left to cover; then a fine pass at x1 for the last
        thirty-odd pixels. Both axes move in every packet, and the pass is
        what fixes the scale, because the scale is a property of the packet.
        """
        rx, ry = dx, dy

        def step(r, lo, hi, scale):
            """How much of `r` this packet should carry, in delta units."""
            v = min(hi, abs(r) // scale)
            if v < lo:
                return 0
            return v if r > 0 else -v

        # Coarse: doubled. Guarded on `or`, not `and` -- one long axis is
        # still worth coarse packets when the other is already close, and
        # whatever the short axis contributes is doubled along with it.
        while max(abs(rx), abs(ry)) >= 2 * self.COARSE_MIN:   # >= 6
            px = step(rx, 0, self.COARSE_MAX, 2)
            py = step(ry, 0, self.COARSE_MAX, 2)
            if max(abs(px), abs(py)) < self.COARSE_MIN:
                break                     # neither axis can reach the scale
            self._packet(px, py)
            rx -= 2 * px
            ry -= 2 * py

        # Fine: exact, 1 or 2 per packet. At most three, since both axes are
        # now under 6.
        while rx or ry:
            px = step(rx, 0, self.FINE_MAX, 1)
            py = step(ry, 0, self.FINE_MAX, 1)
            self._packet(px, py)
            rx -= px
            ry -= py

    #: Pacing between packets. There is no queue pressure left to speak of --
    #: a full-screen move is about nine packets now, not fifteen hundred --
    #: but the guest is a 200MHz TCG box and this costs nothing.
    STEP_GAP = 0.01

    def home(self) -> None:
        """Pin the pointer at (0,0). A corner cannot be overshot.

        QEMU clamps a ps2 delta to 127, which Win98 doubles to 254, so this
        is 1524px of travel against an 800x600 screen -- deliberate excess,
        because overshooting a corner costs nothing and stopping short costs
        a wrong click. This is the only self-correcting operation here, and
        every move starts with it.
        """
        for _ in range(6):
            self._packet(-self.COARSE_MAX, -self.COARSE_MAX)

    def move(self, x: int, y: int) -> None:
        """Put the pointer at (x, y), by way of the corner.

        Homing first looks wasteful and is not: it costs six packets, a move
        across the whole screen costs eight, and the alternative -- caching
        the last position between the separate `qmp.py click` processes that
        dos.py spawns -- buys about 80ms while adding a way to be silently
        WRONG. Nothing in the guest moves the pointer on its own, but a
        person at the SPICE viewer does, and a cache that has gone stale
        clicks somewhere nobody asked for. The corner cannot be stale.
        """
        self.home()
        time.sleep(0.1)
        self._walk(x, y)
        time.sleep(0.15)

    #: Gap between the two clicks of a double, in seconds. Comfortably inside
    #: Windows' default 500ms double-click time, and slow enough to survive a
    #: loaded host -- QEMU's PS/2 emulation has to deliver two distinct press
    #: events and a TCG guest is not quick about noticing. 0.06 was the
    #: original and was never shown to fail; this is margin, not a fix.
    DOUBLE_GAP = 0.12

    def click(self, x: int, y: int, button: str = "left", double: bool = False) -> None:
        self.move(x, y)
        for i in range(2 if double else 1):
            if i:
                time.sleep(self.DOUBLE_GAP)
            self._rel([{"type": "btn", "data": {"down": True, "button": button}}])
            time.sleep(0.05)
            self._rel([{"type": "btn", "data": {"down": False, "button": button}}])
        time.sleep(0.2)

    def status(self) -> str:
        return self.cmd("query-status").get("status", "?")


class SockMonitor(Monitor):
    """Raw QMP over the unix socket runvm.sh asks qemu for."""

    def __init__(self, path: str = SOCK, timeout: float = 10.0):
        self.name = os.path.abspath(path)
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


def _unwrap(name: str, out: str) -> dict:
    r = json.loads(out)
    if "error" in r:
        raise RuntimeError(f"{name}: {r['error']['desc']}")
    return r.get("return", {})


class LibvirtMonitor(Monitor):
    """libvirt's monitor passthrough over one persistent connection.

    Identical wire traffic to the `virsh` path below and 30x cheaper, because
    it does not fork a process and re-open the libvirt connection for every
    one of the several hundred events a single mouse move costs. This is the
    normal transport: the domain is defined, so the raw socket does not exist.
    """

    def __init__(self, domain: str = DOMAIN):
        import libvirt
        import libvirt_qemu
        self._qemu = libvirt_qemu
        self.name = domain
        self.conn = libvirt.open(URI)
        if self.conn is None:
            raise RuntimeError(f"cannot open {URI}")
        self.dom = self.conn.lookupByName(domain)

    def cmd(self, name: str, **args) -> dict:
        payload = {"execute": name}
        if args:
            payload["arguments"] = args
        return _unwrap(name, self._qemu.qemuMonitorCommand(
            self.dom, json.dumps(payload), 0))


class VirshMonitor(Monitor):
    """The same passthrough, shelled out. Kept for a machine without the
    python bindings; it works, and a mouse move on it takes 18 seconds."""

    def __init__(self, domain: str = DOMAIN):
        self.name = domain

    def cmd(self, name: str, **args) -> dict:
        payload = {"execute": name}
        if args:
            payload["arguments"] = args
        out = subprocess.run(
            ["virsh", "-c", URI, "qemu-monitor-command", self.name,
             json.dumps(payload)],
            capture_output=True, text=True, check=True).stdout
        return _unwrap(name, out)


#: Old names. Nothing outside this file imports them, but the docs do.
Qmp = SockMonitor
VirshQmp = VirshMonitor


def connect():
    if os.path.exists(SOCK):
        return SockMonitor()
    try:
        return LibvirtMonitor()
    except ImportError:
        pass                              # no python3-libvirt; fall back
    except Exception as e:                # domain not defined, session gone
        print(f"qmp: libvirt: {e}; falling back to virsh", file=sys.stderr)
    if shutil.which("virsh"):
        return VirshMonitor()
    raise SystemExit(f"no {SOCK} and no virsh -- is the guest running?")


def _netdev_id(q) -> str:
    """Which netdev slirp is behind.

    Asked rather than assumed: libvirt names it hostnet0 today, but that is
    its choice and not a contract, and hostfwd_add against the wrong name
    fails in a way that reads like the guest is not listening.
    """
    # cmd() already unwraps "return", and for human-monitor-command that is
    # the console text itself rather than an object.
    out = str(q.cmd("human-monitor-command",
                    **{"command-line": "info network"}))
    for line in out.splitlines():
        # QEMU draws the netdev as a child of the nic it is attached to:
        #   net0: index=0,type=nic,model=ne2k_isa,macaddr=...
        #    \ hostnet0: index=0,type=user,net=10.0.2.0,restrict=off
        # The backslash is part of that drawing, not of the name, and
        # passing it along is what "extraneous characters at the end of
        # line" means when hostfwd_add refuses.
        line = line.strip().lstrip("\\").strip()
        if "type=user" in line and ":" in line:
            return line.split(":", 1)[0].strip()
    raise SystemExit("qmp: no user-mode netdev; is this guest on slirp?\n"
                     + out)


def hostfwd(q, port: int, *, remove: bool = False, host: str = "127.0.0.1",
            guest_port: int | None = None) -> str:
    """Forward host:port into the guest, or take the forward away.

    Bound to 127.0.0.1 rather than every interface: this opens a door into a
    Windows 98 machine with no patches after 2002, and it is meant for the
    tests on this box, not for the LAN.
    """
    dev = _netdev_id(q)
    inner = guest_port if guest_port is not None else port
    verb = "hostfwd_remove" if remove else "hostfwd_add"
    rule = (f"{dev} tcp:{host}:{port}" if remove
            else f"{dev} tcp:{host}:{port}-:{inner}")
    said = str(q.cmd("human-monitor-command",
                     **{"command-line": f"{verb} {rule}"})).strip()
    if said and said != "{}":
        raise SystemExit(f"qmp: {verb} refused: {said}")
    return f"{verb} {rule}"


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
    elif verb == "home":
        q.home()
    elif verb == "click":
        # `click X Y double` means the same as `dclick X Y`.
        #
        # It is accepted because it was TYPED, repeatedly, by someone who had
        # read `click(self, x, y, button, double=False)` and reasonably
        # assumed the CLI mirrored it. The extra word was silently ignored, so
        # every "double-click" in that session was a single click -- which
        # made a working feature look broken and sent the investigation after
        # the wrong thing twice. A verb that quietly drops an argument it does
        # not understand is worse than one that rejects it.
        extra = [a for a in rest[2:] if a not in ("double", "dbl")]
        if extra:
            # Loudly, not silently. A verb that drops what it does not
            # understand is how "double" went unnoticed in the first place.
            print("qmp: ignoring unknown argument(s): %s" % " ".join(extra),
                  file=sys.stderr)
        q.click(int(rest[0]), int(rest[1]),
                double=bool(set(rest[2:]) & {"double", "dbl"}))
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
    elif verb == "hostfwd":
        if not rest:
            print("hostfwd: which port?", file=sys.stderr)
            return 2
        port = int(rest[0])
        remove = "--remove" in rest or "-r" in rest
        print(hostfwd(q, port, remove=remove))
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
