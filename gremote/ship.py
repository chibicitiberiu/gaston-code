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

import contextlib
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


# --- replacing the server itself --------------------------------------------
#
# WHY ship() CANNOT DO THIS. `ship()` ends the program holding the target
# image and then writes. gremote's own pid is on `Processes::kill`'s
# permanent deny list, so the call comes back `denied` -- measured on the
# real hardware, 2026-10-01 -- and a new build can be deployed to every
# program on the machine EXCEPT the one doing the deploying.
#
# The guest's answer is `UPDATE` plus `gupdate.exe`, a second process that
# outlives the server and puts the new image in place after it has gone;
# win/remote/gupdate.cpp argues the design and
# win/remote/control.cpp's update() argues why stopping the server this way
# is not the remote QUIT docs/REMOTE.md forbids.
#
# WHAT THIS SIDE IS FOR. Everything above happens on a machine that stops
# answering halfway through, so the only party that can say whether it
# WORKED is this one: it stages the build, triggers the update, hangs up,
# and then reconnects and reports what came back. A self-update you cannot
# confirm is a self-update you have to go and look at, which on a machine in
# another room is the whole problem again.


@dataclass(frozen=True)
class UpdateResult:
    """What came back after the server replaced itself."""

    #: The image that was replaced, as the guest named it.
    target: str
    #: Where the new build was staged, and where the old one was kept.
    staged: str
    backup: str
    #: The updater's pid, from the reply the old server managed to send.
    helper_pid: int
    #: How long the port took to answer again.
    reconnect_s: float
    #: HELLO from the server that answered. A fresh dict, not the old one.
    hello: dict
    #: `stat` of the installed image afterwards, and whether it is the size
    #: of what was sent. False with a `backup` present is what a rollback
    #: looks like from out here -- see `rolled_back`.
    installed_size: int
    sent_size: int

    @property
    def matches(self) -> bool:
        return self.installed_size == self.sent_size

    @property
    def rolled_back(self) -> bool:
        """The server answered, but not from the build that was sent.

        gupdate puts the previous image back when the new one does not stay
        up, so a machine that is alive and running a DIFFERENT size than
        what was shipped is the rollback having fired. Read GUPDATE.LOG on
        the guest for which of its reasons it was.
        """
        return not self.matches


def _server_image(remote, exe_name: str) -> str:
    """Where the running server lives, from the process listing.

    Asked rather than configured. On 9x `PS` gives full paths, the server is
    single-instance, and the alternative -- a `build=` header on HELLO --
    costs bytes in an image that has about 2KB of headroom left
    (win/Makefile, check-gremote).
    """
    want = exe_name.lower()
    found = [p.exe for p in remote.ps() if _basename(p.exe) == want]
    if len(found) == 1:
        path = found[0]
        if "\\" not in path and ":" not in path:
            # 9x reports a full path and NT a bare name (win/exec/process.h),
            # and Wine is NT here. A bare name would quietly become a
            # RELATIVE staging path and put the new build somewhere nobody
            # chose, so it is refused instead -- which is why the Wine
            # suite passes target= and the real guest does not have to.
            raise RemoteError(
                f"the process listing gives {path!r} with no directory "
                f"(NT and Wine report bare names); pass target= with the "
                f"full path of the image to replace")
        return path
    if not found:
        raise RemoteError(
            f"no {exe_name} in the process listing, so there is nothing to "
            f"replace; pass target= if it runs under another name")
    raise RemoteError(
        f"{len(found)} copies of {exe_name} are running ({found}); pass "
        f"target= to say which image to replace")


def _sibling(path: str, ext: str) -> str:
    """GREMOTE.EXE -> GREMOTE.<ext>, the same rule gupdate.cpp uses.

    The extension is REPLACED, not appended: `GREMOTE.EXE.OLD` has no 8.3
    spelling and the one FAT invents is not a name anybody can type back.
    """
    head, _, tail = path.rpartition("\\")
    base = tail.rsplit(".", 1)[0] if "." in tail else tail
    return f"{head}\\{base}{ext}" if head else f"{base}{ext}"


def self_update(remote, local_path: str, *, target: str = "",
                staged: str = "", exe_name: str = "gremote.exe",
                wait_ms: int = 60000, settle_s: float = 120.0,
                poll_s: float = 1.0, clean: bool = True) -> UpdateResult:
    """Replace the server with `local_path` and wait for it to answer again.

    Four steps, and the third one takes the machine away mid-sentence:

    1. find the running image, unless `target` says;
    2. `PUT` the new build to `staged` (beside the target, under a name
       gupdate does not use for its own working files);
    3. `UPDATE`, which validates, starts `gupdate.exe` and stops the
       server. The reply is drained onto the wire before the process goes,
       so an OK here is real -- but the link dies immediately after it;
    4. reconnect until the port answers, then report what is installed.

    `settle_s` is generous on purpose. The guest has to wind down five
    threads, the updater polls at 250ms for the image lock AND the
    single-instance mutex, and a cold start off a 1998 platter is not
    quick. Running out raises rather than returning a sad result: "it never
    came back" is not a value anybody should be able to ignore.
    """
    from .client import Remote

    with open(local_path, "rb") as f:
        payload = f.read()

    target = target or _server_image(remote, exe_name)
    staged = staged or _sibling(target, ".UPD")
    backup = _sibling(target, ".OLD")

    remote.put(staged, payload)
    got = remote.stat(staged)
    if got.size != len(payload):
        raise RemoteError(
            f"{staged} is {got.size} bytes, sent {len(payload)}; not "
            f"triggering an update with a half-written image")

    reply = remote.request("UPDATE", {"confirm": "replace", "src": staged,
                                      "wait_ms": str(int(wait_ms))})
    helper_pid = int(reply.headers.get("pid", "0"), 16)
    target = reply.headers.get("target", target)

    # Hang up on our side rather than wait for the FIN. The server is on its
    # way out and the updater is waiting for it to be gone; a connection
    # left open is one more thing for it to wait on.
    host, port, secret = remote.host, remote.port, remote.secret
    remote.close()

    started = time.monotonic()
    deadline = started + settle_s
    last: Exception | None = None
    fresh: Remote | None = None
    while time.monotonic() < deadline:
        # busy_retry_s=0: the point here is the FIRST honest answer. A
        # `busy` would mean somebody else has the box, which is news, not
        # something to sit through.
        probe = Remote(host, secret, port=port, busy_retry_s=0)
        try:
            probe.connect()
            fresh = probe
            break
        except (OSError, RemoteError) as e:
            last = e
            probe.close()
            time.sleep(poll_s)
    if fresh is None:
        raise RemoteError(
            f"{target} was replaced but nothing answered on {host}:{port} "
            f"within {settle_s:g}s (last: {last}). GUPDATE.LOG beside the "
            f"exe says what the updater did, and {backup} is the build "
            f"that was working.")

    took = time.monotonic() - started
    installed = fresh.stat(target)

    result = UpdateResult(
        target=target, staged=staged, backup=backup,
        helper_pid=helper_pid, reconnect_s=took,
        hello=dict(fresh.hello),
        installed_size=installed.size, sent_size=len(payload))

    # Only once it is known to have worked. A staged image left behind after
    # a rollback is the only copy of the build that failed, and deleting it
    # would throw away the thing somebody has to look at.
    if clean and result.matches:
        with contextlib.suppress(Refused, RemoteError):
            fresh.rm(staged)
    fresh.close()
    return result
