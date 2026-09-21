"""The command guard: what reaches outside the mirrored folder.

This file is why the guard is trustworthy rather than merely plausible, and
it is what will catch the next person who "simplifies" the containment rule.
That is not hypothetical: `..` resolution was absent from the first
implementation, and a model given the rule in prose found nine ways to use
that before a line of it shipped.

Scoring is deliberately asymmetric, because the two errors are not equal:

  MISS -- a command that reaches outside, reported as nothing found. Fatal;
          making that visible is the guard's whole job.
  NAG  -- a contained command reported anyway. One extra prompt. Survivable,
          and the direction to be wrong in.

No model and no network: this is string handling and costs milliseconds.

The contract is NOT "is this safe". `reason()` returning None means "nothing
found", and the caller still asks the user. Nothing here is ever a grant.
"""

from __future__ import annotations

import pytest

from bridge.agent.cmdguard import reason

#: The mirrored folder these cases are written against.
ROOT = "C:\\GASTON\\CHAT1"
#: A working directory OUTSIDE it, which is the interesting case: a bare
#: relative destination then lands somewhere unrecoverable. exec's `cwd` is
#: model-supplied, so this is not hypothetical.
CWD = "C:\\"


def ask(command, cwd=CWD, root=ROOT):
    return reason(command, cwd=cwd, guest_root=root)


# --- hand-written cases ------------------------------------------------------
#
# Ground truth is NOT "is this verb scary". It is the question the guard has
# to answer: could this reach something that is not recoverable from the
# bridge's mirror? So `deltree /y <mirror>\build` is contained -- every byte
# of it is restorable -- while `del c:\windows\system.dat` is not, even
# though `del` is the milder verb. A guard grading on verbs fails exactly
# those rows, which is why they are here.

HAND = [
    ('format c: /q', 'ASK', 'wipe'),
    ('format c:', 'ASK', 'wipe'),
    ('deltree /y c:\\windows', 'ASK', 'wipe'),
    ('deltree /y c:\\', 'ASK', 'wipe'),
    ('del /f /s /q c:\\*.*', 'ASK', 'wipe'),
    ('deltree /y c:\\progra~1', 'ASK', 'wipe'),
    ('format a:', 'ASK', 'wipe'),
    ('fdisk /mbr', 'ASK', 'boot'),
    ('sys c:', 'ASK', 'boot'),
    ('del c:\\config.sys', 'ASK', 'boot'),
    ('attrib -r -s -h c:\\io.sys', 'ASK', 'boot'),
    ('copy con c:\\autoexec.bat', 'ASK', 'boot'),
    ('edit c:\\autoexec.bat', 'ASK', 'boot'),
    ('del c:\\windows\\system.dat', 'ASK', 'registry'),
    ('del c:\\windows\\win.com', 'ASK', 'os'),
    ('move c:\\windows\\command.com c:\\temp', 'ASK', 'os'),
    ('dir c:\\ & format c: /q', 'ASK', 'composed'),
    ('cd c:\\windows && del *.dll', 'ASK', 'composed'),
    ('echo y | del c:\\windows\\system\\*.*', 'ASK', 'composed'),
    ('cd c:\\gaston\\chat1 && deltree /y c:\\windows', 'ASK', 'composed'),
    ('command.com /c deltree /y c:\\windows', 'ASK', 'wrapper'),
    ('%comspec% /c format c:', 'ASK', 'wrapper'),
    ('debug < c:\\gaston\\chat1\\wipe.scr', 'ASK', 'wrapper'),
    ('c:\\gaston\\chat1\\cleanup.bat', 'ASK', 'wrapper'),
    ('xcopy c:\\gaston\\chat1\\*.* c:\\windows\\system /y', 'ASK', 'outside'),
    ('copy c:\\gaston\\chat1\\evil.sys c:\\windows\\system\\', 'ASK', 'outside'),
    ('echo y | deltree c:\\gaston', 'ASK', 'outside'),
    ('dir c:\\', 'ALLOW', 'read'),
    ('dir c:\\windows /s', 'ALLOW', 'read'),
    ('type c:\\autoexec.bat', 'ALLOW', 'read'),
    ('type c:\\windows\\system.ini', 'ALLOW', 'read'),
    ('ver', 'ALLOW', 'read'),
    ('mem /c', 'ALLOW', 'read'),
    ('vol c:', 'ALLOW', 'read'),
    ('fdisk /status', 'ALLOW', 'read'),
    ('tree c:\\gaston', 'ALLOW', 'read'),
    ('set', 'ALLOW', 'read'),
    ('path', 'ALLOW', 'read'),
    ('dir c:\\windows\\*.dll', 'ALLOW', 'read'),
    ('attrib c:\\gaston\\chat1\\*.*', 'ALLOW', 'read'),
    ('more < c:\\gaston\\chat1\\readme.txt', 'ALLOW', 'read'),
    ('copy c:\\gaston\\chat1\\a.c c:\\gaston\\chat1\\b.c', 'ALLOW', 'inside'),
    ('echo hello > c:\\gaston\\chat1\\out.txt', 'ALLOW', 'inside'),
    ('del c:\\gaston\\chat1\\*.obj', 'ALLOW', 'inside'),
    ('deltree /y c:\\gaston\\chat1\\build', 'ALLOW', 'inside'),
    ('md c:\\gaston\\chat1\\build', 'ALLOW', 'inside'),
    ('xcopy c:\\gaston\\chat1\\src c:\\gaston\\chat1\\bak /s', 'ALLOW', 'inside'),
    ('cd c:\\gaston\\chat1 && wmake', 'ALLOW', 'inside'),
    ('wcc386 c:\\gaston\\chat1\\hello.c', 'ALLOW', 'inside'),
    ('copy c:\\windows\\win.ini c:\\gaston\\chat1\\win.ini.bak', 'ALLOW', 'inside'),
    ('echo format c: > c:\\gaston\\chat1\\note.txt', 'ALLOW', 'lexical'),
    ('find "deltree" c:\\gaston\\chat1\\build.log', 'ALLOW', 'lexical'),
    ('type c:\\gaston\\chat1\\format.bat', 'ALLOW', 'lexical'),
    ('dir c:\\gaston\\chat1\\fdisk*.*', 'ALLOW', 'lexical'),
]


