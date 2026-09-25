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

import pathlib
import re
import zlib

import conftest
import pytest

from bridge.files.paths import CLASS_EXACT, CLASS_TEXT, classify, normalise

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
    out = conftest.run_guest(EXE_NAME, "--chunk")
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
    out = conftest.run_guest(EXE_NAME, d, timeout=300)
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
    out = conftest.run_guest(EXE_NAME, "--limits", timeout=300)
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
    out = conftest.run_guest(EXE_NAME, "--contained", timeout=300)
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
    re-implemented the producer for the tests, and tools/probe/syncprobe.py
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

    out = conftest.run_guest(EXE_NAME, "--wire", conftest.wine_path(d),
                             timeout=300)
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
    """A row whose length prefix disagrees with what follows is dropped, not
    guessed at -- the parser logs and skips, and the caller catches the
    short count against SYNCEND's files=.

    Finding 04.5: the old assertion used `assert rows == {} or all(r.type for r in rows.values())`,
    which was vacuous because all() is always True for any parsed Entry. The assertion
    must strictly prove that the malformed row was refused.
    """
    from bridge.files import caps as C
    from bridge.files import manifest
    rows, _ = manifest.parse_rows(b"f t 6 0 1 2 99 short\n", C.WIN9X)
    assert rows == {}


# --- the status vocabulary ---------------------------------------------------

def test_sync_status_vocabulary_matches_the_guest():
    """SyncStatus (bridge/status.py) must name exactly the tokens
    win/sync/syncrpc.cpp's four do_* handlers can put in a `status=` header.

    No Wine here -- this is a text diff, not a binary run -- but it lives in
    this module (and inherits its `needs_guest` skip) rather than a plain
    unit test file, because the property it checks is specifically an
    agreement between this package and that one, the same family as every
    other test here. win/rpc/rpc.h was read too (docs/audit/02-bridge-python.md
    02.38 names it alongside syncrpc.cpp): it is the RPC worker's routing
    header, and defines no status vocabulary of its own -- SYNCSCAN/PUT/
    MKDIR/GET are routed through it to syncrpc.cpp's handlers, which are
    where every `status=` value in this file actually comes from.

    Extracted with four targeted regexes rather than a C parser: `status =
    "..."` / `why = "..."` assignments, `put_fail`/`putend_fail`'s third argument, the `?
    "a" : "b"` ternary in do_mkdir (`exists`/`is_file`), and do_scan's
    heap-allocation-failure fallback, which sets `v[1]` beside `k[1] =
    "status"` rather than assigning a variable named `status`. Verified by
    hand against a manual read of the file before this was written: it
    reproduces that reading's 16-token set exactly, matching neither more
    nor fewer.
    """
    src = pathlib.Path(conftest.HERE, "win", "sync", "syncrpc.cpp").read_text()

    found: set[str] = set()
    for m in re.finditer(r'\b(?:status|why)\s*=\s*"([a-z_]+)"', src):
        found.add(m.group(1))
    # put_fail AND putend_fail. The second exists because do_putend closes
    # the temp file's handle before the compare-and-swap check, which makes
    # put_fail's handle-keyed DeleteFileA unreachable -- every refusal after
    # that point has to remove the temp by name instead (docs/mem-audit F1).
    # It carries the same vocabulary, and `changed` now reaches the wire
    # ONLY through it, so missing it here loses a real token.
    for m in re.finditer(r'\bput(?:end)?_fail\([^;]*?,\s*"([a-z_]+)"\s*\)',
                         src):
        found.add(m.group(1))
    for m in re.finditer(r'\?\s*"([a-z_]+)"\s*:\s*"([a-z_]+)"', src):
        found.update(m.groups())
    for m in re.finditer(r'k\[1\]\s*=\s*"status";\s*v\[1\]\s*=\s*"([a-z_]+)"',
                         src):
        found.add(m.group(1))
    assert found, ("extracted nothing from win/sync/syncrpc.cpp -- the "
                   "regexes above no longer match the file's shape")

    from bridge.status import SyncStatus
    bridge_side = {s.value for s in SyncStatus if s is not SyncStatus.UNKNOWN}

    missing = found - bridge_side
    assert not missing, (
        f"win/sync/syncrpc.cpp can send {missing}, which SyncStatus "
        f"(bridge/status.py) does not name. An unrecognised status parses "
        f"to SyncStatus.UNKNOWN rather than a wrong answer -- see "
        f"SyncStatus.parse's docstring -- but the log and the model both "
        f"lose the guest's own word for what happened.")
    extra = bridge_side - found
    assert not extra, (
        f"SyncStatus names {extra}, which this extraction did not find in "
        f"win/sync/syncrpc.cpp -- either the guest no longer sends it, or "
        f"the regexes above need a new case for how it is produced now.")


