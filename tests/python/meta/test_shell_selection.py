"""Which shell a command runs under, and who decides. 02.5.

The machine has as many shells as it has -- `command.com` and `4dos` here,
`sh`/`bash`/`zsh`/`csh` on the Unix box `CLAUDE.md`'s first standing rule says
this surface has to survive being pointed at. Three parties could have picked
between them and only one of them should:

  the guest    decided, from `GetVersion()`, and told nobody. So a second
               installed shell was unreachable, and the bridge's command
               guard was judging commands against a grammar the guest might
               not have been running.
  the bridge   guessed, by counting the profiles it happened to ship. Adding
               a `[shell]` table to a second profile -- the exact change
               `profiles.py` says supporting a new machine should be --
               turned the guard off for every guest that reported no
               inventory.
  the model    picks, per call, from the list the machine reported and the
               system prompt printed.

The third is what is implemented. This file is the check that the first two
stay out of it.
"""

from __future__ import annotations

import pytest

from bridge.agent import cmdguard as G
from bridge.agent import machine, profiles, toolrules
from bridge.agent.toolrules import Found

#: What a two-shell DOS box reports. `4dos` is a real profile in the tree and
#: deliberately carries no `[shell]` tables today, so it stands in for "the
#: machine has this and the bridge cannot drive it".
COMMAND_COM = Found("command.com", "shell", r"C:\WINDOWS\COMMAND.COM", True)
FOURDOS = Found("4dos", "shell", r"D:\4DOS\4DOS.COM", False)
WATCOM = Found("watcom", "compiler", r"C:\WATCOM", True)

#: A second profile WITH guard tables and a run switch, which is what a
#: properly-supported second shell looks like.
OTHER = {"detect": {"kind": "shell"}, "shell": {
    "separators": r"&&|\|\||[;&|]",
    "redirect_write": r"(>>?)\s*(\S+)",
    "variables": r"[$~`]",
    "sep": "/", "quote": "\"'", "switch": "-", "switch_max": 12, "exts": [],
    "read": ["ls"], "write": ["rm"],
    "run_switch": "-c",
}}


@pytest.fixture
def second_shell(monkeypatch):
    monkeypatch.setitem(profiles.ALL, "zzsh", OTHER)
    monkeypatch.setattr(G, "_CACHE", {})
    yield


# --- the machine reports a LIST --------------------------------------------


def test_every_reported_shell_is_listed_not_just_the_first():
    """`shell_of` answers "the first", which is right for the questions that
    are properties of the machine (its PATH separator, its console codepage)
    and wrong for this one. A list is the fact."""
    found = [COMMAND_COM, FOURDOS, WATCOM]
    assert [f.name for f in toolrules.shells_of(found)] == ["command.com",
                                                            "4dos"]
    assert toolrules.shell_of(found) == "command.com"


def test_a_shell_with_no_declared_run_switch_cannot_be_selected():
    """The machine has 4DOS and no profile says how to hand it one command
    line, so composing something would be a guess -- and the guess runs."""
    found = [COMMAND_COM, FOURDOS]
    assert toolrules.shell_command(found, "4dos") is None
    assert [f.name for f in toolrules.usable_shells(found)] == ["command.com"]


def test_the_invocation_is_the_found_path_and_the_declared_switch():
    """Both halves are data: the path is what detection saw, so a 4DOS at
    D:\\4DOS is invoked from there, and the switch is that shell's own
    profile saying how to give it a command."""
    assert (toolrules.shell_command([COMMAND_COM], "command.com")
            == r"C:\WINDOWS\COMMAND.COM /c")


def test_a_shell_the_machine_did_not_report_is_not_invocable():
    assert toolrules.shell_command([COMMAND_COM], "bash") is None


def test_the_prompt_names_the_shells_the_model_may_pick():
    """The model cannot pick from a list it was never shown, and listing one
    it cannot select would be a lie in the paragraph it reads to decide."""
    block = machine.tools_block([COMMAND_COM, FOURDOS, WATCOM])
    assert "Shells on that machine: `command.com`." in block
    assert "`4dos`" not in block.split("Shells on that machine:")[1]
    assert "`shell` argument" in block


def test_the_prompt_says_nothing_about_shells_when_none_were_reported():
    block = machine.tools_block([WATCOM])
    assert "Shells on that machine" not in block


# --- adding a profile must not disable the guard ---------------------------


def test_a_second_shell_profile_does_not_disable_the_guard(second_shell):
    """The regression 02.5 is about, checked where it is now fixed: the
    machine said which shell, so the count of installed profiles never comes
    into it."""
    assert len(profiles.shells()) >= 2
    assert G.reason(r"del C:\WORK\x", guest_root=r"C:\WORK",
                    shell="command.com") is None
    why = G.reason(r"del C:\WINDOWS\WIN.INI", guest_root=r"C:\WORK",
                   shell="command.com")
    assert why is not None
    assert "not one this bridge has rules for" not in why


def test_each_shell_is_judged_by_its_own_grammar(second_shell):
    """The point of naming it. `$T` is a filename character to COMMAND.COM
    and a variable to a Unix shell, and the guard has to believe each shell
    about its own syntax -- so judging a zzsh command by command.com's rules
    is not a near miss, it is a different answer."""
    from bridge.files import caps as C
    sensitive = C.Caps(case=C.CASE_SENSITIVE)
    assert G.reason("rm /work/x", guest_root="/work", shell="zzsh",
                    caps=sensitive) is None
    assert G.reason("rm /etc/passwd", guest_root="/work", shell="zzsh",
                    caps=sensitive) is not None
    assert G.reason(r"del C:\WORK\$T\x", guest_root=r"C:\WORK",
                    shell="command.com") is None


# --- the last resort -------------------------------------------------------


def test_one_installed_profile_is_still_used_when_nothing_named_a_shell():
    """A machine that reported no shells and a call that named none. One
    candidate is the only candidate there is."""
    assert G._default_shell() == "command.com"
    assert G.grammar_for("").usable


def test_several_profiles_and_no_word_from_anyone_is_a_refusal(
        second_shell, caplog):
    """Not a fallback. Inventing an answer is the one thing this module may
    not do, and four regexes used to be Python defaults -- including
    `^[A-Za-z]:`, a drive letter -- so this case kept resolving DRIVE LETTERS
    for a shell it had no tables for."""
    import logging
    with caplog.at_level(logging.ERROR):
        why = G.reason(r"del C:\WORK\x", guest_root=r"C:\WORK")
    assert why is not None
    assert "not one this bridge has rules for" in why
    assert "the machine reported none" in caplog.text


def test_no_profile_ships_a_static_assumption():
    """An earlier attempt at 02.5 let a profile declare itself the shell to
    assume when the machine said nothing. That is a guess wearing a config
    key: it wins on a machine the profile does not describe, and it cannot
    be checked against anything. The fix is that nobody has to assume."""
    for name in profiles.shells():
        table = profiles.shell(name)
        assert "assume_when_unreported" not in table, name
        assert "os_match" not in table, name
