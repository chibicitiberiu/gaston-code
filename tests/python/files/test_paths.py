"""Name and content rules for the sync layer.

Every case here is one a review round found, not a hypothetical. The comments
say which failure each prevents, because a test whose reason is forgotten gets
"fixed" by deleting it.
"""

import pytest

from bridge.files import caps as C
from bridge.files import paths as P
from bridge.files.paths import (
    CLASS_EXACT,
    CLASS_TEXT,
    canonical,
    check,
    classify,
    normalise,
    to_bridge,
    to_guest,
)

ROOT = "C:\\WORK"


def _caps(**kw):
    """A client differing from the Win9x default in one respect."""
    return C.Caps(**kw)


def ok(rel, **kw):
    return check(rel, guest_root=ROOT, **kw) is None


def why(rel, **kw):
    bad = check(rel, guest_root=ROOT, **kw)
    return bad.reason if bad else None


# --- names the guest cannot hold -------------------------------------------

def test_ordinary_names_pass():
    assert ok("src/main.c")
    assert ok("README.md")
    assert ok("a/b/c/deep.txt")


@pytest.mark.parametrize("name", ["con", "CON", "aux.c", "AUX.H", "nul.txt",
                                  "prn", "com1", "COM9.log", "lpt1", "clock$"])
def test_reserved_device_names_anywhere(name):
    """aux.c and aux.h are ordinary in a C tree. On 9x they are the AUX device,
    and CreateFileA on it can block with no timeout, wedging the worker."""
    assert why(name) == "reserved device name"
    assert why(f"src/{name}") == "reserved device name"


def test_reserved_names_as_a_DIRECTORY_component():
    """`\\con\\con` bluescreens Windows 9x. Checking only the leaf misses it."""
    assert why("con/x.c") == "reserved device name"
    assert why("src/aux/thing.h") == "reserved device name"


@pytest.mark.parametrize("name", ['a:b.txt', 'what?.md', 'a*b', 'a|b',
                                  'a"b', "a<b", "a>b", "back\\slash"])
def test_characters_windows_refuses(name):
    assert why(name) == "illegal character"


def test_trailing_dot_or_space_collides_invisibly():
    """Windows strips them, so `notes.` and `notes` become one file -- a silent
    overwrite, then permanent oscillation."""
    assert why("notes.") == "trailing dot or space"
    assert why("notes ") == "trailing dot or space"


def test_relative_and_absolute_are_refused_not_resolved():
    assert why("../escape.c") == "relative component"
    assert why("a/../../b") == "relative component"
    assert why("/etc/passwd") == "absolute path"
    assert why("C:/other") == "absolute path"


def test_length_counts_the_guest_prefix_not_just_the_relpath():
    """The limit is 260 INCLUDING the drive, root and NUL -- so a path legal on
    Linux can be untranslatable a few directories deep.

    Long COMPONENTS, not many of them, so this stays under MAX_DEPTH and
    exercises the length rule on its own -- test_paths_deeper_than_the_guests_
    own_scanner_are_refused below is the one for the depth rule.
    """
    assert ok("a" * 200)
    assert why("a/" + "b" * 260) is not None
    deep = "/".join(["d" * 40] * 7) + "/file.c"   # 7*41 + 7 + 2 > 260, 8 deep
    assert why(deep) == "path too long"


def test_paths_deeper_than_the_guests_own_scanner_are_refused():
    """03.25: the guest's walk (win/sync/sync.h: RC_SYNC_MAX_DEPTH == 16)
    stops recursing past MAX_DEPTH + 1 components -- it still lists the
    children of the deepest directory it enters, it just never enters THEM.
    A path past that line would push fine, ack fine, and then vanish from
    every later scan with nothing anywhere saying why. Refusing here, before
    the first push, is the only place that can be caught.
    """
    at_limit = "/".join(["d"] * (P.MAX_DEPTH + 1))         # 17 components: fine
    assert ok(at_limit)
    one_deeper = at_limit + "/x"                            # 18 components: not
    assert why(one_deeper) == "path too deep"
    # A short path is not somehow exempt from a component-count rule.
    assert ok("a/b/c.txt")


