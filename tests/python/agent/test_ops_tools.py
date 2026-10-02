"""The fs mutations and `launch`: gated on the client saying it answers
them, refusals in words, and the mirrored folder protected."""
from __future__ import annotations

import asyncio

from bridge.agent import tools as machine_tools
from bridge.files import caps as C
from bridge.services.guest_fs import FsError


class FakeFs:
    def __init__(self, raise_status="", headers=None):
        self.calls, self.raise_status = [], raise_status
        self.headers = headers or {"status": "ok"}
        self.flags = []

    async def op(self, guest, op, path, to="", *, overwrite=False,
                 permanent=False):
        self.calls.append((op, path, to))
        self.flags.append((overwrite, permanent))
        if self.raise_status:
            raise FsError(self.raise_status, "C:\\X\\stuck.txt")
        return self.headers


class FakeProc:
    def __init__(self, headers):
        self.headers, self.calls = headers, []

    async def launch(self, guest, command, *, cwd="", show=""):
        self.calls.append((command, cwd, show))
        return self.headers


class Guest:
    def __init__(self, ops=2, work_dir="", work_guest=""):
        self.caps = C.Caps(ops=ops)
        self.work_dir, self.work_guest = work_dir, work_guest


class _P:
    def __init__(self, name, arguments):
        self.name, self.arguments = name, arguments


def _call(guest, name, args, fs=None, proc=None, sync=None):
    srv = machine_tools.build(None, sync, None, guest, fs=fs or FakeFs(),
                              proc=proc)["machine"]["instance"]
    res = asyncio.run(srv._request_handlers["tools/call"].handler(
        None, _P(name, args)))
    content = res["content"] if isinstance(res, dict) else res.content
    text = "\n".join(c["text"] if isinstance(c, dict) else c.text
                     for c in content)
    err = res.get("isError") if isinstance(res, dict) else res.is_error
    return text, err


def _names(guest):
    srv = machine_tools.build(None, None, None, guest,
                              fs=FakeFs())["machine"]["instance"]
    listed = asyncio.run(srv._request_handlers["tools/list"].handler(
        None, None))
    tools = listed.root.tools if hasattr(listed, "root") else listed.tools
    return {t.name for t in tools}


def test_a_client_that_answers_them_is_offered_them():
    assert {"mkdir", "copy", "move", "delete", "rmdir",
            "launch"} <= _names(Guest())


def test_a_client_on_the_first_ops_version_is_not():
    """ops=1 had file-only delete and no copy; the tools now promise trees."""
    assert not {"copy", "delete", "move"} & _names(Guest(ops=1))


def test_a_client_that_never_said_is_not():
    assert not {"mkdir", "move", "delete", "rmdir", "launch"} & _names(
        Guest(ops=0))


def test_list_dir_refuses_at_once_for_an_old_client():
    """It used to send FSLS anyway, and an old client's UI thread logged it
    as unhandled while the tool waited four minutes."""
    fs = FakeFs()
    text, err = _call(Guest(ops=0), "list_dir", {"path": "C:\\"}, fs=fs)
    assert err and "too old" in text


def test_mkdir_sends_the_path_and_says_it_exists():
    fs = FakeFs()
    text, err = _call(Guest(), "mkdir", {"path": r"C:\A\B"}, fs=fs)
    assert not err and fs.calls == [("FSMKDIR", r"C:\A\B", "")]
    assert "exists now" in text


def test_a_refusal_is_the_guests_reason_in_words():
    fs = FakeFs("not_empty")
    text, err = _call(Guest(), "rmdir", {"path": r"C:\A"}, fs=fs)
    assert err and "not empty" in text and "never recursive" in text
    fs = FakeFs("cross_volume")
    text, err = _call(Guest(), "move", {"path": r"C:\A", "to": r"D:\A"}, fs=fs)
    assert err and "folders cannot" in text   # files can, measured


def test_move_needs_both_ends():
    _, err = _call(Guest(), "move", {"path": r"C:\A", "to": ""})
    assert err


class FakeBase:
    def __init__(self, latched=()):
        self.latched = {k: "conflict" for k in latched}
        self.prev = {}


class FakeSync:
    def __init__(self, base):
        self.base = base

    def baseline_lock(self, guest):
        import contextlib

        class _L(contextlib.AbstractAsyncContextManager):
            async def __aexit__(self, *a):
                return False
        return _L()

    def load_baseline(self, guest):
        return self.base


def test_a_path_held_in_conflict_is_not_touched():
    """The person's copy is what the halt protects."""
    fs = FakeFs()
    g = Guest(work_dir="/tmp/m", work_guest=r"C:\WORK")
    text, err = _call(g, "delete", {"path": r"C:\WORK\notes.txt"}, fs=fs,
                      sync=FakeSync(FakeBase(["notes.txt"])))
    assert err and "conflict" in text and fs.calls == []


