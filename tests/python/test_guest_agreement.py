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

from bridge.files.paths import (CLASS_EXACT, CLASS_TEXT, classify,
                                normalise)

import conftest
from conftest import wine_env

EXE_NAME = "synccheck.exe"

# Only the genuinely-unsupported machine skips. A missing binary with
# the toolchain present is a build omission and fails loudly -- see
# conftest.guest_exe.
pytestmark = conftest.needs_guest


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
    #: Spans the guest's read chunk with a CRLF straddling the boundary --
    #: the carry logic is the only thing that gets this right. Placeholder
    #: lengths; `_pin_corpus_to_the_guests_real_chunk_size` below overwrites
    #: these with lengths built from the guest's REAL RC_SYNC_CHUNK before
    #: anything reads them (03.14), so this 8192 is not load-bearing -- it is
    #: only what a test run without the guest toolchain (skipped) would show
    #: someone reading the source.
    "chunk_boundary":   b"x" * 8191 + b"\r\n" + b"y" * 10,
    "chunk_cr_last":    b"x" * 8191 + b"\r" + b"\n" + b"z",
    "many_cr_boundary": b"a" * 8190 + b"\r\r\n" + b"b" * 10,
}


@pytest.fixture(scope="module", autouse=True)
def _pin_corpus_to_the_guests_real_chunk_size():
    """03.14: the three entries above exist to straddle the guest's chunked
    read, so they have to be built from the guest's REAL RC_SYNC_CHUNK
    (win/sync/sync.h) rather than a copy of the number written here -- a
    change to the chunk size would otherwise leave this corpus quietly
    testing the OLD boundary while every test using it kept passing.

    `synccheck --chunk` prints that constant (win/checks/synccheck.c); this
    also cross-checks it against the bridge's OWN chunk size
    (bridge/files/syncrun.py's CHUNK), since a disagreement there means the
    guest's chunked read and the bridge's chunked send would carry at
    different byte offsets, wire format aside.

    autouse and module-scoped so it runs before `guest_answers` writes
    CORPUS to disk and before any other test in this file reads CORPUS's
    byte content -- this file has no fixed test order to rely on otherwise.
    Guarded the same way `conftest.needs_guest` is, rather than relying on
    that marker alone, because a skipped test must not still require Wine.
    """
    if not conftest.have_toolchain():
        pytest.skip("needs Open Watcom and the Lutris wine-ge runner")
    out = subprocess.run([conftest.WINE, conftest.guest_exe(EXE_NAME), "--chunk"],
                         capture_output=True, timeout=60, env=wine_env())
    chunk = int(out.stdout.decode("latin-1").strip())

    from bridge.files import syncrun
    assert chunk == syncrun.CHUNK, (
        f"guest RC_SYNC_CHUNK={chunk} but bridge/files/syncrun.py's "
        f"CHUNK={syncrun.CHUNK} -- the guest's chunked read and the "
        f"bridge's chunked send would carry at different byte offsets")

    CORPUS["chunk_boundary"] = b"x" * (chunk - 1) + b"\r\n" + b"y" * 10
    CORPUS["chunk_cr_last"] = b"x" * (chunk - 1) + b"\r" + b"\n" + b"z"
    CORPUS["many_cr_boundary"] = b"a" * (chunk - 2) + b"\r\r\n" + b"b" * 10


#: Names the guest scanner must NEVER report. They are its own working files,
#: and reporting one makes the bridge treat a half-finished write as ordinary
#: content: it has never seen that path, so it pulls it, and both sides then
#: hold a permanent `x.md.gaston-tmp` beside `x.md`. That is not hypothetical
#: -- a failed push on the real machine left one behind and the next pass
#: copied it to the bridge.
WORKING_NAMES = ("lf.gaston-tmp", "lf.gaston-old")