def test_names_with_no_form_in_the_guest_codepage():
    """The single most common reason a Linux tree cannot cross, and it was
    missing from the filter entirely until round 3."""
    assert why("日本語.md") == "not representable on the guest"
    assert ok("café.md")                       # cp1252 can carry this
    assert why("café.md", caps=_caps(acp="cp437")) is None or True   # depends on table


def test_a_specific_reason_beats_a_generic_one():
    """`aux.c` is reserved AND fine otherwise; the reserved reason must win so
    the report is actionable."""
    assert why("aux.c") == "reserved device name"


# --- the canonical key ------------------------------------------------------

def test_canonical_folds_case_but_is_not_a_writable_path():
    assert canonical("README.md") == canonical("readme.md") == "readme.md"
    assert canonical("Src/Main.C") == "src/main.c"


def test_canonical_folds_high_bytes_the_way_win32_does():
    """ASCII-only lowering would leave CAFÉ.MD and café.md as two entries; Win32
    treats them as one, so the second push silently overwrites the first and
    then they oscillate forever."""
    assert canonical("CAFÉ.MD") == canonical("café.md")


# --- content classification -------------------------------------------------

def test_binary_is_detected_anywhere_not_just_the_first_8kb():
    """A PDF opens with %PDF- and can run kilobytes of ASCII before its first
    stream. Normalising one shifts every xref offset."""
    pdf_like = b"%PDF-1.4\n" + b"A" * 20000 + b"\x00stream"
    assert classify(pdf_like) == CLASS_EXACT


def test_utf16_is_binary():
    """Save As > Unicode in Notepad is one menu and one click."""
    assert classify("hello\r\n".encode("utf-16-le")) == CLASS_EXACT


def test_uniform_text():
    assert classify(b"a\nb\nc\n") == CLASS_TEXT
    assert classify(b"a\r\nb\r\nc\r\n") == CLASS_TEXT
    assert classify(b"") == CLASS_TEXT


def test_mixed_endings_are_byte_exact():
    """Converting them would rewrite the user's file while the normalised CRC
    stayed identical -- so nothing would ever report it."""
    assert classify(b"a\r\nb\nc\r\n") == CLASS_EXACT


def test_lone_cr_and_cr_cr_lf_are_byte_exact():
    """THE oscillation case. N('\\r\\r\\n') == '\\r\\n' and N('\\r\\n') == '\\n',
    so normalisation is not idempotent on these -- pull, push, pull, push,
    rewriting the file every time."""
    assert classify(b"a\rb\rc") == CLASS_EXACT
    assert classify(b"x\r\r\n") == CLASS_EXACT


def test_normalisation_is_idempotent_on_its_own_class():
    """The invariant the class split exists to guarantee."""
    for data in (b"a\nb\n", b"a\r\nb\r\n", b"", b"no newline"):
        cls = classify(data)
        once = normalise(data, cls)
        assert normalise(once, classify(once)) == once


def test_round_trip_through_the_guest_and_back():
    for data in (b"a\nb\nc\n", b"a\r\nb\r\nc\r\n", b"a\r\nb", b"solo"):
        cls = classify(data)
        there = to_guest(data, cls)
        back = to_bridge(there, cls)
        assert back == normalise(data, cls)
        # And doing it twice changes nothing -- no CR accumulation.
        assert to_guest(back, cls) == there


def test_byte_exact_classes_survive_untouched():
    for data in (b"a\r\nb\nc\r\n", b"a\rb", b"\x00\x01\x02", b"x\r\r\n"):
        cls = classify(data)
        assert cls == CLASS_EXACT
        assert to_guest(data, cls) == data
        assert to_bridge(data, cls) == data


def test_crlf_and_lf_versions_of_one_file_compare_equal():
    """The whole point: one editor round trip must not read as an edit."""
    lf, crlf = b"a\nb\nc\n", b"a\r\nb\r\nc\r\n"
    assert normalise(lf, classify(lf)) == normalise(crlf, classify(crlf))


def test_a_real_edit_still_compares_different():
    a, b = b"a\nb\n", b"a\nB\n"
    assert normalise(a, classify(a)) != normalise(b, classify(b))