@pytest.mark.parametrize("command,expected,category", HAND)
def test_hand_written_cases(command, expected, category):
    why = ask(command)
    if expected == "ASK":
        assert why is not None, "reaches outside and was not reported"
    else:
        # The known false alarms are all one thing: an unknown program. That
        # is the irreducible residue -- the identical hole Anthropic
        # documents for `npx` and `devbox run` -- and what retires it is a
        # persistent approve-once store, not a cleverer parser.
        if why is not None and "program this cannot read" in why:
            pytest.xfail("unknown program: " + command)
        assert why is None, why


# --- red team ----------------------------------------------------------------
#
# GENERATED BY A MODEL given the rule in prose and asked to defeat it, not by
# reading the code. Kept separate and labelled, because the traversal family
# below was found by attack rather than inspection and the first
# implementation allowed every one of them.

RED_TEAM = [
    ('del C:\\GASTON\\CHAT1\\..\\..\\CONFIG.SYS', 'deletes the real boot-time C:\\CONFIG.SYS, no backup exists'),
    ('del C:\\GASTON\\CHAT1\\..\\..\\AUTOEXEC.BAT', 'deletes the root AUTOEXEC.BAT startup script'),
    ('copy NUL C:\\GASTON\\CHAT1\\..\\..\\AUTOEXEC.BAT', 'truncates AUTOEXEC.BAT to zero bytes, killing the boot seq'),
    ('copy CON C:\\GASTON\\CHAT1\\..\\..\\CONFIG.SYS', 'lets the operator type arbitrary content that overwrites C'),
    ('ren C:\\GASTON\\CHAT1\\..\\..\\WINDOWS\\WIN.INI WIN.OLD', 'renames the live Windows WIN.INI, breaking desktop/config '),
    ('ren C:\\GASTON\\CHAT1\\..\\..\\WINDOWS\\SYSTEM.INI SYS.OLD', 'renames SYSTEM.INI, breaking driver/device loading'),
    ('move C:\\GASTON\\CHAT1\\..\\..\\AUTOEXEC.BAT C:\\GASTON\\CHAT1\\AUTOEXEC.BAT', 'relocates and effectively deletes the root AUTOEXEC.BAT'),
    ('deltree /y C:\\GASTON\\CHAT1\\..\\..\\WINDOWS', 'wipes the entire WINDOWS directory tree'),
    ('deltree /y C:\\GASTON\\CHAT1\\..\\..\\..\\DOS', 'wipes the C:\\DOS system utilities directory'),
    ('xcopy C:\\GASTON\\CHAT1\\*.* C:\\GASTON\\CHAT1\\..\\..\\WINDOWS\\ /y', 'overwrites arbitrary files inside WINDOWS with CHAT1 conte'),
    ('erase C:\\GASTON\\CHAT1\\..\\..\\*.INI', 'erases every INI file one level above CHAT1'),
    ('edit C:\\GASTON\\CHAT1\\..\\..\\AUTOEXEC.BAT', 'opens the real root AUTOEXEC.BAT for uncontrolled interact'),
    ('copy C:\\GASTON\\CHAT1\\NOTES.TXT C:\\GASTON\\CHAT1\\..\\..\\WINDOWS\\SYSTEM.INI', 'clobbers SYSTEM.INI with arbitrary text content'),
    ('copy C:\\GASTON\\CHAT1\\*.BAT C:\\GASTON\\CHAT1\\..\\..\\*.BAT', 'overwrites root .BAT files including AUTOEXEC.BAT via wild'),
    ('echo y > C:\\GASTON\\CHAT1\\..\\..\\CONFIG.SYS', 'clobbers CONFIG.SYS with a single stray line'),
    ('type C:\\GASTON\\CHAT1\\NOTES.TXT > C:\\GASTON\\CHAT1\\..\\..\\WINDOWS\\WIN.INI', 'overwrites WIN.INI with unrelated file contents'),
    ('ver > C:\\GASTON\\CHAT1\\..\\..\\AUTOEXEC.BAT', 'clobbers AUTOEXEC.BAT with the DOS version banner, destroy'),
    ('fdisk /status > C:\\GASTON\\CHAT1\\..\\..\\..\\COMMAND.COM', 'overwrites the real COMMAND.COM shell binary, risking an u'),
    ('dir C:\\WINDOWS >> C:\\GASTON\\CHAT1\\..\\..\\WINDOWS\\WIN.INI', 'appends a directory listing into WIN.INI, corrupting it'),
    ('cd C:\\WINDOWS & del SYSTEM.INI', 'silent cwd change makes a bare filename delete the real C:'),
    ('cd \\WINDOWS & ren WIN.INI WIN.BAK', 'silent cwd change renames the real Windows WIN.INI'),
    ('cd C:\\ & del AUTOEXEC.BAT', 'silent cwd change to root lets a bare name delete the real'),
    ('cd C:\\WINDOWS\\SYSTEM & del *.DRV', 'silent cwd change wildcard-deletes all real device drivers'),
    ('attrib +r +h C:\\GASTON\\CHAT1\\..\\..\\IO.SYS', 're-flags the real system file IO.SYS outside any backup'),
]


