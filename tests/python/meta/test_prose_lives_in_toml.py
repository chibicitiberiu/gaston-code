r"""Prose the agent or the user reads comes from `bridge/prompts/*.toml`.

A sentence in a `.py` file can only be changed by editing the program, and
`if x: prompt += "..."` scatters the text of a prompt across the branches that
assemble it. Facts about a machine already live in `bridge/profiles/`; the
words the agent reads belong in `bridge/prompts/` for the same reason.

WHAT IS EXEMPT, and the distinction is the point rather than a convenience:

  * **Log and exception messages.** A developer reads those next to the line
    that raised them, and templating breaks the grep that finds them.
  * **Docstrings and comments.** The project's reasoning lives beside the code
    it explains; this rule does not touch it.
  * **Protocol vocabulary** -- frame types, header keys, status tokens. Wire
    grammar, not prose.
  * **`bridge/view/html.py`** -- the stylesheet and the document skeleton are
    presentation. If they ever move it is to a `.css` asset, not to TOML.

THIS IS A RATCHET, not a wall. Each file carries a byte budget measured when
the rule landed; going under one is free and needs no edit here. Going over
means either the prose belongs in a `.toml`, or the budget moves up in the
same commit that spends it, with a reason.
"""

import ast
import pathlib

import pytest

import conftest

ROOT = pathlib.Path(conftest.REPO) / "bridge"

#: Not the product, or not prose. See the docstring.
SKIP_DIRS = {"profiles", "prompts", "devtools", "__pycache__"}
SKIP_FILES = {"html.py"}

#: A literal shorter than this is a word or a token, not a sentence.
MIN_PROSE = 120

#: file -> bytes of non-docstring prose it may still carry. MEASURED, not
#: chosen: this is what was inline when the rule landed. Only ever lower it.
BUDGET = {
    "agent/permissions.py": 2492,
    "services/sync.py": 2103,
    "services/workspace.py": 650,
    "files/syncrun.py": 433,
    "services/turn_runner.py": 415,
    "services/options.py": 259,
    "agent/permdesk.py": 257,
    "status.py": 213,
    "services/guest_exec.py": 211,
    "services/session.py": 153,
    "services/connection.py": 151,
    "files/attrs.py": 151,
    "agent/cmdguard.py": 139,
    "files/sync.py": 136,
}


def _exempt_nodes(tree: ast.AST) -> set:
    """String literals that are arguments to logging or to an exception."""
    out = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Call):
            f = n.func
            is_log = (isinstance(f, ast.Attribute)
                      and isinstance(f.value, ast.Name)
                      and f.value.id in {"log", "logging", "logger"})
            is_exc = (isinstance(f, ast.Name)
                      and (f.id.endswith("Error") or f.id.endswith("Exception")))
            if is_log or is_exc:
                for a in list(n.args) + [k.value for k in n.keywords]:
                    for s in ast.walk(a):
                        if isinstance(s, ast.Constant) and isinstance(s.value, str):
                            out.add(id(s))
        if isinstance(n, ast.Raise):
            for s in ast.walk(n):
                if isinstance(s, ast.Constant) and isinstance(s.value, str):
                    out.add(id(s))
    return out


def _docstrings(tree: ast.AST) -> set:
    out = set()
    for n in ast.walk(tree):
        if isinstance(n, (ast.Module, ast.ClassDef,
                          ast.FunctionDef, ast.AsyncFunctionDef)):
            b = getattr(n, "body", None)
            if (b and isinstance(b[0], ast.Expr)
                    and isinstance(b[0].value, ast.Constant)
                    and isinstance(b[0].value.value, str)):
                out.add(id(b[0].value))
    return out


def prose_bytes(path: pathlib.Path) -> int:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    skip = _docstrings(tree) | _exempt_nodes(tree)
    total = 0
    for n in ast.walk(tree):
        if (isinstance(n, ast.Constant) and isinstance(n.value, str)
                and id(n) not in skip and len(n.value) >= MIN_PROSE):
            total += len(n.value)
    return total


def sources():
    for p in sorted(ROOT.rglob("*.py")):
        if SKIP_DIRS & set(p.relative_to(ROOT).parts):
            continue
        if p.name in SKIP_FILES:
            continue
        yield p


@pytest.mark.parametrize("path", list(sources()),
                         ids=lambda p: str(p).split("bridge/")[-1])
def test_prose_stays_within_its_budget(path):
    rel = str(path.relative_to(ROOT))
    got = prose_bytes(path)
    allowed = BUDGET.get(rel, 0)
    assert got <= allowed, (
        f"{rel} carries {got} bytes of prose, over its {allowed}-byte budget. "
        f"Sentences the agent or the user reads belong in "
        f"bridge/prompts/*.toml -- see bridge/agent/prompts.py. If this text "
        f"genuinely is not prose, raise the budget in this file and say why.")


def test_the_prompts_directory_is_where_prose_lives():
    """The other half: if nothing held any prose, this would pass because the
    words had been deleted rather than moved."""
    import tomllib
    files = sorted((ROOT / "prompts").glob("*.toml"))
    assert files, "no prompt files at all -- the words went missing"
    total = 0
    for f in files:
        with f.open("rb") as fh:
            tomllib.load(fh)
        total += len(f.read_text(encoding="utf-8"))
    assert total > 5000, f"only {total} bytes of prompt text"
