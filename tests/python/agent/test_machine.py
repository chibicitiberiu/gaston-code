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



# --- the inventory block ----------------------------------------------------
#
# What the machine has, resident in the prompt rather than behind a lookup.
# The reason it is resident is measured and is recorded in docs/PROGRESS.md:
# the agent that typed `&` on COMMAND.COM did so with complete confidence,
# and an agent that does not doubt `-l=nt` never goes looking for a note
# telling it about `-l=nt_win`. A gate whose failure mode is silence is not
# a gate. The cost is a bigger prompt on every request, which is why the
# tests below are as much about the budget as about the text.

from bridge.agent.machine import MAX_FACT_BLOCKS, tools_block
from bridge.agent.toolrules import Found

SHELL = Found("command.com", "shell", r"C:\WINDOWS\COMMAND.COM", False)
WATCOM = Found("watcom", "toolchain", r"C:\WATCOM", True)


def test_nothing_detected_says_nothing_at_all():
    """A heading over an empty table is a paragraph the model reads on every
    request to learn nothing -- and "nothing was detected" is a different
    claim from "nothing is installed"."""
    assert tools_block([]) == ""


def test_an_old_client_that_sends_no_body_gets_the_prompt_it_always_got():
    """Every deployed guest is this case until it is reflashed. The feature
    is dark, not broken."""
    out = system_prompt(HELLO, "/w", tools=[])
    assert "What is installed" not in out
    assert "Everything the user says refers to THEIR machine" in out


def test_the_inventory_names_what_was_found():
    out = tools_block([SHELL, WATCOM])
    assert r"C:\WINDOWS\COMMAND.COM" in out
    assert r"C:\WATCOM" in out
    assert "toolchain" in out


def test_every_row_carries_how_it_is_known():
    r"""A guess is not harmless here. The inventory is the only place a
    toolchain is named, so it is the one the agent reaches for: `turboc
    toolchain C:\TC` on a machine without it gets `tcc hello.c` for a Win32
    GUI app Turbo C 2.01 cannot build at all, then a failure the agent has
    been told not to go exploring about."""
    out = tools_block([SHELL, WATCOM])
    assert "not confirmed" in out
    assert "verified on this machine" in out


def test_the_block_permits_one_confirming_command_rather_than_forbidding_it():
    """Models verify things, and asking one to suppress a correct instinct
    does not work. Nine orientation calls collapsing to one is the win; nine
    to zero was never on offer."""
    out = tools_block([WATCOM])
    assert "One command to confirm" in out
    assert "survey" in out


def test_an_absent_row_is_not_read_as_an_absent_tool():
    out = tools_block([WATCOM])
    assert "absent means" in out


def test_a_note_is_written_against_the_root_that_was_found():
    r"""The whole point of detection is finding a toolchain somewhere other
    than the obvious place. A row saying `D:\OW` over a note saying
    `C:\WATCOM` is two authoritative sources disagreeing, with the more
    detailed one wrong."""
    out = tools_block([Found("watcom", "toolchain", r"D:\OW", True)])
    assert r"D:\OW\SETVARS.BAT" in out
    assert "C:\\WATCOM" not in out


def test_the_costliest_watcom_fact_is_resident():
    """`-l=nt_win`. Seven exec calls in the baseline, and it is not in
    wcl386's own help -- it is in a sample makefile."""
    assert "-l=nt_win" in tools_block([WATCOM])


def test_a_detected_tool_with_no_note_still_gets_its_row():
    """Detection may guess; notes may not. A tool nobody has measured is
    listed and left at that."""
    out = tools_block([Found("pkzip", "tool", r"C:\PKZIP\PKZIP.EXE", True)])
    assert "pkzip" in out


def test_at_most_one_toolchain_note_is_resident():
    """The cap is load-bearing: a fresh conversation's prompt is ~3,500
    characters and one toolchain note is ~600 of them. Without this the
    budget is honest only for as long as the feature is unsuccessful."""
    both = tools_block([
        WATCOM,
        Found("command.com", "shell", r"C:\WINDOWS\COMMAND.COM", True),
    ])
    assert both.count("Open Watcom 1.9 at") == 1
    assert MAX_FACT_BLOCKS == 1


def test_a_second_toolchain_is_listed_but_not_explained():
    """It goes in as an inventory row or it does not ship. If that starts
    costing calls, that is the trigger to look at skills -- not to raise the
    cap."""
    out = tools_block([WATCOM, Found("turboc", "toolchain", r"C:\TC", True)])
    assert r"C:\TC" in out          # listed
    assert out.count("measured on this machine, not inferred") <= 1


def test_the_shell_note_is_not_charged_against_the_toolchain_cap():
    """There is no "look it up first" moment for a shell: the first command
    fails before the agent could know to ask. So it is resident regardless of
    what else was found."""
    out = tools_block([SHELL, WATCOM])
    assert "Too many parameters" in out
    assert "-l=nt_win" in out


def test_a_machine_with_only_a_shell_gets_the_shell_note():
    out = tools_block([SHELL])
    assert "2>&1" in out


#: RAISED DELIBERATELY from 1,400, and the reasoning belongs here rather than
#: in a commit nobody reads again.
#:
#: 1,400 was the plan's estimate, made before the corpus had been read. Mining
#: 28 agent sessions and our own development history produced six more facts
#: that each trace to an observed failure -- `start` reporting nothing whether
#: it worked or not (six sessions claimed a window appeared with no evidence),
#: a running .exe locking itself against the linker, `call x.bat && y` silently
#: running neither. Dropping them to defend an estimate would be defending the
#: wrong thing.
#:
#: But a character count is a PROXY, and so was the gate first written here.
#: That gate said: if the prompt grows and exec calls do not fall, the text
#: comes back out. Measured, and it is the wrong gate -- calls did NOT fall
#: (5, against a 2-5 baseline), and the text is staying anyway, because the
#: run showed something the call count cannot see.
#:
#: Six earlier sessions ended by telling the user a window had appeared.
#: `start` returns no output whether the program ran, crashed or is missing,
#: so all six were asserting something they had no evidence for. With the
#: line in, the same task ended: "since it returns no output either way,
#: please check your screen to confirm". Same number of calls, one fewer
#: false claim.
#:
#: So the gate is now two-sided: text earns its place by removing calls OR by
#: removing a wrong statement, and anything that does neither on a task that
#: exercises it comes out. What has NOT been measured is whether the facts
#: added for failures this task never hits -- a locked .exe, `call x.bat &&`
#: -- pay for themselves; they are in on the strength of the transcripts they
#: came from, and that is a weaker warrant than the `start` line has.
#:
#: Breaching 1,400 is also the trigger the plan named for revisiting skills --
#: see docs/BACKLOG.md. This number going up again is that conversation, not
#: another edit here.
MAX_BLOCK_CHARS = 2800


def test_the_block_stays_inside_its_budget():
    """A fresh conversation has no committed folder, so it runs on the
    ~3,500-character branch of the prompt for its whole life. That is the
    denominator this block is spent against."""
    out = tools_block([SHELL, WATCOM])
    assert len(out) < MAX_BLOCK_CHARS, len(out)


def test_the_block_sits_above_the_hardware_section():
    """Order matters for what gets attended to. What is installed answers
    "how do I do this"; the hardware notes answer "do it sparingly"."""
    out = system_prompt(HELLO, "/w", tools=[SHELL, WATCOM])
    assert out.index("What is installed") < out.index("Keep the old machine")
