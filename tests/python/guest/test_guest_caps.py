"""03.12 and 03.19: a capability enum value can be RE-MEANT without
detection, and the Win9x capability defaults are written more than once with
no cross-check.

`win/net/caps.h` defines `RC_CASE_*`/`RC_DEVICES_*`/`RC_EOL_*` etc as small
integers; `bridge/files/caps.py` mirrors them as `CASE_*`/`DEVICES_*`/
`EOL_*`. `caps.py`'s `_enum` only refuses a value OUTSIDE its allowed tuple,
so a RENUMBERING is caught (the wire carries a value nobody declared) but a
RE-MEANING -- the same integer, a different meaning on each side, e.g.
`eol=2` stops meaning "classic Mac" on one side while the other still reads
it that way -- is accepted silently. Only the integer ever crosses the wire;
nothing ever compares the two sides' NAMES for it.

There is no wire behaviour that depends on which meaning a value has, only on
which meaning each side BELIEVES it has, so this cannot be proven the way
synccheck's --limits fixture proves a length boundary by exercising real
enforcement. What can be proven is that the two headers agree: `capscheck.exe`
prints each `RC_*` name next to its real value, by stringifying the macro
name rather than retyping it (tests/guest/capscheck.c), and this file asserts
each one equals the bridge constant of the matching name.
"""
from __future__ import annotations

import conftest
import pytest

from bridge.files import caps as C

pytestmark = conftest.needs_guest

# guest name (win/net/caps.h) -> the bridge constant it must equal
# (bridge/files/caps.py). Explicit rather than derived by string-munging the
# `RC_` prefix off, because the two sides do not even spell these the same
# way -- RC_TRAILING_STRIP vs TRAILING_STRIPPED -- and a mapping that had to
# guess the bridge name would reintroduce exactly the kind of drift this test
# exists to catch.
EXPECTED = {
    "RC_CASE_SENSITIVE":    C.CASE_SENSITIVE,
    "RC_CASE_FOLD":         C.CASE_FOLD,
    "RC_DEVICES_NONE":      C.DEVICES_NONE,
    "RC_DEVICES_DOS":       C.DEVICES_DOS,
    "RC_TRAILING_KEPT":     C.TRAILING_KEPT,
    "RC_TRAILING_STRIP":    C.TRAILING_STRIPPED,
    "RC_ILLEGAL_POSIX":     C.ILLEGAL_POSIX,
    "RC_ILLEGAL_DOS":       C.ILLEGAL_DOS,
    "RC_EOL_LF":            C.EOL_LF,
    "RC_EOL_CRLF":          C.EOL_CRLF,
    "RC_EOL_CR":            C.EOL_CR,
    "RC_DIRRESERVE_NONE":   C.DIRRESERVE_NONE,
    "RC_DIRRESERVE_WIN32":  C.DIRRESERVE_WIN32,
    "RC_WORKRESERVE_NONE":  C.WORKRESERVE_NONE,
    "RC_WORKRESERVE_GASTON": C.WORKRESERVE_GASTON,
    # Declared on both sides since the handshake was written and never
    # cross-checked, because the bridge parsed the header only far enough to
    # count it as "declared" and kept no field to compare against.
    "RC_SHORTNAMES_NONE":   C.SHORTNAMES_NONE,
    "RC_SHORTNAMES_83":     C.SHORTNAMES_83,
    "RC_DEFLATE_NONE":      C.DEFLATE_NONE,
    "RC_DEFLATE_STREAM":    C.DEFLATE_STREAM,
    # Not an enum: the guest's own walk depth, which the bridge assumes when
    # a client does not declare one. If these drift, the bridge refuses paths
    # the guest would have scanned, or accepts ones it will silently stop
    # reporting -- the failure that reads as "deleted on the guest" forever.
    "RC_CAP_MAXDEPTH":      C.MAXDEPTH_GASTON,
    # Same shape: the too_many message quotes this number, so a drift means
    # the user is told the wrong limit.
    "RC_CAP_MAXFILES":      C.MAXFILES_GASTON,
}


@pytest.fixture(scope="module")
def guest_caps():
    out = conftest.run_guest("capscheck.exe")
    rows = {}
    for line in out.stdout.decode("latin-1").splitlines():
        parts = line.split("\t")
        if len(parts) == 2:
            rows[parts[0]] = int(parts[1])
    assert rows, out.stderr.decode("latin-1", "replace")[:800]
    return rows


def test_every_expected_name_was_printed(guest_caps):
    missing = set(EXPECTED) - set(guest_caps)
    assert not missing, f"capscheck.exe did not print: {sorted(missing)}"


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_guest_value_matches_the_bridge(name, guest_caps):
    assert guest_caps[name] == EXPECTED[name], (
        f"{name}={guest_caps[name]} on the guest but "
        f"bridge/files/caps.py expects {EXPECTED[name]} -- the two sides "
        f"would parse the same wire integer as two different capabilities")


# --- 03.19: "what this client IS" vs "what the bridge assumes an
# undeclaring client is" ------------------------------------------------
#
# RC_CAP_CASE etc (win/net/caps.h) are what a real Win98 build actually puts
# in HELLO. `Caps()`'s field defaults (bridge/files/caps.py) are what the
# bridge assumes when a client does not declare at all -- and caps.py's own
# module docstring says an absent field means "the deployed Win9x client,
# which is the only client that has ever connected without declaring", so
# these two are supposed to be the SAME machine described twice.
#
# The enum-name checks above only prove RC_CASE_FOLD's own value didn't
# move; they would not catch RC_CAP_CASE being re-pointed at RC_CASE_SENSITIVE
# on the C side while nothing on the Python side changed at all. This checks
# the alias's target directly, against a real `Caps()` instance rather than
# against another copy of the literals.
_DEFAULT_CAPS = C.Caps()

CAP_EXPECTED = {
    "RC_CAP_CASE":       _DEFAULT_CAPS.case,
    "RC_CAP_DEVICES":    _DEFAULT_CAPS.devices,
    "RC_CAP_TRAILING":   _DEFAULT_CAPS.trailing,
    "RC_CAP_ILLEGAL":    _DEFAULT_CAPS.illegal,
    "RC_CAP_EOL":        _DEFAULT_CAPS.eol,
    "RC_CAP_DIRRESERVE": _DEFAULT_CAPS.dirreserve,
    "RC_CAP_WORKRESERVE": _DEFAULT_CAPS.workreserve,
    "RC_CAP_MAXPATH":    _DEFAULT_CAPS.maxpath,
    "RC_CAP_MAXCOMP":    _DEFAULT_CAPS.maxcomp,
}


def test_every_cap_default_was_printed(guest_caps):
    missing = set(CAP_EXPECTED) - set(guest_caps)
    assert not missing, f"capscheck.exe did not print: {sorted(missing)}"


@pytest.mark.parametrize("name", sorted(CAP_EXPECTED))
def test_this_client_matches_what_the_bridge_assumes_it_is(name, guest_caps):
    assert guest_caps[name] == CAP_EXPECTED[name], (
        f"{name}={guest_caps[name]} is what this client actually declares, "
        f"but Caps() -- what the bridge assumes an undeclaring client is -- "
        f"expects {CAP_EXPECTED[name]}. Those are meant to be the same "
        f"Win9x machine described twice (bridge/files/caps.py's module "
        f"docstring); a live guest and an assumed one would now disagree.")

