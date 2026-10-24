"""Shared setup for the suites that run the SHIPPING guest C under Wine.

Two problems, one file.

**The Wine path was copy-pasted into every suite that needed it** -- the
runner, the prefix and the build directory, three times, drifting
independently. They live here now.

**And those suites skipped SILENTLY.** `pytest.mark.skipif` on
`not os.path.exists(EXE)` cannot tell "this contributor has no Open Watcom"
from "somebody ran `make clean` and forgot to rebuild". The second is a build
omission and should be loud; instead the run went from 2102 passing to "2031
passed, 75 skipped" and reported success. That happened during the audit work
itself, which is how it was noticed: the cross-language agreement checks -- the
only thing proving the guest's classifier and the bridge's still agree -- had
stopped running and nothing said so.

So the rule here is: **a missing binary is a FAILURE when the toolchain that
builds it is present.** Skipping is reserved for a machine that genuinely
cannot build or run the guest.
"""

import os
import shutil
import socket
import subprocess
import tempfile
import time

import pytest

#: The repo root. One spelling, because there were six: three chained
#: `os.path.dirname` calls here and in two other suites, `parents[2]` in four
#: more, and `__import__("os").path.dirname` in the tools. They all meant the
#: same directory and they all counted levels by hand, so moving a test file
#: one directory deeper broke four of them in different ways.
REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

#: Kept because suites import it by name. REPO is the one to reach for.
HERE = REPO

#: Where `make -C win` puts its output. Overridable so a different BUILD works.
BUILD = os.environ.get("BUILD", os.path.join(HERE, "build", "win"))

#: The project uses Lutris's wine-ge build, not a system wine: a standalone
#: Wine with full 32-bit support rather than Proton.
WINE = os.environ.get(
    "WINE",
    os.path.expanduser(
        "~/.local/share/lutris/runners/wine/wine-ge-8-26-x86_64/bin/wine"),
)
#: OUTSIDE THE WORKSPACE, deliberately, and this is not tidiness.
#:
#: A wine prefix contains `dosdevices/z: -> /`. Any recursive walk that follows
#: symlinks therefore re-enters the repo through it, forever:
#:
#:     <prefix>/dosdevices/z:/home/tibi/Dev/RetroAI/<prefix>/dosdevices/z:/...
#:
#: With the prefix inside the workspace that loop is what pinned VS Code's
#: extension host and cpptools at gigabytes -- cpptools walks
#: ${workspaceFolder} recursively for its browse database and followed z:
#: straight out into /. `.vscode/settings.json` carries excludes for it, but an
#: exclude is per-tool whack-a-mole: ripgrep, file watchers, `find`, backup
#: tools and every future extension each need their own, and one that has not
#: heard of it walks the whole filesystem. Keeping 222MB of fake C: drive out
#: of the tree fixes it once, for everything.
#:
#: XDG data rather than cache: losing it means re-initialising a prefix and
#: re-running the Wine gate, which is minutes, not seconds. WINEPREFIX still
#: overrides, so CI or a second checkout can point somewhere else.
PREFIX = os.environ.get(
    "WINEPREFIX",
    os.path.join(
        os.environ.get("XDG_DATA_HOME",
                       os.path.expanduser("~/.local/share")),
        "retroclaude", "wineprefix"),
)

#: The compiler that builds the guest. Its presence is what turns "the binary
#: is missing" from an excuse into a bug.
WATCOM = os.environ.get("WATCOM", "/usr/bin/watcom")
_WCC = os.path.join(WATCOM, "binl64", "wcc386")


def have_toolchain() -> bool:
    return os.path.exists(_WCC) and os.path.exists(WINE)


def guest_exe(name: str) -> str:
    """Path to a guest check binary, insisting it was actually built.

    Raises rather than skips when the toolchain is present, because at that
    point a missing binary means the build step was skipped, not that this
    machine cannot run the test.
    """
    path = os.path.join(BUILD, name)
    if os.path.exists(path):
        return path
    if have_toolchain():
        target = name.replace(".exe", "")
        pytest.fail(
            f"{path} is missing, but Open Watcom and Wine are both present -- "
            f"so this is an unbuilt binary, not an unsupported machine. "
            f"Run `make -C win {target}` (or `make wine-smoke`, which builds "
            f"what it needs). Skipping here would hide the only check that "
            f"the guest's C and the bridge's Python still agree.")
    pytest.skip("needs Open Watcom and the Lutris wine-ge runner")


def wine_env() -> dict:
    return dict(os.environ, WINEPREFIX=PREFIX, WINEDEBUG="-all")


