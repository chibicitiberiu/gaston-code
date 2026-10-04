"""Execution modes: the presets, the decision, and how many times a person
is actually interrupted.

The last half is the user's own complaint made into a test -- "setting auto
and still getting permission prompts is annoying". Five sessions the
workflow review simulated call by call (docs/OPTIONS-PLAN.md) are replayed
through the real desk under each preset, and the bars and checks counted.
"""
from __future__ import annotations

import asyncio

import pytest
from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny

from bridge.agent import categories as K
from bridge.agent import policy as P
from bridge.agent import review
from bridge.agent.permdesk import PermissionDesk
from bridge.files import caps as C
from bridge.files import paths
from bridge.view.transcript import TranscriptView

ROOT = "C:\\WORK\\HELLO"


def hit(cat, doubt=False, subject=""):
    return K.Hit(cat, "rule for " + cat, doubt=doubt, subject=subject)


def verdict(*hits):
    v = K.Verdict()
    v.hits = list(hits)
    return v


# --- the presets --------------------------------------------------------------


def test_the_presets_are_the_plans_table():
    auto = P.preset("auto")
    assert auto.value(K.FILES_WORK) == P.ALLOW
    assert auto.value(K.RUN_BUILT) == P.ALLOW
    assert auto.value(K.FILES_SYS) == P.CHECK
    assert auto.value(K.RUN_SYS) == P.CHECK
    wf = P.preset("workfolder")
    assert wf.value(K.RUN_WORK) == P.ALLOW and wf.value(K.RUN_BUILT) == P.ASK
    assert wf.value(K.WEB) == P.ALLOW
    manual = P.preset("manual")
    assert manual.value(K.READ) == P.ALLOW
    assert all(manual.value(c) == P.ASK for c in K.CATS
               if c not in (K.READ, K.SCREEN_SEE))
    assert P.preset("plan").sdk == "plan"
    assert all(P.preset(m).sdk == "default"
               for m in ("manual", "workfolder", "auto"))


def test_a_category_a_mode_predates_asks():
    m = P.Mode("u-1", "Mine", values={K.READ: P.ALLOW}, builtin=False)
    assert m.value(K.RUN_SYS) == P.ASK


# --- the decision ---------------------------------------------------------------


def test_strictest_wins():
    d = P.decide(P.preset("auto"),
                 verdict(hit(K.READ), hit(K.FILES_WORK), hit(K.FILES_SYS)))
    assert d.value == P.CHECK and d.hit.cat == K.FILES_SYS


def test_doubt_takes_the_stricter_of_else_and_sys_but_never_off():
    m = P.Mode("u", "u", values={**P.preset("auto").values,
                                 K.FILES_ELSE: P.ALLOW,
                                 K.FILES_SYS: P.OFF}, builtin=False)
    assert P.decide(m, verdict(hit(K.FILES_ELSE))).value == P.ALLOW
    assert P.decide(m, verdict(hit(K.FILES_ELSE, doubt=True))).value == P.ASK


def test_refuse_exempt_plan_exit_and_unknown():
    v = K.Verdict()
    v.refuse = "no"
    assert P.decide(P.preset("auto"), v).value == P.REFUSE
    v = K.Verdict()
    v.exempt = True
    assert P.decide(P.preset("manual"), v).value == P.ALLOW
    v = K.Verdict()
    v.plan_exit = True
    assert P.decide(P.preset("auto"), v).value == P.ASK
    assert P.decide(P.preset("auto"), K.Verdict()).value == P.ASK


def test_a_grant_lifts_ask_and_check_only_where_grantable():
    m = P.preset("manual")
    assert P.decide(m, verdict(hit(K.RUN_WORK)), {"run_work"}).value == \
        P.ALLOW
    # Never the system tier, an unknown program, or a doubtful finding.
    assert P.decide(m, verdict(hit(K.RUN_SYS)), {"run_sys"}).value == P.ASK
    assert P.decide(m, verdict(hit(K.RUN_ELSE)), {"run_else"}).value == P.ASK
    assert P.decide(m, verdict(hit(K.FILES_WORK, doubt=True)),
                    {"files_work"}).value == P.ASK


def test_a_grant_with_a_subject_covers_that_subject_only():
    m = P.preset("workfolder")
    a = verdict(hit(K.RUN_BUILT, subject="C:\\W\\A.EXE"))
    b = verdict(hit(K.RUN_BUILT, subject="C:\\W\\B.EXE"))
    g = {"run_built:C:\\W\\A.EXE"}
    assert P.decide(m, a, g).value == P.ALLOW
    assert P.decide(m, b, g).value == P.ASK


def test_legacy_names_map_and_round_trip():
    for old, new in P.LEGACY.items():
        assert P.lookup(old).id == new
        assert P.AS_LEGACY[new] == old


def test_a_user_mode_summary_is_generated():
    m = P.Mode("u-1", "Mine", values={**P.preset("manual").values,
                                     K.RUN_WORK: P.ALLOW}, builtin=False)
    assert "running trusted programs" in P.summary(m)


# --- how often a person is interrupted -------------------------------------------


class Guest:
    def __init__(self, mode_id):
        self.mode_id = mode_id
        self.mode_before_plan = ""
        self.grants = set()
        self.caps = C.Caps(ops=2)
        self.work_dir = ""
        self.work_guest = ROOT
        self.hello = {}
        self.recent_prompts = ["build my program"]
        self.tools = []
        self.view = TranscriptView()
        self.prefs = type("Prefs", (), {
            "lists": K.Lists(
                protect=("C:\\WINDOWS\\**", "C:\\*.SYS", "C:\\AUTOEXEC.BAT"),
                trust=frozenset({"wcl386", "wmake", "notepad"})),
            "modes": None, "general_mode": ""})()

    def on_guest(self, p):
        return paths.on_guest(p, ROOT)


