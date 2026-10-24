r"""Which categories a call lands in -- the table the execution modes stand on.

Each row is a case the plan's reviews raised (docs/PERMISSIONS.md): the red
team's bypasses, the workflow simulation's needless prompts, and the reads
that must stay reads. A row that moves is a behaviour change somebody has to
look at, which is the point of writing them down.

The lists are the CLIENT'S (CLAUDE.md: the client declares OS facts), so the
fixture supplies them the way a client would; nothing here asserts that the
bridge knows what `C:\WINDOWS` is.
"""
from __future__ import annotations

import pytest

from bridge.agent import categories as K
from bridge.files import caps as C
from bridge.files import paths

ROOT = "C:\\WORK\\HELLO"
PROTECT = ("C:\\WINDOWS\\**", "C:\\*.SYS", "C:\\AUTOEXEC.BAT",
           "C:\\GASTON\\GASTON.INI")
TRUST = frozenset({"wcl386", "wmake", "notepad"})


def ctx(**kw) -> K.Ctx:
    base = dict(caps=C.CONSTRAINED, shells=("command.com",), work_root=ROOT,
                lists=K.Lists(protect=PROTECT, trust=TRUST),
                resolve=lambda p: paths.on_guest(p, ROOT))
    base.update(kw)
    return K.Ctx(**base)


def run(tool: str, args: dict, c: K.Ctx | None = None, *,
        listing=(), files=None) -> K.Verdict:
    """Classify, answering `wants` the way the desk would, from a fake
    guest: `listing` is the cwd's names, `files` upper-cased path -> bytes."""
    c = c or ctx()
    name = tool if not tool.islower() or tool.startswith("mcp") else \
        "mcp__machine__" + tool
    for _ in range(5):
        v = K.classify(c, name, args)
        if not v.wants:
            return v
        got = {}
        for kind, p, _key in v.wants:
            got[(kind, p)] = (frozenset(listing) if kind == "ls"
                              else (files or {}).get(p.upper()))
        K.fill(c, v.wants, got)
    raise AssertionError("classify kept asking")


def cats(v: K.Verdict) -> set:
    return {h.cat + ("?" if h.doubt else "") for h in v.hits}


@pytest.mark.parametrize("command,want", [
    # Trusted tools in the work folder, and what they name.
    ("wcl386 hello.c", {"run_work"}),
    ("wcl386 debug.c", {"run_work"}),                # an argument, not debug
    ("wcl386 x.c -fe=C:\\WINDOWS\\COMMAND.COM", {"run_work", "run_sys"}),
    ("wcl386 x.c -fe=D:\\OUT\\X.EXE", {"run_work", "run_else"}),
    # Dangerous programs, wherever they appear as a command.
    ("format c:", {"run_sys"}),
    ("command /c format c:", {"run_sys"}),
    ("lh format c:", {"run_sys"}),
    ("fdisk /STATUS", {"read"}),
    ("chkdsk", {"read"}),
    ("chkdsk /F", {"run_sys"}),
    # Reads stay reads, wherever they point.
    ("type C:\\AUTOEXEC.BAT", {"read"}),
    ("dir C:\\WINDOWS", {"read"}),
    # Writes, by where they land.
    ("copy a.c b.c", {"files_work"}),
    ("copy a.c C:\\WINDOWS\\SYSTEM", {"files_sys"}),
    ("copy a.c D:\\BACKUP", {"files_else"}),
    ("dir > C:\\AUTOEXEC.BAT", {"read", "files_sys"}),
    ('dir > "C:\\AUTOEXEC.BAT"', {"read", "files_sys"}),
    ("dir > NUL", {"read"}),
    ("copy a.c LPT1", {"files_else"}),
    ("del %WINDIR%\\WIN.INI", {"files_else?"}),
    ("del ...\\x.txt", {"files_else?"}),
    # Deletes through the shell are permanent.
    ("del a.obj", {"files_work"}),
    ("del *.*", {"files_work", "purge"}),
    ("deltree /y sub", {"files_work", "purge"}),
    ("deltree /y C:\\WORK\\HELLO", {"files_else", "purge"}),
    # A machine setting, not a file.
    ("date 01-01-98", {"run_else"}),
    ("date", {"read"}),
    # Unknown programs and interpreters.
    ("qbasic /run x.bas", {"run_else"}),
    ("someprog.exe", {"run_else"}),
])
def test_exec_in_the_work_folder(command, want):
    assert cats(run("exec", {"command": command})) == want


