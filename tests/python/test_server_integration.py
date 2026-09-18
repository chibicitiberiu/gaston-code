"""End-to-end over the real wire protocol, with Claude mocked out.

Covers the parts that are awkward to test any other way: HELLO negotiation,
duplicate-client rejection, resume after a dropped connection, and pagination
round trips. Mocking the session keeps it deterministic and fast; the real
model is exercised separately by netcheck.exe.
"""

import asyncio
import zlib
from dataclasses import dataclass, field

import pytest

from claude_agent_sdk import (PermissionResultAllow, PermissionResultDeny,
                              PermissionUpdate)

from bridge.wire.compress import StreamDecompressor
from bridge.wire.protocol import Frame, FrameReader, MAX_FRAME, PROTO_VERSION, hello
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
        self.on_text = kw.get("on_text")
        # Captured so permission tests can drive it. Deliberately NOT called
        # inline from send(): in production the SDK runs it in its own
        # cancellable task (query.py spawn_task), and a fixture that awaits it
        # inline would pass while the cancellation wedge shipped.
        self.can_use_tool = kw.get("can_use_tool")
        self.model = "fake-model"
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

    async def start(self):
        self.started = True

    async def close(self):
        self.started = False

    async def interrupt(self):
        self.interrupted = True

    async def set_model(self, m):
        self.model = m

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
             "displayName": "Haiku"},
        ]

    async def send(self, prompt):
        from bridge.agent.session import Turn
        if self.on_text:
            await self.on_text(f"echo: {prompt}\n")
        return Turn(ok=True, subtype="success", cost_usd=0.01,
                    num_turns=1, duration_ms=5)


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
                      sess=None, from_seq=None, hdrs=None):
        r, w = await asyncio.open_connection("127.0.0.1", self.port)
        # Fresh connection means the bridge resets its compressor, so reset
        # in lockstep. Same for the frame reader.
        self.unzip = StreamDecompressor()
        self._readers.pop(id(r), None)
        self._pending.pop(id(r), None)
        f = hello(secret, sess=sess, from_seq=from_seq)
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


async def test_seqs_are_monotonic_and_unique(harness):
    r, w = await harness.connect()
    await harness.recv_until(r, {"READY"})
    await harness.send(w, Frame("INPUT", {}, b"one"))
    frames = await harness.recv_until(r, {"TURN"}, timeout=5)
    frames += await harness.recv_until(r, {"TURN"}, timeout=5)
    seqs = [f.seq for f in frames if f.seq is not None]
    assert seqs == sorted(seqs)
    assert len(seqs) == len(set(seqs))
    await harness.close(w)


async def test_unknown_frame_type_gets_an_error_not_a_drop(harness):
    r, w = await harness.connect()
    await harness.recv_until(r, {"READY"})
    await harness.send(w, Frame("NONSENSE", {}))
    frames = await harness.recv_until(r, {"ERROR"})
    assert frames[-1].headers["code"] == "unknown_type"
    await harness.close(w)


# --- resume -----------------------------------------------------------------


async def test_resume_replays_missed_frames(harness):
    """The guest will crash. The session must outlive it."""
    r, w = await harness.connect()
    frames = await harness.recv_until(r, {"READY"})
    sess = [f for f in frames if f.type == "READY"][0].headers["sess"]

    await harness.send(w, Frame("INPUT", {}, b"first"))
    got = await harness.recv_until(r, {"TURN"}, timeout=5)
    got += await harness.recv_until(r, {"TURN"}, timeout=5)
    last_seq = max(f.seq for f in got if f.seq is not None)

    # Guest dies.
    await harness.close(w)
    await asyncio.sleep(0.2)

    # Guest comes back asking for everything after what it saw.
    r2, w2 = await harness.connect(sess=sess, from_seq=1)
    replayed = await harness.recv_until(r2, {"READY"})
    replayed += await harness.recv_until(r2, {"TURN"}, timeout=3)
    seqs = [f.seq for f in replayed if f.seq is not None]
    assert seqs, "resume should replay something"
    assert min(seqs) == 1
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


async def test_effort_options_follow_the_selected_model(harness):
    """Haiku has no effort levels at all, so offering five would be lying."""
    r, w = await harness.connect()
    frames = await harness.recv_until(r, {"OPTIONSEND"})
    efforts = [f.headers["id"] for f in frames if f.type == "OPTION"
               and f.headers.get("kind") == "effort"]
    assert efforts == ["low", "high"]
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
    """P1: the guest never sends from=, so the bridge's replay buffer cannot
    bring the bar back by itself. Without an explicit re-issue the guest
    reconnects with no bar while the bridge still holds a live future, and the
    turn hangs until the answer deadline."""
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


async def test_oversized_prompt_does_not_poison_replay(harness):
    """_send stamps into the replay buffer, and Frame.encode raises above
    MAX_FRAME -- so one unencodable frame would make EVERY future replay
    raise, permanently, and the guest could never reconnect again."""
    reader, writer, task = await _connect_and_ask(
        harness, description="x" * 200_000)
    f = await _recv(harness, reader, "PERMREQ")
    assert len(f.body) < MAX_FRAME
    # And the buffer still replays cleanly.
    harness.bridge._guest.replay.since(1)
    task.cancel()
    await harness.close(writer)


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


async def test_exec_is_never_replayed(harness):
    """Replay redraws a transcript. Re-delivering an EXEC would RE-RUN the
    command -- `del c:\\temp\\*` twice is not a redraw."""
    reader, writer = await harness.connect()
    await _recv(harness, reader, "OPTIONSEND")
    before = harness.bridge._guest.replay.next_seq

    task = asyncio.create_task(
        harness.bridge.run_on_guest("ver", timeout_ms=5000, shell=True, cwd=""))
    await _recv(harness, reader, "EXEC")
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    types = [fr.type for fr in harness.bridge._guest.replay.since(before)]
    assert "EXEC" not in types
    assert "EXECKILL" not in types
    await harness.close(writer)


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
