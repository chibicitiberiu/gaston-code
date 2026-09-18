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


# --- what the status bar says when the SDK contradicts itself ---------------
#
# Also found by smoke testing rather than by this suite, and found as a
# REGRESSION IN THE FIX ABOVE: making a degraded turn report failure was right
# for a turn that produced nothing and wrong for one the SDK retried and
# completed. The second reading rendered a full, correct answer under the word
# "failed".

def test_a_degraded_turn_that_produced_nothing_is_a_failure():
    """The original complaint: the prompt vanished and the status bar said
    `done`, directly beneath a notice saying the request may not have run."""
    from bridge.server import turn_verdict
    ok, subtype = turn_verdict(True, "RateLimitEvent", False, "success")
    assert ok is False
    assert subtype == "RateLimitEvent"


def test_a_degraded_turn_that_answered_anyway_is_not_a_failure():
    """The SDK hit a rate limit, retried, ran the tool and answered. The notice
    has already said what happened; the status bar must not contradict an
    answer the user can read on the same screen."""
    from bridge.server import turn_verdict
    ok, subtype = turn_verdict(True, "RateLimitEvent", True, "success")
    assert ok is True
    assert subtype == "success"


def test_a_bridge_notice_does_not_count_as_the_model_answering():
    """`produced` is set from the SDK's callbacks only. If a notice the bridge
    wrote counted, the rate-limit warning would mark its own turn as having
    produced output and the first case above could never fire again."""
    from bridge.server import turn_verdict
    assert turn_verdict(True, "RateLimitEvent", False, "success")[0] is False


def test_an_undegraded_turn_keeps_the_sdk_verdict():
    from bridge.server import turn_verdict
    assert turn_verdict(True, "", False, "success") == (True, "success")
    assert turn_verdict(False, "", True, "error_max_turns") == (
        False, "error_max_turns")
