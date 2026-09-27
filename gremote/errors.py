"""What can go wrong, as types rather than as return codes.

The guest answers every request: `OK` or `ERR code=` for the short ops, and
`DATA` chunks then `END` for the streaming ones. Silence is never an answer
it gives on purpose, so a timeout here means the link or the box is in
trouble, not that the op is slow -- the guest's own budgets are shorter than
ours.
"""

from __future__ import annotations


class RemoteError(Exception):
    """Base for everything this package raises."""


class Refused(RemoteError):
    """The guest answered `ERR code=...`.

    `code` is the machine-readable token; the vocabulary is the guest's and
    is pinned against it by a test, so `except Refused as e: if e.code ==
    "busy"` is a supported thing to write.
    """

    def __init__(self, code: str, op: str = "", detail: str = "") -> None:
        self.code = code
        self.op = op
        self.detail = detail
        where = f" to {op}" if op else ""
        extra = f": {detail}" if detail else ""
        super().__init__(f"the machine refused{where} with {code!r}{extra}")


class Disconnected(RemoteError):
    """The link went away, in either direction.

    Includes the one the heartbeat is for: a server that stopped hearing
    from us drops the connection after `heartbeat_timeout_s`, which is how a
    killed script stops holding the box.
    """


class Timeout(RemoteError):
    """No answer inside the budget. See the module docstring."""