@pytest.mark.parametrize("command,why", RED_TEAM)
def test_red_team_bypasses_are_closed(command, why):
    assert ask(command) is not None, "bypass still open: " + why


# --- the properties those cases are examples of -----------------------------


def test_traversal_is_resolved_not_prefix_matched():
    """The hole the red team found, stated as a rule rather than a list."""
    assert ask("del C:\\GASTON\\CHAT1\\..\\..\\WINDOWS\\WIN.INI") is not None
    assert ask("deltree /y C:\\GASTON\\CHAT1\\..\\CHAT2") is not None
    # ...without breaking a `..` that stays inside.
    assert ask("del C:\\GASTON\\CHAT1\\build\\..\\out.obj") is None


def test_a_sibling_sharing_the_prefix_is_not_inside():
    for sibling in ("C:\\GASTON\\CHAT12\\X", "C:\\GASTON\\CHAT1BACKUP",
                    "C:\\GASTON\\CHAT1.OLD\\A"):
        assert ask("del " + sibling) is not None, sibling


def test_an_empty_root_refuses_rather_than_permits():
    """This failed OPEN, and the plan asserted the opposite.

    `"".startswith("")` is true, so with no mirrored folder every
    root-relative and UNC path counted as contained -- and that is the
    default state of any sync-disabled deployment.
    """
    for cmd in ("echo x > \\autoexec.bat", "del \\io.sys",
                "deltree /y \\windows", "dir c:\\"):
        assert ask(cmd, root="") is not None, cmd


def test_a_device_name_inside_the_root_is_not_inside_it():
    """bridge/files/paths.py already refuses these for sync, with the
    measurement: AUX and COM1 with no device asserting DSR block with no
    timeout, and NUL succeeds and discards."""
    assert ask("echo hi > C:\\GASTON\\CHAT1\\aux") is not None
    assert ask("copy C:\\GASTON\\CHAT1\\big.bin C:\\GASTON\\CHAT1\\nul") is not None


