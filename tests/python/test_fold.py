"""CP1252 folding tests.

The contract that matters: fold() and encode() must NEVER raise, for any input.
They sit on the display path, and an exception here takes out the transcript on
a machine with no debugger.
"""

import pytest

from bridge.fold import decode, decode_oem, encode, fold


def test_ascii_is_untouched():
    s = "Plain ASCII, 0123456789, !@#$%^&*()"
    assert fold(s) == s


def test_cp1252_extras_survive():
    """CP1252 is richer than ASCII and we should use it: curly quotes, dashes,
    the ellipsis and the bullet all have real codepoints."""
    for ch in "éüñçß£€°½":
        assert fold(ch) == ch, f"{ch!r} is in CP1252 and should pass through"


@pytest.mark.parametrize(
    "src,want",
    [
        ("→", "->"),
        ("←", "<-"),
        ("⇒", "=>"),
        ("≠", "!="),
        ("≤", "<="),
        ("≥", ">="),
        ("✓", "[x]"),
        ("✗", "[ ]"),
        ("∞", "inf"),
        ("…", "..."),
        ("•", "*"),
        ("│", "|"),
        ("─", "-"),
        ("├", "+"),
        ("█", "#"),
    ],
)
def test_explicit_transliterations(src, want):
    assert fold(src) == want


def test_smart_quotes_become_straight():
    assert fold("“hello” and ‘world’") == '"hello" and \'world\''


def test_accented_latin_is_stripped_not_dropped():
    """Characters outside CP1252 but Latin-adjacent should degrade to their
    base letter rather than to '?'."""
    assert fold("āēī") == "aei"
    assert fold("ř") == "r"


def test_emoji_are_dropped_not_questioned():
    """A rocket in a status line is decoration. ':rocket:' is noisier than
    nothing, and '?' is worse still."""
    assert fold("Done \U0001f680") == "Done "
    assert fold("✨\U0001f389") == ""


def test_cjk_degrades_visibly():
    """Unlike emoji, dropped text would be a silent lie -- show '?'."""
    out = fold("你好")
    assert out == "??"


def test_zero_width_characters_vanish():
    assert fold("a​b﻿c") == "abc"


def test_nbsp_becomes_a_real_space():
    assert fold("a b") == "a b"


def test_box_drawing_tree_stays_readable():
    """Model output draws directory trees constantly; the shape must survive."""
    tree = "├── bridge\n└── win"
    assert fold(tree) == "+-- bridge\n+-- win"


def test_encode_produces_cp1252_bytes():
    assert encode("café") == b"caf\xe9"
    assert encode("a→b") == b"a->b"


def test_encode_never_raises_on_anything():
    """Fuzz the contract. Anything reachable from a model must not crash the
    display path."""
    import sys

    for cp in range(0, min(sys.maxunicode, 0x30000), 97):
        ch = chr(cp)
        try:
            encode(ch)
        except Exception as exc:  # pragma: no cover
            pytest.fail(f"encode raised on U+{cp:04X}: {exc!r}")


def test_encode_handles_surrogates_and_lone_combiners():
    for s in ["\ud800", "\udfff", "́", "á̂̃"]:
        encode(s)  # must not raise


def test_decode_is_the_inverse_for_representable_text():
    for s in ["hello", "café", "naïve", "£1,000"]:
        assert decode(encode(s)) == s


def test_decode_oem_is_separate_from_ansi():
    """CP437 and CP1252 disagree above 0x7F, and conflating them is the classic
    Win9x encoding bug: console output is OEM, everything else is ANSI."""
    b = b"\xc4\xc4\xc4"
    assert decode_oem(b) == "───"   # CP437 box drawing
    assert decode(b) == "ÄÄÄ"       # CP1252 A-umlaut
    assert decode_oem(b) != decode(b)


def test_fold_is_idempotent():
    for s in ["a→b", "“q”", "café", "✓ done"]:
        assert fold(fold(s)) == fold(s)


def test_realistic_model_output():
    src = (
        "Here’s the plan — three steps:\n"
        "• First, run `make check`\n"
        "• Then verify 2 ≤ n ≤ 10\n"
        "✓ Done \U0001f680\n"
        "├── bridge/\n"
        "└── win/\n"
    )
    out = fold(src)
    out.encode("cp1252")  # must not raise
    assert "->" not in out
    assert "[x] Done" in out
    assert "+-- bridge/" in out
    assert "<=" in out
    assert "\U0001f680" not in out
