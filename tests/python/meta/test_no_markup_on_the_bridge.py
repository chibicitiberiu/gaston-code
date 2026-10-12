"""The bridge emits no markup. This is stage 3's exit condition, as a test.

`ROADMAP.md`'s table states it as a sentence: **"a second client could be
written without touching the bridge."** A sentence is not a gate. Before
this file the only thing standing between that claim and a quiet regression
was somebody remembering it while adding a notice -- and the easiest way to
add a notice has always been to write the `<div>` by hand, which is exactly
how `.thinking` came to be emitted for the whole life of the feature with no
rule anywhere.

So: no file under `bridge/` may contain a markup literal. NO EXCEPTIONS,
since stage 3b.

There was one, for the whole of stage 3: `view/htmlrender.py`, the oracle
that `tests/python/guest/test_render_equivalence.py` compared the guest's
renderer against, byte for byte. Deleting it in that stage would have meant
trusting a new renderer on a platform that cannot be debugged, with no
second opinion. Stage 3b recorded the last passing comparison as a
checked-in fixture and deleted the oracle, and the allowlist went with it --
so this test now says what `ROADMAP.md` always claimed, without a footnote.

WHAT THIS TEST CANNOT SEE, said plainly rather than implied, because a guard
that is trusted beyond its reach is worse than none:

  * an interpolated tag name -- `f"<{tag}>"`, which is how the old renderer
    built `<ul>`/`<ol>` and `<h1>`-`<h6>`. Those three patterns are
    deliberately not in the list below: they could never have matched a
    literal, and listing them would have made the test look stronger than
    it is.
  * a `%`-format or a `.format()` with the brackets in the argument.
  * concatenation: `"<" + "p>"`.

A determined author gets markup past this. Somebody adding a notice in a
hurry does not, and that is the failure this is for.
"""

from __future__ import annotations

import ast
import os
import pathlib
import re

import conftest
import pytest

ROOT = pathlib.Path(conftest.REPO) / "bridge"

#: Directories that are not the bridge's own code. `devtools` holds fakegui,
#: which is a CLIENT -- it is allowed markup for the same reason the Windows
#: client is, and it does not have any.
SKIP_DIRS = {"__pycache__", "prompts", "profiles"}

#: What a markup literal looks like. Closing tags as well as opening ones,
#: because half the ways to get this wrong produce only one of the two --
#: a wrapper split across two literals shows up as a stray `</div>`.
#: `(?<!\?P)` because a REGEX NAMED GROUP looks exactly like a tag:
#: `(?P<code>` and `(?P<em>` are both in blocks.py's inline grammar, and
#: both matched on the first run. A named group is never markup, so the
#: exclusion costs nothing -- but it is the kind of false positive that
#: gets a guard switched off rather than fixed, which is why it is here
#: with its reason rather than as a bare lookbehind.
MARKUP = re.compile(
    r"(?<!\?P)</?(?:p|div|span|pre|code|b|i|em|strong|blockquote|table|tr|td|th"
    r"|ul|ol|li|hr|br|h[1-6]|style|html|head|body|meta|title|img|a)\b"
    r"[^<]*?>",
    re.I,
)


def sources():
    for p in sorted(ROOT.rglob("*.py")):
        rel = p.relative_to(ROOT)
        if SKIP_DIRS & set(rel.parts):
            continue
        yield p, str(rel).replace(os.sep, "/")


def docstring_nodes(tree: ast.AST) -> set:
    """Every string that is a docstring, by identity.

    EXPLICITLY, because a docstring IS an ast.Constant and the walk below
    would otherwise match every comment-shaped explanation of the markup
    this stage removed -- including this file's own reason for existing.
    `bridge/wire/uirec.py` is the one that would have failed first: it
    explains a `<b>` in prose.
    """
    out = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.ClassDef,
                                 ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        body = getattr(node, "body", None)
        if (body and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)):
            out.add(id(body[0].value))
    return out


def markup_literals(path: pathlib.Path) -> list:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    skip = docstring_nodes(tree)
    found = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                and id(node) not in skip):
            for m in MARKUP.findall(node.value):
                found.append((node.lineno, m))
    return found


@pytest.mark.parametrize("path,rel", list(sources()),
                         ids=lambda x: x if isinstance(x, str) else "")
def test_no_file_on_the_bridge_holds_markup(path, rel):
    found = markup_literals(path)
    assert not found, (
        f"{rel} holds markup: {found[:5]}. The bridge sends RECORDS -- what a "
        f"block IS -- and the client decides what it looks like. A second "
        f"client cannot be written without touching this file while that "
        f"literal is in it. If the text genuinely is not markup, widen the "
        f"pattern's exclusion in this file and say why.")


def test_the_guard_can_actually_fail():
    """A grep that matches nothing is indistinguishable from a grep that is
    broken, and this one passes over ~60 files by finding nothing in any of
    them. So it is given something to find."""
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        p = pathlib.Path(d) / "bad.py"
        p.write_text('def f():\n    """A docstring with <p> in it."""\n'
                     '    return "<div class=\\"notice\\">" + x + "</div>"\n',
                     encoding="utf-8")
        found = markup_literals(p)
        # A SET: ast.walk does not visit a BinOp's operands in source
        # order, so the two halves of a concatenated wrapper come back in
        # whichever order the tree gives them.
        assert {m for _, m in found} == {'<div class="notice">', "</div>"}, (
            found)


def test_a_docstring_explaining_markup_is_not_a_violation():
    """The exclusion that makes the rest usable. Half the files on the
    bridge explain in prose what they stopped emitting, and a walk that
    counted those would have to be switched off the week it landed."""
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        p = pathlib.Path(d) / "ok.py"
        p.write_text('"""This used to write <div class="you"> and no '
                     'longer does."""\n\n'
                     'def f():\n    """Returns <p>, conceptually."""\n'
                     '    return 1\n', encoding="utf-8")
        assert markup_literals(p) == []
