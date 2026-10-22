"""The scripts in tools/ and research/ parse, and live where they say they live.

`tools/install-sitter.sh` sat in this repo unable to run. It had an `if`
one-liner followed by a block closed with a bare `fi`, so `bash -n` rejected
it outright -- and nothing in the tree referenced it, so nothing ever tried.
It was deleted rather than guessed at; this is what would have caught it the
day the dangling `fi` was committed.

`bash -n` is a parse, not a run. It will not notice a script that does the
wrong thing, and it cannot: these drive a VM and half of them are
interactive. But "cannot parse" is a real and cheap thing to rule out, and
it is exactly the failure that hides in a script nobody invokes.

The Python half is covered by importing each module, which catches the same
class of thing one level up -- a name used before it was imported, which is
how `syncprobe.py` was found to have no `import os` at all, only
`__import__("os")` inline.
"""

import ast
import os
import re
import shutil
import subprocess

import conftest
import pytest

TOOLS = os.path.join(conftest.REPO, "tools")
RESEARCH = os.path.join(conftest.REPO, "research")

#: One folder per job. Listed rather than discovered, so a new one is a
#: decision somebody made rather than a directory that appeared -- the same
#: rule tests/python/ follows.
FOLDERS = {
    "vm": "drive the guest from outside it",
    "gen": "generate what ships",
    "check": "the static gates make check runs",
}

#: The apparatus. `probes/` was `tools/probe/` until the testing and research
#: trees were split out of the product, and it is checked here still, under
#: the same rule and for the same reasons: not one line of any of them
#: changed in the move, and neither did what can go wrong with one. A probe
#: is what decides what a measurement says, so a name used before it is
#: imported is a number that never arrives.
#:
#: `guest/` is in the list because it is a directory somebody decided on,
#: and contributes nothing to the parse checks: it holds C.
RESEARCH_FOLDERS = {
    "probes": "bridge stubs that drive the guest's own code",
    "guest": "the probes that run ON the machine, in C",
    "deflate-speeds": "one experiment: gate 0.5c and the corpus it needs",
}


def _folders():
    for folder in sorted(FOLDERS):
        yield f"tools/{folder}", os.path.join(TOOLS, folder)
    for folder in sorted(RESEARCH_FOLDERS):
        yield f"research/{folder}", os.path.join(RESEARCH, folder)


def _scripts(ext):
    for label, d in _folders():
        for name in sorted(os.listdir(d)):
            if name.endswith(ext):
                yield f"{label}/{name}", os.path.join(d, name)


@pytest.mark.parametrize("name,path", list(_scripts(".sh")))
def test_a_shell_script_parses(name, path):
    out = subprocess.run(["bash", "-n", path], capture_output=True, text=True)
    assert out.returncode == 0, f"{name} does not parse:\n{out.stderr}"


@pytest.mark.parametrize("name,path", list(_scripts(".py")))
def test_a_python_tool_parses(name, path):
    ast.parse(open(path).read(), path)


@pytest.mark.parametrize("name,path", list(_scripts(".py")))
def test_a_python_tool_imports_what_it_uses(name, path):
    """Every bare name resolves to an import, a definition or a builtin.

    Not a full type check -- it will not catch a wrong argument. It catches
    the one thing that makes a script fail on its first line of real work
    while importing cleanly, which is a module used and never imported.
    """
    tree = ast.parse(open(path).read(), path)
    bound = set(dir(__builtins__)) | {"__file__", "__name__", "__doc__"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            bound |= {(a.asname or a.name).split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            bound |= {a.asname or a.name for a in node.names}
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                               ast.ClassDef)):
            bound.add(node.name)
            bound |= {a.arg for a in getattr(node.args, "args", [])} \
                if hasattr(node, "args") else set()
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            bound.add(node.id)
        elif isinstance(node, (ast.ExceptHandler,)) and node.name:
            bound.add(node.name)
        elif isinstance(node, (ast.comprehension,)):
            for t in ast.walk(node.target):
                if isinstance(t, ast.Name):
                    bound.add(t.id)
        elif isinstance(node, ast.arg):
            bound.add(node.arg)
        elif isinstance(node, ast.alias):
            bound.add(node.asname or node.name.split(".")[0])

    # Only module-level attribute access, e.g. `os.path` -- a bare `foo(...)`
    # would need scope tracking this deliberately does not do.
    used = {n.value.id for n in ast.walk(tree)
            if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name)}
    missing = sorted(u for u in used - bound if not u.startswith("_"))
    assert not missing, f"{name} uses {missing} without importing or defining"


