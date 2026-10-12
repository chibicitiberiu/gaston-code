"""CP1252 folding tests.

The contract that matters: fold() and encode() must NEVER raise, for any input.
They sit on the display path, and an exception here takes out the transcript on
a machine with no debugger.
"""

import pytest

from bridge.wire.fold import decode, decode_oem, encode, fold


def test_ascii_is_untouched():
    s = "Plain ASCII, 0123456789, !@#$%^&*()"
    assert fold(s) == s


def test_cp1252_extras_survive():
    """CP1252 is richer than ASCII and we should use it.

    Latin-1 only, though: everything here is 0xA0 or above, which Win98's
    bitmap UI font renders correctly. The 0x80-0x9F range is representable in
    CP1252 but has no glyphs in that font, so it is folded instead -- see
    test_nothing_in_the_80_9f_range_survives_folding. The euro used to be in
    this list and is not, because on the guest it rendered as a capital I."""
    for ch in "éüñçß£°½¼¾":
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


# --- the CP1252 0x80-0x9F range ---------------------------------------------
#
# Every character here IS representable in CP1252, so the passthrough would
# keep it. They are folded anyway because Win98's stock bitmap UI font has no
# glyphs in that range: on the guest an em dash came out as a vertical bar and
# a euro sign as a capital I. 0xA0 and up is Latin-1 and renders fine.


def test_em_and_en_dashes_become_ascii():
    assert fold("a — b") == "a -- b"
    assert fold("a – b") == "a - b"


def test_euro_becomes_letters():
    assert fold("€10") == "EUR10"


def test_trademark_and_ligatures():
    assert fold("Acme™") == "Acme(tm)"
    assert fold("Œuvre") == "OEuvre"


def test_nothing_in_the_80_9f_range_survives_folding():
    """The property that matters, rather than a list of examples."""
    for b in range(0x80, 0xA0):
        try:
            ch = bytes([b]).decode("cp1252")
        except UnicodeDecodeError:
            continue          # 0x81/0x8D/0x8F/0x90/0x9D are undefined
        folded = fold(ch)
        assert folded.isascii(), f"U+{ord(ch):04X} folded to {folded!r}"


def test_latin1_accents_are_left_alone():
    """Above 0xA0 the font is fine, so folding there would be vandalism."""
    assert fold("café naïve über") == "café naïve über"


# --- the RPC path must not be folded -----------------------------------------
#
# Everything above is about the display path, where an approximate glyph beats
# no glyph. These are the opposite contract, and they exist because EXEC used
# encode() for three phases: the bridge REWROTE the command between the moment
# anything could inspect it and the moment the guest ran it. Found by a red
# team against the path-containment guard, not by this suite.


def test_encode_exact_never_invents_dos_syntax():
    """The four folds that matter, each of which invents DOS syntax.

    A guard that parses the pre-fold string is parsing a different program
    from the one that runs, and no amount of hardening its separator set or
    its `..` resolver can see through that.

    Note the two outcomes, and that BOTH are correct. U+2044 and U+2502 have
    no CP1252 byte, so encode_exact refuses. U+00B7 and U+00BB DO have one
    (0xB7, 0xBB) and pass through as themselves -- fold rewrites them anyway,
    because _fold_char puts the explicit map ahead of the passthrough. What
    the RPC path needs is not "refuse more", it is "never substitute": the
    guest must receive the byte the model wrote, or nothing.
    """
    from bridge.wire.fold import encode_exact

    for ch, invented in (("\u00b7", b"."), ("\u2044", b"/"),
                         ("\u2502", b"|"), ("\u00bb", b">>")):
        assert encode(ch) == invented, "fold no longer rewrites this"
        try:
            out = encode_exact(ch)
        except UnicodeEncodeError:
            continue                      # refused; nothing was invented
        assert out != invented, f"U+{ord(ch):04X} still became {invented!r}"
        assert out == ch.encode("cp1252")


def test_encode_exact_passes_everything_cp1252_can_carry():
    from bridge.wire.fold import encode_exact

    assert encode_exact("dir c:\\work") == b"dir c:\\work"
    # Accented names are real on a CP1252 box and must not be refused.
    assert encode_exact("type c:\\caf\xe9.txt") == "type c:\\café.txt".encode("cp1252")


def test_unrepresentable_names_the_character():
    from bridge.wire.fold import unrepresentable

    assert unrepresentable("dir c:\\work") is None
    assert unrepresentable("caf\xe9") is None
    assert unrepresentable("del x\u2044y") == "\u2044"
    # The FIRST one, so the message names what the reader will look for.
    assert unrepresentable("a\u2502b\u2044c") == "\u2502"


# --- the display codepage is the client's, not a literal ---------------------
#
# `caps.py` negotiates `acp`, maps it through a 28-entry table and REFUSES
# rather than guesses. `paths.py` and `syncrun.py` honour it on every
# filename. This module hardcoded cp1252 at six places and `protocol.py` at
# two more, so the two halves disagreed about what machine they were talking
# to -- and a client whose ANSI page is not 1252 connects today.