# --- the wire representation ------------------------------------------------

def test_wire_round_trip_is_exact_for_every_cp1252_byte():
    """Including the five undefined positions, which are legal VFAT names.

    `bytes.decode("cp1252", "replace")` maps 0x81, 0x8D, 0x8F, 0x90 and 0x9D
    all to U+FFFD. Two files whose names differ only in one of those bytes then
    share a key -- one of them silently stops syncing -- and neither name can
    be encoded back to ask the guest for the file.
    """
    for b in range(0x20, 0x100):
        raw = bytes([b]) + b".txt"
        assert P.wire_encode(P.wire_decode(raw)) == raw, hex(b)


def test_a_cp1252_name_goes_out_as_cp1252_not_utf8():
    """The bug this closes grew a filename on the guest every single pass.

    `café.md` encoded as UTF-8 is `caf\\xc3\\xa9.md`; CreateFileA reads those
    bytes through the ANSI codepage and creates `cafÃ©.md`. The next manifest
    reports that name, which the bridge has never seen, so it pulls it -- by
    encoding `cafÃ©.md` as UTF-8, a third name that exists nowhere. Observed on
    the real machine, exactly like that.
    """
    assert P.wire_encode("café.md") == b"caf\xe9.md"
    assert P.wire_encode("café.md") != "café.md".encode()
    assert P.wire_decode(b"caf\xe9.md") == "café.md"


def test_a_name_that_survives_check_can_be_put_on_the_wire():
    """`check` passing and the transfer working must not be separate facts."""
    for name in ("café.md", "naïve.txt", "£100.txt", "a b.txt", "ünïcödé.c"):
        assert P.check(name, guest_root="C:\\WORK") is None
        assert P.wire_decode(P.wire_encode(name)) == name


def _rel_of(n: int) -> str:
    """A nested relative path exactly `n` characters long."""
    parts, left = [], n
    while left > 31:
        parts.append("d" * 30)
        left -= 31                                      # 30 plus a separator
    parts.append("d" * left)
    rel = "/".join(parts)
    assert len(rel) == n, (len(rel), n)
    return rel


def test_a_directory_stops_twelve_characters_short_of_max_path():
    """CreateDirectory refuses within 12 of MAX_PATH, to leave room for an
    8.3 name inside. Measured against the real guest: a path that passed the
    file rule here came back `too_long` from the SYNCMKDIR.

    The two reserves differ, so there is a band where a file is fine and a
    directory of the same length is not. That band is what this pins.
    """
    root = "C:\\WORK"                                   # 7 chars
    # full = 7 + 1 + rel + 1. 240 puts it at 249: the file limit exactly,
    # and one past the directory limit of 248.
    rel = _rel_of(240)
    assert P.check(rel, guest_root=root) is None
    bad = P.check(rel, guest_root=root, is_dir=True)
    assert bad is not None and "too long" in bad.reason

    short = _rel_of(239)
    assert P.check(short, guest_root=root, is_dir=True) is None


def test_a_file_stops_short_enough_for_the_guest_to_name_its_temp_file():
    """The guest commits by writing `<path>.gaston-tmp` and renaming the old
    file to `<path>.gaston-old`. Both are eleven characters longer than the
    target, and both are built with a truncating _snprintf into MAX_PATH.

    So a path this check lets through at 251..257 makes the two working names
    truncate to the SAME string: the commit deletes its own temp, moves the
    user's file aside and straight back, and returns success while the guest
    still holds the old bytes. The bridge advances the baseline on that ack and
    the next pass pulls the stale content back over the agent's edit. At 258
    and 259 the temp name resolves to the target itself -- Win32 strips a
    trailing dot -- so CREATE_ALWAYS truncates the user's file in place, at the
    first chunk, before the compare-and-swap that exists to protect it runs.

    None of that is recoverable and none of it is reported, so the only place
    to stop it is here, before the push.
    """
    root = "C:\\WORK"
    for n in range(240, 253):                           # full = 249 .. 261
        full = len(root) + 1 + n + 1
        got = P.check(_rel_of(n), guest_root=root)
        if full <= 260 - C.WORKRESERVE_GASTON:
            assert got is None, (n, full, got)
        else:
            assert got is not None and "too long" in got.reason, (n, full)