def test_exec_status_vocabulary_matches_the_guest():
    """ExecStatus (bridge/status.py) must name exactly the tokens
    win/rpc/rpc.cpp sets in EXECRESULT status headers.
    """
    src = pathlib.Path(conftest.HERE, "win", "rpc", "rpc.cpp").read_text()

    found: set[str] = set()
    for m in re.finditer(r'\bstatus\s*=\s*"([a-z_]+)"', src):
        found.add(m.group(1))
    for m in re.finditer(r'(?:k|keys)\[1\]\s*=\s*"status";\s*(?:v|vals)\[1\]\s*=\s*"([a-z_]+)"',
                         src):
        found.add(m.group(1))
    assert found, ("extracted nothing from win/rpc/rpc.cpp -- the "
                   "regexes above no longer match the file's shape")

    from bridge.status import ExecStatus
    bridge_side = {s.value for s in ExecStatus}

    missing = found - bridge_side
    assert not missing, (
        f"win/rpc/rpc.cpp can send {missing}, which ExecStatus "
        f"(bridge/status.py) does not name.")
    extra = bridge_side - found
    assert not extra, (
        f"ExecStatus names {extra}, which this extraction did not find in "
        f"win/rpc/rpc.cpp -- either the guest no longer sends it, or "
        f"the regexes above need a new case for how it is produced now.")


# --- the SYNCPUT path parse, over bodies with real file content -------------
#
# The composition that had no test, and the one that broke. `--contained`
# above drives rc_sync_contained with BARE paths and it was always correct;
# the defect was in do_put, which handed it a pointer that ran off the end of
# the path and into the file's own bytes. So any push whose first 8KB held
# `//` -- an empty path component, and the line-comment token of C++, Java,
# JavaScript, Go and Rust, plus every `http://` URL -- came back `bad_path`
# forever, naming the one thing about the request that was not wrong.
#
# Nothing could catch it: fakegui.py answers SYNCPUT with a canned status
# without parsing the body at all, so the Python side never reached this code,
# and rpccheck only asserts which LANE the frame takes.
#
# These drive the real rc_syncrpc_put_path through synccheck --putpath, with
# bodies shaped exactly as bridge/files/synctransport.py builds them:
# "<len> <rel>" followed immediately by the chunk, with no separator.

@pytest.fixture(scope="module")
def guest_putpath():
    out = conftest.run_guest(EXE_NAME, "--putpath", timeout=300)
    rows = {}
    for line in out.stdout.decode("latin-1").splitlines():
        parts = line.split("\t")
        if len(parts) == 4:
            rows[parts[3]] = (parts[0], parts[1], int(parts[2]))
    assert rows, out.stderr.decode("latin-1", "replace")[:800]
    return rows


