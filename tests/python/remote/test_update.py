"""UPDATE: the server replaces itself and comes back.

`ship()` can put a new build on every program on the machine except the one
doing the shipping -- gremote's own pid is on `Processes::kill`'s permanent
deny list, so the call comes back `denied`, measured on real hardware on
2026-10-01. `UPDATE` plus `gupdate.exe` is the way round that:
win/remote/gupdate.cpp argues the design, win/remote/control.cpp's update()
argues why stopping the server as part of replacing it is not the remote
QUIT docs/REMOTE.md forbids.

WHAT WINE CAN AND CANNOT SETTLE HERE, because it is more than usual.

  It CAN settle: the refusals; the updater being found, started and handed a
  command line it parses; the server stopping; the swap; GREMOTE.OLD being
  kept; the relaunch; the reconnect; and -- the one worth the whole suite --
  the ROLLBACK, by shipping a program that is a valid 32-bit image and
  exits immediately.

  It CANNOT settle the wait. Measured 2026-09-27: Windows 98 LOCKS a running
  exe and Wine does not. So under Wine `imageIsFree()` is true on the first
  poll and the only thing holding gupdate back is the single-instance mutex.
  That is deliberate -- it is why the wait tests two things and not one --
  but it means these tests exercise the mutex half and the real guest has to
  exercise the lock half.

Every test gets its OWN server, because a self-update ends the one it was
given. The teardown reaps by directory: the relaunched gremote is a child of
gupdate, not of pytest, so GuestServer's own terminate() has nothing to
terminate and would leave a process holding the port and the mutex for the
rest of the session.
"""

import os
import signal
import time

import conftest
import pytest

from gremote import Refused, Remote, RemoteError

pytestmark = conftest.needs_guest

SECRET = "not-the-default"


def ini(port: int) -> str:
    return (
        "[server]\n"
        "bind=127.0.0.1\n"
        f"port={port}\n"
        f"secret={SECRET}\n"
        "allow=\n"
        "heartbeat_timeout_s=30\n"
    )


def reap(workdir) -> None:
    """Kill every wine process whose command line names this directory.

    Scanning /proc rather than pkill'ing by name: the suite runs several
    gremotes over its lifetime and killing them by basename would take out
    another module's fixture. The tmp directory is unique per test, so it
    is the precise handle.

    The LAST COMPONENT of it, and not the whole path: a relaunched guest
    program's command line is spelled the guest's way --
    `Z:\\tmp\\pytest-of-...\\<test>\\gremote.exe` -- so matching the unix
    path finds nothing, which is how one survived a run and then refused
    every later fixture with "another copy is already running".
    """
    want = os.path.basename(str(workdir)).encode()
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            with open(f"/proc/{entry}/cmdline", "rb") as f:
                line = f.read()
        except OSError:
            continue
        if want in line and (b"gremote" in line or b"gupdate" in line):
            try:
                os.kill(int(entry), signal.SIGKILL)
            except OSError:
                pass


@pytest.fixture
def box(tmp_path):
    """A gremote under Wine with gupdate.exe beside it, and a live client.

    gupdate is copied in by hand: GuestServer brings one exe and its ini,
    and the whole point of the updater is that it is a SECOND file that has
    to be installed next to the server.
    """
    port = conftest.free_port()
    server = conftest.GuestServer("gremote.exe", tmp_path, ini(port), port)
    import shutil
    shutil.copy(conftest.guest_exe("gupdate.exe"),
                os.path.join(str(tmp_path), "gupdate.exe"))
    with server:
        with Remote("127.0.0.1", SECRET, port=port) as r:
            r.server = server
            r.dir = tmp_path
            r.target = conftest.wine_path(tmp_path / "gremote.exe")
            try:
                yield r
            finally:
                reap(tmp_path)


def named(d, name: str):
    """A directory entry by name, ignoring case, or None.

    FAT ignores case and the guest's names come out however the code that
    wrote them spelled it -- `GREMOTE.OLD` from gupdate's own `.OLD`, next
    to a `gremote.exe` that arrived lower case. On a FAT volume those are
    one namespace; on the ext4 under Wine they are not, and a test that
    hardcoded one spelling would be pinning the spelling rather than the
    behaviour.
    """
    want = name.lower()
    for child in d.iterdir():
        if child.name.lower() == want:
            return child
    return None


def gupdate_log(d) -> str:
    path = named(d, "gupdate.log")
    if path is None:
        return "(no gupdate.log -- the updater never ran, or never started)"
    return path.read_text(errors="replace")


# --- the refusals, every one of which leaves the server running -------------


def still_answering(remote) -> bool:
    try:
        remote.ping()
        return True
    except RemoteError:
        return False


def test_update_without_the_confirmation_is_refused(box):
    """`confirm=replace`, and deliberately not REBOOT's `yes`: a REBOOT
    frame whose type was edited must not come out a valid UPDATE."""
    with pytest.raises(Refused) as e:
        box.request("UPDATE", {"src": box.target})
    assert e.value.code == "unconfirmed"
    assert still_answering(box)


def test_update_with_no_source_is_refused(box):
    with pytest.raises(Refused) as e:
        box.request("UPDATE", {"confirm": "replace"})
    assert e.value.code == "no_source"
    assert still_answering(box)


def test_update_refuses_something_that_is_not_an_image(box):
    """The tooth that keeps this from degrading into a bare QUIT. There is
    no spelling of UPDATE that means "stop": it stops only once it has been
    handed a replacement that reads as a 32-bit PE."""
    staged = box.dir / "notexe.bin"
    box.put(conftest.wine_path(staged), b"this is not a program\r\n" * 100)
    with pytest.raises(Refused) as e:
        box.request("UPDATE", {"confirm": "replace",
                               "src": conftest.wine_path(staged)})
    assert e.value.code == "not_an_image"
    assert still_answering(box)


