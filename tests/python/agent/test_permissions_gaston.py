"""The sync layer's own directory is not the agent's to rewrite.

`.gaston/` lives INSIDE the working directory on purpose: a conflict copy and
the ancestor beside it have to be readable, or a three-way merge has nothing to
merge from. `off_limits` refuses what is outside cwd, so that same placement
left the staged copies writable -- and a staged copy is the last remaining
version of work the user's edit displaced.
"""
import logging
import os

from bridge.agent.permissions import (
    _PATH_TOOLS,
    MAX_BODY,
    off_limits,
    render_request,
    summary,
    telling_argument,
)


def test_reading_a_conflict_copy_is_allowed(tmp_path):
    """The reason it is inside cwd at all."""
    wd = str(tmp_path)
    mine = os.path.join(wd, ".gaston", "conflict", "a.txt", "mine.1")
    assert off_limits("Read", {"file_path": mine}, wd) is None
    assert off_limits("Grep", {"path": os.path.join(wd, ".gaston")}, wd) is None


def test_the_staged_copy_beside_the_folder_is_readable_wherever_the_agent_runs(
        tmp_path):
    """syncrun stages a conflict at the WORK FOLDER'S PARENT/.gaston. With a
    bridge --cwd apart from its --sync-dir the agent runs elsewhere, and its
    own mine.1 was refused as "on the proxy" -- it never merged (measured
    2026-10-03). Reads only; writes there are still refused."""
    wd = str(tmp_path / "work" / "RUNEXACT")
    os.makedirs(wd)
    mine = str(tmp_path / "work" / ".gaston" / "conflict" / "n.txt" / "mine.1")
    assert off_limits("Read", {"file_path": mine}, wd) is None
    assert off_limits("Write", {"file_path": mine}, wd) is not None


def test_writing_into_gaston_is_refused(tmp_path):
    wd = str(tmp_path)
    mine = os.path.join(wd, ".gaston", "conflict", "a.txt", "mine.1")
    for tool, key in (("Write", "file_path"), ("Edit", "file_path")):
        why = off_limits(tool, {key: mine}, wd)
        assert why is not None, f"{tool} into .gaston must be refused"
        # It must say where the merge DOES belong, or the agent retries.
        assert "sync root" in why


def test_an_ordinary_write_inside_cwd_is_still_allowed(tmp_path):
    wd = str(tmp_path)
    assert off_limits(
        "Write", {"file_path": os.path.join(wd, "machine", "a.txt")}, wd) is None


def test_a_name_merely_starting_with_gaston_is_not_refused(tmp_path):
    """commonpath, not startswith: `.gaston-notes` is an ordinary file."""
    wd = str(tmp_path)
    assert off_limits(
        "Write", {"file_path": os.path.join(wd, ".gaston-notes.md")}, wd) is None


def test_outside_cwd_is_still_refused_and_says_so_differently(tmp_path):
    wd = str(tmp_path / "work")
    os.makedirs(wd)
    why = off_limits("Read", {"file_path": "/etc/passwd"}, wd)
    assert why is not None and "on the proxy" in why


# --- Known gap: Bash is not a path tool --------------------------------------
#
# _PATH_TOOLS deliberately excludes Bash (permissions.py:224-226): "a command
# line cannot be parsed for paths reliably". Because off_limits looks the
# tool name up in _PATH_TOOLS before it ever inspects an argument
# (permissions.py:260-262), a Bash call is never refused on grounds of
# location -- not the outside-cwd containment check, not the `.gaston/`
# refusal. This is documented, deliberate, and NOT a bug these tests are
# proposing to fix: server.py:2619 and permissions.py:224/304 all note it in
# comments, and docs/PLAN.md 3.4d / docs/BACKLOG.md are explicit that the
# real close is a sandbox around Bash's execution, not a regex over command
# lines written inside a hardening pass.
#
# These tests exist so the gap is pinned instead of rediscovered as a
# surprise by the next audit. A test going green here is not an endorsement;
# it is a tripwire -- if `off_limits` ever starts refusing Bash, one of these
# will fail and whoever changed it should know they changed agent-visible
# behaviour on purpose.


def test_bash_is_absent_from_path_tools():
    """The root cause in one assertion: Bash has no key in _PATH_TOOLS, so
    off_limits returns None immediately (permissions.py:260-262) without
    ever looking at `command`. Pins the gap; the fix is a sandbox, not a
    parser for shell command lines."""
    assert "Bash" not in _PATH_TOOLS


def test_bash_reaching_outside_cwd_is_not_refused(tmp_path):
    """Pins a known gap. A path-shaped tool asked to reach `/etc` is refused
    (see test_outside_cwd_is_still_refused_and_says_so_differently above);
    the identical target through Bash is not, because off_limits never
    inspects `command`. Not a fix -- the fix is a sandbox around Bash's
    execution (docs/BACKLOG.md), not parsing this string."""
    wd = str(tmp_path / "work")
    os.makedirs(wd)
    why = off_limits("Bash", {"command": "cd /etc && cat hostname"}, wd)
    assert why is None


