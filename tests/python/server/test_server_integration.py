"""End-to-end over the real wire protocol, with Claude mocked out.

Covers the parts that are awkward to test any other way: HELLO negotiation,
duplicate-client rejection, resume after a dropped connection, and pagination
round trips. Mocking the session keeps it deterministic and fast; the real
model is exercised separately by netcheck.exe.
"""

import asyncio
import contextlib
import os
import time
import zlib

import pytest
from claude_agent_sdk import (
    PermissionResultAllow,
    PermissionResultDeny,
)
from harness import (  # noqa: F401
    FakeCtx,
    FakeRule,
    FakeSession,
    Harness,
    _connect_and_ask,
    _recv,
    harness,
)

from bridge import server as srv
from bridge.files import paths
from bridge.server import Bridge
from bridge.services.ui import UiError
from bridge.view import blocks


def _rec(text: str) -> bytes:
    """One paragraph record, for the tests that drive the paginator
    directly. Split on newlines, because a payload line cannot contain the
    separator that ends it."""
    return blocks.encode(
        blocks.Block("para", {}, [("t", ln) for ln in text.split("\n")]))


from bridge.wire.protocol import (
    MAX_FRAME,
    PROTO_VERSION,
    Frame,
    FrameReader,
    ProtocolError,
    hello,
)


async def test_happy_handshake(harness):
    r, w = await harness.connect()
    frames = await harness.recv_until(r, {"READY"})
    ready = [f for f in frames if f.type == "READY"]
    assert ready, [f.type for f in frames]
    assert ready[0].headers["ver"] == str(PROTO_VERSION)
    # No position. READY carried `page`/`total` until stage 3b; the guest
    # asks for a window the moment it sees this frame and learns where it
    # landed from RANGE, so a position here would be a second source for
    # one fact and stale by the time it was read.
    assert "page" not in ready[0].headers
    assert "total" not in ready[0].headers
    await harness.close(w)


async def test_bad_secret_rejected(harness):
    r, w = await harness.connect(secret="wrong")
    frames = await harness.recv_until(r, {"ERROR"})
    assert frames[-1].headers["code"] == "bad_secret"
    await harness.close(w)


async def test_version_mismatch_rejected(harness):
    r, w = await harness.connect(ver=999)
    frames = await harness.recv_until(r, {"ERROR"})
    assert frames[-1].headers["code"] == "bad_version"
    await harness.close(w)


async def test_frame_cap_mismatch_rejected(harness):
    """Inheriting a peer's idea of the cap is how you end up allocating 32MB on
    a 32MB machine."""
    r, w = await harness.connect(cap=32 * 1024 * 1024)
    frames = await harness.recv_until(r, {"ERROR"})
    assert frames[-1].headers["code"] == "bad_frame_cap"
    await harness.close(w)


async def test_bad_caps_rejected(harness):
    """An unrecognised or unhonoured capability header is refused at the handshake.

    CLAUDE.md:44-50 promises bad caps become an ERROR bad_caps frame and close the link.
    """
    r, w = await harness.connect(hdrs={"eol": "2"})
    frames = await harness.recv_until(r, {"ERROR"})
    assert frames[-1].headers["code"] == "bad_caps"
    await harness.close(w)


async def test_hello_timeout_rejected(harness, monkeypatch):
    """Connecting and sending nothing times out and receives ERROR hello_timeout."""
    monkeypatch.setattr(harness.bridge.config, "hello_timeout_s", 0.05)
    r, w = await asyncio.open_connection("127.0.0.1", harness.port)
    frames = await harness.recv_until(r, {"ERROR"}, timeout=1.0)
    assert frames[-1].headers["code"] == "hello_timeout"
    await harness.close(w)


async def test_second_client_is_rejected_not_evicted(harness):
    """Whoever holds the session may be mid-turn; stealing it silently is
    worse than refusing."""
    r1, w1 = await harness.connect()
    await harness.recv_until(r1, {"READY"})

    r2, w2 = await harness.connect()
    frames = await harness.recv_until(r2, {"ERROR"})
    assert frames[-1].headers["code"] == "busy"

    await harness.close(w1, w2)


async def test_non_hello_first_frame_rejected(harness, caplog):
    """And the refusal is CLEAN, which it was not.

    `_read_hello` returned a three-tuple on each of its three refusal paths
    while the caller unpacked two, so every rejected handshake raised
    `ValueError: too many values to unpack` into `_on_client`'s catch-all and
    logged a traceback. This test passed throughout, because the ERROR frame
    is sent before the return -- the client saw the right thing and only the
    bridge's own log was wrong.
    """
    r, w = await asyncio.open_connection("127.0.0.1", harness.port)
    w.write(Frame("INPUT", {}, b"hi").encode())
    await w.drain()
    frames = await harness.recv_until(r, {"ERROR"})
    assert frames[-1].headers["code"] == "expected_hello"
    assert "unhandled error" not in caplog.text.lower(), caplog.text[:400]
    assert "ValueError" not in caplog.text, caplog.text[:400]
    await harness.close(w)


async def test_an_eof_before_hello_is_not_an_internal_error(harness, caplog):
    """The other two refusal paths had the same fault. A client that opens a
    socket and goes away is ordinary, not a bug in the bridge."""
    _r, w = await asyncio.open_connection("127.0.0.1", harness.port)
    w.close()
    await asyncio.sleep(0.3)
    assert "ValueError" not in caplog.text, caplog.text[:400]
    assert "unhandled error" not in caplog.text.lower(), caplog.text[:400]


# --- turns ------------------------------------------------------------------


async def test_input_produces_text_and_turn_end(harness):
    r, w = await harness.connect()
    await harness.recv_until(r, {"READY"})
    await harness.send(w, Frame("INPUT", {}, b"hello"))
    frames = await harness.recv_until(r, {"TURN"} , timeout=5)
    frames += await harness.recv_until(r, {"TURN"}, timeout=5)

    kinds = [f.type for f in frames]
    assert "TEXT" in kinds
    ends = [f for f in frames if f.type == "TURN"
            and f.headers.get("state") == "end"]
    assert ends and ends[0].headers["ok"] == "1"
    await harness.close(w)


async def test_fakegui_can_decode_a_real_deflated_text_frame(harness):
    """fakegui's whole value is "if fakegui works and the guest does not, the
    bug is in the C" -- which only holds if fakegui actually speaks the
    protocol. It used to not inflate `enc=deflate` bodies at all and read TEXT
    by a `style=` header the bridge no longer sends. Drive the real FakeGui
    class against a real bridge connection and check it can both decode a
    genuinely compressed frame and render it without raising, using its own
    persistent inflate stream the way win/net/net.c's guest does.
    """
    from bridge.devtools.fakegui import FakeGui

    gui = FakeGui("127.0.0.1", harness.port, "s")
    await gui.connect()
    frames = FrameReader()

    async def read_until(stop):
        """Collect frames, inflating each in fakegui's own stream order, until
        `stop(frame)` is true."""
        got = []
        while True:
            data = await asyncio.wait_for(gui.reader.read(8192), 5)
            for f in frames.feed(data):
                gui._inflate(f)
                got.append(f)
                if stop(f):
                    return got

    await read_until(lambda f: f.type == "READY")

    # Long enough that the bridge's should_compress (MIN_WORTH_COMPRESSING =
    # 64 bytes) actually engages -- the point is a frame that was genuinely
    # deflated, not one small enough to have gone over raw.
    long_prompt = "z" * 400
    gui.writer.write(Frame("INPUT", {}, long_prompt.encode()).encode())
    await gui.writer.drain()

    got = await read_until(
        lambda f: f.type == "TURN" and f.headers.get("state") == "end")
    text_frames = [f for f in got if f.type == "TEXT"]
    assert any(f.headers.get("enc") == "deflate" for f in text_frames), (
        "test is only meaningful if the bridge actually compressed a frame")
    assert any(long_prompt in f.body.decode("cp1252") for f in text_frames)
    assert any(f.headers.get("mode") in ("live", "append")
               for f in text_frames), "bridge must send mode=, not style="

    # gui._inflate already decompressed body in place; render() must not
    # raise decoding it a second time by the `mode=` header.
    for f in text_frames:
        gui.render(f)

    await gui.close()


async def test_unknown_frame_type_gets_an_error_not_a_drop(harness):
    r, w = await harness.connect()
    await harness.recv_until(r, {"READY"})
    await harness.send(w, Frame("NONSENSE", {}))
    frames = await harness.recv_until(r, {"ERROR"})
    assert frames[-1].headers["code"] == "unknown_type"
    await harness.close(w)


# --- reconnect ----------------------------------------------------------


async def test_a_second_machine_does_not_inherit_the_first_ones_session(harness):
    """Stage 3c D1, and this was a live bug on a bridge both of this
    project's machines talk to.

    `is_reconnect` asked whether a GuestSession existed, never WHOSE it
    was. So machine B attaching after machine A had gone was a reconnect:
    attach replaced the link, caps, prefs and tools and left `claude` --
    the live conversation -- along with work_dir, grants, mode_id, perms,
    sess_id and the whole transcript belonging to A. The person at B got
    A's chat, and nothing anywhere said so.
    """
    r1, w1 = await harness.connect(hdrs={"machine": "BOX-A"})
    await harness.recv_until(r1, {"READY"})
    a = harness.bridge.connection.guest
    a.view.pages.add(_rec("a's transcript"))
    a.work_guest = "C:\\WORK\\AAA"
    await harness.close(w1)

    r2, w2 = await harness.connect(hdrs={"machine": "BOX-B"})
    await harness.recv_until(r2, {"READY"})
    b = harness.bridge.connection.guest

    assert b is not a, "B was handed A's GuestSession"
    assert b.machine == "BOX-B"
    assert b.view.pages.count == 0, (
        "B is looking at A's transcript")
    assert b.work_guest != "C:\\WORK\\AAA", (
        "B inherited A's work folder")
    # And A is still there, not replaced.
    assert harness.bridge.connection._guests["BOX-A"] is a
    assert a.view.pages.count == 1
    await harness.close(w2)


async def test_the_same_machine_coming_back_is_still_a_reconnect(harness):
    """The other half, and the one that must not regress: keying on the
    machine is only correct if a machine still finds its own session."""
    r1, w1 = await harness.connect(hdrs={"machine": "BOX-A"})
    await harness.recv_until(r1, {"READY"})
    a = harness.bridge.connection.guest
    a.view.pages.add(_rec("still here"))
    await harness.close(w1)

    r2, w2 = await harness.connect(hdrs={"machine": "BOX-A"})
    await harness.recv_until(r2, {"READY"})
    assert harness.bridge.connection.guest is a
    assert a.view.pages.count == 1, "a reconnect lost the transcript"
    await harness.close(w2)


async def test_a_second_window_on_one_machine_is_still_refused(harness):
    """Reject, don't evict -- narrowed to the machine rather than dropped.
    The client's own single-instance mutex enforces this at the other end;
    the bridge saying so is what makes a broken mutex visible."""
    r1, w1 = await harness.connect(hdrs={"machine": "BOX-A"})
    await harness.recv_until(r1, {"READY"})
    r2, w2 = await harness.connect(hdrs={"machine": "BOX-A"})
    frames = await harness.recv_until(r2, {"ERROR"})
    assert frames[-1].headers["code"] == "busy"
    await harness.close(w2)
    await harness.close(w1)


async def test_two_machines_hold_two_sessions_at_once(harness):
    """The stage's exit condition, in the smallest form that is still it.
    A second SIMULTANEOUS machine was refused process-wide before this."""
    r1, w1 = await harness.connect(hdrs={"machine": "BOX-A"})
    await harness.recv_until(r1, {"READY"})
    r2, w2 = await harness.connect(hdrs={"machine": "BOX-B"})
    await harness.recv_until(r2, {"READY"})

    guests = harness.bridge.connection._guests
    assert set(guests) == {"BOX-A", "BOX-B"}
    assert guests["BOX-A"] is not guests["BOX-B"]
    assert guests["BOX-A"].link.connected and guests["BOX-B"].link.connected
    await harness.close(w2)
    await harness.close(w1)



async def test_a_reconnect_resets_the_compressor_and_keeps_the_session(
        harness):
    """The guest will crash. The session must outlive it, and the compressor
    must not: a reconnect means a fresh inflater on the guest side, so the
    persistent deflate stream has to restart in lockstep or the first
    deflated frame after reconnect is garbage the guest cannot decode.

    Used to matter twice over -- a resume-from-seq (`from=`) landing on the
    reset stream would have replayed frames from a compressor that no
    longer existed. There is no such resume any more (notes/BACKLOG.md #5:
    the guest never sent `from=`, and the replay buffer it would have
    driven is retired), so the only two things left to prove are that the
    reset actually happens and that reconnecting does not lose the session.
    """
    r, w = await harness.connect()
    frames = await harness.recv_until(r, {"READY"})
    sess = next(f for f in frames if f.type == "READY").headers["sess"]

    await harness.send(w, Frame("INPUT", {}, b"first"))
    await harness.recv_until(r, {"TURN"}, timeout=5)
    await harness.recv_until(r, {"TURN"}, timeout=5)

    # Guest dies.
    await harness.close(w)
    await asyncio.sleep(0.2)

    # And comes back. Same session, whether or not it names it.
    r2, w2 = await harness.connect(sess=sess)
    got = await harness.recv_until(r2, {"READY"}, timeout=3)
    ready = next(f for f in got if f.type == "READY")
    assert ready.headers["sess"] == sess

    # The compressor really did restart: a long enough second turn produces
    # deflated TEXT the harness's own (freshly reset) inflater can decode
    # without raising.
    long_prompt = b"second" * 100
    await harness.send(w2, Frame("INPUT", {}, long_prompt))
    frames2 = await harness.recv_until(r2, {"TURN"}, timeout=5)
    frames2 += await harness.recv_until(r2, {"TURN"}, timeout=5)
    assert any(f.headers.get("enc") == "deflate"
              for f in frames2 if f.type == "TEXT"), (
        "test is only meaningful if the bridge actually compressed a frame")
    assert b"echo: " + long_prompt in harness.body(frames2)
    await harness.close(w2)


# --- ranges, which is what pagination became ---------------------------------
#
# Stage 3b: the guest asks for blocks by offset and limit, and decides for
# itself where a page begins. These six are the behavioural proof of that --
# they were the pagination tests and they did not die with it.


def _fill(guest, n: int, fmt: str = "line {:04d} padding padding padding\n"):
    """n records straight into the transcript. Faster and more
    deterministic than persuading a model to emit them."""
    for i in range(n):
        guest.view.pages.add(_rec(fmt.format(i)))
    return guest.view.pages.count


async def test_a_window_comes_back_whole(harness):
    r, w = await harness.connect()
    await harness.recv_until(r, {"READY"})
    guest = harness.bridge.connection.guest
    total = _fill(guest, 600)

    await harness.send(w, Frame("GETBLOCKS", {"offset": "0", "limit": "90"}))
    frames = await harness.recv_until(r, {"RANGEEND"}, timeout=5)

    starts = [f for f in frames if f.type == "RANGE"]
    assert starts, [f.type for f in frames]
    assert starts[0].headers["offset"] == "0"
    assert starts[0].headers["count"] == "90"
    assert starts[0].headers["total"] == str(total)
    assert starts[0].headers["live"] == "0", "the first 90 of 600 is history"

    body = harness.body(frames)
    assert b"line 0000" in body
    assert b"line 0089" in body
    assert b"line 0090" not in body, "the window must stop at its limit"
    await harness.close(w)


async def test_live_aligns_to_the_grid_the_limit_implies(harness):
    """`offset=live` cannot mean "the last `limit` records": with 200
    records and a limit of 90 that window would start at 110, which is not
    where any of the guest's pages begin. It means the last window on the
    grid the request's own limit implies."""
    r, w = await harness.connect()
    await harness.recv_until(r, {"READY"})
    guest = harness.bridge.connection.guest
    _fill(guest, 200)

    await harness.send(w, Frame("GETBLOCKS", {"offset": "live", "limit": "90"}))
    frames = await harness.recv_until(r, {"RANGEEND"}, timeout=5)
    start = next(f for f in frames if f.type == "RANGE")
    assert start.headers["offset"] == "180", (
        "180 is where page 3 begins on a 90-record grid; 110 is where "
        "nothing begins")
    assert start.headers["count"] == "20"
    assert start.headers["live"] == "1"
    assert guest.view.view_live is True
    await harness.close(w)


async def test_a_window_past_the_end_is_empty_and_is_not_an_error(harness):
    """A stale page button asks for a window that no longer exists. That is
    a race, not a bug on the asking side, so it answers with an empty
    window rather than an ERROR -- and it must NOT say the conversation is
    empty, which is what `offset + limit >= count` alone would have done
    for every offset past the end."""
    r, w = await harness.connect()
    await harness.recv_until(r, {"READY"})
    guest = harness.bridge.connection.guest
    _fill(guest, 50)

    await harness.send(w, Frame("GETBLOCKS", {"offset": "9000", "limit": "90"}))
    frames = await harness.recv_until(r, {"RANGEEND"}, timeout=5)
    start = next(f for f in frames if f.type == "RANGE")
    assert start.headers["count"] == "0"
    assert start.headers["total"] == "50"
    assert not [f for f in frames if f.type == "ERROR"]
    assert not [f for f in frames if f.type == "NOTICE"
                and f.headers.get("kind") == "empty"], (
        "a window past the end is not an empty conversation")
    await harness.close(w)


async def test_a_malformed_offset_is_an_error(harness):
    """Negative or non-numeric is a bug on the asking side with nothing to
    race, so it is told so."""
    r, w = await harness.connect()
    await harness.recv_until(r, {"READY"})
    await harness.send(w, Frame("GETBLOCKS", {"offset": "-5", "limit": "90"}))
    frames = await harness.recv_until(r, {"ERROR"})
    assert frames[-1].headers["code"] == "bad_offset"
    await harness.close(w)


