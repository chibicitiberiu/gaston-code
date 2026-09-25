"""03.53: probe.exe had no automated driver at all, same gap as netcheck.exe
-- it printed its own free-text PASS:/FAIL: verdicts (and, unlike netcheck.c,
`main()` always returns 0 regardless of what they say) and was run by a
human, or not at all.

Most of probe.c answers questions Wine cannot honestly answer FOR this
project -- "Wine proves the code, never the platform" cuts both ways, and
`probe screen`/`probe watch`/`probe html` are exactly the platform questions
that rule exists for: MSHTML hosting cost, directory-change notifications and
BI_RLE8 support are real Win98 behaviour that a Wine prefix has no reason to
reproduce, so a driver asserting on them would fail on Wine's platform
differences and prove nothing about the guest's code. See docs/PROGRESS.md
and win/checks/probe.c's own section comments for why each section exists.

`probe deflate` is the one section that is pure code: it round-trips the
SHIPPING miniz (win/third_party/miniz, the same library net.cpp and sync.cpp
link) through compress and decompress and compares the bytes. That has no
hardware dependency, so it is what this driver actually asserts on -- the
same "run the real check binary, do not re-implement it" discipline as
synccheck/rpccheck/framecheck, just for a section that happens to be a single
line of output rather than a table.
"""
from __future__ import annotations

import conftest

pytestmark = conftest.needs_guest


def test_probe_deflate_round_trips_the_shipping_miniz(tmp_path):
    """`probe deflate` only -- the other sections ask questions real Win98
    hardware has to answer; see the module docstring."""
    out = conftest.run_guest("probe.exe", "deflate", cwd=tmp_path)
    text = out.stdout.decode("latin-1", "replace")
    assert "round trip" in text, (
        f"probe.exe did not reach the deflate section:\n{text}\n"
        f"stderr:\n{out.stderr.decode('latin-1', 'replace')}")
    assert "round trip     : PASS" in text, (
        f"the shipping miniz did not round-trip:\n{text}")
    assert "round trip     : FAIL" not in text, text
