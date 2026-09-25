"""No two things on screen at once may claim the same Alt+key.

A menu-bar mnemonic beats a child button's, silently. The permission bar's
buttons were created as "&Allow" / "A&lways" / "&Deny" with a comment saying
"A/L/D avoid the menu bar's F/C/M/E/H" -- true when it was written, and false
once app/optmenu.cpp added "Mo&de" and "Comm&ands", which took D and A.

So Alt+A opened the Commands menu and Alt+D opened the Mode menu, and two of
the bar's three answers could not be reached from the keyboard at all. Found
by pressing Alt+A on the real guest with a permission prompt up. It matters
because the bar no longer takes focus when the user is typing (audit 01.1) --
that change is only safe if the keyboard route it leaves behind works.

This is a grep, in the style of the project's other cross-file checks: the
collision is between two files that have no reason to read each other, which
is exactly the shape a comment cannot keep true.
"""

import os
import re

import conftest

WIN = os.path.join(conftest.REPO, "win", "app")


def _mnemonics(text, pattern):
    """Alt-keys claimed by strings matching `pattern`, upper-cased."""
    out = {}
    for lit in re.findall(pattern, text):
        m = re.search(r"&(\w)", lit)
        if m:
            out.setdefault(m.group(1).upper(), []).append(lit)
    return out


def _read(name):
    with open(os.path.join(WIN, name), encoding="utf-8", errors="replace") as fh:
        return fh.read()


def menu_bar_mnemonics():
    """Top-level menu-bar entries, from both files that append to it."""
    claimed = {}
    for src in ("main.cpp", "optmenu.cpp"):
        text = _read(src)
        for lit in re.findall(r'AppendMenuA\(\s*bar\s*,[^;]*?"([^"]*&[^"]*)"',
                              text, re.S):
            m = re.search(r"&(\w)", lit)
            if m:
                claimed.setdefault(m.group(1).upper(), []).append(
                    f"{src}: {lit}")
    return claimed


def bar_button_mnemonics():
    """The permission bar's buttons, plus the captions show() writes over
    them from the descriptor table -- both have to be collision-free, because
    the table's text replaces the constructor's on every push."""
    text = _read("bar.cpp")
    claimed = {}
    for lit in re.findall(r'CreateWindowExA\(0,\s*"BUTTON",\s*"([^"]+)"', text):
        m = re.search(r"&(\w)", lit)
        if m:
            claimed.setdefault(m.group(1).upper(), []).append(f"button {lit}")
    for lit in re.findall(r'\{\s*BAR_\w+\s*,\s*\d+\s*,\s*([^}]*)\}', text):
        for cap in re.findall(r'"([^"]*&[^"]*)"', lit):
            m = re.search(r"&(\w)", cap)
            if m:
                claimed.setdefault(m.group(1).upper(), []).append(f"table {cap}")
    return claimed


def test_the_menu_bar_was_found():
    """A regex that matches nothing would make every test below vacuous."""
    menu = menu_bar_mnemonics()
    assert len(menu) >= 6, f"only found {sorted(menu)}; the regex has drifted"
    # The ones this project has had from the start.
    assert {"F", "C", "H"} <= set(menu)


def test_the_bar_buttons_were_found():
    bar = bar_button_mnemonics()
    assert len(bar) >= 3, f"only found {sorted(bar)}; the regex has drifted"


def test_the_menu_bar_has_no_internal_collision():
    menu = menu_bar_mnemonics()
    dupes = {k: v for k, v in menu.items() if len(v) > 1}
    assert not dupes, f"two menu-bar entries claim the same Alt-key: {dupes}"


def test_no_bar_button_is_shadowed_by_the_menu_bar():
    """The failure this test exists for: Alt+A opening Commands, not Allow."""
    menu = menu_bar_mnemonics()
    bar = bar_button_mnemonics()
    clash = {k: (bar[k], menu[k]) for k in bar if k in menu}
    assert not clash, (
        "a permission-bar button shares its Alt-key with a menu-bar entry, "
        "and the menu wins -- so that button cannot be reached from the "
        f"keyboard: {clash}")


def test_the_descriptor_table_keeps_its_mnemonics():
    """show() writes `primary` over the button caption with SetWindowTextA.

    A table entry with no '&' silently removes the mnemonic the constructor
    set up, which is invisible until someone tries the keyboard.
    """
    text = _read("bar.cpp")
    rows = re.findall(r'\{\s*(BAR_\w+)\s*,\s*\d+\s*,\s*"([^"]+)"', text)
    assert rows, "the descriptor table was not found; the regex has drifted"
    for kind, primary in rows:
        assert "&" in primary, (
            f"{kind}'s primary caption {primary!r} has no mnemonic, so "
            "pushing this kind strips the button's Alt-key")
