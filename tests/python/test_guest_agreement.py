"""Do the guest's C classifier and the bridge's Python one agree EXACTLY?

This is the single most important property in the sync layer and it cannot be
established by reading both implementations. If they ever disagree about a
file's class, the two sides compare the outputs of different functions forever:
the file never converges, and every pass rewrites it on the user's machine.

So the same corpus goes through both, and the answers must match byte for byte.
`win/synccheck.exe` is the real `win/sync.c` -- the code that ships -- run under
Wine. Wine cannot model FAT granularity or case-insensitivity, but it models
byte-level file reading perfectly well, which is all this needs.
"""

import os
import subprocess
import zlib

import pytest

from bridge.paths import CLASS_EXACT, CLASS_TEXT, classify, normalise

HERE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
EXE = os.path.join(HERE, "win", "synccheck.exe")

#: The project uses Lutris's wine-ge build, not a system wine -- see
#: spike/Makefile, which records that it is a standalone Wine with full 32-bit
#: support rather than Proton.
WINE = os.environ.get(
    "WINE",
    os.path.expanduser(
        "~/.local/share/lutris/runners/wine/wine-ge-8-26-x86_64/bin/wine"),
)
PREFIX = os.path.join(HERE, "spike", ".wineprefix")

pytestmark = pytest.mark.skipif(
    not os.path.exists(EXE) or not os.path.exists(WINE),
    reason="needs win/synccheck.exe and the Lutris wine-ge runner",
)

#: Deliberately the nastiest content we can think of, because the cases that
#: broke this design were never the obvious ones.
CORPUS = {
    "empty":            b"",
    "lf":               b"a\nb\nc\n",
    "crlf":             b"a\r\nb\r\nc\r\n",
    "mixed":            b"a\r\nb\nc\r\n",
    "lone_cr":          b"a\rb\rc",
    "cr_cr_lf":         b"x\r\r\n",              # the oscillation case
    "cr_at_eof":        b"a\nb\r",               # carry never resolves
    "crlf_no_eol":      b"a\r\nb",
    "nul_first":        b"\x00rest",
    "nul_last":         b"text\r\n\x00",
    "nul_deep":         b"%PDF-1.4\n" + b"ascii\r\n" * 2000 + b"\x00stream",
    "utf16":            "hi\r\nthere\r\n".encode("utf-16-le"),
    "utf8_bom":         b"\xef\xbb\xbfhello\n",
    "high_bytes":       bytes(range(0x80, 0x100)),
    "cp1252_text":      "café\r\nnaïve\r\n".encode("cp1252"),
    "long_lf":          b"line\n" * 5000,
    "long_crlf":        b"line\r\n" * 5000,
    #: Spans the 8192-byte read chunk with a CRLF straddling the boundary --
    #: the carry logic is the only thing that gets this right.
    "chunk_boundary":   b"x" * 8191 + b"\r\n" + b"y" * 10,
    "chunk_cr_last":    b"x" * 8191 + b"\r" + b"\n" + b"z",
    "many_cr_boundary": b"a" * 8190 + b"\r\r\n" + b"b" * 10,
}


@pytest.fixture(scope="module")
def guest_answers(tmp_path_factory):
    d = tmp_path_factory.mktemp("corpus")
    for name, data in CORPUS.items():
        (d / name).write_bytes(data)
    env = dict(os.environ, WINEPREFIX=PREFIX, WINEDEBUG="-all")
    out = subprocess.run([WINE, EXE, str(d)], capture_output=True,
                         timeout=300, env=env)
    rows = {}
    for line in out.stdout.decode("latin-1").splitlines():
        parts = line.split("\t")
        if len(parts) == 5:
            rows[parts[4].strip()] = (parts[0], parts[1], int(parts[2]),
                                      int(parts[3]))
    assert rows, out.stderr.decode("latin-1", "replace")[:800]
    return rows


@pytest.mark.parametrize("name", sorted(CORPUS))
def test_class_matches_the_bridge(name, guest_answers):
    assert name in guest_answers, "the guest did not report this file at all"
    _type, cls, _size, _crc = guest_answers[name]
    assert cls == classify(CORPUS[name]), (
        f"{name}: guest says {cls!r}, bridge says {classify(CORPUS[name])!r} -- "
        "the two sides would compare different functions forever"
    )


@pytest.mark.parametrize("name", sorted(CORPUS))
def test_crc_matches_the_bridge(name, guest_answers):
    _type, cls, _size, crc = guest_answers[name]
    data = CORPUS[name]
    expect = zlib.crc32(normalise(data, classify(data)))
    assert crc == expect, (
        f"{name}: guest crc {crc:#010x}, bridge crc {expect:#010x}"
    )


def test_every_corpus_file_was_seen(guest_answers):
    assert set(guest_answers) >= set(CORPUS)


def test_the_corpus_actually_covers_both_classes():
    """A corpus that happens to be all one class would pass vacuously."""
    seen = {classify(v) for v in CORPUS.values()}
    assert seen == {CLASS_TEXT, CLASS_EXACT}
