"""Unit tests for SessionService (Layer 6)."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bridge.agent import sessions as sesslist
from bridge.agent.conversations import Conversations
from bridge.services import session as session_service
from bridge.services.session import SessionService
from bridge.wire.protocol import Frame


class DummyGuest:
    def __init__(self, sess_id="guest-1", machine="guest-a"):
        self.sess_id = sess_id
        # GuestSession reads this off HELLO; a plain attribute here, since
        # the double has no HELLO to read.
        self.machine = machine
        self.conv_id = "conv-1"
        self.model = "claude-sonnet-4-6"
        self.effort = "high"
        self.perm_mode = "manual"
        self.work_dir = "/tmp/work"
        self.work_guest = "C:\\WORK"
        self.first_prompt = "hello"
        self.pending_resume = ""
        self.claude = None
        self.turn_lock = asyncio.Lock()
        self.perms = MagicMock()
        self.exec_pending = {"exec-1": MagicMock()}
        # Composed like the real GuestSession: the view owns the paginator.
        self.view = MagicMock()
        self.view.pages.count = 2
        self.view.pages.all_text.return_value = "Turn 1\nHello World\n"
        self.link = MagicMock()
        self.link.send_export = AsyncMock()


@pytest.fixture
def mock_deps(tmp_path):
    connection = MagicMock()
    connection.send = AsyncMock(return_value=True)
    presentation = MagicMock()
    presentation.emit_text = AsyncMock()
    presentation.flush_text = AsyncMock()
    presentation.write_block = AsyncMock()
    presentation.send_page = AsyncMock()
    workspace = MagicMock()
    workspace.work_dir = MagicMock(return_value=str(tmp_path / "work"))
    workspace.stored_work_dir = MagicMock(return_value=str(tmp_path / "work"))
    workspace.guest_path = MagicMock(return_value="C:\\WORK\\SUB")
    workspace.folder_name = MagicMock(return_value="SUB")
    workspace.retarget_perms = MagicMock()
    workspace.ensure_work_dir = AsyncMock()
    workspace.config = MagicMock()
    workspace.config.sync_dir = str(tmp_path / "sync")
    # The rows below ran in /tmp: this bridge's own, by SessionService.ours.
    workspace.config.cwd = "/tmp"
    options = MagicMock()
    options.clear_cache = MagicMock()
    options.effective_model = MagicMock(return_value="claude-sonnet-4-6")
    options.effective_effort = MagicMock(return_value="high")
    options.remember_settings = MagicMock()
    options.send_options = AsyncMock()
    guest_exec = MagicMock()
    guest_exec.kill_exec = MagicMock()
    guest_exec.fail_execs = MagicMock()
    conv = Conversations(tmp_path / "conv.json")
    return connection, presentation, workspace, options, guest_exec, conv


def test_conv_title(mock_deps):
    conn, pres, ws, opt, ge, conv = mock_deps
    svc = SessionService(conn, pres, ws, opt, ge, conv)
    guest = DummyGuest()

    with patch("bridge.agent.sessions.listing", return_value=[
        {"id": "conv-1", "title": "My Test Chat"},
        {"id": "conv-2", "title": "Other Chat"},
    ]):
        title = svc.conv_title(guest)
        assert title == "My Test Chat"

    with patch("bridge.agent.sessions.listing", return_value=[]):
        title = svc.conv_title(guest)
        assert title == "conv-1"

    guest.conv_id = ""
    title = svc.conv_title(guest)
    assert title == "Conversation"


def test_restore_settings(mock_deps):
    conn, pres, ws, opt, ge, conv = mock_deps
    svc = SessionService(conn, pres, ws, opt, ge, conv)
    guest = DummyGuest()

    conv.remember("conv-1", model="opus", effort="low", mode="plan")
    svc.restore_settings(guest, "conv-1")
    assert guest.model == "opus"
    assert guest.effort == "low"
    assert guest.perm_mode == "plan"


def test_abandon_pending(mock_deps):
    conn, pres, ws, opt, ge, conv = mock_deps
    svc = SessionService(conn, pres, ws, opt, ge, conv)
    guest = DummyGuest()

    svc.abandon_pending(guest)
    guest.perms.deny_all.assert_called_once_with(
        "rebuild", "The conversation changed before this was answered.")
    ge.kill_exec.assert_called_once_with(guest, "exec-1")
    ge.fail_execs.assert_called_once()


@pytest.mark.asyncio
async def test_send_sessions(mock_deps):
    conn, pres, ws, opt, ge, conv = mock_deps
    svc = SessionService(conn, pres, ws, opt, ge, conv)
    guest = DummyGuest()

    with patch("bridge.agent.sessions.listing", return_value=[
        {"id": "conv-1", "title": "Chat 1", "cwd": "/tmp", "group": ""},
        {"id": "conv-2", "title": "Chat 2", "cwd": "/tmp", "group": ""},
    ]):
        conv.add("conv-1")
        conv.add("conv-2")
        await svc.send_sessions(guest)
        # Check sessions_start, 2 rows, sessions_end were sent
        assert conn.send.call_count == 4


@pytest.mark.asyncio
async def test_on_export(mock_deps):
    conn, pres, ws, opt, ge, conv = mock_deps
    svc = SessionService(conn, pres, ws, opt, ge, conv)
    guest = DummyGuest()

    frame = Frame("EXPORT", {})
    await svc.on_export(guest, frame)
    guest.link.send_export.assert_called_once()
    args = guest.link.send_export.call_args[0]
    assert args[0] is guest
    assert isinstance(args[1], bytes)
    assert b"Gaston Code" in args[1]


# --- the chat list loses nothing to other people's work ---------------------
#
# `sesslist.listing()` is the newest sessions on the WHOLE MACHINE -- every
# project, every Claude Code session on the host -- and `send_sessions`
# narrows that to the ones this bridge started. Filtering after limiting
# meant a turn taken in any other checkout evicted one of the guest's chats.
# Measured when it was found: 50 rows fetched, 33 of ours in them, 54 of ours
# missing entirely, with "wine-prefix-reorganization" and "Break client into
# libs" occupying the top of the list.


def _machine_listing(mine_at, total=400):
    """A host where `mine_at` is the only conversation of ours, and every
    newer row belongs to somebody else's project."""
    rows = []
    for i in range(total):
        rows.append({"id": ("ours" if i == mine_at else f"theirs-{i}"),
                     "title": f"unrelated work {i}",
                     "cwd": "/tmp" if i == mine_at else "/home/dev/other",
                     "group": "Today"})
    return rows