def test_a_planted_program_shadows_a_trusted_name_and_is_judged_by_its_lines():
    """COMMAND.COM looks in the current directory before PATH. A WCL386.BAT
    the agent wrote is what `wcl386` runs, so it is read, not trusted."""
    v = run("exec", {"command": "wcl386 hello.c"}, listing=["WCL386.BAT"],
            files={ROOT + "\\WCL386.BAT": b"deltree /y c:\\windows\r\n"})
    assert "run_work" not in cats(v)
    assert {"run_built", "files_sys", "purge"} <= cats(v)


def test_a_built_program_is_run_built_and_names_itself():
    v = run("exec", {"command": "hello.exe"}, listing=["HELLO.EXE"])
    assert cats(v) == {"run_built"}
    assert v.hits[0].subject == ROOT + "\\HELLO.EXE"


def test_a_build_script_is_judged_line_by_line():
    script = (b"@echo off\r\nwcl386 hello.c\r\nif errorlevel 1 goto end\r\n"
              b"del *.obj\r\n:end\r\n")
    v = run("exec", {"command": "build.bat"}, listing=["BUILD.BAT"],
            files={ROOT + "\\BUILD.BAT": script})
    assert cats(v) <= {"run_built", "run_work", "files_work", "read"}
    assert ROOT + "\\BUILD.BAT" in v.scripts


def test_a_script_that_touches_a_protected_place_is_system():
    v = run("exec", {"command": "x.bat"}, listing=["X.BAT"],
            files={ROOT + "\\X.BAT": b"copy a.txt c:\\autoexec.bat\r\n"})
    assert "files_sys" in cats(v)


def test_a_script_that_cannot_be_read_is_doubt():
    v = run("exec", {"command": "x.bat"}, listing=["X.BAT"], files={})
    assert "run_else?" in cats(v)


def test_a_makefile_can_only_raise_a_trusted_build():
    mk = b"all:\r\n\t$(CC) hello.c\r\n\tcopy hello.exe c:\\windows\r\n"
    v = run("exec", {"command": "wmake"}, listing=["MAKEFILE"],
            files={ROOT + "\\MAKEFILE": mk})
    assert "files_sys" in cats(v)
    assert "run_else" not in cats(v)   # $(CC) is the tool's business


def test_a_program_run_from_elsewhere_is_else():
    v = run("exec", {"command": "wcl386 a.c", "cwd": "C:\\"})
    assert cats(v) == {"run_else"}


def test_without_a_work_folder_every_program_is_else():
    v = run("exec", {"command": "wcl386 a.c"}, ctx(work_root=""))
    assert cats(v) == {"run_else"}


def test_a_client_that_declared_nothing_still_has_the_shell_safety_net():
    """An old client sends no lists. `format` is still system: the shell
    profile's always_ask is the fallback the plan keeps."""
    c = ctx(lists=K.Lists())
    assert cats(run("exec", {"command": "format c:"}, c)) == {"run_sys"}
    assert cats(run("exec", {"command": "copy a C:\\WINDOWS\\X"}, c)) == {
        "files_else"}


def test_a_shell_with_no_grammar_still_gets_the_danger_scan():
    """4DOS has no [shell] table. `format c:` must not hide in doubt."""
    v = run("exec", {"command": "format c:", "shell": "4dos"})
    assert "run_sys" in cats(v)


