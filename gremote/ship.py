"""Put a new build on the machine and start it again.

This is `tools/vm/vmship.sh` as a function, and the two traps that flow has
cost time for are the reason it is code rather than three calls:

**A running exe is locked.** Windows refuses to write to the image of a
process that is running, and the failure is not always loud -- the old
install_client reported success while the guest kept running the previous
binary. So the program is ended and confirmed gone BEFORE anything is
written, and the write is verified after.

**A second copy of the client raises the first and exits.** It holds a named
mutex, so launching a new one while the old is alive looks like a successful
start and changes nothing. Ending the old one is not optional.

What this does not do is ask politely. There is no window to close from here
-- that is the ui lane, step 6 -- so the process is killed. On a test box
that is the right trade: the conversation lives on the bridge, and the thing
being shipped is a build nobody is typing into.
"""

from __future__ import annotations

import ntpath
import time
from dataclasses import dataclass

from .errors import Refused, RemoteError


@dataclass(frozen=True)
class ShipResult:
    """What happened, in enough detail to put in a log."""

    killed: list[int]
    bytes_written: int
    pid: int


def _basename(path: str) -> str:
    return ntpath.basename(path).lower()


def ship(remote, local_path: str, remote_path: str, *,
         relaunch: bool = True, cwd: str = "",
         settle_s: float = 15.0) -> ShipResult:
    """Send `local_path` to `remote_path`, ending anything running from it.

    Returns what was killed, how many bytes landed, and the pid of the
    relaunched program (0 if `relaunch` is false).
    """
    with open(local_path, "rb") as f:
        payload = f.read()

    target = _basename(remote_path)
    killed: list[int] = []

    # Two passes: ask, then insist. A program given a moment to go usually
    # goes, and forcing straight away would end a 16-bit process or a DOS
    # session without needing to -- which on 9x is how you strand a VM.
    for force in (False, True):
        running = [p for p in remote.ps() if _basename(p.exe) == target]
        if not running:
            break
        for proc in running:
            try:
                remote.kill(proc.pid, force=force)
                killed.append(proc.pid)
            except Refused as e:
                # `pending` is not a failure: on 98 TerminateProcess
                # returning success is not the same as the process being
                # gone, and the wait below is what settles it.
                if e.code not in ("pending", "no_such_pid"):
                    raise
        deadline = time.monotonic() + settle_s
        while time.monotonic() < deadline:
            if not [p for p in remote.ps() if _basename(p.exe) == target]:
                break
            time.sleep(0.5)

    still = [p for p in remote.ps() if _basename(p.exe) == target]
    if still:
        raise RemoteError(
            f"{target} is still running as {[hex(p.pid) for p in still]}; "
            f"writing over a locked image would report success and change "
            f"nothing")

    written = remote.put(remote_path, payload)

    # Verified, not assumed. The guest checked the CRC before it renamed, so
    # this is about the file being where it was asked for at the size it was
    # sent -- the cheap half of the check, and the half that catches a path
    # that quietly went somewhere else.
    got = remote.stat(remote_path)
    if got.size != len(payload):
        raise RemoteError(
            f"{remote_path} is {got.size} bytes, sent {len(payload)}")

    pid = remote.launch(remote_path, cwd=cwd) if relaunch else 0
    return ShipResult(killed, written, pid)