def _rows_sent(conn):
    """The ids that actually went out as SESSION frames."""
    return [c.args[1].headers["id"] for c in conn.send.call_args_list
            if c.args[1].type == "SESSION"]


@pytest.mark.asyncio
async def test_an_old_conversation_of_ours_survives_newer_unrelated_ones(mock_deps):
    """The bug, at the layer that had it."""
    conn, pres, ws, opt, ge, conv = mock_deps
    svc = SessionService(conn, pres, ws, opt, ge, conv)
    guest = DummyGuest()
    conv.add("ours")

    rows = _machine_listing(mine_at=200)

    def fake_listing(cwd=None, limit=sesslist.FETCH_LIMIT):
        return rows[:limit]

    with patch("bridge.agent.sessions.listing", fake_listing):
        await svc.send_sessions(guest)

    assert _rows_sent(conn) == ["ours"], (
        "the one conversation this bridge started was 200 rows down a "
        "machine-wide listing; fetching 50 and filtering afterwards is what "
        "lost it")


@pytest.mark.asyncio
async def test_the_fetch_is_wide_enough_for_the_filter(mock_deps):
    """Not just the result: `send_sessions` must not be the place that
    reintroduces a narrow limit."""
    conn, pres, ws, opt, ge, conv = mock_deps
    svc = SessionService(conn, pres, ws, opt, ge, conv)
    asked = {}

    def fake_listing(cwd=None, limit=sesslist.FETCH_LIMIT):
        asked["limit"] = limit
        return []

    with patch("bridge.agent.sessions.listing", fake_listing):
        await svc.send_sessions(DummyGuest())
    assert asked["limit"] >= 200


