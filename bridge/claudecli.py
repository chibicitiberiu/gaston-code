"""Which Claude Code this bridge will run, settled before the socket opens.

THIS USED TO BE SOMEBODY ELSE'S PROBLEM. Every host artefact carried its
own CLI, because claude-agent-sdk's platform wheels bundle one as package
data, so there was nothing to find and nothing to get wrong. The release
now installs the SDK from its sdist, which carries no binary, and the CLI
is the user's -- which makes "where is it, and is it new enough" the most
likely failure in the whole product.

AND THE OLD FAILURE WAS NOT A BAD MESSAGE, IT WAS AN EXCEPTION. The agent
session is built at ATTACH, in Bridge._post_attach, with no `try` there or
at the caller. A Windows 98 user with no Claude Code did not get a
diagnosis; they got a traceback on this side and a dropped connection on
theirs. So the question is asked at startup, on the host, where somebody
can read the answer.

WHY THIS REFUSES WHERE THE SDK WARNS. Below its MINIMUM_CLAUDE_CODE_VERSION
the SDK logs and carries on, which is right for its caller: a developer at
a terminal who will see the warning. This bridge's user is at a machine
that can show them `[bad_secret] rejected` and strings of that shape and
nothing else. Limping here means an unexplained failure in another room,
so a 1.9.x that might have worked is the price, and it is the right one.

WHAT THIS DOES NOT CATCH, because no startup check can: `claude -v` exits 0
with no credentials at all. "Installed but not logged in" is at least as
likely as "not installed" once a guide tells people to install it, and it
lands at attach like before. The `try` at the attach site is what covers
that, and a CLI removed or self-updated while the bridge runs.
"""

from __future__ import annotations

import logging
import os
import platform
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("gaston.claudecli")

#: Mirrors MINIMUM_CLAUDE_CODE_VERSION, which the SDK keeps at
#: _internal/transport/subprocess_cli.py:37 -- three levels into `_internal`
#: in a 1,200-line transport module that importing drags in at startup.
#: Hardcoded on purpose; `__cli_version__` below is a two-line module and is
#: the one private read worth taking.
MINIMUM = (2, 0, 0)

#: Longer than the SDK's own `anyio.fail_after(2)` would be generous and
#: shorter would be a lie on a cold page cache. Measured on this box: 45 ms
#: for the native binary, ~800 ms for an npm install, which is a Node script.
PROBE_TIMEOUT_S = 2.0

_VERSION = re.compile(r"([0-9]+)\.([0-9]+)\.([0-9]+)")

#: Short lines, joined. Not one literal, because the prose ratchet
#: (tests/python/meta/test_prose_lives_in_toml.py) gives this file a budget
#: of zero strings over 120 bytes outside a raise or a log call, and it is
#: right to: long prose belongs where somebody maintaining it will look.
_INSTALL_POSIX = (
    "Install it with:",
    "    curl -fsSL https://claude.ai/install.sh | bash",
)
_INSTALL_WINDOWS = (
    "Install it in PowerShell with:",
    "    irm https://claude.ai/install.ps1 | iex",
)
#: NOT `npm install -g @anthropic-ai/claude-code`, which is what the SDK's
#: own message recommends. npm is what put a stale 1.0.33 on the author's
#: machine, where it sorts AHEAD of the working copy in the search below; on
#: Windows it produces the claude.cmd shim that the SDK then refuses to
#: spawn. Recommending the cause of two of this module's failure modes would
#: be an odd way to end its error message. The native installer writes
#: ~/.local/bin/claude, which is a path this search already looks in.
_ESCAPE = (
    "If it is installed somewhere this did not look, name it:",
    "    gaston-bridge --claude-cli /path/to/claude",
)


class ClaudeCliError(RuntimeError):
    """No usable Claude Code, and the message is the user experience.

    Named `...Error` rather than `...Missing` deliberately: the prose
    ratchet exempts arguments to a call whose name ends in Error, so the
    diagnosis can be built where it is raised instead of assembled into a
    local first.
    """


@dataclass(frozen=True)
class Resolved:
    """What was chosen, and how it was found."""

    path: str
    version: tuple[int, int, int]
    source: str

    @property
    def version_str(self) -> str:
        return "%d.%d.%d" % self.version


def _windows() -> bool:
    return platform.system() == "Windows"


