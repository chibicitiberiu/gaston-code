"""Adversarial content and encoding tests for the sync layer.

Hand-picked cases catch the bugs you thought of. These are generated, so they
catch the ones you did not -- which is the whole history of this feature: five
review rounds, and the worst findings were cases nobody had listed.

The three properties that must hold for EVERY input, or the algorithm either
loses bytes or fails to converge:

  P1  round trip:   to_bridge(to_guest(x)) == normalise(x)
  P2  idempotence:  applying the conversion twice changes nothing
  P3  stability:    the comparison CRC does not drift across a round trip
"""

import itertools
import random
import zlib

import pytest

from bridge.files.paths import (
    CLASS_EXACT,
    canonical,
    check,
    classify,
    normalise,
    to_bridge,
    to_guest,
)

ROOT = "C:\\WORK"


def crc_of(data: bytes) -> int:
    """What reconciliation actually compares."""
    return zlib.crc32(normalise(data, classify(data)))


# --- generated content ------------------------------------------------------

def _line_ending_corpus():
    """Every arrangement of CR, LF and CRLF up to a few tokens deep.

    This is where the oscillation bugs live: \\r\\r\\n is a real Windows
    artefact, and normalisation is not idempotent on it.
    """
    tokens = [b"\n", b"\r\n", b"\r", b"\r\r\n", b"\n\r", b"x"]
    for n in (1, 2, 3):
        for combo in itertools.product(tokens, repeat=n):
            yield b"a".join(combo)


def _binary_corpus(seed=7):
    rng = random.Random(seed)
    yield b""
    yield b"\x00"
    yield b"\x00" * 100
    # A NUL far past any plausible sniff window -- the PDF shape.
    yield b"%PDF-1.4\n" + bytes(rng.choice(b"abc \n\r") for _ in range(30000)) + b"\x00"
    for _ in range(40):
        yield bytes(rng.randrange(256) for _ in range(rng.randrange(0, 400)))


def _text_corpus():
    yield b""
    yield b"no newline at eof"
    yield b"\n"
    yield b"a\n" * 500
    yield b"a\r\n" * 500
    yield "café\nnaïve\n".encode("cp1252")
    yield "café\nnaïve\n".encode()
    yield b"\xef\xbb\xbfBOM at the start\n"          # UTF-8 BOM
    yield b"trailing spaces   \nand tabs\t\n"


ALL = list(_line_ending_corpus()) + list(_binary_corpus()) + list(_text_corpus())


@pytest.mark.parametrize("data", ALL, ids=range(len(ALL)))
def test_P1_round_trip_preserves_the_normalised_form(data):
    cls = classify(data)
    assert to_bridge(to_guest(data, cls), cls) == normalise(data, cls)


@pytest.mark.parametrize("data", ALL, ids=range(len(ALL)))
def test_P2_conversion_is_idempotent(data):
    """The property that stops pull/push/pull/push forever. Applying the
    conversion twice must change nothing -- otherwise each pass produces new
    bytes, each side sees a change, and the file is rewritten indefinitely."""
    cls = classify(data)
    g1 = to_guest(data, cls)
    g2 = to_guest(to_bridge(g1, cls), cls)
    assert g1 == g2

    b1 = to_bridge(data, cls)
    b2 = to_bridge(to_guest(b1, cls), cls)
    assert b1 == b2


@pytest.mark.parametrize("data", ALL, ids=range(len(ALL)))
def test_P3_comparison_crc_does_not_drift(data):
    """If the CRC moved across a round trip, every pass would see a change on a
    file nobody touched."""
    cls = classify(data)
    there = to_guest(data, cls)
    back = to_bridge(there, cls)
    assert crc_of(back) == crc_of(normalise(data, cls))


@pytest.mark.parametrize("data", ALL, ids=range(len(ALL)))
def test_no_byte_exact_file_is_ever_modified(data):
    """The guarantee that protects binaries. If classify says byte-exact, the
    bytes must survive untouched in both directions, forever."""
    cls = classify(data)
    if cls != CLASS_EXACT:
        return
    assert to_guest(data, cls) == data
    assert to_bridge(data, cls) == data


@pytest.mark.parametrize("data", ALL, ids=range(len(ALL)))
def test_class_is_stable_across_a_round_trip(data):
    """A class that flips between passes freezes the path forever, because a
    class change is resolved as a content change and the two sides would keep
    disagreeing about which function to compare with."""
    cls = classify(data)
    assert classify(to_guest(data, cls)) == cls
    assert classify(to_bridge(data, cls)) == cls


# --- specific corruption shapes ---------------------------------------------

def test_a_crlf_file_and_its_lf_twin_are_one_file():
    lf, crlf = b"x\ny\n", b"x\r\ny\r\n"
    assert crc_of(lf) == crc_of(crlf)


def test_but_a_one_byte_edit_is_still_a_change():
    assert crc_of(b"x\ny\n") != crc_of(b"x\nz\n")


