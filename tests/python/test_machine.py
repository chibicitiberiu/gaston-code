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


def test_prompt_states_the_edit_tools_are_relayed():
    out = system_prompt(HELLO, "/work")
    assert "/work" in out
    assert "synchronised" in out


def test_prompt_warns_about_the_hardware():
    """The point of the whole thing: an answer that ignores a 200MHz machine
    with single-digit MB free is the wrong answer for this user."""
    out = system_prompt(HELLO, "/work").lower()
    assert "memory" in out
    assert "install" in out


def test_prompt_separates_the_two_machines():
    out = system_prompt(HELLO, "/work")
    assert "different computers" in out