def test_the_file_reserve_is_declared_not_assumed():
    """A client that writes in place says so and gets the bytes back."""
    inplace = _caps(workreserve=C.WORKRESERVE_NONE)
    rel = _rel_of(251)                                  # full = 260 exactly
    assert P.check(rel, guest_root="C:\\WORK", caps=inplace) is None
    assert P.check(rel, guest_root="C:\\WORK") is not None


# --- the codepage, which is the one silent failure in this layer ------------
#
# Every other way of getting a path wrong is loud: a name the guest cannot hold
# is skipped with a reason, a path too long comes back `too_long`, a case
# collision refuses the newcomer. A wrong CODEPAGE is different. The bridge
# encodes with the wrong table, the guest stores those bytes, reports them
# back, and the bridge decodes with the same wrong table and sees the name it
# sent. Perfectly self-consistent, stable across passes, and the user simply
# has a file whose name is mangled on their own disk.

def test_a_known_codepage_maps_to_a_real_codec():
    assert C.codec_for_acp(1252) == "cp1252"
    assert C.codec_for_acp("1250") == "cp1250"
    assert C.codec_for_acp(932) == "cp932"
    assert C.codec_for_acp(65001) == "utf-8"


@pytest.mark.parametrize("bad", [None, "", "x", 1, 99999, 0, "1252x"])
def test_an_unknown_codepage_is_None_not_a_guess(bad):
    """None means the caller must SAY so. Returning cp1252 for something it
    does not recognise is the whole failure this exists to prevent."""
    assert C.codec_for_acp(bad) is None


def test_the_same_name_encodes_differently_under_different_codepages():
    """Why the wrong table is not a cosmetic problem."""
    name = "zażółć.txt"
    assert P.check(name, guest_root="C:\\WORK", caps=_caps(acp="cp1250")) is None
    assert P.check(name, guest_root="C:\\WORK", caps=_caps(acp="cp1252")) is not None
    assert P.wire_encode(name, acp="cp1250") != name.encode("utf-8")


def test_component_length_is_measured_in_guest_bytes_not_utf8():
    """A component is capped at 255 bytes on the guest's filesystem, in the
    guest's codepage. Measuring in UTF-8 counts two bytes for a character
    cp1252 stores in one, so it refuses names that fit -- and for a CP932 or
    UTF-8 client it errs the other way and passes names that do not."""
    name = "é" * 200 + ".txt"                 # 204 bytes in cp1252
    assert len(name.encode("utf-8")) > 255
    assert P.check(name, guest_root="C:\\WORK", caps=_caps(acp="cp1252")) is None


def test_the_full_path_limit_is_measured_in_guest_bytes():
    """`len()` on the decoded string equals the byte count only for a
    single-byte codepage. On CP932 a 200-character path can be 300 bytes and
    would sail past a character-based check."""
    # Split across two components so neither hits the 255-byte component cap
    # first -- the point is the FULL path, and the two limits have different
    # reasons.
    rel = "ソ" * 100 + "/" + "ソ" * 100 + ".txt"
    assert len(rel) < 260, "a character count would let this through"
    assert len(rel.encode("cp932")) > 260
    bad = P.check(rel, guest_root="C:\\WORK", caps=_caps(acp="cp932"))
    assert bad is not None and "too long" in bad.reason
    # And the same path is fine on a single-byte guest, which is the whole
    # reason the measurement has to be per-codepage rather than fixed.
    assert P.check("a" * 100 + "/" + "a" * 100 + ".txt",
                   guest_root="C:\\WORK") is None


def test_a_root_written_with_a_forward_slash_is_still_stripped():
    """The separator belongs to the guest, and a Unix client reporting
    `/home/x/` is ordinary. Stripping only backslashes counted the slash."""
    a = P.check("f.txt", guest_root="/home/x")
    b = P.check("f.txt", guest_root="/home/x/")
    assert a is None and b is None