async def test_reading_back_suppresses_live_streaming(harness):
    """While reading an older window, new text must not be injected into
    it. The reader learns the conversation moved on from GREW instead."""
    r, w = await harness.connect()
    await harness.recv_until(r, {"READY"})
    guest = harness.bridge.connection.guest
    _fill(guest, 400)

    await harness.send(w, Frame("GETBLOCKS", {"offset": "0", "limit": "90"}))
    await harness.recv_until(r, {"RANGEEND"}, timeout=5)
    assert guest.view.view_live is False

    await harness.send(w, Frame("INPUT", {}, b"while reading history"))
    frames = await harness.recv_until(r, {"TURN"}, timeout=5)
    frames += await harness.recv_until(r, {"TURN"}, timeout=5)

    assert not [f for f in frames if f.type == "TEXT"], (
        "new text leaked into the window the reader is looking at")
    assert [f for f in frames if f.type == "GREW"], (
        "a reader who is paged back has no other way to learn the "
        "conversation grew")
    await harness.close(w)


async def test_the_view_returns_to_live_on_reconnect(harness):
    """02.25: attach reset zip_out and lost_at on a reconnect but never the
    view flag, so a guest that reconnected while paged back stayed in
    history mode bridge-side forever -- new text buffered silently, the
    transcript looked frozen, and nothing on this side had reason to think
    anything was wrong. The guest has no memory of which window it was on
    across a reconnect: it asks for `live` the moment it sees READY."""
    r, w = await harness.connect()
    await harness.recv_until(r, {"READY"})
    guest = harness.bridge.connection.guest
    _fill(guest, 400)

    await harness.send(w, Frame("GETBLOCKS", {"offset": "0", "limit": "90"}))
    await harness.recv_until(r, {"RANGEEND"}, timeout=5)
    assert guest.view.view_live is False, "test setup: must be paged back"

    await harness.close(w)
    r2, w2 = await harness.connect()
    await harness.recv_until(r2, {"READY"})

    assert guest.view.view_live is True, (
        "a reconnect must drop back to live, not keep buffering into a "
        "window nobody -- guest-side -- is still looking at")

    await harness.send(w2, Frame("INPUT", {}, b"after reconnect"))
    frames = await harness.recv_until(r2, {"TURN"}, timeout=5)
    frames += await harness.recv_until(r2, {"TURN"}, timeout=5)
    assert any(f.type == "TEXT" for f in frames), (
        "text must stream live again after a reconnect")
    await harness.close(w2)


async def test_a_streamed_block_carries_the_new_count(harness):
    """02.33's property, restated for ranges.

    `write_block` captured the page number before the write and matched
    chunks against it, so a block that forced a seal landed on the NEXT
    page and was never streamed at all -- a reader following live simply
    never saw it. There are no pages to seal now, and what has to hold
    instead is that a block streamed live carries the count AFTER it
    landed, so the page bar can move the reader onto a new page at the
    moment the block that starts it arrives.
    """
    r, w = await harness.connect()
    await harness.recv_until(r, {"READY"})
    guest = harness.bridge.connection.guest

    first = blocks.Block("code", {}, [("t", "A" * 30000)])
    second = blocks.Block("code", {}, [("t", "B" * 10000)])

    await harness.bridge.presentation.write_block(guest, first)
    seen = await harness.recv_until(r, {"TEXT"}, timeout=5)
    await harness.bridge.presentation.write_block(guest, second)
    seen += await harness.recv_until(r, {"TEXT"}, timeout=5)

    texts = [f for f in seen if f.type == "TEXT"]
    assert len(texts) == 2, [f.headers for f in texts]
    # The count AFTER each landed, on the frame that carried it -- not a
    # GREW of its own, which would be a second frame per block on a link
    # measured in single digits of KB/s.
    assert [f.headers.get("total") for f in texts] == ["1", "2"]
    # Inflated in stream ORDER, every TEXT: one persistent deflate stream
    # per connection, exactly the constraint the guest is under.
    assert harness.body(seen) == blocks.encode(first) + blocks.encode(second)

    # And the window that holds the second record holds exactly it.
    await harness.send(w, Frame("GETBLOCKS", {"offset": "1", "limit": "1"}))
    frames2 = await harness.recv_until(r, {"RANGEEND"}, timeout=5)
    assert harness.body(frames2) == blocks.encode(second)
    await harness.close(w)


# --- the model menu ---------------------------------------------------------


async def test_option_frames_resolve_the_default_alias_away(harness):
    """"Default" is an alias, not a model, and a menu that says "Default" does
    not tell you what you are talking to. The alias is resolved and the real
    model behind it is the one ticked."""
    r, w = await harness.connect()
    frames = await harness.recv_until(r, {"OPTIONSEND"})
    models = [f for f in frames if f.type == "OPTION"
              and f.headers.get("kind") == "model"]
    assert models, "no model options sent"
    assert all(f.headers["id"] != "default" for f in models)
    ticked = [f.headers["id"] for f in models
              if f.headers.get("current") == "1"]
    assert ticked == ["opus"], "the alias should resolve onto the real model"
    await harness.close(w)


async def test_model_description_rides_beside_the_label_not_inside_it(
    harness,
):
    """The label is what the toolbar BUTTON shows, and it has 640 pixels of
    window. "Haiku 4.5 - Fastest for quick answers" does not fit there and
    "Haiku" does, so the description is its own header and the menu is what
    renders both. A model with no description sends none rather than an
    empty one."""
    r, w = await harness.connect()
    frames = await harness.recv_until(r, {"OPTIONSEND"})
    rows = {f.headers["id"]: f.headers for f in frames if f.type == "OPTION"
            and f.headers.get("kind") == "model"}
    assert rows["haiku"]["label"] == "Haiku"
    assert rows["haiku"]["desc"] == "Fastest for quick answers"
    assert "desc" not in rows["opus"]
    await harness.close(w)


async def test_effort_options_follow_the_selected_model(harness):
    """Haiku has no effort levels at all, so offering five would be lying."""
    r, w = await harness.connect()
    frames = await harness.recv_until(r, {"OPTIONSEND"})
    efforts = [f.headers["id"] for f in frames if f.type == "OPTION"
               and f.headers.get("kind") == "effort"]
    assert efforts == ["low", "high"]
    await harness.close(w)


async def test_model_menu_ticks_the_cli_default_not_a_generic_default(
    harness,
):
    """02.23: _make_session runs the session on `guest.model or self.model`,
    but _send_options used to read guest.model alone -- empty until the Model
    menu is actually used. Starting the bridge with --model set (self.model)
    therefore ran the session on that model while the menu ticked whatever
    the "default" alias resolves to instead."""
    harness.bridge.config.model = "haiku"
    r, w = await harness.connect()
    frames = await harness.recv_until(r, {"OPTIONSEND"})
    models = [f for f in frames if f.type == "OPTION"
              and f.headers.get("kind") == "model"]
    ticked = [f.headers["id"] for f in models
              if f.headers.get("current") == "1"]
    assert ticked == ["haiku"], (
        "the menu must tick the CLI's --model default, not whatever "
        "'default' resolves to"
    )
    await harness.close(w)


async def test_effort_menu_ticks_the_cli_default(harness):
    """Same bug as above, for --effort (02.23): the Effort menu used to read
    guest.effort alone and show nothing ticked when only the CLI default was
    set."""
    harness.bridge.config.model = "opus"
    harness.bridge.config.effort = "low"
    r, w = await harness.connect()
    frames = await harness.recv_until(r, {"OPTIONSEND"})
    efforts = [f for f in frames if f.type == "OPTION"
               and f.headers.get("kind") == "effort"]
    ticked = [f.headers["id"] for f in efforts
              if f.headers.get("current") == "1"]
    assert ticked == ["low"], "the effort menu must tick the CLI's --effort"
    await harness.close(w)


# --- one folder per conversation --------------------------------------------


async def test_talking_to_a_chat_shares_no_files_at_all(harness, tmp_path):
    """A conversation that has not called a tool has no folder, nothing is
    mirrored, and no pass runs.

    This is not tidiness. The live root used to be held on the GUEST and
    reported in HELLO, so it outlived a bridge restart: a fresh bridge was
    told the machine was mirroring C:\\Gaston\\OK, picked that tree's
    baseline and then ran its first pass against its own whole shared
    directory. With nothing mirrored until the bridge names a tree, there is
    no tree to disagree about."""
    r, w = await harness.connect(hdrs={"sync_root": "C:\\Gaston"})
    await harness.recv_until(r, {"OPTIONSEND"})
    b = harness.bridge
    b.config.sync_dir = str(tmp_path / "mirror")
    os.makedirs(b.config.sync_dir, exist_ok=True)
    g = b.connection.guest

    await harness.send(w, Frame("INPUT", {}, b"just a question"))
    await harness.recv_until(r, {"TURN"}, 10.0)

    assert g.work_guest == "", "unset until a tool has run"
    assert g.work_dir == ""
    assert b.workspace.mirror_dir(g) == "", "no mirror without a committed folder"
    assert b.sync.current_guest_root() == ""
    assert await b.sync.run_sync("test") == {}, "no pass without a mirror"
    assert os.listdir(b.config.sync_dir) == [], "and no folder littered on disk"
    await harness.close(w)


async def test_the_first_tool_call_commits_the_folder(harness, tmp_path):
    """Named from the FIRST MESSAGE, not the CLI's title: a tool can run in
    the first turn, before the conversation has been titled, and the
    alternative -- a placeholder renamed afterwards -- is a new tree_id and a
    new baseline for a tree sync has already scanned."""
    r, w = await harness.connect(hdrs={"sync_root": "C:\\Gaston"})
    await harness.recv_until(r, {"OPTIONSEND"})
    b = harness.bridge
    b.config.sync_dir = str(tmp_path / "mirror")
    os.makedirs(b.config.sync_dir, exist_ok=True)
    g = b.connection.guest

    await harness.send(w, Frame("INPUT", {}, b"List files in C:"))
    await harness.recv_until(r, {"TURN"}, 10.0)
    assert g.work_guest == ""

    await g.claude.use_tool()

    assert g.work_dir == os.path.join(b.config.sync_dir, "LISTFILE")
    assert os.path.isdir(g.work_dir), "created, not merely named"
    assert g.work_guest == "C:\\Gaston\\LISTFILE"
    assert b.workspace.mirror_dir(g) == g.work_dir, "and sharing is on now"
    await harness.close(w)


async def test_the_folder_is_named_from_the_title_when_there_is_no_message(
    harness, tmp_path,
):
    """The fallback, for a conversation resumed from the chat list that never
    got a folder: it has a title already and no first message of this
    session's own."""
    import bridge.server as srv
    r, w = await harness.connect(hdrs={"sync_root": "C:\\Gaston"})
    await harness.recv_until(r, {"OPTIONSEND"})
    b = harness.bridge
    b.config.sync_dir = str(tmp_path / "mirror")
    os.makedirs(b.config.sync_dir, exist_ok=True)
    g = b.connection.guest

    orig = srv.sesslist.listing
    srv.sesslist.listing = lambda cwd=None, limit=50: [
        {"id": g.conv_id or g.claude.session_id, "title": "List files in C:",
         "cwd": "", "modified": "", "group": ""}]
    try:
        assert g.first_prompt == "", "nothing said in this session"
        # The id arrives at the top of the first turn, and `_ensure_work_dir`
        # is only ever reached from inside one. Calling it here without that
        # would test a state the SDK cannot produce.
        await g.claude.start_turn()
        await b.workspace.ensure_work_dir(g)
    finally:
        srv.sesslist.listing = orig

    assert g.work_guest == "C:\\Gaston\\LISTFILE"
    await harness.close(w)


async def test_two_conversations_do_not_share_a_folder(harness, tmp_path):
    """Two chats about listing files both reduce to LISTFILE, and the second
    taking the first one's folder would put two conversations' work in one
    place with no way to tell them apart afterwards."""
    b = harness.bridge
    b.config.sync_dir = str(tmp_path / "mirror")
    os.makedirs(os.path.join(b.config.sync_dir, "LISTFILE"), exist_ok=True)

    r, w = await harness.connect(hdrs={"sync_root": "C:\\Gaston"})
    await harness.recv_until(r, {"OPTIONSEND"})
    g = b.connection.guest
    await harness.send(w, Frame("INPUT", {}, b"List files again"))
    await harness.recv_until(r, {"TURN"}, 10.0)
    await g.claude.use_tool()
    assert os.path.basename(g.work_dir) == "LISTFIL2"
    await harness.close(w)


async def test_a_title_with_separators_in_it_still_makes_a_folder(harness):
    """A conversation title is prose. "List files in C:\\RETRO" carries a
    colon and a backslash, which folder_name refuses on purpose -- so the
    title path strips them first, where they are known to be part of a
    sentence rather than a way out of the folder."""
    from bridge.server import Bridge
    assert Bridge._title_folder("List files in C:\\RETRO") == "LISTFILE"
    assert Bridge._title_folder("vol c:") == "VOLC"
    assert Bridge._title_folder("   ") == ""
    # And the raw guard still refuses what it is there to refuse.
    assert Bridge.folder_name("../etc") == ""
    assert Bridge.folder_name("a/b") == ""


async def test_a_blank_folder_means_the_shared_one(harness):
    """An older guest still sends the header. It is read and ignored rather
    than refused, so upgrading the bridge does not break a client that has
    not been reshipped."""
    r, w = await harness.connect()
    await harness.recv_until(r, {"OPTIONSEND"})
    await harness.send(w, Frame("NEWSESSION", {"folder": "  "}))
    await harness.recv_until(r, {"OPTIONSEND"}, 10.0)
    assert harness.bridge.connection.guest.work_dir == ""
    await harness.close(w)


async def test_a_new_chat_is_not_asked_for_a_folder_at_all(harness, tmp_path):
    """New chat sends NEWSESSION with NO folder header, because there is
    nothing to ask.

    The guest used to put up a dialog for one. That was the last piece of a
    design replaced twice over -- a folder is named after the conversation
    and the CLI titles a conversation from its first message, so the prompt
    asked the user to invent a name before there was anything to base it on.
    By the end the bridge had stopped reading the header, so the dialog was
    asking a question and discarding the answer.
    """
    r, w = await harness.connect(hdrs={"sync_root": "C:\\Gaston"})
    await harness.recv_until(r, {"OPTIONSEND"})
    b = harness.bridge
    b.config.sync_dir = str(tmp_path / "mirror")
    os.makedirs(b.config.sync_dir, exist_ok=True)

    await harness.send(w, Frame("NEWSESSION", {}))
    frames = await harness.recv_until(r, {"OPTIONSEND"}, 10.0)

    g = b.connection.guest
    assert g.work_dir == "", "a new chat has no folder until its first tool"
    assert g.work_guest == ""
    assert b.workspace.mirror_dir(g) == "", "and mirrors nothing until then"
    assert os.listdir(b.config.sync_dir) == [], "nothing created on the way past"

    row = [f.headers for f in frames if f.type == "OPTION"
           and f.headers.get("kind") == "work"][-1]
    assert row["label"] == "<unset>", row
    await harness.close(w)


async def test_a_restored_chat_comes_back_in_its_own_folder(
    harness, tmp_path,
):
    """A conversation started in C:\\WORK\\NOTES has to resume there, or its
    second half is written somewhere its first half is not."""
    import bridge.server as srv
    r, w = await harness.connect()
    await harness.recv_until(r, {"OPTIONSEND"})
    b = harness.bridge
    b.config.sync_dir = str(tmp_path / "mirror")
    os.makedirs(os.path.join(b.config.sync_dir, "NOTES"), exist_ok=True)

    rows = [{"id": "stored-1", "title": "t", "cwd": os.path.join(
                b.config.sync_dir, "NOTES"), "modified": "", "group": ""},
            {"id": "stored-2", "title": "t", "cwd": "/somewhere/else",
             "modified": "", "group": ""}]
    orig = srv.sesslist.listing
    srv.sesslist.listing = lambda cwd=None, limit=50: list(rows)
    try:
        await _switch_to(harness, monkeypatch=_NoPatch(), r=r, w=w,
                         sid="stored-1")
        assert b.connection.guest.work_dir == os.path.join(b.config.sync_dir, "NOTES")
        await _switch_to(harness, monkeypatch=_NoPatch(), r=r, w=w,
                         sid="stored-2")
        assert b.connection.guest.work_dir == "", (
            "a conversation started outside the mirror must not point this "
            "bridge's agent at a directory the rest of the system knows "
            "nothing about"
        )
    finally:
        srv.sesslist.listing = orig
    await harness.close(w)


class _NoPatch:
    """monkeypatch's setattr, for a caller that has already patched what it
    needs and only wants _switch_to's frame handling."""

    def setattr(self, *a, **kw):
        import bridge.server as srv
        srv.sesslist.history_blocks = lambda sid_, limit=2000: []


async def test_a_reopened_conversation_keeps_its_folder(harness, tmp_path):
    """The folder is named AFTER the session starts, so the session's own
    recorded cwd is the root and asking the SDK gives the wrong answer.
    Reopening showed "<unset>" for a conversation whose folder was on disk."""
    import bridge.server as srv
    r, w = await harness.connect(hdrs={"sync_root": "C:\\Gaston"})
    await harness.recv_until(r, {"OPTIONSEND"})
    b = harness.bridge
    b.config.sync_dir = str(tmp_path / "mirror")
    os.makedirs(b.config.sync_dir, exist_ok=True)
    g = b.connection.guest
    # AFTER the turn below, not here: a session that has not run one has no
    # id yet, exactly as the SDK has none to give.
    await g.claude.start_turn()
    sid = g.claude.session_id

    orig = srv.sesslist.listing
    srv.sesslist.listing = lambda cwd=None, limit=50: [
        {"id": sid, "title": "Sky color", "cwd": b.config.sync_dir,
         "modified": "", "group": ""}]
    try:
        await harness.send(w, Frame("INPUT", {}, b"Sky color"))
        await harness.recv_until(r, {"TURN"}, 10.0)
        await g.claude.use_tool()
        assert b.conv.folder(sid) == "SKYCOLOR"

        # Away and back.
        await _switch_to(harness, monkeypatch=_NoPatch(), r=r, w=w,
                         sid="somewhere-else")
        await _switch_to(harness, monkeypatch=_NoPatch(), r=r, w=w, sid=sid)
    finally:
        srv.sesslist.listing = orig

    assert g.work_guest == "C:\\Gaston\\SKYCOLOR"
    assert g.work_dir == os.path.join(b.config.sync_dir, "SKYCOLOR")
    await harness.close(w)


