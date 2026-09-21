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
import subprocess

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
PREFIX = os.environ.get("WINEPREFIX", os.path.join(HERE, ".wineprefix"))

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
              text: bool = False, env: dict = None):
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
    return subprocess.run(argv, capture_output=True, text=text,
                          timeout=timeout, cwd=str(cwd) if cwd else None,
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


def guest_verdicts(name: str, *args, timeout: float = 300) -> dict:
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
    out = run_guest(name, *args, timeout=timeout)
    rows = {}
    for line in out.stdout.decode("latin-1", "replace").splitlines():
        parts = line.split("\t")
        if len(parts) == 3:
            rows[parts[1]] = (parts[0], parts[2])
    assert rows, f"{name} printed no verdicts:\n{guest_says(out)}"
    return rows
