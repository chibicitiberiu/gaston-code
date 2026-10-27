"""Unit tests for WorkspaceService."""

from unittest.mock import MagicMock

import pytest

from bridge.files import ignores as I
from bridge.services.config import BridgeConfig
from bridge.services.workspace import WorkspaceService


@pytest.fixture
def mock_guest():
    guest = MagicMock()
    guest.hello = {"sync_root": "C:\\WORK"}
    guest.work_dir = "/tmp/mirror/NOTES"
    guest.work_guest = "C:\\WORK\\NOTES"
    guest.conv_id = "test-conv-id"
    return guest


def test_work_dir_fallback(mock_guest):
    cfg = BridgeConfig(secret="sec", cwd="/base/cwd")
    ws = WorkspaceService(config=cfg)

    # When guest has work_dir
    assert ws.work_dir(mock_guest) == "/tmp/mirror/NOTES"

    # When guest has no work_dir, fallback to config.cwd
    mock_guest.work_dir = ""
    assert ws.work_dir(mock_guest) == "/base/cwd"


def test_guest_path_mapping(mock_guest):
    cfg = BridgeConfig(secret="sec")
    ws = WorkspaceService(config=cfg)

    assert ws.guest_path(mock_guest, "SUB") == "C:\\WORK\\SUB"
    assert ws.guest_work_root(mock_guest) == "C:\\WORK\\NOTES"


def test_sub_of(mock_guest):
    cfg = BridgeConfig(secret="sec", sync_dir="/tmp/mirror")
    ws = WorkspaceService(config=cfg)

    assert ws.sub_of(mock_guest) == "NOTES"


# --- naming a conversation's folder on the machine ---------------------------
#
# WHY THESE ARE HERE AT ALL. Until 2026-10-01 `name_work_dir` opened with
#
#     base = self.config.sync_dir
#     if not base:
#         return
#
# so a bridge started with no `--sync-dir` gave the conversation no folder on
# EITHER side -- and on real hardware the agent, with no destination it had
# been given, wrote a file into the root of C: on somebody's own machine.
#
# The two things that early return conflated:
#
#   * the MIRROR (`work_dir`) is a tree on this side with a baseline behind it
#     and deletion propagation through it. That needs `--sync-dir` and is
#     still gated on it.
#   * the WORKING DIRECTORY ON THE MACHINE (`work_guest`) needs none of that.
#     The client creates the folder it is told to use, under the home the user
#     configured in its own INI and which it declares in HELLO.
#
# Everything below is about the second one, and the sharp edge is the NAME: a
# conversation title is free text and the destination is FAT on Windows 98.

import dataclasses

from bridge.files import caps as C

#: A long-name, case-folding client: the names may be 32 characters, and
#: `Notes` and `NOTES` are still one directory.
LONGNAMES = dataclasses.replace(C.CONSTRAINED, shortnames=C.SHORTNAMES_NONE)


class FakeConv:
    """Just enough of `ConvStore` for the naming path: which names are owned,
    and a record of what was handed out."""

    def __init__(self, owned=()):
        self.owned = set(owned)
        self.folders = {}

    def taken(self):
        return set(self.owned)

    def set_folder(self, session_id, folder, guest_path=""):
        self.folders[session_id] = (folder, guest_path)
        self.owned.add(folder)


def _guest(title="", *, caps=C.CONSTRAINED, home="C:\\WORK",
           conv_id="a1b2c3d4-0000-4000-8000-000000000000"):
    g = MagicMock()
    g.hello = {"sync_root": home} if home else {}
    g.guest_sync_root = ""
    g.work_dir = ""
    g.work_guest = ""
    g.work_planned = ""
    g.first_prompt = title
    g.conv_id = conv_id
    g.caps = caps
    g.synccoord.agent_notices = []
    return g


def _ws(conv=None, sync_dir=None, **kw):
    cfg = BridgeConfig(secret="sec", sync_dir=sync_dir, cwd="/base/cwd")
    return WorkspaceService(config=cfg, conv=conv if conv is not None
                            else FakeConv(), **kw)