@pytest.mark.asyncio
async def test_the_list_is_capped_after_filtering_not_before(mock_deps):
    """Bounded, still: the guest's listbox holds MAX_CHATS rows and counts
    its group headers against them.

    The cap is on OUR conversations, so a hundred of somebody else's do not
    use any of it up.
    """
    conn, pres, ws, opt, ge, conv = mock_deps
    svc = SessionService(conn, pres, ws, opt, ge, conv)

    rows = []
    for i in range(300):
        mine = i % 3 == 0
        rows.append({"id": f"c-{i}", "title": f"chat {i}", "cwd": "/tmp",
                     "group": "Today"})
        if mine:
            conv.add(f"c-{i}")

    with patch("bridge.agent.sessions.listing",
               lambda cwd=None, limit=sesslist.FETCH_LIMIT: rows[:limit]):
        await svc.send_sessions(DummyGuest())

    sent = _rows_sent(conn)
    assert len(sent) == session_service.DISPLAY_LIMIT
    assert sent[0] == "c-0", "newest first, and the newest is still ours"
    assert all(i in conv for i in sent), \
        "only conversations this bridge started"
    start = next(c.args[1] for c in conn.send.call_args_list
                 if c.args[1].type == "SESSIONS")
    assert start.headers["count"] == str(len(sent)), \
        "the count the guest is promised is the count it is sent"


# --- ...and the other half of "ours": WHICH MACHINE (BACKLOG.md §21) -------
#
# `mine` is written on the bridge host, so it answers "did this bridge start
# it" and nothing else. Point one bridge at two guests and each one's list
# fills with the other's conversations -- the other's drive letters, the
# other's compiler, the other's sync root in every folder path. The guest
# says which machine it is in HELLO and the filter narrows to it.


@pytest.mark.asyncio
async def test_a_second_guest_does_not_see_the_first_ones_chats(mock_deps):
    """The symptom, at the layer that has it: 87 conversations from a QEMU
    Win98 box in a different Win98 box's list."""
    conn, pres, ws, opt, ge, conv = mock_deps
    svc = SessionService(conn, pres, ws, opt, ge, conv)
    rows = []
    for i in range(10):
        rows.append({"id": f"qemu-{i}", "title": "from the VM", "cwd": "/tmp",
                     "group": "Today"})
        conv.add(f"qemu-{i}", "qemu-box")
    rows.append({"id": "real-1", "title": "from the real box", "cwd": "/tmp",
                 "group": "Today"})
    conv.add("real-1", "real-box")

    with patch("bridge.agent.sessions.listing",
               lambda cwd=None, limit=sesslist.FETCH_LIMIT: rows[:limit]):
        await svc.send_sessions(DummyGuest(machine="real-box"))

    assert _rows_sent(conn) == ["real-1"]


@pytest.mark.asyncio
async def test_conversations_from_before_the_header_stay_in_every_list(
        mock_deps):
    """The compatibility rule, end to end. 89 rows on this developer's box
    have no machine; they must not vanish the moment the field ships."""
    conn, pres, ws, opt, ge, conv = mock_deps
    svc = SessionService(conn, pres, ws, opt, ge, conv)
    rows = [{"id": "legacy", "title": "before the field", "cwd": "/tmp",
             "group": "Older"},
            {"id": "fresh", "title": "after it", "cwd": "/tmp",
             "group": "Today"}]
    conv.add("legacy")                 # no machine -- an upgraded row
    conv.add("fresh", "real-box")

    with patch("bridge.agent.sessions.listing",
               lambda cwd=None, limit=sesslist.FETCH_LIMIT: rows[:limit]):
        await svc.send_sessions(DummyGuest(machine="real-box"))
    assert _rows_sent(conn) == ["legacy", "fresh"]

    conn.send.reset_mock()
    with patch("bridge.agent.sessions.listing",
               lambda cwd=None, limit=sesslist.FETCH_LIMIT: rows[:limit]):
        await svc.send_sessions(DummyGuest(machine="qemu-box"))
    assert _rows_sent(conn) == ["legacy"], (
        "the legacy row belongs to whoever asks; the new one does not")


