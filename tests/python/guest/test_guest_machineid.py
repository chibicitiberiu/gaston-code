"""The guest's own machine identity, exercised by the guest's own code.

One bridge serving two machines used to put 87 of the QEMU box's
conversations in a real Win98 box's chat list, because `mine` is written on
the bridge host and says only that THIS BRIDGE started a conversation
(BACKLOG.md §21). The fix gives the client an identity of its own: a GUID
minted once, kept in `machine.id` beside the exe, and reported in `HELLO`.

Every way that can go wrong is invisible from the outside. A client that
re-mints on each launch behaves perfectly until the morning the chat list is
empty; a client that cannot write the file behaves the same as one that can.
So `checks/idcheck.cpp` runs the shipping `app/machineid.cpp` against a real
filesystem and a real `CoCreateGuid`, and this reads its verdicts.

Wine proves the code, never the platform: a read-only medium and a full disk
are the branches the log lines in `MachineId::load` exist for.
"""

import subprocess

import conftest

EXE_NAME = "idcheck.exe"

pytestmark = conftest.needs_guest


def _run(workdir: str) -> subprocess.CompletedProcess:
    return conftest.run_guest(EXE_NAME, conftest.wine_path(workdir),
                              text=True, timeout=120)


def test_the_identity_holds_every_property_the_chat_list_needs(tmp_path):
    out = _run(str(tmp_path))
    assert "FAILURES" not in out.stdout, out.stdout
    assert out.returncode == 0, out.stdout + out.stderr
    assert "all machine-id properties hold" in out.stdout


def test_each_property_is_actually_reported(tmp_path):
    """The verdicts, named. A check whose scenarios quietly stopped running
    would still print no FAILURES, which is the shape of gap that let 72
    cross-language checks skip in silence (see conftest)."""
    out = _run(str(tmp_path))
    for verdict in ("minted", "persisted", "stable across launches",
                    "distinct per directory", "trailing whitespace trimmed",
                    "garbage replaced and persisted",
                    "no id when it cannot be written"):
        assert f"ok   {verdict}" in out.stdout, out.stdout


def test_the_id_is_on_disk_where_the_bridge_expects_it_to_persist(tmp_path):
    """White-box, on purpose: the file is the whole feature. It has to be
    one line of ASCII in an 8.3 name, because the alternative considered was
    a key in gaston.ini -- which refuses startup on a bad value and is
    rewritten whenever any setting changes."""
    _run(str(tmp_path))
    written = tmp_path / "A" / "machine.id"
    assert written.exists(), "machine.id is not beside the exe"
    text = written.read_text(encoding="ascii")
    assert len(text.splitlines()) == 1
    assert text.strip().count("-") == 4