async def test_a_folder_on_the_machine_needs_no_mirror_on_this_side():
    """THE CORRECTION. The machine declares the home its user configured, the
    conversation gets its own folder inside it, and `--sync-dir` has nothing
    to do with either -- it decides whether there is a tree HERE to pair
    with, which is a separate question."""
    ws = _ws()
    g = _guest("Sky colour")
    await ws.name_work_dir(g)
    assert g.work_guest == "C:\\WORK\\SKYCOLOU"
    assert g.work_dir == "", "no mirror was asked for and none was invented"


async def test_the_mirror_half_is_still_gated_on_sync_dir(tmp_path):
    """And when there IS a `--sync-dir`, both halves happen as before."""
    ws = _ws(sync_dir=str(tmp_path))
    g = _guest("Sky colour")
    await ws.name_work_dir(g)
    assert g.work_guest == "C:\\WORK\\SKYCOLOU"
    assert g.work_dir == str(tmp_path / "SKYCOLOU")
    assert (tmp_path / "SKYCOLOU").is_dir()


async def test_no_folder_is_invented_when_the_machine_declared_no_home():
    """The one case where the honest answer is still "nowhere". A path chosen
    by this side would be a directory on somebody's real disk that nobody
    asked for -- the drive-root mistake one step tidier."""
    ws = _ws()
    g = _guest("Sky colour", home="")
    await ws.name_work_dir(g)
    assert g.work_guest == ""
    assert g.work_dir == ""


# --- sanitisation: the title is prose, the destination is FAT ----------------


async def test_characters_fat_refuses_are_dropped_from_the_title():
    """`< > : " \\ | ? *` and the separators. Taken from `caps.illegal`, not
    from a list written out here."""
    ws = _ws()
    g = _guest('List files in C:\\RETRO <dir> "x"|y?')
    await ws.name_work_dir(g)
    assert g.work_guest == "C:\\WORK\\LISTFILE"


async def test_a_title_longer_than_a_component_is_cut_to_the_declared_limit():
    """Eight characters on a volume that aliases, because an 8-character name
    with no extension IS its own 8.3 alias and so has nothing to collide
    with. Thirty-two on one that does not. Both from `caps`."""
    long = "Investigate the floppy controller timing problem"
    short = _ws()
    g = _guest(long)
    await short.name_work_dir(g)
    assert g.work_guest == "C:\\WORK\\INVESTIG"

    wide = _ws()
    g2 = _guest(long, caps=LONGNAMES)
    await wide.name_work_dir(g2)
    assert g2.work_guest == "C:\\WORK\\Investigatethefloppycontrollerti"
    assert len(g2.work_guest.rsplit("\\", 1)[-1]) == 32


async def test_a_reserved_device_name_never_becomes_a_folder():
    """`CON`, `AUX`, `NUL`, `COM1`.. are devices in EVERY directory on DOS and
    the extension does not save you. Opening one with nothing asserting DSR
    blocks with no timeout, which wedges the RPC worker -- a failure class
    that has already shipped once. A title that reduces to one falls back to
    the conversation id rather than producing no folder at all, which is what
    left the agent with nowhere to write."""
    for title in ("CON", "aux", "COM1", "lpt9", "prn", "nul"):
        ws = _ws()
        g = _guest(title)
        await ws.name_work_dir(g)
        name = g.work_guest.rsplit("\\", 1)[-1]
        assert name == "A1B2C3D4", (title, g.work_guest)

    # And `aux.txt` and `clock$` are NOT among them, which is the part that
    # has to be taken from `caps` rather than judged by eye. Neither the dot
    # nor the `$` can reach a folder name, so what is left is `AUXTXT` and
    # `CLOCK` -- ordinary names, and a better answer than throwing the title
    # away. The reserved check is on the STEM, so `AUX.TXT` would have been a
    # device and `AUXTXT` is not.
    for title, want in (("aux.txt", "AUXTXT"), ("clock$", "CLOCK")):
        ws = _ws()
        g = _guest(title)
        await ws.name_work_dir(g)
        assert g.work_guest == "C:\\WORK\\" + want


