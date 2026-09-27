"""Drive a Win98 machine from Linux over a socket.

The other end is `gremote.exe` (`win/remote/`, docs/REMOTE.md step 4), built
from the same static libraries the Gaston client links. It listens and this
connects, which is the reverse of the client -- and the reason this exists is
that the real Pentium 200 has no QMP and no ssh, so until now every
measurement on it meant burning a floppy and reading COM1.

    from gremote import Remote

    with Remote("192.168.1.50", secret="...") as r:
        print(r.hello["os"])

Blocking sockets and one reader thread. No asyncio: this is a test harness
run from a script, and the bridge's event loop is not something a test tool
should have to stand up.
"""

from __future__ import annotations

from .client import DEFAULT_PORT, Remote
from .errors import Disconnected, Refused, RemoteError, Timeout
from .ship import ShipResult

__all__ = [
    "DEFAULT_PORT",
    "Disconnected",
    "Refused",
    "Remote",
    "RemoteError",
    "ShipResult",
    "Timeout",
]
