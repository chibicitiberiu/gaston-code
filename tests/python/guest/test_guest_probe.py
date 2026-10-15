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

It also needs CORPUS.TXT beside the exe, as of stage 5. `make -C win probe`
builds the corpus next to probe.exe for exactly that reason, and the second
test here is the on-path test for the refusal when it is absent -- a switch
whose on-path nothing exercises is the failure this project keeps finding,
and this one was found by THIS test going red when the refusal landed.
"""
from __future__ import annotations

import os
import shutil

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
    # The corpus it actually read, so a run of this test and a run on real
    # hardware can be told apart -- or confirmed to be the same bytes.
    assert "crc32" in text, f"the corpus line lost its crc32:\n{text}"


def test_probe_deflate_refuses_without_a_corpus(tmp_path):
    """No CORPUS.TXT beside the exe means a refusal, not a fallback.

    The synthetic sample this section used to build compressed 164:1, and its
    throughput was quoted as a threshold for a year by four documents --
    win/checks/probe.c's own comment said in as many words that it was not
    usable. A fallback is how that happened: it printed a plausible figure
    under the same heading and nothing downstream could tell the two apart.

    So the refusal is load-bearing and gets its own test. probe.exe is copied
    somewhere with no corpus beside it, because the path comes from
    GetModuleFileNameA rather than the working directory -- running the built
    exe from a bare cwd would still find the corpus in build/win.
    """
    exe = shutil.copy(conftest.guest_exe("probe.exe"),
                      os.path.join(tmp_path, "probe.exe"))
    assert not os.path.exists(os.path.join(tmp_path, "CORPUS.TXT"))
    out = conftest.run_guest_at(exe, "deflate", cwd=tmp_path)
    text = out.stdout.decode("latin-1", "replace")
    assert "REFUSED" in text, f"expected a refusal, got:\n{text}"
    assert "no CORPUS.TXT beside this exe" in text, text
    # And it must NOT have measured anything, which is the whole point.
    assert "round trip" not in text, (
        f"it fell back to a synthetic sample instead of refusing:\n{text}")
