"""An SDK message this bridge does not handle must reach the user.

Found by smoke testing the real app on the real machine, not by this suite.
The session layer logged an unrecognised SDK message at DEBUG and dropped it.
One of those is `RateLimitEvent`, which ends a turn having produced nothing --
so the user typed a prompt, watched the app return to idle, and there was
nothing anywhere on screen to say why. `CLAUDE.md`'s first standing rule is
that the person at the keyboard never loses a byte.

No test here could have caught it: it needs the live SDK in a state nothing in
this suite can fake. What a test CAN do is pin the handling, which is what
this does -- the session's fallback routes the type through `on_system`, and
the bridge turns it into a notice.
"""
from __future__ import annotations

import pytest

from bridge.view import html as H


class _Unknown:
    """Stands in for an SDK type this bridge has never seen."""


@pytest.mark.asyncio
async def test_an_unhandled_sdk_message_is_announced_not_swallowed():
    from bridge.agent import session as S

    seen: list[tuple[str, dict]] = []

    async def on_system(subtype: str, data: dict) -> None:
        seen.append((subtype, data))

    s = S.Session.__new__(S.Session)
    s.on_text = s.on_tool = s.on_tool_result = s.on_thinking = None
    s.on_system = on_system
    s._streaming = False

    await S.Session._dispatch(s, _Unknown())

    assert seen, "an unrecognised SDK message was dropped silently"
    assert seen[0][0] == "sdk_event"
    assert seen[0][1]["event"] == "_Unknown"


def test_the_rate_limit_notice_says_what_to_do():
    """The wording matters: the user's prompt may simply not have run."""
    note = H.notice(
        "The model provider is rate limiting this account. "
        "The request may not have run -- try again shortly.")
    assert "rate limiting" in note
    assert "may not have run" in note
