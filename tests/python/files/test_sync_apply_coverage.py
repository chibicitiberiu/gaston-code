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


import os
import zlib

from bridge.files import paths as P
from bridge.status import SyncStatus


async def test_pull_advances_the_baseline_from_the_ack(tmp_path):
    """Invariant 1, for the pull direction.

    Re-scanning our own file to set the baseline would pick up a write the
    agent made during the transfer; the next pass then pulls the guest's older
    content over it with no conflict raised.

    Behavioral test (Audit 04.10): rather than inspecting source text slices
    for variable names, we execute Pass.run with a concurrent edit occurring
    immediately upon pull completion. The baseline must record the transfer's
    ack CRC (not the modified on-disk CRC), ensuring the subsequent diff
    produces a PUSH (preserving the agent's edit) rather than a PULL
    (clobbering it).
    """
    root = str(tmp_path / "tree")
    os.makedirs(root, exist_ok=True)
    state = str(tmp_path / "state")
    os.makedirs(state, exist_ok=True)

    guest_bytes = b"guest v1\n"
    guest_crc = zlib.crc32(P.normalise(guest_bytes, P.CLASS_TEXT))

    class MockTransport:
        has_pending_prompt = False

        async def yield_(self):
            pass

        async def get(self, rel_wire):
            return S.GetResult(status=SyncStatus.OK, crc=guest_crc,
                               cls=P.CLASS_TEXT, data=guest_bytes)

    class ConcurrentEditPass(Pass):
        async def scan_guest(self):
            return {
                "a.txt": S.Entry(type=S.Type.FILE, cls=P.CLASS_TEXT,
                                 crc=guest_crc, display="a.txt")
            }

        async def pull(self, k, rel, expect):
            ok = await super().pull(k, rel, expect)
            # Simulate agent concurrent edit right after the pull writes to disk
            with open(os.path.join(self.root, rel), "wb") as f:
                f.write(b"agent mid-transfer write\n")
            return ok

    p = ConcurrentEditPass(MockTransport(), None, root, state)
    res = await p.run()
    assert res["pulled"] == 1

    # Invariant 1: baseline must reflect the transfer ack, not the modified disk
    base_entry = p.base.rows.get("a.txt")
    assert base_entry is not None
    assert base_entry.crc == guest_crc, (
        "Baseline must match the ack CRC, not the modified disk file"
    )

    # Next pass diff: guest is unchanged from baseline, bridge is modified from baseline.
    # Therefore, diff must decide PUSH to preserve agent's edit, not PULL.
    bridge_rows = p.scan_bridge()
    guest_rows = await p.scan_guest()
    decisions = S.diff(guest_rows, bridge_rows, p.base)
    assert len(decisions) == 1
    assert decisions[0].act is S.Act.PUSH, (
        f"Expected PUSH so agent edit is preserved, got {decisions[0].act}"
    )