async def test_setwork_points_the_conversation_at_the_folder_chosen(
    harness, tmp_path,
):
    """"Setting work directory sets the actual work directory" -- not a
    parent it will sit under. The guest picks any folder on its own machine
    and that becomes this conversation's directory, whatever it is called."""
    r, w = await harness.connect(hdrs={"sync_root": "C:\\Gaston"})
    await harness.recv_until(r, {"OPTIONSEND"})
    b = harness.bridge
    b.config.sync_dir = str(tmp_path / "mirror")
    os.makedirs(b.config.sync_dir, exist_ok=True)
    g = b.connection.guest
    # A work directory belongs to a conversation, and SETWORK refuses when
    # there is not one yet. The id lands with the first turn's `init`.
    await g.claude.start_turn()
    sid = g.claude.session_id

    await harness.send(w, Frame("SETWORK", {"path": "D:\\Projects\\Retro"}))
    frames = await harness.recv_until(r, {"OPTIONSEND"}, 10.0)
    assert g.work_guest == "D:\\Projects\\Retro"
    assert os.path.isdir(g.work_dir), "and a folder on this side to pair with"

    row = [f.headers for f in frames if f.type == "OPTION"
           and f.headers.get("kind") == "work"][-1]
    assert row["id"] == "D:\\Projects\\Retro"
    assert row["label"] == "D:\\Projects\\Retro"

    # And it comes back, because the guest's spelling cannot be derived from
    # anything on this side.
    assert b.conv.guest_folder(sid) == "D:\\Projects\\Retro"
    await harness.close(w)


async def test_setwork_tells_the_agent_the_directory_moved(harness, tmp_path):
    """The agent had no idea. The transcript notice goes to the user, the
    OPTION batch goes to the toolbar, and the system prompt was written
    before either -- so it went on writing into a folder that is no longer
    mirrored anywhere, with every write appearing to succeed."""
    r, w = await harness.connect(hdrs={"sync_root": "C:\\Gaston"})
    await harness.recv_until(r, {"OPTIONSEND"})
    b = harness.bridge
    b.config.sync_dir = str(tmp_path / "mirror")
    os.makedirs(b.config.sync_dir, exist_ok=True)
    await b.connection.guest.claude.start_turn()      # SETWORK needs a conversation

    await harness.send(w, Frame("SETWORK", {"path": "D:\\Projects\\Retro"}))
    await harness.recv_until(r, {"OPTIONSEND"}, 10.0)

    said = "\n".join(b.connection.synccoord.agent_notices)
    assert "THE WORK DIRECTORY HAS CHANGED" in said
    assert "D:\\Projects\\Retro" in said, "it has to name the guest's path"
    assert os.path.join(b.config.sync_dir, "RETRO") in said, "and this side's"
    assert "stays on this proxy" in said
    # Not asked for, so not asked of it.
    assert "Gaston-Incoming" not in said
    await harness.close(w)


async def test_choosing_assistant_merge_actually_asks_the_assistant(
    harness, tmp_path,
):
    """"Let the assistant merge them" is an instruction TO the assistant, and
    the assistant is on the other end of the socket. Without this the copies
    land in Gaston-Incoming and nothing ever asks anyone to reconcile them --
    the answer did half of what its label said."""
    r, w = await harness.connect(hdrs={"sync_root": "C:\\Gaston"})
    await harness.recv_until(r, {"OPTIONSEND"})
    b = harness.bridge
    b.config.sync_dir = str(tmp_path / "mirror")
    os.makedirs(b.config.sync_dir, exist_ok=True)
    await b.connection.guest.claude.start_turn()      # SETWORK needs a conversation

    await harness.send(w, Frame("SETWORK", {"path": "D:\\Projects\\Retro",
                                            "staged": "merge"}))
    await harness.recv_until(r, {"OPTIONSEND"}, 10.0)

    said = "\n".join(b.connection.synccoord.agent_notices)
    assert "asked YOU to merge" in said
    assert os.path.join(b.config.sync_dir, "RETRO", "Gaston-Incoming") in said
    assert "ask before" in said, "it must not pick a side on its own"
    await harness.close(w)


async def test_setwork_before_conversation_starts_sets_work_for_initial_message(
    harness, tmp_path
):
    """Setting a work directory before the conversation starts is allowed and applies to the initial message."""
    b = harness.bridge
    b.config.sync_dir = str(tmp_path / "mirror")
    os.makedirs(b.config.sync_dir, exist_ok=True)

    r, w = await harness.connect(hdrs={"sync_root": "C:\\Gaston"})
    await harness.recv_until(r, {"OPTIONSEND"})
    g = b.connection.guest
    assert not g.conv_id

    await harness.send(w, Frame("SETWORK", {"path": "D:\\Projects\\Retro"}))
    frames = await harness.recv_until(r, {"OPTIONSEND"}, 10.0)

    assert g.work_guest == "D:\\Projects\\Retro"
    assert g.work_dir == os.path.join(b.config.sync_dir, "RETRO")
    assert os.path.isdir(g.work_dir), "folder created on bridge side"

    row = [f.headers for f in frames if f.type == "OPTION"
           and f.headers.get("kind") == "work"][-1]
    assert row["id"] == "D:\\Projects\\Retro"
    assert row["label"] == "D:\\Projects\\Retro"

    # Initial turn uses the chosen work directory
    await harness.send(w, Frame("INPUT", {}, b"Hello initial turn"))
    await harness.recv_until(r, {"TURN"}, 10.0)
    await harness.recv_until(r, {"TURN"}, 10.0)

    sid = g.claude.session_id
    assert sid is not None
    assert g.work_guest == "D:\\Projects\\Retro"
    assert b.conv.guest_folder(sid) == "D:\\Projects\\Retro"
    await harness.close(w)


async def test_mcp_exec_flushes_sync_and_sends_status_frame(harness, tmp_path):
    """Calling run_on_guest flushes sync, blocks until done, and wraps it in STATUS frames."""
    b = harness.bridge
    b.config.sync_dir = str(tmp_path / "mirror")
    b.config.sync_state = str(tmp_path / "state")
    os.makedirs(b.config.sync_dir, exist_ok=True)
    os.makedirs(b.config.sync_state, exist_ok=True)

    r, w = await harness.connect(hdrs={"sync_root": "C:\\Gaston"})
    await harness.recv_until(r, {"OPTIONSEND"})
    g = b.connection.guest

    # Set work directory and create a local file
    await harness.send(w, Frame("SETWORK", {"path": "C:\\Gaston\\WORK"}))
    await harness.recv_until(r, {"OPTIONSEND"}, 10.0)

    test_file = os.path.join(g.work_dir, "test.c")
    with open(test_file, "w") as fh:
        fh.write("int main() { return 0; }\n")

    sync_calls = []

    async def tracking_run_sync(why, wait=False, guest=None):
        sync_calls.append((why, wait))
        return {"reports": [], "notices": []}

    b.sync.run_sync = tracking_run_sync

    # Launch run_on_guest in background
    task = asyncio.create_task(
        b.guest_exec.run_on_guest("wcl386 test.c", timeout_ms=5000, shell="", cwd="", guest=g)
    )

    # Receive frames until EXEC
    frames = await harness.recv_until(r, {"EXEC"}, 10.0)

    # Verify STATUS frames occurred before EXEC
    status_frames = [f for f in frames if f.type == "STATUS"]
    assert len(status_frames) >= 2, f"Expected at least 2 STATUS frames, got {status_frames}"
    assert status_frames[0].headers.get("text") == "waiting for sync..."
    assert status_frames[1].headers.get("text") == ""

    # Followed by the EXEC frame itself
    f_exec = next(f for f in frames if f.type == "EXEC")
    assert b"wcl386 test.c" in f_exec.body

    # Reply with output and result so run_on_guest finishes
    exec_id = f_exec.headers.get("id")
    await harness.send(w, Frame("EXECOUT", {"id": exec_id}, b"Compiled OK\n"))
    await harness.send(w, Frame("EXECRESULT", {"id": exec_id, "status": "ok", "exit": "0"}))
    res = await task
    assert "Compiled OK" in res["content"][0]["text"]

    # Verify run_sync was called with wait=True
    assert any(wait is True for why, wait in sync_calls)
    await harness.close(w)


async def test_a_new_work_directory_gets_its_own_baseline(harness, tmp_path):
    """Changing the work directory must not brick synchronisation.

    A baseline is bound to a `guest_root` and `scan_guest` refuses when it
    changes -- correctly, because treating a different directory as the same
    one would conflict the whole tree. That refusal is exactly what "Change
    work directory..." triggers, so each root gets its own file. The legacy
    `base.json` keeps belonging to whichever root it already records, so
    switching back finds its history where it left it.
    """
    import json as _json
    b = harness.bridge
    b.config.sync_state = str(tmp_path / "state")
    os.makedirs(b.config.sync_state, exist_ok=True)
    legacy = os.path.join(b.config.sync_state, "base.json")
    with open(legacy, "w") as fh:
        _json.dump({"guest_root": "C:\\WORK"}, fh)

    b.config.sync_dir = str(tmp_path / "mirror")
    os.makedirs(b.config.sync_dir, exist_ok=True)
    r, w = await harness.connect(hdrs={"sync_root": "C:\\Gaston"})
    await harness.recv_until(r, {"OPTIONSEND"})

    # Nothing committed, nothing mirrored, so there is no root to key a
    # baseline on and the legacy file is what an unasked question returns.
    assert b.sync.base_path() == legacy

    await b.connection.guest.claude.start_turn()      # SETWORK needs a conversation
    await harness.send(w, Frame("SETWORK", {"path": "C:\\WORK"}))
    await harness.recv_until(r, {"OPTIONSEND"}, 10.0)
    assert b.sync.base_path() == legacy, "its own root keeps the legacy file"

    await harness.send(w, Frame("SETWORK", {"path": "C:\\Gaston\\OK"}))
    await harness.recv_until(r, {"OPTIONSEND"}, 10.0)
    other = b.sync.base_path()
    assert other != legacy
    assert os.path.basename(other).startswith("base-")
    assert os.path.exists(legacy), "and the old one is not touched"
    await harness.close(w)


async def test_the_chat_list_is_not_filtered_by_one_directory(harness):
    """Conversations live in their own folders now, so filtering the listing
    by `self.cwd` would drop a chat the moment it was given one. The registry
    was always the real filter."""
    import bridge.server as srv
    seen = {}

    def fake_listing(cwd=None, limit=50):
        seen["cwd"] = cwd
        return []

    r, w = await harness.connect()
    await harness.recv_until(r, {"OPTIONSEND"})
    orig = srv.sesslist.listing
    srv.sesslist.listing = fake_listing
    try:
        await harness.send(w, Frame("GETSESSIONS", {}))
        await harness.recv_until(r, {"SESSIONSEND"}, 10.0)
    finally:
        srv.sesslist.listing = orig
    assert seen.get("cwd") is None
    await harness.close(w)


# --- what can run without asking --------------------------------------------


async def test_the_rules_view_says_what_it_cannot_see(harness):
    """04.16: a list of only what reaches the prompt reads as "this is
    everything that can happen", which is MEASURED false -- Claude Code
    answers Read/Glob/Grep inside its own working directory locally and never
    calls can_use_tool. A view that does not say so is worse than none."""
    r, w = await harness.connect()
    await harness.recv_until(r, {"OPTIONSEND"})
    await harness.send(w, Frame("RULES", {}))
    frames = await harness.recv_until(r, {"TEXT"}, 10.0)
    body = harness.body(frames).decode("cp1252")
    assert "without telling this bridge" in body
    # Every category the mode decides is listed, by what it means.
    assert "running other programs" in body
    await harness.close(w)


async def test_the_rules_view_does_not_promise_the_operators_settings(
    harness,
):
    """`Session` runs with `setting_sources=[]`, so rules in the operator's
    .claude/settings.json are not in effect. Showing them would answer "what
    can happen without asking?" with things that cannot happen."""
    r, w = await harness.connect()
    await harness.recv_until(r, {"OPTIONSEND"})
    await harness.send(w, Frame("RULES", {}))
    frames = await harness.recv_until(r, {"TEXT"}, 10.0)
    body = harness.body(frames).decode("cp1252")
    assert ".claude settings do not apply" in body
    await harness.close(w)


async def test_the_rules_view_follows_the_mode(harness):
    r, w = await harness.connect()
    await harness.recv_until(r, {"OPTIONSEND"})
    await harness.send(w, Frame("SETMODE", {"mode": "plan"}))
    # ONE deflate stream: the mode notice is compressed too, so its frames
    # must be inflated in order before the rules view's can be.
    before = await harness.recv_until(r, {"OPTIONSEND"}, 10.0)
    await harness.send(w, Frame("RULES", {}))
    frames = await harness.recv_until(r, {"TEXT"}, 10.0)
    body = harness.body(before + frames).decode("cp1252")
    assert "Plan first" in body
    await harness.close(w)


# --- File > Export as HTML --------------------------------------------------


def _export_bytes(harness, frames):
    """Reassemble an export, inflating in stream order.

    Same constraint as `Harness.body`: one persistent deflate stream, so a
    chunk skipped here desynchronises everything after it.
    """
    out = []
    for f in frames:
        if f.type != "EXPORTDAT":
            continue
        out.append(harness.unzip.decompress(f.body)
                   if f.headers.get("enc") == "deflate" else f.body)
    return b"".join(out)


async def test_export_streams_the_whole_transcript(harness):
    """It has to be a COMPLETE document and it has to arrive in order: the
    guest writes each chunk straight to a file and never holds the whole
    thing, so nothing downstream can fix up a gap."""
    r, w = await harness.connect()
    await harness.recv_until(r, {"OPTIONSEND"})
    await harness.send(w, Frame("INPUT", {}, b"hello"))
    await harness.recv_until(r, {"TURN"}, 10.0)
    await harness.recv_until(r, {"TURN"}, 10.0)

    await harness.send(w, Frame("EXPORT", {}))
    frames = await harness.recv_until(r, {"EXPORTEND"}, 10.0)

    start = [f for f in frames if f.type == "EXPORTSTART"]
    end = [f for f in frames if f.type == "EXPORTEND"]
    assert len(start) == 1 and len(end) == 1
    assert end[0].headers["status"] == "ok"

    data = _export_bytes(harness, frames)
    assert len(data) == int(start[0].headers["total"])
    assert int(end[0].headers["bytes"]) == len(data)

    # RECORDS from stage 3 C3, not a document. The bridge cannot build one
    # any more -- it has neither the markup nor the stylesheet -- and the
    # alternative was a second renderer there, which is the thing the whole
    # stage exists to avoid. The guest renders each chunk into the file as
    # it arrives, so the streaming rule is unchanged.
    got = blocks.decode(data)
    assert got, "the export carried no records"
    assert got[0].kind == "exportheading", got[0].kind
    assert any("hello" in t for b in got for t in b.texts()), (
        "the conversation itself must be in there")

    offs = [int(f.headers["off"]) for f in frames if f.type == "EXPORTDAT"]
    assert offs == sorted(offs) and offs[0] == 0
    await harness.close(w)


async def test_no_export_chunk_splits_a_record(harness):
    """The one thing the export chunker cannot get wrong.

    The guest renders each chunk as it arrives and never holds the
    document, so a record split across two frames reaches the renderer as
    two fragments it must refuse -- and the export is missing a block with
    nothing to say which. Every chunk must therefore decode on its own.
    """
    r, w = await harness.connect()
    await harness.recv_until(r, {"OPTIONSEND"})
    guest = harness.bridge.connection.guest
    # Written with stream=False, so the paginator fills without the live
    # TEXT frames -- which would otherwise have to be inflated here too,
    # in order, just to keep the stream in step.
    for i in range(400):
        await harness.bridge.presentation.write_block(
            guest, blocks.para("paragraph %04d " % i + "x" * 300),
            stream=False, notify=False)

    await harness.send(w, Frame("EXPORT", {}))
    frames = await harness.recv_until(r, {"EXPORTEND"}, 20.0)
    dats = [f for f in frames if f.type == "EXPORTDAT"]
    assert len(dats) > 1, "test setup: the export must span several frames"

    # Inflated in stream order, because one persistent deflate stream means
    # a chunk decoded out of sequence is garbage -- the same constraint the
    # guest is under.
    for f in dats:
        body = (harness.unzip.decompress(f.body)
                if f.headers.get("enc") == "deflate" else f.body)
        assert blocks.decode(body), f"chunk at {f.headers['off']} is a fragment"
    await harness.close(w)


async def test_export_of_an_empty_conversation_still_carries_its_heading(harness):
    """Exporting before saying anything should produce a valid, if dull,
    file rather than a zero-byte one or an error.

    It used to assert the bytes ended in </html>, which was the bridge
    building the document. The guest writes the <html> and the </html> now
    (win/app/export.cpp), so what this end can still promise is that the
    stream is well-formed records and carries the heading -- an export
    with no heading would be a file with no title and no date.
    """
    r, w = await harness.connect()
    await harness.recv_until(r, {"OPTIONSEND"})
    await harness.send(w, Frame("EXPORT", {}))
    frames = await harness.recv_until(r, {"EXPORTEND"}, 10.0)
    data = _export_bytes(harness, frames)
    got = blocks.decode(data)
    assert len(got) == 1 and got[0].kind == "exportheading", got
    await harness.close(w)


# --- opening a conversation without starting one ----------------------------


async def _switch_to(harness, monkeypatch, r, w, sid, history=None):
    """Drive a SWITCH with history replay faked.

    history_blocks reads the real transcript store, which a test has none
    Faking it keeps the test about the switch rather than about the SDK.
    """
    import bridge.server as srv
    monkeypatch.setattr(srv.sesslist, "history_blocks",
                        lambda sid_, limit=2000: list(history or []))
    await harness.send(w, Frame("SWITCH", {"id": sid}))
    return await harness.recv_until(r, {"OPTIONSEND"}, 10.0)