def test_every_tool_lives_in_a_folder():
    """Nothing loose in tools/ or research/, and no folder nobody chose.

    Both trees, in one test rather than two, because the rule is one rule:
    a directory here is a decision somebody made, not a directory that
    appeared. research/ joined when the apparatus was split out of the
    product -- `tools/probe/` became `research/probes/` and `win/checks/`
    became `tests/guest/` and `research/guest/`.
    """
    allowed = {"toolkit.py", "vmlib.sh", "README.md"}
    loose = {n for n in os.listdir(TOOLS)
             if os.path.isfile(os.path.join(TOOLS, n))} - allowed
    assert not loose, f"put these in one of {sorted(FOLDERS)}: {sorted(loose)}"

    dirs = {n for n in os.listdir(TOOLS)
            if os.path.isdir(os.path.join(TOOLS, n))
            and not n.startswith((".", "__"))}
    assert dirs == set(FOLDERS), f"unexpected: {dirs ^ set(FOLDERS)}"

    loose = {n for n in os.listdir(RESEARCH)
             if os.path.isfile(os.path.join(RESEARCH, n))} - {"README.md"}
    assert not loose, (
        f"put these in one of {sorted(RESEARCH_FOLDERS)}: {sorted(loose)}")

    dirs = {n for n in os.listdir(RESEARCH)
            if os.path.isdir(os.path.join(RESEARCH, n))
            and not n.startswith((".", "__"))}
    assert dirs == set(RESEARCH_FOLDERS), (
        f"unexpected: {dirs ^ set(RESEARCH_FOLDERS)}")


#: The one path a script still spells out by hand: put `tools/` on sys.path
#: so `import toolkit` works. TWO spellings, because a script is either one
#: directory inside `tools/` or two inside `research/` -- and that second one
#: exists because the probes DID move out of tools/, which is the thing this
#: file's docstring says a count cannot survive. Neither spelling finds the
#: repo root; `toolkit.REPO` does that by walking up for markers, which is
#: the whole point. Compared with whitespace removed, so a wrapped line
#: cannot slip past the check below on a line break.
FIND_TOOLKIT = (
    "sys.path.insert(0,os.path.dirname(os.path.dirname("
    "os.path.abspath(__file__))))",
    "sys.path.insert(0,os.path.join(os.path.dirname(os.path.dirname("
    'os.path.dirname(os.path.abspath(__file__)))),"tools"))',
)


def test_nothing_counts_directory_levels_to_find_the_repo():
    """The reason toolkit.py and vmlib.sh exist.

    Fifteen scripts each walked up from `__file__` by a hardcoded number of
    `..`, in five spellings. Every one of them was wrong the moment these
    files moved one directory deeper, and wrong SILENTLY -- a generator
    would have written its output into `tools/` instead of `win/resources/`.

    The reach for `toolkit.py` itself is the carved-out exception, and it is
    stripped before the counting below rather than tolerated, so the
    exception cannot grow a second use by accident.
    """
    for name, path in _scripts(".py"):
        flat = re.sub(r"\s+", "", open(path).read())
        assert '__import__("os")' not in flat, name
        found = sum(flat.count(f) for f in FIND_TOOLKIT)
        assert found <= 1, f"{name} reaches toolkit.py {found} times"
        rest = flat
        for f in FIND_TOOLKIT:
            rest = rest.replace(f, "")
        assert "os.path.dirname(os.path.dirname(os.path.dirname(" \
            not in rest, name
        assert rest.count("os.path.dirname(os.path.dirname(") <= 1, name


def test_the_shared_modules_agree_on_the_repo_root():
    """toolkit.py and vmlib.sh find it independently; they must agree."""
    out = subprocess.run(
        ["bash", "-c", f'. "{TOOLS}/vmlib.sh" && printf "%s" "$REPO"'],
        capture_output=True, text=True, check=True)
    assert out.stdout == conftest.REPO, (out.stdout, conftest.REPO)


# --- the generators write where the assets actually live ---------------------


GENERATORS = [("tools/gen/mkicon.py", "win/resources/gaston.ico"),
              ("tools/gen/mkspin.py", "win/resources/spin.gif")]


@pytest.mark.parametrize("script,asset", GENERATORS)
def test_a_generator_reproduces_its_committed_asset(script, asset, tmp_path):
    """Run it and the tree must not change.

    This is not pedantry about reproducibility. Both generators were writing
    into `win/app/`, which is where these assets used to live before they
    moved to `win/resources/` -- their own docstrings had been updated and
    their code had not. Running either one produced sixteen untracked files
    beside the source and left the committed assets untouched, so the
    generator of record silently stopped being the generator of anything.

    Byte-identical is the right assertion because it was ALSO true the whole
    time: the output was correct, just filed in the wrong drawer, which is
    why nothing looked wrong.
    """
    # The SYSTEM python3, not the venv's, because that is how these are run
    # and it is where Pillow is. Checking the venv instead made both of these
    # skip -- a guard that never runs, which is the thing this repo keeps
    # learning about.
    py = shutil.which("python3")
    if py is None:
        pytest.skip("no python3 on PATH")
    have_pil = subprocess.run([py, "-c", "import PIL"], capture_output=True)
    if have_pil.returncode != 0:
        pytest.skip("needs Pillow under python3; the generators draw with it")

    target = os.path.join(conftest.REPO, asset)
    before = open(target, "rb").read()
    out = subprocess.run([py, os.path.join(conftest.REPO, script)],
                         capture_output=True, text=True, cwd=tmp_path)
    assert out.returncode == 0, out.stderr[-800:]
    assert open(target, "rb").read() == before, (
        f"{script} no longer reproduces {asset}")
    # The whole path, not the basename. Checking only "gaston.ico" passed
    # happily against the broken version, because "win/app/gaston.ico" ends
    # in it too -- the assertion could not see the one thing it was for.
    assert asset in out.stdout.replace(os.sep, "/"), (
        f"{script} ran but wrote nothing to {asset}. It said:\n"
        + "\n".join(out.stdout.splitlines()[-4:]))