class Ctx:
    tool_use_id = "toolu_1"
    suggestions: tuple = ()
    title = description = display_name = None


#: What is in the work folder, for the classifier's cwd-first lookup.
FOLDER = {"HELLO.C", "HELLO.EXE", "BUILD.BAT"}
FILES = {ROOT + "\\BUILD.BAT":
         b"@echo off\r\nwcl386 hello.c\r\nif errorlevel 1 goto end\r\n"
         b"del *.obj\r\n:end\r\n"}

SESSIONS = {
    "hello world": [
        ("mcp__machine__toolnote", {"name": "watcom"}),
        ("mcp__machine__put_file", {"path": "hello.c", "content": "x"}),
        ("mcp__machine__exec", {"command": "wcl386 hello.c"}),
        ("mcp__machine__exec", {"command": "hello.exe"}),
        ("mcp__machine__put_file", {"path": "hello.c", "content": "y"}),
        ("mcp__machine__exec", {"command": "build.bat"}),
        ("mcp__machine__exec", {"command": "hello.exe"}),
        ("mcp__machine__exec", {"command": "dir"}),
    ],
    "config.sys": [
        ("mcp__machine__get_file", {"path": "C:\\CONFIG.SYS"}),
        ("mcp__machine__list_dir", {"path": "C:\\"}),
        ("mcp__machine__exec", {"command": "mem /c"}),
        ("mcp__machine__put_file", {"path": "C:\\CONFIG.SYS",
                                    "content": "x"}),
        ("mcp__machine__exec", {"command": "scanreg /backup"}),
    ],
    "notepad letter": [
        ("mcp__machine__launch", {"command": "notepad letter.txt"}),
        ("mcp__machine__ui", {}),
        ("mcp__machine__input", {"steps": ["Dear Sir"]}),
        ("mcp__machine__shot", {}),
    ],
    "clean temp": [
        ("mcp__machine__list_dir", {"path": "C:\\TEMP"}),
        *[("mcp__machine__delete", {"path": f"C:\\TEMP\\F{i}.TMP"})
          for i in range(5)],
    ],
    "port": [
        ("mcp__machine__get_file", {"path": "D:\\PROJECTS\\FOO\\MAIN.C"}),
        ("Edit", {"file_path": "/nowhere/main.c"}),
        ("mcp__machine__exec", {"command": "wmake"}),
        ("mcp__machine__exec", {"command": "hello.exe"}),
    ],
}


def _count(mode_id: str, calls: list, monkeypatch) -> tuple[int, int]:
    """(bars, checks) for one session under one mode. The checker says SAFE
    to everything; a bar is answered Allow at once."""
    checks = []

    async def fake_review(guest, tool, args, preview_text="", **kw):
        checks.append(tool)
        return True, "consistent"

    monkeypatch.setattr(review, "review", fake_review)
    sent = []
    guest = Guest(mode_id)

    async def write_block(html):
        pass

    async def send(frame):
        sent.append(frame)
        if frame.type == "PERMREQ":
            asyncio.get_running_loop().call_soon(
                lambda: asyncio.ensure_future(desk.reply(
                    frame.headers["id"], "allow")))

    async def fetch(kind, path):
        if kind == "ls":
            return sorted(FOLDER)
        return FILES.get(path.upper())

    desk = PermissionDesk(guest, cwd="/tmp", write_block=write_block,
                          send=send, spawn=asyncio.ensure_future,
                          fetch=fetch)

    async def go():
        cb = desk.callback()
        for tool, args in calls:
            r = await cb(tool, args, Ctx())
            assert isinstance(r, PermissionResultAllow | PermissionResultDeny)

    asyncio.run(go())
    return sum(1 for f in sent if f.type == "PERMREQ"), len(checks)


@pytest.mark.parametrize("name", SESSIONS)
def test_auto_never_puts_up_a_bar_when_the_checker_is_sure(name, monkeypatch):
    bars, _ = _count("auto", SESSIONS[name], monkeypatch)
    assert bars == 0


def test_auto_builds_and_runs_hello_world_without_even_a_check(monkeypatch):
    """No rubber-stamp checks on the build-and-run loop: everything in it is
    the work folder, a trusted compiler, or something built there."""
    bars, checks = _count("auto", SESSIONS["hello world"], monkeypatch)
    assert (bars, checks) == (0, 0)


def test_auto_checks_the_system_files(monkeypatch):
    _, checks = _count("auto", SESSIONS["config.sys"], monkeypatch)
    assert checks == 2          # CONFIG.SYS, and scanreg


def test_free_in_work_folder_asks_little_for_the_build_loop(monkeypatch):
    bars, _ = _count("workfolder", SESSIONS["hello world"], monkeypatch)
    # Running what it built (hello.exe twice, build.bat once) asks; writing
    # and compiling do not.
    assert bars == 3


def test_ask_first_does_not_ask_to_read(monkeypatch):
    bars, _ = _count("manual", [
        ("mcp__machine__get_file", {"path": "C:\\AUTOEXEC.BAT"}),
        ("mcp__machine__list_dir", {"path": "C:\\"}),
        ("mcp__machine__exec", {"command": "dir C:\\WINDOWS"}),
        ("mcp__machine__exec", {"command": "type C:\\CONFIG.SYS"}),
        ("mcp__machine__ps", {}),
        ("mcp__machine__drives", {}),
    ], monkeypatch)
    assert bars == 0
