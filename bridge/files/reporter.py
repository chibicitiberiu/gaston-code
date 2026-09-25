"""Reporter: what has already been said, to whom.

`first_report`, `note`, `notices_delivered` and `agent_notices_delivered`
gather the per-`Pass` half of notice-tracking: the fingerprinting logic, the
two durable queues (`pending_notice`/`pending_notice_agent`), and the
`reported` fingerprint map, all on one `Baseline` (docs/audit/
02-bridge-python.md #4.2, fix item 4).

The Bridge-lifetime half stays separate, on purpose. `Reporter` is built
fresh per `Pass` -- one reconciliation against one guest -- while the
in-memory agent-notice cache has to outlive any single pass, since
`TurnRunner.with_sync_preamble` reads it between passes on every turn.
Giving `Reporter` a reference to that state would mean either rebuilding it
identically every pass, or making `Reporter` itself Bridge-lifetime, which is
what `SyncCoordinator` is for instead (`bridge/synccoord.py`, reached via
`GuestSession.synccoord.agent_notices`). The two objects answer different
questions on different timelines and were never meant to merge.
"""

from __future__ import annotations

from . import sync as S


class Reporter:
    """Owns the two durable notice queues and the `reported` fingerprint
    map on one `Baseline`, and the one file both live in.

    One instance per `Pass`, over the same `Baseline` object `Pass.base`
    holds -- not a copy, so mutations either makes are visible to the
    other, the same way `Pass.base.acp`/`.tree_id`/`.guest_root` are set
    directly by `Pass` itself alongside this.
    """

    def __init__(self, base: S.Baseline, state_path: str) -> None:
        self.base = base
        self.state_path = state_path

    def first_report(self, key: str, reason: str, g, b) -> bool:
        """Is this situation new, or the same one we announced last pass?

        Fingerprinted on the reason AND the content of whichever side still
        holds the path, so "the user deleted main.c and you still have it"
        is said once -- but said again if one of them then changes it,
        which is a different fact about a different file.
        """
        fp = "|".join([
            reason,
            f"g:{g.cls}:{g.crc}" if g is not None else "g:-",
            f"b:{b.cls}:{b.crc}" if b is not None else "b:-",
        ])
        if self.base.reported.get(key) == fp:
            return False
        self.base.reported[key] = fp
        return True

    def note(self, text: str) -> None:
        """Record a notice durably BEFORE acting on it, for BOTH audiences.

        A crash between the baseline advance and the notification leaves the
        agent's work in conflict/ with nobody told, and the next pass sees
        agreement and says nothing.

        The user's copy (`pending_notice`) and the agent's
        (`pending_notice_agent`) are two separate durable queues, cleared by
        two separate deliveries -- `notices_delivered` and
        `agent_notices_delivered` -- because neither reaching its audience
        implies the other did (02.20).

        This still rewrites the WHOLE baseline -- fsync plus the `.prev`
        rename -- once per distinct notice. The audit judges that
        durability argument sound and only the granularity wrong (#4.2);
        moving the four methods here does not change that, and this pass
        does not weaken it to make it cheaper.
        """
        self.base.pending_notice.append(text)
        self.base.pending_notice_agent.append(text)
        self.base.save(self.state_path)

    def notices_delivered(self, texts: list) -> None:
        """Called once the caller has actually put them on the wire.

        The queue is PENDING, not a log. Nothing cleared it, so every notice
        ever raised was re-delivered on every pass -- the user would have seen
        the same conflict message every thirty seconds, which is worse than
        not reporting it, because it buries the next real one.

        Cleared by value rather than by emptying the list: a pass can raise a
        new notice while the previous batch is being written, and truncating
        would drop it with nobody told, which is the exact failure `note` is
        durable to prevent.
        """
        if not texts:
            return
        gone = list(texts)
        keep = []
        for t in self.base.pending_notice:
            if t in gone:
                gone.remove(t)
            else:
                keep.append(t)
        self.base.pending_notice = keep
        self.base.save(self.state_path)

    def agent_notices_delivered(self, texts: list) -> None:
        """`notices_delivered`'s twin for the agent's own durable queue.

        Called once the caller has actually handed these to the model in a
        turn preamble -- not when a pass merely produced them, and not the
        same moment the user's copy is cleared (02.20).
        """
        if not texts:
            return
        gone = list(texts)
        keep = []
        for t in self.base.pending_notice_agent:
            if t in gone:
                gone.remove(t)
            else:
                keep.append(t)
        self.base.pending_notice_agent = keep
        self.base.save(self.state_path)
