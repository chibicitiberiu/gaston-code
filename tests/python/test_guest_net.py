"""03.53: netcheck.exe had no automated driver at all -- it printed its own
free-text PASS:/FAIL: verdicts and was run by a human, or not at all, so a
green `make test` said nothing about the guest's networking and framing code.

`netcheck.exe` links the REAL net.cpp and frameproto.cpp (see win/checks/
netcheck.c) and drives a full connect / HELLO / INPUT / TEXT / TURN round
trip -- the same code path win/app/main.cpp uses, without needing GUI
automation to drive it.

The one thing it cannot do unattended is talk to a real model: its default
prompt asks for an exact reply, which only a live Claude session can give,
and this suite is not going to spend API budget or become flaky over the
network on every `make test`. What it needs from the far end is a REAL
bridge speaking the real wire protocol -- HELLO negotiation, frame counting,
compression -- which `test_server_integration.py`'s `harness` fixture already
provides with `Session` replaced by `FakeSession` (echoes the prompt back).
netcheck.c does not check the TEXT it gets back for content, only that some
arrived and that the frame count balances, so the swap is invisible to it.

Driving this for the first time surfaced a real bug, and it was in the
HARNESS rather than in the guest -- which is worth recording, because the
first diagnosis blamed net.cpp and was wrong.

netcheck.exe reported "FAIL: 1 frames leaked" against a real Bridge on every
run, with all 13 frames (READY, DOC, 4x OPTION, OPTIONSEND, ANCHOR, TURN
start, 3x TEXT, TURN end) printed and freed by its own WM_RC_FRAME handler
first. The count was CORRECT. netcheck's main loop sets `g_done` from inside
the dispatch loop, so the instant the TURN end is handled it stops pumping,
while the reader thread can still post a frame that arrived in the same
breath. That frame is genuinely in flight, and `inflight` says so.

So the assertion was demanding a quiescence the harness never waited for.
netcheck now calls `rc_net_stop` FIRST -- which joins the reader, after which
nothing further can be posted -- and drains the queue afterwards. Only then
does `inflight == 0` mean what the check claims: every frame handed to the UI
thread was freed by it.

Kept as a cautionary note because the wrong diagnosis was the plausible one.
A counter that reads 1 when you expect 0 looks like a leak, and the reflex is
to go looking at the allocator.
"""
from __future__ import annotations

import asyncio

import pytest

import conftest
from conftest import WINE, guest_exe, wine_env

# harness (a real Bridge, Session monkeypatched to FakeSession, on its own
# port) is defined in test_server_integration.py and re-used rather than
# duplicated, same as test_fakegui_conformance.py already does.
from test_server_integration import harness   # noqa: F401

pytestmark = conftest.needs_guest


@pytest.fixture
async def netcheck_result(harness):
    """Run netcheck.exe once against a real Bridge; every test below reads
    the same run rather than re-running the exe once per assertion.

    Function-scoped, not module-scoped: `harness` itself is function-scoped
    (a fresh port per test, per its own docstring), so a module-scoped
    fixture here would either hold a stale harness or force one anyway --
    this just follows the shorter-lived fixture's own lifetime.
    """
    exe = guest_exe("netcheck.exe")
    proc = await asyncio.create_subprocess_exec(
        WINE, exe, "127.0.0.1", str(harness.port), "s",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        env=wine_env())
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=60)
    except (TimeoutError, asyncio.TimeoutError):
        proc.kill()
        await proc.communicate()
        pytest.fail("netcheck.exe did not finish within 60s")
    return out.decode("latin-1", "replace"), err.decode("latin-1", "replace")


def test_netcheck_gets_a_real_reply_and_a_clean_turn_end(netcheck_result):
    """The three failure modes netcheck.c reports that are genuinely about
    this round trip working at all: an error frame, a timed-out wait, or no
    text ever arriving. See win/checks/netcheck.c's four `FAIL:` checks --
    the fourth, the leaked-frame count, is asserted on its own below."""
    text, err = netcheck_result
    assert "FAIL: error frame or link failure" not in text, (text, err)
    assert "FAIL: timed out" not in text, (text, err)
    assert "FAIL: no text received" not in text, (text, err)
    assert "TURN         state=end" in text, (text, err)
    assert "echo: Reply with exactly: PONG-42" in text, (text, err)


def test_netcheck_reports_no_leaked_frames(netcheck_result):
    """Every frame posted to the UI thread was freed by it.

    This is net.h's three-way ownership rule asserted end to end against the
    real net.cpp, which nothing else does. It was an xfail until the harness
    learned to stop the link before counting; see the module docstring.
    """

    text, err = netcheck_result
    assert "frames leaked" not in text, (text, err)
    assert "PASS: guest framing + networking round trip" in text, (text, err)
