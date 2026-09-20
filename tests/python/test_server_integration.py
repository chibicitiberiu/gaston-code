"""End-to-end over the real wire protocol, with Claude mocked out.

Covers the parts that are awkward to test any other way: HELLO negotiation,
duplicate-client rejection, resume after a dropped connection, and pagination
round trips. Mocking the session keeps it deterministic and fast; the real
model is exercised separately by netcheck.exe.
"""

import os
import asyncio
import time
import zlib
from dataclasses import dataclass, field

import pytest

from claude_agent_sdk import (PermissionResultAllow, PermissionResultDeny,
                              PermissionUpdate)

from bridge.wire.compress import StreamDecompressor
from bridge.wire.protocol import (Frame, FrameReader, MAX_FRAME, PROTO_VERSION,
                                  ProtocolError, hello)
from bridge import server as srv
from bridge.server import Bridge


@dataclass
class FakeRule:
    tool_name: str = "Bash"
    rule_content: str | None = "vol:*"


@dataclass
class FakeCtx:
    """Shaped like ToolPermissionContext, with the values the spikes measured.

    Note title=None and blocked_path=None: that is what a real Read of
    /etc/hostname actually produced, and the plan had assumed otherwise.
    """

    tool_use_id: str = "toolu_test_1"
    suggestions: list = field(default_factory=lambda: [
        PermissionUpdate(type="addRules", behavior="allow",
                         destination="session", rules=[FakeRule()])])
    title: str | None = None
    display_name: str | None = "Bash"
    description: str | None = "vol c:"
    blocked_path: str | None = None
    decision_reason: str | None = None
    agent_id: str | None = None
    signal: object | None = None


class FakeSession:
    """Stands in for bridge.session.Session."""

    _seq = 0

    def __init__(self, **kw):
        # What the bridge asked us to resume, so a test can assert that
        # switching a chat built NOTHING and that the first message built the
        # right thing.
        self.resume = kw.get("resume")
        # Where the bridge said to work. A conversation with its own folder
        # has to actually RUN in it, not merely be labelled with it.
        self.cwd = kw.get("cwd")
        self.model = kw.get("model") or "fake-model"
        self.effort = kw.get("effort")
        self.on_text = kw.get("on_text")
        # Captured so a test can drive the FIRST TOOL CALL, which is what
        # commits a conversation to a work folder. Nothing is mirrored before
        # it, so "the folder appears" and "the folder does not appear" are
        # both assertions that need this.
        self.on_tool = kw.get("on_tool")
        # The bridge learns the conversation id from this, not from start().
        self.on_system = kw.get("on_system")
        # Captured so permission tests can drive it. Deliberately NOT called
        # inline from send(): in production the SDK runs it in its own
        # cancellable task (query.py spawn_task), and a fixture that awaits it
        # inline would pass while the cancellation wedge shipped.
        self.can_use_tool = kw.get("can_use_tool")
        # What Session.__init__ is given, and what set_permission_mode
        # changes. Held here rather than ignored so a test can assert the
        # bridge passed the guest's choice into the REBUILT session as well
        # as into the live one.
        self.permission_mode = kw.get("permission_mode", "default")
        self.started = False
        self.interrupted = False
        FakeSession._seq += 1
        self.session_id = f"fake-session-{FakeSession._seq}"

    def ask(self, tool_name="Bash", args=None, **ctx_kw):
        """Spawn a permission request the way the SDK does: a detached task.

        Returns the task, so a test can cancel it and assert the guest still
        gets its PERMEND.
        """
        ctx = FakeCtx(**ctx_kw)
        coro = self.can_use_tool(tool_name, args or {"command": "vol c:"}, ctx)
        return asyncio.create_task(coro)

    async def use_tool(self, name="Bash", args=None):
        """Report a tool call the way the SDK does, so the bridge's own
        `on_tool` runs -- including the work-folder commitment."""
        await self.on_tool(name, args or {"command": "vol c:"})

    async def start(self):
        self.started = True

    async def close(self):
        self.started = False

    async def interrupt(self):
        self.interrupted = True

    async def set_model(self, m):
        self.model = m

    async def set_permission_mode(self, m):
        self.permission_mode = m

    async def server_models(self):
        """Shaped like what the CLI returns, including the "default" alias
        that the menu is expected to resolve away rather than show."""
        return [
            {"value": "default", "resolvedModel": "fake-opus",
             "displayName": "Default (recommended)", "supportsEffort": True,
             "supportedEffortLevels": ["low", "high"]},
            {"value": "opus", "resolvedModel": "fake-opus",
             "displayName": "Opus", "supportsEffort": True,
             "supportedEffortLevels": ["low", "high"]},
            {"value": "haiku", "resolvedModel": "fake-haiku",
             "displayName": "Haiku",
             "description": "Fastest for quick answers"},
        ]

    async def server_commands(self):
        """Shaped like what the CLI returns. Deliberately a MIXTURE: two the
        menu wants, one it wants but this install does not have (/init is
        absent), and two it must never offer."""
        return [
            {"name": "compact", "description": "Free up context",
             "argumentHint": "<options>"},
            {"name": "rename", "description": "Rename the conversation",
             "argumentHint": "[name]"},
            {"name": "recap", "description": "One-line recap",
             "argumentHint": ""},
            {"name": "usage", "description": "Session cost", "argumentHint": ""},
            {"name": "model", "description": "Set the model",
             "argumentHint": "<model>"},
            {"name": "heapdump", "description": "Dump the JS heap to ~/Desktop",
             "argumentHint": ""},
        ]

    async def send(self, prompt):
        from bridge.agent.session import Turn
        # A command the CLI resolves locally streams nothing back. Modelled
        # rather than assumed away: the bridge's "returned nothing" notice is
        # only reachable through a turn that really does produce no output.
        if getattr(self, "silent", False):
            return Turn(ok=True, subtype="success", cost_usd=0.0,
                        num_turns=1, duration_ms=10)
        if self.on_text:
            await self.on_text(f"echo: {prompt}\n")
        return Turn(ok=True, subtype="success", cost_usd=0.01,
                    num_turns=1, duration_ms=5)

    #: Context-window fullness, 0-100. Implemented here rather than letting
    #: the call site use getattr: this fake stands in for the whole Session
    #: interface, and a real Session that stopped answering would be a bug
    #: worth a failing test rather than a silently blank gauge.
    context_pct: int | None = 42

    async def context_usage(self):
        return self.context_pct