#: Suites that need a guest binary mark themselves with this rather than
#: repeating the condition. Two marks, and both matter:
#:
#:   `guest` is the SELECTOR. It is what lets `make test-fast` skip the ten
#:   Wine suites and finish in 15 seconds instead of three minutes, and what
#:   lets `make wine-smoke` run exactly those ten without a hand-maintained
#:   list of filenames in the Makefile. That list had already drifted: it was
#:   missing test_guest_workcopy.py and test_tooldet_agreement.py, so two
#:   suites were in nobody's gate.
#:
#:   `skipif` is the CONDITION, and it covers only the genuinely-unsupported
#:   case -- see the module docstring on why a missing binary is a failure
#:   rather than a skip.
#:
#: `pytestmark` takes a list, so a suite still writes one line.
needs_guest = [
    pytest.mark.guest,
    pytest.mark.skipif(
        not have_toolchain(),
        reason="needs Open Watcom and the Lutris wine-ge runner",
    ),
]


# --- running a guest check binary -------------------------------------------
#
# Ten suites spawn a Wine process, and all ten spelled it out: the runner, the
# binary, `capture_output=True`, an env from `wine_env()`, a timeout, and then
# `out.stderr.decode("latin-1", "replace")[:800]` in the assertion message.
# Ten copies of one call that drift apart -- three different timeouts for the
# same class of binary, and two suites that forgot `timeout=` entirely and
# would hang the suite rather than fail it.


def wine_path(p) -> str:
    """A host path as the guest sees it through Wine's Z: drive.

    Four copies of this line existed, one of them in a suite that also wrote
    it inline a second time. It looks too small to share right up until a
    path with a forward slash in it reaches a binary that splits on
    backslashes.
    """
    return "Z:" + str(p).replace("/", "\\")


def run_guest(name: str, *args, cwd=None, timeout: float = 60,
              text: bool = False, env: dict | None = None):
    """Run a guest check binary under Wine and hand back the CompletedProcess.

    `name` is the binary, e.g. "synccheck.exe" -- resolved through `guest_exe`,
    so a missing binary is still the loud failure described above rather than
    a silent skip.

    A timeout is not optional here and the default is deliberate. Wine can
    hang on a guest binary that is waiting for something that will never
    arrive, and a hung test takes the whole suite with it; `test_guest_net.py`
    needs a longer one and passes it, which is the point of the parameter.
    """
    argv = [WINE, guest_exe(name), *[str(a) for a in args]]
    if cwd is not None:
        return subprocess.run(argv, capture_output=True, text=text,
                              timeout=timeout, cwd=str(cwd),
                              env=env if env is not None else wine_env())
    with tempfile.TemporaryDirectory(prefix="rc-guest-") as tmpdir:
        return subprocess.run(argv, capture_output=True, text=text,
                              timeout=timeout, cwd=tmpdir,
                              env=env if env is not None else wine_env())


def run_guest_at(path: str, *args, cwd=None, timeout: float = 60,
                 text: bool = False, env: dict | None = None):
    """As `run_guest`, for a binary at an ARBITRARY path.

    `run_guest` resolves its name inside BUILD, which is right for every
    check binary. This exists for the one case that needs the exe somewhere
    else: probe.exe finds CORPUS.TXT beside ITSELF via GetModuleFileNameA, so
    testing the no-corpus refusal means putting a copy of the exe in a
    directory that has no corpus in it. A bare cwd would not do -- the real
    build directory has the corpus, and the probe would find it anyway.
    """
    argv = [WINE, path, *[str(a) for a in args]]
    return subprocess.run(argv, capture_output=True, text=text,
                          timeout=timeout,
                          cwd=str(cwd) if cwd is not None else None,
                          env=env if env is not None else wine_env())


def guest_says(out) -> str:
    """What the binary printed, for an assertion message.

    latin-1 because the guest emits OEM bytes and this must never raise while
    building the explanation of a failure -- a decode error here replaces the
    real failure with a UnicodeDecodeError, which is how you spend an hour on
    the wrong bug. Capped, because a check binary that goes wrong goes wrong
    at length.
    """
    err = out.stderr if isinstance(out.stderr, str) else \
        out.stderr.decode("latin-1", "replace")
    return err[:800]


def guest_verdicts(name: str, *args, cwd=None, timeout: float = 300) -> dict:
    """Run a check binary that prints `verdict<TAB>case<TAB>detail` rows.

    `cppcheck` and `rpccheck` print exactly this and had byte-identical
    parsers. `capscheck` does NOT -- it prints `name<TAB>number` -- and this
    helper was first written assuming all four agreed, with the columns the
    wrong way round into the bargain. Worth recording: the shared shape was
    taken from a survey and not checked against the files, which would have
    turned every verdict into its own key and passed, because the assertion
    below only asks that SOMETHING parsed.

    That assertion is still the load-bearing part. A binary that prints
    nothing parses to `{}`, and a suite that then iterates over `{}` passes
    every assertion it makes while checking nothing at all.
    """
    out = run_guest(name, *args, cwd=cwd, timeout=timeout)
    rows = {}
    for line in out.stdout.decode("latin-1", "replace").splitlines():
        parts = line.split("\t")
        if len(parts) == 3:
            rows[parts[1]] = (parts[0], parts[2])
    assert rows, f"{name} printed no verdicts:\n{guest_says(out)}"
    return rows


