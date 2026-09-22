r"""No operating system is named in anything the bridge SAYS or DECIDES.

The bridge is meant to survive being pointed at a different machine -- the
first standing rule in `CLAUDE.md`. That claim was only half true: the tool
surface was OS-neutral while the command guard's verb tables, the reserved
device names and the shell notes were Python literals in `bridge/agent/`.
Supporting another shell meant editing the guard. Now it means adding
`bridge/profiles/<shell>.toml`.

THIS TEST WALKS STRING LITERALS, NOT COMMENTS, and the distinction is the
whole point rather than a convenience. A string literal is something the
bridge shows a user, sends to a model, writes to a log or compares against --
behaviour. A comment naming Windows 98 is usually the opposite: it is the
record of why a line is the way it is, often something measured the hard way
("AUX with nothing asserting DSR blocks with no timeout"). Deleting those
would remove the reasoning that stops someone re-introducing exactly what was
just taken out.

So: facts about a machine live in profiles, reasons live in comments, and
neither lives in a string.
"""

import ast
import pathlib

import pytest

import conftest

ROOT = pathlib.Path(conftest.REPO) / "bridge"

#: Lowercased needles. Deliberately narrow -- this is about naming a specific
#: platform, not about the word "path".
#: `windows 9x` WITH A SPACE was missing, and `paths.py` was shipping
#: "-- on Windows 9x this is the CON device" straight to the assistant. The
#: test read clean for months while the invariant it exists to protect was
#: broken in a live string, which is worse than having no test: a guard
#: nobody can see failing is one nobody maintains.
BANNED = ("windows 98", "windows 95", "windows 9x", "win 9x",
          "win9x", "win95", "win98",
          "windows 3.1", "windows nt", "ms-dos", "msdos",
          "command.com", "cmd.exe", "4dos", "c:\\")

#: `profiles/` is where naming a machine is the POINT, and it is not Python.
#: `devtools/` is our own test harness, not the product.
SKIP_DIRS = {"profiles", "devtools", "__pycache__"}


def sources():
    for p in sorted(ROOT.rglob("*.py")):
        if SKIP_DIRS & set(p.relative_to(ROOT).parts):
            continue
        yield p


def literals(path: pathlib.Path):
    """Every string constant that is not a docstring.

    Docstrings are excluded for the same reason comments are: they explain,
    they are not shown to anyone at runtime.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    docs = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef,
                             ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", None)
            if (body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                docs.add(id(body[0].value))
    for node in ast.walk(tree):
        if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                and id(node) not in docs):
            yield node.lineno, node.value


@pytest.mark.parametrize("path", list(sources()), ids=lambda p: p.name)
def test_no_operating_system_is_named_in_a_string(path):
    bad = []
    for lineno, text in literals(path):
        low = text.lower()
        for needle in BANNED:
            if needle in low:
                bad.append(f"{path.name}:{lineno} names {needle!r}")
    assert not bad, (
        "A machine is named in a string, which means the bridge either shows "
        "it to someone or decides on it. That fact belongs in "
        "bridge/profiles/<tool>.toml:\n  " + "\n  ".join(bad))


def test_the_profiles_directory_is_where_machines_are_named():
    """The other half: if nothing named a machine anywhere, this test would
    be passing because the knowledge had been deleted rather than moved."""
    names = [p.name for p in (ROOT / "profiles").glob("*.toml")]
    assert names, "no profiles at all -- the knowledge went missing"
    blob = "\n".join((ROOT / "profiles" / n).read_text(encoding="utf-8").lower()
                     for n in names)
    assert "command.com" in blob


def test_a_shell_profile_carries_the_guard_tables():
    """Adding a shell must be adding a file, not editing the guard."""
    from bridge.agent import profiles
    for shell in profiles.shells():
        t = profiles.shell(shell)
        for key in ("read", "write", "devices", "unbounded", "always_ask"):
            assert t.get(key), f"{shell} profile has no {key}"
