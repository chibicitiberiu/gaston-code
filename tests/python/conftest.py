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

import pytest

HERE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

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
#: repeating the condition. It covers only the genuinely-unsupported case.
needs_guest = pytest.mark.skipif(
    not have_toolchain(),
    reason="needs Open Watcom and the Lutris wine-ge runner",
)