# --- a guest binary that STAYS UP -------------------------------------------
#
# run_guest above is subprocess.run: start a binary, wait for it to finish,
# read what it printed. Every guest binary until now has been a client or a
# one-shot check, so that was the whole shape.
#
# gremote is neither. It listens and the test is the caller, which is the
# direction docs/REMOTE.md flagged as having no precedent here -- wirecheck
# proves libwire's server half with both ends inside one Wine process, and
# test_guest_net.py crosses the boundary with Python listening and Wine
# dialling out. Nothing had yet connected INTO a socket held by a Wine
# process.
#
# It is also a GUI-subsystem image, so it prints nothing: gremote.log beside
# the exe is the diagnostic, and log() is what an assertion message shows.
#
# There is no remote QUIT, deliberately (REMOTE.md: "stopping gremote
# remotely is EXEC of something that kills it, which is deliberate
# friction"), so stopping it is killing the process.

def free_port() -> int:
    """A port nothing is listening on, as far as anyone can tell.

    Inherently a guess -- the socket is closed before the guest binds it --
    but the alternative is parsing a port back out of the log, and the guest
    has to be told which port to bind before it can report one.
    """
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class GuestServer:
    """A guest exe running under Wine with a port open, as a context manager.

    `ini` is written beside a copy of the exe in `workdir`, under BOTH the
    long name and the 8.3 one -- not because Wine needs it, but because the
    guest does and a test that only ever writes one name would not catch the
    day that changes (notes/gremote-plan.md, trap 1).
    """

    def __init__(self, exe_name: str, workdir, ini: str, port: int,
                 ini_name: str = "gremote.ini",
                 ready: str = "server: listening on"):
        self.exe_name = exe_name
        self.dir = str(workdir)
        self.port = port
        self.ready = ready
        self.proc = None
        shutil.copy(guest_exe(exe_name), os.path.join(self.dir, exe_name))
        body = ini.replace("\n", "\r\n").encode("latin-1")
        for name in (ini_name, ini_name.split(".")[0][:8].upper() + ".INI"):
            with open(os.path.join(self.dir, name), "wb") as f:
                f.write(body)

    def log(self) -> str:
        path = os.path.join(self.dir, self.exe_name.replace(".exe", ".log"))
        try:
            with open(path, errors="replace") as f:
                return f.read()
        except OSError as e:
            return f"(no log: {e})"

    def refusal(self, timeout: float = 60) -> tuple[int, str]:
        """Start it expecting it to REFUSE to run, and hand back the exit
        code and the log. For the settings that must stop a machine coming up
        listening at all -- there is no socket to ask in that case, and the
        log is the whole diagnostic."""
        proc = subprocess.Popen(
            [WINE, os.path.join(self.dir, self.exe_name)],
            cwd=self.dir, env=wine_env(),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            code = proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)
            pytest.fail(f"{self.exe_name} kept running; it should have "
                        f"refused to start\n{self.log()}")
        return code, self.log()

    def __enter__(self):
        self.proc = subprocess.Popen(
            [WINE, os.path.join(self.dir, self.exe_name)],
            cwd=self.dir, env=wine_env(),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        # Readiness comes from the LOG, not from a test connection.
        # gremote serves one client at a time, so a probe that connects and
        # hangs up is a client: it takes the slot, and the next connection --
        # the actual test -- is answered `busy` until the accept loop notices
        # the hangup and tears the session down. Every test in the suite
        # failed that way once. The log is opened FILE_SHARE_READ precisely
        # so it can be read while the guest is writing it.
        deadline = time.time() + 40
        while time.time() < deadline:
            if self.proc.poll() is not None:
                pytest.fail(f"{self.exe_name} exited with "
                            f"{self.proc.returncode} before it was ready\n"
                            f"{self.log()}")
            if self.ready in self.log():
                return self
            time.sleep(0.2)
        self.__exit__(None, None, None)
        pytest.fail(f"{self.exe_name} never logged {self.ready!r}\n"
                    f"{self.log()}")

    def __exit__(self, *exc):
        if self.proc is None:
            return
        self.proc.terminate()
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(timeout=10)
        self.proc = None
