"""Adding a shell profile must not turn the command guard off. 02.5.

`bridge/agent/profiles.py`'s docstring promises that supporting a new machine
means adding a file. That promise was false for the guard: `_default_shell`
returned a shell only when EXACTLY ONE profile carried `[shell]` tables, and
`PermissionDesk._shell` passes "" for every guest that reports no inventory
-- which `bridge/server.py` says is "every deployed one until it is
reflashed". So dropping a `[shell]` table into a second profile degraded
every legacy guest to "the shell on this machine is not one this bridge has
rules for" on every single command.

`test_no_os_in_code.py` walks `profiles.shells()` and asserts each carries
the tables; nothing asserted the guard still DECIDED anything once there were
two. That is this file.
"""

from __future__ import annotations

import pytest

from bridge.agent import cmdguard as G
from bridge.agent import profiles

#: A second shell, enough of one to carry `[shell]` tables. Deliberately not
#: a copy of command.com's: the point is that the guard must keep answering
#: for the FIRST shell when a stranger shows up.
OTHER = {"detect": {"kind": "shell"}, "shell": {
    "separators": r"&&|\|\||[;&|]",
    "redirect_write": r"(>>?)\s*(\S+)",
    "variables": r"[$~`]",
    "sep": "/", "quote": "\"'", "switch": "-", "switch_max": 12, "exts": [],
    "read": ["ls"], "write": ["rm"],
}}


@pytest.fixture
def second_shell(monkeypatch):
    monkeypatch.setitem(profiles.ALL, "zzsh", OTHER)
    monkeypatch.setattr(G, "_CACHE", {})
    yield


def test_exactly_one_profile_claims_the_fallback():
    """Two claims is a misconfiguration the guard refuses to resolve, so the
    shipped tree must contain exactly one."""
    claimed = [n for n in profiles.shells()
               if profiles.shell(n).get("assume_when_unreported")]
    assert claimed == ["command.com"], claimed


def test_a_silent_guest_still_gets_a_usable_grammar(second_shell):
    assert len(profiles.shells()) >= 2
    assert G.grammar_for("").usable


def test_a_silent_guest_still_gets_real_verdicts(second_shell):
    assert G.reason(r"del C:\WORK\x", guest_root=r"C:\WORK") is None
    why = G.reason(r"del C:\WINDOWS\WIN.INI", guest_root=r"C:\WORK")
    assert why is not None
    assert "not one this bridge has rules for" not in why


def test_a_named_shell_is_still_judged_by_its_own_rules(second_shell):
    assert G.reason("rm /work/x", guest_root="/work", shell="zzsh") is None
    assert G.reason("rm /etc/passwd", guest_root="/work",
                    shell="zzsh") is not None
