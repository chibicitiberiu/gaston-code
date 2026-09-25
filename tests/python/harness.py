"""One bridge, one guest, one socket -- the scaffolding four suites share.

This lived inside `test_server_integration.py`, and three other suites reached
into it with `from test_server_integration import harness`. That works right
up until the suites are not all in one directory, which is how it surfaced:
importing a fixture out of a sibling TEST module depends on that module being
on the path, and nothing says it should be.

So the scaffolding is a module of its own and the test suite is a suite. The
four callers -- server integration, sync over the wire, the fakegui
conformance check and netcheck under Wine -- import from here.

`harness` is a fixture, and importing a fixture by name is how pytest expects
it to be shared; the `# noqa: F401` at each call site marks that the name is
used by pytest rather than by the module.
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
        # The whole system_prompt dict, so a test can assert on text that was
        # only ever true AT BUILD TIME. The tool inventory is the case: it
        # has to be in the prompt this session was constructed with, and
        # reading it back off the guest afterwards would pass even if the
        # prompt had been frozen a moment too early.
        self.system_prompt = kw.get("system_prompt")
        self.fork = kw.get("fork", False)
        self.started = False
        self.interrupted = False
        self.last_prompt = None
        self.last_prompt_messages = []
        FakeSession._seq += 1
        # DEFERRED, because the real Session's is. `Session._session_id` is
        # set from the `init` system message and nothing else
        # (bridge/agent/session.py:353), and that message arrives during the
        # FIRST TURN -- not at construction and not at start(). This fake
        # used to hand the id out in __init__, which made `guest.conv_id`
        # true the moment a client existed and hid a whole class of bug: the
        # bridge writing per-conversation state at a moment when production
        # has no id to write it against.
        #
        # Resuming keeps the id; forking mints a new one. That is what the
        # effort fork is FOR, and a fake that kept the id through a fork
        # would pass a test asserting the forked conversation inherited its
        # settings when production had lost them.
        self._pending_id = (self.resume if (self.resume and not self.fork)
                            else f"fake-session-{FakeSession._seq}")
        self.session_id = None

    async def start_turn(self):
        """What the SDK sends at the top of the first turn.

        Public, because a test that drives the bridge's internals directly --
        `_ensure_work_dir`, a SETWORK frame -- reaches code that asks which
        conversation it is for, and in production nothing reaches that code
        before this message has been seen. Idempotent.
        """
        if self.session_id is not None:
            return
        self.session_id = self._pending_id
        if self.on_system:
            await self.on_system("init", {"session_id": self.session_id})

    def ask(self, tool_name="Bash", args=None, **ctx_kw):
        """Spawn a permission request the way the SDK does: a detached task.

        Returns the task, so a test can cancel it and assert the guest still
        gets its PERMEND.
        """
        ctx = FakeCtx(**ctx_kw)

        async def run():
            # The init message is the FIRST thing in the SDK's stream, so
            # nothing -- no text, no tool call, no permission request --
            # reaches the bridge before the conversation has an id. A fake
            # that let a permission request in first would leave
            # `_ensure_work_dir` with no id to name a folder against, which
            # is a state production cannot produce.
            await self.start_turn()
            return await self.can_use_tool(
                tool_name, args or {"command": "vol c:"}, ctx)

        return asyncio.create_task(run())

    async def use_tool(self, name="Bash", args=None):
        """Report a tool call the way the SDK does, so the bridge's own
        `on_tool` runs -- including the work-folder commitment."""
        await self.start_turn()          # ordered as the SDK orders it
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
        self.busy = True
        try:
            # The id arrives here, ahead of any output, exactly as it does from
            # the CLI.
            await self.start_turn()
            self.last_prompt = prompt
            self.last_prompt_messages = []
            # A command the CLI resolves locally streams nothing back. Modelled
            # rather than assumed away: the bridge's "returned nothing" notice is
            # only reachable through a turn that really does produce no output.
            if getattr(self, "silent", False):
                return Turn(ok=True, subtype="success", cost_usd=0.0,
                            num_turns=1, duration_ms=10)
            display_text = prompt
            if hasattr(prompt, "__aiter__"):
                collected = []
                async for item in prompt:
                    collected.append(item)
                self.last_prompt_messages = collected
                parts = []
                for msg in collected:
                    content = msg.get("message", {}).get("content", "")
                    if isinstance(content, str):
                        parts.append(content)
                    elif isinstance(content, list):
                        for b in content:
                            if isinstance(b, dict):
                                if b.get("type") == "text":
                                    parts.append(b.get("text", ""))
                                elif b.get("type") == "image":
                                    parts.append("[image]")
                display_text = " ".join(parts)
            if self.on_text:
                await self.on_text(f"echo: {display_text}\n")
            return Turn(ok=True, subtype="success", cost_usd=0.01,
                        num_turns=1, duration_ms=5)
        finally:
            self.busy = False

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
                      sess=None, hdrs=None, tools=None):
        r, w = await asyncio.open_connection("127.0.0.1", self.port)
        # Fresh connection means the bridge resets its compressor, so reset
        # in lockstep. Same for the frame reader.
        self.unzip = StreamDecompressor()
        self._readers.pop(id(r), None)
        self._pending.pop(id(r), None)
        f = hello(secret, sess=sess)
        if tools is not None:
            # HELLO's BODY is the machine's tool inventory. Passing it here
            # rather than through a later frame is the whole design: _attach
            # builds the session -- and freezes the system prompt -- before
            # _pump reads anything else.
            f.body = tools
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
    # The conversation store must not touch the real one in $HOME, and each
    # test needs to start from an empty one or the chat list depends on what
    # ran before it. Its directory is also where the three legacy files are
    # looked for, so pointing it at tmp_path isolates those too.
    monkeypatch.setenv("GASTON_STATE", str(tmp_path / "conversations.json"))

    server = None
    b = None
    for _ in range(60):
        _next_port[0] += 1
        port = _next_port[0]
        b = Bridge("s", host="127.0.0.1", port=port)
        try:
            server = await asyncio.start_server(b.connection.on_client, "127.0.0.1", port)
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


async def _recv(harness, reader, type_, timeout=5.0):
    """recv_until returns the whole batch; the frame we waited for is last."""
    return (await harness.recv_until(reader, {type_}, timeout))[-1]


async def _connect_and_ask(harness, **ctx_kw):
    """Connect a guest, raise one prompt, and return (reader, writer, task)."""
    reader, writer = await harness.connect()
    await _recv(harness, reader, "OPTIONSEND")
    task = harness.bridge.connection.guest.claude.ask(**ctx_kw)
    return reader, writer, task
