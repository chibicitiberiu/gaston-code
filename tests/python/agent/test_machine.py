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
    assert "NOT copied to the connected machine on their\nown" in out
    # And the third correction, for the same reason as the first two: the
    # prompt used to say "there is no tool that transfers them", which stopped
    # being true when put_file shipped. Refusing a thing it can do is the same
    # class of mistake as promising one it cannot -- so what is asserted now
    # is that BOTH halves are said: the mirror is off, and one file at a time
    # can still be moved deliberately.
    assert "put_file" in out


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
CSCRIPT = Found("cscript", "tool", r"C:\WINDOWS\COMMAND\CSCRIPT.EXE", True, version="5.0")


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
    detailed one wrong.

    THE BODY MOVED (2026-10-01): it is fetched with `mcp__machine__toolnote`
    rather than printed here, so the property is asserted where the text now
    comes out -- `test_agent_tools.py`
    `test_the_note_is_rendered_against_the_root_that_was_found`. What this
    still owns is the half that stayed: nothing resident may name a root
    detection did not find.
    """
    out = tools_block([Found("watcom", "toolchain", r"D:\OW", True)])
    assert r"D:\OW" in out
    assert "C:\\WATCOM" not in out


def test_the_costliest_watcom_fact_is_reachable():
    """`-l=nt_win`. Seven exec calls in the baseline, and it is not in
    wcl386's own help -- it is in a sample makefile.

    It was resident until 2026-10-01 and is now one `toolnote` call away.
    What has to hold HERE is that the agent knows to make the call: a
    headline naming Watcom, and the tool named next to it.
    """
    out = tools_block([WATCOM])
    assert "Open Watcom C/C++ 1.9" in out
    assert "mcp__machine__toolnote" in out


def test_a_detected_tool_with_no_note_still_gets_its_row():
    """Detection may guess; notes may not. A tool nobody has measured is
    listed and left at that."""
    out = tools_block([Found("pkzip", "tool", r"C:\PKZIP\PKZIP.EXE", True)])
    assert "pkzip" in out


def test_no_toolchain_body_is_resident():
    """WAS `test_at_most_one_toolchain_note_is_resident`, and one was the
    bug. `found` arrives alphabetically, so on the machine this is built for
    -- a Sun JDK and Visual C++ 6 -- the JDK took the only slot and the C++
    note was dropped with nothing said. Raising the cap was never available:
    shell + msvc renders 3,707 characters against 2,800.

    So the number is 0 and the bodies are fetched. See MAX_FACT_BLOCKS in
    machine.py for the measurements.
    """
    both = tools_block([
        WATCOM,
        Found("command.com", "shell", r"C:\WINDOWS\COMMAND.COM", True),
    ])
    assert "Open Watcom 1.9 at" not in both
    assert MAX_FACT_BLOCKS == 0


def test_a_second_toolchain_is_listed_and_is_no_longer_a_dead_end():
    """It used to go in as an inventory row and nothing more -- a name with
    no way to learn anything about it. Now the row is still a row and the
    note behind it is one call away."""
    out = tools_block([WATCOM, Found("turboc", "toolchain", r"C:\TC", True)])
    assert r"C:\TC" in out          # listed
    assert "Open Watcom C/C++ 1.9" in out
    # turboc has no measured note, so it gets a row and no headline --
    # detection may guess, notes may not.
    assert out.count("mcp__machine__toolnote") >= 1


def test_the_shell_note_is_resident_whatever_else_was_found():
    """There is no "look it up first" moment for a shell: the first command
    fails before the agent could know to ask. So it is resident regardless of
    what else was found -- and it is the one note that did NOT move behind
    `toolnote`."""
    out = tools_block([SHELL, WATCOM])
    assert "Too many parameters" in out
    assert "Open Watcom C/C++ 1.9" in out


def test_a_machine_with_only_a_shell_gets_the_shell_note():
    out = tools_block([SHELL])
    assert "2>&1" in out


def test_cscript_is_advertised_alongside_the_toolchain():
    """It used to have its own resident slot. Now the shell note is resident
    and both of the others are headlines -- the point being that neither is
    dropped, which is what the old per-kind budgets did as soon as a machine
    had two of anything."""
    out = tools_block([SHELL, CSCRIPT, WATCOM])
    assert "Too many parameters" in out
    assert "Windows Script Host (CScript)" in out
    assert "Open Watcom C/C++ 1.9" in out
    assert "CSCRIPT.EXE (v5.0)" in out


def test_cscript_alone_with_shell_stays_inside_budget():
    out = tools_block([SHELL, CSCRIPT])
    assert len(out) < MAX_BLOCK_CHARS


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
#:
#: 2026-10-01: THAT CONVERSATION HAPPENED, and what moved was the shape
#: rather than the budget. Note BODIES are no longer resident -- they are
#: fetched with `mcp__machine__toolnote` and each detected tool costs one
#: headline line instead. So this number no longer bounds something
#: open-ended; it bounds the fixed prose, the inventory rows, the shell
#: note, and about 35 characters per measured tool.
#:
#: Measured the same day. The real target machine -- command.com, cscript
#: 5.0, Sun JDK 1.1.3, Visual C++ 6.0, PATH printed -- was 3,848 characters
#: WITH THE VISUAL C++ NOTE ALREADY SILENTLY DROPPED, and is 2,716 now with
#: every note reachable.
#:
#: 2800 -> 2900 IS THE ONE DELIBERATE RISE, and it buys a test rather than
#: slack. A machine carrying every note the profile tree has (the four
#: above plus Watcom) renders 2,849, which 2800 did not admit -- so the
#: budget could only ever have been asserted against a combination chosen
#: to fit, which is what the old 2800 was doing and is how the dropped note
#: went unnoticed in the first place. The cap is now set above the worst
#: case the tree can actually produce, and the worst case is what is
#: asserted. The remaining 51 characters are a little over one more
#: headline, so a sixth measured tool fits and a seventh does not -- and
#: this says so rather than a prompt nobody is counting.
#:
#: NOT COVERED, deliberately: a machine with two shells whose notes are
#: both resident. Only `command.com` has a note today; `4dos` with one
#: would add roughly 170 characters and breach. Shell notes are the one
#: thing that stays resident (there is no "look it up first" moment for a
#: shell), so that is a real decision and not an oversight -- it is just
#: not one any machine has forced yet.
MAX_BLOCK_CHARS = 2900


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


def test_a_machine_with_no_shell_is_told_so():
    """Without it every command was spawned bare in silence and `ver` failed
    to start with nothing saying why (BACKLOG 17)."""
    out = tools_block([WATCOM])
    assert "reported no shell" in out
    assert "reported no shell" not in tools_block([SHELL, WATCOM])


JDK = Found("jdk", "toolchain", r"C:\Java-WorkShop20\JDK", True, version="1.1.3")


def test_the_jdk_note_stays_inside_the_same_budget_watcom_does():
    """Measured 2026-10-01, with the shell note resident as it always is:
    watcom 2,774 and jdk 2,788 against the 2,800 cap -- 12 characters spare.

    That is deliberately thin and it is the whole reason this assertion
    exists rather than a comment. The JDK note is 960 rendered characters
    against Watcom's 964, so it does not move the worst case the cap was set
    from; anything added to it does, immediately and loudly.

    THIS IS NOT A VERDICT ON MAX_FACT_BLOCKS. It says one toolchain note
    fits, not that one is the right number: the machine this note was
    measured on also has Visual C++ 6, `jdk` sorts before `msvc`, and the
    note that loses the single slot to alphabetical order is the one
    carrying `cl` failing with no output at all. That combination is 2,915
    here -- over the cap with the C++ note already dropped -- and it is an
    open question, not something this test blesses.
    """
    out = tools_block([SHELL, JDK])
    assert len(out) < MAX_BLOCK_CHARS, len(out)


def test_the_jdk_note_is_rendered_against_the_root_that_was_found():
    r"""A standalone 1.1 install is nowhere near Java WorkShop's layout, and
    the note must not quote one while the inventory row names the other.
    It matters more here than for Watcom: the commands in this note are
    written out in full, because nothing on that machine puts the JDK on
    PATH.

    THE COMMANDS MOVED behind `mcp__machine__toolnote` on 2026-10-01 and
    the assertion about them went with them (`test_agent_tools.py`
    `test_the_note_is_rendered_against_the_root_that_was_found`). What is
    left here is the half that is still resident, and it is the half that
    would mislead: the root in the row, and no trace of the layout the
    profile happened to be measured on.
    """
    out = tools_block([SHELL, Found("jdk", "toolchain", r"C:\JDK1.1.8",
                                    True, version="1.1.8")])
    assert r"C:\JDK1.1.8" in out
    assert "Java-WorkShop20" not in out


# --- the two sides disagreeing about the mirror ------------------------------
#
# Observed on real hardware, 2026-10-01. The client created `C:\WORK` at
# startup and announced it in HELLO; the bridge had been started with no
# `--sync-dir`, so it mirrored nothing and never created a conversation
# folder. The log held "guest mirrors C:\WORK" and, two minutes later,
# "no folder is mirrored from this machine", with nothing joining them. The
# agent read the second half, concluded it had nowhere agreed to write, and
# put a file in the root of C: on somebody's actual disk.


def test_a_declared_share_with_no_mirror_is_named_as_a_disagreement():
    """The machine's own share is a fact the agent needs even when -- in
    fact ESPECIALLY when -- this bridge is not honouring it. Told only
    "nothing is mirrored", an agent has no destination at all."""
    out = system_prompt(HELLO, "/work", sync_dir=None, guest_root=None,
                        home="C:\\WORK")
    assert "C:\\WORK" in out
    assert "is not\nmirroring it" in out
    # And still the truth about the copying, which has not changed.
    assert "NOT copied to the connected machine on their\nown" in out


def test_the_disagreement_is_not_reported_as_a_fault():
    """"NAMING A FOLDER THAT DOES NOT EXIST YET sent one agent off to
    diagnose a sync fault instead of writing the file it had been asked
    for." The same trap, one branch over: a folder that exists and is not
    being copied reads like a broken mirror unless the prompt says it is
    the configuration."""
    out = system_prompt(HELLO, "/work", sync_dir=None, guest_root=None,
                        home="C:\\WORK")
    assert "do not go looking for a synchronisation\nproblem" in out


def test_a_drive_root_is_named_as_the_destination_that_is_never_right():
    """What nothing said before. `put_file` takes any path, the bar shows
    one clipped line, and `C:\\X.TXT` reads much like `C:\\WORK\\X.TXT` at
    640x480."""
    out = system_prompt(HELLO, "/work", sync_dir=None, guest_root=None,
                        home="C:\\WORK")
    assert "Never the root of a drive" in out
    assert "Where it goes: `C:\\WORK`" in out


def test_a_machine_that_declared_nothing_is_told_to_ask():
    """No share, so there is no folder to name -- and inventing one would
    be the bridge guessing at somebody's disk layout."""
    out = system_prompt(HELLO, "/work", sync_dir=None, guest_root=None,
                        home="")
    assert "named no shared folder" in out
    assert "ask where a file should go" in out
    assert "Never the root\nof a drive" in out