def _native_exe(path: str) -> bool:
    """The SDK's `_is_windows_native_exe`, which travels with the Windows
    branch below. CreateProcess runs .exe and .com directly; npm's Windows
    install is a .cmd shim, and the SDK refuses to spawn one EVEN when
    handed it as an explicit cli_path. A resolver that accepted any
    `which("claude")` there would print a confident startup line and die at
    the first attach."""
    name = path.replace("\\", "/").rsplit("/", 1)[-1]
    return name.rstrip(". ").lower().endswith((".exe", ".com"))


def _bundled() -> str | None:
    """The SDK's own copy, if this is a platform-wheel install.

    Located by public attribute rather than by importing the private
    transport module. It is kept FIRST so the documented fat install -- for
    anyone who wants one file and no prerequisite -- keeps working unchanged.
    """
    try:
        import claude_agent_sdk
    except ImportError:              # pragma: no cover - the SDK is required
        return None
    name = "claude.exe" if _windows() else "claude"
    path = Path(claude_agent_sdk.__file__).parent / "_bundled" / name
    return str(path) if path.is_file() else None


def _known_locations() -> list[Path]:
    """The SDK's list, and the Windows branch is NOT an oversight.

    There it probes exactly one path and deliberately not the six POSIX
    ones: a rooted-but-driveless "/usr/local/bin/claude" resolves against
    the current drive, which is a place another local user can write -- a
    binary-planting surface.
    """
    home = Path.home()
    if _windows():
        return [home / ".local/bin/claude.exe"]
    return [
        home / ".npm-global/bin/claude",
        Path("/usr/local/bin/claude"),
        home / ".local/bin/claude",
        home / "node_modules/.bin/claude",
        home / ".yarn/bin/claude",
        home / ".claude/local/claude",
    ]


def candidates() -> list[tuple[str, str]]:
    """Every copy worth probing, in the SDK's order, as (source, path).

    Order matters and matching the SDK matters: if this module and the SDK
    disagree about which copy is in play, the version in the startup line is
    not the version that runs.
    """
    out: list[tuple[str, str]] = []
    bundled = _bundled()
    if bundled:
        out.append(("bundled with the SDK", bundled))

    hit = shutil.which("claude")
    if hit and (not _windows() or _native_exe(hit)):
        out.append(("on PATH", hit))
    elif hit:
        exe = shutil.which("claude.exe")
        if exe and _native_exe(exe):
            out.append(("on PATH", exe))

    for path in _known_locations():
        if path.is_file():
            out.append(("a known install location", str(path)))
    return _dedupe(out)