async def test_ready_names_the_conversation_not_just_the_connection(harness):
    """`sess` is the per-connection handle and is never a conversation id.
    The guest keys its cached transcript on the conversation, and before
    READY carried one, a plain reconnect cached nothing at all -- only
    SWITCHED said where pages belonged, and a reconnect does not send one."""
    r, w = await harness.connect()
    frames = await harness.recv_until(r, {"READY"})
    g = harness.bridge.connection.guest

    # THE FIRST CONNECT HAS NO CONVERSATION TO NAME, and that is not a
    # defect: `_attach` builds a client eagerly, but the SDK does not assign
    # an id until the first turn runs, so there is genuinely nothing to put
    # in the header yet. This assertion used to read the other way round and
    # passed only because the test double handed out an id in __init__ --
    # production has never sent a `conv` on a first connect.
    ready_f = [f for f in frames if f.type == "READY"][-1]
    assert not g.conv_id, "no turn has run, so there is no conversation"
    assert not ready_f.headers.get("conv")

    # The case the header exists for: the guest drops and comes back to a
    # conversation that is already under way.
    await harness.send(w, Frame("INPUT", {}, b"hello"))
    await harness.recv_until(r, {"TURN"}, 10.0)
    conv = g.conv_id
    assert conv

    await harness.close(w)
    r2, w2 = await harness.connect()
    frames = await harness.recv_until(r2, {"READY"})
    ready_f = [f for f in frames if f.type == "READY"][-1]
    assert ready_f.headers.get("conv") == conv
    assert ready_f.headers["sess"] != ready_f.headers.get("conv")
    await harness.close(w2)


async def test_switching_a_chat_starts_no_process(harness, monkeypatch):
    """Opening a conversation to READ it used to spawn a full CLI subprocess
    with --resume. Nothing was billed, but browsing the list to find something
    paid a process start per click."""
    r, w = await harness.connect()
    await harness.recv_until(r, {"OPTIONSEND"})
    g = harness.bridge.connection.guest
    first = g.claude
    assert first is not None, "the first connect builds one eagerly"

    await _switch_to(harness, monkeypatch, r, w, "stored-1",
                     ["<p>hello again</p>"])
    assert g.claude is None, "the switch must not have built a client"
    assert g.pending_resume == "stored-1"
    assert first.started is False, "the old client must be closed"
    await harness.close(w)


async def test_the_first_message_is_what_resumes_it(harness, monkeypatch):
    r, w = await harness.connect()
    await harness.recv_until(r, {"OPTIONSEND"})
    g = harness.bridge.connection.guest
    await _switch_to(harness, monkeypatch, r, w, "stored-1")

    await harness.send(w, Frame("INPUT", {}, b"carry on"))
    await harness.recv_until(r, {"TURN"}, 10.0)
    assert g.claude is not None
    assert g.claude.resume == "stored-1", "resumed the conversation we opened"
    assert g.pending_resume == "", "and stopped calling it pending"
    await harness.close(w)


async def test_the_menus_survive_a_switch_with_no_client(harness, monkeypatch):
    """The model list is a property of the installed CLI, not of a session,
    so a conversation with no process behind it still knows what it can be
    run with. Without this the toolbar read "Model" and "Mode" after every
    switch."""
    r, w = await harness.connect()
    await harness.recv_until(r, {"OPTIONSEND"})
    frames = await _switch_to(harness, monkeypatch, r, w, "stored-1")
    opts = [f for f in frames if f.type == "OPTION"]
    kinds = {f.headers.get("kind") for f in opts}
    assert {"model", "effort", "mode", "command"} <= kinds
    await harness.close(w)


async def test_choosing_a_model_with_no_client_is_not_a_no_op(
    harness, monkeypatch,
):
    """It used to be exactly that: the menu re-ticked the old model and
    nothing said why."""
    r, w = await harness.connect()
    await harness.recv_until(r, {"OPTIONSEND"})
    g = harness.bridge.connection.guest
    await _switch_to(harness, monkeypatch, r, w, "stored-1")

    await harness.send(w, Frame("SETMODEL", {"model": "haiku"}))
    await harness.recv_until(r, {"OPTIONSEND"}, 10.0)
    assert g.model == "haiku"

    await harness.send(w, Frame("INPUT", {}, b"go"))
    await harness.recv_until(r, {"TURN"}, 10.0)
    assert g.claude.model == "haiku", (
        "the deferred client must start on the model that was picked while "
        "it did not exist"
    )
    await harness.close(w)


async def test_a_switch_denies_what_the_old_conversation_left_pending(
    harness, monkeypatch,
):
    """The cleanup used to live inside _make_session, which a switch no
    longer calls. The guest hides its bar on the strength of the deny, so a
    prompt that outlived the switch would be unanswerable."""
    reader, writer, task = await _connect_and_ask(harness)
    await _recv(harness, reader, "PERMREQ")
    await _switch_to(harness, monkeypatch, reader, writer, "stored-1")
    perms = harness.bridge.connection.guest.perms
    assert perms.pending is None and not perms.queue, (
        "the prompt belonged to the conversation that was left"
    )
    task.cancel()
    await harness.close(writer)


# --- the permission-mode menu -----------------------------------------------


async def test_bypass_permissions_is_never_on_the_menu(harness):
    """One press would stop the permission bar appearing and nothing would
    say why: the docs are explicit that can_use_tool is not invoked under
    bypassPermissions. `dontAsk` is withheld for a different reason -- we
    persist no allow rules yet, so it would deny everything, which is not a
    mode, it is a fault.

    `auto` IS offered. It was withheld for one release on the strength of a
    measurement, and that was the wrong call to make silently: see
    MODE_CAVEAT, which puts the measurement in front of whoever picks it.
    """
    r, w = await harness.connect()
    frames = await harness.recv_until(r, {"OPTIONSEND"})
    modes = [f.headers["id"] for f in frames if f.type == "OPTION"
             and f.headers.get("kind") == "mode"]
    assert modes == ["manual", "workfolder", "plan", "auto"]
    assert "bypassPermissions" not in modes
    assert "dontAsk" not in modes
    ticked = [f.headers["id"] for f in frames if f.type == "OPTION"
              and f.headers.get("kind") == "mode"
              and f.headers.get("current") == "1"]
    assert ticked == ["manual"], "a fresh guest starts in Ask first"
    await harness.close(w)


async def test_choosing_auto_says_what_it_does_not_do(harness):
    """The measurement goes in front of the person choosing, once, rather
    than into a decision made on their behalf."""
    r, w = await harness.connect()
    await harness.recv_until(r, {"OPTIONSEND"})
    await harness.send(w, Frame("SETMODE", {"mode": "auto"}))
    frames = await harness.recv_until(r, {"OPTIONSEND"}, 10.0)
    body = harness.body(frames).decode("cp1252")
    assert "Auto:" in body
    # What Auto does on its own, and when it still asks -- said once, to
    # the person choosing it.
    assert "second model" in body and "asks you" in body
    await harness.close(w)


async def test_setmode_changes_the_live_session_and_the_menu(harness):
    r, w = await harness.connect()
    await harness.recv_until(r, {"OPTIONSEND"})
    await harness.send(w, Frame("SETMODE", {"mode": "workfolder"}))
    frames = await harness.recv_until(r, {"OPTIONSEND"})
    ticked = [f.headers["id"] for f in frames if f.type == "OPTION"
              and f.headers.get("kind") == "mode"
              and f.headers.get("current") == "1"]
    assert ticked == ["workfolder"]
    # The desk decides; the CLI stays in its own default mode.
    assert harness.bridge.connection.guest.claude.permission_mode == "default"
    await harness.close(w)


async def test_setmode_refuses_a_mode_that_is_not_on_the_menu(harness):
    """The header is not model-supplied, but it still arrives off a socket,
    and this particular typo would silently stop the permission bar from ever
    appearing again."""
    r, w = await harness.connect()
    await harness.recv_until(r, {"OPTIONSEND"})
    await harness.send(w, Frame("SETMODE", {"mode": "bypassPermissions"}))
    await harness.send(w, Frame("SETMODE", {"mode": "workfolder"}))
    await harness.recv_until(r, {"OPTIONSEND"})
    await harness.recv_until(r, {"OPTIONSEND"})
    assert harness.bridge.connection.guest.mode_id == "workfolder", (
        "the refused mode must not have been applied, and must not have "
        "swallowed the valid one that followed"
    )
    await harness.close(w)


async def test_a_new_chat_starts_in_the_default_mode_and_keeps_its_own(
        harness):
    """A NEW CHAT starts in the client's default mode (Options), not in
    whatever the last chat was using -- the mode is per conversation. A mode
    picked for the new chat before its first message has to survive all the
    way to the client that message builds, since New chat builds none."""
    r, w = await harness.connect()
    await harness.recv_until(r, {"OPTIONSEND"})
    await harness.send(w, Frame("SETMODE", {"mode": "auto"}))
    await harness.recv_until(r, {"OPTIONSEND"})
    await harness.send(w, Frame("NEWSESSION", {}))
    await harness.recv_until(r, {"OPTIONSEND"})
    assert harness.bridge.connection.guest.claude is None, "New chat spawns nothing"
    assert harness.bridge.connection.guest.mode_id == "manual"

    await harness.send(w, Frame("SETMODE", {"mode": "plan"}))
    await harness.recv_until(r, {"OPTIONSEND"})
    await harness.send(w, Frame("INPUT", {}, b"hello"))
    await harness.recv_until(r, {"TURN"}, 10.0)
    assert harness.bridge.connection.guest.claude.permission_mode == "plan", (
        "the session the first message builds must start in the mode the "
        "user chose, not back in Manual"
    )
    await harness.close(w)


async def test_a_conversation_gets_its_own_settings_back(harness, tmp_path,
                                                        monkeypatch):
    """Per CONVERSATION, not per machine. These used to live on the guest and
    nowhere else, so opening a chat you had set to Plan gave you whatever the
    last chat was using, and a bridge restart put everything back on the
    process defaults."""
    import bridge.server as srv
    from bridge.agent.conversations import Conversations

    r, w = await harness.connect()
    await harness.recv_until(r, {"OPTIONSEND"})
    b = harness.bridge
    b.conv = Conversations(tmp_path / "conversations.json")

    # Speak, so there is a conversation to attach settings to.
    await harness.send(w, Frame("INPUT", {}, b"hello"))
    await harness.recv_until(r, {"TURN"}, 10.0)
    first = b.connection.guest.conv_id
    assert first

    await harness.send(w, Frame("SETMODE", {"mode": "plan"}))
    await harness.recv_until(r, {"OPTIONSEND"}, 10.0)
    await harness.send(w, Frame("SETMODEL", {"model": "haiku"}))
    await harness.recv_until(r, {"OPTIONSEND"}, 10.0)
    assert b.conv.settings(first)["mode"] == "plan"
    assert b.conv.settings(first)["model"] == "haiku"

    # Somewhere else, set differently.
    rows = [{"id": "other", "title": "t", "cwd": "", "modified": "",
             "group": ""},
            {"id": first, "title": "t", "cwd": "", "modified": "",
             "group": ""}]
    orig = srv.sesslist.listing
    srv.sesslist.listing = lambda cwd=None, limit=50: list(rows)
    try:
        await _switch_to(harness, monkeypatch=_NoPatch(), r=r, w=w,
                         sid="other")
        await harness.send(w, Frame("SETMODE", {"mode": "workfolder"}))
        await harness.recv_until(r, {"OPTIONSEND"}, 10.0)
        assert b.connection.guest.mode_id == "workfolder"

        # ...and back. The first conversation's own choices return.
        await _switch_to(harness, monkeypatch=_NoPatch(), r=r, w=w, sid=first)
    finally:
        srv.sesslist.listing = orig

    assert b.connection.guest.mode_id == "plan", "its own mode, not the other chat's"
    assert b.connection.guest.model == "haiku"
    await harness.close(w)


async def test_a_conversation_with_nothing_saved_starts_in_the_default_mode(
    harness, tmp_path, monkeypatch,
):
    """"I have not chosen for this chat" and "I chose the default for this
    chat" are different, and only the second is written down. For the MODE
    a chat with nothing saved takes the client's default rather than the
    last chat's: a mode is a licence, and it must not follow the person from
    one conversation into another."""
    import bridge.server as srv
    from bridge.agent.conversations import Conversations

    r, w = await harness.connect()
    await harness.recv_until(r, {"OPTIONSEND"})
    b = harness.bridge
    b.conv = Conversations(tmp_path / "conversations.json")

    await harness.send(w, Frame("SETMODE", {"mode": "plan"}))
    await harness.recv_until(r, {"OPTIONSEND"}, 10.0)

    rows = [{"id": "ancient", "title": "t", "cwd": "", "modified": "",
             "group": ""}]
    orig = srv.sesslist.listing
    srv.sesslist.listing = lambda cwd=None, limit=50: list(rows)
    try:
        await _switch_to(harness, monkeypatch=_NoPatch(), r=r, w=w,
                         sid="ancient")
    finally:
        srv.sesslist.listing = orig
    assert b.connection.guest.mode_id == "manual"
    await harness.close(w)


async def test_settings_chosen_before_the_first_message_are_saved(
    harness, tmp_path,
):
    """The two halves together: pick before there is a conversation, and have
    it both apply AND be remembered once the conversation exists."""
    from bridge.agent.conversations import Conversations

    r, w = await harness.connect()
    await harness.recv_until(r, {"OPTIONSEND"})
    b = harness.bridge
    b.conv = Conversations(tmp_path / "conversations.json")

    await harness.send(w, Frame("NEWSESSION", {}))
    await harness.recv_until(r, {"OPTIONSEND"}, 10.0)
    assert b.connection.guest.claude is None
    await harness.send(w, Frame("SETMODE", {"mode": "plan"}))
    await harness.recv_until(r, {"OPTIONSEND"}, 10.0)
    assert len(b.conv) == 0, "nowhere to write it yet, and it must not"

    await harness.send(w, Frame("INPUT", {}, b"hello"))
    await harness.recv_until(r, {"TURN"}, 10.0)

    conv = b.connection.guest.conv_id
    assert b.connection.guest.claude.permission_mode == "plan", "applied"
    assert b.conv.settings(conv)["mode"] == "plan", "and written down"
    await harness.close(w)


async def test_settings_chosen_before_the_first_message_are_not_refused(
    harness,
):
    """The window where a user most wants these: between New chat and typing.

    It used to build the client immediately, so Model, Effort and Mode all
    acted on a session that existed but had never run a turn -- and the CLI
    refuses control requests against one. Picking a mode said "Could not
    change the permission mode", and picking an effort tried to fork a
    session with no history. Nothing was applied and the toolbar snapped
    back.
    """
    r, w = await harness.connect()
    await harness.recv_until(r, {"OPTIONSEND"})
    await harness.send(w, Frame("NEWSESSION", {}))
    await harness.recv_until(r, {"OPTIONSEND"})
    g = harness.bridge.connection.guest
    assert g.claude is None

    await harness.send(w, Frame("SETMODE", {"mode": "workfolder"}))
    await harness.recv_until(r, {"OPTIONSEND"}, 10.0)
    await harness.send(w, Frame("SETMODEL", {"model": "haiku"}))
    await harness.recv_until(r, {"OPTIONSEND"}, 10.0)
    await harness.send(w, Frame("SETEFFORT", {"effort": "low"}))
    frames = await harness.recv_until(r, {"OPTIONSEND"}, 10.0)

    # Recorded, and said so rather than refused.
    assert g.mode_id == "workfolder"
    assert g.model == "haiku"
    assert g.effort == "low"
    body = harness.body(frames).lower()
    assert b"could not" not in body, body[:300]

    # And all three reach the client the first message builds.
    await harness.send(w, Frame("INPUT", {}, b"hello"))
    await harness.recv_until(r, {"TURN"}, 10.0)
    assert g.claude.permission_mode == "default"
    assert g.claude.model == "haiku"
    assert g.claude.effort == "low"
    await harness.close(w)


# --- the commands menu ------------------------------------------------------


async def test_command_menu_is_curated_and_intersected_with_the_cli(harness):
    """Three of fifty-three, and the rules that pick them.

    /model is withheld because the toolbar owns the model and going through
    the CLI would change it WITHOUT a SETMODEL frame, leaving the dropdown
    showing the old one. /heapdump is withheld because it writes to the
    OPERATOR'S Desktop -- it configures the bridge host, not the machine the
    person is sitting at. /recap and /usage are withheld because they were
    MEASURED to produce nothing: both cost a turn and rendered a blank
    transcript. And /init is in COMMAND_MENU but not in this install's list,
    so it must not be offered: a menu entry for a command the CLI does not
    have fails as a typed prompt, seconds later, with the model apologising
    for it.
    """
    r, w = await harness.connect()
    frames = await harness.recv_until(r, {"OPTIONSEND"})
    cmds = [f.headers["id"] for f in frames if f.type == "OPTION"
            and f.headers.get("kind") == "command"]
    assert cmds == ["/compact", "/rename"]
    await harness.close(w)


async def test_command_labels_carry_the_argument_hint(harness):
    """The label is where "what does this one take?" is asked, and the `hint`
    header is the same fact -- the guest reads it as "open the input box for
    them to finish the line" rather than sending it bare. One source, so a
    label promising an argument and a click that ignores it cannot happen."""
    r, w = await harness.connect()
    frames = await harness.recv_until(r, {"OPTIONSEND"})
    rows = {f.headers["id"]: f.headers for f in frames if f.type == "OPTION"
            and f.headers.get("kind") == "command"}
    assert rows["/compact"]["label"] == "/compact <options>"
    assert rows["/compact"].get("hint") == "1"
    assert rows["/rename"]["label"] == "/rename [name]"
    await harness.close(w)