def test_update_refuses_a_source_that_is_missing(box):
    with pytest.raises(Refused) as e:
        box.request("UPDATE", {"confirm": "replace",
                               "src": conftest.wine_path(
                                   box.dir / "nothing.exe")})
    assert e.value.code == "not_an_image"
    assert still_answering(box)


def test_update_refuses_its_own_image_as_the_source(box):
    """Replacing the running image with itself is the closest thing to a
    bare quit this op has, so it is named and refused rather than allowed
    to fail later in the updater."""
    with pytest.raises(Refused) as e:
        box.request("UPDATE", {"confirm": "replace", "src": box.target})
    assert e.value.code == "same_file"
    assert still_answering(box)


def test_update_refuses_when_the_updater_is_not_installed(tmp_path):
    """No gupdate.exe beside the server, no way to stop it. The absence is
    a refusal and not a fallback -- which is the structural reason UPDATE
    cannot be used to take the machine away from itself."""
    port = conftest.free_port()
    with conftest.GuestServer("gremote.exe", tmp_path, ini(port), port):
        with Remote("127.0.0.1", SECRET, port=port) as r:
            staged = tmp_path / "new.exe"
            with open(conftest.guest_exe("capscheck.exe"), "rb") as f:
                r.put(conftest.wine_path(staged), f.read())
            with pytest.raises(Refused) as e:
                r.request("UPDATE", {"confirm": "replace",
                                     "src": conftest.wine_path(staged)})
            assert e.value.code == "no_updater"
            assert still_answering(r)


# --- the whole flow ---------------------------------------------------------


def test_self_update_installs_the_new_build_and_it_answers_again(box):
    """End to end: stage, stop, swap, relaunch, reconnect, report.

    The "new build" is gremote.exe with a kilobyte of zeroes appended. A PE
    with an overlay still loads (checked), and the extra bytes are what
    makes the installed image distinguishable from the one it replaced --
    without them `matches` would be true whether or not anything happened.
    """
    with open(conftest.guest_exe("gremote.exe"), "rb") as f:
        payload = f.read() + b"\0" * 1024
    newbuild = box.dir / "newbuild.exe"
    newbuild.write_bytes(payload)

    res = box.self_update(str(newbuild), target=box.target)

    log = gupdate_log(box.dir)
    assert res.matches, (res, log)
    assert not res.rolled_back, (res, log)
    assert res.helper_pid > 0, res
    assert res.hello.get("ver"), res.hello
    # The files, read straight off the host: under Wine the guest's
    # directory IS this directory.
    assert named(box.dir, "gremote.exe").read_bytes() == payload, log
    assert named(box.dir, "gremote.old"), ("no way back was kept", log)
    assert not named(box.dir, "gremote.upd"), ("the staged image was left "
                                               "behind", log)


def test_the_old_build_is_kept_under_a_name_a_person_can_type(box):
    """GREMOTE.EXE -> GREMOTE.OLD, the extension replaced rather than
    appended: `GREMOTE.EXE.OLD` has no 8.3 spelling and the one FAT invents
    is not a name anybody can type back."""
    before = named(box.dir, "gremote.exe").read_bytes()
    with open(conftest.guest_exe("gremote.exe"), "rb") as f:
        payload = f.read() + b"\0" * 2048
    newbuild = box.dir / "newbuild.exe"
    newbuild.write_bytes(payload)

    box.self_update(str(newbuild), target=box.target)
    assert named(box.dir, "gremote.old").read_bytes() == before, \
        gupdate_log(box.dir)


def test_a_replacement_that_does_not_stay_up_is_rolled_back(box):
    """The rollback this platform needs, driven for real.

    capscheck.exe is a genuine 32-bit image -- so it passes UPDATE's
    validation, which is the point: the server cannot tell a build that
    works from one that does not, and should not pretend to. It then starts
    and exits at once, which is what an NT-only import at load looks like
    from the updater's side. gupdate waits `-verify` for it, sees it go, and
    puts GREMOTE.OLD back.

    The machine answering afterwards is the assertion. `rolled_back` is how
    this side finds out, because the server that answers cannot tell you
    it is not the one you sent.
    """
    original = named(box.dir, "gremote.exe").read_bytes()
    broken = conftest.guest_exe("capscheck.exe")

    res = box.self_update(broken, target=box.target)

    log = gupdate_log(box.dir)
    assert res.rolled_back, (res, log)
    assert "ROLLING BACK" in log, log
    assert named(box.dir, "gremote.exe").read_bytes() == original, log
    # The image that failed is still where it was staged, because it is the
    # only copy of the thing somebody now has to look at.
    assert named(box.dir, "gremote.upd"), log
    # And the box is usable.
    with Remote("127.0.0.1", SECRET, port=box.port) as r:
        assert r.sysinfo().mem_kb > 0


def test_the_reconnect_is_reported_rather_than_assumed(box):
    """The whole reason this flow is code and not three calls: a self-update
    you cannot confirm is one you have to walk over and look at."""
    with open(conftest.guest_exe("gremote.exe"), "rb") as f:
        payload = f.read() + b"\0" * 512
    newbuild = box.dir / "newbuild.exe"
    newbuild.write_bytes(payload)

    before = time.monotonic()
    res = box.self_update(str(newbuild), target=box.target)
    assert 0 < res.reconnect_s <= time.monotonic() - before
    assert res.target.lower().endswith("gremote.exe")
    assert res.backup.lower().endswith("gremote.old")
    assert res.staged.lower().endswith("gremote.upd")
