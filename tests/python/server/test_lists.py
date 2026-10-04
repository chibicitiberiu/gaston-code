"""The Options inspector: GETLISTS, LISTENT, LISTEND.

Transparency was the person's own ask: every list that decides which row of
a mode a call lands in is shown with why each entry is there and which file
says so, and a line can be tried against the lists and mode the person is
ABOUT to save -- not the ones in force, or "Try it" would answer for a
configuration they have already changed.
"""
from __future__ import annotations

import pytest  # noqa: F401
from harness import harness  # noqa: F401

from bridge.wire.protocol import Frame


async def _lists(harness, r, w, headers, body=b""):
    await harness.send(w, Frame("GETLISTS", headers, body))
    frames = await harness.recv_until(r, {"LISTEND"}, 5.0)
    rows = []
    for f in frames:
        if f.type == "LISTENT":
            rows += [ln.split("\t") for ln in
                     f.body.decode("cp1252").splitlines() if ln]
    return rows, frames[-1]


async def test_the_shell_list_says_what_each_verb_does_and_where(harness):
    r, w = await harness.connect(prefs=b"")
    await harness.recv_until(r, {"OPTIONSEND"})
    rows, end = await _lists(harness, r, w, {"list": "shell"})
    fmt = [x for x in rows if x[0] == "format"]
    assert fmt and "volume" in fmt[0][1]
    assert fmt[0][2].startswith("bridge: profiles/")
    assert any(x[0] == "dir" and x[1] == "only reads" for x in rows)
    assert end.headers["hidden"] == "0"
    await harness.close(w)


async def test_the_ignore_list_names_the_bridge_and_the_client(harness):
    r, w = await harness.connect(prefs=b"sync.ignore=*.bak")
    await harness.recv_until(r, {"OPTIONSEND"})
    rows, _ = await _lists(harness, r, w, {"list": "ignore"})
    assert any(x[0] == ".git/" and "ignores.py" in x[2] for x in rows)
    assert any(x[0] == "*.bak" and "gaston.ini" in x[2] for x in rows)
    await harness.close(w)


async def test_a_test_line_uses_the_pending_lists_and_mode(harness):
    """Saved: nothing protected. Pending (the sheet's copy): C:\\WINDOWS
    protected and a mode of the person's own. The answer is for the
    pending one."""
    r, w = await harness.connect(prefs=b"")
    await harness.recv_until(r, {"OPTIONSEND"})
    harness.bridge.connection.guest.work_guest = "C:\\WORK"
    pending = (b"copy a.c C:\\WINDOWS\\X.C\n"
               b"protect=C:\\WINDOWS\\**\n"
               b"mode.u-0a1b2c3d.label=Mine\n"
               b"mode.u-0a1b2c3d.c.files_sys=off\n")
    _, end = await _lists(harness, r, w, {"kind": "exec",
                                          "mode": "u-0a1b2c3d"}, pending)
    assert end.headers["list"] == "test"
    assert end.headers["value"] == "off"
    assert end.headers["mode"] == "Mine"
    assert end.headers["cat"] == "files_sys"
    assert b"protected place" in end.body
    await harness.close(w)


async def test_a_path_is_judged_as_a_write(harness):
    r, w = await harness.connect(prefs=b"protect=C:\\*.SYS")
    await harness.recv_until(r, {"OPTIONSEND"})
    _, end = await _lists(harness, r, w, {"kind": "path", "mode": "auto"},
                          b"C:\\CONFIG.SYS")
    assert end.headers["value"] == "check"
    assert end.headers["cat"] == "files_sys"
    await harness.close(w)


async def test_a_long_list_is_capped_and_says_how_many_were_left(
        harness, monkeypatch):
    from bridge.services import permission as perm
    monkeypatch.setattr(perm, "LIST_CAP", 5)
    r, w = await harness.connect(prefs=b"")
    await harness.recv_until(r, {"OPTIONSEND"})
    rows, end = await _lists(harness, r, w, {"list": "shell"})
    assert len(rows) == 5 and int(end.headers["hidden"]) > 0
    await harness.close(w)
