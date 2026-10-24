"""The import gate must be able to go red.

`make -C win check` used to PRINT the DLL list, the W-suffix imports and the
OS/subsystem version -- three `objdump | grep` pipelines that exit 0 whatever
they find. So CLAUDE.md's "`make check` audits imports", guarding the one
failure mode it calls undebuggable (an NT-only import kills the image at load,
before WinMain, with no log), was a human reading scrollback. In CI or under
`make all` a regression went green. (audit 01.29, and 03.1/04.6.)

The W-suffix grep also aimed at the wrong class entirely: `MoveFileExA` is
ANSI, has no `W` suffix, lives in KERNEL32 and does not exist on 9x --
`win/sync/sync.cpp` says in a comment that somebody will want it to make the
commit atomic.

These tests drive `tools/check/imports.py` against synthetic import tables, so
they assert the GATE's behaviour rather than today's binary. A gate that
cannot fail is indistinguishable from one that passes, which is the whole
point of the finding.
"""

import importlib.util
import os

import conftest
import pytest

GATE = os.path.join(conftest.REPO, "tools", "check", "imports.py")


@pytest.fixture(scope="module")
def gate():
    spec = importlib.util.spec_from_file_location("rc_imports_gate", GATE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def baseline(gate):
    """The real binary's imports, so the synthetic cases stay realistic."""
    exe = os.path.join(conftest.REPO, "build", "win", "gaston.exe")
    if not os.path.exists(exe):
        pytest.skip("gaston.exe not built")
    return gate.imports(exe)


def _run(gate, monkeypatch, dlls, syms):
    monkeypatch.setattr(gate, "imports", lambda exe: (dlls, syms))
    exe = os.path.join(conftest.REPO, "build", "win", "gaston.exe")
    return gate.main(["imports.py", exe])


def test_the_real_binary_passes(gate, baseline, monkeypatch):
    d, s = baseline
    assert _run(gate, monkeypatch, d, s) == 0


@pytest.mark.parametrize("sym", [
    # The one sync.cpp's own comment says somebody will reach for.
    "MoveFileExA",
    # The ones other files document avoiding.
    "SetHandleInformation",
    "GetDiskFreeSpaceExA",
    # PLAN.md R6 names this as NT-only.
    "ReadDirectoryChangesW",
    "CreateHardLinkA",
    "GetFileAttributesExA",
])
def test_an_nt_only_import_fails_the_gate(gate, baseline, monkeypatch, sym):
    """Each of these loads fine under Wine and kills the image on Win98."""
    d, s = baseline
    assert _run(gate, monkeypatch, d, s | {sym}) == 1, (
        f"{sym} passed the gate; it would die at load on the guest")


def test_a_new_dll_dependency_fails_the_gate(gate, baseline, monkeypatch):
    d, s = baseline
    assert _run(gate, monkeypatch, d | {"PSAPI.DLL"}, s) == 1


def test_an_unexpected_w_suffix_fails_the_gate(gate, baseline, monkeypatch):
    d, s = baseline
    assert _run(gate, monkeypatch, d, s | {"CreateFileW"}) == 1


def test_the_two_known_w_imports_are_still_accepted(gate, baseline,
                                                    monkeypatch):
    """GetCommandLineW and GetModuleFileNameW are called by the CRT startup
    and their results discarded -- measured in
    notes/audit/00-cpp-toolchain-measured.md and unchanged by the C++
    migration. They must not start failing the build."""
    d, s = baseline
    assert {"GetCommandLineW", "GetModuleFileNameW"} <= s
    assert _run(gate, monkeypatch, d, s) == 0


def test_the_gate_reads_symbol_names_not_ordinals(gate, baseline):
    """objdump prints "<addr> <none> <ordinal> <name>".

    Taking the field straight after `<none>` collects ORDINALS, which collide
    across DLLs and can never match a deny-list entry -- the gate would report
    "ok" for a binary importing every NT-only symbol there is. This caught
    itself during development: 199 import entries came back as 96 "names".
    """
    _, syms = baseline
    assert "CreateFileA" in syms
    assert not any(s.isdigit() for s in syms), \
        "the gate is parsing ordinals, not symbol names"
