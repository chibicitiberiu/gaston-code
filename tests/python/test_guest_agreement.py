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


#: Names the guest scanner must NEVER report. They are its own working files,
#: and reporting one makes the bridge treat a half-finished write as ordinary
#: content: it has never seen that path, so it pulls it, and both sides then
#: hold a permanent `x.md.gaston-tmp` beside `x.md`. That is not hypothetical
#: -- a failed push on the real machine left one behind and the next pass
#: copied it to the bridge.
WORKING_NAMES = ("lf.gaston-tmp", "lf.gaston-old")


@pytest.fixture(scope="module")
def guest_answers(tmp_path_factory):
    d = tmp_path_factory.mktemp("corpus")
    for name, data in CORPUS.items():
        (d / name).write_bytes(data)
    for name in WORKING_NAMES:
        (d / name).write_bytes(b"should not be reported\n")
    env = dict(os.environ, WINEPREFIX=PREFIX, WINEDEBUG="-all")
    out = subprocess.run([WINE, EXE, str(d)], capture_output=True,
                         timeout=300, env=env)
    rows = {}
    for line in out.stdout.decode("latin-1").splitlines():
        parts = line.split("\t")
        if len(parts) == 7:
            rows[parts[6].strip()] = (parts[0], parts[1], int(parts[2]),
                                      int(parts[3]), int(parts[4]),
                                      int(parts[5]))
    assert rows, out.stderr.decode("latin-1", "replace")[:800]
    return rows


@pytest.mark.parametrize("name", sorted(CORPUS))
def test_class_matches_the_bridge(name, guest_answers):
    assert name in guest_answers, "the guest did not report this file at all"
    _type, cls, _size, _crc, _n, _r = guest_answers[name]
    assert cls == classify(CORPUS[name]), (
        f"{name}: guest says {cls!r}, bridge says {classify(CORPUS[name])!r} -- "
        "the two sides would compare different functions forever"
    )


@pytest.mark.parametrize("name", sorted(CORPUS))
def test_crc_matches_the_bridge(name, guest_answers):
    _type, cls, _size, crc, _n, _r = guest_answers[name]
    data = CORPUS[name]
    expect = zlib.crc32(normalise(data, classify(data)))
    assert crc == expect, (
        f"{name}: guest crc {crc:#010x}, bridge crc {expect:#010x}"
    )


def test_every_corpus_file_was_seen(guest_answers):
    assert set(guest_answers) >= set(CORPUS)


def test_the_guest_scanner_hides_its_own_working_files(guest_answers):
    """The shipping `walk` in win/sync.c, not a reimplementation of it."""
    leaked = [n for n in WORKING_NAMES if n in guest_answers]
    assert not leaked, (
        f"the guest manifest offered {leaked} -- the bridge would pull them "
        f"and they would then sync forever")


def test_the_corpus_actually_covers_both_classes():
    """A corpus that happens to be all one class would pass vacuously."""
    seen = {classify(v) for v in CORPUS.values()}
    assert seen == {CLASS_TEXT, CLASS_EXACT}


@pytest.mark.parametrize("name", sorted(CORPUS))
def test_both_checksums_match_the_bridge(name, guest_answers):
    """Not just the one the guest's own classifier selected.

    `.gitattributes` can declare a path byte-exact that the content reads as
    text, and then the bridge compares against the RAW checksum of a file the
    guest called text -- a number that, before v2, was computed and thrown
    away. A raw checksum that happened to be right only for files already
    classified byte-exact would pass every other test in this file and still
    make every `-text` override a permanent disagreement.
    """
    _type, _cls, _size, _crc, crc_norm, crc_raw = guest_answers[name]
    data = CORPUS[name]
    assert crc_raw == zlib.crc32(data), f"{name}: raw checksum"
    assert crc_norm == zlib.crc32(normalise(data, CLASS_TEXT)), \
        f"{name}: normalised checksum"


def test_the_selected_checksum_is_one_of_the_two(guest_answers):
    """`crc` is a convenience, and a convenience that drifts from the pair it
    summarises is worse than not having it."""
    for name, (_t, cls, _s, crc, crc_norm, crc_raw) in guest_answers.items():
        if name in CORPUS:
            assert crc == (crc_norm if cls == CLASS_TEXT else crc_raw), name


# --- the working-name reserve ----------------------------------------------
#
# The bridge refuses a file path with fewer than WORKRESERVE_GASTON characters
# to spare, and the guest refuses to open one. Those are two numbers in two
# languages describing one rule, which is exactly the shape that drifts. The
# test below reads the guest's real boundary and compares it against
# bridge/caps.py rather than against a constant written here.

@pytest.fixture(scope="module")
def guest_limits():
    env = dict(os.environ, WINEPREFIX=PREFIX, WINEDEBUG="-all")
    out = subprocess.run([WINE, EXE, "--limits"], capture_output=True,
                         timeout=300, env=env)
    rows = {}
    for line in out.stdout.decode("latin-1").splitlines():
        parts = line.split("\t")
        if len(parts) == 3:
            rows[int(parts[0])] = parts[1]
    assert rows, out.stderr.decode("latin-1", "replace")[:800]
    return rows


def test_the_guest_refuses_a_path_with_no_room_for_its_temp_name(guest_limits):
    """The boundary the guest actually enforces, not the one it documents.

    A path of 251..257 makes `.gaston-tmp` and `.gaston-old` truncate to the
    same string, and the commit then deletes its own temp, moves the user's
    file aside and straight back, and reports success -- so the bridge advances
    its baseline over bytes the guest never took. At 258 and 259 the temp name
    resolves to the target itself and CREATE_ALWAYS truncates the user's file
    before anything is verified. Both are silent, so the boundary is the fix.
    """
    from bridge import caps as C

    limit = C.Caps().maxpath - C.WORKRESERVE_GASTON       # 249
    # `full` here counts root + separator + rel, with no NUL -- one less than
    # the bridge's `full`, which includes it. Compare like for like.
    for length, verdict in sorted(guest_limits.items()):
        if length + 1 <= limit:
            assert verdict == "allowed", (length, verdict)
        else:
            assert verdict == "refused", (length, verdict)


def test_the_guest_never_builds_a_truncated_working_name(guest_limits):
    """Whatever it accepts, it must not have silently shortened."""
    assert any(v == "refused" for v in guest_limits.values()), \
        "nothing was refused; the reserve is not being enforced at all"
    assert any(v == "allowed" for v in guest_limits.values()), \
        "everything was refused; the boundary is in the wrong place"