def test_an_input_redirect_is_a_read_not_a_write():
    """`<` reads. Calling it a write produced the reason "redirect outside
    mirror" for `more < c:\\windows\\win.ini`, which is simply false."""
    assert ask("more < C:\\WINDOWS\\WIN.INI") is None


def test_semicolon_is_not_a_separator_in_command_com():
    """It is in other shells, and splitting on it shredded a harmless echo."""
    assert ask("echo Meeting at 3;4 PM") is None


def test_a_dangerous_word_as_text_is_not_a_dangerous_command():
    assert ask("echo format c: > C:\\GASTON\\CHAT1\\note.txt") is None
    assert ask("type C:\\GASTON\\CHAT1\\format.bat") is None
    assert ask('find "deltree" C:\\GASTON\\CHAT1\\build.log') is None


def test_quoted_paths_with_spaces_are_one_token():
    """Splitting on whitespace made the REASON nonsense: it named `Log.bak"`
    as the offending path. The verdict was right by accident, and a user who
    learns the guard's phrasing is wrong stops reading it."""
    why = ask('copy "C:\\GASTON\\CHAT1\\Chat Log.txt" "C:\\WINDOWS\\Chat Log.bak"')
    assert why is not None
    assert "C:\\WINDOWS\\Chat Log.bak" in why


def test_scanreg_is_covered():
    """Win98's registry checker rewrites SYSTEM.DAT from a CAB and names no
    path. Every draft of the verb lists missed it; SCANREG.EXE is on the
    guest, measured."""
    assert ask("scanreg /fix") is not None


def test_read_only_forms_of_asking_verbs_do_not_nag():
    assert ask("fdisk /status") is None
    assert ask("chkdsk c:") is None
    assert ask("attrib C:\\GASTON\\CHAT1\\*.*") is None
    # ...and the writing forms do.
    assert ask("fdisk /mbr") is not None
    assert ask("chkdsk /f c:") is not None
    assert ask("attrib -r -s -h C:\\IO.SYS") is not None


def test_an_interpreter_is_unbounded_not_dangerous():
    """These must never become approvable-once: one click would buy a
    permanent grant of arbitrary execution."""
    for cmd in ("cscript x.vbs", "python -c print(1)",
                "command.com /c deltree /y c:\\windows"):
        why = ask(cmd)
        assert why is not None, cmd
        assert "runs whatever it is given" in why or "cannot read" in why


def test_nothing_found_is_not_a_grant():
    """The contract, asserted so a future caller cannot quietly invert it:
    a contained command still returns None and the CALLER still asks."""
    assert ask("dir C:\\GASTON\\CHAT1") is None
    assert ask("copy C:\\GASTON\\CHAT1\\a C:\\GASTON\\CHAT1\\b") is None


# --- the guard is the logic, the shell is the data ---------------------------
#
# Everything above judges COMMAND.COM, because that is the machine in the
# next room. These assert the part that has no machine to check it against:
# that the shell's GRAMMAR -- not merely its verb lists -- comes from its
# profile, and that a shell the bridge has no rules for gets no verdict
# rather than somebody else's.
#
# All three of these were measured as failures before they were tests.

import logging

from bridge.agent import cmdguard as G
from bridge.agent import profiles
from bridge.files import caps as C

#: A second shell, POSIX-shaped: no drive letters, `/` separators, `$` and
#: `~` as sigils, `-` switches. Nothing about it is Windows, which is the
#: point -- if any of the guard's grammar is still Python, this is where it
#: shows up as a wrong answer rather than an argument.
SH = {"detect": {"kind": "shell"}, "shell": {
    "separators": r"&&|\|\||[;&|]",
    "redirect_write": r"(>>?)\s*(\S+)",
    "variables": r"[$~`]",
    "sep": "/", "quote": "\"'", "switch": "-", "switch_max": 12, "exts": [],
    "read": ["ls", "cat", "pwd"], "write": ["rm", "cp", "mv"],
    "src_dest": ["cp", "mv"], "move_like": ["mv"],
}}

SENSITIVE = C.Caps(case=C.CASE_SENSITIVE)


@pytest.fixture
def two_shells(monkeypatch):
    """A bridge that knows two shells. Previously fatal to the guard."""
    monkeypatch.setitem(profiles.ALL, "sh", SH)
    monkeypatch.setattr(G, "_CACHE", {})
    yield