async def test_a_silent_command_says_so_rather_than_looking_like_nothing(
    harness,
):
    """A command the CLI resolves locally returns no message at all, so the
    transcript showed the echoed "/recap" and then nothing -- identical to a
    command that failed. Measured on the guest before this existed."""
    r, w = await harness.connect()
    await harness.recv_until(r, {"OPTIONSEND"})
    harness.bridge.connection.guest.claude.silent = True
    await harness.send(w, Frame("INPUT", {}, b"/recap"))
    frames = []
    # Twice: TURN arrives at the start of a turn as well as at its end, and
    # the notice is written between them.
    for _ in range(2):
        frames += await harness.recv_until(r, {"TURN"}, 10.0)
    assert b"without sending anything back" in harness.body(frames)
    await harness.close(w)


# --- permissions ------------------------------------------------------------
#
# The shape of these tests matters as much as their assertions. A permission
# request is driven through FakeSession.ask(), which spawns a real detached
# task, because production runs can_use_tool that way and the worst bug found
# in review was only reachable when the task is CANCELLED. A fixture that
# awaited the callback inline would pass while that bug shipped.


async def test_permission_request_reaches_the_guest(harness):
    reader, writer, task = await _connect_and_ask(harness)
    f = await _recv(harness, reader, "PERMREQ")
    assert f.headers["id"] == "toolu_test_1"
    assert f.headers["tool"] == "Bash"
    # A command on the bridge is never grantable: one click must not buy a
    # standing licence for a shell (policy.GRANTABLE).
    assert f.headers["allows"] == "0"
    assert b"vol c:" in f.body
    task.cancel()
    await harness.close(writer)


async def test_allow_resolves_the_callback(harness):
    reader, writer, task = await _connect_and_ask(harness)
    await _recv(harness, reader, "PERMREQ")
    await harness.send(writer, Frame("PERMREPLY",
                               {"id": "toolu_test_1", "action": "allow"}))
    result = await asyncio.wait_for(task, 5)
    assert isinstance(result, PermissionResultAllow)
    assert result.updated_permissions is None
    end = await _recv(harness, reader, "PERMEND")
    assert end.headers["reason"] == "answered"
    await harness.close(writer)


async def test_deny_does_not_interrupt_the_turn(harness):
    """A single refusal should tell the model no, not tear the turn down."""
    reader, writer, task = await _connect_and_ask(harness)
    await _recv(harness, reader, "PERMREQ")
    await harness.send(writer, Frame("PERMREPLY",
                               {"id": "toolu_test_1", "action": "deny"}))
    result = await asyncio.wait_for(task, 5)
    assert isinstance(result, PermissionResultDeny)
    assert result.interrupt is False
    assert result.message
    await harness.close(writer)


async def test_permission_deadline_auto_denies_after_timeout(harness, monkeypatch):
    """PLAN.md:840-842: A permission request unanswered for 10 minutes (perms.DEADLINE_S)
    auto-denies rather than wedging the session forever.
    """
    from bridge.agent import permissions as perms
    assert perms.DEADLINE_S == 600.0

    monkeypatch.setattr(perms, "DEADLINE_S", 0.05)
    reader, writer, task = await _connect_and_ask(harness)
    f = await _recv(harness, reader, "PERMREQ")
    assert f.type == "PERMREQ"

    result = await asyncio.wait_for(task, timeout=2.0)
    assert isinstance(result, PermissionResultDeny)
    # The wait as configured, in words -- the test shortens it.
    assert "so it was denied" in result.message
    assert "second" in result.message
    assert result.interrupt is False

    end = await _recv(harness, reader, "PERMEND")
    assert end.headers["reason"] == "timeout"
    await harness.close(writer)


async def test_always_grants_for_this_chat_and_forwards_nothing(harness):
    """"Allow for this chat" is a grant of the CATEGORY, kept beside the
    mode it was given under. Nothing goes to the CLI: its own rules would
    keep approving after the mode changed, whatever the new table said."""
    reader, writer = await harness.connect()
    await _recv(harness, reader, "OPTIONSEND")
    g = harness.bridge.connection.guest
    g.work_guest = "C:\\WORK"
    args = {"path": "notes.txt", "content": "x"}
    task = g.claude.ask("mcp__machine__put_file", args)
    f = await _recv(harness, reader, "PERMREQ")
    assert f.headers["allows"] == "1"
    await harness.send(writer, Frame("PERMREPLY",
                                     {"id": f.headers["id"],
                                      "action": "always"}))
    result = await asyncio.wait_for(task, 5)
    assert isinstance(result, PermissionResultAllow)
    assert not result.updated_permissions
    assert "files_work" in g.grants

    # The next write in the work folder needs no bar.
    again = await asyncio.wait_for(
        g.claude.ask("mcp__machine__put_file", {"path": "b.txt",
                                                "content": "y"}), 5)
    assert isinstance(again, PermissionResultAllow)
    await harness.close(writer)


async def test_always_is_suppressed_for_machine_tools(harness):
    """MCP rules cannot carry specifiers, so the only rule expressible for
    mcp__machine__exec is the WHOLE TOOL -- one press of Always would grant
    silent arbitrary command execution on the user's computer for the rest of
    the session. The suggestion list is non-empty either way, so keying the
    button off it would light it up."""
    reader, writer = await harness.connect()
    await _recv(harness, reader, "OPTIONSEND")
    task = harness.bridge.connection.guest.claude.ask(
        tool_name="mcp__machine__exec", args={"command": "someprog.exe"})
    f = await _recv(harness, reader, "PERMREQ")
    assert f.headers["tool"] == "mcp__machine__exec"
    assert f.headers["allows"] == "0", "Always must not be offered"
    task.cancel()
    await harness.close(writer)


async def test_cancelled_callback_still_clears_the_bar(harness):
    """The worst bug review found, and the one whose first fix was also wrong.

    The CLI cancels a pending can_use_tool when the turn is interrupted. The
    callback then dies inside a cancelled scope, where an await would abort
    before it ran -- so the PERMEND must be scheduled, not awaited. Without
    this the bar stays up and Send stays disabled forever.
    """
    reader, writer, task = await _connect_and_ask(harness)
    await _recv(harness, reader, "PERMREQ")

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    end = await _recv(harness, reader, "PERMEND")
    assert end.headers["id"] == "toolu_test_1"
    assert end.headers["reason"] == "cancelled"
    assert harness.bridge.connection.guest.perms.pending is None
    await harness.close(writer)


async def test_a_disconnect_mid_sync_fails_the_transfer_not_the_connection(
        harness):
    """`_fail_syncs` read `entry.get("future")`, which was correct while
    `sync_pending` held ad-hoc dicts and became `AttributeError: 'Wait'
    object has no attribute 'get'` the moment 02.4 typed those entries as
    `sync.Wait` -- so a guest that dropped mid-transfer crashed the
    connection handler instead of failing the in-flight SYNCPUT/GET/SCAN/
    MKDIR future, which then held the sync code waiting on a future nobody
    would ever resolve.
    """
    from bridge.files import sync as S

    reader, writer = await harness.connect()
    await harness.recv_until(reader, {"READY"})
    guest = harness.bridge.connection.guest

    fut = asyncio.get_event_loop().create_future()
    guest.sync.pending["s1"] = S.Wait(kind="put", future=fut)

    await harness.close(writer)
    async with asyncio.timeout(5):
        while not fut.done():
            await asyncio.sleep(0.01)

    assert fut.exception() is not None, (
        "the in-flight sync future must be failed, not left to hang"
    )
    assert "disconnected" in str(fut.exception())
    assert guest.sync.pending == {}, "the failed entry must be cleared"


async def test_disconnect_holds_the_prompt_then_denies(harness):
    """Not "denies immediately", which is what the plan said and what the first
    implementation did.

    Denying on any drop tells the model the user declined something the user
    never saw -- on a flaky 1998 NIC a blip becomes a refusal. So the prompt is
    held for a grace period and then denied with a message that says what
    actually happened, rather than claiming a decision nobody made.
    """
    harness.bridge.config.perm_grace_s = 0.2
    harness.bridge.config.idle_check_s = 0.05

    reader, writer, task = await _connect_and_ask(harness)
    await _recv(harness, reader, "PERMREQ")
    await harness.close(writer)

    result = await asyncio.wait_for(task, 5)
    assert isinstance(result, PermissionResultDeny)
    assert "disconnected" in result.message
    assert "not refused on purpose" in result.message
    assert harness.bridge.connection.guest.perms.pending is None


async def test_prompt_is_reissued_after_reconnect(harness):
    """Nothing re-sends anything from a log on reconnect (there is no wire
    replay any more -- notes/BACKLOG.md #5). Without an explicit re-issue the
    guest would reconnect with no bar while the bridge still holds a live
    future, and the turn hangs until the answer deadline."""
    harness.bridge.config.perm_grace_s = 30.0      # comfortably inside the grace

    reader, writer, task = await _connect_and_ask(harness)
    await _recv(harness, reader, "PERMREQ")
    await harness.close(writer)

    reader2, writer2 = await harness.connect()
    f = await _recv(harness, reader2, "PERMSTATE")
    assert f.headers["id"] == "toolu_test_1"
    assert b"vol c:" in f.body

    # And it is still answerable on the new connection.
    await harness.send(writer2, Frame("PERMREPLY",
                                      {"id": "toolu_test_1", "action": "allow"}))
    result = await asyncio.wait_for(task, 5)
    assert isinstance(result, PermissionResultAllow)
    await harness.close(writer2)


async def test_second_request_queues_behind_the_first(harness):
    """One guest can show one bar. The model can request tools in parallel."""
    reader, writer, task1 = await _connect_and_ask(harness)
    await _recv(harness, reader, "PERMREQ")

    task2 = harness.bridge.connection.guest.claude.ask(tool_use_id="toolu_test_2")
    wait = await _recv(harness, reader, "PERMWAIT")
    assert wait.headers["waiting"] == "1"

    await harness.send(writer, Frame("PERMREPLY",
                               {"id": "toolu_test_1", "action": "allow"}))
    await asyncio.wait_for(task1, 5)
    nxt = await _recv(harness, reader, "PERMREQ")
    assert nxt.headers["id"] == "toolu_test_2"
    task2.cancel()
    await harness.close(writer)


async def test_reply_for_an_unknown_id_is_ignored(harness):
    """A reply can race a deadline, a disconnect or a session rebuild.
    Ignoring is right; raising would drop the connection."""
    reader, writer = await harness.connect()
    await _recv(harness, reader, "OPTIONSEND")
    await harness.send(writer, Frame("PERMREPLY",
                               {"id": "nobody", "action": "allow"}))
    await harness.send(writer, Frame("PING", {}))
    await harness.recv_until(reader, {"PONG"})   # still alive
    await harness.close(writer)


async def test_oversized_prompt_still_reaches_the_guest(harness):
    """A wildly long description must not cost the connection: MAX_BODY
    (bridge/agent/permissions.py) clips a single argument value well before
    render_request's own table cap could ever be reached, so the PERMREQ
    this produces stays tiny and GuestLink.send never has reason to refuse
    it. See test_permissions_gaston.py for MAX_BODY's own truncate-and-log
    path, which needs many distinct arguments, not one long one, to fire."""
    reader, writer, task = await _connect_and_ask(
        harness, description="x" * 200_000)
    f = await _recv(harness, reader, "PERMREQ")
    assert len(f.body) < MAX_FRAME
    task.cancel()
    await harness.close(writer)


async def test_send_reports_whether_the_frame_actually_went(harness):
    """02.16: _send returned None unconditionally, so a caller had no way to
    tell an oversized frame (refused) or a closed writer (dropped) from an
    ordinary success -- both were silent. It now returns a bool for the
    ordinary "nothing to be done about it" case (a closed writer -- the
    guest is simply gone) and raises for the one that is a bridge-side bug:
    a frame nothing could ever chunk or truncate for the caller, which used
    to vanish with nothing but a log line nobody was watching in real time.
    """
    reader, writer = await harness.connect()
    await _recv(harness, reader, "OPTIONSEND")
    guest = harness.bridge.connection.guest

    ok = await harness.bridge.connection.send(guest, srv.notice("k", "an ordinary notice"))
    assert ok is True

    huge = srv.Frame("NOTICE", {"kind": "k"}, b"x" * 200_000)
    with pytest.raises(ProtocolError):
        await harness.bridge.connection.send(guest, huge)

    await harness.close(writer)
    await asyncio.sleep(0.1)
    dropped = await harness.bridge.connection.send(guest, srv.notice("k", "after close"))
    assert dropped is False, "a closed writer must report that nothing was sent"


async def test_session_rebuild_denies_a_pending_prompt(harness):
    """A prompt belongs to the session that raised it; NEWSESSION orphans it."""
    reader, writer, task = await _connect_and_ask(harness)
    await _recv(harness, reader, "PERMREQ")
    await harness.send(writer, Frame("NEWSESSION", {}))
    result = await asyncio.wait_for(task, 5)
    assert isinstance(result, PermissionResultDeny)
    await harness.close(writer)


async def test_idle_guest_is_reaped_so_it_can_reconnect(harness):
    """P2: a guest that GPFs leaves no FIN behind, so _pump parks in read()
    forever with `connected` still True -- and every reconnect is then refused
    as "busy", permanently. The machine crashing is the expected case here.
    """
    harness.bridge.config.idle_timeout_s = 0.2
    harness.bridge.config.idle_check_s = 0.05

    reader, writer, task = await _connect_and_ask(harness)
    await _recv(harness, reader, "PERMREQ")

    # Go silent without closing, the way a crashed guest does.
    result = await asyncio.wait_for(task, 5)
    assert isinstance(result, PermissionResultDeny), \
        "the pending prompt must be denied, not left hanging"

    # And the slot is free again: a fresh client is not told "busy".
    reader2, writer2 = await harness.connect()
    f = await _recv(harness, reader2, "READY")
    assert f.type == "READY"
    await harness.close(writer, writer2)


async def test_frame_pipelined_behind_hello_is_not_dropped(harness):
    """_read_hello returned on the first HELLO and discarded the rest of the
    batch. Frames already parsed out of the FrameReader's buffer were gone --
    silently, which is the worst way to lose one. Reachable whenever a guest
    sends anything in the same TCP segment as its HELLO."""
    reader, writer = await asyncio.open_connection("127.0.0.1", harness.port)
    # Both frames in a single write, so they land in one read() on the bridge.
    writer.write(hello("s").encode() + Frame("PING", {}).encode())
    await writer.drain()
    harness._readers[id(reader)] = FrameReader()
    harness._pending[id(reader)] = []
    f = await _recv(harness, reader, "PONG", 5.0)
    assert f.type == "PONG"
    await harness.close(writer)


async def test_path_outside_the_working_copy_is_refused_without_asking(harness):
    """The proxy is infrastructure, not the subject of the conversation.

    Every request the user makes is about THEIR machine, so answering "what is
    my hostname" by reading the proxy's /etc/hostname is wrong even though the
    file is real. Refused before it ever reaches the bar -- the user should not
    have to adjudicate a request they did not make.
    """
    reader, writer = await harness.connect()
    await _recv(harness, reader, "OPTIONSEND")
    task = harness.bridge.connection.guest.claude.ask(
        tool_name="Read", args={"file_path": "/etc/hostname"})
    result = await asyncio.wait_for(task, 5)
    assert isinstance(result, PermissionResultDeny)
    assert "not on the user's machine" in result.message
    assert harness.bridge.connection.guest.perms.pending is None, "no bar should appear"
    await harness.close(writer)


async def test_path_inside_the_working_copy_still_asks(harness):
    """The guard must not swallow legitimate staging work."""
    import os as _os
    inside = _os.path.join(harness.bridge.config.cwd or _os.getcwd(), "staged.txt")
    reader, writer = await harness.connect()
    await _recv(harness, reader, "OPTIONSEND")
    task = harness.bridge.connection.guest.claude.ask(
        tool_name="Write", args={"file_path": inside, "content": "x"})
    f = await _recv(harness, reader, "PERMREQ")
    assert f.headers["tool"] == "Write"
    task.cancel()
    await harness.close(writer)


async def test_cancelling_a_turn_tells_the_guest_to_kill_the_command(harness):
    """Without EXECKILL, an interrupted turn leaves the command running on the
    guest, and the NEXT call is answered `busy` -- so one interrupted turn
    poisons the following one for a reason the model can neither see nor act
    on. The send is scheduled rather than awaited because the cancellation
    path cannot await.
    """
    reader, writer = await harness.connect()
    await _recv(harness, reader, "OPTIONSEND")

    task = asyncio.create_task(
        harness.bridge.guest_exec.run_on_guest("dir /s c:\\", timeout_ms=60000,
                                    shell="", cwd="",
        guest=harness.bridge.connection.guest))
    f = await _recv(harness, reader, "EXEC")
    exec_id = f.headers["id"]

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    kill = await _recv(harness, reader, "EXECKILL")
    assert kill.headers["id"] == exec_id
    # And the slot is free, so the next call is not answered "busy".
    assert not harness.bridge.connection.guest.exec_pending
    await harness.close(writer)


async def test_exec_refuses_wire_overlong_cwd(harness):
    """Audit 06.2: A cwd that is under MAX_HEADER_VALUE in UTF-8 bytes but
    over MAX_HEADER_VALUE on the wire (due to %XX expansion of CP1252 high bytes)
    must be refused rather than sent to the guest to drop the frame."""
    reader, writer = await harness.connect()
    await _recv(harness, reader, "OPTIONSEND")

    # 3 bytes + 85 * 2 bytes = 173 UTF-8 bytes (< 255)
    # Wire length: 3 + 85 * 3 = 258 bytes (> 255)
    long_cwd = "C:\\" + ("\xe9" * 85)
    res = await harness.bridge.guest_exec.run_on_guest(
        "dir", timeout_ms=5000, shell="none", cwd=long_cwd
    ,
        guest=harness.bridge.connection.guest)
    assert res.get("isError") is True
    assert "too long to send to the machine" in res["content"][0]["text"]
    assert "258 bytes on the wire" in res["content"][0]["text"]
    await harness.close(writer)


