"""The machine description appended to the system prompt.

The model executes on a modern Linux box and, told nothing, answers as though
that is the machine in question. It is not.
"""

from bridge.agent.machine import describe, system_prompt

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
    assert "their version is kept" in low
    assert "never overwritten and never lost" in " ".join(low.split())
    assert "refusing your writes" in low


def test_prompt_gives_the_conflict_procedure_in_order():
    """Knowing a conflict happened is not the same as knowing what to do.

    The agent has to read three files, merge, and say what it kept. Leave any
    of it out and the likely behaviour is to rewrite the file -- which succeeds
    here, reaches nobody, and loses track of which version is which.
    """
    out = system_prompt(HELLO, "/work", sync_dir="/work/machine",
                        guest_root="C:\\WORK")
    assert "mine.N" in out and "base.N" in out
    assert "common ancestor" in out
    assert "Continue button" in out
    assert "different name" in out, "the obvious workaround is not ruled out"


def test_prompt_tells_the_agent_not_to_ask_for_a_second_confirmation():
    """Pressing Continue IS the confirmation, and it has already lifted the
    refusal by the time the agent is told anything.

    An agent that asks again puts a second dialog in front of someone who just
    answered the question, on a 640x480 screen, carrying no information the
    first one did not.
    """
    out = system_prompt(HELLO, "/work", sync_dir="/work/machine",
                        guest_root="C:\\WORK")
    flat = " ".join(out.split())
    assert "already lifted the refusal" in flat
    assert "do not ask them again" in flat.lower()
    assert "Merge. Keep both changes where they are independent" in flat
    # sync_resolve is still documented, but as the exception rather than a gate
    assert "should not normally be necessary" in flat


def test_prompt_says_the_conflict_message_is_not_from_the_user():
    """It arrives as a turn preamble, and a model that reads it as the user
    speaking will answer it conversationally instead of acting."""
    out = system_prompt(HELLO, "/work", sync_dir="/work/machine",
                        guest_root="C:\\WORK")
    assert "not from the user" in out


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


def test_the_conflict_path_is_the_one_that_exists():
    """The model was told `.gaston/conflict/<path>/mine.N` -- relative, so it
    resolved from the agent's cwd to <work_dir>/.gaston/conflict, a directory
    nothing creates. The real one is a level up
    (`syncrun.Pass._conflict_dir`), outside the mirror so a staged copy does
    not sync back to the machine it was staged away from.

    Told to read a path that has never existed, and a Read of the real one
    was refused for being outside the working copy. Both halves are fixed;
    this pins the prompt half.
    """
    out = system_prompt(HELLO, "/work/CHAT", sync_dir="/work/CHAT",
                        guest_root="C:\\Gaston\\CHAT")
    assert "/work/.gaston/conflict/<path>/mine.N" in out, out[:0] or "wrong path"
    assert "`.gaston/conflict/<path>/mine.N`" not in out


def test_the_conflict_path_agrees_with_the_sync_layer():
    """Computed two ways in two files. If they drift, the model is sent
    somewhere empty again and nothing else notices."""
    import os
    from bridge.files.syncrun import Pass

    workdir = "/work/CHAT"
    out = system_prompt(HELLO, workdir, sync_dir=workdir,
                        guest_root="C:\\Gaston\\CHAT")
    p = Pass.__new__(Pass)
    p._conflict_dir = os.path.join(
        os.path.dirname(workdir.rstrip("/")), ".gaston", "conflict")
    assert p.conflict_dir in out, (p.conflict_dir, out[:200])