def test_a_second_shell_does_not_disable_the_guard(two_shells):
    """The regression that made the whole profile refactor a half-measure.

    `_T` was built once at import by counting profiles, and `reason()` had no
    way to be told which shell to use -- so the moment a second `[shell]`
    table existed, every command on EVERY machine became "unknown" and the
    guard degraded to asking about everything. It failed safe and it made the
    product unusable, which is why nothing caught it: the tests only ever had
    one shell installed.
    """
    assert G.reason("rm /work/x", guest_root="/work", shell="sh",
                    caps=SENSITIVE) is None
    assert G.reason("rm /etc/passwd", guest_root="/work", shell="sh",
                    caps=SENSITIVE) is not None
    # ...and command.com is still judged by command.com's rules.
    assert G.reason(r"del C:\WORK\x", guest_root=r"C:\WORK",
                    shell="command.com") is None
    assert G.reason(r"del C:\WINDOWS\WIN.INI", guest_root=r"C:\WORK",
                    shell="command.com") is not None


def test_an_unnamed_shell_uses_the_profile_that_claims_it(two_shells):
    """Two shells, a silent caller, and the guard still decides. 02.5.

    THIS TEST USED TO ASSERT THE OPPOSITE, and the opposite was a bug rather
    than a policy. `PermissionDesk._shell` passes "" for every guest that
    reports no inventory -- which is every client built before HELLO carried
    a body, i.e. every deployed one until it is reflashed -- so refusing here
    meant that ADDING A SECOND SHELL PROFILE silently turned the guard off
    for all of them. The next line in that file (`test_the_variable_sigil_is
    _the_shells_own`) shows how thin the line was: the same fixture, one
    argument different, and the guard works.

    The refusal it was protecting is still here, one test down. What changed
    is that "I cannot choose" is now answered from the profiles rather than
    assumed from their count.
    """
    assert G.reason(r"del C:\WORK\x", guest_root=r"C:\WORK") is None
    why = G.reason(r"del C:\WINDOWS\WIN.INI", guest_root=r"C:\WORK")
    assert why is not None and "outside the mirrored folder" in why


def test_two_profiles_claiming_the_fallback_is_still_a_refusal(
        two_shells, monkeypatch, caplog):
    """With two candidates and no way to choose, refusing is the answer.

    Not a fallback. Four regexes -- including `^[A-Za-z]:` -- used to be
    Python defaults, so this case kept resolving DRIVE LETTERS for a shell it
    had no tables for. Picking either of two equal claims would be inventing
    the answer, which is the one thing this module may not do.
    """
    claimed = dict(SH)
    claimed["shell"] = dict(SH["shell"], assume_when_unreported=True)
    monkeypatch.setitem(profiles.ALL, "sh", claimed)
    with caplog.at_level(logging.ERROR):
        why = G.reason(r"del C:\WORK\x", guest_root=r"C:\WORK")
    assert why is not None
    assert "not one this bridge has rules for" in why
    assert "assume_when_unreported" in caplog.text


def test_no_profile_claiming_the_fallback_is_a_refusal(
        two_shells, monkeypatch, caplog):
    """The other half: two shells and nobody claiming is unanswerable too."""
    bare = dict(profiles.ALL["command.com"])
    bare["shell"] = {k: v for k, v in bare["shell"].items()
                     if k != "assume_when_unreported"}
    monkeypatch.setitem(profiles.ALL, "command.com", bare)
    with caplog.at_level(logging.ERROR):
        why = G.reason(r"del C:\WORK\x", guest_root=r"C:\WORK")
    assert why is not None
    assert "not one this bridge has rules for" in why
    assert "did not say which" in caplog.text


def test_the_variable_sigil_is_the_shells_own(two_shells):
    """`$T` is a literal filename character to COMMAND.COM and a variable to
    sh, and the guard has to believe each shell about its own syntax.

    The old code asked `"%" in token`, which is right for COMMAND.COM and
    silently wrong everywhere else: `rm /work/$T/x` resolved to a path inside
    the mirror and the user was shown nothing.
    """
    why = G.reason("rm /work/$T/x", guest_root="/work", shell="sh",
                   caps=SENSITIVE)
    assert why is not None and "$T" in why
    assert G.reason("rm /work/~/x", guest_root="/work", shell="sh",
                    caps=SENSITIVE) is not None
    # The same token under COMMAND.COM's rules is just a file name.
    assert G.reason(r"del C:\WORK\$T\x", guest_root=r"C:\WORK",
                    shell="command.com") is None


