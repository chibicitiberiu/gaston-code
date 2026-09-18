"""SyncCoordinator: the one thing that outlives a `Pass`.

The audit's #1.4 (docs/audit/02-bridge-python.md) sketches a much larger
object -- the piece that decides when a pass runs, what the user and the
agent are told, when the halt bar goes up, and who may write `base.json`.
That is `server.py`'s `run_sync`/`sync_status_text`/`sync_resolve` and
friends today, and moving all of it is out of scope here (02.43 records
that as future work, same as the audit's own "this is also the piece R7
needs" note). What forced this type to actually exist is narrower: two
separate passes independently left something where it was, because the
object that should own it did not exist.

1. `GuestSession`'s halt trio (`halt_id`, `halt_paths`, `halt_text`) used to
   live on `GuestSession` as a derived VIEW of `base.latched` -- the 02.42
   comment that used to sit on those fields explains why extracting a
   `GuestHalt` there would have moved three fields sideways without fixing
   anything: `Bridge._reconcile_halt` already derives them at every attach
   and every pass. What needed a home was the derivation and the fields
   together, not either alone.
2. `Bridge._agent_notices` is a Bridge-lifetime cache read between passes by
   `Bridge._with_sync_preamble`, on every turn -- see the module docstring
   on `bridge/files/reporter.py` for why `Reporter`, built fresh per
   `Pass`, cannot own it.

Both want an object that outlives a `Pass` and is Bridge-lifetime. That
second part is not a stretch for the halt trio despite the name "guest"
state: `Bridge._guest` is created once per Bridge process, on the first
`_attach`, and reused for every reconnect after that -- so a
`SyncCoordinator` built alongside `Bridge` itself has exactly the lifetime
the halt bar already had in practice. It is also one step towards the
per-client split R7 needs (audit §7.1), not a step away from it: "one
coordinator per client" is a constructor argument once the state that
needs splitting lives in one place instead of two.

Sending frames stays on `Bridge`: it owns the wire (`GuestLink`, `_send`)
and the connected guest. This class only decides and remembers; its
methods mutate state and return what the caller should put on the wire --
the same split `Pass` already uses against `SyncTransport` (02.19).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field


@dataclass
class SyncCoordinator:
    """Bridge-lifetime owner of the latch set's two derived views: the halt
    bar shown on the guest's screen, and the notices still owed to the
    agent. One instance per `Bridge`, constructed alongside it and never
    replaced -- matching `Bridge._guest`, which is also built once per
    process and reused across every reconnect.
    docs/audit/02-bridge-python.md 02.43.
    """

    #: The halt currently on the user's screen, if any. A halt is the
    #: bridge stopping the agent and handing the decision to the person at
    #: the keyboard; it clears when they press Continue, and never on a
    #: timer. This is not independent state -- it is a VIEW of
    #: `base.latched` on disk, and `Bridge._reconcile_halt` derives it at
    #: every attach and at the end of every pass (docs/audit/
    #: 02-bridge-python.md 02.21).
    halt_id: str = ""
    #: The paths the bar on screen was raised FOR. Continue answers those
    #: and nothing else: a conflict found while the bar is already up
    #: raises no new bar (there is only one), so the user is never shown
    #: it -- and releasing everything meant their press lifted the write
    #: refusal on a path they had not seen.
    halt_paths: frozenset = frozenset()
    #: What the bar says, kept so it can be put back after a reconnect. The
    #: id alone is not enough to redisplay it.
    halt_text: str = ""
    #: Notices waiting to ride in on the agent's next turn. Separate from
    #: `Baseline.pending_notice`, which is the USER's copy and is cleared
    #: when it reaches their screen: the two audiences need the same news
    #: and neither delivery implies the other (02.20). This is the
    #: in-memory cache; the durable half is `Baseline.pending_notice_agent`.
    agent_notices: list[str] = field(default_factory=list)

    def raise_halt(self, line: str, paths=()) -> str | None:
        """Idempotent while a bar is already showing.

        Returns the new id to send a HALT for, or None if nothing changed
        because one was already up -- the caller (`Bridge._raise_halt`)
        sends the frame; this only decides and remembers.
        """
        if self.halt_id:
            return None
        self.halt_id = f"h{uuid.uuid4().hex[:8]}"
        self.halt_paths = frozenset(paths)
        self.halt_text = line
        return self.halt_id

    def clear_halt(self) -> str | None:
        """Idempotent while no bar is showing.

        Returns the id to send HALTEND for, or None if there was nothing to
        clear -- the caller (`Bridge._clear_halt`) sends the frame.
        """
        if not self.halt_id:
            return None
        hid, self.halt_id = self.halt_id, ""
        self.halt_paths = frozenset()
        self.halt_text = ""
        return hid