def _dedupe(found: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """One entry per real file, keeping the FIRST spelling of it.

    The symlink, not its target. A native install makes
    ~/.local/bin/claude a symlink into ~/.local/share/claude/versions/ and
    repoints it on each self-update -- the author's box has five versions
    there -- so pinning the resolved file would freeze a long-running
    bridge to a version the user has since replaced.
    realpath is only the identity used to notice that `which` and the
    location list found the same file twice, which here they do.
    """
    seen: set[str] = set()
    out: list[tuple[str, str]] = []
    for source, path in found:
        try:
            key = os.path.realpath(path)
        except OSError:
            key = path
        if key not in seen:
            seen.add(key)
            out.append((source, path))
    return out


def probe(path: str) -> tuple[int, int, int] | None:
    """`claude -v` parsed, or None for anything that is not a usable answer.

    OSError, NOT CalledProcessError: a directory and a non-executable file
    both raise PermissionError here, and the SDK's location list filters on
    `exists() and is_file()` with no X_OK check, so it hands back paths that
    cannot be run. stdin is closed because a CLI that decides to prompt
    would otherwise hang until the timeout.
    """
    try:
        done = subprocess.run([path, "-v"], capture_output=True, text=True,
                              stdin=subprocess.DEVNULL,
                              timeout=PROBE_TIMEOUT_S, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    match = _VERSION.match((done.stdout or "").strip())
    if match is None:
        return None
    # Tuples of ints, never strings: "2.1.310" < "2.1.49" lexically, and
    # that comparison would refuse a current CLI as too old.
    return (int(match.group(1)), int(match.group(2)), int(match.group(3)))


def sdk_cli_version() -> tuple[int, int, int] | None:
    """What the installed SDK was built against, for the warning threshold.

    The one private read in this module. `_cli_version` is a two-line
    module holding one constant, and a threshold that drifts in silence is
    worse than an import that breaks loudly -- but not worth refusing over,
    hence the catch.
    """
    try:
        from claude_agent_sdk import _cli_version
        match = _VERSION.match(_cli_version.__cli_version__)
    except Exception:                # pragma: no cover - private, may move
        return None
    if match is None:                # pragma: no cover
        return None
    return (int(match.group(1)), int(match.group(2)), int(match.group(3)))


def _listing(tried: list[tuple[str, str, tuple[int, int, int] | None]]) -> str:
    lines = []
    for source, path, version in tried:
        if version is None:
            why = "did not answer -v"
        else:
            why = "%d.%d.%d, below the %d.%d.%d floor" % (*version, *MINIMUM)
        lines.append("    %s  (%s) -- %s" % (path, source, why))
    return "\n".join(lines)


def _hint() -> str:
    install = _INSTALL_WINDOWS if _windows() else _INSTALL_POSIX
    return "\n".join((*install, "", *_ESCAPE))


def resolve(configured: str | None = None) -> Resolved:
    """Pick the Claude Code to run, or raise `ClaudeCliError` saying why not.

    PROBE LAZILY, and that is a measured decision rather than a style one.
    An exhaustive scan would be seven subprocesses, and an npm-installed
    1.0.33 answers `-v` in ~800 ms -- five and a half seconds of startup.
    Worse, that one is not read-only: it creates ~/.claude.json under a
    fresh HOME and rewrites the whole file against an existing one, so
    scanning everything would have the bridge rewrite the user's live
    Claude Code config on every start, racing any running modern CLI.

    So: stop at the first candidate that passes. One subprocess, ~45 ms.
    What each probe learned is kept, so the report on total failure costs no
    extra process -- and on that path latency is free anyway, because the
    bridge is about to refuse.
    """
    if configured:
        # An explicit setting that is wrong is ALWAYS an error and never a
        # fallback: silently searching past it would mean the bridge runs a
        # CLI the user did not name, which is the failure --claude-cli
        # exists to rule out.
        version = probe(configured)
        if version is None:
            raise ClaudeCliError(
                "--claude-cli %s did not answer `claude -v`. It has to be a "
                "Claude Code executable: not a directory, not a wrapper "
                "script, and readable by this user.\n\n%s"
                % (configured, _hint()))
        if version < MINIMUM:
            raise ClaudeCliError(
                "--claude-cli %s is Claude Code %d.%d.%d, and this bridge "
                "needs %d.%d.%d or newer.\n\n%s"
                % (configured, *version, *MINIMUM, _hint()))
        return _announce(Resolved(configured, version, "--claude-cli"))

    found = candidates()
    if not found:
        raise ClaudeCliError(
            "Claude Code is not installed, and this bridge runs it rather "
            "than carrying its own copy.\n\n%s" % _hint())

    tried: list[tuple[str, str, tuple[int, int, int] | None]] = []
    for source, path in found:
        version = probe(path)
        tried.append((source, path, version))
        if version is not None and version >= MINIMUM:
            return _announce(Resolved(path, version, source))

    # EVERY candidate, not just the first. This box is the reason: it
    # carries /usr/local/bin/claude at 1.0.33 from an npm install AND
    # ~/.local/bin/claude at 2.1.310, and /usr/local/bin comes first in the
    # SDK's order. Run from anywhere ~/.local/bin is not on PATH and a
    # faithful search picks the one under the floor -- so a bare refusal
    # would name a CLI the user forgot they had while a working one sat on
    # the same disk.
    raise ClaudeCliError(
        "No usable Claude Code. This bridge needs %d.%d.%d or newer, and "
        "found:\n\n%s\n\n%s" % (*MINIMUM, _listing(tried), _hint()))


def _announce(found: Resolved) -> Resolved:
    log.info("claude code %s at %s (%s)",
             found.version_str, found.path, found.source)
    sdk = sdk_cli_version()
    if sdk is not None and found.version < sdk:
        log.warning("claude code %s is older than the %d.%d.%d this SDK was "
                    "built against; update it if anything behaves oddly",
                    found.version_str, *sdk)
    return found
