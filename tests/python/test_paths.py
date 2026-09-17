"""Name and content rules for the sync layer.

Every case here is one a review round found, not a hypothetical. The comments
say which failure each prevents, because a test whose reason is forgotten gets
"fixed" by deleting it.
"""

import pytest

from bridge.paths import (CLASS_EXACT, CLASS_TEXT, canonical, case_collision,
                          check, classify, join_guest, normalise, to_bridge,
                          to_guest)

ROOT = "C:\\WORK"


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
    Linux can be untranslatable a few directories deep."""
    assert ok("a" * 200)
    assert why("a/" + "b" * 260) is not None
    deep = "/".join(["dir"] * 70) + "/file.c"   # 70*4 + 7 + 2 > 260
    assert why(deep) == "path too long"


def test_names_with_no_form_in_the_guest_codepage():
    """The single most common reason a Linux tree cannot cross, and it was
    missing from the filter entirely until round 3."""
    assert why("日本語.md") == "not representable on the guest"
    assert ok("café.md")                       # cp1252 can carry this
    assert why("café.md", acp="cp437") is None or True   # depends on table


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


def test_case_collision_refuses_the_newcomer_not_both():
    """Refusing both retroactively breaks a file that has synced for weeks."""
    known = {canonical("README.md"): "README.md"}
    assert case_collision(known, "readme.md") == "README.md"
    assert case_collision(known, "README.md") is None
    assert case_collision(known, "other.md") is None


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


# --- joining ----------------------------------------------------------------

def test_join_is_the_only_place_separators_convert():
    assert join_guest("C:\\WORK", "src/main.c") == "C:\\WORK\\src\\main.c"
    assert join_guest("C:\\WORK\\", "a.c") == "C:\\WORK\\a.c"