async def test_an_empty_or_all_punctuation_title_still_gets_a_folder():
    """"???", "....", a line of Cyrillic on a cp1252 machine. Each reduces to
    nothing a FAT volume will hold, and each used to produce NO folder --
    which is exactly the state the agent was in when it chose `C:\\`."""
    for title in ("", "   ", "???", "....", "---", "Привет"):
        ws = _ws()
        g = _guest(title)
        await ws.name_work_dir(g)
        assert g.work_guest == "C:\\WORK\\A1B2C3D4", title


async def test_a_trailing_dot_or_space_cannot_survive_the_name():
    """Windows strips both silently, so `NOTES.` and `NOTES` would be one
    directory and two conversations. Nothing but alphanumerics, `-` and `_`
    reaches the name, so this is structural rather than checked."""
    ws = _ws()
    g = _guest("Notes. ")
    await ws.name_work_dir(g)
    assert g.work_guest == "C:\\WORK\\NOTES"


# --- collisions: a shared folder is lost work, not untidiness ----------------


async def test_two_conversations_with_one_title_get_two_folders():
    ws = _ws(conv=FakeConv({"SKYCOLOU"}))
    g = _guest("Sky colour")
    await ws.name_work_dir(g)
    assert g.work_guest == "C:\\WORK\\SKYCOLO2"


async def test_the_disambiguating_tail_keeps_the_name_inside_the_limit():
    """The tail REPLACES the end of the stem. Appending would make nine
    characters on an 8.3 volume and the volume would invent its own name,
    which is the collision this is resolving, one layer down."""
    ws = _ws(conv=FakeConv({"SKYCOLOU", "SKYCOLO2", "SKYCOLO3"}))
    g = _guest("Sky colour")
    await ws.name_work_dir(g)
    name = g.work_guest.rsplit("\\", 1)[-1]
    assert name == "SKYCOLO4"
    assert len(name) == 8


async def test_a_collision_is_judged_under_the_clients_case_rule():
    """`taken()` compares raw strings, which is Python's rule. On a
    case-folding volume `Notes` and `NOTES` are ONE directory, so the second
    conversation's files would land on top of the first one's -- loss, not
    untidiness. A shortnames client uppercases and cannot show this; a
    long-name one can."""
    ws = _ws(conv=FakeConv({"Notes"}))
    g = _guest("NOTES", caps=LONGNAMES)
    await ws.name_work_dir(g)
    assert g.work_guest == "C:\\WORK\\NOTES2"


async def test_a_mirror_directory_already_there_also_counts_as_taken(tmp_path):
    """The store knows what conversations own; the mirror can hold a folder
    from a previous run of this bridge whose store has gone."""
    (tmp_path / "SKYCOLOU").mkdir()
    ws = _ws(sync_dir=str(tmp_path))
    g = _guest("Sky colour")
    await ws.name_work_dir(g)
    assert g.work_guest == "C:\\WORK\\SKYCOLO2"


async def test_nothing_is_named_when_every_candidate_is_owned():
    """Ninety-nine from the title, ninety-nine from the conversation id, and
    then it stops. Reusing one of them would put two conversations' files in
    one folder, which is the thing being prevented -- so the answer is no
    folder, and the agent is told so rather than handed a path that belongs
    to somebody else."""
    owned = {"SKYCOLOU"} | {f"SKYCOLO{n}" for n in range(2, 10)}
    owned |= {"SKYCOL" + str(n) for n in range(10, 100)}
    owned |= {"A1B2C3D4"} | {f"A1B2C3D{n}" for n in range(2, 10)}
    owned |= {"A1B2C3" + str(n) for n in range(10, 100)}
    ws = _ws(conv=FakeConv(owned))
    g = _guest("Sky colour")
    await ws.name_work_dir(g)
    assert g.work_guest == ""


# --- what the prompt is allowed to promise ----------------------------------


async def test_the_planned_folder_is_the_folder_that_gets_made():
    """The prompt is built when the session is and the folder is committed at
    the first tool call, so the prompt can only name the one that is coming.
    If the two could diverge the agent would hold a path that never gets
    made -- which, with no mirror, is the only path it is ever given."""
    ws = _ws()
    g = _guest("Sky colour")
    planned = ws.planned_guest_dir(g)
    assert planned == "C:\\WORK\\SKYCOLOU"
    # Another conversation takes the name in between. The plan still wins:
    # it is what the agent was told.
    ws.conv.owned.add("SKYCOLOU")
    await ws.name_work_dir(g)
    assert g.work_guest == planned


