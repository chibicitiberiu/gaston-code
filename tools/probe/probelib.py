"""The part every probe in this folder had written out for itself.

A probe here is a bridge STUB: it stands in for `bridge/server.py`, accepts
the one guest, and drives some corner of the guest's own code directly --
the sync state machine, the permission bar's state stack, the transcript
view, the shutdown drain. They exist because the thing under test is C
running on Windows 98, and the cheapest way to poke at it is to be the
server it already knows how to talk to.

Four of them, and all four opened the socket the same way: `SO_REUSEADDR`,
bind, `listen(1)`, print where we are waiting, accept, print who turned up.
Three of them also declared the same `--port`/`--secret` arguments. What
they do NOT share is the receive loop -- each reads a different conversation
-- so that stays in each probe, and this module stops at the handshake.

`SO_REUSEADDR` is the load-bearing line. Without it a probe restarted inside
the TIME_WAIT window fails to bind, which looks exactly like "the guest is
already connected to something else".
"""

from __future__ import annotations

import argparse
import socket

#: Where the bridge normally listens, so the guest's own retrocl.ini works
#: against a probe without editing anything on the machine.
PORT = 7890

#: The development secret in vm/retrocl.ini. Not a credential; it exists so a
#: stray process on the host cannot be mistaken for the guest.
SECRET = "dev-secret"


def arg_parser(description: str = "") -> argparse.ArgumentParser:
    """`--host`, `--port` and `--secret`, spelled once.

    `--host` is here for all four now. Two of them hardcoded "0.0.0.0" and
    two took the flag, which meant a probe you could point at a specific
    interface and a probe you could not, with nothing to say which was which.
    """
    ap = argparse.ArgumentParser(description=description)
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=PORT)
    ap.add_argument("--secret", default=SECRET)
    return ap


def listen_for_guest(host: str = "0.0.0.0", port: int = PORT):
    """Take one connection and return it, saying so at both ends.

    Both prints matter when you are watching this next to a VM window: the
    first tells you the probe is up before you launch the client, and the
    second is the only confirmation that what connected was the guest rather
    than something else on the host.
    """
    s = socket.socket()
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind((host, port))
    s.listen(1)
    print(f"waiting for the guest on {host}:{port} ...", flush=True)
    conn, peer = s.accept()
    print(f"guest connected from {peer}", flush=True)
    return conn, peer


def secret_ok(hello, secret: str) -> bool:
    """Did the guest send the secret we expected?

    A probe that skips this will happily talk to anything that opens the
    port, and then report the guest's behaviour based on a conversation the
    guest was never part of.
    """
    return hello.headers.get("secret") == secret