def test_the_mirrored_branch_is_untouched_by_any_of_this():
    """Three paragraphs, one true at a time. The sync-on text must not have
    acquired a sentence about a disagreement that cannot exist there."""
    out = system_prompt(HELLO, "/work", sync_dir="/work/machine",
                        guest_root="C:\\WORK", home="C:\\WORK")
    assert "is not\nmirroring it" not in out
    assert "named no shared folder" not in out


# --- two toolchains on one machine ------------------------------------------
#
# THE BUG THIS PINS. `MAX_FACT_BLOCKS = 1` rendered exactly one toolchain
# note, and `found` arrives alphabetically from `toolrules.inventory`, so on
# the user's real machine -- which has a Sun JDK and Visual C++ 6 -- `jdk`
# took the slot and the C++ note was dropped in silence. The fact lost that
# way was `cl` producing NO OUTPUT AT ALL when a directory is missing from
# PATH: a failure that reads as success.
#
# Raising the cap was not available either: shell 893 + msvc 1813 renders
# 3,707 characters against a 2,800 cap, before the inventory rows.
#
# So the shape changed instead. Bodies are no longer resident; each detected
# tool gets a one-line headline and the agent fetches the body with
# `mcp__machine__toolnote`. These tests are about the property that broke --
# neither toolchain is silently forgotten -- and not about which vehicle
# delivers it.