class Harness:
    def __init__(self, bridge, port):
        self.bridge = bridge
        self.port = port
        # The guest keeps one persistent inflate stream per connection; so
        # must anything reading this protocol, or frame N+1 is garbage.
        self.unzip = StreamDecompressor()
        self._readers: dict[int, FrameReader] = {}
        self._pending: dict[int, list] = {}

    def body(self, frames) -> bytes:
        """Concatenated TEXT bodies, inflating in stream order.

        Order matters: a persistent deflate stream cannot be decoded out of
        sequence, which is exactly the constraint the guest is under.
        """
        out = []
        for f in frames:
            if f.type != "TEXT":
                continue
            out.append(self.unzip.decompress(f.body)
                       if f.enc == "deflate" else f.body)
        return b"".join(out)

    async def connect(self, secret="s", *, ver=None, cap=None,
                      sess=None, hdrs=None):
        r, w = await asyncio.open_connection("127.0.0.1", self.port)
        # Fresh connection means the bridge resets its compressor, so reset
        # in lockstep. Same for the frame reader.
        self.unzip = StreamDecompressor()
        self._readers.pop(id(r), None)
        self._pending.pop(id(r), None)
        f = hello(secret, sess=sess)
        if ver is not None:
            f.headers["ver"] = str(ver)
        if cap is not None:
            f.headers["max_frame"] = str(cap)
        # Capability headers, for the tests that care which client this is.
        # Absent means the deployed Win9x client's documented defaults, which
        # is what every other test wants.
        if hdrs:
            f.headers.update({k: str(v) for k, v in hdrs.items()})
        w.write(f.encode())
        await w.drain()
        return r, w

    async def recv_until(self, reader, types, timeout=5.0):
        """Collect frames until one of `types` arrives.

        Two things here are per-connection, not per-call, and getting either
        wrong makes the server look like it never sent frames it did send:

        * the FrameReader, because a fresh one discards whatever partial frame
          the previous call had buffered;
        * the pending queue, because several frames usually arrive in a single
          read() and returning on the first match would drop the rest of that
          batch on the floor.
        """
        key = id(reader)
        fr = self._readers.setdefault(key, FrameReader())
        pending = self._pending.setdefault(key, [])
        frames = []

        # Anything left over from a previous call comes first.
        while pending:
            f = pending.pop(0)
            frames.append(f)
            if f.type in types:
                return frames

        async def run():
            while True:
                data = await reader.read(8192)
                if not data:
                    return
                batch = fr.feed(data)
                for i, f in enumerate(batch):
                    frames.append(f)
                    if f.type in types:
                        pending.extend(batch[i + 1:])
                        return

        try:
            await asyncio.wait_for(run(), timeout)
        except (TimeoutError, asyncio.TimeoutError):
            pass
        return frames

    @staticmethod
    async def send(writer, f):
        writer.write(f.encode())
        await writer.drain()

    @staticmethod
    async def close(*writers):
        """Close and let the server-side handler observe EOF.

        Without the yield the handler is still sitting in read() when the
        fixture tears down.
        """
        for w in writers:
            try:
                w.close()
            except Exception:
                pass
        await asyncio.sleep(0.05)


_next_port = [7900]


@pytest.fixture
async def harness(monkeypatch, tmp_path):
    """One bridge on its own port per test.

    Ports are handed out sequentially rather than reused, because a socket in
    TIME_WAIT from the previous test makes bind() fail intermittently and that
    reads as a flaky test rather than what it is.
    """
    import bridge.server as srv
    monkeypatch.setattr(srv, "Session", FakeSession)
    # The session registry must not touch the real one in $HOME, and each test
    # needs to start from an empty one or the chat list depends on what ran
    # before it.
    monkeypatch.setenv("GASTON_STATE", str(tmp_path / "sessions.json"))

    server = None
    b = None
    for _ in range(60):
        _next_port[0] += 1
        port = _next_port[0]
        b = Bridge("s", host="127.0.0.1", port=port)
        try:
            server = await asyncio.start_server(b._on_client, "127.0.0.1", port)
        except OSError:
            continue
        break
    if server is None:
        pytest.skip("no free port")

    try:
        yield Harness(b, port)
    finally:
        # Since Python 3.12 Server.wait_closed() waits for EXISTING
        # connections to finish, not just for the listener to stop. A test
        # that leaves a connection open would therefore hang teardown
        # forever. Bound it.
        server.close()
        try:
            await asyncio.wait_for(server.wait_closed(), 1.0)
        except (TimeoutError, asyncio.TimeoutError):
            pass
        await b.close()


# --- handshake --------------------------------------------------------------


async def test_happy_handshake(harness):
    r, w = await harness.connect()
    frames = await harness.recv_until(r, {"READY"})
    ready = [f for f in frames if f.type == "READY"]
    assert ready, [f.type for f in frames]
    assert ready[0].headers["ver"] == str(PROTO_VERSION)
    assert ready[0].headers["page"] == "1"
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


async def test_second_client_is_rejected_not_evicted(harness):
    """Whoever holds the session may be mid-turn; stealing it silently is
    worse than refusing."""
    r1, w1 = await harness.connect()
    await harness.recv_until(r1, {"READY"})

    r2, w2 = await harness.connect()
    frames = await harness.recv_until(r2, {"ERROR"})
    assert frames[-1].headers["code"] == "busy"

    await harness.close(w1, w2)


async def test_non_hello_first_frame_rejected(harness):
    r, w = await asyncio.open_connection("127.0.0.1", harness.port)
    w.write(Frame("INPUT", {}, b"hi").encode())
    await w.drain()
    frames = await harness.recv_until(r, {"ERROR"})
    assert frames[-1].headers["code"] == "expected_hello"
    await harness.close(w)


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


async def test_a_reconnect_resets_the_compressor_and_keeps_the_session(
        harness):
    """The guest will crash. The session must outlive it, and the compressor
    must not: a reconnect means a fresh inflater on the guest side, so the
    persistent deflate stream has to restart in lockstep or the first
    deflated frame after reconnect is garbage the guest cannot decode.

    Used to matter twice over -- a resume-from-seq (`from=`) landing on the
    reset stream would have replayed frames from a compressor that no
    longer existed. There is no such resume any more (docs/BACKLOG.md #5:
    the guest never sent `from=`, and the replay buffer it would have
    driven is retired), so the only two things left to prove are that the
    reset actually happens and that reconnecting does not lose the session.
    """
    r, w = await harness.connect()
    frames = await harness.recv_until(r, {"READY"})
    sess = [f for f in frames if f.type == "READY"][0].headers["sess"]

    await harness.send(w, Frame("INPUT", {}, b"first"))
    await harness.recv_until(r, {"TURN"}, timeout=5)
    await harness.recv_until(r, {"TURN"}, timeout=5)

    # Guest dies.
    await harness.close(w)
    await asyncio.sleep(0.2)

    # And comes back. Same session, whether or not it names it.
    r2, w2 = await harness.connect(sess=sess)
    got = await harness.recv_until(r2, {"READY"}, timeout=3)
    ready = [f for f in got if f.type == "READY"][0]
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