async def test_a_planned_folder_is_not_created_by_asking_for_it(tmp_path):
    """`planned_guest_dir` is read by prompt building, which happens for a
    conversation that may never call a tool. It must not make a directory."""
    ws = _ws(sync_dir=str(tmp_path))
    g = _guest("Sky colour")
    assert ws.planned_guest_dir(g) == "C:\\WORK\\SKYCOLOU"
    assert list(tmp_path.iterdir()) == []
    assert g.work_guest == ""


async def test_nothing_is_planned_when_the_machine_declared_no_home():
    ws = _ws()
    g = _guest("Sky colour", home="")
    assert ws.planned_guest_dir(g) == ""


async def test_a_committed_folder_is_what_planned_reports():
    ws = _ws()
    g = _guest("Sky colour")
    g.work_guest = "C:\\WORK\\REAL"
    assert ws.planned_guest_dir(g) == "C:\\WORK\\REAL"


async def test_a_work_root_that_leaves_no_room_names_no_folder():
    """`caps.maxpath` and `caps.dirreserve`, which a component-length check
    cannot see. A directory has to stop twelve characters short of MAX_PATH on
    Win32 so an 8.3 name still fits INSIDE it -- and a client that declares a
    work root long enough to swallow that reserve can hold no conversation
    folder at all. Saying so beats naming one the client cannot create, which
    puts the agent back where it started with a path that does not exist."""
    deep = "C:\\" + "\\".join("DIRNAME" + str(n) for n in range(32))
    ws = _ws()
    g = _guest("Sky colour", home=deep)
    await ws.name_work_dir(g)
    assert g.work_guest == ""


async def test_a_long_work_root_that_still_has_room_gets_its_folder():
    """The other side of the same check: refusing is for a root that really
    has no room, not for any root longer than the test machine's."""
    deep = "C:\\" + "\\".join("DIRNAME" + str(n) for n in range(20))
    ws = _ws()
    g = _guest("Sky colour", home=deep)
    await ws.name_work_dir(g)
    assert g.work_guest == deep + "\\SKYCOLOU"


async def test_a_new_folder_is_made_with_its_ignore_list_in_it(tmp_path):
    """The list reaches the user because it is a file in their own work
    folder, not a constant in a program on the other side of the link."""
    ws = _ws(sync_dir=str(tmp_path))
    g = _guest("Sky colour")
    await ws.name_work_dir(g)
    text = (tmp_path / "SKYCOLOU" / ".gastonignore").read_text()
    assert text.splitlines()[0] == I.MARKER
    assert "\ntemp/\n" in text


async def test_the_notice_says_where_the_list_is_only_when_one_was_written():
    """No mirror, nothing written, and nothing to point at -- a notice
    naming a file that is not there is worse than no notice."""
    wrote = []

    async def block(guest, html):
        wrote.append(html)

    ws = _ws(write_block=block)
    await ws.name_work_dir(_guest("Sky colour"))
    assert wrote and "gastonignore" not in str(wrote[0])


async def test_the_notice_points_at_the_list_when_there_is_one(tmp_path):
    wrote = []

    async def block(guest, html):
        wrote.append(html)

    ws = _ws(sync_dir=str(tmp_path), write_block=block)
    await ws.name_work_dir(_guest("Sky colour"))
    assert wrote and ".gastonignore" in str(wrote[0])


async def test_a_folder_that_already_has_a_list_keeps_it(tmp_path):
    """`on_setwork` points at a folder the user chose, which may be a tree
    they already ignore things in."""
    (tmp_path / "SKYCOLOU").mkdir()
    (tmp_path / "SKYCOLOU" / ".gastonignore").write_text("mine/\n")
    ws = _ws(sync_dir=str(tmp_path))
    g = _guest("Sky colour")
    await ws.name_work_dir(g)
    assert (tmp_path / "SKYCOLOU" / ".gastonignore").read_text() == "mine/\n"