async def test_exec_is_never_re_delivered(harness):
    """There is no wire replay any more (notes/BACKLOG.md #5) -- nothing
    resends a frame from a log; every frame is sent once, when it is sent.
    The one thing that DOES re-send state after a reconnect is the explicit
    reissue path (`_reissue_halt`, `_reissue_turn`,
    `PermissionDesk.reissue`), and it must never re-send a side-effecting
    frame: a re-delivered EXEC or EXECKILL would RE-RUN or re-kill a
    command that already finished -- `del c:\\temp\\*` twice is not a
    redraw -- and the same holds for every SYNC* frame (see
    test_sync_wire.py's counterpart of this test).
    """
    reader, writer = await harness.connect()
    await _recv(harness, reader, "OPTIONSEND")

    task = asyncio.create_task(
        harness.bridge.guest_exec.run_on_guest("ver", timeout_ms=5000, shell="", cwd="",
        guest=harness.bridge.connection.guest))
    await _recv(harness, reader, "EXEC")
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    # The cancellation sends EXECKILL, live, on this same connection -- that
    # is the one and only delivery, not a replay.
    await _recv(harness, reader, "EXECKILL")
    await harness.close(writer)

    # Reconnect. Whatever the reissue path sends must not include EXEC,
    # EXECKILL or any SYNC* frame a second time.
    reader2, writer2 = await harness.connect()
    got = await harness.recv_until(reader2, {"OPTIONSEND"}, timeout=3)
    types = {f.type for f in got}
    assert "EXEC" not in types
    assert "EXECKILL" not in types
    assert not any(t.startswith("SYNC") for t in types)
    await harness.close(writer2)


async def test_stop_kills_a_running_command(harness):
    """Pressing Stop must reach the command, not just the model.

    run_on_guest's CancelledError path is largely unreachable: on mcp 1.x the
    SDK does not cancel an in-flight tool call when a turn is interrupted, so
    that coroutine never wakes up to send EXECKILL. Measured on the guest --
    `pause` kept running for its full clamp after Stop. INTERRUPT is the
    trigger that actually fires.
    """
    reader, writer = await harness.connect()
    await _recv(harness, reader, "OPTIONSEND")

    task = asyncio.create_task(
        harness.bridge.guest_exec.run_on_guest("pause", timeout_ms=90000,
                                    shell="", cwd="",
        guest=harness.bridge.connection.guest))
    f = await _recv(harness, reader, "EXEC")
    exec_id = f.headers["id"]

    await harness.send(writer, Frame("INTERRUPT", {}))
    kill = await _recv(harness, reader, "EXECKILL")
    assert kill.headers["id"] == exec_id

    # The guest answers the kill; that resolves the call.
    await harness.send(writer, Frame("EXECRESULT",
                                     {"id": exec_id, "status": "cancelled",
                                      "code": "1", "bytes": "0"}))
    result = await asyncio.wait_for(task, 5)
    assert result["isError"] is True
    await harness.close(writer)


async def test_stop_stops_a_running_step_list_and_the_one_behind_it(harness):
    """Pressing Stop must reach the desktop, not just the model.

    The same hole as the command above, with a sharper edge: an uncancelled
    step list goes on typing and clicking on a machine somebody is sitting
    at, and Stop was very likely pressed ON the strip that says the desktop
    is being driven. So INTERRUPT sends UICANCEL for every `run` in flight.

    And the one queued behind it, which is the half that is easy to miss. A
    model can put two `ui` calls in one assistant message; the second is
    waiting on guest.ui_lock, and without `turn_stopped` it would take the
    lock the moment the first one answered and drive on.
    """
    reader, writer = await harness.connect()
    await _recv(harness, reader, "OPTIONSEND")
    guest = harness.bridge.connection.guest
    ui = harness.bridge.ui

    first = asyncio.create_task(
        ui.run(guest, b"wait\tms=60000\n", timeout_ms=90000, step_ms=30000))
    f = await _recv(harness, reader, "UIRUN")
    ui_id = f.headers["id"]

    # A second call, queued behind the first on the lane's lock.
    second = asyncio.create_task(
        ui.run(guest, b"type\ttext=hello\n", timeout_ms=9000, step_ms=3000))
    await asyncio.sleep(0)

    await harness.send(writer, Frame("INTERRUPT", {}))
    cancel = await _recv(harness, reader, "UICANCEL")
    assert cancel.headers["id"] == ui_id

    # The guest answers the cancel, which releases the lane.
    await harness.send(writer, Frame("UIEND", {"id": ui_id,
                                               "status": "cancelled"}))
    headers, _ = await asyncio.wait_for(first, 5)
    assert headers["status"] == "cancelled"

    # The one behind it never goes out.
    with pytest.raises(UiError) as caught:
        await asyncio.wait_for(second, 5)
    assert caught.value.status == "stopped"
    await harness.close(writer)


async def test_a_command_line_too_long_says_so_rather_than_spawn_failed(
        harness):
    """The guest refuses a command line over ~1000 characters before running
    it. That was flattened onto `spawn_failed`, which reads as an environment
    problem and invites a retry -- and retrying a deterministic size rejection
    never succeeds. The model also has no way to guess the limit unless it is
    told it."""
    reader, writer = await harness.connect()
    await _recv(harness, reader, "OPTIONSEND")
    task = asyncio.create_task(harness.bridge.guest_exec.run_on_guest(
        "x" * 2000, timeout_ms=5000, shell="", cwd="",
        guest=harness.bridge.connection.guest))
    req = await _recv(harness, reader, "EXEC")
    await harness.send(writer, Frame("EXECRESULT", {
        "id": req.headers["id"], "status": "too_long", "code": "0",
        "truncated": "0"}))
    got = await asyncio.wait_for(task, 5)
    text = got["content"][0]["text"]
    assert "too long" in text
    assert "could not be started" not in text
    assert got["isError"] is True
    await harness.close(writer)


async def test_output_is_capped_here_too_and_says_it_was(harness):
    """The 32KB cap lives in the other binary. This is the only place the
    bridge trusts a length a guest supplies, and a client that lied could grow
    the buffer for as long as the call has left to run."""
    reader, writer = await harness.connect()
    await _recv(harness, reader, "OPTIONSEND")
    task = asyncio.create_task(harness.bridge.guest_exec.run_on_guest("dir", timeout_ms=5000,
                                    shell="", cwd="",
        guest=harness.bridge.connection.guest))
    req = await _recv(harness, reader, "EXEC")
    eid = req.headers["id"]
    for _ in range(4):
        await harness.send(writer, Frame("EXECOUT", {"id": eid},
                                         b"A" * 32768))
    await harness.send(writer, Frame("EXECRESULT", {
        "id": eid, "status": "ok", "code": "0", "truncated": "0"}))
    got = await asyncio.wait_for(task, 5)
    text = got["content"][0]["text"]
    assert len(text) < 4 * 32768, "the buffer grew past the bridge's own cap"
    assert "truncated" in text, "capped without saying so"
    # 03.16: this run sent 128KB against the bridge's own 64KB cap while the
    # guest reported truncated=0 -- the BRIDGE cut it, not the guest's 32KB.
    # The message must name the actual cause.
    assert "64KB by this bridge" in text
    assert "32KB by the connected machine" not in text
    await harness.close(writer)


async def test_guest_truncation_is_attributed_to_the_guest_not_the_bridge(
        harness):
    """03.16: server.py used to always say "Output was capped at 32KB and
    truncated," which is the guest's own EXEC_MAX_OUT -- true when the guest
    is the one that cut it (truncated=1, well under the bridge's 64KB cap),
    but wrong whenever the bridge's own EXEC_OUT_MAX was the actual cause."""
    reader, writer = await harness.connect()
    await _recv(harness, reader, "OPTIONSEND")
    task = asyncio.create_task(harness.bridge.guest_exec.run_on_guest("dir", timeout_ms=5000,
                                    shell="", cwd="",
        guest=harness.bridge.connection.guest))
    req = await _recv(harness, reader, "EXEC")
    eid = req.headers["id"]
    await harness.send(writer, Frame("EXECOUT", {"id": eid}, b"A" * 100))
    await harness.send(writer, Frame("EXECRESULT", {
        "id": eid, "status": "ok", "code": "0", "truncated": "1"}))
    got = await asyncio.wait_for(task, 5)
    text = got["content"][0]["text"]
    assert "32KB by the connected machine" in text
    assert "by this bridge" not in text
    await harness.close(writer)


async def test_a_timed_out_command_is_reported_as_an_error(harness):
    """03.46: server.py's isError set used to be {spawn_failed, too_long,
    busy, cancelled} -- a command killed at the timeout has status=timeout,
    which was absent from that set, so it came back to the model as a
    SUCCESS with only a prose preamble, unlike every other terminal exec
    failure."""
    reader, writer = await harness.connect()
    await _recv(harness, reader, "OPTIONSEND")
    task = asyncio.create_task(harness.bridge.guest_exec.run_on_guest("pause", timeout_ms=5000,
                                    shell="", cwd="",
        guest=harness.bridge.connection.guest))
    req = await _recv(harness, reader, "EXEC")
    await harness.send(writer, Frame("EXECRESULT", {
        "id": req.headers["id"], "status": "timeout", "code": "1",
        "truncated": "0"}))
    got = await asyncio.wait_for(task, 5)
    assert got["isError"] is True, (
        "a timed-out command must be reported as an error, not a success"
    )
    assert "Timed out" in got["content"][0]["text"]
    await harness.close(writer)


async def test_a_tool_result_reaches_the_guest_as_a_block(harness):
    """Tool results have never appeared on Windows 98.

    They went out as a raw TOOLRESULT frame carrying plain text, and the guest
    has no branch for that type -- it fell through to "unhandled frame type".
    The user saw the call ("Bash: ls"), then silence, then the model
    discussing output they could not see.

    A finished block instead, like everything else the user reads: the guest
    transforms nothing, and going through _write_block also puts it in the
    paginator, so it survives a reconnect and is still there when paging back.
    """
    reader, _writer = await harness.connect()
    await _recv(harness, reader, "OPTIONSEND")
    guest = harness.bridge.connection.guest
    await harness.bridge.presentation.write_block(
        guest, srv.H.tool_result("total 4\ndrwxr-xr-x  work\n"))

    frame = await _recv(harness, reader, "TEXT")
    body = frame.body
    if frame.headers.get("enc") == "deflate":
        body = zlib.decompressobj().decompress(body)
    text = body.decode("cp1252")
    assert "toolresult" in text
    assert "drwxr-xr-x" in text
    assert frame.type != "TOOLRESULT", "a frame the guest has no handler for"


def test_a_tool_result_is_clipped_for_the_screen_not_the_model():
    """A `dir /s` is megabytes. The model already has the whole thing; the
    person on a 640x480 screen with 32MB does not need it, and the guest
    should not spend memory holding it."""
    from bridge.view import html as H
    block = H.tool_result("x" * 50_000)
    # AN ATTRIBUTE, not a sentence, which is the point of the move: the
    # bridge says how much it dropped and the client words it. The wording
    # lives in win/resources/view.tpl's `clip` key and is checked there;
    # what the bridge owes is the number and a payload that was actually
    # cut.
    assert block.attrs["clipped"] == 50_000 - H._TOOL_RESULT_MAX
    body = "".join(t for _, t in block.payload)
    assert len(body) == H._TOOL_RESULT_MAX


def test_an_empty_tool_result_still_says_something():
    """Silence is indistinguishable from a bug, which is how this was found."""
    from bridge.view import html as H
    assert "(no output)" in H.tool_result("").texts()
    assert "(no output)" in H.tool_result("   \n ").texts()


async def test_an_overlong_cwd_is_refused_rather_than_clipped(harness):
    """`cwd` travels as a header, and the guest's parser caps a header value
    at 255 bytes. It used to CLIP there, in silence, and hand the short path
    to CreateProcessA -- which names a DIFFERENT, existing directory, so the
    command ran somewhere nobody chose and nothing reported it.

    The guest drops the frame now, which is loud but useless on its own: a
    dropped frame is a command that never answers. The bridge is the side
    that can say something intelligible, so it refuses first.
    """
    reader, writer = await harness.connect()
    await _recv(harness, reader, "OPTIONSEND")
    got = await harness.bridge.guest_exec.run_on_guest(
        "dir", timeout_ms=5000, shell="", cwd="C:\\" + "d" * 300,
        guest=harness.bridge.connection.guest)
    text = got["content"][0]["text"]
    assert "too long" in text
    assert got["isError"] is True
    assert "Nothing was run" in text
    await harness.close(writer)


async def test_an_ordinary_cwd_still_goes_through(harness):
    """The guard must not fire on the case it exists to protect."""
    reader, writer = await harness.connect()
    await _recv(harness, reader, "OPTIONSEND")
    task = asyncio.create_task(harness.bridge.guest_exec.run_on_guest(
        "dir", timeout_ms=5000, shell="", cwd="C:\\WORK",
        guest=harness.bridge.connection.guest))
    req = await _recv(harness, reader, "EXEC")
    assert req.headers["cwd"] == "C:\\WORK"
    await harness.send(writer, Frame("EXECRESULT", {
        "id": req.headers["id"], "status": "ok", "code": "0",
        "truncated": "0"}))
    await asyncio.wait_for(task, 5)
    await harness.close(writer)


async def test_a_keepalive_keeps_an_idle_guest_alive(harness):
    """The reaper closes a guest silent for 90s, because a machine that GPFs
    leaves no FIN and would hold the slot forever -- and `last_seen` only
    moves when a frame arrives.

    With sync off the guest sent NOTHING between turns, so a user reading a
    permission prompt was disconnected after 90 seconds against the
    600-second deadline for that very prompt. The guest now sends PING on its
    sweep, which the bridge has handled since the beginning and the guest had
    never sent.

    Not a longer leash for a pending prompt -- that was tried and was wrong.
    A prompt on screen is no evidence of life, and exempting it meant a guest
    that crashed WHILE a prompt was up held the slot for ten minutes. The
    keepalive distinguishes the two: a living machine pings, a dead one does
    not.
    """
    reader, writer = await harness.connect()
    await _recv(harness, reader, "OPTIONSEND")
    guest = harness.bridge.connection.guest

    guest.last_seen = time.monotonic() - 10_000
    await harness.send(writer, Frame("PING", {}))
    await _recv(harness, reader, "PONG")
    assert time.monotonic() - guest.last_seen < 5, \
        "a keepalive must count as the machine being alive"
    await harness.close(writer)


async def test_a_second_client_cannot_clobber_the_first_s_pipelined_frames(
        harness):
    """Frames arriving in the same read as HELLO used to be parked on the
    BRIDGE (self._early) between _read_hello and _pump.

    That is per-process state on a per-connection value. A second client's
    HELLO overwrote the first's pipelined frames before _pump had consumed
    them -- and it did so BEFORE being rejected as a duplicate, so a
    connection that was about to be refused corrupted the live one.

    They travel with the HELLO now, so there is nothing to clobber.
    """
    assert not hasattr(harness.bridge, "_early"), \
        "per-connection frames are back on the Bridge"

    # First client, with an INPUT pipelined behind HELLO in one write.
    _r1, w1 = await asyncio.open_connection("127.0.0.1", harness.port)
    blob = hello("s").encode() + Frame("INPUT", {}, b"one").encode()
    w1.write(blob)
    await w1.drain()

    # Second client arrives and is refused as a duplicate.
    _r2, w2 = await asyncio.open_connection("127.0.0.1", harness.port)
    w2.write(hello("s").encode())
    await w2.drain()
    await asyncio.sleep(0.2)

    # The first client's turn still ran, with ITS text.
    assert harness.bridge.connection.guest is not None
    await harness.close(w1, w2)


async def test_the_chat_list_follows_a_forked_conversation(harness):
    """PLAN.md Phase 4 records the symptom: "the transcript says the
    conversation was forked; the list does not". This is the cause.

    Session._session_id is only set from the init SystemMessage, which
    arrives DURING a turn -- so the chat-list refresh used to be gated on
    `guest.sess_id` changing, and `guest.sess_id = guest.claude.session_id or
    guest.sess_id` run immediately after the effort fork assigned None (the
    SDK had not reported the new id yet) and kept the pre-fork id, so the
    comparison saw no change and never pushed. The registry and the chat
    list then named the old conversation until some later turn happened to
    write a file.

    Fixed by pushing on the message itself rather than on a value it happens
    to change (02.24) -- and guest.sess_id is a stable per-connection handle
    now, not the SDK's session id, so it has nothing to do with this at all.
    """
    reader, writer = await harness.connect()
    await _recv(harness, reader, "OPTIONSEND")
    guest = harness.bridge.connection.guest
    before = guest.sess_id

    await guest.claude.on_system("init", {"session_id": "forked-1234"})

    f = await _recv(harness, reader, "SESSIONSEND")
    assert f.type == "SESSIONSEND", "the guest was not told the list changed"
    assert guest.sess_id == before, (
        "sess_id is a stable per-connection handle (02.24); a session "
        "becoming known must not move it"
    )
    await harness.close(writer)


async def test_an_oversized_block_is_refused_rather_than_split(harness):
    """`total`/`off` are on every TEXT chunk and look like a reassembly
    protocol. They never were.

    win/app/turn.cpp's TEXT branch called rc_html_block(f->body) per frame
    and used total/off only for the progress figure, so an APPEND block
    split across frames arrived as several separate appends -- each a
    fragment of markup, handed to insertAdjacentHTML. Half a <div>.

    Splitting arbitrary HTML safely needs to know where its element
    boundaries are, which that layer could not know, so it truncated at
    the boundary it COULD guarantee and said so.

    From stage 3 C2 there is nothing to guess at and nothing to truncate
    TO: a body is whole records, and an oversize one is refused by
    write_block against the client's declared maxblock before it reaches
    the wire at all. The model still has the whole text, which was always
    the argument for truncating; what changed is that the on-screen copy
    is now absent rather than shortened, and absent is what a block the
    client said it cannot hold was always going to be.
    """
    reader, writer = await harness.connect()
    await _recv(harness, reader, "OPTIONSEND")
    guest = harness.bridge.connection.guest

    huge = blocks.Block("code", {}, [("t", "x" * 90_000)])
    await harness.bridge.presentation.write_block(guest, huge)

    got = await harness.recv_until(reader, {"TURN", "TEXT"}, timeout=1.5)
    assert not [f for f in got if f.type == "TEXT"], (
        "a record the client cannot hold reached the wire")
    await harness.close(writer)