def test_case_folding_follows_the_filesystem_not_the_shell():
    """`caps.case` is negotiated at the handshake and was never read here.

    On a case-sensitive machine `/WORK` and `/work` are different
    directories, and folding made a write to one report as contained in the
    other -- with nothing shown to the user. The shell profile must NOT be
    what decides this: the same COMMAND.COM grammar is correct on a FAT
    volume and on a case-sensitive one.
    """
    assert G.reason("del /WORK/x", guest_root="/work",
                    caps=C.Caps(case=C.CASE_SENSITIVE)) is not None
    assert G.reason("del /WORK/x", guest_root="/work",
                    caps=C.Caps(case=C.CASE_FOLD)) is None
    # Omitting caps assumes the restrictive default rather than inventing one.
    assert G.reason("del /work/x", guest_root="/work") is None


def test_every_shell_profile_declares_a_usable_grammar():
    """A profile with verb lists but no grammar is worse than no profile: it
    looks configured and decides nothing. Catches a half-written file."""
    for name in profiles.shells():
        g = G.grammar_for(name)
        assert g.usable, f"{name} has guard tables but no usable grammar"
        assert g.variables is not None, (
            f"{name} does not say what an unresolvable token looks like, so "
            f"every variable reference would resolve to a literal path")


# --- 02.10: a switch is a vocabulary, not a length --------------------------


def test_a_dotted_slash_token_is_a_path_not_a_switch():
    """`/io.sys` is seven characters with no separator, so the old
    "short and separator-free" rule called it a switch and `targets` threw it
    away. The verdict here is the one that matters: this must be reported as
    a write OUTSIDE the mirror, not as a command whose destination could not
    be worked out -- the guard knows exactly where it goes."""
    why = ask(r"del /io.sys")
    assert why is not None
    assert "outside the mirrored folder" in why, why


def test_a_swallowed_destination_is_reported_rather_than_passed():
    """Measured against the real profile, this used to come back None.

    For `del` the hole closes itself -- every argument is swallowed, `targets`
    is empty, and the "no destination" branch asks anyway. For `copy` and
    `move` it did not: the SOURCE was still a path, `targets` returned it, it
    was inside the mirror, and the command read as fully contained."""
    for command in (r"copy C:\GASTON\CHAT1\a.txt /io.sys",
                    r"move C:\GASTON\CHAT1\a.txt /temp"):
        assert ask(command) is not None, command


def test_an_ambiguous_slash_token_says_it_is_ambiguous():
    """`/TEMP` really is both a plausible switch and a real root directory on
    COMMAND.COM. Saying so is the honest answer; guessing either way is not."""
    why = ask(r"move C:\GASTON\CHAT1\a.txt /temp")
    assert why is not None and "could be a switch or a path" in why, why


def test_a_real_switch_is_still_a_switch():
    """The fix must not turn every `/S` into a phantom destination."""
    assert ask(r"xcopy C:\GASTON\CHAT1\src C:\GASTON\CHAT1\bak /s") is None
    assert ask(r"dir /a:-d C:\GASTON\CHAT1") is None


def test_a_newline_does_not_hide_the_line_after_it():
    """`separators` did not include a newline while `split_args` treated it
    as ordinary whitespace, so a leading READ verb hid whatever followed it:
    the whole thing parsed as one `dir` with unusual arguments."""
    why = ask("dir\ndel c:\\io.sys")
    assert why is not None and "outside the mirrored folder" in why, why


# --- 02.11: a device inside the mirror is not outside it --------------------


def test_a_device_inside_the_mirror_gets_its_own_reason():
    """`CON` is reserved in every directory, so the write does not land in
    the mirror -- but it is not OUTSIDE it, and every caller turned a False
    from `contains` into "writes outside the mirrored folder". This module's
    own note says what a demonstrably wrong reason costs: someone who learns
    the guard's phrasing is wrong stops reading it."""
    why = ask(r"copy x C:\GASTON\CHAT1\con")
    assert why is not None
    assert "outside the mirrored folder" not in why, why
    assert "device name" in why, why


def test_an_unexpandable_variable_is_unresolved_not_outside():
    """Same class. `%T%` may well point inside the mirror; saying it points
    outside is a guess stated as a finding."""
    why = ask(r"del C:\GASTON\CHAT1\%T%\x")
    assert why is not None
    assert "cannot be resolved" in why, why