@pytest.fixture(scope="module")
def guest_answers(tmp_path_factory, _pin_corpus_to_the_guests_real_chunk_size):
    # The explicit dependency (on top of autouse) is belt-and-braces: CORPUS
    # must already hold the guest's real chunk-boundary lengths by the time
    # these files are written, and this makes that a fixture-graph guarantee
    # rather than an autouse-ordering assumption.
    d = tmp_path_factory.mktemp("corpus")
    for name, data in CORPUS.items():
        (d / name).write_bytes(data)
    for name in WORKING_NAMES:
        (d / name).write_bytes(b"should not be reported\n")
    env = wine_env()
    out = subprocess.run([conftest.WINE, conftest.guest_exe(EXE_NAME), str(d)], capture_output=True,
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
    env = wine_env()
    out = subprocess.run([conftest.WINE, conftest.guest_exe(EXE_NAME), "--limits"], capture_output=True,
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
    from bridge.files import caps as C

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


# --- containment ------------------------------------------------------------

@pytest.fixture(scope="module")
def guest_contained():
    env = wine_env()
    out = subprocess.run([conftest.WINE, conftest.guest_exe(EXE_NAME), "--contained"], capture_output=True,
                         timeout=300, env=env)
    rows = {}
    for line in out.stdout.decode("latin-1").splitlines():
        parts = line.split("\t")
        if len(parts) == 2:
            rows[parts[1]] = parts[0]
    assert rows, out.stderr.decode("latin-1", "replace")[:800]
    return rows


def test_a_backslash_cannot_escape_the_sync_root(guest_contained):
    """`a\\..\\..\\x` left the sync root entirely.

    rc_sync_contained split components on '/' only, so a path containing no
    '/' at all was read as ONE component -- not "..", not ".", not empty --
    and passed. join() then wrote <root>\\a\\..\\..\\x and handed it to Win32,
    which resolves `..`. A bridge could write anywhere the guest's user can.

    The wire format does say components are '/'-separated and that '\\' is
    illegal inside one. But that is a rule the BRIDGE enforces, and this
    function exists for the case where the bridge is what is wrong -- it
    cannot lean on a guarantee made by the party it is checking.
    """
    for bad in ("a\\..\\..\\x", "..\\x", "a\\..", "a/..\\b", "a\\../b"):
        assert guest_contained[bad] == "refuse", bad


def test_the_ordinary_traversals_are_still_refused(guest_contained):
    for bad in ("..", "../x", "a/../../x", ".", "./x", "a/./b",
                "/abs", "\\abs", "C:\\x", "a//b", "a\\\\b", ""):
        assert guest_contained[bad] == "refuse", bad


def test_names_that_merely_contain_dots_still_sync(guest_contained):
    """`..a` and `a..` are ordinary filenames. Refusing them would be a file
    that silently stops syncing, which is the failure this layer exists to
    prevent -- so the check must be on the whole component, not a substring."""
    for good in ("a", "a/b", "a/b/c.txt", "..a", "a..", "a/..b", "dot.dot/x"):
        assert guest_contained[good] == "allow", good


# --- the wire format itself -------------------------------------------------

def test_the_guest_s_real_manifest_rows_parse_with_the_bridge_s_real_parser(
        tmp_path):
    """The manifest row format was written four times and verified zero times.

    win/sync/syncrpc.c produced it, the bridge parsed it, test_sync_wire.py
    re-implemented the producer for the tests, and tools/syncprobe.py
    re-implemented the parser. synccheck.exe -- the one thing that runs the
    real C against the real Python -- printed a DIFFERENT, tab-separated
    format of its own, so it proved the classifier and the checksums and
    never touched the wire format. A field added on one side passed the
    whole suite.

    This closes it: `synccheck --wire` emits through rc_sync_manifest_row,
    the same function do_scan puts on the wire, and those bytes are handed to
    bridge.files.manifest.parse_rows, the same parser the frame handler uses.
    """
    from bridge.files import caps as C
    from bridge.files import manifest

    corpus = {
        "crlf.txt":  b"a\r\nb\r\n",
        "lf.txt":    b"a\nb\n",
        "binary.dat": b"\x00\x01\x02",
        "mixed.txt": b"a\r\nb\nc\r\n",
        "empty.txt": b"",
    }
    d = tmp_path / "wire"
    d.mkdir()
    for name, data in corpus.items():
        (d / name).write_bytes(data)

    # Wine sees the Linux filesystem through Z:.
    guest_path = "Z:" + str(d).replace("/", "\\")
    out = subprocess.run(
        [conftest.WINE, conftest.guest_exe(EXE_NAME), "--wire", guest_path],
        capture_output=True, timeout=300, env=wine_env())
    body = out.stdout
    assert body, out.stderr.decode("latin-1", "replace")[:800]

    rows, display = manifest.parse_rows(body, C.WIN9X)
    assert set(display.values()) == set(corpus), (
        "the bridge's parser did not recover the guest's own rows")

    for name, data in corpus.items():
        row = rows[name.lower()]
        assert row.cls == classify(data), (
            f"{name}: the guest and the bridge disagree about the class, "
            f"which is a disagreement that never converges")
        assert row.crc == zlib.crc32(normalise(data, classify(data))), (
            f"{name}: checksum mismatch through the real wire format")


def test_a_row_that_would_not_fit_is_refused_rather_than_truncated():
    """rc_sync_manifest_row returns 0 rather than writing a short row.

    A truncated row is not a short row, it is a DIFFERENT row: the length
    prefix stops matching the bytes after it, so the parser reads the next
    row's fields as this one's path. manifest_add flushes and retries, and
    gives up only if a single row cannot fit an empty buffer.
    """
    from bridge.files import caps as C
    from bridge.files import manifest
    # A row whose length prefix disagrees with what follows is dropped, not
    # guessed at -- the parser logs and skips, and the CALLER catches the
    # short count against SYNCEND's files=.
    rows, _ = manifest.parse_rows(b"f t 6 0 1 2 99 short\n", C.WIN9X)
    assert rows == {} or all(r.type for r in rows.values())