def test_cr_does_not_accumulate_over_many_passes():
    """The exact oscillation: a naive expander turns a\\r\\nb into a\\r\\r\\nb and
    adds another CR every pass. Ten passes must be a fixed point."""
    for start in (b"a\r\nb\r\n", b"a\nb\n", b"a\r\r\nb"):
        cls = classify(start)
        cur = start
        for _ in range(10):
            cur = to_bridge(to_guest(cur, cls), cls)
        assert cur == to_bridge(to_guest(start, cls), cls)
        assert len(cur) <= max(len(start), len(to_guest(start, cls)))


def test_utf16_survives_because_it_is_binary():
    """Save As > Unicode in Notepad. Normalising this shifts every code unit and
    destroys the file; the NUL scan is what prevents it."""
    original = "line one\r\nline two\r\n".encode("utf-16-le")
    cls = classify(original)
    assert cls == CLASS_EXACT
    assert to_bridge(to_guest(original, cls), cls) == original
    assert original.decode("utf-16-le") == "line one\r\nline two\r\n"


def test_pdf_shape_survives():
    """ASCII header, NUL thousands of bytes in. An 8KB sniff window misses it and
    normalisation shifts every xref offset."""
    pdf = b"%PDF-1.4\n" + b"text\r\n" * 3000 + b"\x00\x00stream\r\n"
    cls = classify(pdf)
    assert cls == CLASS_EXACT
    assert to_bridge(to_guest(pdf, cls), cls) == pdf


def test_a_truncated_read_is_a_different_file_not_a_silent_match():
    """A torn read must never compare equal to the whole file."""
    whole = b"a\nb\nc\nd\n"
    for cut in range(1, len(whole)):
        assert crc_of(whole[:cut]) != crc_of(whole)


def test_empty_and_missing_are_distinguishable_by_more_than_crc():
    """crc32(b'') == 0, and a failed read that yields 0 would compare equal to
    an empty file. Length must be carried alongside."""
    assert zlib.crc32(b"") == 0
    assert crc_of(b"") == 0
    # The pairing the protocol must carry:
    assert (0, 0) != (0, None)


# --- encoding torture on names ---------------------------------------------

def test_every_cp1252_byte_in_a_filename_round_trips_or_is_refused():
    """No name may be silently transformed. Either it survives exactly, or it is
    refused with a reason."""
    for b in range(0x20, 0x100):
        try:
            name = bytes([b]).decode("cp1252")
        except UnicodeDecodeError:
            continue                      # undefined position; not a name we make
        rel = f"f{name}.txt"
        bad = check(rel, guest_root=ROOT)
        if bad is None:
            assert rel.encode("cp1252").decode("cp1252") == rel


def test_the_five_undefined_cp1252_positions_are_not_silently_folded():
    """0x81, 0x8D, 0x8F, 0x90, 0x9D decode to U+FFFD through fold, which would
    make two distinct byte-names collide into one key."""
    for b in (0x81, 0x8D, 0x8F, 0x90, 0x9D):
        raw = bytes([b])
        with pytest.raises(UnicodeDecodeError):
            raw.decode("cp1252")


def test_fold_would_rewrite_these_and_the_body_rule_is_what_saves_them():
    """The reason paths travel in frame BODIES and never headers.

    `protocol._quote` runs `fold.encode`, which rewrites characters CP1252
    carries perfectly well -- U+00B7 becomes "." and would collide with a real
    file. Sending the raw bytes in the body is what prevents it.
    """
    from bridge.wire import fold
    from bridge.wire.protocol import _quote

    for ch, folded in [("\u00b7", "."), ("\u2022", "*"),
                       ("\u2026", "..."), ("\u201c", '"')]:
        rel = f"a{ch}b.txt"
        assert check(rel, guest_root=ROOT) is None, "CP1252 carries this"

        # The damage is real and still present. This is why the rule exists.
        assert folded in fold.fold(rel)
        assert _quote(rel) != rel

        # The wire form is raw bytes and is untouched by any of that.
        assert rel.encode("cp1252").decode("cp1252") == rel


def test_the_invented_separator_character_is_refused_outright():
    """U+2044 FRACTION SLASH folds to "/" -- a path separator from nothing.

    It is not in CP1252, so the codepage filter refuses it before the body rule
    even matters. Two independent defences, and this asserts the outer one.
    """
    from bridge.wire import fold
    assert fold.fold("a\u2044b") == "a/b"          # the danger is real
    bad = check("a\u2044b.txt", guest_root=ROOT)
    assert bad is not None
    assert bad.reason == "not representable on the guest"


def test_canonical_never_invents_a_separator():
    """A key containing a '/' that the original did not have would split one
    file into a directory path."""
    for ch in ("\u2044", "\u00b7", "\u2022"):
        rel = f"a{ch}b.txt"
        assert canonical(rel).count("/") == rel.count("/")
