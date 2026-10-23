"""The suite's own wiring, checked by the suite.

Three times now the same shape of bug has cost real time: a list of test
files maintained by hand beside a directory that grows, and the growth not
reflected in the list. The conftest docstring describes the first one -- 72
cross-language checks skipping in silence while the run reported success.
The second was `make wine-smoke`, which named eight suites when there were
ten, so `test_guest_workcopy.py` and `test_tooldet_agreement.py` were in no
gate at all. The third was `guest-checks`, missing the two binaries those
same suites need.

The fix for all three was to select by MARK rather than by filename, which
moves the question to "is the mark on it?" -- and that is a question a test
can answer. This file asks it.

Nothing here runs a guest binary or needs a toolchain. It reads the tree.
"""

import ast
import os

import conftest
import pytest

TESTS = os.path.join(conftest.REPO, "tests", "python")

#: One directory per layer, and every suite in one of them. The names are
#: listed rather than discovered so that adding a folder is a decision
#: somebody made, not a directory that appeared.
LAYERS = {"agent", "files", "wire", "view", "server", "guest", "meta", "tools",
          "services", "remote"}

#: What a suite touches when it needs a built guest binary under Wine.
GUEST_API = {
    "guest_exe", "WINE", "wine_env", "have_toolchain",
    "run_guest", "guest_verdicts", "wine_path",
    # gremote listens, so it needs a guest process that STAYS UP and a port
    # to reach it on. Same rule: a suite that touches these needs Wine and
    # has to say so, or it silently rejoins `make test-fast`.
    "GuestServer", "free_port",
}


def _modules():
    """Every suite in the tree, as (layer/name, path).

    Walks, because the suites live one directory down now. This used to
    `listdir` a single flat directory, and when the files moved it kept
    passing while looking at four of them -- which is the same failure it
    exists to catch, so it is worth saying twice: a check that enumerates
    has to enumerate the thing that grew, not the thing it was written
    beside.
    """
    for layer in sorted(LAYERS):
        d = os.path.join(TESTS, layer)
        for name in sorted(os.listdir(d)):
            if name.startswith("test_") and name.endswith(".py"):
                yield f"{layer}/{name}", os.path.join(d, name)


def _uses_guest_api(tree) -> bool:
    """Does this module import or use the Wine plumbing out of conftest?"""
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "conftest":
            if GUEST_API & {a.name for a in node.names}:
                return True
        elif isinstance(node, ast.Attribute) and getattr(node.value, "id", "") == "conftest":
            if node.attr in GUEST_API:
                return True
    return False


def _marks(tree) -> set:
    """The names in a module-level `pytestmark`, however it is spelled."""
    out = set()
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if not any(getattr(t, "id", "") == "pytestmark" for t in node.targets):
            continue
        for sub in ast.walk(node.value):
            if isinstance(sub, ast.Attribute):
                out.add(sub.attr)
            elif isinstance(sub, ast.Name):
                out.add(sub.id)
    return out


@pytest.mark.parametrize("name,path", list(_modules()))
def test_a_suite_that_needs_wine_says_so(name, path):
    """...because otherwise it is in no gate, and looks fine.

    `test_guest_frame.py` was exactly this: it leaned on `guest_exe`'s own
    skip, which works, so nothing was red -- but `-m guest` could not see it
    and neither could the Makefile's list. An unmarked Wine suite also lands
    in `make test-fast`, where it costs the two seconds per test that target
    exists to avoid.

    The filename prefix is not the signal and never was: `test_guest_css.py`
    is pure Python, and `test_tooldet_agreement.py` needs Wine.
    """
    tree = ast.parse(open(path).read(), path)
    if not _uses_guest_api(tree):
        return
    assert "needs_guest" in _marks(tree) or "guest" in _marks(tree), (
        f"{name} imports conftest's Wine plumbing but has no module-level "
        f"`pytestmark = conftest.needs_guest`, so `-m guest` cannot select "
        f"it and `make wine-smoke` will not run it")


def test_every_suite_lives_in_a_layer():
    """Nothing loose at the top of tests/python, and no stray folders.

    The flat directory is what made three of the bugs above possible: a
    fixture imported out of a sibling test module, a Makefile listing
    filenames, and a hygiene check that read one directory. A file dropped
    at the root would quietly reintroduce the first of those, because it
    would be importable by name from anywhere.
    """
    loose = [n for n in os.listdir(TESTS)
             if n.startswith("test_") and n.endswith(".py")]
    assert not loose, f"put these in a layer folder: {loose}"

    dirs = {n for n in os.listdir(TESTS)
            if os.path.isdir(os.path.join(TESTS, n))
            and not n.startswith((".", "__"))}
    assert dirs == LAYERS, f"unexpected: {dirs ^ LAYERS}"


def test_the_shared_scaffolding_is_not_a_test_module():
    """`harness` has to be importable as itself.

    Three suites used to import the `harness` fixture out of the server
    integration suite, which only worked while every suite sat in one
    directory. If the scaffolding moves back inside a test module, those
    imports come back with it.

    Parsed rather than grepped, because the first cut of this check searched
    for the import as a STRING and matched the sentence above -- the check
    failed on its own explanation of itself.
    """
    assert os.path.exists(os.path.join(TESTS, "harness.py"))
    for name, path in _modules():
        tree = ast.parse(open(path).read(), path)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                assert not (node.module or "").startswith("test_"), (
                    f"{name} imports from the test module {node.module!r}; "
                    f"shared scaffolding belongs in harness.py or conftest.py")


