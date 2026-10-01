"""`ui`'s arguments have to mean what they say.

One of these was reported from a real session (2026-10-01): the agent asked
for the windows of one process and got a 25-control dump of the whole desktop,
could not find what it was looking for in it, and had no reason to suspect the
scope it asked for was not the scope it got. `int(a.get("pid") or 0)` makes
"absent" and "0" the same value and the guest reads 0 as "no filter", so the
narrowest scope on offer silently became the widest.
"""
from __future__ import annotations

import asyncio

from bridge.agent import tools as machine_tools
from bridge.files import caps as C


class FakeUi:
    def __init__(self):
        self.calls: list[dict] = []

    async def tree(self, guest, **kw):
        self.calls.append(kw)
        return [], {}


class AutomationGuest:
    caps = C.CONSTRAINED.__class__(automation=C.AUTOMATION_SCREEN)


class _Params:
    def __init__(self, name, arguments):
        self.name, self.arguments = name, arguments


def _call(srv, name, args):
    handler = srv._request_handlers["tools/call"].handler
    return asyncio.run(handler(None, _Params(name, args)))


def _body(res) -> str:
    content = res["content"] if isinstance(res, dict) else res.content
    return "\n".join(c["text"] if isinstance(c, dict) else c.text
                     for c in content)


def _server(ui):
    return machine_tools.build(None, None, None, AutomationGuest(),
                               ui=ui)["machine"]["instance"]


def test_scope_pid_without_a_pid_is_refused():
    ui = FakeUi()
    out = _body(_call(_server(ui), "ui", {"scope": "pid"}))
    assert ui.calls == []            # the machine was never asked
    assert "ps" in out               # and the agent is told how to get one


def test_scope_pid_with_zero_is_the_same_refusal():
    """What the live session actually passed."""
    ui = FakeUi()
    out = _body(_call(_server(ui), "ui", {"scope": "pid", "pid": 0}))
    assert ui.calls == []
    assert "0 is not one" in out


def test_a_pid_with_the_wrong_scope_is_refused_too():
    """The other direction, and it was equally silent: a pid passed with
    `scope: "desktop"` was simply ignored, so the agent believed it had
    narrowed something."""
    ui = FakeUi()
    out = _body(_call(_server(ui), "ui", {"scope": "desktop", "pid": 4321}))
    assert ui.calls == []
    assert "4321" in out


def test_scope_pid_with_a_pid_goes_through():
    ui = FakeUi()
    _call(_server(ui), "ui", {"scope": "pid", "pid": 4321})
    assert ui.calls[0]["scope"] == "pid"
    assert ui.calls[0]["pid"] == 4321


def test_the_default_scope_still_needs_no_pid():
    ui = FakeUi()
    _call(_server(ui), "ui", {})
    assert ui.calls[0]["scope"] == "desktop"
    assert ui.calls[0]["pid"] == 0
