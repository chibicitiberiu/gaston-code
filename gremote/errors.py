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


class Unencodable(RemoteError):
    """A header value has a character the machine's codepage cannot hold.

    Raised HERE, before anything is sent. The frame codec would otherwise
    write `?` in its place and the guest would act on a different name --
    on the Athlon a `PUT` of `日本.txt` came back refused as `readonly`,
    which is both wrong and the guest being asked about `??.txt`.
    """

    def __init__(self, key: str, value: str, char: str, acp: str) -> None:
        self.key = key
        self.value = value
        self.char = char
        super().__init__(f"{key} {value!r}: {char!r} is not in the machine's "
                         f"codepage ({acp}), so nothing was sent")


class Cancelled(RemoteError):
    """Raised in the thread that was waiting for a cancelled request.

    The machine promised never to answer it, so waiting out the budget would
    be waiting for something that is not coming. This is what the promise
    looks like from the caller's side.
    """


class Disconnected(RemoteError):
    """The link went away, in either direction.

    Includes the one the heartbeat is for: a server that stopped hearing
    from us drops the connection after `heartbeat_timeout_s`, which is how a
    killed script stops holding the box.
    """


class Timeout(RemoteError):
    """No answer inside the budget. See the module docstring."""