import pytest

from bridge.files import caps as C
from bridge.wire import fold as foldmod


@pytest.fixture
def cp1251():
    """A Russian Win98. `acp=1251` is in the table, so it connects."""
    tokens = foldmod.use("cp1251", oem="cp866")
    yield
    for var, tok in zip((foldmod._codec, foldmod._oem), tokens):
        if tok is not None:
            var.reset(tok)


CYRILLIC = "Привет.txt"     # "Privet.txt"


def test_a_name_that_syncs_is_a_name_that_can_be_displayed(cp1251):
    """The bug, stated as the contract it broke.

    The file layer and the display layer have to agree about the machine.
    They did not: this name round-tripped perfectly through `wire_encode`
    and came out `??????.txt` in every transcript line, permission prompt
    and export -- on a client the handshake accepts.
    """
    from bridge.files import paths as P
    caps = C.from_hello({"acp": "1251"})
    assert P.wire_decode(P.wire_encode(CYRILLIC, acp=caps.acp),
                         acp=caps.acp) == CYRILLIC
    assert foldmod.decode(foldmod.encode(CYRILLIC)) == CYRILLIC
    assert b"?" not in foldmod.encode(CYRILLIC)


def test_the_default_is_still_the_restrictive_assumption():
    """Nothing bound means the machine this was built for."""
    assert foldmod.codec() == foldmod.DEFAULT_CODEC
    assert foldmod.oem_codec() == foldmod.DEFAULT_OEM


def test_ascii_folds_identically_under_every_supported_codepage():
    """Why the un-threaded frame builders are safe rather than lucky: every
    page in `caps._ACP_CODECS` is an ASCII superset, so the forty-odd short
    English strings encode the same under all of them."""
    text = "Nobody answered the permission request within ten minutes."
    seen = {foldmod.encode(text, cp) for cp in set(C._ACP_CODECS.values())}
    assert len(seen) == 1


def test_the_console_codepage_is_a_separate_question(cp1251):
    """A Russian machine is cp1251 for its GUI and cp866 for its console.
    `decode_oem` defaulted to cp437 for every machine, and `net.cpp` had
    already written down that these are different questions."""
    assert foldmod.oem_codec() == "cp866"
    # 0x90 is 'R' in cp866 and a control character in cp1251.
    assert foldmod.decode_oem(b"\x90") == "Р"
    assert foldmod.decode_oem(b"\x90", "cp437") == "É"


def test_the_charset_name_and_the_fold_table_come_from_one_place(cp1251):
    """Two independent hardcodings of one assumption: `fold.encode` said
    cp1252 and the <meta> tag said windows-1252. Correctly paired by
    accident, wrong together on any other machine.

    BOTH DOCUMENTS LEFT THIS MACHINE in stage 3 -- the export at C3, the
    live view at C4 -- and the guest derives the name from its own
    GetACP() now. So there is no <meta> here to check against.

    What is still here is the half that makes the pairing possible:
    html_charset and the fold table are driven by the SAME codec, so the
    name this bridge would give and the bytes it actually writes cannot
    come apart. tests/python/meta/test_guest_css.py compares this
    function against the guest's switch, which is the other half, and is
    a stronger check than the one it replaces -- two derivations in two
    languages rather than one value used twice.
    """
    from bridge.files.caps import html_charset

    assert foldmod.codec() == "cp1251"
    assert html_charset(foldmod.codec()) == "windows-1251"
    assert CYRILLIC.encode("cp1251") == foldmod.encode(CYRILLIC)


def test_a_folded_header_uses_one_table_both_ways(cp1251):
    """`protocol.exportend` folded with `fold.encode` and decoded with a
    literal cp1252, so the round trip could invent characters instead of
    removing them."""
    from bridge.wire.protocol import export_end
    f = export_end(False, reason=CYRILLIC)
    assert f.headers["reason"] == CYRILLIC


def test_two_guests_with_different_codepages_do_not_see_each_others():
    """The assumption the context-variable design rests on, asserted rather
    than reasoned about.

    `asyncio.start_server` runs each connection's handler in its own task,
    so each gets its own copy of the context; `_attach` is AWAITED from
    `_on_client`, so binding inside it reaches the connection's own context
    and every task spawned from there afterwards. If either half were false
    -- if `_attach` were spawned rather than awaited, say -- one guest would
    silently take another's codepage, and the symptom would be mojibake on a
    machine nobody changed.
    """
    import asyncio

    async def attach(cp):
        foldmod.use(cp)

    async def spawned():
        return foldmod.codec()

    async def one_connection(cp):
        await attach(cp)
        return cp, foldmod.codec(), await asyncio.create_task(spawned())

    async def both():
        return await asyncio.gather(one_connection("cp1251"),
                                    one_connection("cp932"))

    for want, here, in_task in asyncio.run(both()):
        assert here == want and in_task == want
    # ...and nothing leaked into the process-wide default.
    assert foldmod.codec() == foldmod.DEFAULT_CODEC
