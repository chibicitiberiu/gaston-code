"""The C++ prelude's guards, run as the shipping code under Wine.

A guard that is wrong is worse than no guard, because it is trusted. These
diff `win/checks/cppcheck.cpp`'s verdicts rather than reading the header and
believing it.

Every case is drawn from a bug this codebase has actually had. The one that
matters most is `one-over-refused` at EQUALITY: `b14be6d` was a directory
whose search pattern was exactly one byte short, and a checked append that
only caught overflow would have missed it.
"""

import os
import subprocess

import pytest

import conftest
from conftest import wine_env

EXE_NAME = "cppcheck.exe"

# Only the genuinely-unsupported machine skips. A missing binary with
# the toolchain present is a build omission and fails loudly -- see
# conftest.guest_exe.
pytestmark = conftest.needs_guest


@pytest.fixture(scope="module")
def verdicts():
    env = wine_env()
    out = subprocess.run([conftest.WINE, conftest.guest_exe(EXE_NAME)], capture_output=True, timeout=300, env=env)
    rows = {}
    for line in out.stdout.decode("latin-1").splitlines():
        parts = line.split("\t")
        if len(parts) == 3:
            rows[parts[1]] = (parts[0], parts[2])
    assert rows, out.stderr.decode("latin-1", "replace")[:800]
    return rows


def test_every_guard_passes(verdicts):
    bad = {k: v for k, v in verdicts.items() if v[0] != "pass"}
    assert not bad, bad


def test_truncation_is_refused_at_the_limit_not_past_it(verdicts):
    """b14be6d turned on exactly this: one byte short lost a whole subtree."""
    assert verdicts["exact-fit"][0] == "pass"
    assert verdicts["one-over-refused"][0] == "pass"


def test_destructors_fire_on_every_path(verdicts):
    """RAII is only worth having if the early-return path is covered -- that
    is the path where the paired operation was being missed in the C."""
    assert verdicts["dtor-on-early-return"][1] == "ba"
    assert verdicts["dtor-reverse-order"][1] == "bca"


def test_ownership_transfer_does_not_double_release(verdicts):
    """A copied guard releases twice, and CloseHandle on a recycled handle is
    worse than a leak -- so the guards are non-copyable and hand ownership on
    explicitly."""
    assert verdicts["handle-release-keeps"][0] == "pass"
    assert verdicts["heapptr-release"][0] == "pass"