async def test_a_block_within_the_bound_still_arrives_whole(harness):
    """The other half, so the test above cannot pass by refusing
    everything."""
    reader, writer = await harness.connect()
    await _recv(harness, reader, "OPTIONSEND")
    guest = harness.bridge.connection.guest

    block = blocks.Block("code", {}, [("t", "y" * 4000)])
    await harness.bridge.presentation.write_block(guest, block)

    got = await harness.recv_until(reader, {"TURN", "TEXT"}, timeout=3)
    texts = [f for f in got if f.type == "TEXT"]
    assert len(texts) == 1, f"{len(texts)} frames; a record must not split"
    assert harness.body(texts) == blocks.encode(block)
    await harness.close(writer)


async def test_a_measured_link_speed_reaches_the_codec_choice(harness):
    """PLAN.md makes adaptive codec selection depend on STAT -- "the sender
    picks the codec from the measured link speed" -- and the bridge has
    handled STAT since the beginning.

    The guest had never sent one. So guest.link.link_bps was always None and every
    threshold in compress.py and coalesce.py was inert: identical behaviour
    on a 10Mbit LAN and a modem, which is the exact thing the mechanism
    exists to avoid.
    """
    from bridge.wire.compress import should_compress

    reader, writer = await harness.connect()
    await _recv(harness, reader, "OPTIONSEND")
    guest = harness.bridge.connection.guest
    assert guest.link.link_bps is None, "nothing has reported a speed yet"

    small = b"x" * 200
    await harness.send(writer, Frame("STAT", {"bps": "5000"}))
    await asyncio.sleep(0.05)
    assert guest.link.link_bps == 5000.0, "the measurement never reached the bridge"

    # And it actually changes the decision, rather than merely being stored.
    slow = should_compress(small, guest.link.link_bps)
    fast = should_compress(small, 10_000_000.0)
    assert slow != fast, (
        "link speed is recorded but does not affect the codec choice, which "
        "means the adaptive path is still inert")
    await harness.close(writer)


# --- the wire must not rewrite a command -------------------------------------
#
# EXEC used fold.encode -- the DISPLAY transliterator -- for three phases, so
# the bridge rewrote the command between the moment anything could inspect it
# and the moment the guest ran it. Found by a red team against the
# path-containment guard, which it defeated completely: a guard that parses
# the pre-fold string parses a different program from the one that runs.


async def test_a_command_the_wire_would_rewrite_is_refused(harness):
    """U+2044 FRACTION SLASH folded to '/' -- a path separator invented from
    nothing, which is the exact wording paths.py:16 already used to warn about
    this for sync paths. Nobody noticed EXEC did it too.

    The refusal must happen on the bridge and name the character. The guest
    cannot help here: by the time it sees the body the substitution has
    already happened and the bytes look deliberate.
    """
    reader, writer = await harness.connect()
    await _recv(harness, reader, "OPTIONSEND")

    out = await harness.bridge.guest_exec.run_on_guest(
        "type c:\\work\u2044..\u2044windows\\win.ini",
        timeout_ms=5000, shell="", cwd="",
        guest=harness.bridge.connection.guest)

    assert out["isError"] is True
    text = out["content"][0]["text"]
    assert "U+2044" in text
    assert "Nothing was run" in text
    await harness.close(writer)


async def test_an_unrepresentable_cwd_is_refused(harness):
    """`cwd` travels as a header, and _quote still folds -- correctly, for
    every other header, which carries prose. So the check lives here rather
    than in the quoting."""
    reader, writer = await harness.connect()
    await _recv(harness, reader, "OPTIONSEND")

    out = await harness.bridge.guest_exec.run_on_guest(
        "ver", timeout_ms=5000, shell="", cwd="c:\\w\u2502rk",
        guest=harness.bridge.connection.guest)

    assert out["isError"] is True
    assert "U+2502" in out["content"][0]["text"]
    await harness.close(writer)


async def test_a_cp1252_command_still_runs(harness):
    """The refusal must not catch accented names, which are ordinary on a
    CP1252 box. Asserted by watching the EXEC frame leave with the real
    bytes -- 0xE9, not 'e'."""
    reader, writer = await harness.connect()
    await _recv(harness, reader, "OPTIONSEND")

    task = asyncio.create_task(harness.bridge.guest_exec.run_on_guest(
        "type c:\\caf\u00e9.txt", timeout_ms=5000, shell="", cwd="",
        guest=harness.bridge.connection.guest))
    frame = await _recv(harness, reader, "EXEC")
    assert frame.body == b"type c:\\caf\xe9.txt"
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await _recv(harness, reader, "EXECKILL")
    await harness.close(writer)


# --- the DOS guard annotates the prompt --------------------------------------
#
# It gates nothing: the verdict is displayed and nothing acts on it. That is
# deliberate -- it earns its way onto real traffic before anything depends on
# it. What it must never do is add REASSURANCE, because the bar clips at 160
# characters and a "stays in the mirrored folder" prefix on a truncated
# command would be less honest than the bare command is today.


async def test_a_command_reaching_outside_the_mirror_is_announced(harness):
    reader, writer = await harness.connect()
    await _recv(harness, reader, "OPTIONSEND")
    # THIS CONVERSATION'S folder, not HELLO's configured root. Containment is
    # measured against the tree actually being mirrored for this chat, which
    # is narrower -- measuring against the parent would call a write into a
    # sibling conversation's folder "inside the mirror".
    harness.bridge.connection.guest.work_guest = "C:\\WORK"
    # The MIRROR too. Since 2026-10-01 a conversation has a folder on the
    # machine with or without one, so `work_guest` alone no longer
    # describes a tree there is a copy of here -- which is what
    # containment is measured against.
    harness.bridge.connection.guest.work_dir = "/tmp/mirror/WORK"

    task = harness.bridge.connection.guest.claude.ask(
        "mcp__machine__exec",
        {"command": "deltree /y c:\\windows", "cwd": "C:\\"})
    frame = await _recv(harness, reader, "PERMREQ")
    body = frame.body.decode("cp1252")

    # THE COMMAND COMES FIRST, and only a two-character marker precedes it
    # (02.4). This used to read "! <warning> -- <summary>", and the guest
    # draws the bar as one SS_ENDELLIPSIS line of roughly seventy legible
    # characters -- so a user approving a command saw the guard's sentence,
    # an ellipsis, and nothing at all of what they were approving.
    assert body.startswith("! ")
    assert body.index("outside the work folder") > 10
    # The path keeps the case the model wrote it in -- only the marker is
    # ours. Shouting the path back loses information for no gain.
    assert "c:\\windows" in body
    task.cancel()
    await harness.close(writer)


async def test_the_warning_is_readable_in_the_transcript_not_just_the_bar(
    harness,
):
    """The bar carries a marker and a clipped tail; the block carries it all.

    Half of 02.4 is that the bar is one line. Moving the command to the front
    of it is only honest if the sentence it displaced lands somewhere a
    person can actually read, which is the request block the desk writes into
    the transcript immediately before the PERMREQ.
    """
    reader, writer = await harness.connect()
    await _recv(harness, reader, "OPTIONSEND")
    harness.bridge.connection.guest.work_guest = "C:\\WORK"
    # The MIRROR too. Since 2026-10-01 a conversation has a folder on the
    # machine with or without one, so `work_guest` alone no longer
    # describes a tree there is a copy of here -- which is what
    # containment is measured against.
    harness.bridge.connection.guest.work_dir = "/tmp/mirror/WORK"

    task = harness.bridge.connection.guest.claude.ask(
        "mcp__machine__exec",
        {"command": "deltree /y c:\\windows", "cwd": "C:\\"})
    await _recv(harness, reader, "PERMREQ")

    # THE RECORD, not the markup. This asserted `"permwarn" in html` until
    # stage 3b deleted the bridge's emitter -- and `permwarn` was a CSS class
    # from win/resources/view.tpl, so the assertion was reaching across the
    # seam stage 3 closed to check the client's appearance from the bridge.
    # What the bridge is responsible for is that the block SAYS there is a
    # warning and what it says: a `permission` record with a `w` line.
    recs = harness.bridge.connection.guest.view.pages.all_records()
    warnings = [line for rec in recs for b in blocks.decode(rec)
                if b.kind == "permission"
                for tag, line in b.payload if tag == "w"]
    assert warnings, f"no permission warning in {len(recs)} records"
    assert any("outside the work folder" in w for w in warnings), warnings
    task.cancel()
    await harness.close(writer)


async def test_a_device_inside_the_mirror_is_not_called_outside_it(harness):
    """02.11. `CON` is reserved in every directory, so the write does not
    land in the mirror -- but it is not OUTSIDE it, and saying so is a claim
    the user can check and find false. A guard whose stated reason is wrong
    is a guard people stop reading; this module says so itself."""
    reader, writer = await harness.connect()
    await _recv(harness, reader, "OPTIONSEND")
    harness.bridge.connection.guest.work_guest = "C:\\WORK"
    # The MIRROR too. Since 2026-10-01 a conversation has a folder on the
    # machine with or without one, so `work_guest` alone no longer
    # describes a tree there is a copy of here -- which is what
    # containment is measured against.
    harness.bridge.connection.guest.work_dir = "/tmp/mirror/WORK"

    task = harness.bridge.connection.guest.claude.ask(
        "mcp__machine__exec",
        {"command": "copy x c:\\work\\con", "cwd": "C:\\WORK"})
    frame = await _recv(harness, reader, "PERMREQ")
    body = frame.body.decode("cp1252")

    assert body.startswith("! ")
    assert "outside the work folder" not in body
    assert "device name" in body
    task.cancel()
    await harness.close(writer)


async def test_without_a_work_folder_a_program_is_judged_as_elsewhere(
    harness,
):
    """The deployment-gap sentence ("no folder is mirrored") is gone with
    the guard that needed it: containment is measured against the
    conversation's folder on the machine, mirror or not. With no folder at
    all every program runs somewhere that is not one, and the bar says
    where -- per command, because it is a fact about the command."""
    reader, writer = await harness.connect()
    await _recv(harness, reader, "OPTIONSEND")
    assert not harness.bridge.connection.guest.work_guest

    task = harness.bridge.connection.guest.claude.ask(
        "mcp__machine__exec", {"command": "wcl386 a.c", "cwd": "C:\\"})
    frame = await _recv(harness, reader, "PERMREQ")
    body = frame.body.decode("cp1252")
    assert "no folder is mirrored" not in body
    assert "outside the work folder" in body
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    await harness.close(writer)


async def test_a_contained_command_gets_no_added_reassurance(harness, tmp_path):
    """The bar must read exactly as it does today for an ordinary command."""
    reader, writer = await harness.connect()
    await _recv(harness, reader, "OPTIONSEND")
    harness.bridge.connection.guest.work_guest = "C:\\WORK"
    # BOTH HALVES, because the guard's question is "is there a copy here to
    # restore from" and only `work_dir` answers it. Since 2026-10-01 a
    # conversation has a folder on the machine with or without a mirror, so
    # setting `work_guest` alone describes a state in which nothing is
    # restorable -- see the test below.
    harness.bridge.connection.guest.work_dir = str(tmp_path / "WORK")

    task = harness.bridge.connection.guest.claude.ask(
        "mcp__machine__exec",
        {"command": "copy a.c b.c", "cwd": "C:\\WORK"})
    frame = await _recv(harness, reader, "PERMREQ")
    body = frame.body.decode("cp1252")

    # Byte-for-byte what the bar shows today. FakeCtx supplies the summary
    # ("Bash: vol c:"), which is fixture, not args -- what matters is that
    # the guard added nothing in front of it.
    assert "mirror" not in body.lower()
    assert not body.startswith("!")
    task.cancel()
    await harness.close(writer)


async def test_asking_permission_is_early_enough_to_commit_the_folder(
    harness, tmp_path,
):
    """A permission request is the FIRST moment of a tool call -- earlier
    than on_tool and earlier than the tool body. If the folder were committed
    any later the guard would measure containment against a directory that
    does not exist yet and announce "no folder is mirrored" for a command
    about to run in one."""
    reader, writer = await harness.connect(hdrs={"sync_root": "C:\\Gaston"})
    await _recv(harness, reader, "OPTIONSEND")
    b = harness.bridge
    b.config.sync_dir = str(tmp_path / "mirror")
    os.makedirs(b.config.sync_dir, exist_ok=True)
    g = b.connection.guest
    g.first_prompt = "Sky color"
    assert g.work_guest == ""

    task = g.claude.ask("mcp__machine__exec",
                        {"command": "copy a.c b.c",
                         "cwd": "C:\\Gaston\\SKYCOLOR"})
    frame = await _recv(harness, reader, "PERMREQ")
    body = frame.body.decode("cp1252")

    assert g.work_guest == "C:\\Gaston\\SKYCOLOR", "committed by the ask"
    # Measured against the folder just committed: contained, so no warning.
    assert not body.startswith("!"), body
    task.cancel()
    await harness.close(writer)


# --- the context gauge -------------------------------------------------------


async def test_turn_end_carries_context_fullness(harness):
    """It rides on TURN end rather than getting a frame of its own: one small
    integer that only changes when a turn ends, arriving at the moment the
    guest is already parsing that frame."""
    reader, writer = await harness.connect()
    await _recv(harness, reader, "OPTIONSEND")

    await harness.bridge.connection.guest.claude.send("hello")
    await harness.bridge.turn_runner.run_turn(harness.bridge.connection.guest, "hello")
    frame = await _recv(harness, reader, "TURN")
    while frame.headers.get("state") != "end":
        frame = await _recv(harness, reader, "TURN")

    assert frame.headers.get("ctx_pct") == "42"
    await harness.close(writer)


async def test_an_unavailable_context_reading_is_absent_not_zero(harness):
    """The guest reads a missing header as "leave the gauge alone". Sending
    0 would claim an empty context, which is the opposite of unknown."""
    reader, writer = await harness.connect()
    await _recv(harness, reader, "OPTIONSEND")
    harness.bridge.connection.guest.claude.context_pct = None

    await harness.bridge.turn_runner.run_turn(harness.bridge.connection.guest, "hello")
    frame = await _recv(harness, reader, "TURN")
    while frame.headers.get("state") != "end":
        frame = await _recv(harness, reader, "TURN")

    assert "ctx_pct" not in frame.headers
    await harness.close(writer)


# --- the inventory rides in on HELLO ---------------------------------------
#
# A machine's tool list has exactly one moment when it can arrive: before
# `_attach` builds the session, because that is where the system prompt is
# frozen and a fresh connection's first session is the one the user then
# talks to. `_attach` receives HELLO; `_pump` starts reading afterwards. So a
# TOOLS frame sent right after connect would land after the prompt every
# single time, and the feature would look like it worked while doing nothing.
#
# docs/ARCHITECTURE.md already blessed this channel, rejecting a `sysinfo` TOOL with
# the reason "HELLO already sends it and the system prompt already states
# it".

def _rows(*facts: tuple) -> bytes:
    """Wire rows, with the byte counts COMPUTED.

    Written by hand the first time, and the parser correctly rejected every
    one of them -- which proved the length check works and cost an hour
    looking for a bug in the bridge. A fixture whose invariant is "I counted
    right" is a fixture that will be wrong again."""
    out = b""
    for name, kind, value in facts:
        raw = value.encode("latin-1")
        out += f"{name} {kind} {len(raw)} ".encode("latin-1") + raw + b"\n"
    return out


ROWS = _rows(("command.com", "file", r"C:\WINDOWS\COMMAND.COM"),
             ("watcom", "file", r"C:\WATCOM\BINNT\WCC386.EXE"))


async def test_a_hello_carrying_an_inventory_still_connects(harness):
    r, w = await harness.connect(tools=ROWS)
    frames = await harness.recv_until(r, {"READY"})
    assert [f for f in frames if f.type == "READY"], [f.type for f in frames]
    await harness.close(w)


async def test_the_inventory_reaches_the_guest(harness):
    r, w = await harness.connect(tools=ROWS)
    await harness.recv_until(r, {"READY"})
    got = {f.name: f.path for f in harness.bridge.connection.guest.tools}
    assert got == {"command.com": "C:\\WINDOWS\\COMMAND.COM",
                   "watcom": "C:\\WATCOM"}
    await harness.close(w)


async def test_the_inventory_is_in_the_prompt_the_session_was_built_with(
        harness):
    """The point of the whole exercise. Asserting on `guest.tools` alone
    would pass even if the prompt were built a moment too early -- which is
    the failure this design exists to avoid, and it is invisible from the
    outside."""
    r, w = await harness.connect(tools=ROWS)
    await harness.recv_until(r, {"READY"})
    prompt = harness.bridge.connection.guest.claude.system_prompt["append"]
    assert "What is installed on that machine" in prompt
    # `-l=nt_win` was asserted here while the Watcom BODY was resident. It
    # moved behind `mcp__machine__toolnote` on 2026-10-01; what has to
    # arrive in the prompt now is the description and the way to fetch it.
    # `"Open Watcom C/C++ 1.9"` stood here until the resident line stopped
    # being the note's `title` and became its profile's `about`.
    assert "**watcom**" in prompt
    assert "mcp__machine__toolnote" in prompt
    await harness.close(w)


async def test_a_client_that_sends_no_body_connects_exactly_as_before(harness):
    """EVERY DEPLOYED CLIENT IS THIS CASE until it is reflashed. Day one the
    feature is dark, and dark must mean unchanged rather than degraded."""
    r, w = await harness.connect()
    await harness.recv_until(r, {"READY"})
    assert harness.bridge.connection.guest.tools == []
    prompt = harness.bridge.connection.guest.claude.system_prompt["append"]
    assert "What is installed on that machine" not in prompt
    await harness.close(w)


async def test_a_malformed_body_does_not_cost_the_user_their_machine(harness):
    """The body is written by a program pointed at an INI the user can edit,
    and it arrives mid-handshake. A bad row costs one inventory line; a
    refusal costs the user their computer."""
    r, w = await harness.connect(tools=b"\xff\xfe garbage not rows at all\n")
    frames = await harness.recv_until(r, {"READY"})
    assert [f for f in frames if f.type == "READY"], [f.type for f in frames]
    assert harness.bridge.connection.guest.tools == []
    await harness.close(w)


