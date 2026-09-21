"""What the client at the other end is, and why the defaults lean the way they do.

The path rules used to be Windows 9x rules with Windows 9x numbers baked into
the bridge. Every one of them is a property of the CLIENT, and the bridge is
meant to survive being pointed at MS-DOS, Windows 3.1, NT 4 -- or a PDP running
Unix, which shares none of them.
"""
from __future__ import annotations

import pytest

from bridge.files import caps as C
from bridge.files import paths as P


def test_a_client_that_says_nothing_is_the_one_that_already_exists():
    """Absence means the deployed Win9x guest, because that is the only client
    that has ever connected without declaring."""
    c = C.from_hello({})
    assert not c.declared
    assert (c.acp, c.case, c.devices, c.eol) == (
        "cp1252", C.CASE_FOLD, C.DEVICES_DOS, C.EOL_CRLF)
    assert (c.maxpath, c.maxdirpath, c.maxcomp) == (260, 248, 255)


def test_every_default_is_the_restrictive_one():
    """The dangerous interpretation must require positive evidence.

    Guessing `devices=none` on a DOS box means pushing `aux.c`, and CreateFileA
    on AUX with nothing asserting DSR blocks with no timeout -- it wedges the
    guest's worker. Guessing `devices=dos` on Unix skips one file and prints
    why. The two errors are not comparable, so the default is not a coin toss.
    """
    c = C.from_hello({})
    assert c.devices == C.DEVICES_DOS
    assert c.case == C.CASE_FOLD
    assert c.trailing == C.TRAILING_STRIPPED
    assert c.illegal == C.ILLEGAL_DOS


def test_a_unix_client_gets_unix_rules():
    h = {"acp": "65001", "case": "0", "devices": "0", "trailing": "0",
         "illegal": "0", "eol": "0", "shortnames": "0", "dirreserve": "0",
         "maxpath": "4096", "maxcomp": "255"}
    c = C.from_hello(h)
    assert c.declared
    assert c.acp == "utf-8" and c.eol == C.EOL_LF
    assert c.maxdirpath == 4096

    # And the rules actually follow it.
    assert P.check("aux.c", guest_root="/home/u", caps=c) is None
    assert P.check("notes.", guest_root="/home/u", caps=c) is None
    assert P.check("a:b.txt", guest_root="/home/u", caps=c) is None
    assert P.canonical("Makefile", caps=c) == "Makefile"
    assert P.to_guest(b"a\nb\n", P.CLASS_TEXT, c) == b"a\nb\n"


def test_the_same_names_are_still_refused_for_a_dos_client():
    c = C.from_hello({})
    assert P.check("aux.c", guest_root="C:\\WORK", caps=c) is not None
    assert P.check("notes.", guest_root="C:\\WORK", caps=c) is not None
    assert P.canonical("Makefile", caps=c) == "makefile"
    assert P.to_guest(b"a\nb\n", P.CLASS_TEXT, c) == b"a\r\nb\r\n"


def test_universal_rules_are_not_negotiable():
    """A backslash and a control byte are refused for EVERY client.

    NUL ends a C string, 0x0A ends a manifest line -- and the bridge splits the
    manifest on newlines before it ever reads an entry's length prefix, so a
    newline in a name breaks the parse whatever the filesystem allows. The
    backslash is illegal because the wire separator is `/` and it must never
    arrive inside a component.
    """
    unix = C.POSIX
    assert P.check("a\\b.txt", guest_root="/home/u", caps=unix) is not None
    assert P.check("a\nb.txt", guest_root="/home/u", caps=unix) is not None
    assert P.check("a\x00b.txt", guest_root="/home/u", caps=unix) is not None
    assert P.check("../escape", guest_root="/home/u", caps=unix) is not None


@pytest.mark.parametrize("field,value", [
    ("case", "2"), ("devices", "7"), ("eol", "9"), ("illegal", "-1"),
    ("case", "yes"), ("maxpath", "3"), ("dirreserve", "999"),
])
def test_a_value_the_bridge_does_not_know_is_refused_not_defaulted(field, value):
    """The difference from a packed bitfield, stated as a test.

    With bits, a value the bridge does not understand is indistinguishable
    from the bit being clear -- which is the permissive reading, arrived at in
    silence. Here it stops and names the field.
    """
    with pytest.raises(C.CapsError) as e:
        C.from_hello({field: value})
    assert field in str(e.value)


def test_an_unknown_codepage_is_refused_because_it_cannot_be_detected():
    """Every other wrong answer here is loud. This one round-trips.

    The bridge would encode a name with the wrong table, the client stores
    those bytes, reports them back, and the bridge decodes with the SAME wrong
    table and sees the name it sent. Self-consistent forever, and the user has
    a file called something else on their own disk.
    """
    with pytest.raises(C.CapsError) as e:
        C.from_hello({"acp": "99999"})
    assert "mangled" in str(e.value)


