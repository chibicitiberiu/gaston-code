"""Pattern matching for the sync scope, and the scope-change rule.

Two things are being protected here. The obvious one is that `.venv/` and
`vm/*.qcow2` must not arrive on a machine with 1.7GB free. The subtle one is
that EDITING the pattern set is routine -- one line added to `.gitignore` can
remove a thousand paths from the scan -- and the natural reading of "present
last pass, absent now" is that the user deleted them. Acting on that would
propagate a mass deletion nobody asked for.
"""
from __future__ import annotations

import pytest

from bridge.files import ignores as I


def m(*patterns: str):
    return I.Ignores([I._Rule(p, "") for p in patterns], [])


@pytest.mark.parametrize("pattern,path,want", [
    # The basename rule: an unanchored pattern matches at any depth.
    ("*.o", "a.o", True),
    ("*.o", "src/deep/a.o", True),
    ("*.o", "a.object", False),
    # ...but `*` does not cross a separator, which fnmatch gets wrong.
    ("src/*.c", "src/a.c", True),
    ("src/*.c", "src/deep/a.c", False),
    # An excluded directory excludes everything beneath it.
    ("build/", "build/", True),
    ("build/", "build/x.c", True),
    ("build/", "build/deep/x.c", True),
    ("build/", "mybuild/x.c", False),
    # A trailing slash means directories only.
    ("cache/", "cache.txt", False),
    # `**`.
    ("**/tmp/", "a/b/tmp/x", True),
    ("**/tmp/", "tmp/x", True),
    # Character classes.
    ("[abc].txt", "b.txt", True),
    ("[abc].txt", "d.txt", False),
    ("[!abc].txt", "d.txt", True),
])
def test_patterns(pattern, path, want):
    assert m(pattern)(path) is want


def test_negation_is_last_rule_wins():
    """`!` is the only reason rule order matters, and it is why a plain set
    of patterns is not enough."""
    n = m("*.o", "!keep.o")
    assert n("a.o") is True
    assert n("keep.o") is False
    # Order matters: reversed, the exclusion wins again.
    r = m("!keep.o", "*.o")
    assert r("keep.o") is True


def test_a_directory_must_be_asked_about_with_a_trailing_slash():
    """The contract, stated as a test because getting it wrong is silent.

    `build/` is a pattern about a directory. Asking `m("build")` is asking
    about a FILE called build, and the honest answer is no. A caller that holds
    a manifest row knows the type and has to say so -- `Pass._ignores` appends
    the slash for DIR rows for exactly this reason, and without it a directory
    that should have left the scan would have stayed in it.
    """
    n = m("build/")
    assert n("build/") is True
    assert n("build") is False


def test_defaults_cover_what_every_tree_has(tmp_path):
    n = I.load(str(tmp_path))
    for p in (".git/config", "__pycache__/x.pyc", ".venv/lib/x.py",
              "node_modules/a/b.js", "vm/disk.qcow2", "a.o",
              "x.md.gaston-tmp", "x.md.gaston-old"):
        assert n(p) is True, p
    for p in ("src/main.c", "README.md", "docs/a.md"):
        assert n(p) is False, p


def test_nested_gitignore_applies_to_its_own_subtree(tmp_path):
    """git reads them at every level, and a tree whose `src/.gitignore` was
    skipped would push build output from the directory the user was most
    careful about."""
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / ".gitignore").write_text("generated/\n*.tmp\n")
    (tmp_path / "docs").mkdir()
    n = I.load(str(tmp_path))
    assert n("src/generated/x.c") is True
    assert n("src/a.tmp") is True
    assert n("docs/a.tmp") is False, "a nested rule escaped its subtree"


def test_root_gitignore_and_gastonignore_are_both_read(tmp_path):
    (tmp_path / ".gitignore").write_text("*.log\n")
    (tmp_path / ".gastonignore").write_text("notes/\n")
    n = I.load(str(tmp_path))
    assert n("a.log") is True
    assert n("notes/x.md") is True
    assert len(n.sources) == 2


def test_comments_and_blank_lines(tmp_path):
    (tmp_path / ".gitignore").write_text("# a comment\n\n   \n*.log\n")
    n = I.load(str(tmp_path))
    assert n("a.log") is True
    assert n("a comment") is False