def test_bash_touching_gaston_is_not_refused(tmp_path):
    """Pins a known gap. Write/Edit into `.gaston/` are refused by
    _inside_gaston (see test_writing_into_gaston_is_refused above); the same
    write done through Bash is not, because Bash is absent from
    _PATH_TOOLS and _inside_gaston is never reached. Not a fix -- the fix is
    a sandbox around Bash's execution (docs/BACKLOG.md), not parsing this
    string."""
    wd = str(tmp_path)
    target = os.path.join(wd, ".gaston", "conflict", "a.txt", "mine.1")
    why = off_limits("Bash", {"command": f"echo pwned > {target}"}, wd)
    assert why is None


def test_bash_is_asymmetric_with_the_path_tool_it_stands_in_for(tmp_path):
    """The actual finding is not "Bash passes" in isolation -- read alone
    that could look like Bash is fine. It is that the SAME target is refused
    through a path-shaped tool and waved through Bash, for no reason other
    than which tool the model happened to pick. That asymmetry is the gap;
    closing it means sandboxing Bash, not special-casing these two calls."""
    wd = str(tmp_path / "work")
    os.makedirs(wd)

    outside = "/etc/passwd"
    assert off_limits("Read", {"file_path": outside}, wd) is not None
    assert off_limits("Bash", {"command": f"cat {outside}"}, wd) is None

    gaston_target = os.path.join(wd, ".gaston", "conflict", "a.txt", "mine.1")
    assert off_limits("Write", {"file_path": gaston_target}, wd) is not None
    assert off_limits(
        "Bash", {"command": f"echo x > {gaston_target}"}, wd) is None


# --- 02.15: one shared answer to "which argument matters" -------------------


def test_telling_argument_prefers_command_over_everything_else():
    args = {"file_path": "/tmp/x", "command": "vol c:", "prompt": "ignored"}
    assert telling_argument(args) == ("command", "vol c:")


def test_telling_argument_falls_back_through_the_shared_order():
    assert telling_argument({"url": "http://x"}) == ("url", "http://x")
    assert telling_argument({"pattern": "*.txt"}) == ("pattern", "*.txt")


def test_telling_argument_ignores_falsy_and_unlisted_values():
    assert telling_argument({"command": "", "path": None}) is None
    assert telling_argument({"selector": "#id"}) is None
    assert telling_argument({}) is None
    assert telling_argument("not a dict") is None


def test_render_request_and_summary_use_the_same_fallback_argument():
    """render_request's desc and summary's bar text used to pick the
    fallback argument from two independently-maintained lists in a
    different order. Now both go through telling_argument, so they can no
    longer disagree about which argument is the interesting one."""
    args = {"pattern": "*.log", "url": "http://example/"}

    class Ctx:
        display_name = None
        title = None
        description = None

    html = render_request("Grep", args, Ctx())
    line = summary("Grep", Ctx(), args)
    assert "*.log" in html
    assert "*.log" in line


class _NoCtx:
    display_name = None
    title = None
    description = None


def test_kill_names_the_process_it_will_end():
    """The bar read "kill" and nothing else: approving the end of a program
    without being told which one."""
    line = summary("mcp__machine__kill", _NoCtx(), {"pid": "FFF1A2B3"})
    assert "End a program" in line and "FFF1A2B3" in line
    forced = summary("mcp__machine__kill", _NoCtx(),
                     {"pid": "FFF1A2B3", "force": True})
    assert "forced" in forced


def test_an_input_call_is_described_step_by_step():
    """Approving one call approves the whole list, so both the bar and the
    block below it say what every step does."""
    args = {"steps": [{"focus": "Notepad"}, "hi[Enter]", {"close": "Notepad"}]}
    line = summary("mcp__machine__input", _NoCtx(), args)
    assert "keyboard and mouse" in line
    assert "bring Notepad to the front" in line
    html = render_request("mcp__machine__input", args, _NoCtx())
    assert "<ol>" in html and "close Notepad" in html


def test_a_move_names_both_ends_and_a_launch_its_command():
    line = summary("mcp__machine__move", _NoCtx(),
                   {"path": r"C:\A.TXT", "to": r"C:\B\A.TXT"})
    assert "Move or rename" in line and r"C:\B\A.TXT" in line
    line = summary("mcp__machine__launch", _NoCtx(), {"command": "notepad"})
    assert "Start a program" in line and "notepad" in line
    line = summary("mcp__machine__delete", _NoCtx(), {"path": r"C:\X"})
    assert "Delete" in line and "Recycle Bin" in line and r"C:\X" in line
    line = summary("mcp__machine__delete", _NoCtx(),
                   {"path": r"C:\X", "permanent": True,
                    "_preview": "a folder of 3 files"})
    assert "PERMANENTLY" in line and "3 files" in line


# --- MAX_BODY --------------------------------------------------------------


