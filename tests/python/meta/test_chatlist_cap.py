"""The chat list's bound is held on two sides, in two languages.

`win/app/chatlist.h` caps the listbox at `MAX_CHATS` rows and its own comment
says header rows count against it. `bridge/services/session.py` caps what it
sends at `DISPLAY_LIMIT`, and `bridge/agent/sessions._age_group` decides how
many header rows there will be. Nothing connected the three, so the bridge
could be raised to a round 64 and the oldest conversations would vanish on
the guest with no error anywhere -- which is the shape of the bug this cap
was added to stop, one layer further on.

Reads both sources as text. No Wine, no build.
"""

from __future__ import annotations

import ast
import inspect
import re
from pathlib import Path

import conftest

from bridge.agent import sessions as sesslist
from bridge.services import session as session_service

CHATLIST_H = Path(conftest.REPO) / "win" / "app" / "chatlist.h"


def _max_chats() -> int:
    src = CHATLIST_H.read_text()
    m = re.search(r"#define\s+MAX_CHATS\s+(\d+)", src)
    assert m, f"no #define MAX_CHATS in {CHATLIST_H}"
    return int(m.group(1))


def _age_groups() -> set:
    """Every bucket `_age_group` can name, read out of its returns.

    Parsed rather than called: the buckets are chosen by date arithmetic
    against today, and a test that drives it with timestamps would be
    asserting the calendar.
    """
    tree = ast.parse(inspect.getsource(sesslist._age_group))
    return {n.value.value for n in ast.walk(tree)
            if isinstance(n, ast.Return)
            and isinstance(n.value, ast.Constant)
            and isinstance(n.value.value, str)}


def test_the_bridge_never_sends_more_rows_than_the_guest_can_hold():
    headers = len(_age_groups())
    assert headers, "no age groups found; the parse above has gone stale"
    assert session_service.DISPLAY_LIMIT + headers <= _max_chats(), (
        f"{session_service.DISPLAY_LIMIT} conversations plus {headers} group "
        f"headers is more than MAX_CHATS ({_max_chats()}) in {CHATLIST_H}; "
        f"the guest would drop the tail of the list in silence")


def test_there_is_room_for_the_row_that_says_rows_are_missing():
    """The list ends with "N older not shown" when the cap bit, and that is a
    row like any other -- it counts against MAX_CHATS like any other.

    Measured on the real box the moment it shipped: 59 conversations plus four
    age groups plus the notice is exactly 64, so it fitted by one. On a day
    whose conversations span all five buckets it would not have, and the row
    that got dropped would have been the one telling the user rows were
    dropped. That is the failure this asserts against, not an off-by-one.
    """
    headers = len(_age_groups())
    assert session_service.DISPLAY_LIMIT + headers + 1 <= _max_chats(), (
        f"{session_service.DISPLAY_LIMIT} conversations plus {headers} group "
        f"headers leaves no room for the \"N older not shown\" row inside "
        f"MAX_CHATS ({_max_chats()}); when every age group is present the "
        f"notice is what the guest would silently drop -- lower DISPLAY_LIMIT "
        f"in {Path(session_service.__file__).name}, not this assertion")


def test_the_cap_is_worth_having():
    """A display cap below the fetch, or the fetch is the only bound and the
    wire carries rows the list cannot show."""
    assert session_service.DISPLAY_LIMIT < sesslist.FETCH_LIMIT