MSVC = Found("msvc", "toolchain", r"C:\Program Files\Microsoft Visual Studio\VC98",
             True, version="6.0")


def test_two_toolchains_both_reach_the_agent():
    """Neither is silently dropped. Before this, `jdk` sorted first and the
    `cl` note was never rendered on the one machine that has both."""
    out = tools_block([SHELL, JDK, MSVC])
    assert "jdk" in out and "msvc" in out
    # Each is named with enough that the agent knows a note exists for it.
    assert out.count("toolnote") >= 1
    assert "Sun JDK 1.1" in out
    assert "Microsoft Visual C++ 6.0" in out


def test_the_agent_is_told_how_to_fetch_a_note():
    """A capability nothing advertises will not be used. The pointer names
    the tool and says when to call it, or the headline is just trivia."""
    out = tools_block([SHELL, JDK, MSVC])
    assert "mcp__machine__toolnote" in out


def test_the_real_machines_combination_fits_the_budget():
    """shell + cscript + msvc + jdk, which is what is actually connected.
    Under the old shape this was 3,848 characters with the Visual C++ note
    ALREADY DROPPED.

    Measured with the PATH block, because that is what ships: every live
    session prints it, and a budget asserted against a shape the product
    does not produce is not a budget. 2,710 as at 2026-10-01.
    """
    path = [r"C:\WINDOWS", r"C:\WINDOWS\COMMAND"]
    out = tools_block([SHELL, CSCRIPT, JDK, MSVC], path=path)
    assert len(out) < MAX_BLOCK_CHARS, len(out)
    assert len(tools_block([SHELL, CSCRIPT, JDK, MSVC])) < MAX_BLOCK_CHARS