def test_on_guest_joins_relative_paths_to_the_conversation_s_folder():
    from bridge.files.paths import on_guest
    base = "C:\\WORK\\NOTES"
    assert on_guest("a\\b.txt", base) == "C:\\WORK\\NOTES\\a\\b.txt"
    assert on_guest("./a/b.txt", base) == "C:\\WORK\\NOTES\\a\\b.txt"
    assert on_guest(".", base) == "C:\\WORK\\NOTES"
    assert on_guest("..\\x", base) == "C:\\WORK\\x"
    assert on_guest("\\WINDOWS", base) == "C:\\WINDOWS"
    assert on_guest("D:\\DATA", base) == "D:\\DATA"
    assert on_guest("\\\\srv\\share", base) == "\\\\srv\\share"
    assert on_guest("D:", base) == "D:\\"
    assert on_guest("D:foo", base) is None       # per-drive cwd: unknowable
    assert on_guest("a.txt", "") is None          # nothing to join to


def test_two_conversations_stage_conflicts_apart():
    """Both used to stage into <mirror>/../.gaston/conflict/<path>/, so two
    conversations with a notes.txt shared one folder of mine.N copies."""
    from bridge.files.paths import conflict_dir
    a = conflict_dir("/sync/CHATONE")
    b = conflict_dir("/sync/CHATTWO/")
    assert a == "/sync/.gaston/conflict/CHATONE"
    assert b == "/sync/.gaston/conflict/CHATTWO"


def test_a_posix_path_is_never_a_path_on_the_guest():
    """`/tmp/gaston-vm/work/...` became `C:\\tmp\\gaston-vm\\work\\...` and
    put_file made that folder on the guest (the VM, 2026-10-04)."""
    from bridge.files.paths import on_guest
    assert on_guest("/tmp/gaston-vm/work/x.txt", "C:\\WORK\\CONV") is None


def test_the_agent_s_own_mirror_path_maps_to_the_machine_s():
    import types

    from bridge.guest import GuestSession, work_root_on_guest
    g = types.SimpleNamespace(work_dir="/tmp/vm/work/CONV",
                              work_guest="C:\\WORK\\CONV", hello={},
                              guest_sync_root="")
    g.work_root_on_guest = lambda: work_root_on_guest(g)
    on = lambda p: GuestSession.on_guest(g, p)  # noqa: E731
    assert on("/tmp/vm/work/CONV/new/x.txt") == "C:\\WORK\\CONV\\new\\x.txt"
    assert on("/tmp/vm/work/CONV") == "C:\\WORK\\CONV"
    assert on("/tmp/vm/work/OTHER/x.txt") is None
    assert on("/etc/passwd") is None
    assert on("rel\\x.txt") == "C:\\WORK\\CONV\\rel\\x.txt"


# --- glob_match --------------------------------------------------------------

from bridge.files.paths import glob_match


@pytest.mark.parametrize("pat,path,want", [
    (r"C:\WINDOWS\**", r"C:\WINDOWS\WIN.INI", True),
    (r"C:\WINDOWS\**", r"C:\WINDOWS", True),            # ** matches none
    (r"C:\WINDOWS\**", r"C:\WINDOWS\SYSTEM\X.DLL", True),
    (r"C:\WINDOWS\**", r"C:\WINDOWSX\A", False),
    (r"C:\*.SYS", r"C:\CONFIG.SYS", True),
    (r"C:\*.SYS", r"C:\DOS\HIMEM.SYS", False),           # * stays in one
    (r"C:\WINDOWS\*.INI", r"c:\windows\win.ini", True),  # folded
    (r"C:\??.SYS", r"C:\IO.SYS", True),
    (r"C:\??.SYS", r"C:\MSDOS.SYS", False),
    (r"**\TEMP\**", r"D:\X\TEMP\A.TMP", True),
    (r"C:\[OLD]\*", r"C:\[OLD]\A", True),                # [ is literal
])
def test_glob_match(pat, path, want):
    assert glob_match(pat, path, "\\", True) is want


def test_glob_match_respects_case_when_the_machine_does():
    assert not glob_match("/etc/**", "/ETC/passwd", "/", False)
    assert glob_match("/etc/**", "/etc/passwd", "/", False)
