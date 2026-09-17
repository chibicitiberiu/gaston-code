"""The machine description appended to the system prompt.

The model executes on a modern Linux box and, told nothing, answers as though
that is the machine in question. It is not.
"""

from bridge.machine import describe, system_prompt

HELLO = {
    "os": "Windows 4.10 build 2222 A",
    "cpu": "x86 family 6 model 1, 1 cpu",
    "mem_kb": "32288",
    "mem_free_kb": "9928",
    "screen": "640x480x8",
    "disk_free_kb": "1746161",
}


def test_describe_reports_the_guest_not_the_bridge():
    out = describe(HELLO)
    assert "Windows 4.10" in out
    assert "31 MB total" in out and "9 MB free" in out
    assert "640x480x8" in out


def test_missing_fields_do_not_raise():
    """HELLO comes from a machine that may be older than the one tested, and
    a KeyError here would stop every session starting."""
    out = describe({})
    assert "unknown" in out


def test_nonsense_numbers_do_not_raise():
    assert "unknown" in describe({"mem_kb": "not a number"})


def test_prompt_does_not_promise_file_sync_when_there_is_none():
    """Sync is OFF unless a root is named, and the prompt must say so.

    This assertion has now been correct in both directions and wrong in both.
    It first claimed edits were "synchronised to the connected machine
    automatically" when no sync layer existed, so the model would have edited
    files on Linux and reported them as delivered. The fix was to say the
    opposite -- which became equally wrong the day sync shipped, because then
    it refuses a thing it can do.

    So it is not a fact about the release any more, it is a fact about THIS
    SESSION: what the prompt says depends on whether a sync root was given.
    """
    out = system_prompt(HELLO, "/work")
    assert "/work" in out
    assert "NOT yet copied" in out
    assert "there is no tool\nthat transfers them" in out


def test_prompt_names_the_mirrored_folder_when_sync_is_on():
    """"Your edits reach the user" without naming the folder is useless.

    `off_limits` confines the agent to cwd, and the sync root is a directory
    inside it. An edit outside that directory succeeds and achieves nothing,
    and the agent has no way to tell unless the prompt says which one it is.
    """
    out = system_prompt(HELLO, "/work", sync_dir="/work/machine",
                        guest_root="C:\\WORK")
    assert "machine/" in out
    assert "C:\\WORK" in out
    assert "NOT yet copied" not in out
    assert "never leaves this proxy" in out


def test_prompt_warns_about_names_that_cannot_cross():
    """The single most common way an agent achieves nothing and reports
    success: writing `aux.c`, or a path Windows cannot hold."""
    out = system_prompt(HELLO, "/work", sync_dir="/work/machine",
                        guest_root="C:\\WORK")
    assert "aux.c" in out
    assert "skipped and reported rather than renamed" in out


def test_prompt_says_the_users_copy_wins():
    """And that the refusal is mechanical, so the agent does not try to argue
    with it by writing again."""
    out = system_prompt(HELLO, "/work", sync_dir="/work/machine",
                        guest_root="C:\\WORK")
    low = out.lower()
    assert "machine's version is kept" in low
    assert "stop editing that file" in low
    assert "refuse your writes" in low


def test_prompt_does_not_promise_tools_that_do_not_exist():
    """It also advertised looking at the screen and listing windows, both of
    which are a much later phase."""
    out = system_prompt(HELLO, "/work").lower()
    assert "listing its windows" not in out
    assert "mcp__machine__" in out


def test_prompt_warns_about_the_hardware():
    """The point of the whole thing: an answer that ignores a 200MHz machine
    with single-digit MB free is the wrong answer for this user."""
    out = system_prompt(HELLO, "/work").lower()
    assert "memory" in out
    assert "install" in out


def test_prompt_separates_the_two_machines():
    out = system_prompt(HELLO, "/work")
    assert "different computers" in out