@pytest.mark.parametrize("label,path", [
    ("cpp-line-comment",  "src/main.cpp"),
    ("js-spdx",           "web/app.js"),
    ("url-in-text",       "notes.txt"),
    ("dotdot-in-content", "Makefile"),
    ("backslashes",       "a.txt"),
])
def test_content_after_the_path_cannot_refuse_the_push(guest_putpath,
                                                       label, path):
    """A file whose first chunk contains `//`, `/../` or `\\\\` still pushes.

    Every one of these was `bad_path` before the parse was fixed, which meant
    the agent could not write a source file with a line comment onto a machine
    whose entire purpose is writing code on it -- and the bridge retried it
    every thirty seconds forever, because a refused push does not advance the
    baseline.
    """
    verdict, got, _ = guest_putpath[label]
    assert verdict == "ok", f"{label}: the CONTENT was read as part of the path"
    assert got == path, f"{label}: parsed the wrong path"


@pytest.mark.parametrize("label", [
    "c-block-comment", "plain-ascii", "empty-content",
])
def test_ordinary_bodies_still_parse(guest_putpath, label):
    assert guest_putpath[label][0] == "ok"


@pytest.mark.parametrize("label", [
    "escape-path", "absolute-path", "drive-path",
])
def test_the_path_itself_is_still_checked(guest_putpath, label):
    """The escape defence must survive the fix.

    Containment is now checked on the bounded COPY rather than on the raw
    pointer, so the question "did moving the check weaken it" has to be asked
    out loud. It did not: the path is still fully validated, and the copy is
    what the rest of do_put actually uses.
    """
    verdict, got, _ = guest_putpath[label]
    assert verdict == "bad_path", f"{label} was ACCEPTED"
    assert got == "", "a refused path must not be left in the output buffer"


def test_a_body_with_no_separator_is_refused(guest_putpath):
    """`body + plen + 1` would be one byte past the frame allocation.

    net.cpp NUL-terminates the payload over a `len + 1` block, so the
    prefix-walk stops on that NUL -- and the byte after it is outside the
    block. Win98 puts no guard page behind a small heap block, so reading
    there does not fault; it reads adjacent heap and acts on it as a path.
    """
    assert guest_putpath["no-separator"][0] == "no_sep"


def test_a_declared_length_past_the_frame_is_refused(guest_putpath):
    """The bridge's `<len>` is wire data and is not believed on its own."""
    assert guest_putpath["length-past-end"][0] == "no_sep"


def test_the_data_offset_is_where_the_content_starts(guest_putpath):
    """The write offset comes from the parser, not a second computation.

    do_put writes the chunk from this offset. Deriving it anywhere else is how
    it drifts from the prefix the parser actually consumed, and a wrong offset
    writes the length prefix into the user's file.
    """
    for label, path in (("cpp-line-comment", "src/main.cpp"),
                        ("plain-ascii", "readme.md")):
        _, _, off = guest_putpath[label]
        assert off == len(str(len(path))) + 1 + len(path), label


# --- rc_sync_commit cleans up after itself, on every refusal ----------------
#
# Four of its five failure returns deleted the temp file first; the fifth --
# the `got.type != 'f'` verification right after the handle is closed -- did
# not. That asymmetry is now fixed (docs/mem-audit F2).
#
# BE CLEAR ABOUT WHAT THESE TESTS DO AND DO NOT CATCH. They were written to
# guard F2 and they DO NOT: reintroducing the bug leaves all four of them
# green, which was checked rather than assumed. The reason is worth keeping,
# because it is also what makes F2 a small finding instead of a large one.
# There are only three ways to reach that branch, and rc_sync_stat_file
# decides which:
#
#   attr == INVALID   the file is absent -- nothing to delete
#   FILE_ATTRIBUTE_DIRECTORY   a directory -- DeleteFileA cannot remove one
#   CreateFileA fails          another handle holds it with no sharing, so
#                              DeleteFileA fails for the same reason
#
# In every one of those the temp is either already gone or undeletable, so
# the missing cleanup could not leak anything. The ONE case where it could
# is the fourth: `ReadFile` failing after a successful open (a bad sector, a
# network drive dropping out) closes the handle and reports 'u' with the
# file present and nothing holding it. That needs fault injection to reach
# and there is no way in from here.
#
# What these tests are actually worth: they pin the cleanup behaviour of
# every reachable path, so a later "make this consistent" change cannot
# quietly start deleting a locked temp, and a later simplification cannot
# drop the delete from the crc refusal. Driven through synccheck's --commit
# mode, against the SHIPPING sync.o rather than a reimplementation.