def test_a_folder_holding_a_conflict_is_not_touched_either():
    fs = FakeFs()
    g = Guest(work_dir="/tmp/m", work_guest=r"C:\WORK")
    _, err = _call(g, "move", {"path": r"C:\WORK\sub",
                                  "to": r"C:\WORK\sub2"}, fs=fs,
                      sync=FakeSync(FakeBase(["sub/a.txt"])))
    assert err and fs.calls == []


def test_outside_the_mirror_nothing_is_consulted():
    fs = FakeFs()
    g = Guest(work_dir="/tmp/m", work_guest=r"C:\WORK")
    _, err = _call(g, "delete", {"path": r"C:\WORK2\x.txt"}, fs=fs,
                   sync=FakeSync(FakeBase(["x.txt"])))
    assert not err and fs.calls == [("FSRM", r"C:\WORK2\x.txt", "")]


def test_the_sync_layers_own_folder_is_refused():
    g = Guest(work_dir="/tmp/m", work_guest=r"C:\WORK")
    text, err = _call(g, "delete", {"path": r"C:\WORK\.gaston\x"},
                      sync=FakeSync(FakeBase()))
    assert err and "sync layer" in text


def test_launch_returns_the_pid():
    proc = FakeProc({"status": "ok", "pid": "FFF12345"})
    text, err = _call(Guest(), "launch", {"command": "notepad"}, proc=proc)
    assert not err and "FFF12345" in text
    assert proc.calls == [("notepad", "", "")]


def test_launch_says_why_it_did_not_start():
    proc = FakeProc({"status": "not_found", "error": "2"})
    text, err = _call(Guest(), "launch", {"command": "nosuch"}, proc=proc)
    assert err and "full path" in text


def test_delete_goes_to_the_recycle_bin_unless_permanent():
    fs = FakeFs(headers={"status": "ok", "how": "recycled", "files": "3",
                         "dirs": "1", "bytes": "2048"})
    text, err = _call(Guest(), "delete", {"path": r"C:\OLD"}, fs=fs)
    assert not err and "Recycle Bin" in text and "3 files" in text
    assert fs.flags == [(False, False)]
    fs = FakeFs(headers={"status": "ok", "how": "deleted", "files": "1"})
    text, _ = _call(Guest(), "delete", {"path": r"C:\OLD",
                                        "permanent": True}, fs=fs)
    assert "for good" in text and fs.flags == [(False, True)]


def test_copy_sends_overwrite_only_when_asked():
    fs = FakeFs(headers={"status": "ok", "files": "2", "bytes": "10"})
    text, err = _call(Guest(), "copy", {"path": r"C:\A", "to": r"D:\B"},
                      fs=fs)
    assert not err and fs.calls == [("FSCOPY", r"C:\A", r"D:\B")]
    assert fs.flags == [(False, False)] and "every file checked" in text
    _call(Guest(), "copy", {"path": r"C:\A", "to": r"D:\B",
                            "overwrite": True}, fs=fs)
    assert fs.flags[-1] == (True, False)


def test_a_move_says_whether_it_renamed_or_copied():
    fs = FakeFs(headers={"status": "ok", "how": "copied", "files": "5"})
    text, _ = _call(Guest(), "move", {"path": r"C:\A", "to": r"D:\A"},
                    fs=fs)
    assert "original removed" in text


def test_a_tree_refusal_names_where_it_stopped():
    fs = FakeFs("short_copy")
    text, err = _call(Guest(), "copy", {"path": r"C:\A", "to": r"D:\A"},
                      fs=fs)
    assert err and "shorter" in text and "stuck.txt" in text


# --- the preview and the reviewer ------------------------------------------


def test_the_preview_counts_what_a_folder_holds():
    from bridge.agent.review import describe
    line = describe({"how": "dir", "files": "132", "dirs": "14",
                     "bytes": str(3 * 1024 * 1024)}, ["a.txt", "b/c.txt"])
    assert "132 files in 14 folders, 3.0 MB" in line and "b\\c.txt" in line
    assert describe({"how": "file", "bytes": "2048"}, []) == "a file, 2 KB"


def test_the_reviewer_approves_only_on_safe(monkeypatch):
    from bridge.agent import review

    async def said(text):
        async def _ask(prompt):
            return text
        return _ask

    class G:
        recent_prompts = ("delete the old build folder C:\\WORK\\BUILD",)

    for answer, ok in (("SAFE\nthe person asked for it", True),
                       ("ASK\nnot mentioned", False), ("maybe", False)):
        monkeypatch.setattr(review, "_ask", asyncio.run(said(answer)))
        approved, why = asyncio.run(review.review(
            G(), "mcp__machine__delete", {"path": r"C:\WORK\BUILD"}))
        assert approved is ok and why


def test_a_reviewer_that_fails_hands_it_to_the_person(monkeypatch):
    from bridge.agent import review

    async def boom(prompt):
        raise RuntimeError("no network")
    monkeypatch.setattr(review, "_ask", boom)
    approved, why = asyncio.run(review.review(
        type("G", (), {"recent_prompts": []})(), "mcp__machine__delete", {}))
    assert not approved and "could not be reached" in why
