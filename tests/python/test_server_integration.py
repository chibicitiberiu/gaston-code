"""End-to-end over the real wire protocol, with Claude mocked out.

Covers the parts that are awkward to test any other way: HELLO negotiation,
duplicate-client rejection, resume after a dropped connection, and pagination
round trips. Mocking the session keeps it deterministic and fast; the real
model is exercised separately by netcheck.exe.
"""

import asyncio

import pytest

from bridge.compress import StreamDecompressor
from bridge.protocol import Frame, FrameReader, MAX_FRAME, PROTO_VERSION, hello
from bridge.server import Bridge


class FakeSession:
    """Stands in for bridge.session.Session."""

    def __init__(self, **kw):
        self.on_text = kw.get("on_text")
        self.model = "fake-model"
        self.started = False
        self.interrupted = False

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
        from bridge.session import Turn
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
                      sess=None, from_seq=None):
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
async def harness(monkeypatch):
    """One bridge on its own port per test.

    Ports are handed out sequentially rather than reused, because a socket in
    TIME_WAIT from the previous test makes bind() fail intermittently and that
    reads as a flaky test rather than what it is.
    """
    import bridge.server as srv
    monkeypatch.setattr(srv, "Session", FakeSession)

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