def test_the_guest_mark_selects_something():
    """A mark nothing carries deselects nothing, and `make wine-smoke` would
    pass instantly and green while running no guest code whatsoever."""
    marked = [n for n, p in _modules()
              if {"needs_guest", "guest"} & _marks(ast.parse(open(p).read(), p))]
    assert len(marked) >= 10, marked


def test_needs_guest_carries_both_a_selector_and_a_condition():
    """It is a list for a reason, and a future edit could quietly drop half.

    Losing `guest` puts every Wine suite back in the fast run; losing the
    `skipif` makes the suite fail rather than skip on a machine with no Open
    Watcom, which is the one case skipping is actually for.
    """
    names = {m.name for m in conftest.needs_guest}
    assert names == {"guest", "skipif"}, names


# --- bridge.server's public surface -----------------------------------------


def test_everything_in_server_all_actually_resolves():
    """`__all__` is a promise; an entry that does not resolve is a lie.

    It also protects the imports a lint pass would call dead. Two of them are
    reached BY NAME rather than imported: `SessionService._resolve_session_cls`
    asks `hasattr(sys.modules["bridge.server"], "Session")` so a test can swap
    the SDK session for a fake, and tests monkeypatch `srv.sesslist.listing`.
    `ruff --fix` removed both as unused and 248 tests failed at once.
    """
    import bridge.server as srv

    missing = [n for n in srv.__all__ if not hasattr(srv, n)]
    assert not missing, (
        f"bridge.server.__all__ names {missing}, which do not exist. Either "
        f"the import was removed as unused -- it is in __all__ precisely so "
        f"that does not happen -- or the name moved.")


def test_names_reached_through_bridge_server_are_declared():
    """Anything tests reach as `srv.<name>` has to be in `__all__`.

    Otherwise the next lint pass deletes the import and nothing says so until
    the suite runs.
    """
    import pathlib
    import re

    import conftest

    import bridge.server as srv

    root = pathlib.Path(conftest.REPO)
    blob = "\n".join(
        p.read_text(encoding="utf-8", errors="ignore")
        for p in list((root / "tests").rglob("*.py"))
        + list((root / "tools").rglob("*.py"))
        + list((root / "research").rglob("*.py")))
    reached = set(re.findall(r"\bsrv\.(\w+)", blob))
    reached |= set(re.findall(r"\bbridge\.server\.(\w+)", blob))
    undeclared = sorted(
        n for n in reached
        if hasattr(srv, n) and not n.startswith("_") and n not in srv.__all__)
    assert not undeclared, (
        f"reached through bridge.server but not in its __all__: {undeclared}")


# --- comments that outlive the code they describe ---------------------------
#
# Every false comment found in the September cleanup had the same shape: a
# field was deleted or moved, and the block documenting it stayed put --
# either stranded above an unrelated field, or as a section banner with
# nothing left under it. Both are cheap to detect and neither has a
# legitimate use, so they are errors rather than style.

BRIDGE = os.path.join(conftest.REPO, "bridge")


def _bridge_sources():
    for root, dirs, names in os.walk(BRIDGE):
        dirs[:] = [d for d in dirs if d != "__pycache__"]
        for name in sorted(names):
            if name.endswith(".py"):
                yield os.path.join(root, name)


def _next_code_line(lines, i):
    """The first line after `i` that is neither blank nor a comment."""
    for j in range(i + 1, len(lines)):
        stripped = lines[j].strip()
        if stripped and not stripped.startswith("#"):
            return j
    return None


@pytest.mark.parametrize(
    "path", sorted(_bridge_sources()),
    ids=lambda p: os.path.relpath(p, conftest.REPO))
def test_a_doc_comment_block_still_has_something_to_document(path):
    """A `#:` block must be followed by the thing it documents.

    Two stacked `#:` blocks with no field between them means the first one's
    field is gone. That is how `bridge/guest.py` came to explain the halt
    coordinator directly above `guest_sync_root`.
    """
    lines = open(path, encoding="utf-8").read().splitlines()
    for i, line in enumerate(lines):
        if not line.strip().startswith("#:"):
            continue
        if i + 1 < len(lines) and lines[i + 1].strip().startswith("#:"):
            continue                       # still inside the same block
        j = _next_code_line(lines, i)
        assert j is not None, (
            f"{os.path.relpath(path, conftest.REPO)}:{i + 1} `#:` block "
            f"documents nothing -- it runs to the end of the file")
        between = lines[i + 1:j]
        assert not any(b.strip().startswith("#:") for b in between), (
            f"{os.path.relpath(path, conftest.REPO)}:{i + 1} `#:` block is "
            f"stranded: the next thing is another `#:` block, so whatever "
            f"this one documented is gone")


@pytest.mark.parametrize(
    "path", sorted(_bridge_sources()),
    ids=lambda p: os.path.relpath(p, conftest.REPO))
def test_a_section_banner_still_has_a_section(path):
    """A `# --- name ---` banner must have code under it.

    An empty one is left behind when everything it introduced moved out, and
    it then reads as a promise the file does not keep.
    """
    lines = open(path, encoding="utf-8").read().splitlines()
    for i, line in enumerate(lines):
        if not line.strip().startswith("# ---"):
            continue
        j = _next_code_line(lines, i)
        assert j is not None, (
            f"{os.path.relpath(path, conftest.REPO)}:{i + 1} banner "
            f"{line.strip()[:48]!r} introduces nothing -- end of file")
        between = lines[i + 1:j]
        assert not any(b.strip().startswith("# ---") for b in between), (
            f"{os.path.relpath(path, conftest.REPO)}:{i + 1} banner "
            f"{line.strip()[:48]!r} introduces nothing -- the next thing is "
            f"another banner")