def test_adding_an_option_does_not_break_either_side():
    """The extensibility claim, as a test.

    A field a bridge has never heard of is ignored; a field a client does not
    send takes the documented default. Neither side needs the other's version,
    and there is no width to run out of.
    """
    c = C.from_hello({"case": "0", "some_future_field": "42"})
    assert c.case == C.CASE_SENSITIVE
    assert c.devices == C.DEVICES_DOS          # untouched by the unknown field


def test_dirreserve_is_a_number_so_a_new_client_needs_no_new_field():
    c = C.from_hello({"dirreserve": "4", "maxpath": "512"})
    assert c.maxdirpath == 508


def test_the_summary_says_whether_it_was_declared_or_assumed():
    """Because "why did that not sync" starts by asking what the bridge
    thought it was talking to."""
    assert "assumed" in C.from_hello({}).summary()
    assert "declared" in C.from_hello({"case": "1"}).summary()


def test_eol_cr_is_refused_at_the_handshake():
    """Declarable is not the same as implemented.

    to_guest would convert to lone CRs, but the guest normalises only CRLF --
    so its crc_norm still contains them, the compare-and-swap never matches,
    and every text file comes back crc_mismatch on every pass, forever. Loud,
    but the log points at corruption rather than at a capability the bridge
    cannot honour.
    """
    with pytest.raises(C.CapsError) as e:
        C.from_hello({"acp": "1252", "eol": str(C.EOL_CR)})
    assert "eol" in str(e.value)


def test_the_two_endings_we_do_implement_still_pass():
    assert C.from_hello({"acp": "1252", "eol": "0"}).eol == C.EOL_LF
    assert C.from_hello({"acp": "1252", "eol": "1"}).eol == C.EOL_CRLF


def test_eol_cr_is_refused_per_path_too():
    """A handshake refusal is only half of it: attrs are consulted per PATH,
    so `*.txt eol=cr` in a real .gitattributes would reach a client that
    declared itself perfectly legally."""
    from bridge.files import attrs as A
    assert "cr" not in A.EOL_NAMES


# --- capabilities the client declared and the bridge threw away --------------
#
# Both of these were sent by the guest, counted toward `declared`, and then
# discarded -- so the bridge took the permissive branch on a client that had
# asked in writing for the restrictive one, which is the exact failure the
# restrictive-default design in this module exists to prevent.

def test_shortnames_is_read_and_not_merely_counted():
    """`from_hello({'shortnames':'0'}) == from_hello({'shortnames':'1'})` was
    True: the key appeared only in the `declared` tuple and had no field.

    The guest has sent RC_CAP_SHORTNAMES since the handshake was written
    (win/net/net.cpp), and `Bridge.FOLDER_MAX = 8` hardcoded the very rule it
    describes.
    """
    short = C.from_hello({"shortnames": "1"})
    long_ = C.from_hello({"shortnames": "0"})
    assert short != long_
    assert short.shortnames == C.SHORTNAMES_83
    assert long_.shortnames == C.SHORTNAMES_NONE
    with pytest.raises(C.CapsError):
        C.from_hello({"shortnames": "2"})


def test_maxdepth_is_declared_like_every_other_client_rule():
    """It lived in `paths.MAX_DEPTH` as a bare 16 mirroring the guest's own
    RC_SYNC_MAX_DEPTH, with sixty lines explaining that it belongs to that
    client's walk -- and every client got it.

    Wrong in both directions: a deeper walker has trees refused for nothing,
    a shallower one gets the silent never-scanned-again bug the comment
    describes.
    """
    assert C.from_hello({}).maxdepth == C.MAXDEPTH_GASTON
    assert C.from_hello({"maxdepth": "8"}).maxdepth == 8
    assert C.POSIX.maxdepth > C.MAXDEPTH_GASTON
    with pytest.raises(C.CapsError):
        C.from_hello({"maxdepth": "0"})


def test_a_shallower_client_has_its_own_limit_enforced():
    """The check has to read the DECLARED depth, not the default."""
    from bridge.files import paths as P
    shallow = C.from_hello({"maxdepth": "2"})
    deep = "a/b/c/d/e/f.txt"
    assert P.check(deep, guest_root="C:\\WORK", caps=shallow) is not None
    assert P.check(deep, guest_root="C:\\WORK", caps=C.from_hello({})) is None


def test_the_summary_names_both_so_a_failed_sync_can_be_read():
    s = C.from_hello({"shortnames": "0", "maxdepth": "9"}).summary()
    assert "maxdepth=9" in s and "shortnames=long" in s