def test_the_budget_is_asserted_against_the_worst_case_the_tree_can_make():
    """Not against a combination chosen to fit, which is how the old cap
    held: 2800 was true of `shell + watcom` and false of the machine the
    product is actually for.

    So this is every profile in the tree that has a note, at once, with the
    longer `basis` wording and the PATH block. 2,849 as at 2026-10-01.
    A sixth measured tool still fits and a seventh does not, and this is
    where that is said rather than in a prompt nobody is counting.
    """
    every = [SHELL, CSCRIPT, JDK, MSVC,
             Found("watcom", "toolchain", r"C:\WATCOM", True)]
    out = tools_block(every, path=[r"C:\WINDOWS", r"C:\WINDOWS\COMMAND"])
    # Every one of them advertised, none of them expanded.
    for headline in ("Windows Script Host (CScript)", "Sun JDK 1.1",
                     "Microsoft Visual C++ 6.0", "Open Watcom C/C++ 1.9"):
        assert headline in out, headline
    assert len(out) < MAX_BLOCK_CHARS, len(out)


def test_the_shell_note_is_still_resident_in_full():
    """Not moved behind the fetch. There is no "look it up first" moment for
    a shell: the first command fails before the agent knows to ask."""
    out = tools_block([SHELL, JDK, MSVC])
    assert "Too many parameters" in out
    assert "2>&1" in out


# --- a note gaining versions must not cost the resident block a character ---
#
# The budget above has 51 characters of slack against the worst case the tree
# can produce, and `toolnote` grew a `version` parameter on 2026-10-01 so a
# profile can carry a body per release. That is deliberately all FETCHED
# text: the resident side is one headline per tool and a pointer, and the
# headline comes from the base `[note]` whatever else the profile documents.
#
# Said here rather than trusted, because the failure is not a crash. Adding
# `[[note.variant]]` to msvc.toml and quietly spending the slack would show
# up as the block being truncated on the one machine this is built for, with
# nothing in the logs.

from bridge.agent import toolnotes as _tn


def test_a_profile_with_several_versions_costs_the_prompt_nothing(monkeypatch):
    before = tools_block([SHELL, JDK, MSVC],
                         path=[r"C:\WINDOWS", r"C:\WINDOWS\COMMAND"])
    base = _tn.NOTES["jdk"]
    monkeypatch.setitem(_tn.NOTES, "jdk", _tn.Note(
        title=base.title, measured=base.measured, body=base.body,
        default_version=base.default_version,
        headline_text=base.headline_text, for_version="1.1",
        variants=(_tn.Body("1.2", "Sun JDK 1.2", "a 1.2 machine, 2026-10-01",
                           "A whole second body, " + "x" * 2000),
                  _tn.Body("1.3", "Sun JDK 1.3", "a 1.3 machine, 2026-10-01",
                           "And a third, " + "y" * 2000))))
    after = tools_block([SHELL, JDK, MSVC],
                        path=[r"C:\WINDOWS", r"C:\WINDOWS\COMMAND"])
    assert after == before


def test_the_resident_headline_is_the_base_notes(monkeypatch):
    """Not the newest variant's. A machine reporting 1.3 would otherwise get
    a headline for a version and a note body for another, which is the same
    two-authoritative-sources-disagreeing shape as a note quoting the wrong
    root -- and the inventory row is the thing the agent reaches for."""
    base = _tn.NOTES["jdk"]
    monkeypatch.setitem(_tn.NOTES, "jdk", _tn.Note(
        title=base.title, measured=base.measured, body=base.body,
        default_version=base.default_version,
        headline_text=base.headline_text, for_version="1.1",
        variants=(_tn.Body("1.3", "Sun JDK 1.3", "a 1.3 box, 2026-10-01",
                           "The 1.3 body."),)))
    out = tools_block([SHELL, JDK])
    assert "Sun JDK 1.1" in out
    assert "Sun JDK 1.3" not in out