def test_writing_into_gaston_is_refused_whatever_the_mode():
    v = run("exec", {"command": "copy x .gaston\\base"})
    assert v.refuse
    assert run("put_file", {"path": ".gaston\\x"}).refuse


def test_sync_rules_are_not_work():
    assert cats(run("put_file", {"path": ".gitignore"})) == {"files_else"}


@pytest.mark.parametrize("tool,args,want", [
    ("put_file", {"path": "src\\a.c"}, {"files_work"}),
    ("put_file", {"path": "C:\\CONFIG.SYS"}, {"files_sys"}),
    ("put_file", {"path": "C:\\GASTON\\GASTON.INI"}, {"files_sys"}),
    ("mkdir", {"path": "D:\\NEW"}, {"files_else"}),
    ("copy", {"path": "a.c", "to": "C:\\WINDOWS\\A.C"}, {"files_sys"}),
    ("move", {"path": "a.c", "to": "D:\\A.C"}, {"files_work", "files_else"}),
    ("move", {"path": ROOT, "to": "D:\\X"}, {"files_else"}),
    ("delete", {"path": "a.c"}, {"files_work"}),
    ("delete", {"path": "a.c", "permanent": True}, {"files_work", "purge"}),
    ("delete", {"path": ROOT}, {"files_else"}),
    ("rmdir", {"path": "C:\\TEMP\\X"}, {"files_else"}),
    ("get_file", {"path": "C:\\AUTOEXEC.BAT"}, {"read"}),
    ("get_file", {"path": "\\\\SERVER\\SHARE\\X"}, {"files_else"}),
    ("list_dir", {"path": "C:\\"}, {"read"}),
    ("drives", {}, {"read"}),
    ("ps", {}, {"read"}),
    ("kill", {"pid": 12}, {"run_else"}),
    ("ui", {}, {"screen_see"}),
    ("shot", {}, {"screen_see"}),
    ("input", {"steps": ["x"]}, {"screen_drive"}),
    ("launch", {"command": "notepad a.txt"}, {"run_work"}),
    ("launch", {"command": "regedit /s x.reg"}, {"run_sys"}),
    ("launch", {"command": "sol.exe"}, {"run_else"}),
    ("Bash", {"command": "ls"}, {"bridge_shell"}),
    ("WebFetch", {"url": "http://x"}, {"web"}),
    ("WebSearch", {"query": "x"}, {"web"}),
])
def test_tools(tool, args, want):
    assert cats(run(tool, args)) == want


def test_bridge_edits_by_where_the_copy_is(tmp_path):
    c = ctx(bridge_dir=str(tmp_path))
    assert cats(run("Write", {"file_path": str(tmp_path / "a.c")}, c)) == {
        "files_work"}
    assert cats(run("Write", {"file_path": "/etc/x"}, c)) == {"files_else"}


def test_bridge_local_tools_are_outside_the_matrix():
    assert run("toolnote", {"name": "watcom"}).exempt
    assert run("sync_status", {}).exempt


def test_plan_exit_is_its_own_thing():
    assert K.classify(ctx(), "ExitPlanMode", {}).plan_exit


def test_an_unknown_tool_lands_nowhere_and_the_policy_asks():
    v = K.classify(ctx(), "SomethingNew", {})
    assert not v.hits and not v.exempt and not v.plan_exit


def test_every_hit_explains_itself():
    v = run("exec", {"command": "wcl386 x.c -fe=C:\\WINDOWS\\X.EXE"})
    assert all(h.rule for h in v.hits)
    sys_hit = next(h for h in v.hits if h.cat == "run_sys")
    assert "C:\\WINDOWS\\**" in sys_hit.rule and sys_hit.source == "client"


def test_launch_and_a_direct_spawn_without_a_grammar_are_doubt_not_a_crash():
    v = run("launch", {"command": "regedit /s x.reg", "shell": "4dos"})
    assert "run_sys" in cats(v)
    v = run("launch", {"command": "sol.exe", "shell": "4dos"})
    assert cats(v) == {"run_else?"}
