"""`.gitattributes`, read natively.

The unit half. The wire half -- a `-text` file actually surviving a push and a
pull byte for byte -- is in test_sync_wire.py, because it needs both sides.
"""
import os

import pytest

from bridge.files import attrs as A
from bridge.files import paths as P


def tree(tmp_path, **files):
    for name, text in files.items():
        full = tmp_path / name
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(text)
    return A.load(str(tmp_path))


def test_a_plain_gitattributes_is_read_without_being_edited(tmp_path):
    """The whole point of reading it natively: a real one is mostly attributes
    this layer has no opinion about, and it must not need a special version."""
    a = tree(tmp_path, **{".gitattributes": (
        "* text=auto\n"
        "*.c text diff=cpp\n"
        "*.png binary\n"
        "*.mk -text\n"
        "Makefile eol=lf\n"
        "# a comment\n"
        "\n")})
    assert a.of("a.png").text is False
    assert a.of("b.mk").text is False
    assert a.of("m.c").text is True
    assert a.of("Makefile").eol == 0


def test_minus_text_forces_byte_exact(tmp_path):
    a = tree(tmp_path, **{".gitattributes": "*.mk -text\n"})
    assert a.effective_class("x.mk", P.CLASS_TEXT) == (P.CLASS_EXACT, None)


def test_binary_is_the_same_request(tmp_path):
    a = tree(tmp_path, **{".gitattributes": "*.dat binary\n"})
    assert a.effective_class("x.dat", P.CLASS_TEXT)[0] == P.CLASS_EXACT


def test_text_cannot_force_a_file_normalisation_would_mangle(tmp_path):
    """Normalisation has to be idempotent or the algorithm oscillates forever.

    A lone CR or a `\\r\\r\\n` is exactly the content where `N` is not, so `text`
    is refused there -- and refused OUT LOUD, because someone who wrote it and
    got the opposite behaviour would otherwise trust a guarantee they do not
    have.
    """
    a = tree(tmp_path, **{".gitattributes": "*.txt text\n"})
    cls, why = a.effective_class("odd.txt", P.CLASS_EXACT)
    assert cls == P.CLASS_EXACT
    assert why and "not reversible" in why


def test_text_on_ordinary_text_is_honoured_silently(tmp_path):
    a = tree(tmp_path, **{".gitattributes": "*.txt text\n"})
    assert a.effective_class("fine.txt", P.CLASS_TEXT) == (P.CLASS_TEXT, None)


def test_eol_overrides_the_clients_own_convention(tmp_path):
    """The Makefile case. CRLF everywhere else, LF for this one path."""
    a = tree(tmp_path, **{".gitattributes": "Makefile eol=lf\n"})
    assert a.eol_for("Makefile", 1) == 0        # client says CRLF, path says LF
    assert a.eol_for("main.c", 1) == 1          # everything else unaffected


def test_eol_keeps_the_path_in_the_text_class(tmp_path):
    """Not the same request as -text, and the difference is the whole reason
    both exist: eol= still compares folded, so the two sides may legitimately
    hold different endings and only the content has to agree."""
    a = tree(tmp_path, **{".gitattributes": "Makefile eol=lf\n"})
    assert a.effective_class("Makefile", P.CLASS_TEXT)[0] == P.CLASS_TEXT


def test_a_nested_file_applies_to_its_own_subtree(tmp_path):
    a = tree(tmp_path, **{
        ".gitattributes": "*.txt text\n",
        "fixtures/.gitattributes": "*.txt -text\n"})
    assert a.effective_class("notes.txt", P.CLASS_TEXT)[0] == P.CLASS_TEXT
    assert a.effective_class("fixtures/crlf.txt", P.CLASS_TEXT)[0] == P.CLASS_EXACT


def test_gastonattributes_wins_over_gitattributes(tmp_path):
    """So a tree can say something to sync without saying it to git."""
    a = tree(tmp_path, **{
        ".gitattributes": "*.txt text\n",
        ".gastonattributes": "*.txt -text\n"})
    assert a.effective_class("x.txt", P.CLASS_TEXT)[0] == P.CLASS_EXACT


def test_the_last_matching_rule_wins_per_attribute(tmp_path):
    a = tree(tmp_path, **{".gitattributes": "* text\n*.bin -text\n"})
    assert a.of("a.bin").text is False
    assert a.of("a.txt").text is True


def test_an_unparseable_line_does_not_take_the_file_with_it(tmp_path):
    a = tree(tmp_path, **{".gitattributes": "nonsense\n*.mk -text\n"})
    assert a.effective_class("x.mk", P.CLASS_TEXT)[0] == P.CLASS_EXACT


def test_no_attributes_file_means_content_decides(tmp_path):
    a = A.load(str(tmp_path))
    assert len(a) == 0
    for cls in (P.CLASS_TEXT, P.CLASS_EXACT):
        assert a.effective_class("a.txt", cls) == (cls, None)


def test_eol_lf_actually_changes_what_goes_on_the_wire():
    """The conversion itself, since that is what the user sees on their disk."""
    from bridge.files import caps as C
    data = b"all: build\n\tmake\n"
    assert P.to_guest(data, P.CLASS_TEXT, C.WIN9X) == b"all: build\r\n\tmake\r\n"
    assert P.to_guest(data, P.CLASS_TEXT, C.WIN9X, eol=0) == data


def test_sync_status_explains_the_rules_to_the_agent(tmp_path, monkeypatch):
    """An agent that cannot see WHY a file is byte-exact reads a deliberate
    `-text` as a misclassification and tries to correct it."""
    from bridge.server import Bridge

    root = tmp_path / "machine"
    root.mkdir()
    (root / ".gitattributes").write_text("*.mk -text\nMakefile eol=lf\n")
    state = tmp_path / "state"
    state.mkdir()

    b = Bridge.__new__(Bridge)
    b.sync_dir = str(root)
    b.sync_state = str(state)
    b.guest_sync_root = "C:\\WORK"
    # A conversation with a folder: without one nothing is mirrored and the
    # status is a sentence saying so rather than a report about a tree.
    from bridge.server import GuestSession, TranscriptView, Paginator
    b._guest = GuestSession(view=TranscriptView(pages=Paginator(4096, 90)))
    b._guest.work_dir = str(root)
    b._guest.work_guest = "C:\\WORK"
    b._last_sync_reports = []
    b.cwd = str(tmp_path)
    text = b.sync_status_text()
    assert "2 in force" in text
    assert ".gitattributes" in text
    assert "-text" in text and "eol=lf" in text