def test_oversized_argument_table_is_truncated_and_logged(caplog):
    """MAX_BODY exists so a PERMREQ body always stays well under the 64KB
    wire cap -- a Write with a huge `content`, a Task prompt or a long
    Edit.new_string would otherwise produce a frame GuestLink.send refuses
    outright. Losing the argument table is survivable; losing the
    connection is not, so render_request degrades instead of failing.

    That degrade must be visible to whoever is watching the bridge, not
    just to the person staring at the guest's screen -- an operator has no
    other way to notice a tool is routinely producing prompts this large.
    """
    class Ctx:
        display_name = "Write"
        title = None
        description = None

    # A single value is clipped to MAX_VALUE (400 chars) before it ever
    # reaches the table, so this needs many distinct arguments, not one huge
    # one, to push the assembled table itself past MAX_BODY.
    args = {f"arg{i}": "x" * 400 for i in range(40)}
    with caplog.at_level(logging.WARNING, logger="bridge.permissions"):
        html = render_request("Write", args, Ctx())

    assert len(html) <= MAX_BODY
    assert "too large to display" in html
    assert any("too large" in r.message for r in caplog.records), (
        "truncating the argument table must be logged, not silent")


def test_a_near_miss_names_the_folder_instead_of_saying_proxy():
    """`/home/x/work/notes.txt` is exactly as much "on the proxy" as
    `/etc/passwd` is, so the generic refusal explains nothing about why one
    is refused and its sibling directory allowed.

    Measured on the guest: an agent asked to create a file wrote to the
    shared root one directory above the conversation's folder, was told the
    path "is on the proxy, not on the user's machine", and spent a turn
    diagnosing a synchronisation fault instead of writing the file one
    directory down.
    """
    from bridge.agent import permissions as P

    why = P.off_limits("Write", {"file_path": "/home/x/work/HI.TXT"},
                       "/home/x/work/CREATEHI", shared="/home/x/work")
    assert why is not None
    assert "outside this conversation's folder" in why
    assert "/home/x/work/CREATEHI" in why, "it has to name where to write"
    assert "nothing to diagnose" in why
    assert "on the proxy, not on the user" not in why


def test_a_genuinely_foreign_path_still_gets_the_old_answer():
    """The near-miss wording must not swallow the case it was carved out of:
    /etc/passwd is not one directory too high, it is the wrong machine."""
    from bridge.agent import permissions as P

    why = P.off_limits("Read", {"file_path": "/etc/passwd"},
                       "/home/x/work/CREATEHI", shared="/home/x/work")
    assert why is not None
    assert "is on the proxy, not on the user's machine" in why
    assert "outside this conversation's folder" not in why


def test_inside_the_folder_is_still_allowed_through():
    from bridge.agent import permissions as P

    assert P.off_limits("Write", {"file_path": "/home/x/work/CREATEHI/a.txt"},
                        "/home/x/work/CREATEHI", shared="/home/x/work") is None


def test_the_agent_can_read_the_conflict_copies_it_is_told_to_merge():
    """The staged copies live OUTSIDE the working folder on purpose -- a copy
    inside it would sync straight back to the machine it was staged away
    from. But that put them outside cwd, where the near-miss refusal fires
    and tells the model "there is nothing to diagnose" about the one
    directory the conflict notice had just told it to open.
    """
    from bridge.agent import permissions as P

    staged = "/home/x/work/.gaston/conflict/notes.txt/mine.1"
    assert P.off_limits("Read", {"file_path": staged},
                        "/home/x/work/CHAT", shared="/home/x/work") is None


def test_but_it_still_cannot_write_there():
    """A staged copy is the only remaining copy of the agent's own work. The
    mechanism meant to preserve it must not be the thing that destroys it."""
    from bridge.agent import permissions as P

    staged = "/home/x/work/.gaston/conflict/notes.txt/mine.1"
    why = P.off_limits("Write", {"file_path": staged},
                       "/home/x/work/CHAT", shared="/home/x/work")
    assert why is not None
    assert ".gaston" in why


def test_the_write_guard_covers_the_location_that_is_actually_used():
    """It only ever guarded `<work_dir>/.gaston`, which nothing creates.
    `syncrun.Pass` stages at `dirname(work_dir)/.gaston`. Guarding the
    phantom is not a guard."""
    from bridge.agent import permissions as P

    for target in ("/home/x/work/CHAT/.gaston/conflict/a/mine.1",
                   "/home/x/work/.gaston/conflict/a/mine.1"):
        why = P.off_limits("Edit", {"file_path": target},
                           "/home/x/work/CHAT", shared="/home/x/work")
        assert why is not None, target


def test_a_read_outside_the_folder_that_is_not_staged_is_still_refused():
    """The carve-out is `.gaston` only -- it must not become a hole through
    which the whole shared root is readable."""
    from bridge.agent import permissions as P

    why = P.off_limits("Read", {"file_path": "/home/x/work/OTHER/notes.txt"},
                       "/home/x/work/CHAT", shared="/home/x/work")
    assert why is not None
    assert "outside this conversation" in why