@pytest.mark.asyncio
async def test_a_client_that_sends_no_machine_keeps_the_old_wide_list(
        mock_deps):
    """A guest whose id could not be minted or written sends no header. It
    gets the list it has always had rather than an empty one."""
    conn, pres, ws, opt, ge, conv = mock_deps
    svc = SessionService(conn, pres, ws, opt, ge, conv)
    rows = [{"id": "a", "title": "x", "cwd": "/tmp", "group": "Today"},
            {"id": "b", "title": "y", "cwd": "/tmp", "group": "Today"}]
    conv.add("a", "qemu-box")
    conv.add("b", "real-box")

    with patch("bridge.agent.sessions.listing",
               lambda cwd=None, limit=sesslist.FETCH_LIMIT: rows[:limit]):
        await svc.send_sessions(DummyGuest(machine=""))
    assert _rows_sent(conn) == ["a", "b"]


@pytest.mark.asyncio
async def test_the_machine_filter_runs_before_the_cap_not_after(mock_deps):
    """The order the whole function turns on: FETCH WIDE, FILTER, CAP LAST.

    Another machine's conversations are newer and more numerous than ours.
    Capping before this filter -- or applying the machine filter to an
    already-capped list -- sends a short list, or none at all, while the
    guest's own chats sit further down the fetch.
    """
    conn, pres, ws, opt, ge, conv = mock_deps
    svc = SessionService(conn, pres, ws, opt, ge, conv)

    rows = []
    for i in range(200):            # newer, and not ours
        rows.append({"id": f"qemu-{i}", "title": "VM work", "cwd": "/tmp",
                     "group": "Today"})
        conv.add(f"qemu-{i}", "qemu-box")
    for i in range(80):             # ours, all of it past the display cap
        rows.append({"id": f"real-{i}", "title": "our work", "cwd": "/tmp",
                     "group": "Today"})
        conv.add(f"real-{i}", "real-box")

    asked = {}

    def fake_listing(cwd=None, limit=sesslist.FETCH_LIMIT):
        asked["limit"] = limit
        return rows[:limit]

    with patch("bridge.agent.sessions.listing", fake_listing):
        await svc.send_sessions(DummyGuest(machine="real-box"))

    assert asked["limit"] == sesslist.FETCH_LIMIT, "the fetch is still wide"
    sent = _rows_sent(conn)
    assert len(sent) == session_service.DISPLAY_LIMIT, \
        "the cap is applied last, to OUR rows"
    assert sent == [f"real-{i}" for i in range(session_service.DISPLAY_LIMIT)]


@pytest.mark.asyncio
async def test_a_conversation_another_bridge_ran_is_not_listed(mock_deps):
    """The conversation store is one file in $HOME for every bridge on the
    host. A second bridge with its own --cwd/--sync-dir listed the first
    one's conversations and resumed them in the wrong folders (2026-10-03).
    Where a session RAN decides; a row with no cwd is kept."""
    conn, pres, ws, opt, ge, conv = mock_deps
    svc = SessionService(conn, pres, ws, opt, ge, conv)
    for i in ("here", "there", "unknown"):
        conv.add(i)
    rows = [{"id": "here", "title": "a", "cwd": "/tmp/x/CONV", "group": ""},
            {"id": "there", "title": "b", "cwd": "/srv/other-bridge/work",
             "group": ""},
            {"id": "unknown", "title": "c", "cwd": "", "group": ""}]
    with patch("bridge.agent.sessions.listing",
               lambda cwd=None, limit=sesslist.FETCH_LIMIT: rows):
        await svc.send_sessions(DummyGuest())
    assert _rows_sent(conn) == ["here", "unknown"]
