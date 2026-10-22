"""What the scripts in tools/ all needed and each worked out for itself.

Every tool here has to find the repo root, because it imports `bridge` or
reads `win/` or writes `vm/`. Nine of them computed it by hand and in three
different spellings -- `os.path.dirname` chained twice, the same chained
three times, and `__import__("os").path.dirname(...)` inline. All nine
counted directory levels, so all nine broke the moment these scripts moved
one directory deeper, which is exactly what happened.

So it is found rather than counted. Walk up until a directory looks like
this repo; the answer does not change when a script moves, and a tool run
from a copied checkout finds ITS root rather than the one that was true when
somebody wrote the constant.

    import toolkit
    toolkit.on_path()            # so `import bridge...` works
    from bridge.wire import protocol

Getting to this module is the one path a script still spells out. For a
script under `tools/` that is one level up to a sibling directory:

    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

The probes moved out of `tools/` to `research/probes/`, and that is the
sentence this file used to carry -- "a sibling directory that is not going
to move" -- happening anyway. `REPO` did not notice, because it is found
rather than counted; only this one line had to change, to up three and back
down into `tools/`:

    sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
        os.path.dirname(os.path.abspath(__file__)))), "tools"))
"""

from __future__ import annotations

import os
import sys

#: What makes a directory this repo rather than some parent of it. Two
#: markers, not one: `.git` alone is wrong in a worktree or a submodule, and
#: CLAUDE.md alone would match a copy of the file anywhere.
MARKERS = ("CLAUDE.md", "bridge", "win")


def _find_repo() -> str:
    d = os.path.dirname(os.path.abspath(__file__))
    while True:
        if all(os.path.exists(os.path.join(d, m)) for m in MARKERS):
            return d
        parent = os.path.dirname(d)
        if parent == d:
            raise RuntimeError(
                "cannot find the repo root above %s -- looked for %s"
                % (os.path.abspath(__file__), ", ".join(MARKERS)))
        d = parent


#: The repo root. One spelling, found not counted.
REPO = _find_repo()

#: The libvirt domain and URI. Named here because five scripts had their own
#: copy of the string and three of those had no environment override, so
#: pointing the tools at a second VM meant editing three files.
DOMAIN = os.environ.get("RC_DOMAIN", "retroclaude-win98")
URI = os.environ.get("LIBVIRT_DEFAULT_URI", "qemu:///session")


def on_path() -> str:
    """Put the repo root on `sys.path` so `import bridge...` resolves."""
    if REPO not in sys.path:
        sys.path.insert(0, REPO)
    return REPO


def repo_path(*parts) -> str:
    """A path inside the repo, e.g. `repo_path("vm", "tools.iso")`."""
    return os.path.join(REPO, *parts)
