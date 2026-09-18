"""Every action the diff can produce has code in `Pass.run` that applies it.

Three actions shipped with no handler at all -- `MKDIR`, `PULL`, and, for a
guest-only directory, the create half of `ADOPT`. Each was decided correctly by
`sync.py`, ranked correctly by `order()`, and then fell off the end of the
if/elif chain in `run()` and vanished. The pass reported success. On the real
Windows 98 box that looked like "7 files, 6 transferred" and then, once push
worked, like a machine whose edits simply never came back.

The whole point of `sync.py` being a pure decision function is that the state
table can be enumerated and tested -- which it is, exhaustively. None of that
helps if the applier quietly ignores a verdict, so the link between the two is
what needs a test.

`run` is checked by source inspection rather than by driving a pass per action.
That is deliberate: a fake guest would let a missing branch pass as "the guest
did not need to do anything", which is the exact failure being tested for.
"""
from __future__ import annotations

import inspect

from bridge.files import sync as S
from bridge.files.syncrun import Pass


def test_run_handles_every_act():
    src = inspect.getsource(Pass.run)
    missing = [a.name for a in S.Act
               if a is not S.Act.NOTHING and f"S.Act.{a.name}" not in src]
    assert not missing, (
        f"Pass.run has no branch for {missing}. The diff can produce these and "
        f"the pass will silently do nothing for the paths that resolve to them."
    )


def test_unhandled_act_is_loud():
    """The chain ends in an else that reports, not in a fall-through."""
    src = inspect.getsource(Pass.run)
    tail = src[src.rindex("elif dec.act"):]
    assert "else:" in tail, "no default branch: an unhandled act would vanish"
    assert "unhandled" in tail


def test_transfer_primitives_exist():
    """Both directions and the directory primitive, by name.

    A pass that can only push is not a sync layer, and the missing half was not
    visible from either side's logs.
    """
    for name in ("push", "pull", "mkdir"):
        assert callable(getattr(Pass, name, None)), f"Pass.{name} is missing"


def test_pull_advances_the_baseline_from_the_ack():
    """Invariant 1, for the pull direction.

    Re-scanning our own file to set the baseline would pick up a write the
    agent made during the transfer; the next pass then pulls the guest's older
    content over it with no conflict raised.
    """
    src = inspect.getsource(Pass.run)
    branch = src[src.index("S.Act.PULL"):src.index("S.Act.MKDIR")]
    assert "self._pulled[dec.key]" in branch, (
        "the PULL branch must take its baseline row from the transfer's ack, "
        "not from a fresh stat of the file we just wrote")