# --- pagination -------------------------------------------------------------


async def test_pagination_round_trip(harness):
    r, w = await harness.connect()
    await harness.recv_until(r, {"READY"})

    # Fill several pages directly -- faster and more deterministic than
    # persuading a model to emit 600 lines.
    guest = harness.bridge._guest
    for i in range(600):
        guest.pages.add("plain", f"line {i:04d} padding padding padding\n")
        if i % 100 == 0:
            guest.pages.message_boundary()
    total = guest.pages.count
    assert total > 2, f"expected several pages, got {total}"

    # Ask for page 1 and check it comes back whole.
    await harness.send(w, Frame("GETPAGE", {"page": "1"}))
    frames = await harness.recv_until(r, {"PAGEEND"}, timeout=5)

    starts = [f for f in frames if f.type == "PAGESTART"]
    assert starts, [f.type for f in frames]
    assert starts[0].headers["page"] == "1"
    assert starts[0].headers["total"] == str(total)
    assert starts[0].headers["live"] == "0", "page 1 is history, not live"

    body = harness.body(frames)
    assert b"line 0000" in body
    assert b"line 0599" not in body, "page 1 must not contain the last line"
    await harness.close(w)


async def test_getpage_live_returns_to_following(harness):
    r, w = await harness.connect()
    await harness.recv_until(r, {"READY"})
    guest = harness.bridge._guest
    for i in range(400):
        guest.pages.add("plain", f"line {i}\n")
        if i % 100 == 0:
            guest.pages.message_boundary()

    await harness.send(w, Frame("GETPAGE", {"page": "1"}))
    await harness.recv_until(r, {"PAGEEND"}, timeout=5)
    assert guest.view_page == 1

    await harness.send(w, Frame("GETPAGE", {"page": "live"}))
    frames = await harness.recv_until(r, {"PAGEEND"}, timeout=5)
    starts = [f for f in frames if f.type == "PAGESTART"]
    assert starts[0].headers["live"] == "1"
    assert guest.view_page is None, "live means follow, not a fixed page"
    await harness.close(w)


async def test_bad_page_number_errors(harness):
    r, w = await harness.connect()
    await harness.recv_until(r, {"READY"})
    await harness.send(w, Frame("GETPAGE", {"page": "9999"}))
    frames = await harness.recv_until(r, {"ERROR"})
    assert frames[-1].headers["code"] == "no_such_page"
    await harness.close(w)


async def test_history_mode_suppresses_live_streaming(harness):
    """While reading an old page, new text must NOT be injected into it."""
    r, w = await harness.connect()
    await harness.recv_until(r, {"READY"})
    guest = harness.bridge._guest
    for i in range(400):
        guest.pages.add("plain", f"line {i}\n")
        if i % 100 == 0:
            guest.pages.message_boundary()

    await harness.send(w, Frame("GETPAGE", {"page": "1"}))
    await harness.recv_until(r, {"PAGEEND"}, timeout=5)

    await harness.send(w, Frame("INPUT", {}, b"while reading history"))
    frames = await harness.recv_until(r, {"TURN"}, timeout=5)
    frames += await harness.recv_until(r, {"TURN"}, timeout=5)

    text_pages = {f.headers.get("page") for f in frames if f.type == "TEXT"}
    assert "1" not in text_pages, (
        "new text leaked onto the history page the reader is looking at"
    )
    await harness.close(w)


async def test_view_page_resets_on_reconnect(harness):
    """02.25: _attach reset zip_out and lost_at on a reconnect but never
    view_page, so a guest that reconnected while paged back stayed in
    HISTORY mode bridge-side forever. _write_block kept buffering new text
    into the paginator silently -- the transcript looked frozen and the
    bridge had no reason to think anything was wrong, because the flag that
    says "the reader is watching an old page" was never cleared. The guest
    itself has no memory of which page it was on across a reconnect (it
    redraws from the fresh DOC just sent), so it is looking at LIVE again
    regardless."""
    r, w = await harness.connect()
    await harness.recv_until(r, {"READY"})
    guest = harness.bridge._guest
    for i in range(400):
        guest.pages.add("plain", f"line {i}\n")
        if i % 100 == 0:
            guest.pages.message_boundary()

    await harness.send(w, Frame("GETPAGE", {"page": "1"}))
    await harness.recv_until(r, {"PAGEEND"}, timeout=5)
    assert guest.view_page == 1, "test setup: guest must be paged back"

    await harness.close(w)
    r2, w2 = await harness.connect()
    await harness.recv_until(r2, {"READY"})

    assert guest.view_page is None, (
        "a reconnect must drop back to LIVE, not keep buffering into a page "
        "nobody -- guest-side -- is still looking at"
    )

    await harness.send(w2, Frame("INPUT", {}, b"after reconnect"))
    frames = await harness.recv_until(r2, {"TURN"}, timeout=5)
    frames += await harness.recv_until(r2, {"TURN"}, timeout=5)
    assert any(f.type == "TEXT" for f in frames), (
        "text must stream live again after a reconnect"
    )
    await harness.close(w2)