@pytest.fixture(scope="module")
def commit_cases(tmp_path_factory):
    d = tmp_path_factory.mktemp("commit")
    out = conftest.run_guest(EXE_NAME, "--commit", conftest.wine_path(d),
                             timeout=300)
    rows = {}
    for line in out.stdout.decode("latin-1").splitlines():
        parts = line.split("\t")
        if len(parts) >= 3:
            rows[parts[0]] = (int(parts[1]), parts[2] == "1")
    assert rows, out.stderr.decode("latin-1", "replace")[:800]
    return rows


def test_a_refused_commit_takes_its_temp_file_with_it(commit_cases):
    """The ordinary refusal: the bytes arrived but the checksum disagrees."""
    rc, left_behind = commit_cases["crc_mismatch"]
    assert rc == -2, f"expected crc_mismatch to refuse with -2, got {rc}"
    assert not left_behind, (
        "rc_sync_commit refused the transfer and left its .gaston-tmp in the "
        "tree. The scanner skips that name, so nothing will ever report it "
        "and nothing will remove it until the next rc_sync_recover.")


def test_the_verification_branch_is_reachable_and_does_not_fault(commit_cases):
    """The path that used to skip its cleanup.

    Asserts what is reachable from outside: the branch is live, it answers
    -1, and it does not fault when the file is already gone. It cannot
    assert the fix -- see the note above this fixture for why.
    """
    rc, left_behind = commit_cases["vanished"]
    assert rc == -1, f"expected the verification branch to refuse with -1, got {rc}"
    assert not left_behind


def test_a_locked_temp_is_refused_and_honestly_left_alone(commit_cases):
    """`type == 'u'` with the file still present, and that one MUST survive.

    DeleteFileA on a file another handle holds open fails, and it should:
    guessing that the file is ours to remove while somebody else is reading
    it is worse than leaving it for rc_sync_recover. This is here so a future
    "make cleanup unconditional" change has to argue with a test.
    """
    rc, left_behind = commit_cases["locked"]
    assert rc == -1, f"expected a locked temp to refuse with -1, got {rc}"
    assert left_behind, (
        "the temp was removed while another handle held it open -- either "
        "Wine is not enforcing the share mode, or the cleanup stopped "
        "checking whether the delete succeeded")


def test_a_successful_commit_moves_the_temp_rather_than_deleting_it(commit_cases):
    """The success path, so cleanup-on-failure cannot be faked by deleting
    unconditionally: the temp must be gone AND the target must exist."""
    rc, left_behind = commit_cases["ok"]
    assert rc == 0, f"expected a good commit to return 0, got {rc}"
    assert not left_behind


def test_successful_commit_preserves_readonly_attribute(commit_cases):
    """05.4: a commit replacing a read-only target restores FILE_ATTRIBUTE_READONLY."""
    rc, ro_preserved = commit_cases["readonly_ok"]
    assert rc == 0, f"expected 0, got {rc}"
    assert ro_preserved, "read-only attribute was not restored on successful commit"


def test_failed_commit_restores_readonly_attribute(commit_cases):
    """05.4: a failed commit (-3) restores FILE_ATTRIBUTE_READONLY on the target
    rather than silently leaving write protection stripped."""
    rc, ro_preserved = commit_cases["readonly_fail"]
    assert rc == -3, f"expected -3 on locked target, got {rc}"
    assert ro_preserved, "read-only attribute was stripped by failed commit"

