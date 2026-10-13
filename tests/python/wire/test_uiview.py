"""What a control tree says when part of it is deliberately missing.

The guest never walks its own windows, and for a long time it also never
said so. A real Haiku agent on the VM (docs/measurements/e2e-vm-20260927.txt)
was asked for the client's own control tree, got a list the client was merely
absent from, and read that as "not open" -- it looped tree, shot, tree until
a human pressed Deny. The count is on the wire now, and these are the lines
that have to carry it to the model.
"""

from __future__ import annotations

from bridge.wire.uirec import NF_ENABLED, NF_VISIBLE, UiNode
from bridge.wire.uiview import render_tree


def node(path: str = "0", text: str = "OK", sel: str = 'button[text="OK"]'):
    return UiNode(
        src="w", path=path, parent="", ctrl=1, child=0,
        rect=(0, 0, 40, 20), flags=NF_VISIBLE | NF_ENABLED,
        cls="Button", text=text, role="button", sel=sel,
    )


def test_hidden_self_is_announced():
    out = render_tree([node()], {"nodes": "1", "self": "2"})
    assert "2 window(s) of this program itself" in out
    # Before the controls: a warning under the list is a warning unread.
    assert out.index("window(s) of this program") < out.index("sel=")


def test_no_self_no_line():
    out = render_tree([node()], {"nodes": "1", "self": "0"})
    assert "this program itself" not in out


def test_missing_header_is_not_an_error():
    """An older guest sends no self= at all; it must still render."""
    out = render_tree([node()], {"nodes": "1"})
    assert "sel=" in out
    assert "this program itself" not in out


def test_empty_tree_still_explains_itself():
    """The silent case -- and the one the agent actually hit."""
    out = render_tree([], {"nodes": "0", "self": "1"})
    assert "1 window(s) of this program itself" in out
    assert "try `shot`" in out


def test_empty_tree_without_self_says_only_the_usual():
    out = render_tree([], {"nodes": "0", "self": "0"})
    assert "this program itself" not in out
    assert "try `shot`" in out


def test_the_line_tells_the_model_to_stop_looking():
    """The failure was a loop, so the words have to end one."""
    out = render_tree([], {"nodes": "0", "self": "1"})
    assert "never will be" in out
    assert "Do not go looking for it." in out


# --- refusals that have to stay actionable -----------------------------

from bridge.agent.tools import _ui_explain
from bridge.services.ui import UiError


def test_occluded_names_what_is_on_top():
    """The point of BACKLOG 16: "something is on top" is not actionable."""
    out = _ui_explain(UiError("occluded", "", {"on_top": 'Notepad "Untitled"'}))
    assert 'Notepad "Untitled"' in out
    # And it must not still be telling the model to do the thing click now
    # does for itself.
    assert "Bring the window forward first" not in out


def test_occluded_survives_a_guest_that_does_not_send_on_top():
    """An older client sends no on_top; the sentence still has to render."""
    out = _ui_explain(UiError("occluded", "", {}))
    assert "another window" in out
    assert "refused" in out or "nothing was clicked" in out


def test_occluded_says_the_raise_was_already_tried():
    out = _ui_explain(UiError("occluded", "", {"on_top": "Shell_TrayWnd"}))
    assert "brought to the front" in out


# --- the FOCUS op ------------------------------------------------------

from bridge.wire import protocol as P


def test_ui_run_carries_the_steps_in_the_body():
    """Steps ride the BODY: a selector clipped to a 255-byte header is a
    different selector."""
    f = P.ui_run("u7", b"focus\tsel=Notepad\n", timeout_ms=5000, step_ms=900,
                 settle=200)
    assert f.type == "UIRUN" and f.body == b"focus\tsel=Notepad\n"
    assert f.headers == {"id": "u7", "timeout_ms": "5000", "step_ms": "900",
                         "settle": "200"}


def test_ui_cancel_names_the_run_it_stops():
    f = P.ui_cancel("u7")
    assert f.type == "UICANCEL" and f.headers == {"id": "u7"}


# --- OCR, and the machine that has no OCR engine ---------------------------
#
# The absent case is the one worth pinning, because it is what every machine
# but the one this was written on will do. `to_png` set the contract for a
# missing dependency -- None rather than an exception, so the tool says so
# and carries on -- and `to_text` has to keep it or a bridge without
# Tesseract fails every `ocr` call instead of explaining itself once.

def test_no_tesseract_is_none_and_not_an_exception(monkeypatch):
    import bridge.wire.uiview as V
    monkeypatch.setattr(V.shutil, "which", lambda _name: None)
    assert V.to_text(b"BM not really a bitmap") is None


def test_tesseract_that_refuses_is_none_too(monkeypatch):
    """A non-zero exit is a refusal, not a crash: an unreadable capture
    should read as "nothing found here", never as a broken bridge."""
    import subprocess

    import bridge.wire.uiview as V

    monkeypatch.setattr(V.shutil, "which", lambda _name: "/usr/bin/tesseract")

    def refuse(*_a, **_k):
        return subprocess.CompletedProcess([], 1, b"", b"Error in pixReadMem")

    monkeypatch.setattr(V.subprocess, "run", refuse)
    assert V.to_text(b"BM nonsense") is None


def test_what_tesseract_printed_is_what_comes_back(monkeypatch):
    import subprocess

    import bridge.wire.uiview as V

    seen = {}

    def ran(cmd, **kw):
        seen["cmd"] = cmd
        seen["timeout"] = kw.get("timeout")
        return subprocess.CompletedProcess(cmd, 0, b"Setup is copying\n", b"")

    monkeypatch.setattr(V.shutil, "which", lambda _name: "/usr/bin/tesseract")
    monkeypatch.setattr(V.subprocess, "run", ran)
    assert V.to_text(b"BM bitmap") == "Setup is copying\n"
    # --psm 6 is the one argument with a reason behind it: a dialog is a
    # uniform block of text, and the default page segmentation is worse at
    # it (docs/PLAN.md). Pinned so it is not dropped as noise.
    assert "--psm" in seen["cmd"] and "6" in seen["cmd"]
    assert seen["cmd"][2] == "stdout"
    assert seen["timeout"] == V.OCR_TIMEOUT_S


def test_the_capture_is_handed_over_as_a_file_that_goes_away(monkeypatch):
    """Tesseract is given a path, never our stdin, and the temp directory is
    its own so the name cannot collide with a concurrent call."""
    import os
    import subprocess

    import bridge.wire.uiview as V

    paths = []

    def ran(cmd, **_kw):
        paths.append(cmd[1])
        assert os.path.exists(cmd[1]), "the capture is not there to read"
        with open(cmd[1], "rb") as fp:
            assert fp.read() == b"BM the bytes the guest sent"
        return subprocess.CompletedProcess(cmd, 0, b"ok\n", b"")

    monkeypatch.setattr(V.shutil, "which", lambda _name: "/usr/bin/tesseract")
    monkeypatch.setattr(V.subprocess, "run", ran)
    V.to_text(b"BM the bytes the guest sent")
    assert paths and not os.path.exists(paths[0]), "the temp file stayed"