async def test_a_block_split_across_a_page_boundary_is_not_rendered_twice(harness):
    """02.33: `_write_block` captured `page_no` before `pages.add`, then sent
    the WHOLE html as a live APPEND tagged with that one page number. When a
    single block is long enough to split mid-run across the hard cap, the
    tail lands on the next page (pages.py's `write`/`add`) but was ALSO
    already inside the bytes just streamed live under the old page's number
    -- so paging forward to where the tail actually lives re-sends it, and
    the reader sees it twice.

    Fixed by only ever streaming the piece of the run that landed on the page
    the stream is tagged with; the tail reaches the guest when it is paged
    to, not before.
    """
    r, w = await harness.connect()
    await harness.recv_until(r, {"READY"})
    guest = harness.bridge._guest

    # hard_bytes is 2x the default page_bytes (16384) with no mem_kb header,
    # so this run must split: 32000 'A's, a newline the splitter should
    # prefer to break at, then 10000 'B's that can only fit on the next page.
    first_half = "A" * 32000
    second_half = "B" * 10000
    big = first_half + "\n" + second_half

    await harness.bridge._write_block(guest, big)
    frames = await harness.recv_until(r, {"PAGESEALED"}, timeout=5)

    sealed = [f for f in frames if f.type == "PAGESEALED"]
    assert sealed and sealed[0].headers["page"] == "1", (
        "test setup: the run must actually split across a seal"
    )
    assert guest.pages.count == 2, "test setup: expected exactly two pages"

    live_body = harness.body(frames).decode("cp1252")
    assert "B" not in live_body, (
        "the tail that landed on page 2 was streamed live under page 1's "
        "APPEND -- it will be sent again when the reader pages forward"
    )
    assert live_body == first_half + "\n", (
        "page 1's live stream must be exactly what pages.write put on page 1"
    )

    await harness.send(w, Frame("GETPAGE", {"page": "2"}))
    frames2 = await harness.recv_until(r, {"PAGEEND"}, timeout=5)
    page2_body = harness.body(frames2).decode("cp1252")
    assert page2_body == second_half, (
        "page 2 must hold exactly the tail, with nothing repeated from what "
        "already streamed live on page 1"
    )
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
    harness.bridge.model = "haiku"
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
    harness.bridge.model = "opus"
    harness.bridge.effort = "low"
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
    b.sync_dir = str(tmp_path / "mirror")
    os.makedirs(b.sync_dir, exist_ok=True)
    g = b._guest

    await harness.send(w, Frame("INPUT", {}, b"just a question"))
    await harness.recv_until(r, {"TURN"}, 10.0)

    assert g.work_guest == "", "unset until a tool has run"
    assert g.work_dir == ""
    assert b._mirror_dir(g) == "", "no mirror without a committed folder"
    assert b._current_guest_root() == ""
    assert await b.run_sync("test") == {}, "no pass without a mirror"
    assert os.listdir(b.sync_dir) == [], "and no folder littered on disk"
    await harness.close(w)


async def test_the_first_tool_call_commits_the_folder(harness, tmp_path):
    """Named from the FIRST MESSAGE, not the CLI's title: a tool can run in
    the first turn, before the conversation has been titled, and the
    alternative -- a placeholder renamed afterwards -- is a new tree_id and a
    new baseline for a tree sync has already scanned."""
    r, w = await harness.connect(hdrs={"sync_root": "C:\\Gaston"})
    await harness.recv_until(r, {"OPTIONSEND"})
    b = harness.bridge
    b.sync_dir = str(tmp_path / "mirror")
    os.makedirs(b.sync_dir, exist_ok=True)
    g = b._guest

    await harness.send(w, Frame("INPUT", {}, b"List files in C:"))
    await harness.recv_until(r, {"TURN"}, 10.0)
    assert g.work_guest == ""

    await g.claude.use_tool()

    assert g.work_dir == os.path.join(b.sync_dir, "LISTFILE")
    assert os.path.isdir(g.work_dir), "created, not merely named"
    assert g.work_guest == "C:\\Gaston\\LISTFILE"
    assert b._mirror_dir(g) == g.work_dir, "and sharing is on now"
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
    b.sync_dir = str(tmp_path / "mirror")
    os.makedirs(b.sync_dir, exist_ok=True)
    g = b._guest

    orig = srv.sesslist.listing
    srv.sesslist.listing = lambda cwd=None, limit=50: [
        {"id": g.conv_id or g.claude.session_id, "title": "List files in C:",
         "cwd": "", "modified": "", "group": ""}]
    try:
        assert g.first_prompt == "", "nothing said in this session"
        await b._ensure_work_dir(g)
    finally:
        srv.sesslist.listing = orig

    assert g.work_guest == "C:\\Gaston\\LISTFILE"
    await harness.close(w)


