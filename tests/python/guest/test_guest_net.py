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
import socket

import conftest
import pytest
from conftest import WINE, guest_exe, wine_env

# harness (a real Bridge, Session monkeypatched to FakeSession, on its own
# port) is defined in test_server_integration.py and re-used rather than
# duplicated, same as test_fakegui_conformance.py already does.
from harness import harness  # noqa: F401

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
    except TimeoutError:
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


# --------------------------------------------------------- slow-reader gate
#
# docs/BACKLOG.md #1's throttled-link gate (`tc netem rate 40kbit`) needs root
# and has never run. The cheaper fallback the plan itself names -- and the one
# used here -- is a deliberate slow reader: rather than throttle the wire, it
# fills the guest's outbound queue directly by refusing to drain it, which is
# what win/net/net.cpp's backpressure path actually needs exercised. Unlike
# netem this needs no root and lives in the suite instead of being run by
# hand.
#
# It reaches two weaknesses BACKLOG.md names that a LAN-speed Wine run never
# does, because the queue never fills and the backpressure path never runs:
# the 64KB outbound queue's byte cap, and the up-to-5s block inside
# rc_net_send that backpressure causes.
#
# MEASURED against the code before this fix (do not re-derive this by hand --
# rerun win/checks/netcheck.c's --slowsend mode against a stalled reader if
# it needs re-checking): three rc_net_send calls in a row, each already over
# the 64KB cap, each waited its full ~5s budget and then queued the frame
# ANYWAY -- 65558, then 98337, then 131116 bytes, every single one reported
# as a successful send. That is not "the cap plus one frame", the ceiling the
# old comment in net.h claimed; it is unbounded growth on a machine with 28KB
# free, one blocked caller at a time. rc_net_send now fails a send it cannot
# queue within the wait instead of enqueueing past the cap.
async def _stalled_reader_handler(reader, writer):
    sock = writer.get_extra_info("socket")
    if sock is not None:
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 2048)
        except OSError:
            pass
    # The whole point: accept the connection and never read from it again.
    # netcheck.exe decides when to stop sending; this handler just holds the
    # socket open (and undrained) until the fixture tears the server down.
    try:
        await asyncio.sleep(3600)
    except asyncio.CancelledError:
        pass


@pytest.fixture
async def stalled_reader():
    """A bare TCP listener that accepts one connection and never reads it.

    Deliberately NOT the `harness` fixture: this scenario does not speak the
    wire protocol at all (win/checks/netcheck.c's --slowsend mode skips HELLO
    negotiation and hammers rc_net_send directly), so a real Bridge would be
    answering a HELLO that never arrives instead of doing anything useful.
    """
    server = await asyncio.start_server(_stalled_reader_handler,
                                        "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        yield port
    finally:
        server.close()
        try:
            await asyncio.wait_for(server.wait_closed(), 1.0)
        except TimeoutError:
            pass


async def _run_slowsend(port):
    exe = guest_exe("netcheck.exe")
    proc = await asyncio.create_subprocess_exec(
        WINE, exe, "--slowsend", "127.0.0.1", str(port), "s",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        env=wine_env())
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=90)
    except TimeoutError:
        proc.kill()
        await proc.communicate()
        pytest.fail("netcheck.exe --slowsend did not finish within 90s -- "
                    "see win/checks/netcheck.c's SLOWSEND_MAX_FRAMES/"
                    "SLOWSEND_SLOW_STREAK, which are meant to bound this")
    return (out.decode("latin-1", "replace"), err.decode("latin-1", "replace"),
            proc.returncode)


@pytest.fixture
async def slowsend_result(stalled_reader):
    """Run netcheck.exe --slowsend once against the stalled reader; every
    test below reads the same run rather than re-running the ~30s scenario
    once per assertion. Function-scoped like `netcheck_result` above, for the
    same reason: `stalled_reader` hands out a fresh ephemeral port per test.
    """
    return await _run_slowsend(stalled_reader)


def test_slowsend_reaches_real_backpressure(slowsend_result):
    """The stalled reader actually produced backpressure -- otherwise every
    other assertion here would pass vacuously. netcheck.c's own "nothing ever
    slowed down" check is what makes a vacuous run loud instead of green, and
    its "backpressure happened ... but every send still succeeded" check is
    the one the pre-fix code actually tripped (see the module docstring for
    the measured numbers: three sends in a row, each already over the 64KB
    cap, each blocked ~5s and then queued the frame anyway)."""
    text, err, rc = slowsend_result
    assert "FAIL: nothing ever slowed down" not in text, (text, err)
    assert "FAIL: backpressure happened" not in text, (text, err)


def test_slowsend_bounded_queue_failed_send_no_deadlock(slowsend_result):
    """The three invariants this scenario exists to guarantee, all from one
    run: the 64KB outbound queue does not grow past its cap under
    backpressure (win/net/net.h's RC_OUTQ_MAX_BYTES comment), a send that
    cannot be queued within the wait fails and says so rather than blocking
    past the documented ~5s budget, and the check itself reaches its own
    PASS line and exits 0 -- proof rc_net_stop's bounded thread-join waits
    did their job even though the reader thread never gets a FIN back from a
    peer that never closes its side."""
    text, err, rc = slowsend_result
    assert "FAIL: queue grew past its cap" not in text, (text, err)
    assert "FAIL: a send blocked" not in text, (text, err)
    assert "PASS: slow-reader scenario" in text, (text, err)
    assert rc == 0, (text, err, rc)