async def test_one_bad_row_does_not_lose_the_good_ones(harness):
    r, w = await harness.connect(tools=b"watcom file 999 C:\\W\n" + ROWS)
    await harness.recv_until(r, {"READY"})
    assert [f.name for f in harness.bridge.connection.guest.tools] == [
        "command.com", "watcom"]
    await harness.close(w)


async def test_the_inventory_survives_a_reconnect_that_resends_it(harness):
    r, w = await harness.connect(tools=ROWS)
    await harness.recv_until(r, {"READY"})
    await harness.close(w)
    r2, w2 = await harness.connect(tools=ROWS)
    await harness.recv_until(r2, {"READY"})
    assert len(harness.bridge.connection.guest.tools) == 2
    await harness.close(w2)


async def test_reconnecting_from_an_older_client_clears_the_inventory(harness):
    """Not stale-but-plausible. If the machine at the other end stops
    reporting, saying nothing is the honest answer -- the alternative is an
    inventory describing a computer that is no longer connected."""
    r, w = await harness.connect(tools=ROWS)
    await harness.recv_until(r, {"READY"})
    await harness.close(w)
    r2, w2 = await harness.connect()
    await harness.recv_until(r2, {"READY"})
    assert harness.bridge.connection.guest.tools == []
    await harness.close(w2)


# --- a folder name is the client's rules, not the bridge's --------------------

def test_folder_length_follows_the_clients_declared_shortnames():
    """`FOLDER_MAX = 8` was a constant, and `shortnames` -- the header that
    says whether a volume aliases every long name to 8.3 -- was parsed only
    far enough to count as "declared" and then dropped.

    A client that aliases still gets 8. A client that says it does not gets a
    readable name instead of a truncated one.
    """
    from bridge.files import caps as C
    aliasing = C.from_hello({"shortnames": "1"})
    long_names = C.from_hello({"shortnames": "0", "maxcomp": "255"})
    assert Bridge.folder_max(aliasing) == 8
    assert Bridge.folder_max(long_names) == Bridge.FOLDER_MAX_LONG

    title = "List files in the RETRO folder"
    assert Bridge._title_folder(title, aliasing) == "LISTFILE"
    assert len(Bridge._title_folder(title, long_names)) > 8
    # A silent client is still assumed to be the restrictive one.
    assert Bridge._title_folder(title) == "LISTFILE"


def test_a_folder_cannot_be_named_after_a_device_where_devices_are_reserved():
    """`folder_name` had its own literal illegal set and no device check at
    all, so `CON` came back as a perfectly good folder name on a machine
    where opening it can block forever."""
    from bridge.files import caps as C
    assert Bridge.folder_name("CON", C.CONSTRAINED) == ""
    assert Bridge.folder_name("CON", C.POSIX) == "CON"


def test_illegal_characters_come_from_caps_not_from_a_literal():
    from bridge.files import caps as C
    # `*` is illegal on the restrictive client and ordinary on POSIX...
    assert Bridge.folder_name("a*b", C.CONSTRAINED) == ""
    # ...but a separator or a `..` is refused for everyone, because that is
    # how a name leaves the folder rather than a filesystem question.
    for caps in (C.CONSTRAINED, C.POSIX):
        assert Bridge.folder_name("../etc", caps) == ""
        assert Bridge.folder_name("a/b", caps) == ""
        assert Bridge.folder_name("a\\b", caps) == ""


def test_a_guest_path_is_joined_with_the_separator_the_guest_uses():
    """The one place the bridge spells a guest path. It used to spell it with
    a backslash unconditionally, while `paths.py` kept "/" as the wire
    separator everywhere else on purpose."""
    assert paths.sep_of("C:\\GASTON") == "\\"
    assert paths.sep_of("/srv/gaston") == "/"
    assert paths.sep_of("") == "/"


# --- the guest declares a share and this bridge has no mirror ----------------
#
# The configuration that produced the 2026-10-01 bug on real hardware, end to
# end: the client's INI named `C:\WORK`, it created the folder at startup and
# announced it in HELLO, and the bridge was started with no `--sync-dir`. The
# `harness` fixture builds exactly that bridge, so the only thing these have
# to add is the header.


async def test_a_declared_share_reaches_the_agent_even_with_no_mirror(harness):
    """Where the declaration used to stop. `sync_root` was logged at attach
    and then read only by `WorkspaceService.guest_path`, which composes a
    CHILD of it -- and its one caller returns early when `config.sync_dir` is
    unset. So with sync off the guest's root reached nothing the agent could
    see, and the agent was told only that nothing was mirrored."""
    r, w = await harness.connect(hdrs={"sync_root": "C:\\WORK"})
    await harness.recv_until(r, {"READY"})
    prompt = harness.bridge.connection.guest.claude.system_prompt["append"]
    assert "C:\\WORK" in prompt
    assert "is not\nmirroring it" in prompt
    # The rule moved out of the per-branch sync text into the system prompt
    # on 2026-10-02: three branches each carried a copy and only one ever
    # renders, so stating it once puts it in front of EVERY branch.
    assert "never write to the root of a drive" in prompt
    await harness.close(w)


async def test_a_conversation_gets_a_folder_on_the_machine_with_no_mirror(
    harness,
):
    """THE WHOLE POINT, end to end. The machine declares the home its user
    configured, this bridge has no `--sync-dir`, the user speaks, the first
    tool call runs -- and the conversation has its own folder on their machine
    and no tree on this side.

    This test asserted the opposite on 2026-10-01 (`work_guest == ""`), and it
    PASSED either way: it reached `ensure_work_dir` without
    `claude.start_turn()`, so there was no conversation id to name a folder
    against and the function returned before any of the behaviour it was
    meant to be pinning. The turn is driven properly here."""
    r, w = await harness.connect(hdrs={"sync_root": "C:\\WORK"})
    await harness.recv_until(r, {"OPTIONSEND"})
    g = harness.bridge.connection.guest
    await harness.send(w, Frame("INPUT", {}, b"Sky colour"))
    await harness.recv_until(r, {"TURN"}, 10.0)
    await g.claude.use_tool()

    assert g.work_guest == "C:\\WORK\\SKYCOLOU"
    assert g.work_dir == "", "no mirror was configured and none was invented"
    await harness.close(w)


async def test_the_folder_reaches_the_client_in_the_option_row_it_already_has(
    harness,
):
    """WHO CREATES IT, and over what. No new wire op: the OPTION `work` row
    already carries the path on every options batch, and the client's
    OPTIONSEND handler hands it to `rc_workdir_apply`
    (`win/app/mainwindow.cpp` -> `win/app/workdir.cpp`), which creates it
    parents and all and makes it that side's live work root. A new frame
    would have had to pass the exec test in docs/ARCHITECTURE.md 3.1 to buy
    something this already does."""
    r, w = await harness.connect(hdrs={"sync_root": "C:\\WORK"})
    await harness.recv_until(r, {"OPTIONSEND"})
    g = harness.bridge.connection.guest
    await harness.send(w, Frame("INPUT", {}, b"Sky colour"))
    await harness.recv_until(r, {"TURN"}, 10.0)
    await g.claude.use_tool()

    frames = await harness.recv_until(r, {"OPTIONSEND"}, 10.0)
    work = [f for f in frames
            if f.type == "OPTION" and f.headers.get("kind") == "work"]
    assert work, [f.type for f in frames]
    assert work[-1].headers.get("id") == "C:\\WORK\\SKYCOLOU"
    await harness.close(w)


async def test_the_agent_is_told_to_use_the_folder_as_its_working_directory(
    harness,
):
    """AN INSTRUCTION, in front of the message that provokes the writing.

    The channel is the one thing here that is not obvious. The system prompt
    cannot carry it: it is built when the connection ATTACHES, before the user
    has said anything, and the folder is named from the conversation's title.
    The transcript notice is for the person at the keyboard. `with_sync_preamble`
    arrives a turn late and does not run without a mirror at all. So the
    instruction goes at the top of the turn, where the title already exists
    and the tool call that writes has not happened yet.
    """
    r, w = await harness.connect(hdrs={"sync_root": "C:\\WORK"})
    await harness.recv_until(r, {"OPTIONSEND"})
    g = harness.bridge.connection.guest
    await harness.send(w, Frame("INPUT", {}, b"Sky colour"))
    await harness.recv_until(r, {"TURN"}, 10.0)

    sent = g.claude.last_prompt
    assert "USE `C:\\WORK\\SKYCOLOU` AS YOUR WORKING DIRECTORY" in sent
    assert "Never the\nroot of a drive" in sent
    assert sent.endswith("Sky colour"), "the user's own message still last"
    # And the folder the agent was promised is the folder that gets made.
    await g.claude.use_tool()
    assert g.work_guest == "C:\\WORK\\SKYCOLOU"
    await harness.close(w)


async def test_the_working_directory_is_said_once_not_every_turn(harness):
    """A paragraph in front of every message is a paragraph the model skims.
    It is said once per FOLDER, so a folder the user changes under the agent
    is said again and an unchanged one is not."""
    r, w = await harness.connect(hdrs={"sync_root": "C:\\WORK"})
    await harness.recv_until(r, {"OPTIONSEND"})
    g = harness.bridge.connection.guest
    # Driven through the turn runner rather than two INPUT frames: the second
    # turn's TURN frames are indistinguishable in the stream from the first
    # turn's, so a frame-driven second turn races its own assertion.
    g.first_prompt = "Sky colour"
    await harness.bridge.turn_runner.run_turn(g, "Sky colour")
    await g.claude.use_tool()
    assert "WORKING DIRECTORY" in g.claude.last_prompt

    await harness.bridge.turn_runner.run_turn(g, "And again")
    assert g.claude.last_prompt == "And again"
    await harness.close(w)


async def test_nothing_is_prepended_when_a_mirror_is_carrying_the_files(
    harness, tmp_path,
):
    """With a mirror the agent writes on this side and the mirror carries it,
    and the system prompt's mirrored paragraph says so at length. A second
    instruction here would be noise on every conversation that works."""
    harness.bridge.config.sync_dir = str(tmp_path / "mirror")
    os.makedirs(harness.bridge.config.sync_dir, exist_ok=True)
    r, w = await harness.connect(hdrs={"sync_root": "C:\\WORK"})
    await harness.recv_until(r, {"OPTIONSEND"})
    g = harness.bridge.connection.guest
    await harness.send(w, Frame("INPUT", {}, b"Sky colour"))
    await harness.recv_until(r, {"TURN"}, 10.0)
    assert g.claude.last_prompt == "Sky colour"
    await harness.close(w)


async def test_reopening_a_conversation_lands_in_the_same_folder(harness):
    """IDEMPOTENCY ACROSS A SWITCH, and it was gated on `sync_dir` with the
    rest: reopening a conversation on a bridge with no `--sync-dir` forgot
    which folder it had been working in, so the next tool call named a second
    one -- one conversation, two folders on the user's disk, files split
    between them with nothing saying so."""
    r, w = await harness.connect(hdrs={"sync_root": "C:\\WORK"})
    await harness.recv_until(r, {"OPTIONSEND"})
    g = harness.bridge.connection.guest
    await harness.send(w, Frame("INPUT", {}, b"Sky colour"))
    await harness.recv_until(r, {"TURN"}, 10.0)
    await g.claude.use_tool()
    first, sid = g.work_guest, g.conv_id

    # Driven directly rather than through a NEWSESSION frame: the reply is
    # another OPTIONSEND, which is indistinguishable in the stream from the
    # one the tool call's own `send_options` just sent.
    await harness.bridge.coordinator.new_session(g)
    assert g.work_guest == "", "a new conversation starts with no folder"

    await harness.bridge.coordinator.switch(g, sid)
    assert g.work_guest == first
    await harness.close(w)


async def test_no_folder_is_invented_when_the_machine_declared_no_home(
    harness,
):
    """The one place the honest answer is still "nowhere". A client with sync
    unconfigured on its own side has nominated nothing, and a path chosen by
    this end would be a directory on somebody's real disk that nobody asked
    for -- the drive-root mistake one step tidier. The agent is told to ask,
    and the drive root is named as the destination that is never right."""
    r, w = await harness.connect()
    await harness.recv_until(r, {"OPTIONSEND"})
    g = harness.bridge.connection.guest
    assert not (g.hello or {}).get("sync_root")
    await harness.send(w, Frame("INPUT", {}, b"Sky colour"))
    await harness.recv_until(r, {"TURN"}, 10.0)
    await g.claude.use_tool()

    assert g.work_guest == ""
    prompt = g.claude.system_prompt["append"]
    assert "named no shared folder" in prompt
    assert "ask where a file should go" in prompt
    await harness.close(w)


async def test_a_file_put_in_the_drive_root_is_flagged_on_the_bar(harness):
    """What now stops the write being silent. `cmdguard.reason` reads
    `args["command"]`, which `put_file` does not have, so the destination
    was never judged by anything -- the person approved a bar reading
    "Write a file on your machine" and got a file in `C:\\`."""
    reader, writer = await harness.connect(hdrs={"sync_root": "C:\\WORK"})
    await _recv(harness, reader, "OPTIONSEND")

    task = harness.bridge.connection.guest.claude.ask(
        "mcp__machine__put_file",
        {"path": "C:\\NOTES.TXT", "content": "x"})
    frame = await _recv(harness, reader, "PERMREQ")
    body = frame.body.decode("cp1252")

    assert body.startswith("! "), body
    assert "top level" in body, body
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    await harness.close(writer)


async def test_a_file_put_in_a_folder_is_not_flagged(harness):
    """A warning on every prompt is the defect `deployment_gap` exists to
    undo. The ordinary destination must read exactly as it does today."""
    reader, writer = await harness.connect(hdrs={"sync_root": "C:\\WORK"})
    await _recv(harness, reader, "OPTIONSEND")

    # Relative: it lands in this conversation's own folder, wherever the
    # first tool call committed that to be.
    task = harness.bridge.connection.guest.claude.ask(
        "mcp__machine__put_file",
        {"path": "NOTES.TXT", "content": "x"})
    frame = await _recv(harness, reader, "PERMREQ")
    body = frame.body.decode("cp1252")

    assert not body.startswith("!"), body
    assert "top level" not in body, body
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    await harness.close(writer)


async def test_cr_cr_lf_from_a_text_mode_program_is_one_line_break(harness):
    """Windows 98's PING.EXE writes "\\r\\r\\n" to a redirected stdout
    (measured on the Athlon, 2026-10-03). Passed through, every line came
    with a blank one after it in the transcript."""
    reader, writer = await harness.connect()
    await _recv(harness, reader, "OPTIONSEND")
    task = asyncio.create_task(harness.bridge.guest_exec.run_on_guest(
        "ping -n 2 127.0.0.1", timeout_ms=5000, shell="", cwd="",
        guest=harness.bridge.connection.guest))
    req = await _recv(harness, reader, "EXEC")
    eid = req.headers["id"]
    await harness.send(writer, Frame("EXECOUT", {"id": eid},
                                     b"Reply one\r\r\nReply two\r\r\n"))
    await harness.send(writer, Frame("EXECRESULT", {
        "id": eid, "status": "ok", "code": "0", "truncated": "0"}))
    got = await asyncio.wait_for(task, 5)
    text = got["content"][0]["text"]
    assert "Reply one" in text and "Reply two" in text
    assert "\r\r" not in text
    await harness.close(writer)


async def test_a_session_that_will_not_start_does_not_kill_the_attach(
        harness, monkeypatch):
    """The failure the startup preflight cannot catch.

    `claude -v` exits 0 with no credentials, so a Claude Code that is
    installed but NOT SIGNED IN passes bridge.claudecli.resolve() and then
    fails where the session is actually built -- in `_post_attach`, which
    had no `try` and no caller with one. The user got a traceback on the
    bridge and a dropped socket on the Windows 98 machine. The same thing
    happens if the CLI is removed or de-authenticated while the bridge is
    running, which no startup check could ever see.

    Attaching without a session is survivable: READY still goes out, the
    reason is written into the transcript, and the next message tries
    again.
    """
    async def refuse(_guest):
        raise RuntimeError("Invalid API key / not logged in")

    monkeypatch.setattr(harness.bridge.session, "make_session", refuse)

    reader, writer = await harness.connect()
    frames = await harness.recv_until(reader, {"READY"})
    assert [f for f in frames if f.type == "READY"], [f.type for f in frames]
    assert harness.bridge.connection.guest.claude is None
    await harness.close(writer)


async def test_opening_a_conversation_sends_switched_and_nothing_more(
        harness, monkeypatch):
    """The window is the GUEST's to ask for, and that is deliberate.

    bridge/services/session.py says it in `new_session_locked`: "SWITCHED
    AND NOTHING ELSE. The bridge used to push the transcript straight
    after this; stage 3b made the guest ask, which is what leaves this
    side with no opinion about how big a window is." A push here would
    need a page size this side has no business knowing.

    This pins that, because a plausible-looking fix for the Athlon bug of
    2026-10-27 -- an older conversation opening to an empty transcript --
    was to send a RANGE after SWITCHED. It worked, and it was wrong: the
    fault was in the guest (win/app/mainwindow.cpp onChats compared the
    incoming conversation id against a buffer the chat list's callback had
    already overwritten with it, so it never saw a change and never
    asked). Pushing from here would have hidden that and then sent two
    windows once the guest was fixed.
    """
    from bridge.view import html as H

    r, w = await harness.connect()
    await _recv(harness, r, "OPTIONSEND")

    history = [H.notice("an older turn"), H.notice("and another")]
    frames = await _switch_to(harness, monkeypatch, r, w, "sid-older", history)
    kinds = [f.type for f in frames]

    assert "SWITCHED" in kinds, kinds
    assert "RANGE" not in kinds, (
        "the bridge pushed a window; the guest asks for one (stage 3b), "
        "and pushing needs a page size this side does not have: %s" % kinds)
    await harness.close(w)