async def test_two_conversations_do_not_share_a_folder(harness, tmp_path):
    """Two chats about listing files both reduce to LISTFILE, and the second
    taking the first one's folder would put two conversations' work in one
    place with no way to tell them apart afterwards."""
    b = harness.bridge
    b.sync_dir = str(tmp_path / "mirror")
    os.makedirs(os.path.join(b.sync_dir, "LISTFILE"), exist_ok=True)

    r, w = await harness.connect(hdrs={"sync_root": "C:\\Gaston"})
    await harness.recv_until(r, {"OPTIONSEND"})
    g = b._guest
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
    r, w = await harness.connect()
    await harness.recv_until(r, {"OPTIONSEND"})
    await harness.send(w, Frame("NEWSESSION", {"folder": "  "}))
    await harness.recv_until(r, {"OPTIONSEND"}, 10.0)
    assert harness.bridge._guest.work_dir == ""
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
    b.sync_dir = str(tmp_path / "mirror")
    os.makedirs(os.path.join(b.sync_dir, "NOTES"), exist_ok=True)

    rows = [{"id": "stored-1", "title": "t", "cwd": os.path.join(
                b.sync_dir, "NOTES"), "modified": "", "group": ""},
            {"id": "stored-2", "title": "t", "cwd": "/somewhere/else",
             "modified": "", "group": ""}]
    orig = srv.sesslist.listing
    srv.sesslist.listing = lambda cwd=None, limit=50: list(rows)
    try:
        await _switch_to(harness, monkeypatch=_NoPatch(), r=r, w=w,
                         sid="stored-1")
        assert b._guest.work_dir == os.path.join(b.sync_dir, "NOTES")
        await _switch_to(harness, monkeypatch=_NoPatch(), r=r, w=w,
                         sid="stored-2")
        assert b._guest.work_dir == "", (
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
        srv.sesslist.history_html = lambda sid_, limit=2000: []


async def test_a_reopened_conversation_keeps_its_folder(harness, tmp_path):
    """The folder is named AFTER the session starts, so the session's own
    recorded cwd is the root and asking the SDK gives the wrong answer.
    Reopening showed "<unset>" for a conversation whose folder was on disk."""
    import bridge.server as srv
    r, w = await harness.connect(hdrs={"sync_root": "C:\\Gaston"})
    await harness.recv_until(r, {"OPTIONSEND"})
    b = harness.bridge
    b.sync_dir = str(tmp_path / "mirror")
    os.makedirs(b.sync_dir, exist_ok=True)
    g = b._guest
    sid = g.claude.session_id

    orig = srv.sesslist.listing
    srv.sesslist.listing = lambda cwd=None, limit=50: [
        {"id": sid, "title": "Sky color", "cwd": b.sync_dir,
         "modified": "", "group": ""}]
    try:
        await harness.send(w, Frame("INPUT", {}, b"Sky color"))
        await harness.recv_until(r, {"TURN"}, 10.0)
        await g.claude.use_tool()
        assert b.folders.get(sid) == "SKYCOLOR"

        # Away and back.
        await _switch_to(harness, monkeypatch=_NoPatch(), r=r, w=w,
                         sid="somewhere-else")
        await _switch_to(harness, monkeypatch=_NoPatch(), r=r, w=w, sid=sid)
    finally:
        srv.sesslist.listing = orig

    assert g.work_guest == "C:\\Gaston\\SKYCOLOR"
    assert g.work_dir == os.path.join(b.sync_dir, "SKYCOLOR")
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
    b.sync_dir = str(tmp_path / "mirror")
    os.makedirs(b.sync_dir, exist_ok=True)
    g = b._guest
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
    assert b.folders.guest(sid) == "D:\\Projects\\Retro"
    await harness.close(w)


async def test_setwork_tells_the_agent_the_directory_moved(harness, tmp_path):
    """The agent had no idea. The transcript notice goes to the user, the
    OPTION batch goes to the toolbar, and the system prompt was written
    before either -- so it went on writing into a folder that is no longer
    mirrored anywhere, with every write appearing to succeed."""
    r, w = await harness.connect(hdrs={"sync_root": "C:\\Gaston"})
    await harness.recv_until(r, {"OPTIONSEND"})
    b = harness.bridge
    b.sync_dir = str(tmp_path / "mirror")
    os.makedirs(b.sync_dir, exist_ok=True)

    await harness.send(w, Frame("SETWORK", {"path": "D:\\Projects\\Retro"}))
    await harness.recv_until(r, {"OPTIONSEND"}, 10.0)

    said = "\n".join(b.synccoord.agent_notices)
    assert "THE WORK DIRECTORY HAS CHANGED" in said
    assert "D:\\Projects\\Retro" in said, "it has to name the guest's path"
    assert os.path.join(b.sync_dir, "RETRO") in said, "and this side's"
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
    b.sync_dir = str(tmp_path / "mirror")
    os.makedirs(b.sync_dir, exist_ok=True)

    await harness.send(w, Frame("SETWORK", {"path": "D:\\Projects\\Retro",
                                            "staged": "merge"}))
    await harness.recv_until(r, {"OPTIONSEND"}, 10.0)

    said = "\n".join(b.synccoord.agent_notices)
    assert "asked YOU to merge" in said
    assert os.path.join(b.sync_dir, "RETRO", "Gaston-Incoming") in said
    assert "ask before" in said, "it must not pick a side on its own"
    await harness.close(w)


async def test_setwork_before_the_conversation_exists_is_refused(harness):
    """A work directory belongs to a conversation, and there is not one yet."""
    r, w = await harness.connect(hdrs={"sync_root": "C:\\Gaston"})
    await harness.recv_until(r, {"OPTIONSEND"})
    harness.bridge._guest.identity.claude.session_id = None
    harness.bridge._guest.pending_resume = ""
    await harness.send(w, Frame("SETWORK", {"path": "D:\\Nope"}))
    frames = await harness.recv_until(r, {"TEXT"}, 10.0)
    assert b"Send a message first" in harness.body(frames)
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
    b.sync_state = str(tmp_path / "state")
    os.makedirs(b.sync_state, exist_ok=True)
    legacy = os.path.join(b.sync_state, "base.json")
    with open(legacy, "w") as fh:
        _json.dump({"guest_root": "C:\\WORK"}, fh)

    b.sync_dir = str(tmp_path / "mirror")
    os.makedirs(b.sync_dir, exist_ok=True)
    r, w = await harness.connect(hdrs={"sync_root": "C:\\Gaston"})
    await harness.recv_until(r, {"OPTIONSEND"})

    # Nothing committed, nothing mirrored, so there is no root to key a
    # baseline on and the legacy file is what an unasked question returns.
    assert b._base_path() == legacy

    await harness.send(w, Frame("SETWORK", {"path": "C:\\WORK"}))
    await harness.recv_until(r, {"OPTIONSEND"}, 10.0)
    assert b._base_path() == legacy, "its own root keeps the legacy file"

    await harness.send(w, Frame("SETWORK", {"path": "C:\\Gaston\\OK"}))
    await harness.recv_until(r, {"OPTIONSEND"}, 10.0)
    other = b._base_path()
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
    assert "not complete" in body
    assert "mcp__machine__exec" in body
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
    assert "settings.json" in body and "do <b>not</b> apply" in body
    await harness.close(w)


async def test_the_rules_view_follows_the_mode(harness):
    r, w = await harness.connect()
    await harness.recv_until(r, {"OPTIONSEND"})
    await harness.send(w, Frame("SETMODE", {"mode": "plan"}))
    await harness.recv_until(r, {"OPTIONSEND"}, 10.0)
    await harness.send(w, Frame("RULES", {}))
    frames = await harness.recv_until(r, {"TEXT"}, 10.0)
    body = harness.body(frames).decode("cp1252")
    assert "Mode: Plan" in body
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
    text = data.decode("cp1252")
    assert text.startswith("<html>") and text.rstrip().endswith("</html>")
    assert "hello" in text, "the conversation itself must be in there"
    assert "<style>" in text, "and it must stand alone, not need our CSS"

    offs = [int(f.headers["off"]) for f in frames if f.type == "EXPORTDAT"]
    assert offs == sorted(offs) and offs[0] == 0
    await harness.close(w)


async def test_export_of_an_empty_conversation_is_still_a_document(harness):
    """Exporting before saying anything should produce a valid, if dull,
    file rather than a zero-byte one or an error."""
    r, w = await harness.connect()
    await harness.recv_until(r, {"OPTIONSEND"})
    await harness.send(w, Frame("EXPORT", {}))
    frames = await harness.recv_until(r, {"EXPORTEND"}, 10.0)
    data = _export_bytes(harness, frames)
    assert data.decode("cp1252").rstrip().endswith("</html>")
    await harness.close(w)


# --- opening a conversation without starting one ----------------------------


async def _switch_to(harness, monkeypatch, r, w, sid, blocks=None):
    """Drive a SWITCH with history replay faked.

    history_html reads the real transcript store, which a test has none of.
    Faking it keeps the test about the switch rather than about the SDK.
    """
    import bridge.server as srv
    monkeypatch.setattr(srv.sesslist, "history_html",
                        lambda sid_, limit=2000: list(blocks or []))
    await harness.send(w, Frame("SWITCH", {"id": sid}))
    return await harness.recv_until(r, {"OPTIONSEND"}, 10.0)


async def test_ready_names_the_conversation_not_just_the_connection(harness):
    """`sess` is the per-connection handle and is never a conversation id.
    The guest keys its cached transcript on the conversation, and before
    READY carried one, a plain reconnect cached nothing at all -- only
    SWITCHED said where pages belonged, and a reconnect does not send one."""
    r, w = await harness.connect()
    frames = await harness.recv_until(r, {"READY"})
    ready_f = [f for f in frames if f.type == "READY"][-1]
    g = harness.bridge._guest
    assert ready_f.headers.get("conv") == g.conv_id
    assert ready_f.headers["sess"] != ready_f.headers.get("conv")
    await harness.close(w)


async def test_switching_a_chat_starts_no_process(harness, monkeypatch):
    """Opening a conversation to READ it used to spawn a full CLI subprocess
    with --resume. Nothing was billed, but browsing the list to find something
    paid a process start per click."""
    r, w = await harness.connect()
    await harness.recv_until(r, {"OPTIONSEND"})
    g = harness.bridge._guest
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
    g = harness.bridge._guest
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
    g = harness.bridge._guest
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
    perms = harness.bridge._guest.perms
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
    assert modes == ["default", "acceptEdits", "plan", "auto"]
    assert "bypassPermissions" not in modes
    assert "dontAsk" not in modes
    ticked = [f.headers["id"] for f in frames if f.type == "OPTION"
              and f.headers.get("kind") == "mode"
              and f.headers.get("current") == "1"]
    assert ticked == ["default"], "a fresh guest starts in Manual"
    await harness.close(w)


async def test_choosing_auto_says_what_it_does_not_do(harness):
    """The measurement goes in front of the person choosing, once, rather
    than into a decision made on their behalf."""
    r, w = await harness.connect()
    await harness.recv_until(r, {"OPTIONSEND"})
    await harness.send(w, Frame("SETMODE", {"mode": "auto"}))
    frames = await harness.recv_until(r, {"OPTIONSEND"}, 10.0)
    body = harness.body(frames).decode("cp1252")
    assert "Permission mode set to Auto" in body
    assert "not consulted" in body
    await harness.close(w)


async def test_setmode_changes_the_live_session_and_the_menu(harness):
    r, w = await harness.connect()
    await harness.recv_until(r, {"OPTIONSEND"})
    await harness.send(w, Frame("SETMODE", {"mode": "acceptEdits"}))
    frames = await harness.recv_until(r, {"OPTIONSEND"})
    ticked = [f.headers["id"] for f in frames if f.type == "OPTION"
              and f.headers.get("kind") == "mode"
              and f.headers.get("current") == "1"]
    assert ticked == ["acceptEdits"]
    assert harness.bridge._guest.claude.permission_mode == "acceptEdits"
    await harness.close(w)


async def test_setmode_refuses_a_mode_that_is_not_on_the_menu(harness):
    """The header is not model-supplied, but it still arrives off a socket,
    and this particular typo would silently stop the permission bar from ever
    appearing again."""
    r, w = await harness.connect()
    await harness.recv_until(r, {"OPTIONSEND"})
    await harness.send(w, Frame("SETMODE", {"mode": "bypassPermissions"}))
    await harness.send(w, Frame("SETMODE", {"mode": "acceptEdits"}))
    await harness.recv_until(r, {"OPTIONSEND"})
    assert harness.bridge._guest.perm_mode == "acceptEdits", (
        "the refused mode must not have been applied, and must not have "
        "swallowed the valid one that followed"
    )
    await harness.close(w)


async def test_the_chosen_mode_survives_a_rebuilt_session(harness):
    """A new session throws the Session object away. The mode the user picked
    is on the guest, not on that object, precisely so it comes back."""
    r, w = await harness.connect()
    await harness.recv_until(r, {"OPTIONSEND"})
    await harness.send(w, Frame("SETMODE", {"mode": "plan"}))
    await harness.recv_until(r, {"OPTIONSEND"})
    await harness.send(w, Frame("NEWSESSION", {}))
    await harness.recv_until(r, {"OPTIONSEND"})
    assert harness.bridge._guest.claude.permission_mode == "plan", (
        "the rebuilt session must start in the mode the user chose, not "
        "back in Manual"
    )
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
    harness.bridge._guest.claude.silent = True
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


async def _recv(harness, reader, type_, timeout=5.0):
    """recv_until returns the whole batch; the frame we waited for is last."""
    return (await harness.recv_until(reader, {type_}, timeout))[-1]


async def _connect_and_ask(harness, **ctx_kw):
    """Connect a guest, raise one prompt, and return (reader, writer, task)."""
    reader, writer = await harness.connect()
    await _recv(harness, reader, "OPTIONSEND")
    task = harness.bridge._guest.claude.ask(**ctx_kw)
    return reader, writer, task


async def test_permission_request_reaches_the_guest(harness):
    reader, writer, task = await _connect_and_ask(harness)
    f = await _recv(harness, reader, "PERMREQ")
    assert f.headers["id"] == "toolu_test_1"
    assert f.headers["tool"] == "Bash"
    # A built-in with a scoped suggestion may offer Always.
    assert f.headers["allows"] == "1"
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


async def test_always_forces_session_scope(harness):
    """PermissionUpdate.destination can name userSettings. A dialog on a Win98
    box quietly rewriting the developer's settings files is a surprise nobody
    asked for."""
    reader, writer, task = await _connect_and_ask(harness)
    await _recv(harness, reader, "PERMREQ")
    await harness.send(writer, Frame("PERMREPLY",
                               {"id": "toolu_test_1", "action": "always"}))
    result = await asyncio.wait_for(task, 5)
    assert isinstance(result, PermissionResultAllow)
    assert result.updated_permissions
    assert all(u.destination == "session" for u in result.updated_permissions)
    await harness.close(writer)


async def test_always_is_suppressed_for_machine_tools(harness):
    """MCP rules cannot carry specifiers, so the only rule expressible for
    mcp__machine__exec is the WHOLE TOOL -- one press of Always would grant
    silent arbitrary command execution on the user's computer for the rest of
    the session. The suggestion list is non-empty either way, so keying the
    button off it would light it up."""
    reader, writer = await harness.connect()
    await _recv(harness, reader, "OPTIONSEND")
    task = harness.bridge._guest.claude.ask(tool_name="mcp__machine__exec")
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
    assert harness.bridge._guest.perm_pending is None
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
    guest = harness.bridge._guest

    fut = asyncio.get_event_loop().create_future()
    guest.sync_pending["s1"] = S.Wait(kind="put", future=fut)

    await harness.close(writer)
    async with asyncio.timeout(5):
        while not fut.done():
            await asyncio.sleep(0.01)

    assert fut.exception() is not None, (
        "the in-flight sync future must be failed, not left to hang"
    )
    assert "disconnected" in str(fut.exception())
    assert guest.sync_pending == {}, "the failed entry must be cleared"


async def test_disconnect_holds_the_prompt_then_denies(harness):
    """Not "denies immediately", which is what the plan said and what the first
    implementation did.

    Denying on any drop tells the model the user declined something the user
    never saw -- on a flaky 1998 NIC a blip becomes a refusal. So the prompt is
    held for a grace period and then denied with a message that says what
    actually happened, rather than claiming a decision nobody made.
    """
    harness.bridge.perm_grace_s = 0.2
    harness.bridge.idle_check_s = 0.05

    reader, writer, task = await _connect_and_ask(harness)
    await _recv(harness, reader, "PERMREQ")
    await harness.close(writer)

    result = await asyncio.wait_for(task, 5)
    assert isinstance(result, PermissionResultDeny)
    assert "disconnected" in result.message
    assert "not refused on purpose" in result.message
    assert harness.bridge._guest.perm_pending is None


async def test_prompt_is_reissued_after_reconnect(harness):
    """Nothing re-sends anything from a log on reconnect (there is no wire
    replay any more -- docs/BACKLOG.md #5). Without an explicit re-issue the
    guest would reconnect with no bar while the bridge still holds a live
    future, and the turn hangs until the answer deadline."""
    harness.bridge.perm_grace_s = 30.0      # comfortably inside the grace

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

    task2 = harness.bridge._guest.claude.ask(tool_use_id="toolu_test_2")
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
    guest = harness.bridge._guest

    ok = await harness.bridge._send(guest, srv.notice("k", "an ordinary notice"))
    assert ok is True

    huge = srv.Frame("NOTICE", {"kind": "k"}, b"x" * 200_000)
    with pytest.raises(ProtocolError):
        await harness.bridge._send(guest, huge)

    await harness.close(writer)
    await asyncio.sleep(0.1)
    dropped = await harness.bridge._send(guest, srv.notice("k", "after close"))
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
    harness.bridge.idle_timeout_s = 0.2
    harness.bridge.idle_check_s = 0.05

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
    task = harness.bridge._guest.claude.ask(
        tool_name="Read", args={"file_path": "/etc/hostname"})
    result = await asyncio.wait_for(task, 5)
    assert isinstance(result, PermissionResultDeny)
    assert "not on the user's machine" in result.message
    assert harness.bridge._guest.perm_pending is None, "no bar should appear"
    await harness.close(writer)


async def test_path_inside_the_working_copy_still_asks(harness):
    """The guard must not swallow legitimate staging work."""
    import os as _os
    inside = _os.path.join(harness.bridge.cwd or _os.getcwd(), "staged.txt")
    reader, writer = await harness.connect()
    await _recv(harness, reader, "OPTIONSEND")
    task = harness.bridge._guest.claude.ask(
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
        harness.bridge.run_on_guest("dir /s c:\\", timeout_ms=60000,
                                    shell=True, cwd=""))
    f = await _recv(harness, reader, "EXEC")
    exec_id = f.headers["id"]

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    kill = await _recv(harness, reader, "EXECKILL")
    assert kill.headers["id"] == exec_id
    # And the slot is free, so the next call is not answered "busy".
    assert not harness.bridge._guest.exec_pending
    await harness.close(writer)


async def test_exec_is_never_re_delivered(harness):
    """There is no wire replay any more (docs/BACKLOG.md #5) -- nothing
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
        harness.bridge.run_on_guest("ver", timeout_ms=5000, shell=True, cwd=""))
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
        harness.bridge.run_on_guest("pause", timeout_ms=90000,
                                    shell=True, cwd=""))
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


async def test_a_command_line_too_long_says_so_rather_than_spawn_failed(
        harness):
    """The guest refuses a command line over ~1000 characters before running
    it. That was flattened onto `spawn_failed`, which reads as an environment
    problem and invites a retry -- and retrying a deterministic size rejection
    never succeeds. The model also has no way to guess the limit unless it is
    told it."""
    reader, writer = await harness.connect()
    await _recv(harness, reader, "OPTIONSEND")
    task = asyncio.create_task(harness.bridge.run_on_guest("x" * 2000, timeout_ms=5000,
                                    shell=True, cwd=""))
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
    task = asyncio.create_task(harness.bridge.run_on_guest("dir", timeout_ms=5000,
                                    shell=True, cwd=""))
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
    task = asyncio.create_task(harness.bridge.run_on_guest("dir", timeout_ms=5000,
                                    shell=True, cwd=""))
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
    task = asyncio.create_task(harness.bridge.run_on_guest("pause", timeout_ms=5000,
                                    shell=True, cwd=""))
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
    reader, writer = await harness.connect()
    await _recv(harness, reader, "OPTIONSEND")
    guest = harness.bridge._guest
    await harness.bridge._write_block(
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
    out = H.tool_result("x" * 50_000)
    assert len(out) < 4000
    assert "more characters were sent to the model" in out


def test_an_empty_tool_result_still_says_something():
    """Silence is indistinguishable from a bug, which is how this was found."""
    from bridge.view import html as H
    assert "(no output)" in H.tool_result("")
    assert "(no output)" in H.tool_result("   \n ")


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
    got = await harness.bridge.run_on_guest(
        "dir", timeout_ms=5000, shell=True, cwd="C:\\" + "d" * 300)
    text = got["content"][0]["text"]
    assert "too long" in text
    assert got["isError"] is True
    assert "Nothing was run" in text
    await harness.close(writer)


async def test_an_ordinary_cwd_still_goes_through(harness):
    """The guard must not fire on the case it exists to protect."""
    reader, writer = await harness.connect()
    await _recv(harness, reader, "OPTIONSEND")
    task = asyncio.create_task(harness.bridge.run_on_guest(
        "dir", timeout_ms=5000, shell=True, cwd="C:\\WORK"))
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
    guest = harness.bridge._guest

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
    r1, w1 = await asyncio.open_connection("127.0.0.1", harness.port)
    blob = hello("s").encode() + Frame("INPUT", {}, b"one").encode()
    w1.write(blob)
    await w1.drain()

    # Second client arrives and is refused as a duplicate.
    r2, w2 = await asyncio.open_connection("127.0.0.1", harness.port)
    w2.write(hello("s").encode())
    await w2.drain()
    await asyncio.sleep(0.2)

    # The first client's turn still ran, with ITS text.
    assert harness.bridge._guest is not None
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
    guest = harness.bridge._guest
    before = guest.sess_id

    await guest.claude.on_system("init", {"session_id": "forked-1234"})

    f = await _recv(harness, reader, "SESSIONSEND")
    assert f.type == "SESSIONSEND", "the guest was not told the list changed"
    assert guest.sess_id == before, (
        "sess_id is a stable per-connection handle (02.24); a session "
        "becoming known must not move it"
    )
    await harness.close(writer)


async def test_an_oversized_block_is_truncated_not_split_mid_element(harness):
    """`total`/`off` are on every TEXT chunk and look like a reassembly
    protocol. They are not.

    win/app/main.c's TEXT branch calls rc_html_block(f->body) per frame and
    uses total/off only for the progress figure, so an APPEND block split
    across frames arrives as several separate appends -- each a fragment of
    markup, handed to insertAdjacentHTML. Half a <div>.

    Splitting arbitrary HTML safely needs to know where its element
    boundaries are, which this layer cannot know. So it truncates at the
    boundary it CAN guarantee and says so. The model still has the whole
    text; only the on-screen copy is shortened.
    """
    reader, writer = await harness.connect()
    await _recv(harness, reader, "OPTIONSEND")
    guest = harness.bridge._guest

    huge = '<div class="tool">' + ("x" * 90_000) + "</div>"
    await harness.bridge._send_html(guest, huge, srv.APPEND, 1)

    got = await harness.recv_until(reader, {"TURN", "TEXT"}, timeout=3)
    texts = [f for f in got if f.type == "TEXT"]
    assert len(texts) == 1, (
        f"the block was split into {len(texts)} frames; the guest appends "
        f"each one separately, so this is markup cut in half")
    body = harness.body(texts)
    assert b"too large to send" in body, "truncated without saying so"
    await harness.close(writer)


async def test_a_measured_link_speed_reaches_the_codec_choice(harness):
    """PLAN.md makes adaptive codec selection depend on STAT -- "the sender
    picks the codec from the measured link speed" -- and the bridge has
    handled STAT since the beginning.

    The guest had never sent one. So guest.link_bps was always None and every
    threshold in compress.py and coalesce.py was inert: identical behaviour
    on a 10Mbit LAN and a modem, which is the exact thing the mechanism
    exists to avoid.
    """
    from bridge.wire.compress import should_compress

    reader, writer = await harness.connect()
    await _recv(harness, reader, "OPTIONSEND")
    guest = harness.bridge._guest
    assert guest.link_bps is None, "nothing has reported a speed yet"

    small = b"x" * 200
    await harness.send(writer, Frame("STAT", {"bps": "5000"}))
    await asyncio.sleep(0.05)
    assert guest.link_bps == 5000.0, "the measurement never reached the bridge"

    # And it actually changes the decision, rather than merely being stored.
    slow = should_compress(small, guest.link_bps)
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

    out = await harness.bridge.run_on_guest(
        "type c:\\work\u2044..\u2044windows\\win.ini",
        timeout_ms=5000, shell=True, cwd="")

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

    out = await harness.bridge.run_on_guest(
        "ver", timeout_ms=5000, shell=True, cwd="c:\\w\u2502rk")

    assert out["isError"] is True
    assert "U+2502" in out["content"][0]["text"]
    await harness.close(writer)


async def test_a_cp1252_command_still_runs(harness):
    """The refusal must not catch accented names, which are ordinary on a
    CP1252 box. Asserted by watching the EXEC frame leave with the real
    bytes -- 0xE9, not 'e'."""
    reader, writer = await harness.connect()
    await _recv(harness, reader, "OPTIONSEND")

    task = asyncio.create_task(harness.bridge.run_on_guest(
        "type c:\\caf\u00e9.txt", timeout_ms=5000, shell=True, cwd=""))
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
    harness.bridge._guest.work_guest = "C:\\WORK"

    task = harness.bridge._guest.claude.ask(
        "mcp__machine__exec",
        {"command": "deltree /y c:\\windows", "cwd": "C:\\"})
    frame = await _recv(harness, reader, "PERMREQ")
    body = frame.body.decode("cp1252")

    assert body.startswith("! writes outside the mirrored folder: ")
    # The path keeps the case the model wrote it in -- only the marker is
    # ours. Shouting the path back loses information for no gain.
    assert "c:\\windows" in body
    task.cancel()
    await harness.close(writer)


async def test_a_contained_command_gets_no_added_reassurance(harness):
    """The bar must read exactly as it does today for an ordinary command."""
    reader, writer = await harness.connect()
    await _recv(harness, reader, "OPTIONSEND")
    harness.bridge._guest.work_guest = "C:\\WORK"

    task = harness.bridge._guest.claude.ask(
        "mcp__machine__exec",
        {"command": "dir c:\\work", "cwd": "C:\\WORK"})
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
    b.sync_dir = str(tmp_path / "mirror")
    os.makedirs(b.sync_dir, exist_ok=True)
    g = b._guest
    g.first_prompt = "Sky color"
    assert g.work_guest == ""

    task = g.claude.ask("mcp__machine__exec",
                        {"command": "dir", "cwd": "C:\\Gaston\\SKYCOLOR"})
    frame = await _recv(harness, reader, "PERMREQ")
    body = frame.body.decode("cp1252")

    assert g.work_guest == "C:\\Gaston\\SKYCOLOR", "committed by the ask"
    assert "no folder is mirrored" not in body.lower()
    task.cancel()
    await harness.close(writer)


# --- the context gauge -------------------------------------------------------


async def test_turn_end_carries_context_fullness(harness):
    """It rides on TURN end rather than getting a frame of its own: one small
    integer that only changes when a turn ends, arriving at the moment the
    guest is already parsing that frame."""
    reader, writer = await harness.connect()
    await _recv(harness, reader, "OPTIONSEND")

    await harness.bridge._guest.claude.send("hello")
    await harness.bridge._run_turn(harness.bridge._guest, "hello")
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
    harness.bridge._guest.claude.context_pct = None

    await harness.bridge._run_turn(harness.bridge._guest, "hello")
    frame = await _recv(harness, reader, "TURN")
    while frame.headers.get("state") != "end":
        frame = await _recv(harness, reader, "TURN")

    assert "ctx_pct" not in frame.headers
    await harness.close(writer)
