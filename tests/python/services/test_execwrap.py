r"""Recovering an exit code from a shell that loses it.

`command.com /c` exits 0 whatever happened, so a failed compile and a clean one
were indistinguishable from the bridge and the agent had to go and look -- a
live session verified every single build with a follow-up `dir`.

Everything asserted here was measured on real Windows 98 on 2026-10-01
(docs/measurements/exit-codes-via-batch-p200-20261002.txt). The three that
would each have broken it silently:

    without `call`, a nested .bat never returns and the verdict never runs
    WITH `call`, a builtin's redirection breaks: "File creation error"
    a builtin has no exit code at all -- `dir NOSUCHFILE` leaves errorlevel 0
"""
from __future__ import annotations

from bridge.services import execwrap as W

SHELL = "command.com"


def test_a_builtin_gets_no_wrapper():
    """Because wrapping one would report SUCCESS for a command that failed,
    which is strictly worse than saying nothing: `dir NOSUCHFILE.XYZ` prints
    "File not found" and leaves errorlevel at 0."""
    assert not W.applies(SHELL, "dir")
    assert not W.applies(SHELL, "dir C:\\WINDOWS")
    assert not W.applies(SHELL, "echo hello > f.txt")
    assert not W.applies(SHELL, "type f.txt")


def test_a_program_gets_one():
    assert W.applies(SHELL, "javac Hello.java")
    assert W.applies(SHELL, r"C:\Java\bin\javac -d . Hello.java")
    assert W.applies(SHELL, "build.bat")


def test_a_builtin_is_recognised_through_a_path_and_an_extension():
    """`C:\\WINDOWS\\COMMAND\\dir.exe` is not what anyone means, but a leading
    token is whatever the agent wrote and the match has to be on the verb."""
    assert not W.applies(SHELL, r"C:\WINDOWS\COMMAND\DIR.COM x")
    assert not W.applies(SHELL, '"dir"')


def test_an_empty_command_gets_nothing():
    assert not W.applies(SHELL, "")
    assert not W.applies(SHELL, "   ")


def test_a_shell_with_no_exit_code_table_is_left_alone():
    """The table is how a shell says it loses the code. A shell that does not
    say so keeps the code the client already reports."""
    assert not W.applies("4dos", "javac Hello.java")


def test_call_goes_on_a_batch_file_and_only_a_batch_file():
    """Both directions measured, and they need opposite treatment."""
    assert b"call build.bat" in W.body(SHELL, "build.bat")
    assert b"call" not in W.body(SHELL, "javac A.java").replace(
        b"@echo off", b"")


def test_the_wrapper_is_written_with_the_shells_own_endings():
    """The other half of the same day's measurement: an LF-only .bat is read
    as ONE command line on this shell and exits 0."""
    out = W.body(SHELL, "javac A.java")
    assert b"\r\n" in out
    assert b"\n" not in out.replace(b"\r\n", b"")


def test_the_command_is_in_the_file_byte_for_byte():
    """The user approved this text and the guard classified it. If the wrapper
    ever rewrote it, the permission dialog would be describing something other
    than what runs."""
    cmd = r'cl /nologo /Fe"my prog.exe" temp.cpp > out.txt'
    assert cmd.encode() in W.body(SHELL, cmd)


def test_both_verdict_lines_are_there():
    """`if errorlevel 1` and `if not errorlevel 1`. One alone cannot
    distinguish "succeeded" from "the line never ran"."""
    out = W.body(SHELL, "javac A.java")
    assert b"if errorlevel 1 echo " in out
    assert b"if not errorlevel 1 echo " in out


def test_the_path_joins_the_temp_directory_the_machine_declared():
    """`GetTempPathA` already carries its trailing separator; a client that
    somehow sent one without still has to get a valid path."""
    assert W.path(SHELL, "C:\\WINDOWS\\TEMP\\").startswith(
        "C:\\WINDOWS\\TEMP\\GEXEC")
    assert W.path(SHELL, "C:\\WINDOWS\\TEMP").startswith(
        "C:\\WINDOWS\\TEMP\\GEXEC")


def test_the_verdict_is_taken_off_the_output():
    assert W.split("temp.cpp\n" + W.SENTINEL + " ok\n") == ("temp.cpp", "ok")
    assert W.split("temp.cpp\n" + W.SENTINEL + " fail\n") == ("temp.cpp",
                                                              "fail")


def test_the_last_matching_line_wins():
    """The wrapper echoes after the command, so a program that printed the
    same text earlier keeps its own output."""
    text = f"a program printed {W.SENTINEL} ok\nreal output\n{W.SENTINEL} fail\n"
    out, verdict = W.split(text)
    assert verdict == "fail"
    assert f"a program printed {W.SENTINEL} ok" in out


def test_no_verdict_means_no_claim():
    """The line never arrived -- the command killed the shell, or the output
    cap bit before it. Saying nothing is the honest answer; guessing "ok" would
    report a crash as a clean run."""
    assert W.split("output with no verdict\n") == ("output with no verdict\n",
                                                   "")
    assert W.split(f"{W.SENTINEL} something-else\n")[1] == ""


def test_the_sentinel_cannot_be_rewritten_by_the_shell():
    """A sentinel containing a redirect, a pipe or a variable sigil would be
    interpreted rather than printed."""
    for ch in ">", "<", "|", "&", "%", '"':
        assert ch not in W.SENTINEL


def test_errorlevel_is_forced_before_the_command_runs():
    r"""THE CASE THE FIRST VERSION OF THIS GOT WRONG, on real hardware.

    `if errorlevel` reads whatever the last PROGRAM left behind, so a command
    that never ran leaves it alone -- and `build-nothing-here.exe` reported
    SUCCESS: the shell printed "Bad command or file name" and errorlevel stayed
    at 0. That is the one case the feature was built to close.

    Six cases are correct on the Athlon box since the pre-set went in
    (docs/measurements/exit-codes-via-batch-p200-20261002.txt), including a
    missing program and `javac` on a source file that is not there.
    """
    out = W.body(SHELL, "javac A.java")
    lines = out.decode().split("\r\n")
    # Before the command, not after: after it would overwrite the answer.
    assert lines[0] == "@echo off"
    probe = next(i for i, ln in enumerate(lines) if ln.startswith("echo x|"))
    assert "find" in lines[probe]
    assert lines.index("javac A.java") > probe


def test_the_preset_prints_nothing_into_the_users_output():
    r"""`attrib nosuchfile.xyz` also sets errorlevel and prints "File not
    found - nosuchfile.xyz", which would land in the output as though the
    command had said it."""
    assert "> nul" in W.body(SHELL, "javac A.java").decode()


def test_a_find_planted_in_the_cwd_is_never_run():
    """The shell searches the current directory before PATH, and a wrapped
    command runs with its cwd in the work folder, where the agent can write.
    A FIND.BAT there would run on every wrapped exec, under every mode, inside
    a command the user never saw. The probe is skipped instead, and every
    spelling the shell would accept for `find` is checked before it."""
    text = W.body(SHELL, "javac A.java").decode("ascii")
    lines = [ln.strip().lower() for ln in text.splitlines()]
    probe = next(i for i, ln in enumerate(lines) if ln.startswith("echo x|"))
    for ext in ("com", "exe", "bat"):
        guard = f"if exist find.{ext} goto run"
        assert guard in lines and lines.index(guard) < probe
    assert ":run" in lines and lines.index(":run") > probe
    assert lines.index(":run") < next(i for i, ln in enumerate(lines)
                                      if "javac" in ln)
